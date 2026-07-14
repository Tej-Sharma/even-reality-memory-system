// Wrapper around the Even Hub SDK: initializes the bridge, owns the single
// full-screen text container we render everything into, captures mic audio, and
// routes touchpad/ring input to a handler. Keeps the SDK surface in one place so
// the rest of the app deals in plain strings + callbacks.
//
// Hardware facts this file encodes (verified against SDK 0.0.11 + the official
// ASR template + community notes):
// - CLICK_EVENT is enum 0 and protobuf omits zero-valued fields, so a real tap
//   often arrives with eventType MISSING. The fix is per-envelope: if a
//   text/list/sys event envelope is present, a missing eventType means CLICK.
//   Never blanket-treat undefined as tap across all events.
// - The simulator delivers taps via sysEvent; real hardware delivers them via
//   textEvent/listEvent of the isEventCapture container. Handle all three.
// - audioControl() resolves to a boolean; denial is a silent `false`, never an
//   exception. Always check it.
// - PCM arrives as Uint8Array frames on event.audioEvent.audioPcm (s16le,
//   16 kHz, mono, ~10ms frames) through the same onEvenHubEvent channel.
import {
  AudioInputSource,
  CreateStartUpPageContainer,
  OsEventTypeList,
  TextContainerProperty,
  TextContainerUpgrade,
  waitForEvenAppBridge,
} from '@evenrealities/even_hub_sdk';

import {
  SAMPLE_RATE,
  VAD_MAX_WAIT_FOR_SPEECH_MS,
  VAD_MIN_RECORD_MS,
  VAD_RMS_THRESHOLD,
  VAD_SILENCE_MS,
} from './config';

// The SDK's TS types aren't exhaustive for event payloads, so we keep a loose
// Bridge shape and lean on the concrete classes imported above for calls.
export interface Bridge {
  createStartUpPageContainer(arg: any): Promise<number>;
  textContainerUpgrade(arg: any): Promise<any>;
  shutDownPageContainer(mode: number): Promise<any>;
  audioControl(on: boolean, source?: any): Promise<boolean>;
  onEvenHubEvent(cb: (event: any) => void): () => void;
  getLocalStorage?(key: string): Promise<string | null>;
  setLocalStorage?(key: string, value: string): Promise<void>;
}

export type InputKind = 'tap' | 'double' | 'up' | 'down' | 'exit';

const CONTAINER_ID = 1;
const CONTAINER_NAME = 'main';

let bridge: Bridge | null = null;
let recording = false;
let audioChunks: Uint8Array[] = [];
let chunkCount = 0;
let debugEnabled = false;
let lastEventSummary = '';

/** Wrap a bridge promise with a timeout so a silent host never wedges the app.
 *  Resolves to `fallback` on timeout instead of hanging forever. */
function withTimeout<T>(p: Promise<T>, ms: number, fallback: T): Promise<T> {
  return Promise.race([
    p,
    new Promise<T>((resolve) => setTimeout(() => resolve(fallback), ms)),
  ]);
}

export function setDebug(on: boolean) {
  debugEnabled = on;
}
export const isDebug = () => debugEnabled;
export const getLastEventSummary = () => lastEventSummary;
export const getChunkCount = () => chunkCount;

/** Initialize the bridge and paint the first frame. Must be awaited before any
 *  other SDK call (on hardware the bridge isn't ready until this resolves). */
export async function initGlasses(initialText: string): Promise<Bridge> {
  bridge = (await waitForEvenAppBridge()) as unknown as Bridge;

  const main = new TextContainerProperty({
    xPosition: 0,
    yPosition: 0,
    width: 576,
    height: 288,
    borderWidth: 0,
    borderColor: 5,
    paddingLength: 6,
    containerID: CONTAINER_ID,
    containerName: CONTAINER_NAME,
    content: initialText,
    isEventCapture: 1, // this container receives all input events
  });

  const result = await bridge.createStartUpPageContainer(
    new CreateStartUpPageContainer({ containerTotalNum: 1, textObject: [main] })
  );
  if (result !== 0) {
    // 1=invalid params, 2=oversize, 3=out of memory. Audio control is only
    // available AFTER a successful startup page, so surface this loudly.
    console.error('[glasses] createStartUpPageContainer failed with code', result);
    lastEventSummary = `startup page failed: code ${result}`;
  }
  return bridge;
}

