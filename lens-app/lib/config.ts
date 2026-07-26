// Backend base URL. Override with NEXT_PUBLIC_API_BASE at build time. The domain
// must be allowlisted in the backend CORS config (constella.app / www.constella.app
// already are) and in the glasses app.json network whitelist.
export const API_BASE: string = (
  process.env.NEXT_PUBLIC_API_BASE || 'https://fastfind.app'
).replace(/\/$/, '');

// Glasses mic delivers signed-16-bit LE mono PCM at 16 kHz (per Even docs).
export const SAMPLE_RATE = 16000;

// Safety cap so a forgotten recording doesn't stream forever.
export const MAX_RECORD_MS = 60_000;

// --- Voice-activity detection (auto-stop so no second tap is needed) ---
// RMS of s16 samples above this counts as speech. Glasses mics idle well below
// ~300; normal speech lands well above 1000.
export const VAD_RMS_THRESHOLD = 500;
// Stop after this much continuous silence once speech has been heard.
export const VAD_SILENCE_MS = 1600;
// Give up if no speech is heard at all within this window.
export const VAD_MAX_WAIT_FOR_SPEECH_MS = 6000;
// Never auto-stop before this much total recording (guards against a clipped
// first syllable counting as "speech then silence").
export const VAD_MIN_RECORD_MS = 700;

// --- Meeting mode ---
// Upload cadence for continuous transcription. Short chunks = near-live
// transcript (words appear a few seconds after they're spoken). Transcription
// now runs on our own GPU (whisper-server) so each call is free — cheap to go
// fast. ~4s of 16kHz s16le PCM is ~128KB raw, trivial over the phone link.
export const MEETING_CHUNK_MS = 4_000;
// Hard ceiling on one meeting session (backend session TTL is 4h).
export const MEETING_MAX_MS = 3 * 60 * 60 * 1000;
// How often the lens polls for a new live AI cue while recording. Kept tight so
// a fresh insight lands on the card quickly after enough new speech accrues.
export const MEETING_CUE_POLL_MS = 4000;
