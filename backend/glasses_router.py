"""Smart-glasses client router (Even Realities G2 / Even Hub plugin).

What this is: the backend for a keyboardless smart-glasses app. The glasses
have no keyboard and only a tiny 576x288 display, so the user cannot type an
email/code there. This router therefore implements a **device-link login**
(same idea as logging a TV into an app): the glasses show a short code + URL,
the user finishes the email login in their phone browser, and the glasses poll
until they receive a backend JWT.

Once logged in, the glasses app can:
  - transcribe a spoken utterance (raw PCM from the mic -> Whisper text),
  - query the user's Constella/Lackey memory (RAG) and get lens-sized results,
  - store a spoken note into the user's Constella knowledge base.

Everything is scoped to the user's tenant. In this backend
`tenant_name == auth.sub == Firebase UID == Qdrant group_id`, so once we resolve
the UID from the email we mint a normal backend JWT and reuse all existing
memory/storage code paths unchanged.

Auth model: this router is mounted WITHOUT `_session_deps` (the login endpoints
must be public and the /link page is public). The data endpoints guard
themselves per-endpoint with `Depends(require_auth)`. It is allowlisted in
tests/test_http_route_auth_coverage.py for that reason.
"""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import struct
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
import sentry_sdk
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from constants import is_dev
from ai.openai_setup import openai_client
from ai.retrieval.retrieve_results import retrieve_results
from db.redis.context_redis import get_sync_redis_client
from slackbot.identity import get_or_create_user_by_email
from utils.constella.public_mcp_tools import add_public_thought
from utils.loops import send_transactional_email
from utils.security.auth import (
	AuthContext,
	decode_and_validate_access_token,
	mint_access_token,
	require_auth,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/glasses", tags=["glasses"])


# --- Config -----------------------------------------------------------------

# TTLs (seconds). Device-link codes are short-lived; the emailed OTP is a bit
# longer so a user has time to open the email.
_DEVICE_TTL = 900          # 15 min for the whole link handshake
_OTP_TTL = 600             # 10 min for the emailed 6-digit code
_POLL_INTERVAL = 3         # seconds the glasses should wait between polls
_MAX_OTP_ATTEMPTS = 6      # lock the code after this many bad tries
_MAX_PCM_B64_CHARS = 40_000_000  # ~30MB of base64 audio ceiling (~15 min PCM)

# Human-typed link code alphabet: no 0/O/1/I/L to avoid confusion on a phone.
_USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# Loops transactional template id that renders the login code. Set this to your
# own "Glasses Login Code" template (data variable: `code`) via the env var.
# When unset (or in dev), the code is logged instead of emailed.
_LOOPS_TEMPLATE_ID = os.getenv("GLASSES_LOGIN_LOOPS_TEMPLATE_ID", "")

# Public origin that serves the /glasses/link page (shown on the lens). Defaults
# to the production API host, which serves this router.
_PUBLIC_BASE = os.getenv("GLASSES_PUBLIC_BASE", "https://fastfind.app").rstrip("/")

# Pepper for hashing the OTP at rest in Redis so a Redis dump never reveals codes.
_PEPPER = os.getenv("JWT_SECRET", "") or "glasses-otp-pepper"


# --- Redis key helpers ------------------------------------------------------

def _device_key(device_code: str) -> str:
	return f"glasses:device:{device_code}"


def _usercode_key(user_code: str) -> str:
	return f"glasses:usercode:{user_code}"


def _otp_key(email: str) -> str:
	return f"glasses:otp:{email}"


# --- Small pure helpers -----------------------------------------------------

def _norm_email(email: str) -> str:
	"""Lowercase + trim an email so lookups and OTP keys are stable."""
	return (email or "").strip().lower()


def _norm_user_code(user_code: str) -> str:
	"""Normalize a typed link code (uppercase, strip spaces/dashes)."""
	return (user_code or "").strip().upper().replace(" ", "").replace("-", "")


def _hash_code(code: str, salt: str) -> str:
	"""HMAC-hash an OTP with a server pepper + per-user salt (email).

	We store only the hash in Redis so a leaked Redis snapshot can't reveal
	live login codes.
	"""
	return hmac.new(_PEPPER.encode(), f"{salt}:{code}".encode(), hashlib.sha256).hexdigest()


def _gen_numeric_code(n: int = 6) -> str:
	"""Cryptographically-random n-digit numeric OTP (zero-padded)."""
	return "".join(secrets.choice("0123456789") for _ in range(n))


def _gen_user_code() -> str:
	"""Short human-typable link code, grouped as XXXX-XXXX for readability."""
	raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
	return f"{raw[:4]}-{raw[4:]}"


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
	"""Wrap raw 16-bit mono PCM in a minimal WAV container.

	The glasses mic delivers signed-16-bit little-endian mono PCM. Whisper needs
	a recognizable container, so we prepend a 44-byte WAV header. No re-encoding.
	"""
	num_channels = 1
	bits_per_sample = 16
	byte_rate = sample_rate * num_channels * bits_per_sample // 8
	block_align = num_channels * bits_per_sample // 8
	data_len = len(pcm_bytes)
	header = b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
	header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, num_channels, sample_rate, byte_rate, block_align, bits_per_sample)
	header += b"data" + struct.pack("<I", data_len)
	return header + pcm_bytes


def _clean_snippet(text: str, limit: int = 200) -> str:
	"""Collapse whitespace and clip note content to a lens-friendly snippet."""
	s = " ".join((text or "").split())
	return s[:limit].rstrip() + ("…" if len(s) > limit else "")


# --- Sync core logic (run via asyncio.to_thread from the async endpoints) ----
# These touch Redis / Firebase / Loops / OpenAI / Qdrant, all of which block, so
# every async endpoint offloads them to a worker thread to avoid wedging the
# event loop (the same discipline the search router uses for retrieve_results).

def _start_device() -> Dict[str, Any]:
	"""Begin a device-link session: allocate a device_code + short user_code.

	Stores two short-lived Redis records (device->state, user_code->device) so
	the phone-browser step can find the device by the code shown on the lens.
	Returns what the glasses need to display + poll.
	"""
	client = get_sync_redis_client()
	device_code = secrets.token_urlsafe(32)
	# Retry a couple times on the astronomically-unlikely user_code collision.
	user_code = _gen_user_code()
	for _ in range(3):
		if not client.get(_usercode_key(_norm_user_code(user_code))):
			break
		user_code = _gen_user_code()
	record = {"status": "pending", "user_code": user_code, "created": int(time.time())}
	client.set(_device_key(device_code), json.dumps(record), ex=_DEVICE_TTL)
	client.set(_usercode_key(_norm_user_code(user_code)), device_code, ex=_DEVICE_TTL)
	return {
		"device_code": device_code,
		"user_code": user_code,
		"verification_url": f"{_PUBLIC_BASE}/glasses/link?code={_norm_user_code(user_code)}",
		"interval": _POLL_INTERVAL,
		"expires_in": _DEVICE_TTL,
	}


def _poll_device(device_code: str) -> Dict[str, Any]:
	"""Check whether a device-link session has been approved in the browser.

	Returns {status: pending|approved|expired}. On approval it returns the minted
	token exactly once, then deletes the records so the token isn't left in Redis.
	"""
	client = get_sync_redis_client()
	raw = client.get(_device_key(device_code))
	if not raw:
		return {"status": "expired"}
	data = json.loads(raw)
	if data.get("status") != "approved":
		return {"status": "pending"}
	# Approved: hand back the token once, then clean up both keys.
	client.delete(_device_key(device_code))
	if data.get("user_code"):
		client.delete(_usercode_key(_norm_user_code(data["user_code"])))
	return {
		"status": "approved",
		"token": data.get("token"),
		"tenant_name": data.get("tenant_name"),
		"email": data.get("email"),
	}


def _request_login_code(email: str) -> Dict[str, Any]:
	"""Resolve the tenant for an email and email them a 6-digit login code.

	Resolves (or creates) the Constella user id via get_or_create_user_by_email,
	stores a hashed OTP in Redis, and sends the code through Loops. In dev (or
	when no Loops template is configured) the code is logged instead of emailed.
	Returns a generic {ok: True} regardless, plus the code in dev for convenience.
	"""
	email = _norm_email(email)
	if "@" not in email or "." not in email.split("@")[-1]:
		raise HTTPException(status_code=400, detail="Invalid email")

	# The uid IS the tenant. Do this now so verify only has to compare the code.
	uid = get_or_create_user_by_email(email)
	if not uid:
		# Don't reveal resolution failures to the caller; just don't store a code.
		logger.warning("[glasses] could not resolve uid for %s", email)
		return {"ok": True}

	code = _gen_numeric_code(6)
	record = {"code_hash": _hash_code(code, email), "uid": uid, "email": email, "attempts": 0}
	get_sync_redis_client().set(_otp_key(email), json.dumps(record), ex=_OTP_TTL)

	if is_dev or not _LOOPS_TEMPLATE_ID:
		logger.info("[glasses] DEV login code for %s: %s", email, code)
	else:
		send_transactional_email(email, _LOOPS_TEMPLATE_ID, {"code": code, "login_code": code, "otp": code})

	out: Dict[str, Any] = {"ok": True}
	if is_dev:
		out["dev_code"] = code
	return out


