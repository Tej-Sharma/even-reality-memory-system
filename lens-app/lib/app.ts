// Constella for Even G2 — app logic.
//
// One-tap flow: tap → speak → VAD auto-stops on silence → backend transcribes,
// decides ask-vs-note itself, and acts. No modes, no second tap (a tap while
// recording still force-stops early). Controls:
//   tap          speak (or stop early / dismiss result)
//   double-tap   exit
//
// Exposed as startGlassesApp() and invoked once from the React client entry.
// Design rule learned from hardware debugging: ALWAYS render feedback BEFORE
// awaiting a bridge call, and never trust a bridge call to resolve or succeed —
// audioControl denial is a silent `false`, and a hung call must not wedge the
// state machine (glasses.ts wraps everything in timeouts).
import {
  ApiError,
  fetchMe,
  meetingChunk,
  meetingCue,
  meetingEnd,
  meetingStart,
  voiceAudio,
} from './api';
import {
  MAX_RECORD_MS,
  MEETING_CHUNK_MS,
  MEETING_CUE_POLL_MS,
  MEETING_MAX_MS,
} from './config';
import {
  type InputKind,
  drainRecordingBuffer,
  enterMeetingLayout,
  exitApp,
  exitMeetingLayout,
  getChunkCount,
  getLastEventSummary,
  initGlasses,
  isRecording,
  onInput,
  pcmFromBase64,
  pcmToBase64,
  render,
  renderCueList,
  renderRec,
  renderTranscript,
  sampleRate,
  setDebug,
  startRecording,
  stopRecording,
} from './glasses';
import { runLogin } from './login';
import { bindStore, clearSession, getToken, saveView } from './store';

type Phase = 'idle' | 'recording' | 'busy' | 'result' | 'meeting';
type View = 'quick' | 'meeting';

let phase: Phase = 'idle';
let view: View = 'quick';
let recordTimer: ReturnType<typeof setTimeout> | null = null;
let bootStarted = false;

// Live meeting session state (only while phase === 'meeting').
let meetingId = '';
let meetingStartedAt = 0;
let meetingSeq = 0;
let meetingTail = '';
let meetingPending: Uint8Array[] = [];
let meetingUploadPromise: Promise<void> | null = null;
let meetingChunkTimer: ReturnType<typeof setInterval> | null = null;
let meetingTickTimer: ReturnType<typeof setInterval> | null = null;
// Live AI cues shown on the lens. Newest is expanded at the top; older ones
// collapse into a list below it. meetingCues[0] is the most recent.
let meetingCues: string[] = [];
let meetingCueSeq = 0;
let meetingCueBusy = false;
// Whether the newest cue's card is expanded (full text) or collapsed (preview).
// A new cue auto-expands; tapping the card toggles it.
let meetingCueExpanded = true;
let meetingCueTimer: ReturnType<typeof setInterval> | null = null;
// True once the two-pane meeting layout (transcript + bordered cue card) is
// live. False = host rejected the rebuild, so we fall back to the single
// container with the cue inlined as ">> ..." text.
let meetingTwoPane = false;

function homeScreen(): string {
  if (view === 'meeting') {
    return [
      'MEETING mode',
      '',
      'Tap to start transcribing.',
      'The whole meeting is saved',
      'to memory when you finish.',
      '',
      'Swipe: quick mode',
    ].join('\n');
  }
  return [
    'Constella',
    '',
    'Tap, speak, done.',
    'Ask or capture a thought.',
    '',
    'Swipe: meeting mode',
    'Double-tap: exit',
  ].join('\n');
}

async function goIdle() {
  phase = 'idle';
  await render(homeScreen());
}

async function beginRecording() {
  phase = 'recording';
  // Feedback FIRST — if the mic fails we still want the screen to react to the tap.
  await render(
    'Listening...\n\nJust stop talking when done\n(or tap to stop).'
  );
  // VAD fires finishRecording after trailing silence — that's the "no second tap".
  const ok = await startRecording(() => void finishRecording());
  if (!ok) {
    phase = 'result';
    await render(
      'Mic unavailable.\n\nCheck glasses are worn +\nconnected, then tap to retry.'
    );
    return;
  }
  // Hard cap so a noisy room can't keep the mic open forever.
  recordTimer = setTimeout(() => void finishRecording(), MAX_RECORD_MS);
}

