"""
Dedicated local speech-to-text (ASR) server with GPU support.

Runs faster-whisper (CTranslate2) on the shared GPU as ONE single-process
service so the model is loaded exactly once into VRAM. The FastAPI API has many
uvicorn workers; if each loaded Whisper in-process it would duplicate the model
N times and block the event loop. So, exactly like `embedding_server.py`, this
is a separate container the API calls over internal HTTP.

Key design decisions:
1. Single process, model loaded once at startup (WHISPER_WORKERS defaults to 1).
   More workers = more model copies in VRAM, so keep it at 1.
2. Blocking transcribe() runs in a thread pool with a timeout so a stuck decode
   never wedges the event loop.
3. VAD filter is ON by default. Whisper hallucinates confident garbage on
   silence ("Thank you for watching", etc.); dropping non-speech frames first
   removes most of those artifacts.
4. Internal API-key auth (same trust boundary + key as the embedding server) so
   this GPU service is never an open abuse vector.
"""
import os
import io
import wave
import asyncio
import threading
import concurrent.futures
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Whisper Server")


def _env_bool(name: str, default: bool = False) -> bool:
	"""Read a boolean env var; treat 1/true/yes/on as True, else the default."""
	value = os.getenv(name)
	if value is None:
		return default
	return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
	"""Read an int env var, falling back to the default on anything invalid."""
	value = os.getenv(name)
	if value is None:
		return default
	try:
		return int(value)
	except ValueError:
		print(f"[CONFIG WARN] {name}={value!r} invalid; using default={default}")
		return default


# --- Config -----------------------------------------------------------------
# Device: explicit WHISPER_DEVICE wins; otherwise cpu in dev, cuda in prod.
_device = (
	os.environ.get("WHISPER_DEVICE", "").strip().lower()
	or ("cpu" if os.environ.get("ENV") == "dev" else "cuda")
)
# int8_float16 is the sweet spot on the RTX 4000 Ada (small VRAM, near-fp16
# accuracy). On CPU fall back to plain int8.
_compute_type = (
	os.environ.get("WHISPER_COMPUTE_TYPE", "").strip()
	or ("int8" if _device == "cpu" else "int8_float16")
)
# large-v3-turbo: ~1.5GB, RTF ~0.1 on this GPU, accuracy near large-v3.
_model_name = os.environ.get("WHISPER_MODEL", "large-v3-turbo").strip()
_language = os.environ.get("WHISPER_LANGUAGE", "en").strip() or None
_beam_size = _env_int("WHISPER_BEAM_SIZE", 5)
_vad_filter = _env_bool("WHISPER_VAD_FILTER", True)
_transcribe_timeout = _env_int("WHISPER_TIMEOUT", 30)

_model = None
_model_lock = threading.Lock()
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def _require_internal_api_key(
	x_whisper_api_key: Optional[str] = Header(default=None, alias="x-whisper-api-key"),
) -> None:
	"""Reject callers that don't present the internal API key.

	Reuses the embedding server's key (same internal trust boundary) unless a
	dedicated WHISPER_SERVER_API_KEY is set. Auth is required whenever a key is
	configured, or in any non-dev environment. This GPU service must never be an
	open, unauthenticated abuse vector.
	"""
	configured_key = os.getenv("WHISPER_SERVER_API_KEY") or os.getenv("EMBEDDING_SERVER_API_KEY")
	require_auth = bool(configured_key) or (os.getenv("ENV") != "dev")
	require_auth = _env_bool("WHISPER_SERVER_REQUIRE_AUTH", default=require_auth)
	if not require_auth:
		return
	if not configured_key:
		raise HTTPException(status_code=500, detail="Whisper server auth required but no API key is set")
	if not x_whisper_api_key or x_whisper_api_key != configured_key:
		raise HTTPException(status_code=401, detail="Unauthorized")