def _verify_and_link(user_code: str, email: str, code: str) -> Dict[str, Any]:
	"""Verify the emailed code and approve the pending glasses device.

	On a correct code we mint a backend JWT (sub=tenant=uid) and stamp it onto
	the device record so the glasses' next poll picks it up. Wrong codes count
	toward a small attempt cap; the OTP is single-use.
	"""
	email = _norm_email(email)
	user_code = _norm_user_code(user_code)
	client = get_sync_redis_client()

	raw = client.get(_otp_key(email))
	if not raw:
		raise HTTPException(status_code=400, detail="Code expired or not requested")
	data = json.loads(raw)

	attempts = int(data.get("attempts", 0))
	if attempts >= _MAX_OTP_ATTEMPTS:
		client.delete(_otp_key(email))
		raise HTTPException(status_code=429, detail="Too many attempts, request a new code")

	if not hmac.compare_digest(_hash_code(code, email), data.get("code_hash", "")):
		data["attempts"] = attempts + 1
		client.set(_otp_key(email), json.dumps(data), ex=_OTP_TTL)
		raise HTTPException(status_code=400, detail="Invalid code")

	uid = data["uid"]
	token = mint_access_token(sub=uid, email=email, tenant_name=uid)

	# Agent-token-only mode: no glasses code means the user just wants a token
	# to paste into the Even app's "Add Agent" screen (Settings -> Even AI).
	if user_code:
		# Device-link mode: find the pending glasses this browser is linking.
		device_code = client.get(_usercode_key(user_code))
		if not device_code:
			raise HTTPException(status_code=400, detail="Invalid or expired link code")
		dev_raw = client.get(_device_key(device_code))
		if not dev_raw:
			raise HTTPException(status_code=400, detail="Link session expired, restart on your glasses")
		dev = json.loads(dev_raw)
		dev.update({"status": "approved", "token": token, "tenant_name": uid, "email": email})
		client.set(_device_key(device_code), json.dumps(dev), ex=_DEVICE_TTL)

	client.delete(_otp_key(email))  # OTP is single-use
	# Return the agent credentials either way so the page can offer the Even AI
	# custom-agent setup (URL + Bearer token) after a successful email login.
	return {
		"ok": True,
		"agent_token": token,
		"agent_url": f"{_PUBLIC_BASE}/glasses/agent/v1/chat/completions",
	}


def _transcribe_wav_local(wav: bytes) -> str:
	"""Transcribe a WAV buffer on our own GPU via the local whisper-server.

	Primary path: free (no per-call API cost), low latency (no cloud roundtrip),
	and immune to the OpenAI quota running dry. Sends the WAV as base64 to the
	internal whisper-server with the shared internal API key. Raises on any
	failure so `_transcribe_pcm` cleanly falls back to OpenAI/Deepgram.
	"""
	base_url = os.getenv("WHISPER_SERVER_URL")
	if not base_url or os.getenv("USE_WHISPER_SERVER", "true").strip().lower() not in {"1", "true", "yes", "on"}:
		raise RuntimeError("whisper-server disabled")
	api_key = os.getenv("WHISPER_SERVER_API_KEY") or os.getenv("EMBEDDING_SERVER_API_KEY") or ""
	resp = requests.post(
		f"{base_url.rstrip('/')}/transcribe",
		headers={"x-whisper-api-key": api_key, "Content-Type": "application/json"},
		json={"audio_base64": base64.b64encode(wav).decode("ascii"), "language": "en"},
		timeout=30,
	)
	resp.raise_for_status()
	return (resp.json().get("text") or "").strip()


def _transcribe_wav_deepgram(wav: bytes) -> str:
	"""Transcribe a WAV buffer with Deepgram's prerecorded API (nova-2).

	Fallback provider: the key is already provisioned on prod (Horizon streaming
	STT uses it) and it isn't tied to the OpenAI billing quota, which has run dry
	before and silently killed glasses transcription while Whisper was the only path.
	"""
	api_key = os.getenv("DEEPGRAM_API_KEY")
	if not api_key:
		raise RuntimeError("DEEPGRAM_API_KEY not set")
	resp = requests.post(
		"https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true",
		headers={"Authorization": f"Token {api_key}", "Content-Type": "audio/wav"},
		data=wav,
		timeout=30,
	)
	resp.raise_for_status()
	body = resp.json()
	return (
		(body.get("results", {}).get("channels") or [{}])[0]
		.get("alternatives", [{}])[0]
		.get("transcript", "")
	).strip()


def _transcribe_pcm(pcm_b64: str, sample_rate: int = 16000) -> str:
	"""Decode base64 PCM from the glasses and transcribe it.

	Tries OpenAI Whisper first, falls back to Deepgram. Raises a 503 with the
	real provider error in the detail so the glasses can show something useful
	instead of a generic failure.
	"""
	if not pcm_b64:
		raise HTTPException(status_code=400, detail="No audio provided")
	if len(pcm_b64) > _MAX_PCM_B64_CHARS:
		raise HTTPException(status_code=413, detail="Audio too long")
	try:
		pcm_bytes = base64.b64decode(pcm_b64)
	except Exception:
		raise HTTPException(status_code=400, detail="Invalid base64 audio")
	if len(pcm_bytes) < 2000:  # < ~60ms of audio: nothing useful was said
		return ""
	wav = _pcm_to_wav(pcm_bytes, sample_rate=sample_rate)

	errors: List[str] = []
	# Primary: our own GPU whisper-server (free, low-latency, quota-proof).
	try:
		text = _transcribe_wav_local(wav)
		return text
	except Exception as e:
		errors.append(f"local: {type(e).__name__}")
		logger.warning("[glasses] local whisper-server failed, trying OpenAI: %s", e)

	try:
		bio = io.BytesIO(wav)
		bio.name = "audio.wav"  # OpenAI SDK infers format from the filename
		transcript = openai_client.audio.transcriptions.create(model="whisper-1", file=bio)
		return (getattr(transcript, "text", "") or "").strip()
	except Exception as e:
		errors.append(f"whisper: {type(e).__name__}")
		logger.warning("[glasses] whisper transcription failed, trying deepgram: %s", e)

	try:
		return _transcribe_wav_deepgram(wav)
	except Exception as e:
		errors.append(f"deepgram: {type(e).__name__}")
		logger.error("[glasses] deepgram transcription failed: %s", e)
		sentry_sdk.capture_exception(e)

	raise HTTPException(status_code=503, detail=f"Transcription unavailable ({'; '.join(errors)})")


# Question-ish openers for the heuristic intent fallback. Kept deliberately
# simple: a spoken query to a memory assistant almost always opens with one of
# these; everything else is treated as a capture (losing a saved thought is
# worse than mis-answering a question the user can just re-ask).
_ASK_OPENERS = (
	"who", "what", "when", "where", "why", "how", "which", "whats", "what's",
	"did", "do", "does", "is", "are", "was", "were", "am", "can", "could",
	"should", "would", "will", "have", "has", "had",
	"tell", "show", "find", "search", "look", "recall", "list", "give",
)

# --- Deterministic spoken triggers -------------------------------------------
# Longest-first so multi-word phrases win over their single-word prefixes
# ("note to self" before "note"). Matching is case-insensitive, requires a word
# boundary after the trigger, and tolerates lead-in fillers the STT prepends
# ("And remember that..." -> note). When a trigger matches, the trigger phrase
# (plus a connective like "that/to/this") is stripped from the stored/queried
# text so notes and queries stay clean.
# NOTE for Even AI voice path: Even's native skills intercept some words BEFORE
# we ever see them (observed: "save", "list", "add", "note" -> QuickList). The
# reliable spoken triggers there are "remember"/"memo" and "recall"/"lookup".
# The full lists still matter for the Hub app path where we do our own STT.
_NOTE_TRIGGERS = tuple(sorted((
	"note to self", "keep in mind", "write down", "take a note", "add note",
	"don't forget", "dont forget",
	"remember", "memorize", "memo", "note", "capture", "jot", "log", "record", "save",
), key=len, reverse=True))
_ASK_TRIGGERS = tuple(sorted((
	"what do i know about", "do i know anything about", "check memory for",
	"ask my memories about", "ask my memories", "ask my memory", "ask memory",
	"look up", "lookup", "recall", "retrieve", "search", "query",
	"ask", "asked", "question", "find", "check",
), key=len, reverse=True))