/** Replace the on-lens text (flicker-free). Clips defensively; appends a debug
 *  footer (last event + audio chunk count) when ?debug=1. */
export async function render(text: string): Promise<void> {
  if (!bridge) return;
  let content = (text || '').slice(0, 800);
  if (debugEnabled) {
    content += `\n--\ndbg ev:${lastEventSummary || 'none'} chunks:${chunkCount}`;
  }
  await withTimeout(
    bridge.textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: CONTAINER_ID,
        containerName: CONTAINER_NAME,
        content,
      })
    ),
    3000,
    false
  );
}

/** Exit the app to the system (required behavior on the root page). */
export async function exitApp(): Promise<void> {
  try {
    if (recording) await stopRecording();
    await bridge?.shutDownPageContainer(1);
  } catch {
    /* ignore */
  }
}

/** Subscribe to touchpad/ring input, normalized to InputKind. Audio frames are
 *  siphoned off here too while recording (same event channel). */
export function onInput(handler: (kind: InputKind) => void): void {
  bridge?.onEvenHubEvent((event: any) => {
    // --- Audio branch first: PCM frames flow through this same channel ---
    const audio = event?.audioEvent;
    if (audio?.audioPcm != null) {
      if (recording) {
        const bytes = toBytes(audio.audioPcm);
        if (bytes && bytes.length) {
          audioChunks.push(bytes);
          chunkCount++;
          trackVoiceActivity(bytes);
        }
      }
      return;
    }

    // --- Input branch: taps arrive via textEvent/listEvent on hardware and
    // sysEvent on the simulator. CLICK(0) is omitted on the wire, so coalesce
    // missing eventType to 0 ONLY when an envelope is actually present. ---
    const src = event?.textEvent ?? event?.listEvent ?? event?.sysEvent;
    if (!src) return;
    const type = Number(src.eventType ?? 0);
    lastEventSummary = `${event?.textEvent ? 'text' : event?.listEvent ? 'list' : 'sys'}:${type}`;
    console.log('[glasses] input event', lastEventSummary, src);

    switch (type) {
      case OsEventTypeList.CLICK_EVENT: // 0
        handler('tap');
        break;
      case OsEventTypeList.DOUBLE_CLICK_EVENT: // 3
        handler('double');
        break;
      case OsEventTypeList.SCROLL_TOP_EVENT: // 1
        handler('up');
        break;
      case OsEventTypeList.SCROLL_BOTTOM_EVENT: // 2
        handler('down');
        break;
      case OsEventTypeList.ABNORMAL_EXIT_EVENT: // 6
      case OsEventTypeList.SYSTEM_EXIT_EVENT: // 7
        handler('exit'); // host is tearing us down — release the mic
        break;
      default:
        break; // foreground enter/exit, IMU, etc.
    }
  });
}

// --- Voice-activity detection state (drives no-second-tap auto-stop) ---
let vadSpeechHeard = false;
let vadLastLoudTs = 0;
let vadRecordStartTs = 0;
let vadFired = false;
let vadAutoStopCb: (() => void) | null = null;

/** Per-frame VAD: RMS of the s16le samples decides speech vs silence, and the
 *  auto-stop callback fires once after trailing silence (or no speech at all).
 *  Byte-wise s16 decode avoids Int16Array alignment traps on sliced buffers. */