async function finishRecording() {
  if (phase !== 'recording') return;
  if (recordTimer) {
    clearTimeout(recordTimer);
    recordTimer = null;
  }
  phase = 'busy';
  await render('On it...');
  const pcm = await stopRecording();
  if (!pcm) {
    // Mic opened but no PCM frames arrived — distinct from "mic denied".
    await render(
      `No audio received (${getChunkCount()} frames).\n\nTap to try again.`
    );
    phase = 'result';
    return;
  }
  try {
    const res = await voiceAudio(pcm, sampleRate());
    if (res.intent === 'ask') {
      const header = res.transcript ? `You: ${res.transcript}\n\n` : '';
      await render(`${header}${res.display_text || 'No matching memories.'}`);
    } else if (res.intent === 'note') {
      await render(
        res.ok
          ? `Saved.\n${res.title || res.transcript || ''}`
          : "Couldn't save that.\nTap to retry."
      );
    } else {
      await render("Didn't catch that.\nTap to try again.");
    }
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      // Token went stale — drop it and bounce back to login.
      await clearSession();
      await render('Session expired.\nRe-linking...');
      await runLogin();
      await goIdle();
      return;
    }
    // Show the backend's actual error detail — "Something went wrong" hides
    // real causes (e.g. transcription provider quota) and makes debugging on
    // hardware impossible.
    const detail =
      e instanceof ApiError
        ? `Error ${e.status}: ${e.message}`.slice(0, 160)
        : 'Network error - check phone connection.';
    await render(`${detail}\n\nTap to continue.`);
  }
  phase = 'result';
}

// --- Meeting mode -----------------------------------------------------------