# STT loves to prepend connective fillers to the utterance; strip them before
# trigger matching so "And remember that X" still routes deterministically.
# "even" is included because the wake word bleeds into transcripts —
# "Hey Even, ask my memories..." arrives as "Hey Even ask my memories...".
_FILLER_RE = re.compile(
	r"^(?:(?:and|so|also|hey|even|ok|okay|um|uh|please|now|then|alright)[,.\s]+)+",
	re.IGNORECASE,
)
_CONNECTIVE_RE = re.compile(r"^(?:that|to|this|about|for|if)\b[,\s]*", re.IGNORECASE)


def _match_trigger(stripped: str, triggers: tuple) -> Optional[str]:
	"""Return the remainder after a leading trigger phrase, or None.

	A match requires the utterance to START with the trigger followed by a
	non-alphanumeric boundary, so "recorder" never matches "record".
	"""
	low = stripped.lower()
	for trig in triggers:
		if not low.startswith(trig):
			continue
		if len(low) > len(trig) and low[len(trig)].isalnum():
			continue
		rest = stripped[len(trig):].lstrip(" ,.:;-")
		rest = _CONNECTIVE_RE.sub("", rest).strip()
		return rest or stripped
	return None


def _route_utterance(text: str) -> tuple:
	"""Deterministically route a spoken utterance: (intent, cleaned_text).

	Priority: explicit note trigger -> explicit ask trigger -> question-opener
	word (deterministic ask) -> LLM classifier -> default note. The cleaned
	text (trigger removed) is what gets stored or queried, so "remember that X"
	saves just "X". Ask is deliberately easy to hit: a misrouted question is
	annoying (it pollutes memory), while explicit captures are protected by the
	note triggers running first.
	"""
	raw = (text or "").strip()
	stripped = _FILLER_RE.sub("", raw).lstrip(" ,.:;-").strip() or raw
	rest = _match_trigger(stripped, _NOTE_TRIGGERS)
	if rest is not None:
		print(f"[glasses] route=note (trigger) text={stripped[:60]!r}")
		return "note", rest
	rest = _match_trigger(stripped, _ASK_TRIGGERS)
	if rest is not None:
		print(f"[glasses] route=ask (trigger) text={stripped[:60]!r}")
		return "ask", rest
	# Any question-opener first word routes to ask deterministically — no LLM
	# needed for "what's my next meeting" / "when did Diane..." style asks.
	first = stripped.lower().split()[0].strip(",.:;!?") if stripped.split() else ""
	if first in _ASK_OPENERS or stripped.rstrip().endswith("?"):
		print(f"[glasses] route=ask (opener) text={stripped[:60]!r}")
		return "ask", stripped
	intent = _classify_intent(stripped)
	print(f"[glasses] route={intent} (llm/heuristic) text={stripped[:60]!r}")
	return intent, stripped


def _seen_recently(uid: str, text: str, ttl: int = 20) -> bool:
	"""True when this exact utterance from this user was just processed.

	The Even app double-fires each utterance (observed: two identical POSTs per
	spoken sentence), which duplicated saved notes. Redis SETNX gives us a cheap
	idempotency window; on Redis failure we err toward processing (never drop).
	"""
	try:
		digest = hashlib.sha256(f"{uid}:{text}".encode()).hexdigest()[:20]
		key = f"glasses:dedupe:{uid}:{digest}"
		return not get_sync_redis_client().set(key, "1", nx=True, ex=ttl)
	except Exception:
		return False


# Sync OpenRouter client for the intent tiebreak. NOT the main openai_client:
# that hits api.openai.com directly (billing-quota fragile, and repo model
# aliases like gpt-5.4-nano only exist on OpenRouter). gpt-4.1-mini is
# non-reasoning, so a tiny completion budget actually yields tokens.
_openrouter_client = None


def _get_openrouter_client():
	global _openrouter_client
	if _openrouter_client is None:
		from openai import OpenAI

		_openrouter_client = OpenAI(
			base_url="https://openrouter.ai/api/v1",
			api_key=os.getenv("OPENROUTER_API_KEY"),
			timeout=8.0,
		)
	return _openrouter_client


def _classify_intent(text: str) -> str:
	"""LLM tiebreak for utterances no deterministic rule caught (see
	_route_utterance, which already handled triggers and question openers).

	On any failure, default to 'note' so nothing the user said is ever lost —
	question-shaped utterances were already routed to ask deterministically.
	"""
	t = (text or "").strip()
	if not t:
		return "note"
	try:
		resp = _get_openrouter_client().chat.completions.create(
			model="openai/gpt-4.1-mini",
			messages=[
				{
					"role": "system",
					"content": (
						"Classify a smart-glasses voice utterance. Reply with exactly one word.\n"
						"'ask' = the user wants to retrieve, question, or hear about their saved knowledge/memory.\n"
						"'note' = the user is capturing a thought, fact, idea, reminder, or observation."
					),
				},
				{"role": "user", "content": t[:500]},
			],
			max_tokens=4,
			temperature=0,
		)
		answer = (resp.choices[0].message.content or "").strip().lower()
		if "ask" in answer:
			return "ask"
		if "note" in answer:
			return "note"
	except Exception as e:
		print(f"[glasses] intent LLM failed ({type(e).__name__}), defaulting to note")
	return "note"


def _query_memory(uid: str, query: str, top_k: int = 8) -> Dict[str, Any]:
	"""Run a RAG query over the user's Constella memory and shape it for the lens.

	Returns both a structured `results` list (for the app) and a compact
	`display_text` string (terse, no emoji) sized for the 576x288 green display.
	"""
	query = (query or "").strip()
	if not query:
		return {"results": [], "display_text": "", "count": 0, "query": ""}

	payload = retrieve_results(
		user_id=uid,
		query=query,
		top_k=top_k,
		similarity_setting=0.5,
		include_postgres_content=True,
	)
	raw_results = payload.get("results", []) or []

	compact: List[Dict[str, Any]] = []
	for r in raw_results[:top_k]:
		content = r.get("fullContent") or r.get("content") or r.get("fileText") or r.get("text") or ""
		compact.append({
			"uniqueid": r.get("uniqueid"),
			"title": (r.get("title") or "").strip() or "(untitled)",
			"snippet": _clean_snippet(content, 200),
			"score": r.get("score"),
		})

	# Lens text: top few as "- Title: snippet", capped so it fits the display.
	lines: List[str] = []
	for c in compact[:3]:
		lines.append(f"- {c['title']}: {_clean_snippet(c['snippet'], 90)}")
	display_text = ("\n".join(lines) if lines else "No matching memories.")[:420]

	return {"results": compact, "display_text": display_text, "count": len(compact), "query": query}


def _store_note(uid: str, content: str, title: Optional[str] = None) -> Dict[str, Any]:
	"""Store a spoken note into the user's Constella knowledge base.

	Synthesizes a title from the first line when the client didn't supply one
	(add_public_thought rejects blank titles).
	"""
	content = (content or "").strip()
	if not content:
		raise HTTPException(status_code=400, detail="Empty note")

	if not (title or "").strip():
		first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
		title = (first_line[:60].rstrip() or f"Note {datetime.now(tz=timezone.utc):%Y-%m-%d %H:%M}")
	title = title.strip()

	res = add_public_thought(user_id=uid, title=title, content=content)
	return {"ok": bool(res.get("success")), "uniqueid": res.get("uniqueid"), "title": title}


# --- Request models ---------------------------------------------------------

class DeviceStartReq(BaseModel):
	label: Optional[str] = None  # optional device nickname, currently unused


class DevicePollReq(BaseModel):
	device_code: str


class RequestCodeReq(BaseModel):
	email: str


class LinkVerifyReq(BaseModel):
	user_code: str = ""  # empty = agent-token-only login (no glasses device to approve)
	email: str
	code: str


class TranscribeReq(BaseModel):
	pcm_base64: str
	sample_rate: int = 16000


class QueryReq(BaseModel):
	query: str
	top_k: int = 8


class NoteReq(BaseModel):
	content: str
	title: Optional[str] = None


class AskAudioReq(BaseModel):
	pcm_base64: str
	sample_rate: int = 16000
	top_k: int = 8


class NoteAudioReq(BaseModel):
	pcm_base64: str
	sample_rate: int = 16000
	title: Optional[str] = None


class VoiceReq(BaseModel):
	pcm_base64: str
	sample_rate: int = 16000
	top_k: int = 8


class MeetingStartReq(BaseModel):
	title: Optional[str] = None


class MeetingChunkReq(BaseModel):
	meeting_id: str
	pcm_base64: str
	sample_rate: int = 16000
	seq: int = 0