def _get_model():
	"""Load the faster-whisper model once (thread-safe) and reuse it forever.

	The weights download from HuggingFace into ~/.cache/huggingface, which is a
	Docker named volume (hf_cache), so this download happens only on the very
	first container start and survives every later rebuild/deploy.
	"""
	global _model
	if _model is not None:
		return _model
	with _model_lock:
		if _model is None:
			from faster_whisper import WhisperModel

			print(f"[MODEL] Loading faster-whisper '{_model_name}' device={_device} compute={_compute_type}")
			_model = WhisperModel(_model_name, device=_device, compute_type=_compute_type)
			print("[MODEL] Whisper model ready")
	return _model


def _wav_b64_to_float32(audio_b64: str):
	"""Turn a base64 WAV (16-bit PCM mono) into a float32 numpy array in [-1, 1].

	We decode with the stdlib `wave` module + numpy so faster-whisper gets raw
	samples directly and never has to shell out to ffmpeg/PyAV to decode audio.
	"""
	import base64
	import numpy as np

	raw = base64.b64decode(audio_b64)
	with wave.open(io.BytesIO(raw), "rb") as wf:
		n_channels = wf.getnchannels()
		sampwidth = wf.getsampwidth()
		frames = wf.readframes(wf.getnframes())
	if sampwidth != 2:
		raise ValueError(f"expected 16-bit PCM, got sampwidth={sampwidth}")
	samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
	# Downmix any accidental stereo to mono so the model always sees one channel.
	if n_channels > 1:
		samples = samples.reshape(-1, n_channels).mean(axis=1)
	return samples


def _transcribe_sync(audio_b64: str, language: Optional[str]) -> str:
	"""Blocking transcription: decode audio, run Whisper, join the segment text.

	Runs inside the thread pool (never on the event loop). condition_on_previous
	_text is off to stop the model from looping a hallucination across segments.
	"""
	model = _get_model()
	audio = _wav_b64_to_float32(audio_b64)
	segments, _info = model.transcribe(
		audio,
		language=language or _language,
		beam_size=_beam_size,
		vad_filter=_vad_filter,
		condition_on_previous_text=False,
	)
	return "".join(seg.text for seg in segments).strip()


class TranscribeRequest(BaseModel):
	audio_base64: str
	language: Optional[str] = None


class TranscribeResponse(BaseModel):
	text: str


@app.post("/transcribe", response_model=TranscribeResponse, dependencies=[Depends(_require_internal_api_key)])
async def transcribe(req: TranscribeRequest):
	"""Transcribe one base64 WAV clip to text. Times out instead of hanging."""
	if not req.audio_base64:
		raise HTTPException(status_code=400, detail="No audio provided")
	loop = asyncio.get_event_loop()
	try:
		text = await asyncio.wait_for(
			loop.run_in_executor(_executor, _transcribe_sync, req.audio_base64, req.language),
			timeout=_transcribe_timeout,
		)
	except asyncio.TimeoutError:
		raise HTTPException(status_code=504, detail="Transcription timed out")
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Transcription failed: {type(e).__name__}: {e}")
	return TranscribeResponse(text=text)


@app.get("/health")
async def health():
	"""Report readiness. `model_loaded` is false until the first transcribe warms it."""
	return {
		"status": "ok",
		"device": _device,
		"compute_type": _compute_type,
		"model": _model_name,
		"model_loaded": _model is not None,
	}


@app.on_event("startup")
def _preload() -> None:
	"""Warm the model at boot so the healthcheck only passes once it's ready and
	the first real request isn't slowed by a cold load."""
	try:
		_get_model()
	except Exception as e:
		print(f"[PRELOAD ERROR] {type(e).__name__}: {e}")


if __name__ == "__main__":
	import uvicorn

	workers = int(os.getenv("WHISPER_WORKERS", "1"))
	port = int(os.getenv("WHISPER_PORT", "8002"))
	print(f"[STARTUP] Starting whisper server on port {port} with {workers} worker(s)")
	uvicorn.run(
		"whisper_server:app",
		host="0.0.0.0",
		port=port,
		workers=workers,
	)
