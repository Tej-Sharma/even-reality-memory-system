# Even Reality Memory System

An open-source **AI memory system for [Even Realities G2](https://www.evenrealities.com/) smart glasses**. Talk to your glasses to capture thoughts, ask your own knowledge base out loud, and turn whole meetings into searchable memory — with live AI insights on the lens as you speak.

<img width="576" height="288" alt="3-recall-answer" src="https://github.com/user-attachments/assets/da76ff2e-39c7-4680-bd91-bf0cf2ee2b81" />


No phone in your hand. Just say it, and it's remembered. Ask, and it answers.


> Built on top of [Constella](https://constella.app), an open knowledge base. This repo is the glasses integration extracted on its own so you can see exactly how it works and build your own.

---

## Why this exists

Out of the box, smart glasses can't remember anything for you. There's no "second brain" behind them — no place your thoughts land, no way to ask what you already know. This project wires a real memory system to the glasses:

- **Capture** a thought the instant you have it, by voice, hands-free.
- **Recall** anything you've saved with a real AI answer (not just a search box) that can also reach your connected tools.
- **Ingest** entire meetings into your memory, and get **live AI cues** on the lens while the conversation is still happening.

The green images throughout this README are real frames from the glasses display (576×288, monochrome green — that's the actual hardware).

---

## The three features

### 1. Instant voice capture

Say *"remember that Diane wants the pilot extended two weeks"* and it lands in your knowledge base. The glasses do the speech-to-text; the backend decides whether you're capturing or asking, and acts.

<p align="center">
  <img src="docs/images/listening.png" width="380" alt="Listening state">
  &nbsp;
  <img src="docs/images/saved-note.png" width="380" alt="Saved note confirmation">
</p>

### 2. Recall that's a real AI answer

Ask *"what did Diane say about the pilot?"* and you get a composed answer, not three raw snippets. Under the hood it runs a full agent loop with tool calls: hybrid RAG search over your memory (dense vectors + keyword, fused), plus your connected integrations, all sized down to fit the lens.

<p align="center">
  <img src="docs/images/recall.png" width="480" alt="AI recall answer on the lens">
</p>

### 3. Meeting mode with live AI cues

Start a meeting and the mic streams continuously. Every stretch of speech is transcribed and saved as one searchable note. **While you talk**, the backend quietly analyzes the conversation, searches your memory and its connections, checks the web when useful, and surfaces one sharp insight on the lens — no interaction needed.

<p align="center">
  <img src="docs/images/meeting-mode.png" width="300" alt="Meeting mode start">
  &nbsp;
  <img src="docs/images/meeting-recording.png" width="300" alt="Meeting recording with live transcript">
  &nbsp;
  <img src="docs/images/meeting-saved.png" width="300" alt="Meeting saved to memory">
</p>

---

## How it works

Three pieces, and one important fact about the platform: **no code runs on the glasses**. Your app is a web app running inside the Even phone app's WebView; the glasses are a Bluetooth display plus a 4-mic array. The phone app relays touch/audio events in and renders your container out.

```
┌────────────────┐   PCM audio / taps    ┌──────────────────┐   HTTPS   ┌─────────────────────┐
│  Even G2       │ ───────────────────►  │  Lens app        │ ────────► │  Backend router      │
│  glasses       │                       │  (WebView, TS)   │           │  (FastAPI, Python)   │
│  576×288 green │ ◄─────────────────── │  lens-app/       │ ◄──────── │  backend/            │
└────────────────┘   text to display     └──────────────────┘   JSON    └─────────────────────┘
                                                                              │
                                                        ┌─────────────────────┼─────────────────────┐
                                                        ▼                     ▼                     ▼
                                                  Speech-to-text        Hybrid RAG            Web search
                                                  (Whisper / Deepgram)  (vector + keyword)    + agent tools
```

### Repository layout

| Path | What it is |
|------|-----------|
| `backend/glasses_router.py` | The whole backend: device-link login, speech-to-text, capture/ask routing, agent-loop recall, meeting transcription, and the live-cue engine. A FastAPI router. |
| `lens-app/` | The app that runs on the glasses (TypeScript). State machine, mic capture with voice-activity detection, the Even Hub SDK wrapper, device-link login UI, and the meeting/cue UI. |
| `package/` | Builds the `.ehpk` package you upload to the Even developer portal. |
| `docs/images/` | The lens screenshots used in this README. |

### A few design decisions worth calling out

- **Device-link login.** The glasses have no keyboard, so login works like a TV: the lens shows a short code + URL, you finish the email login on your phone, and the glasses poll until they get a token.
- **Capture vs. ask routing is deterministic first, LLM second.** Explicit trigger words and question shapes are handled by rules; only genuinely ambiguous utterances hit a small model. Ambiguity defaults to *saving*, because a misfiled note is recoverable and a lost thought is not.
- **Live cues never block recording.** Cue generation (plan → RAG → follow connections → optional web search → compose one insight, or decline) runs in a background thread behind a lock. The lens just polls for the newest one. A bad cue mid-conversation is worse than none, so the model is told to stay silent when it has nothing genuinely useful.
- **Everything is sized for a tiny green display.** Answers are clamped to plain text, no markdown, no emoji, a few hundred characters.

---

## Running it

This is the integration layer, lifted out of a larger product so the moving parts are readable. The backend router imports a handful of internal helpers (memory search, note storage, auth, Redis, an email sender) — swap those for your own equivalents, or wire it into your own knowledge base.

**Lens app (local dev on the glasses):**

```bash
cd lens-app
# point it at your backend
export NEXT_PUBLIC_API_BASE="https://your-backend.example.com"
# serve it (any static host / Next.js app-router route works),
# then load the URL as a dev app in the Even phone app via QR.
```

**Package for the Even developer portal:**

```bash
cd package
./build.sh          # bundles the lens app and produces an .ehpk
```

**Backend:** drop `glasses_router.py` into a FastAPI app and mount it. It expects environment variables for the services it calls (STT provider keys, an OpenAI-compatible LLM endpoint, Redis, etc.) — never hardcode these; read them from the environment. No credentials are included in this repo.

---

## Platform notes (learned the hard way on real hardware)

- The glasses activate on the **"Hey Even"** wake word or a long-press menu. Even's own AI skills (QuickList, Translate, etc.) intercept some phrasings *before* your app sees them — words like "remember" and "recall" reliably get through.
- The Even app **fires every utterance twice**, so capture needs a short dedupe window.
- Hub apps are **foreground-only**: keep the Even app open on the phone during use.
- The SDK creates its bridge globals on import, so detect the real WebView with the host-injected handle, not the SDK's own globals.

---

## Credits

Built by [Tejas Sharma](https://github.com/Tej-Sharma) on top of [Constella](https://constella.app). Uses the [Even Hub SDK](https://hub.evenrealities.com) and [Even Realities G2](https://www.evenrealities.com/) glasses.

## License

[MIT](LICENSE) — do what you like with it.