class MeetingEndReq(BaseModel):
	meeting_id: str
	title: Optional[str] = None
	# Optional final flush: the last partial chunk of audio recorded before end.
	pcm_base64: Optional[str] = None
	sample_rate: int = 16000
	final_seq: Optional[int] = None


class MeetingCueReq(BaseModel):
	meeting_id: str
	# Only return a cue newer than this sequence number (0 = any cue).
	after_seq: int = 0


# --- Auth: device-link flow (public) ----------------------------------------

@router.post("/auth/device/start")
async def device_start(payload: DeviceStartReq = DeviceStartReq()):
	"""Glasses call this to begin login; returns a code + URL to show the user."""
	try:
		return await asyncio.to_thread(_start_device)
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/auth/device/poll")
async def device_poll(payload: DevicePollReq):
	"""Glasses poll this until status == approved, then store the returned token."""
	try:
		return await asyncio.to_thread(_poll_device, payload.device_code)
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/auth/link/request-code")
async def link_request_code(payload: RequestCodeReq):
	"""Phone browser calls this to email the user a 6-digit login code."""
	try:
		return await asyncio.to_thread(_request_login_code, payload.email)
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/auth/link/verify")
async def link_verify(payload: LinkVerifyReq):
	"""Phone browser calls this with the emailed code to approve the glasses."""
	try:
		return await asyncio.to_thread(_verify_and_link, payload.user_code, payload.email, payload.code)
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/link", response_class=HTMLResponse)
async def link_page(code: str = ""):
	"""Serve the phone-browser login page the user opens from their glasses.

	Self-contained HTML (same-origin as the API, so no CORS). Step 1: enter email
	-> request code. Step 2: enter the emailed code -> verify + link the device.
	"""
	return HTMLResponse(_LINK_PAGE_HTML.replace("__PREFILL_CODE__", _norm_user_code(code)))


# --- Session-guarded data endpoints -----------------------------------------

@router.get("/me")
async def me(auth: AuthContext = Depends(require_auth)):
	"""Return the logged-in identity so the app can validate a stored token."""
	return {"tenant_name": auth.sub, "email": auth.email}


@router.post("/transcribe")
async def transcribe(payload: TranscribeReq, auth: AuthContext = Depends(require_auth)):
	"""Transcribe a spoken utterance (glasses PCM) to text via Whisper."""
	try:
		text = await asyncio.to_thread(_transcribe_pcm, payload.pcm_base64, payload.sample_rate)
		return {"text": text}
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/query")
async def query(payload: QueryReq, auth: AuthContext = Depends(require_auth)):
	"""Query the user's memory (RAG) with already-transcribed text."""
	try:
		return await asyncio.to_thread(_query_memory, auth.sub, payload.query, payload.top_k)
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/note")
async def note(payload: NoteReq, auth: AuthContext = Depends(require_auth)):
	"""Store a note into the user's Constella knowledge base."""
	try:
		return await asyncio.to_thread(_store_note, auth.sub, payload.content, payload.title)
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/ask-audio")
async def ask_audio(payload: AskAudioReq, auth: AuthContext = Depends(require_auth)):
	"""One-shot: transcribe spoken audio then RAG-query memory (fewer BLE calls)."""
	try:
		text = await asyncio.to_thread(_transcribe_pcm, payload.pcm_base64, payload.sample_rate)
		if not text:
			return {"transcript": "", "results": [], "display_text": "Didn't catch that.", "count": 0}
		result = await asyncio.to_thread(_query_memory, auth.sub, text, payload.top_k)
		result["transcript"] = text
		return result
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/voice")
async def voice(payload: VoiceReq, auth: AuthContext = Depends(require_auth)):
	"""One-tap unified voice endpoint: transcribe, auto-route ask-vs-note, act.

	The glasses send one utterance; we transcribe it, classify the intent
	(question about memory vs thought to capture), and either run the RAG query
	or store the note. Returns `intent` plus the same fields as /ask-audio or
	/note-audio so the lens can render one unified result.
	"""
	try:
		text = await asyncio.to_thread(_transcribe_pcm, payload.pcm_base64, payload.sample_rate)
		if not text:
			return {"intent": "none", "transcript": "", "display_text": "Didn't catch that."}
		intent, cleaned = await asyncio.to_thread(_route_utterance, text)
		if intent == "ask":
			# Full agent loop (LLM + integration tools + memory), RAG fallback inside.
			answer = await _agent_ask(auth.sub, cleaned)
			result: Dict[str, Any] = {"results": [], "display_text": answer, "count": 0}
		elif await asyncio.to_thread(_seen_recently, auth.sub, text):
			# Client retry inside the dedupe window — acknowledge, don't double-store.
			first_line = next((ln.strip() for ln in cleaned.splitlines() if ln.strip()), cleaned)
			result = {"ok": True, "uniqueid": None, "title": first_line[:60]}
		else:
			result = await asyncio.to_thread(_store_note, auth.sub, cleaned, None)
		result["intent"] = intent
		result["transcript"] = text
		return result
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


# --- Even AI custom-agent surface (OpenAI-compatible) ------------------------
# The Even Realities app lets users register a custom AI agent (Settings ->
# Even AI -> Add Agent: name, URL, token). Holding the LEFT TouchBar then
# speaking sends the TRANSCRIBED text to that URL as an OpenAI-style
# chat-completions request, and the response content renders on the lens.
# This makes hands-free capture/recall work with ZERO app launch: Even does the
# STT, we just classify ask-vs-note and act. Constraints from the community
# writeups: ~30s response window, plain text only, ~400 readable chars.

async def _agent_auth(request: Request) -> AuthContext:
	"""Authenticate an Even AI agent call via `Authorization: Bearer <jwt>`.

	The Even app sends the configured agent token as a Bearer header (not our
	usual `access-token` header), so this parses both and validates the same
	backend JWT the link flow mints.
	"""
	header = request.headers.get("authorization") or ""
	token = header[7:].strip() if header.lower().startswith("bearer ") else ""
	if not token:
		token = (request.headers.get("access-token") or "").strip()
	if not token:
		raise HTTPException(status_code=401, detail="Missing bearer token")
	return decode_and_validate_access_token(token)


def _extract_agent_utterance(body: Dict[str, Any]) -> str:
	"""Pull the last user message out of an OpenAI-style chat request.

	Content may be a plain string or the array-of-parts form; both are handled.
	"""
	messages = body.get("messages") or []
	for m in reversed(messages):
		if not isinstance(m, dict) or (m.get("role") or "user") != "user":
			continue
		content = m.get("content")
		if isinstance(content, str):
			return content.strip()
		if isinstance(content, list):
			parts = [
				p.get("text", "")
				for p in content
				if isinstance(p, dict) and p.get("type") in (None, "text")
			]
			return " ".join(x for x in parts if x).strip()
	return ""


# Lens-mode instruction prepended to the user question when running the full
# agent loop: the display is 576x288 monochrome text, so the model must answer
# tersely in plain text. Riding on user_text keeps the change contained to this
# router instead of adding a source branch to the shared Stella generator.
_LENS_STYLE_PREFIX = (
	"[Answering on a smart-glasses text display: reply in plain text only, no "
	"markdown, no emoji, no lists with symbols, maximum 350 characters, direct "
	"and specific.] "
)

