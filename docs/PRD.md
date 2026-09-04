# PRD — Personal Local Meeting/Interview Assist Tool ("Parakeet Clone")

**Owner:** Jon
**Status:** v1.2 — Phase 1 (MVP) complete as of Day 11 (2026-09-03), success criteria in §11 met
**Scope:** Strictly personal use. Not for distribution, sale, or third-party use.

---

## 1. Overview

A Windows desktop tool that listens to system audio during a live call (work meeting, mock interview, or real interview), transcribes it locally in real time, and — on a trigger — asks a locally-invoked Claude (via the Claude Code CLI, using Jon's existing Claude.ai subscription, not a paid API key) to generate a suggested response. The suggestion appears in an app window Jon can glance at during the call.

A post-call summary feature is explicitly a later phase, not part of the MVP.

## 2. Goals

- Real-time-enough suggested responses during live work meetings and interviews (mock, and eventually real).
- Run entirely locally on Windows: local speech-to-text, local audio capture, Claude invoked through the CLI using an existing subscription — no per-token API billing.
- Low-friction display: a normal window to start, a toggleable always-on-top overlay later.
- Nothing about this needs to look or behave like a shippable product — it needs to work reliably for one user, on one machine.

## 3. Non-Goals

- Not distributed, packaged as an installer, or sold to anyone else.
- Not integrating with specific meeting platforms (Zoom/Teams/Meet APIs) — audio capture is platform-agnostic via system loopback.
- Not building disclosure/consent-management features — that's a decision Jon makes per-use, not something the app automates or enforces.
- Not multi-user, not cloud-synced, no accounts.
- Speaker diarization, meeting-bot integrations, and a polished installable app are out of scope unless a later phase explicitly adds them.

## 4. Users & Use Cases

Single user (Jon). Three use modes, same underlying pipeline:

1. **Work meetings** (primary) — live assist during calls, eventually with a post-meeting summary of what was discussed.
2. **Mock interviews** — practice runs, live or reviewed after.
3. **Real job interviews** — used live, as interview practice.

The suggestion prompt should differ by mode (a work-meeting suggestion looks different from an interview-answer suggestion), so "mode" is a first-class setting, not an afterthought.

## 5. Legal & Ethical Considerations (acknowledged, not re-litigated)

Flagged once during planning, carried into the design rather than ignored:


- The Philippines' Anti-Wiretapping Act (RA 4200) generally requires consent to record private communications. This shapes one concrete design default: **the MVP does not persist raw audio or transcripts to disk.** Everything lives in memory for the duration of a session and is discarded on close unless a later phase adds an explicit, opt-in "save this session" action.

This isn't legal advice, just a design constraint carried from the earlier discussion.

## 6. System Architecture

**Revised after Day 0 feasibility testing** (see §12 for the log). WSL cannot capture Windows system audio loopback — WSLg's PulseAudio bridge only carries WSL-internal GUI-app audio, never native Windows app output. This forces a two-process, two-OS-environment architecture:

```
Windows-native process                          WSL process
------------------------                        -----------
WASAPI loopback capture      --PCM frames-->     VAD-segmented local STT (faster-whisper)
Global hotkey listener       --trigger evt-->    Pause/silence trigger detection
PySide6 window                                   Long-lived `claude` CLI subprocess
 (transcript + suggestions)  <--text-------      (stream-json mode)
                              local TCP socket,
                              WSL-side listens,
                              Windows-side connects
                              (WSL2 localhost forwarding)
```

Key components:

- **Audio capture (Windows-native):** WASAPI loopback via `pyaudiowpatch` or `soundcard`, run as a native Windows Python process — not inside WSL. Captures what's playing out of speakers/headphones (the other participant's side). Own-mic capture is deferred to the summary phase (§8, Phase 3).
- **GUI (Windows-native):** PySide6 window (transcript pane + suggestions pane) runs in the *same* native Windows process as audio capture, not via WSLg. Decided this way specifically so Phase 2's always-on-top/click-through overlay doesn't require relocating the GUI later — it's already native.
- **IPC:** a local TCP socket between the two processes. WSL-side listens on `127.0.0.1:<port>`; the Windows-side process connects to it as a client, relying on WSL2's default localhost-forwarding (a port opened inside WSL is reachable from Windows via `localhost`) — the well-supported direction, versus WSL initiating a connection out to a Windows-hosted port. Two lightweight message types: raw PCM audio frames (Windows → WSL) and JSON control/text messages (both directions: transcript segments and suggestions WSL → Windows; hotkey trigger events and mode/settings changes Windows → WSL).
- **Local STT (WSL):** `faster-whisper` (`small.en`, CPU, int8), chunked by voice-activity detection rather than fixed time windows.
- **Claude invocation (WSL):** one long-lived `claude` CLI process per session in headless streaming mode (`--input-format stream-json --output-format stream-json`, confirmed viable in Day 0) rather than spawning a fresh process per suggestion — avoids per-call CLI cold-start overhead. Context (mode, rolling transcript window) is managed by our app and streamed in, not the CLI's own session/`--continue` behavior, so meetings never cross-contaminate.
- **Trigger logic:** pause detection runs on the WSL side (it already sees the audio stream for STT); the manual hotkey is captured natively on Windows and forwarded to WSL over the control socket.
- **One repo, one dev workflow:** both sides live in the same git repo (`windows_app/`, `wsl_app/`), authored from a single `claude` CLI session run in WSL. The Windows-native component doesn't need a second Claude Code setup — it's just Python code that gets *executed* by a native Windows Python install. WSL can invoke Windows binaries directly (`python.exe`) via its built-in interop, so the Windows-side process can typically be launched right from the WSL shell without opening a separate terminal.