function trackVoiceActivity(chunk: Uint8Array): void {
  if (!vadAutoStopCb || vadFired) return;
  const n = chunk.length >> 1;
  let sum = 0;
  for (let i = 0; i < n; i++) {
    let v = chunk[2 * i] | (chunk[2 * i + 1] << 8);
    if (v >= 0x8000) v -= 0x10000;
    sum += v * v;
  }
  const rms = Math.sqrt(sum / Math.max(1, n));
  const now = Date.now();
  if (rms >= VAD_RMS_THRESHOLD) {
    vadSpeechHeard = true;
    vadLastLoudTs = now;
    return;
  }
  const elapsed = now - vadRecordStartTs;
  const silentFor = now - vadLastLoudTs;
  const shouldStop = vadSpeechHeard
    ? elapsed > VAD_MIN_RECORD_MS && silentFor > VAD_SILENCE_MS
    : elapsed > VAD_MAX_WAIT_FOR_SPEECH_MS;
  if (shouldStop) {
    vadFired = true;
    const cb = vadAutoStopCb;
    vadAutoStopCb = null;
    cb();
  }
}

/** Start capturing from the glasses mic. Returns true when the host actually
 *  opened the mic — a silent `false` is the documented denial path.
 *  `onAutoStop` fires once when VAD detects the utterance has ended. */
export async function startRecording(onAutoStop?: () => void): Promise<boolean> {
  if (!bridge || recording) return recording;
  audioChunks = [];
  chunkCount = 0;
  vadSpeechHeard = false;
  vadFired = false;
  vadRecordStartTs = Date.now();
  vadLastLoudTs = vadRecordStartTs;
  vadAutoStopCb = onAutoStop ?? null;
  let ok = false;
  try {
    ok = await withTimeout(
      bridge.audioControl(true, AudioInputSource.Glasses),
      5000,
      false
    );
  } catch (e) {
    console.error('[glasses] audioControl(true) threw', e);
    ok = false;
  }
  recording = ok;
  if (!ok) {
    lastEventSummary = 'audioControl(true) -> false';
    vadAutoStopCb = null;
  }
  return ok;
}

/** Stop capture and return the whole utterance as base64 s16le PCM. */
export async function stopRecording(): Promise<string> {
  if (!bridge) return '';
  recording = false;
  vadAutoStopCb = null; // manual stop wins — cancel any pending auto-stop
  try {
    await withTimeout(bridge.audioControl(false), 3000, false);
  } catch {
    /* ignore */
  }
  const total = audioChunks.reduce((n, c) => n + c.length, 0);
  const merged = new Uint8Array(total);
  let off = 0;
  for (const c of audioChunks) {
    merged.set(c, off);
    off += c.length;
  }
  audioChunks = [];
  return merged.length ? u8ToBase64(merged) : '';
}

export const isRecording = () => recording;
export const sampleRate = () => SAMPLE_RATE;

/** Meeting mode: take everything recorded so far as raw bytes and reset the
 *  buffer while the mic KEEPS running. Caller owns encoding/upload/retries. */
export function drainRecordingBuffer(): Uint8Array {
  const total = audioChunks.reduce((n, c) => n + c.length, 0);
  const merged = new Uint8Array(total);
  let off = 0;
  for (const c of audioChunks) {
    merged.set(c, off);
    off += c.length;
  }
  audioChunks = [];
  return merged;
}

/** Encode raw PCM bytes for upload (exposed for meeting-mode chunking). */
export const pcmToBase64 = (bytes: Uint8Array) => (bytes.length ? u8ToBase64(bytes) : '');

// Normalize a host audioPcm payload (Uint8Array | number[] | base64 string) to bytes.
// SDK 0.0.11 already normalizes to Uint8Array, but keep the fallbacks for safety.
function toBytes(pcm: unknown): Uint8Array | null {
  if (pcm instanceof Uint8Array) return pcm;
  if (Array.isArray(pcm)) return Uint8Array.from(pcm as number[]);
  if (typeof pcm === 'string') {
    try {
      const bin = atob(pcm);
      const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out;
    } catch {
      return null;
    }
  }
  return null;
}

// Chunked base64 so large PCM buffers don't blow the call stack on fromCharCode.
function u8ToBase64(bytes: Uint8Array): string {
  let bin = '';
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    bin += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return btoa(bin);
}