_MD_MARKS_RE = re.compile(r"[*_#`>]+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _lens_clean(text: str) -> str:
	"""Strip markdown artifacts and clamp an agent answer to lens size."""
	t = _MD_LINK_RE.sub(r"\1", text or "")
	t = _MD_MARKS_RE.sub("", t)
	t = re.sub(r"[ \t]+", " ", t)
	t = re.sub(r"\n{3,}", "\n\n", t)
	return t.strip()[:400]


async def _agent_ask(uid: str, question: str) -> str:
	"""Answer a recall via the full Stella v2 agent loop, RAG as safety net.

	The loop gets tool calling (memory retrieval + the user's connected Nango
	integrations) and a persistent chat_id so follow-up questions keep context.
	The Even AI surface hard-cuts around ~30s, so the loop gets 24s; on timeout
	or error we fall back to the fast raw-RAG snippets rather than failing.
	Runs on OpenRouter, so it is independent of the OpenAI billing quota.
	"""
	try:
		from ai.stella_v2.full_response_generator import generate_full_response

		answer = await asyncio.wait_for(
			generate_full_response(
				user_id=uid,
				chat_id=f"glasses:{uid}",
				user_text=_LENS_STYLE_PREFIX + question,
				use_tools=True,
				source="glasses",
				include_nango_tools=True,
			),
			timeout=24,
		)
		cleaned = _lens_clean(answer if isinstance(answer, str) else str(answer or ""))
		if cleaned:
			return cleaned
	except Exception as e:
		logger.warning("[glasses] agent ask failed, falling back to raw RAG: %s", e)
	result = await asyncio.to_thread(_query_memory, uid, question, 6)
	return (result.get("display_text") or "No matching memories.")[:400]


def _note_reply(uid: str, raw_text: str, cleaned: str) -> str:
	"""Save a capture (dedupe-guarded) and return the lens acknowledgment."""
	if _seen_recently(uid, raw_text):
		first_line = next((ln.strip() for ln in cleaned.splitlines() if ln.strip()), cleaned)
		return f"Saved: {first_line[:60]}"
	result = _store_note(uid, cleaned, None)
	if result.get("ok"):
		return f"Saved: {result.get('title')}"
	return "Couldn't save that, try again."


@router.post("/agent/v1/chat/completions")
@router.post("/agent/chat/completions")
async def agent_chat_completions(request: Request):
	"""OpenAI-compatible endpoint for the Even AI custom-agent integration."""
	auth = await _agent_auth(request)
	try:
		body = await request.json()
	except Exception:
		raise HTTPException(status_code=400, detail="Invalid JSON body")
	if not isinstance(body, dict):
		body = {}

	text = _extract_agent_utterance(body)
	if not text:
		content = "Didn't catch that."
	else:
		intent, cleaned = await asyncio.to_thread(_route_utterance, text)
		if intent == "ask":
			content = await _agent_ask(auth.sub, cleaned)
		else:
			content = await asyncio.to_thread(_note_reply, auth.sub, text, cleaned)

	return {
		"id": f"chatcmpl-{secrets.token_hex(12)}",
		"object": "chat.completion",
		"created": int(time.time()),
		"model": body.get("model") or "constella-memory",
		"choices": [
			{
				"index": 0,
				"message": {"role": "assistant", "content": content},
				"finish_reason": "stop",
			}
		],
		"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
	}


@router.get("/agent/v1/models")
async def agent_models():
	"""Static model list — some OpenAI clients probe this before first use."""
	return {
		"object": "list",
		"data": [{"id": "constella-memory", "object": "model", "created": 0, "owned_by": "constella"}],
	}


@router.post("/note-audio")
async def note_audio(payload: NoteAudioReq, auth: AuthContext = Depends(require_auth)):
	"""One-shot: transcribe spoken audio then store it as a note."""
	try:
		text = await asyncio.to_thread(_transcribe_pcm, payload.pcm_base64, payload.sample_rate)
		if not text:
			return {"transcript": "", "ok": False, "uniqueid": None, "title": None}
		result = await asyncio.to_thread(_store_note, auth.sub, text, payload.title)
		result["transcript"] = text
		return result
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


# --- Meeting mode: continuous transcription into one Constella note ----------
# The Hub app streams the mic in ~25s chunks; each chunk is transcribed and
# appended to a Redis-backed session. Ending the meeting assembles the full
# transcript and stores it through the SAME parent-note + chunk-sync pipeline
# the notes router uses (via _store_note -> add_public_thought), so the whole
# meeting is immediately searchable/recallable like any other memory.

_MEETING_TTL = 4 * 3600          # session lifetime; a forgotten session self-expires
_MEETING_MAX_CHARS = 200_000     # ~4h of dense speech; hard cap against runaways
_MEETING_LOCK_TTL = 300          # covers transcription + durable note storage
_MEETING_LOCK_WAIT = 90          # let /end wait for an active chunk transcription


def _meeting_meta_key(uid: str, mid: str) -> str:
	return f"glasses:meeting:{uid}:{mid}:meta"


def _meeting_parts_key(uid: str, mid: str) -> str:
	return f"glasses:meeting:{uid}:{mid}:parts"


def _meeting_seen_seq_key(uid: str, mid: str) -> str:
	"""Name the Redis set that prevents retried audio chunks from duplicating."""
	return f"glasses:meeting:{uid}:{mid}:seen-seq"


def _meeting_mutation_lock_key(uid: str, mid: str) -> str:
	"""Name the Redis lock that keeps chunk append and final save in order."""
	return f"glasses:meeting:{uid}:{mid}:mutation-lock"


def _meeting_result_key(uid: str, mid: str) -> str:
	"""Name the short-lived completion receipt returned for safe /end retries."""
	return f"glasses:meeting:{uid}:{mid}:result"


def _meeting_start(uid: str, title: Optional[str]) -> Dict[str, Any]:
	"""Open a Redis-backed meeting for `uid` and return its new opaque id.

	The optional `title` is saved in temporary metadata. This mutates Redis only;
	the durable searchable note is created later by `_meeting_end`.
	"""
	client = get_sync_redis_client()
	mid = secrets.token_urlsafe(9)
	meta = {"title": (title or "").strip(), "started": int(time.time())}
	client.set(_meeting_meta_key(uid, mid), json.dumps(meta), ex=_MEETING_TTL)
	return {"meeting_id": mid}


def _meeting_append(uid: str, mid: str, pcm_b64: str, sample_rate: int, seq: int) -> Dict[str, Any]:
	"""Transcribe one audio chunk and append it to the session transcript.

	`seq` is a client-stable retry id. This holds the meeting's Redis lock while
	transcribing so `/meeting/end` cannot finish ahead of an active chunk, and it
	records each sequence once so a lost HTTP response cannot duplicate speech.
	Unknown/expired sessions return 404; provider failures leave Redis unchanged.
	"""
	client = get_sync_redis_client()
	lock = client.lock(
		_meeting_mutation_lock_key(uid, mid),
		timeout=_MEETING_LOCK_TTL,
		blocking_timeout=_MEETING_LOCK_WAIT,
	)
	with lock:
		meta_key = _meeting_meta_key(uid, mid)
		if not client.get(meta_key):
			raise HTTPException(status_code=404, detail="Meeting session expired or unknown")

		seen_key = _meeting_seen_seq_key(uid, mid)
		if client.sismember(seen_key, str(seq)):
			return {"text": "", "seq": seq, "duplicate": True}

		text = _transcribe_pcm(pcm_b64, sample_rate)
		if text:
			# Save the text and retry marker together under the meeting lock so
			# /end sees either the complete chunk or none of it.
			parts_key = _meeting_parts_key(uid, mid)
			client.rpush(parts_key, text)
			client.sadd(seen_key, str(seq))
			client.expire(parts_key, _MEETING_TTL)
			client.expire(seen_key, _MEETING_TTL)
			client.expire(meta_key, _MEETING_TTL)

	# Cue generation stays outside the lock because it is best-effort and must
	# never delay another chunk or the user's final save.
	if text:
		_maybe_trigger_cue(uid, mid)
	return {"text": text, "seq": seq, "duplicate": False}


def _meeting_end(
	uid: str,
	mid: str,
	title: Optional[str],
	final_pcm_b64: Optional[str],
	sample_rate: int,
	final_seq: Optional[int] = None,
) -> Dict[str, Any]:
	"""Finish one meeting and save its transcript as a searchable note.

	`uid` and `mid` select the user's Redis session; `title` can override its
	stored title, while `final_pcm_b64`/`final_seq` identify the last audio flush.
	The meeting lock waits for active chunks and the completion receipt makes a
	retried `/end` return the original note id. A failed durable write keeps the
	session intact so the client can retry instead of losing the transcript.
	"""
	client = get_sync_redis_client()
	lock = client.lock(
		_meeting_mutation_lock_key(uid, mid),
		timeout=_MEETING_LOCK_TTL,
		blocking_timeout=_MEETING_LOCK_WAIT,
	)
	with lock:
		result_key = _meeting_result_key(uid, mid)
		cached_result = client.get(result_key)
		if cached_result:
			return json.loads(cached_result)

		meta_raw = client.get(_meeting_meta_key(uid, mid))
		if not meta_raw:
			raise HTTPException(status_code=404, detail="Meeting session expired or unknown")
		meta = json.loads(meta_raw)

		# Flush the final partial chunk once. A stable `final_seq` means a lost
		# /end response can be retried without transcribing or appending it twice.
		seen_key = _meeting_seen_seq_key(uid, mid)
		final_already_saved = (
			final_seq is not None and client.sismember(seen_key, str(final_seq))
		)
		if final_pcm_b64 and not final_already_saved:
			try:
				tail = _transcribe_pcm(final_pcm_b64, sample_rate)
				if tail:
					client.rpush(_meeting_parts_key(uid, mid), tail)
					if final_seq is not None:
						client.sadd(seen_key, str(final_seq))
			except HTTPException:
				pass  # a bad final chunk must not lose the rest of the meeting

		parts = client.lrange(_meeting_parts_key(uid, mid), 0, -1) or []
		transcript = "\n".join(p for p in parts if p).strip()[:_MEETING_MAX_CHARS]

		if not transcript:
			result = {"ok": False, "title": None, "uniqueid": None, "chars": 0}
			client.set(result_key, json.dumps(result), ex=_MEETING_TTL)
			_cleanup_meeting_session(client, uid, mid)
			return result

		started = datetime.fromtimestamp(int(meta.get("started") or time.time()), tz=timezone.utc)
		note_title = (title or meta.get("title") or "").strip() or f"Meeting {started:%b %d %H:%M} UTC"
		result = _store_note(uid, transcript, note_title)
		result["chars"] = len(transcript)
		if result.get("ok"):
			# Store the receipt before cleanup so a client that lost the HTTP
			# response gets the same note id instead of creating a duplicate.
			client.set(result_key, json.dumps(result), ex=_MEETING_TTL)
			_cleanup_meeting_session(client, uid, mid)
		return result


def _cleanup_meeting_session(client, uid: str, mid: str) -> None:
	"""Remove one completed meeting's temporary transcript and Cue state.

	`client` is the Redis connection mutated in place; `uid` and `mid` scope every
	deleted key. Callers must first confirm the transcript is empty or durably
	saved because this cleanup cannot recover meeting audio.
	"""
	client.delete(_meeting_meta_key(uid, mid))
	client.delete(_meeting_parts_key(uid, mid))
	client.delete(_meeting_seen_seq_key(uid, mid))
	client.delete(
		_cue_key(uid, mid), _cue_seq_key(uid, mid),
		_cue_mark_key(uid, mid), _cue_lock_key(uid, mid),
		_cue_hist_key(uid, mid),
	)


# --- Live AI cues during a meeting -------------------------------------------
# While a meeting is being transcribed, the backend watches the transcript and
# periodically surfaces ONE short, genuinely useful insight on the lens (like
# Even's native Conversate "AI cues", but powered by the user's own memory).
# Trigger: >= _CUE_MIN_WORDS words OR >= _CUE_MIN_SENTENCES sentences of new
# speech since the last analysis. Pipeline per cue (runs in a daemon thread so
# chunk uploads never wait): plan queries from the conversation -> RAG over the
# user's memory -> follow connections + a second retrieval hop -> optional web
# search (Exa) -> compose one lens-sized insight (or decline with NONE).

_CUE_MIN_WORDS = 20
_CUE_MIN_SENTENCES = 3
_CUE_CONTEXT_CHARS = 2000   # background conversation the planner/composer see;
                            # kept tight so stale topics can't outweigh the
                            # newest speech (~5 min of talk)
_CUE_MAX_LEN = 900          # the newest cue expands into its own card on the lens,
                            # so give it room for a full multi-sentence, compound
                            # explanation (not a fragment). The card shows what
                            # fits; the rest is clipped rather than cut at gen time.
_CUE_LOCK_TTL = 90          # generation lock; expires even if a worker dies
_CUE_HISTORY = 5            # prior cues fed back so the thread builds forward

# Models (OpenRouter). Planning stays on the small query-generation model, while
# the user-visible Cue response uses Gemini Flash for fast live synthesis.
_CUE_PLAN_MODEL = "openai/gpt-4.1-mini"
_CUE_COMPOSE_MODEL = "google/gemini-3.6-flash"


def _cue_key(uid: str, mid: str) -> str:
	return f"glasses:meeting:{uid}:{mid}:cue"


def _cue_seq_key(uid: str, mid: str) -> str:
	return f"glasses:meeting:{uid}:{mid}:cueseq"


def _cue_mark_key(uid: str, mid: str) -> str:
	return f"glasses:meeting:{uid}:{mid}:cuemark"


def _cue_lock_key(uid: str, mid: str) -> str:
	return f"glasses:meeting:{uid}:{mid}:cuelock"


def _cue_hist_key(uid: str, mid: str) -> str:
	return f"glasses:meeting:{uid}:{mid}:cuehist"


def _meeting_transcript(client, uid: str, mid: str) -> str:
	parts = client.lrange(_meeting_parts_key(uid, mid), 0, -1) or []
	return "\n".join(p for p in parts if p).strip()[:_MEETING_MAX_CHARS]


def _maybe_trigger_cue(uid: str, mid: str) -> None:
	"""Check the new-speech threshold and spawn cue generation if it's met.

	Never raises — cue generation is best-effort garnish on top of meeting
	transcription and must not break chunk uploads. The Redis SETNX lock keeps
	at most one generation in flight per meeting.
	"""
	try:
		client = get_sync_redis_client()
		transcript = _meeting_transcript(client, uid, mid)
		mark = int(client.get(_cue_mark_key(uid, mid)) or 0)
		fresh = transcript[mark:]
		# Sentence boundaries approximated by terminal punctuation; STT output
		# is punctuated by Whisper/Deepgram so this is reliable enough.
		if len(fresh.split()) < _CUE_MIN_WORDS and len(re.findall(r"[.!?]", fresh)) < _CUE_MIN_SENTENCES:
			return
		if not client.set(_cue_lock_key(uid, mid), "1", nx=True, ex=_CUE_LOCK_TTL):
			return  # a generation is already running
		threading.Thread(target=_generate_cue_safe, args=(uid, mid), daemon=True).start()
	except Exception:
		traceback.print_exc()


def _generate_cue_safe(uid: str, mid: str) -> None:
	"""Thread entrypoint: run one cue generation, always releasing the lock."""
	client = get_sync_redis_client()
	try:
		_generate_cue(client, uid, mid)
	except Exception as e:
		print(f"[glasses] cue generation failed: {type(e).__name__}: {e}")
		traceback.print_exc()
	finally:
		try:
			client.delete(_cue_lock_key(uid, mid))
		except Exception:
			pass


def _cue_plan(conversation: str, fresh: str) -> Dict[str, Any]:
	"""Stage 1: decide what to look up for this stretch of conversation.

	Returns {"memory_queries": [...], "web_query": str|None}. On any failure,
	falls back to searching memory with the fresh segment itself and no web.
	"""
	try:
		resp = _get_openrouter_client().chat.completions.create(
			model=_CUE_PLAN_MODEL,
			messages=[
				{
					"role": "system",
					"content": (
						"You plan lookups for a real-time meeting copilot that gives the "
						"wearer an EDGE. Given the newest stretch of speech, first infer what "
						"they are actually deciding or wrestling with, then output JSON:\n"
						'{"memory_queries": [up to 3 short searches over the user\'s personal '
						"knowledge base that would surface decision-relevant context: past "
						"decisions on this, related numbers, prior positions, tensions or "
						'commitments they may have forgotten], "web_query": "one web search '
						'if an external fact would sharpen the call, else null"}\n'
						"Aim the queries at what would reveal a blind spot or settle the "
						"decision — not just the nouns mentioned. All queries must be about "
						"the CURRENT topic (what was JUST said); if the subject changed, "
						"ignore earlier topics entirely."
					),
				},
				{
					"role": "user",
					"content": (
						f"JUST SAID (current topic — plan for THIS):\n{fresh}\n\n"
						f"EARLIER CONVERSATION (background only):\n{conversation}"
					),
				},
			],
			max_tokens=200,
			temperature=0.2,
			response_format={"type": "json_object"},
		)
		plan = json.loads(resp.choices[0].message.content or "{}")
		queries = [q.strip() for q in (plan.get("memory_queries") or []) if isinstance(q, str) and q.strip()][:3]
		web_q = plan.get("web_query")
		web_q = web_q.strip() if isinstance(web_q, str) and web_q.strip() else None
		if queries or web_q:
			return {"memory_queries": queries, "web_query": web_q}
	except Exception as e:
		print(f"[glasses] cue planner failed ({type(e).__name__}), using fallback queries")
	return {"memory_queries": [fresh[:200]], "web_query": None}


def _cue_compact_results(raw: List[Dict[str, Any]], limit: int) -> List[str]:
	"""Shrink retrieval records to 'Title: snippet' lines the composer can read."""
	lines: List[str] = []
	seen: set = set()
	for r in raw:
		uniqueid = r.get("uniqueid")
		if uniqueid in seen:
			continue
		seen.add(uniqueid)
		title = (r.get("title") or "").strip() or "(untitled)"
		content = r.get("fullContent") or r.get("content") or r.get("fileText") or r.get("text") or ""
		lines.append(f"- {title}: {_clean_snippet(content, 180)}")
		if len(lines) >= limit:
			break
	return lines


def _cue_connection_titles(raw: List[Dict[str, Any]], limit: int = 6) -> List[str]:
	"""Pull titles of notes CONNECTED to the hits (Obsidian-style links), for the
	second retrieval hop. Connection payload shapes vary, so read defensively."""
	titles: List[str] = []
	for r in raw:
		for c in (r.get("connections") or []):
			t = (c.get("title") if isinstance(c, dict) else str(c) or "").strip()
			if t and t not in titles:
				titles.append(t)
			if len(titles) >= limit:
				return titles
	return titles


def _generate_cue(client, uid: str, mid: str) -> None:
	"""Run the full cue pipeline once and publish the result to Redis."""
	transcript = _meeting_transcript(client, uid, mid)
	mark = int(client.get(_cue_mark_key(uid, mid)) or 0)
	fresh = transcript[mark:].strip()
	if not fresh:
		return
	conversation = transcript[-_CUE_CONTEXT_CHARS:]

	plan = _cue_plan(conversation, fresh)

	# Hop 1: RAG over the user's memory with the planned queries.
	hop1_raw: List[Dict[str, Any]] = []
	try:
		hop1_raw = retrieve_results(
			user_id=uid,
			query=fresh[:300],
			queries=plan["memory_queries"],
			top_k=6,
			similarity_setting=0.45,
			include_postgres_content=True,
		).get("results", []) or []
	except Exception as e:
		print(f"[glasses] cue hop1 retrieval failed: {e}")

	# Hop 2 (multi-hop): follow the connections of hop-1 hits plus their titles,
	# surfacing related context the first query wouldn't have matched directly.
	hop2_raw: List[Dict[str, Any]] = []
	hop2_queries = _cue_connection_titles(hop1_raw) + [
		(r.get("title") or "").strip() for r in hop1_raw[:3] if (r.get("title") or "").strip()
	]
	hop2_queries = [q for q in dict.fromkeys(hop2_queries) if q][:4]
	if hop2_queries:
		try:
			hop2_raw = retrieve_results(
				user_id=uid,
				query=hop2_queries[0],
				queries=hop2_queries,
				top_k=4,
				similarity_setting=0.45,
				include_postgres_content=True,
			).get("results", []) or []
		except Exception as e:
			print(f"[glasses] cue hop2 retrieval failed: {e}")

	# Optional web search (Exa, same tool Stella uses).
	web_lines: List[str] = []
	if plan.get("web_query"):
		try:
			from ai.stella_v2.general_tools import search_web

			web = search_web(plan["web_query"], max_results=3, include_contents=True)
			for item in (web.get("results") or [])[:3]:
				title = (item.get("title") or "").strip()
				snippet = _clean_snippet(item.get("text") or item.get("snippet") or "", 160)
				url = (item.get("url") or "").strip()
				if title or snippet:
					web_lines.append(f"- {title}: {snippet} ({url})")
		except Exception as e:
			print(f"[glasses] cue web search failed: {e}")

	hop1_ids = {r.get("uniqueid") for r in hop1_raw}
	memory_lines = _cue_compact_results(hop1_raw, 5)
	related_lines = _cue_compact_results(
		[r for r in hop2_raw if r.get("uniqueid") not in hop1_ids], 3
	)

	# Stage 2: THINK, then compose one sharp cue — or decline. A bad cue mid-
	# conversation is worse than none. Prior cues thread FORWARD: each new cue
	# should go a step deeper than the last, not surface the same note again.
	prior_cues = [c for c in (client.lrange(_cue_hist_key(uid, mid), 0, -1) or []) if c]
	# Newest speech leads: the cue must be about the CURRENT topic, so the
	# composer sees it first and history is explicitly demoted to background.
	sections = [
		f"JUST SAID (current topic — the cue MUST be about this):\n{fresh}",
		f"EARLIER CONVERSATION (background only — topics here may be stale):\n{conversation}",
	]
	if prior_cues:
		sections.append(
			"INSIGHTS ALREADY GIVEN (build FORWARD from these — go deeper or to the "
			"next step, never restate):\n" + "\n".join(f"- {c}" for c in prior_cues)
		)
	if memory_lines:
		sections.append("FROM THE USER'S MEMORY (raw material to reason over, not to parrot):\n" + "\n".join(memory_lines))
	if related_lines:
		sections.append("CONNECTED/RELATED NOTES (2nd hop):\n" + "\n".join(related_lines))
	if web_lines:
		sections.append("WEB RESULTS:\n" + "\n".join(web_lines))

	resp = _get_openrouter_client().chat.completions.create(
		model=_CUE_COMPOSE_MODEL,
		messages=[
			{
				"role": "system",
				"content": (
					"You are a razor-sharp strategic advisor whispering to someone mid-"
					"conversation through their smart glasses. You see the live talk, what "
					"they just said, their own notes and memory, related material, and web "
					"facts.\n\n"
					"CRITICAL -- SYNTHESIS, NOT RECALL: The user already knows what is in "
					"their own notes. Reading a memory back to them is useless. Their past "
					"memories are raw FUEL, never the output. Your only job is to SYNTHESIZE: "
					"fuse what they JUST said with one or more things from their memory (or "
					"the web) to produce ONE conclusion they have NOT reached yet. The shape "
					"is always: [something from their past] + [what is happening right now] = "
					"[an edge they cannot see themselves]. If your cue could get the reply "
					"'yeah, I know, that is in my notes', it has FAILED -- rewrite it into the "
					"insight that follows FROM those notes.\n\n"
					f"Output ONE cue, max {_CUE_MAX_LEN} characters, plain text, no markdown, "
					"no emoji. It must be a NEW thought, doing at least one of these, "
					"specific to the decision they are wrestling with right now:\n"
					"- Connect the dots: tie what they are saying now to a past decision, "
					"number, or tension so a non-obvious pattern pops ('You hit this exact "
					"wall in the Q2 launch -- it was not pricing then either').\n"
					"- Name a blind spot or non-obvious implication ('Your churn note blames "
					"onboarding, not price -- this discount will not fix it').\n"
					"- Point one concrete path forward their own history supports ('Ship the "
					"half-day pilot first; your notes show full builds stall here').\n"
					"- Deliver a decision-changing fact (memory or web) that flips the "
					"current call.\n\n"
					"Reason across the sources and LAND a conclusion. Never restate a note, "
					"never say 'your note says' or echo a title -- draw the inference the "
					"notes point to.\n\n"
					"When you lack enough to make a confident call, ASK the one sharp "
					"question that would unlock it -- the missing piece of data that would "
					"change the decision ('What's the actual CAC? That decides if $10/mo "
					"works'). Lean toward ending with a pointed question whenever a real "
					"unknown is blocking a clear decision: good advisors ask more than they "
					"assert. But the question must expose a genuine decision-relevant "
					"unknown -- never generic coaching like 'have you considered...'.\n\n"
					"Be concrete and specific to what they're deciding. Build FORWARD from "
					"insights already given -- go deeper or to the next step, never restate. "
					"Stay on the CURRENT topic; if the subject changed, older material is "
					"off-limits.\n\n"
					"FORMAT -- READ THIS TWICE: Write a MULTI-SENTENCE, COMPOUND explanation "
					"(3 to 5 sentences) in plain, everyday language. Lay out the reasoning: "
					"state the insight, connect it to the specific memory/fact it rests on, "
					"explain WHY it matters for the decision right now, and end with the "
					"concrete next step or the one sharp question that unlocks it. It must "
					"stand on its own and fully explain itself -- a busy person reads it once "
					"and gets the whole thought. NEVER output a cryptic fragment, an aphorism, "
					"a headline, or a half-finished thought (e.g. 'full transcripts are a "
					"graveyard' is BANNED -- it explains nothing). Say the actual insight and "
					"the reasoning behind it, in real words. If you genuinely have nothing "
					"sharp and new to add, output exactly NONE."
				),
			},
			{"role": "user", "content": "\n\n".join(sections)[:14000]},
		],
		max_tokens=1200,
		temperature=0.5,
	)
	cue_text = (resp.choices[0].message.content or "").strip()

	# Log exactly what OpenRouter spent so we can see if reasoning/"thinking"
	# tokens are eating the budget and truncating the visible cue. finish_reason
	# == "length" means the model hit max_tokens (cut off); a big reasoning_tokens
	# with small output chars means thinking ate the budget.
	try:
		_choice = resp.choices[0]
		_usage = getattr(resp, "usage", None)
		_finish = getattr(_choice, "finish_reason", None)
		_prompt_tok = getattr(_usage, "prompt_tokens", None) if _usage else None
		_completion_tok = getattr(_usage, "completion_tokens", None) if _usage else None
		_total_tok = getattr(_usage, "total_tokens", None) if _usage else None
		# Reasoning tokens live under completion_tokens_details on reasoning models.
		_details = getattr(_usage, "completion_tokens_details", None) if _usage else None
		_reasoning_tok = None
		if _details is not None:
			_reasoning_tok = getattr(_details, "reasoning_tokens", None)
			if _reasoning_tok is None and isinstance(_details, dict):
				_reasoning_tok = _details.get("reasoning_tokens")
		print(
			f"[glasses] cue compose usage model={_CUE_COMPOSE_MODEL} "
			f"finish={_finish} prompt_tok={_prompt_tok} completion_tok={_completion_tok} "
			f"reasoning_tok={_reasoning_tok} total_tok={_total_tok} "
			f"out_chars={len(cue_text)} max_tokens=1200"
		)
		if _finish == "length":
			print("[glasses] cue WARNING: hit max_tokens (finish_reason=length) -- output truncated")
	except Exception as _e:
		print(f"[glasses] cue usage log failed: {type(_e).__name__}: {_e}")

	# Advance the mark even when declining, so the same words aren't re-analyzed;
	# new speech re-arms the trigger naturally.
	client.set(_cue_mark_key(uid, mid), str(len(transcript)), ex=_MEETING_TTL)

	if not cue_text or cue_text.upper() == "NONE" or len(cue_text) < 8:
		print(f"[glasses] cue declined for {mid}")
		return
	# Clamp at a word boundary — a mid-word cutoff on the lens reads as a glitch.
	if len(cue_text) > _CUE_MAX_LEN:
		cut = cue_text[:_CUE_MAX_LEN]
		cue_text = cut[: cut.rfind(" ")].rstrip(" ,;:") if " " in cut else cut
	seq = client.incr(_cue_seq_key(uid, mid))
	client.expire(_cue_seq_key(uid, mid), _MEETING_TTL)
	client.set(
		_cue_key(uid, mid),
		json.dumps({"seq": seq, "text": cue_text, "ts": int(time.time())}),
		ex=_MEETING_TTL,
	)
	# Remember what was shown so later cues thread forward from it (not repeat).
	hist_key = _cue_hist_key(uid, mid)
	client.rpush(hist_key, cue_text)
	client.ltrim(hist_key, -_CUE_HISTORY, -1)
	client.expire(hist_key, _MEETING_TTL)
	print(f"[glasses] cue #{seq} for {mid}: {cue_text}")


def _meeting_cue(uid: str, mid: str, after_seq: int) -> Dict[str, Any]:
	"""Return the newest cue if it's newer than what the lens already shows."""
	client = get_sync_redis_client()
	if not client.get(_meeting_meta_key(uid, mid)):
		raise HTTPException(status_code=404, detail="Meeting session expired or unknown")
	raw = client.get(_cue_key(uid, mid))
	if not raw:
		return {"cue": None}
	cue = json.loads(raw)
	return {"cue": cue if int(cue.get("seq") or 0) > after_seq else None}


@router.post("/meeting/start")
async def meeting_start(
	payload: MeetingStartReq,
	request: Request,
	auth: AuthContext = Depends(require_auth),
):
	"""Open a meeting-transcription session.

	Logs the calling client's Origin so we can tell WHICH lens build is running:
	the hosted app (Origin: https://www.constella.app, always latest) vs. a
	packaged .ehpk (null/other origin, frozen at pack time). A stale package is
	invisible server-side otherwise — it uploads chunks fine but never polls
	/meeting/cue, so cues generate into the void.
	"""
	try:
		print(
			f"[glasses] meeting/start client origin={request.headers.get('origin')!r} "
			f"ua={(request.headers.get('user-agent') or '')[:80]!r}"
		)
		return await asyncio.to_thread(_meeting_start, auth.sub, payload.title)
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/meeting/chunk")
async def meeting_chunk(payload: MeetingChunkReq, auth: AuthContext = Depends(require_auth)):
	"""Transcribe and append one audio chunk to an open meeting."""
	try:
		return await asyncio.to_thread(
			_meeting_append, auth.sub, payload.meeting_id, payload.pcm_base64, payload.sample_rate, payload.seq
		)
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/meeting/cue")
async def meeting_cue(payload: MeetingCueReq, auth: AuthContext = Depends(require_auth)):
	"""Poll for the newest live AI cue of an open meeting (lens calls this)."""
	try:
		return await asyncio.to_thread(_meeting_cue, auth.sub, payload.meeting_id, payload.after_seq)
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/meeting/end")
async def meeting_end(payload: MeetingEndReq, auth: AuthContext = Depends(require_auth)):
	"""Finalize a meeting: assemble transcript, store as searchable memory."""
	try:
		return await asyncio.to_thread(
			_meeting_end,
			auth.sub,
			payload.meeting_id,
			payload.title,
			payload.pcm_base64,
			payload.sample_rate,
			payload.final_seq,
		)
	except HTTPException:
		raise
	except Exception as e:
		traceback.print_exc()
		sentry_sdk.capture_exception(e)
		raise HTTPException(status_code=500, detail=str(e)) from e


# --- The phone-browser login page (inline, self-contained) ------------------

_LINK_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
<title>Link your glasses · Constella</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0f1115; color: #e9ecf1; padding: 24px; }
  .card { width: 100%; max-width: 380px; background: #171a21; border: 1px solid #262b36;
    border-radius: 16px; padding: 28px 24px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  p.sub { margin: 0 0 20px; color: #99a2b2; font-size: 14px; line-height: 1.45; }
  label { display: block; font-size: 13px; color: #b7bfcc; margin: 14px 0 6px; }
  input { width: 100%; padding: 12px 14px; font-size: 16px; border-radius: 10px;
    border: 1px solid #2d333f; background: #0f1216; color: #e9ecf1; }
  input:focus { outline: none; border-color: #4c8bf5; }
  button { width: 100%; margin-top: 18px; padding: 13px; font-size: 15px; font-weight: 600;
    border: none; border-radius: 10px; background: #4c8bf5; color: #fff; cursor: pointer; }
  button:disabled { opacity: .55; cursor: default; }
  .msg { margin-top: 14px; font-size: 13px; min-height: 18px; }
  .msg.err { color: #ff7a7a; }
  .msg.ok { color: #67d98b; }
  .step { display: none; }
  .step.active { display: block; }
  .code-in { letter-spacing: 4px; text-align: center; font-size: 20px; }
</style>
</head>
<body>
  <div class="card">
    <h1>Link your glasses</h1>
    <p class="sub">Sign in with your Constella email to connect your smart glasses to your memory.</p>

    <div id="step1" class="step active">
      <label for="email">Email</label>
      <input id="email" type="email" inputmode="email" autocomplete="email" placeholder="you@example.com" />
      <button id="sendBtn">Send code</button>
      <div id="msg1" class="msg"></div>
    </div>

    <div id="step2" class="step">
      <label for="code">6-digit code (check your email)</label>
      <input id="code" class="code-in" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="123456" />
      <label for="userCode">Code shown on your glasses</label>
      <input id="userCode" value="__PREFILL_CODE__" placeholder="XXXX-XXXX" />
      <button id="verifyBtn">Verify &amp; link</button>
      <div id="msg2" class="msg"></div>
    </div>
  </div>

<script>
  var email = "";
  function q(id){ return document.getElementById(id); }
  function show(id, text, ok){ var el = q(id); el.textContent = text; el.className = "msg " + (ok ? "ok" : "err"); }

  q("sendBtn").addEventListener("click", async function(){
    email = (q("email").value || "").trim().toLowerCase();
    if (!email || email.indexOf("@") < 0) { show("msg1", "Enter a valid email.", false); return; }
    q("sendBtn").disabled = true; show("msg1", "Sending…", true);
    try {
      var res = await fetch("/glasses/auth/link/request-code", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email })
      });
      if (!res.ok) throw new Error("request failed");
      q("step1").classList.remove("active"); q("step2").classList.add("active");
      show("msg2", "Code sent to " + email, true); q("code").focus();
    } catch (e) { show("msg1", "Could not send code. Try again.", false); }
    finally { q("sendBtn").disabled = false; }
  });

  q("verifyBtn").addEventListener("click", async function(){
    var code = (q("code").value || "").trim();
    var userCode = (q("userCode").value || "").trim();
    if (code.length < 6) { show("msg2", "Enter the 6-digit code.", false); return; }
    if (!userCode) { show("msg2", "Enter the code shown on your glasses.", false); return; }
    q("verifyBtn").disabled = true; show("msg2", "Linking…", true);
    try {
      var res = await fetch("/glasses/auth/link/verify", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_code: userCode, email: email, code: code })
      });
      var data = await res.json().catch(function(){ return {}; });
      if (!res.ok) { show("msg2", data.detail || "Invalid code.", false); q("verifyBtn").disabled = false; return; }
      q("step2").innerHTML = '<h1 style="margin-top:8px">All set</h1><p class="sub">Your glasses are linked. Head back to them, they\\'ll connect automatically.</p>';
    } catch (e) { show("msg2", "Something went wrong. Try again.", false); q("verifyBtn").disabled = false; }
  });
</script>
</body>
</html>"""