function meetingElapsed(): string {
  const s = Math.floor((Date.now() - meetingStartedAt) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** One collapsed list line: strip newlines and clip to keep it single-line. */
function oneLine(text: string, max: number): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

/** Build the cue-list text: newest insight expanded at the top, older ones
 *  collapsed into a short list beneath it (most recent first). */
function formatCueList(): string {
  if (!meetingCues.length) return 'Listening for insights...';
  const [newest, ...older] = meetingCues;
  // Newest card: full text when expanded, a one-line preview when collapsed.
  let out = meetingCueExpanded
    ? newest
    : `${oneLine(newest, 90)}\n(tap card to expand)`;
  if (older.length) {
    const items = older
      .slice(0, 4)
      .map((c) => `• ${oneLine(c, 58)}`)
      .join('\n');
    out += `\n\n— earlier —\n${items}`;
  }
  return out;
}

async function renderMeetingScreen() {
  // Meeting HUD: cue list up top (its own container), REC badge top-right, live
  // transcript along the bottom. Each is its own container so a new cue never
  // reflows the transcript and vice-versa.
  if (meetingTwoPane) {
    const tail = meetingTail ? `...${meetingTail.slice(-140)}` : '(listening)';
    await renderRec(`● REC  ${meetingElapsed()}`);
    await renderCueList(formatCueList());
    await renderTranscript(`${tail}   ·   Tap card: expand   ·   Double-tap: save`);
    return;
  }
  // Fallback (single container): newest cue inline above a longer transcript.
  // Surface WHY the card HUD is off so we can diagnose on-device without ?debug.
  const newest = meetingCues[0] || '';
  const tail = meetingTail
    ? `...${meetingTail.slice(newest ? -80 : -180)}`
    : '(listening)';
  const cueBlock = newest ? `>> ${newest}\n\n` : '';
  await render(
    `Recording meeting  ${meetingElapsed()}\n\n${cueBlock}${tail}\n\n[${getLastEventSummary()}]\nTap: finish & save`
  );
}

/** Poll the backend for a fresh AI cue (memory + connections + web insight the
 *  server derives from the conversation). Re-renders only when a new one lands. */
async function pollMeetingCue() {
  if (phase !== 'meeting' || !meetingId || meetingCueBusy) return;
  meetingCueBusy = true;
  try {
    const res = await meetingCue(meetingId, meetingCueSeq);
    if (res.cue && res.cue.seq > meetingCueSeq) {
      meetingCueSeq = res.cue.seq;
      // Newest goes to the front and auto-expands; the previous one collapses
      // into the list below it.
      meetingCues.unshift(res.cue.text);
      if (meetingCues.length > 8) meetingCues.length = 8;
      meetingCueExpanded = true; // a fresh insight always opens expanded
      // Touch only the cue-list container so the transcript doesn't reflow.
      if (meetingTwoPane) void renderCueList(formatCueList());
      else void renderMeetingScreen();
    }
  } catch {
    /* cue polling is best-effort; recording carries on */
  } finally {
    meetingCueBusy = false;
  }
}

function concatBytes(parts: Uint8Array[]): Uint8Array {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}

/** Drain the mic buffer and upload it as one transcript chunk. Failed uploads
 *  are re-queued so audio is never dropped — the next tick retries them. */
async function uploadMeetingChunk() {
  const fresh = drainRecordingBuffer();
  if (fresh.length) meetingPending.push(fresh);
  if (meetingUploadPromise || !meetingPending.length || !meetingId) {
    return meetingUploadPromise;
  }
  const batch = meetingPending;
  meetingPending = [];
  const seq = meetingSeq;
  meetingUploadPromise = (async () => {
    try {
      const res = await meetingChunk(
        meetingId,
        pcmToBase64(concatBytes(batch)),
        sampleRate(),
        seq
      );
      meetingSeq = seq + 1;
      if (res.text) meetingTail = res.text;
      if (phase === 'meeting') void renderMeetingScreen();
    } catch {
      meetingPending.unshift(...batch); // retry with the same seq on the next tick
    } finally {
      meetingUploadPromise = null;
    }
  })();
  return meetingUploadPromise;
}

async function beginMeeting() {
  phase = 'busy';
  await render('Starting meeting...');
  try {
    const { meeting_id } = await meetingStart();
    meetingId = meeting_id;
  } catch (e) {
    phase = 'result';
    const detail =
      e instanceof ApiError
        ? `Error ${e.status}: ${e.message}`.slice(0, 120)
        : 'Network error.';
    await render(`Couldn't start meeting.\n${detail}\n\nTap to continue.`);
    return;
  }
  const ok = await startRecording(); // no VAD auto-stop in meeting mode
  if (!ok) {
    meetingId = '';
    phase = 'result';
    await render(
      'Mic unavailable.\n\nCheck glasses are worn +\nconnected, then tap to retry.'
    );
    return;
  }
  meetingStartedAt = Date.now();
  meetingSeq = 0;
  meetingTail = '';
  meetingPending = [];
  meetingCues = [];
  meetingCueSeq = 0;
  meetingCueExpanded = true;
  phase = 'meeting';
  // Switch to the meeting HUD (cue list + REC badge + transcript strip). If the
  // host rejects the rebuild, meetingTwoPane stays false and we fall back to the
  // single-container inline cue layout.
  meetingTwoPane = await enterMeetingLayout(
    'Listening for insights...',
    '(listening)   ·   Tap card: expand   ·   Double-tap: save',
    '● REC  0:00'
  );
  meetingChunkTimer = setInterval(
    () => void uploadMeetingChunk(),
    MEETING_CHUNK_MS
  );
  meetingCueTimer = setInterval(
    () => void pollMeetingCue(),
    MEETING_CUE_POLL_MS
  );
  meetingTickTimer = setInterval(() => {
    if (Date.now() - meetingStartedAt > MEETING_MAX_MS) {
      void finishMeeting(); // hard cap
      return;
    }
    void renderMeetingScreen();
  }, 5000);
  await renderMeetingScreen();
}

/** Stop recording and durably finalize the current meeting.
 *  This clears timers/Cue state, waits for `meetingUploadPromise`, retries any
 *  pending audio with its stable sequence, and changes `phase` to the result
 *  screen. The backend keeps the same note receipt safe across request retries. */
async function finishMeeting() {
  if (phase !== 'meeting') return;
  phase = 'busy';
  if (meetingChunkTimer) clearInterval(meetingChunkTimer);
  if (meetingTickTimer) clearInterval(meetingTickTimer);
  if (meetingCueTimer) clearInterval(meetingCueTimer);
  meetingChunkTimer = meetingTickTimer = meetingCueTimer = null;
  meetingCues = [];
  meetingCueSeq = 0;
  // Collapse the meeting HUD back to one full container so the result and later
  // screens render normally.
  if (meetingTwoPane) {
    await exitMeetingLayout('Saving meeting...');
    meetingTwoPane = false;
  } else {
    await render('Saving meeting...');
  }

  // Wait for the active upload, then retry any failed batch with its same
  // sequence id so /end cannot overtake it or save duplicate transcript text.
  if (meetingUploadPromise) await meetingUploadPromise;
  await uploadMeetingChunk();

  // Stop the mic before assembling the final flush so frames arriving during
  // shutdown join any failed upload batch instead of disappearing.
  const stoppedPcm = await stopRecording();
  if (stoppedPcm) meetingPending.push(pcmFromBase64(stoppedPcm));
  const finalPcm = pcmToBase64(concatBytes(meetingPending));
  meetingPending = [];
  const id = meetingId;
  const finalSeq = meetingSeq;
  try {
    const res = await meetingEnd(
      id,
      finalPcm || undefined,
      sampleRate(),
      undefined,
      finalSeq
    );
    meetingId = '';
    // Show the real count: raw chars under 1k (so a short meeting never reads
    // "0k"), otherwise a 1-decimal "k" figure.
    const chars = res.chars || 0;
    const charsLabel =
      chars < 1000 ? `${chars} chars` : `${(chars / 1000).toFixed(1)}k chars`;
    await render(
      res.ok
        ? `Meeting saved.\n${res.title || ''}\n(${charsLabel})\n\nAsk me about it anytime.`
        : 'Nothing captured.\nTap to continue.'
    );
  } catch (e) {
    meetingId = '';
    const detail =
      e instanceof ApiError
        ? `Error ${e.status}: ${e.message}`.slice(0, 120)
        : 'Network error.';
    await render(`Couldn't save meeting.\n${detail}\n\nTap to continue.`);
  }
  phase = 'result';
}

function handleInput(kind: InputKind, containerId?: number) {
  // Host teardown (system/abnormal exit): flush what we can, release the mic.
  if (kind === 'exit') {
    if (phase === 'meeting') {
      void finishMeeting(); // best-effort save before the WebView dies
    } else if (isRecording()) {
      void stopRecording();
    }
    return;
  }

  // Double-tap: in a meeting it finishes & saves (protect the data);
  // everywhere else it exits the app.
  if (kind === 'double') {
    if (phase === 'meeting') {
      void finishMeeting();
      return;
    }
    void exitApp();
    return;
  }

  if (kind === 'up' || kind === 'down') {
    if (phase === 'recording' || phase === 'busy' || phase === 'meeting')
      return;
    view = view === 'quick' ? 'meeting' : 'quick';
    void saveView(view); // boot lands on the last-used view next launch
    void goIdle();
    return;
  }

  // tap
  switch (phase) {
    case 'idle':
      if (view === 'meeting') void beginMeeting();
      else void beginRecording();
      break;
    case 'recording':
      void finishRecording(); // early stop — VAD would have gotten there anyway
      break;
    case 'meeting':
      // In the HUD the cue card is the sole tap-capture container, so a single
      // tap toggles expand/collapse and DOUBLE-tap finishes (handled above). In
      // the single-container fallback there's no card, so a tap finishes.
      if (meetingTwoPane) {
        meetingCueExpanded = !meetingCueExpanded;
        void renderCueList(formatCueList());
      } else {
        void finishMeeting();
      }
      break;
    case 'result':
      void goIdle();
      break;
    case 'busy':
      break; // ignore taps while working
  }
}

// Scripted screens for store screenshots (?demo=1 in the Even simulator).
// Rendered through the real lens pipeline; tap advances to the next screen.
const DEMO_SCREENS: string[] = [
  [
    'Constella',
    '',
    'Tap, speak, done.',
    'Ask or capture a thought.',
    '',
    'Swipe: meeting mode',
    'Double-tap: exit',
  ].join('\n'),
  'Listening...\n\nJust stop talking when done\n(or tap to stop).',
  [
    'You: what did Diane say about the pilot?',
    '',
    'Diane wants the pilot extended two weeks',
    'and a usage report before the renewal',
    'call on Friday.',
  ].join('\n'),
  'Saved: Follow up with Diane before Friday',
  [
    'MEETING mode',
    '',
    'Tap to start transcribing.',
    'The whole meeting is saved',
    'to memory when you finish.',
    '',
    'Swipe: quick mode',
  ].join('\n'),
  [
    'Recording meeting  12:41',
    '',
    "...so let's lock the launch for the 24th",
    'and Sarah owns the demo video. I will',
    'draft the pricing page by Monday.',
    '',
    'Tap: finish & save',
  ].join('\n'),
  'Meeting saved.\nProduct sync - launch planning\n(14k chars)\n\nAsk me about it anytime.',
];

async function runDemoMode(): Promise<void> {
  let i = 0;
  await initGlasses(DEMO_SCREENS[0]);
  onInput((kind) => {
    if (kind !== 'tap') return;
    i = (i + 1) % DEMO_SCREENS.length;
    void render(DEMO_SCREENS[i]);
  });
}

/** Static demo of the live meeting HUD (cue card + REC badge + transcript) with
 *  sample data and NO backend/auth — used to capture store screenshots of the
 *  AI-cue feature. Tapping the card toggles expand/collapse like the real app. */
async function runCueDemo(): Promise<void> {
  await initGlasses('Constella');
  meetingCues = [
    'Your Q2 retention note pins churn on onboarding friction, not price, so ' +
      "the discount you're weighing won't move the needle. Ship the guided-setup " +
      'pilot you sketched in March first, then re-test pricing. What is day-1 ' +
      'activation right now? That decides which lever actually matters.',
    'Pricing tests here stalled twice before, both on positioning not cost.',
    'Diane flagged this same enterprise blocker in the Linear thread Tuesday.',
  ];
  meetingCueExpanded = true;
  meetingTwoPane = await enterMeetingLayout(
    formatCueList(),
    '...so if we drop it to ten a month, does that actually fix the churn or',
    '● REC  12:47'
  );
  // If the host rejects the rebuild, at least paint the fallback so the screen
  // isn't blank during capture.
  if (!meetingTwoPane) await render(`>> ${meetingCues[0]}`);
  onInput((kind) => {
    if (kind !== 'tap') return;
    meetingCueExpanded = !meetingCueExpanded;
    void renderCueList(formatCueList());
  });
}

/** Boot the glasses app. Safe to call multiple times; only the first runs. */
export async function startGlassesApp(): Promise<void> {
  if (bootStarted) return;
  bootStarted = true;
  try {
    try {
      const demoParam = new URLSearchParams(window.location.search).get('demo');
      if (demoParam === 'cue') {
        await runCueDemo();
        return;
      }
      if (demoParam === '1') {
        await runDemoMode();
        return;
      }
    } catch {
      /* ignore */
    }
    // Boot params:
    //   ?debug=1          on-lens debug footer (last event + PCM frame count)
    //   ?mode=meeting     boot into MEETING home (one tap from launch to record)
    //   ?autostart=1      with mode=meeting: start recording immediately on open
    let autostartMeeting = false;
    try {
      const params = new URLSearchParams(window.location.search);
      setDebug(params.get('debug') === '1');
      if (params.get('mode') === 'meeting') {
        view = 'meeting';
        autostartMeeting = params.get('autostart') === '1';
      }
    } catch {
      /* ignore */
    }

    const bridge = await initGlasses('Constella\n\nStarting...');
    bindStore(bridge);
    onInput(handleInput);

    // No boot param? Restore whichever view was used last (SDK storage survives
    // app restarts), so meeting-heavy users land one tap from recording.
    if (view === 'quick') {
      try {
        if ((await bridge.getLocalStorage?.('constella_view')) === 'meeting') {
          view = 'meeting';
        }
      } catch {
        /* ignore */
      }
    }

    // Release the mic if the WebView unloads mid-recording.
    window.addEventListener('beforeunload', () => {
      if (isRecording()) void stopRecording();
    });

    // Validate an existing token; if missing/invalid, run the login handshake.
    let loggedIn = false;
    if (await getToken()) {
      try {
        await fetchMe();
        loggedIn = true;
      } catch {
        await clearSession();
      }
    }
    if (!loggedIn) {
      await runLogin();
    }

    if (autostartMeeting) {
      await beginMeeting();
      return;
    }
    await goIdle();
  } catch (e) {
    console.error('fatal', e);
    void render('Startup error.\nReopen the app.');
  }
}