## 7. Open Decisions Needing Your Sign-off

I'm proposing defaults below so we're not stalled on every micro-choice — flag any you want changed before I write the dev plan:

| # | Decision | Proposed default | Why |
|---|---|---|---|
| 1 | STT engine/model | `faster-whisper`, `small.en`, CPU-only (int8 quantized for speed) | Confirmed: AMD GPU, no CUDA — no local GPU acceleration path for faster-whisper, so we tune for CPU speed instead |
| 2 | GUI framework | Python + PySide6 (Qt) | Native Windows always-on-top/click-through overlay support later, same language as the audio/STT stack, no second runtime |
| 3 | Trigger mechanism | Auto-pause-detection + manual hotkey, both enabled | Maximizes responsiveness without losing manual control |
| 4 | Claude session model | One long-lived streaming CLI process per meeting session, app-managed rolling context | Avoids per-call cold-start latency; avoids cross-session context bleed |
| 5 | Persistence | Off by default in MVP; nothing written to disk unless explicitly enabled later | Matches the legal/privacy consideration in §5 |
| 6 | Windows-only for now | Confirmed | You confirmed this already — listed here for completeness |
| 7 | Build workflow | Single `claude` CLI session, run in WSL, authors both `windows_app/` and `wsl_app/` in one repo. Windows-side code is executed via WSL→Windows interop (`python.exe`) or a separate Windows terminal | Confirmed Day 0: `claude` works in WSL with subscription auth. No need for a second Claude Code setup just because part of the runtime is Windows-native |
| 8 | Repo | Local git repo + private GitHub remote | Version history and off-machine backup, kept private. Created in Day 0: `github.com/jjaguilar08/adarna-ai`, nothing committed yet |
| 9 | Python version | 3.10.12 (WSL's existing version) — no upgrade to 3.11 | Nothing in the stack (faster-whisper, PySide6, sockets/asyncio) actually requires 3.11+; that was an arbitrary default on my part, not a real requirement |
| 10 | Audio capture location | Native Windows process (confirmed necessary, not optional) | Day 0 proved WSLg's PulseAudio bridge can't see native Windows app audio — `RDPSink.monitor` only carries WSL-internal GUI-app sound |
| 11 | GUI location | Native Windows process, same one as audio capture | Since a native Windows component is now required anyway, building the GUI there too avoids relocating it for Phase 2's overlay later |
| 12 | IPC mechanism | Local TCP socket, WSL-side listens, Windows-side connects (WSL2 localhost forwarding) | The better-supported connection direction for WSL2; avoids relying on the WSL virtual-switch gateway IP from the Windows side |

## 8. Functional Requirements by Phase

### Phase 1 — Live Assist Core (MVP)
- Start/stop a session from the app window.
- Live system-audio capture via WASAPI loopback, with a device picker and a "test capture" button.
- Local streaming STT producing a visible rolling transcript.
- Mode toggle: "Work Meeting" vs "Interview" (changes the suggestion system prompt/tone).
- Auto-trigger on pause + manual hotkey trigger, both feeding the long-lived Claude CLI subprocess.
- Suggested responses appear in-window, appended to a running list for that session (so you can scroll back within a live call). **Superseded Day 13 — see Phase 1.5b below.**
- One-click copy-to-clipboard for any suggestion.
- Settings: audio device, Whisper model size, mode, silence threshold.
- No disk persistence of audio or transcript by default.

### Phase 1.5 — Suggestion & Context Refinements
Added post-MVP (2026-09-03), based on real usage of the Phase 1 build. Touches core suggestion logic only — no UI relocation — done before Phase 2 so the overlay is built against the improved behavior rather than needing rework afterward.

- **Pre-session context notes:** a multi-line text box in the settings panel to paste free-form notes before starting a session — a CV/job description for interviews, a PRD/agenda for meetings. Sent once as part of `settings_changed` (same pattern as mode/model/pause settings), included in the system prompt/context for that session only, not hot-swapped mid-session.
- **Output style change (both modes):** suggestions switch from a ready-to-read spoken script to terms/concepts/key points the user builds their own answer from — e.g. asked about OOP principles, the output is terms like "Encapsulation, Inheritance, Polymorphism, Abstraction" rather than a full sentence to say aloud. Applies to both Work Meeting and Interview modes. This is a system-prompt change, not a new message type. **Reverted Day 13 — see Phase 1.5b below.**
- **Auto-suggest on/off toggle, changeable mid-session:** when on, behaves as today (pause-triggered + hotkey/button both generate). When off, pause-triggered generation is suppressed entirely — only an explicit manual trigger (the existing hotkey, plus a corresponding in-window button) generates a suggestion, from the most recent transcript context. Toggling happens live during a running session (a new control in the window, and worth deciding during implementation whether the existing global hotkey also gets a toggle-specific variant or stays purely "generate now").

### Phase 1.5b — Output & Display Adjustments
Added 2026-09-03, after one real session's use of Phase 1.5's terms-style output and growing suggestion list. Small, targeted changes rather than a new phase of work.

- **Output style, reverted to script-style, both modes:** the terms/concepts style above didn't land well in practice — reverting to a ready-to-read script, but more detailed than Day 6's original single bare line: a few sentences (2-4) with real substance/reasoning behind them, not just one line, still short enough to skim and say in the moment.
- **Suggestion display auto-clears on each generation:** the suggestion pane now shows only the latest suggestion — generating a new one automatically replaces the previous one, rather than appending to a growing scrollable list. Trades away in-session scroll-back (Phase 1's original behavior) for a pane that never gets crowded.

### Phase 2 — Overlay + Polish
- Toggleable always-on-top overlay. Runs alongside the main window, not in place of it — both stay open independently, with a separate global hotkey to show/hide the overlay (distinct from the existing suggestion-trigger hotkey). **Core landed Day 14** (2026-09-03) — one item still open: a real physical keypress test of the overlay hotkey (and both hotkeys back-to-back) hasn't been confirmed by the user yet.
- **Visual redesign, Day 18 (2026-09-04):** the user shared a real ParakeetAI screenshot as a reference — the overlay is being restyled to match its look (dark, semi-transparent, rounded panel, clean glanceable typography), and scope grew slightly from Day 14's "suggestion only" decision: the overlay now also shows the question/transcript excerpt that prompted the suggestion, displayed above the answer (a new `suggestion` protocol field carrying that context). Explicitly kept out of scope for now, even though the reference screenshot shows them: a session timer, a manual "Clear" button, and the screenshot/chat toolbar buttons — those last two are genuinely new capabilities (screen capture + analysis, free-form chat) treated as separate future features, not part of the overlay polish work.
- Draggable/resizable overlay, adjustable opacity, optional click-through mode (toggleable — mutually exclusive with dragging while enabled). Being built alongside the visual redesign in Day 18.
- Explicit, opt-in session persistence (transcript + suggestions saved locally) — off unless turned on. Not started.

### Phase 2.5 — Transcript & Suggestion Refinements
Added 2026-09-03, based on real usage after the overlay landed. Inserted ahead of the remaining Phase 2 polish/persistence work (drag/resize/opacity/click-through, persistence) since these were flagged as higher-priority.

- **Real-time streaming transcript:** the current pipeline waits for a VAD-detected pause (400ms silence) before transcribing a whole closed segment with Whisper — this reads as slow and chunky compared to something like Google Meet's live captions, and fast speech in particular doesn't transcribe accurately under the current batch-per-segment approach. **Feasibility spike done Day 16, implemented and confirmed working Day 17 (2026-09-04).** Real numbers from the spike: `faster-whisper` has no incremental/cached decoding, so true ~1s word-level captions aren't viable on this hardware; the practical ceiling is `base.en` at a ~1.5-2s partial-update cadence, with a 68-95% revision rate (most partial words get corrected at least once before settling) — a genuine, expected flicker, not a bug, since this is a batch model repeatedly re-decoding a growing window rather than a purpose-built streaming acoustic model like Google Meet's. Implementation (`wsl_app/streaming_transcriber.py`) replaced the old segment-then-transcribe pipeline with LocalAgreement-2 streaming, found and fixed two additional real bugs beyond the spike's original buffer-trim finding (unbounded buffer growth silently dropping content; a trim-point calculation that could regress backward), and removed the Whisper-model-size setting entirely since the live path now always requires `base.en`. Live-tested on the real Windows app and user-confirmed working, no issues.
- **Multilingual/code-switching accuracy:** the Day 12 open item (Filipino/Taglish speech garbling under English-only models) was evaluated alongside the streaming spike. Multilingual `small` matched `small.en` exactly on clean English at no latency cost, but didn't clearly fix the Taglish test either; only `medium` showed real improvement, at ~3.5x the latency — ruled out for the live path. **Still open**: the spike's Taglish test used an English TTS voice reading Taglish text, not real Filipino-accented speech, so this isn't treated as a real answer — needs a genuine recorded sample before a final call. Day 17 stays on `base.en` for now rather than adopting an unproven multilingual switch.
- **Suggestion format — script + bullets:** suggestions become a short 1-2 sentence lead (the "script" framing), followed by 3-5 bullet points of supporting details/angles — closer to how the real ParakeetAI presents suggestions, letting the user pick and choose what to actually say rather than reading a fixed script verbatim. Applies to both modes, replacing Day 13's plain multi-sentence script style. Lower-risk than the transcript work — a prompt/formatting change, not an architecture change.

### Phase 3 — Summary
- Add mic capture alongside loopback so both sides of the conversation are transcribed.
- Post-meeting summary generation (key points, decisions, action items) via the Claude CLI.
- Export summary to markdown/txt.

### Phase 4 — Possible future work (not committed)
- Speaker diarization improvements, meeting-platform-specific hooks, a session history browser.

## 9. Non-Functional Requirements

- **Latency:** suggestion visible within **~5–8 seconds** of trigger firing, after the first turn of a session (first turn may be slower due to CLI startup). **Resolved Day 10** — this target was ~2-4s originally, then flagged as a likely 8-12s+ problem from Day 6's isolated component measurements (STT 2.4–6.1s, Claude CLI 4.02–6.23s). Day 10's real 17-minute end-to-end session measured actual pause-to-suggestion at 3-5s in the typical case (STT dropped to 1.4-2.1s under real conditions; the pause timer + CLI call make up the rest), for a realistic total of ~5-8s. Decision: accept ~5-8s as the target rather than trading real cost (smaller Whisper model, a tighter prompt) for a smaller gap than assumed. The occasional 10-20s+ outlier tracked to `claude` CLI subprocess-recycling CPU contention, not model size or prompt length — a scheduling issue, not a latency-budget one; worth a future look but not a Day 10/11 blocker.
- **Resource usage:** must not noticeably degrade the actual meeting app's performance; STT model size is adjustable to trade accuracy for CPU/GPU load.
- **Reliability:** a crashed CLI subprocess should be detected and restarted without losing the live transcript already captured.
- **Privacy:** no data leaves the machine except the Claude CLI's own calls under your existing authenticated session; no telemetry, no analytics.
- **Packaging:** runs as a dev tool from source (Python venv) — no installer needed.

## 10. Risks & Mitigations

- **Subscription rate limits:** a chatty real-time tool could hit Pro/Max usage limits faster than normal chat use → mitigated by trigger-gating (not a suggestion every few seconds) and graceful in-app handling of auth/rate-limit errors.
- **STT accuracy** on accented speech, crosstalk, or poor audio → adjustable model size, manual re-trigger as fallback.
- **WASAPI loopback quirks** (device changes, default device switching mid-call) → explicit device selection + test-capture button rather than always trusting the OS default.
- **CLI streaming mode is being used outside its typical interactive use** → its exact behavior (rate limits, stream-json stability, session semantics) needs to be verified empirically early, before the rest of the build depends on it. This should be the very first spike in the dev plan, not assumed.
- **Legal exposure** from persisted recordings → default-off persistence in Phase 1, addressed deliberately in Phase 2.
- **Two-process architecture adds real failure surface**: socket disconnects, one side crashing while the other keeps running, WSL2 localhost-forwarding quirks, a possible first-run Windows Firewall prompt for the native process → explicit reconnect/restart handling is now its own dev-plan day (see Day 9) rather than an afterthought.
- **All testing to date (Days 4-11) used TTS/synthetic playback, never a real Zoom/Teams call.** Day 11 found that a real USB headset's WASAPI loopback stops delivering audio callbacks during *any* silence gap when the source is discrete TTS renders (each `Speak()` call opens/closes its own render session) — a real meeting app very likely holds one continuously-open stream instead, which should behave differently, but this hasn't been directly confirmed → do a first real-call test early, before relying on this for anything high-stakes like a real interview.

## 12. Day 0 Feasibility Log

- ✅ `claude` CLI headless stream-json mode confirmed viable (`--input-format stream-json --output-format stream-json` exist and work; `-p ... --output-format json` returns clean JSON). Architecture in §6 builds on this directly.
- ✅ Subscription auth confirmed (`oauthAccount` in `~/.claude.json`, no `ANTHROPIC_API_KEY` set). `total_cost_usd` in CLI JSON output is list-price telemetry only, not an actual charge.
- ❌ WSL-native audio loopback capture does not work and structurally can't via WSLg's PulseAudio bridge (`RDPSink.monitor` stayed `SUSPENDED`, recorded pure silence — confirmed via `sox stat`, not just assumed). This is the finding that forced the §6 architecture revision.
- Repo created: `github.com/jjaguilar08/adarna-ai` (private), local clone at `/home/jon/projects/adarna-ai`, git initialized, nothing committed yet.
- Python 3.10.12 confirmed sufficient — no upgrade needed (see §7, decision 9).

## 11. Success Criteria (MVP Definition of Done)

**✅ Met — Day 11 (2026-09-03).** All four criteria confirmed in one real, unbroken ~15-minute end-to-end session, hotkey included (4/4 real presses, 4/4 suggestions in 2-3s each).

- Can start the app during a real Windows work meeting (audio via speakers/headphones) and see a live rolling transcript of the other side of the conversation.
- A suggestion appears in-window within a few seconds of either a detected pause or a manual hotkey press.
- Runs through a 15–30 minute mock session end-to-end without crashing or requiring a restart.
- No audio or transcript is written to disk unless explicitly enabled.

---

**Next steps:** once you sign off on this (and the open decisions in §7), I'll write a day-by-day dev plan, starting with a Day 0/1 spike specifically to validate the `claude` CLI's streaming headless behavior under subscription auth — since the whole latency story in §6 depends on that working the way I've assumed.
