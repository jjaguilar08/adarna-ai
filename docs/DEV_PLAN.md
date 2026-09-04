# Dev Plan — Personal Local Meeting/Interview Assist Tool

Paced for a couple hours most days. Each "Day" below is a milestone, not a strict calendar day — if one runs long or short, just pick up where you left off. I'll hand you one Claude Code prompt per milestone, in your next message's reply, only after you sign off on the milestone before it.

Scope for this plan: **Phase 1 (MVP) only**, per the PRD. Phase 2 (overlay) and Phase 3 (summary) get their own plans once Phase 1 is working end-to-end.

---

**Status: Day 0 complete.** Findings forced an architecture change — see PRD §6/§12. Audio capture and the GUI now run as a native Windows process; WSL handles STT and Claude CLI orchestration, connected over a local socket. Days 1–10 below are rewritten accordingly (now 11 milestones instead of 10 — the IPC bridge is genuinely new work, not free).

## Day 0 — Environment & Feasibility Check (no app code yet)

This is the "what do we need to cover before we start" step. Nothing here is built by Claude Code — it's you, on your machine, confirming the ground is solid before we spend milestones building on assumptions.

**Checklist:**
1. **Python** — 3.11+ installed and on PATH (`python --version`). Install via python.org if not.
2. **Git** — installed (`git --version`), and a GitHub account ready. Create a new **private** repo (empty, no template) — name it whatever you like; I'll refer to it as `meeting-copilot` unless you tell me otherwise.
3. **Claude Code CLI auth** — run `claude` interactively once and confirm it's using your Claude.ai subscription login, not an API key. (You already confirmed this is the case — this is just re-verifying nothing's changed.)
4. **The actual risk item from PRD §10** — verify the CLI supports headless streaming the way the architecture assumes:
   - Run `claude -p "say hi" --output-format json` once from a terminal and confirm it returns clean JSON (not a chat-UI-only error).
   - Check `claude --help` for `--input-format stream-json` / `--output-format stream-json` and confirm those flags exist in your installed version (`claude --version`).
   - If stream-json isn't available or behaves unexpectedly, tell me — that changes the architecture in PRD §6 (we'd fall back to spawning a fresh `claude -p` call per suggestion, which is simpler but slower; still workable, just a different latency budget).
5. **Audio setup** — identify which Windows output device your meetings actually play through (default speakers, a specific headset, etc.) — you'll pick this device by name in Day 2.
6. **WSL audio loopback feasibility (new — you're developing in WSL)** — this is the one genuinely unproven piece. From a WSL terminal:
   - `pactl list sources` — look for an entry ending in `.monitor` tied to your default sink. That's the loopback/system-audio source WASAPI loopback would normally give you natively.
   - If one exists, try actually recording a few seconds from it while playing audio on Windows (e.g. `parecord --device=<the .monitor source> test.wav` then play it back) and confirm the played-back file actually contains what was playing.
   - Report back exactly what you find: no `.monitor` source at all, a source exists but recording is silent/garbled, or it works cleanly. This single result decides whether Day 2 builds against WSL's PulseAudio bridge, or we split into the hybrid/native-Windows approach discussed earlier.

**Done when:** all six boxes are checked and you've told me the results of #4 and #6 specifically — those two decide how Day 2 and Day 5 get built.

---

## Day 1 — Two-Sided Scaffold ✅ done

Actual outcomes: repo already existed from Day 0 (`adarna-ai`), used as project root. Both hello-worlds built and cross-boundary launch proven (Windows window opened from the WSL shell). Settled on `webrtcvad` over `silero-vad`. Established local (gitignored) `CLAUDE.md` coding conventions — entry functions read like a table of contents, every function gets a docstring as the first line inside its body — and a local (gitignored) `project_notes.md` running dev log. Committed `ea5b6ce`, pushed.

Gotchas worth carrying into later days: the Windows Python install isn't on PATH as `python.exe` from WSL (a Store alias stub intercepts it) — call it by full path. `.venv/Scripts/*.exe` under `windows_app/.venv` lose their execute bit when the venv is created from WSL onto the repo's filesystem — `chmod +x` fixes it, only matters if a venv gets recreated. Compiling `webrtcvad` needed `build-essential`/`python3.10-dev`/`python3.10-venv` installed via sudo from a real terminal (no tty in the agent's shell to reuse cached sudo) — already done, shouldn't recur.

<details><summary>Original Day 1 scope (for reference)</summary>

## Day 1 — Two-Sided Scaffold

- Repo structure inside `adarna-ai/`: `windows_app/` (native Windows Python, its own venv) and `wsl_app/` (WSL Python 3.10.12, its own venv).
- `wsl_app/` deps: `faster-whisper`, `webrtcvad` or `silero-vad`, standard lib `socket`/`asyncio`.
- `windows_app/` deps: `pyaudiowpatch`, `PySide6`. Confirm a native Windows Python install exists (separate from WSL's) — install one via python.org if not; check first rather than assuming.
- Trivial "hello world" for each side: `wsl_app` prints "ready" and exits; `windows_app` opens a blank PySide6 window and closes cleanly.
- Confirm you can launch `windows_app`'s script from the WSL shell via interop (`python.exe windows_app/main.py`, using the Windows-native Python) so day-to-day work doesn't require a second terminal.
- Initial commit + push to `github.com/jjaguilar08/adarna-ai`.

**Done when:** both hello-worlds run, the Windows one launched from your WSL shell, and it's committed/pushed.

</details>

## Day 2 — IPC Bridge ✅ done

Actual outcomes: newline-delimited JSON over TCP on port 8765 (from shared `ipc_config.json`), `wsl_app` as an `asyncio.start_server`, `windows_app`'s connection handling as a `WslConnection` (renamed from `IpcClient` after review — clearer for a non-technical skim) on a background thread with Qt-signal handoff to the UI thread. All three manual tests passed: connect, disconnect-survives, auto-reconnect-without-restart. WSL2 localhost forwarding needed zero extra config — fully transparent.

Gotchas worth carrying forward: stdout is block-buffered when redirected to a file for manual testing (`python -u` or `PYTHONUNBUFFERED=1` fixes it — not a code bug). `pkill -f` sometimes didn't kill the backgrounded test server; `pkill -9 -f` did.

<details><summary>Original Day 2 scope (for reference)</summary>

## Day 2 — IPC Bridge

- `wsl_app` opens a TCP socket listening on `127.0.0.1:<port>`.
- `windows_app` connects to it as a client (relying on WSL2's default localhost forwarding).
- Define the message protocol: JSON control messages (both directions) and raw PCM audio frames (Windows → WSL) — even a stub frame is fine for now, real audio comes in Day 3.
- Round-trip proof: `windows_app` sends a "ping" JSON message, `wsl_app` acks it, `windows_app`'s window shows the connection is live.
- Basic reconnect-on-drop logic (bare minimum for now — full reliability work is Day 9).

**Done when:** you can start `wsl_app`, then `windows_app`, and see a live "connected" indicator in the window that survives you manually killing and restarting either side.

</details>

## Day 3 — Real Audio Capture Over the Bridge ✅ done

Actual outcomes: WASAPI loopback capture via `pyaudiowpatch` (verified API empirically against the installed version, not guessed) at the device's native format (`paFloat32`, 48kHz, 2ch for the default Realtek output). `WslConnection` gained a write-lock around `send_message()` so the ping loop and the new audio thread can share the socket safely. `wsl_app` added `AudioLevelTracker`, logging one peak-amplitude summary per second rather than per chunk. Manual test confirmed real signal rising with playback and decaying to silence after — not a stats bug always reporting "sound." New `numpy` dependency (wsl_app only).

Known rough edge, not yet fixed: `AudioCaptureManager._stop_capture()`'s `thread.join(timeout=2)` runs on the Qt main thread, so a device switch can briefly (~2s) block the UI. Deferred to Day 5, since that's when start/stop stops being automatic and this becomes user-facing.

<details><summary>Original Day 3 scope (for reference)</summary>

## Day 3 — Real Audio Capture Over the Bridge

- WASAPI loopback capture in `windows_app` from the output device you identified in Day 0, using the socket from Day 2 to stream real PCM frames to `wsl_app`.
- `wsl_app` doesn't transcribe yet — just logs stream stats (chunk count, sample rate, and a peak-amplitude check like Day 0's `sox stat`, so you're not just trusting the pipe is nonempty).
- A device picker and "test capture" indicator in the Windows window.

**Done when:** playing audio on your PC produces real, non-silent stats logged on the WSL side — proof the full Windows-capture → socket → WSL pipeline actually carries audio, not just that each half works alone.

</details>

## Day 4 — Local Speech-to-Text ✅ done

Actual outcomes: VAD segmentation (`webrtcvad`, aggressiveness 2, 400ms silence-close, 20s max-segment) feeding `faster-whisper` (`small.en`, CPU, int8), tested against real speech (Windows SAPI TTS through loopback, deliberately alongside real background noise as a stress test — 8 segments over ~2 minutes, strong transcription quality). See PRD §9 for the latency watch item this surfaced (2.4–6.1s per segment, ~4s apparent fixed floor). A readability pass renamed everything audio-processing-jargon-y (`pcm_bytes` → `audio_bytes`, `resample_to_16k_mono_pcm()` → `convert_audio_to_common_format()`, etc.) — this user's plain-naming bar is stricter than docstrings alone, worth a first pass at plain naming before presenting new signal-processing code, not just after.

Carried forward into Day 5 below: a minimum-duration filter (skip transcription for segments under ~300ms — a VAD false positive still cost a full 4.0s transcribe call for nothing) and the Day 3 thread-join-blocks-UI rough edge, since Day 5 makes stop an explicit user action for the first time.

<details><summary>Original Day 4 scope (for reference)</summary>

## Day 4 — Local Speech-to-Text

- Wire `faster-whisper` (`small.en`, CPU, int8) to the PCM stream arriving over the socket.
- VAD-based chunking (segment on speech boundaries, not fixed time windows).
- Transcribed segments logged on the WSL side — no UI wiring back to Windows yet.

**Done when:** speaking or playing speech into the loopback source produces reasonably accurate transcribed text in the WSL-side log within a couple seconds per segment.

</details>

## Day 5 — Live Transcript in the Windows UI ✅ done

Actual outcomes: `session_started`/`session_stopped`/`transcript` message types added. `wsl_app` tracks one `session_segmenter` per active session (force-closes and transcribes whatever's buffered on stop, ignores stray `audio_chunk`s with no session active), and the minimum-duration filter (`MIN_SEGMENT_SECONDS_TO_TRANSCRIBE = 0.3`) closes out the Day 4 follow-up. `windows_app` restructured `WslConnection` so pinging moved to its own thread, freeing the main connection thread to receive server-initiated `transcript` messages; `AudioCaptureManager.stop_capture()` no longer blocks on `thread.join()` — it signals and returns, with a `capture_thread_finished` signal doing cleanup once the thread actually exits (fixes the Day 3 rough edge, and device-switch mid-session no longer blocks either). New Start/Stop Session buttons and a read-only transcript pane. Review caught "VAD" as leftover jargon in comments (reworded to "speech detector," matching the Day 4 plain-naming bar). All three manual tests passed. Committed `a70be50`.

<details><summary>Original Day 5 scope (for reference)</summary>

## Day 5 — Live Transcript in the Windows UI

- Send Day 4's transcript segments back over the socket (WSL → Windows) as JSON control messages.
- Render them in the PySide6 window as a scrolling transcript pane.
- Start/stop session controls in the window (this replaces Day 3's "capture starts automatically on connect" — now explicit, which is also why this is the day to fix the thread-join-blocks-UI rough edge below).
- Fold in two carried-forward items from Day 4: the minimum-duration transcription filter, and making session stop non-blocking on the Qt main thread.

**Done when:** starting a session shows a live-updating transcript in the Windows window itself — no WSL-side log-watching required — and stopping a session doesn't visibly freeze the UI.

</details>

## Day 6 — Claude CLI Integration ✅ done

Actual outcomes: protocol discovered empirically (not guessed) — `stream-json` output requires `-p --verbose`; `--system-prompt` fully replaces the default framing; `--tools ""` rules out permission-prompt hangs entirely rather than just mitigating them; input is one `{"type":"user","message":{...}}` line per prompt, output is a stream of lines ending in a `"result"` line with the full answer. `ClaudeCli` wraps the long-lived subprocess, standalone (not wired to the live pipeline yet). Confirmed: multi-turn context is fully automatic within one process — no session bookkeeping needed on our side — and the second call was meaningfully faster than the first (6.23s → 4.02s), validating the PRD §6 architecture bet. Finding for Day 7/8: default response format is too verbose (multi-option + meta-commentary) for a spoken suggestion.

**Important consequence for Day 7, not in the original scope below:** since the CLI remembers all prior turns automatically, and Day 7 calls `ask()` repeatedly throughout a session, the subprocess's own internal context grows unboundedly over a long meeting — separate from the rolling transcript window we deliberately pass each time. Day 7 adds periodic subprocess recycling to bound this, beyond just "manage the prompt we send."

<details><summary>Original Day 6 scope (for reference)</summary>

## Day 6 — Claude CLI Integration

- On the WSL side: long-lived `claude` subprocess in stream-json mode (confirmed viable in Day 0).
- Hardcoded test: send one fixed prompt + a snippet of transcript, confirm a response comes back and gets logged.
- Most likely milestone to surface surprises (parsing stream-json output, process lifecycle) — budget extra time here if needed.

**Done when:** you can trigger one suggestion generation manually (e.g. a temporary debug call) and see Claude's response logged on the WSL side, sourced from real transcript content.

</details>

## Day 7 — Trigger Logic ✅ done

Actual outcomes: `SuggestionTrigger` (pause timer + hotkey, deliberately named to avoid "debounce" as jargon) and `MeetingSession` (bundles VoiceSegmenter + ClaudeCli + rolling context + trigger, created at `session_started`, torn down at `session_stopped`/disconnect) both landed in `wsl_app`. Subprocess recycling every 10 `ask()` calls confirmed working live. Hotkey: `pynput` chosen over `keyboard` (automated elevation testing was inconclusive — a WSL-interop artifact — so the decision leaned on docs plus a real physical keypress test, which is what actually matters). All 5 manual tests passed, including a real non-elevated hotkey press working without windows_app having focus.

A `/code-review` pass (now a standard step for this user, not just file-by-file walkthroughs) found and fixed 3 real concurrency bugs: `ClaudeCli.stop()` blocking the event loop synchronously, a race between session close and an in-flight hotkey-triggered `ask()`, and — most seriously — no mutual exclusion between the pause-timer and hotkey trigger paths, which could have let two concurrent `ask()` calls read each other's answers off the shared, uncorrelated stdout stream. Fixed with an `asyncio.Lock`, verified with a scripted event-ordering test.

Two real bugs surfaced during testing and deliberately deferred: a WASAPI capture-thread crash after ~10+ min whose own cleanup path suppresses the signal that would tell the UI it died (small fix folded into Day 8 below, given real meetings routinely exceed 10 minutes), and an unhandled `ConnectionResetError` when a segment finishes transcribing after windows_app has already disconnected (Day 9 scope).

<details><summary>Original Day 7 scope (for reference)</summary>

## Day 7 — Trigger Logic

- Silence/pause detection on the WSL side (default ~1.2s after the other side stops speaking) that auto-fires a suggestion request using the recent rolling transcript window.
- Manual global hotkey captured natively on Windows, forwarded to WSL as a control message — works even mid-silence-timer.
- Rolling-context management so the prompt sent to Claude doesn't grow unbounded over a long meeting.

**Done when:** during a live test conversation, pausing naturally triggers a suggestion, and the Windows-side hotkey also works on demand.

</details>

## Day 8 — Suggestions UI, Mode Toggle, Settings ✅ done

Actual outcomes: `print()`-based suggestions replaced with a real Qt pane (`create_suggestions_section()`), backed by a `LatestSuggestion` `QObject` subclass so its signal can be safely queued cross-thread. "Copy Latest Suggestion" button. Mode toggle (Work Meeting / Interview) wired end-to-end — a live test confirmed a real tone difference between the two, not just a prompt-string change with no observable effect. Settings panel (Whisper model size, mode, suggestion pause delay) captured via a new `settings_changed` message sent once right before `session_started`; applies to the new session only, not hot-swapped live. Folded in the Day 7 WASAPI teardown fix, broadened from catching just `OSError` to `Exception` after testing showed other exception types could hit the same suppressed-signal path — confirmed fixed via a targeted forced-failure test, not just code inspection.

A `/code-review` pass (two rounds, six findings total) also caught: a stale call site in `test_claude_cli.py` after `ClaudeCli`'s constructor gained a required `system_prompt` param; bare `.connect()` calls left in `main()` violating the table-of-contents convention (moved into `connect_incoming_messages_to_ui()`); a docstring omission; and a fragile `SUGGESTION_PAUSE_PRESETS_SECONDS.index(DEFAULT_SUGGESTION_PAUSE_SECONDS)` startup-crash risk, fixed by deriving the default from the presets list itself instead of keeping the two in sync by hand.

Real bugs found live and deliberately deferred to Day 9: an unhandled `ConnectionResetError` when a segment finishes transcribing after windows_app has already disconnected (currently just an unretrieved-task-exception, not a crash); a failed Whisper model reload during `session_started` silently drops the connection while windows_app's Start/Stop buttons stay stuck showing the session as active.

Review format also evolved this day: diffs for all changed files shown in-conversation as `git diff` output, confirmed as the preferred format going forward (see the closing instruction on Day 9's prompt below), alongside the `/code-review` pass.

<details><summary>Original Day 8 scope (for reference)</summary>

## Day 8 — Suggestions UI, Mode Toggle, Settings

- Suggestions already flow over the socket as of Day 7 (windows_app just `print()`s them) — replace that with a real pane in the window. Qt widgets handle Unicode natively, so Day 7's em-dash console-encoding cosmetic issue disappears by construction, not as a separate fix.
- Copy-to-clipboard (simplest: one "Copy Latest Suggestion" button rather than per-line click-to-copy — revisit only if that feels limiting once used live).
- Mode toggle (Work Meeting / Interview) in the Windows UI, forwarded to WSL, changes the system prompt sent to Claude.
- Settings panel (Windows side): Whisper model size, mode, and the suggestion pause delay (the Day 7 `SuggestionTrigger` threshold — the more meaningful "silence threshold" to expose than VAD's internal 400ms segment-close value, which stays an unexposed constant). Settings are captured and sent once when Start Session is pressed, applied to the new session only — not hot-swapped into a running one.
- Folded in from Day 7: fix the WASAPI capture-thread crash's suppressed-signal bug (wrap the `finally` block's stream teardown in its own try/except so `capture_thread_finished` always fires).

**Done when:** you can run a full session from the Windows window alone — no logs, no code edits — switch modes and see the suggestion tone change accordingly, and confirm the capture-thread signal now fires even if teardown itself fails.

</details>

## Day 9 — Reliability ✅ done

Actual outcomes: all three items landed — the Day 7 `ConnectionResetError` fix (`send_message()` now catches and swallows `OSError` at the one choke point every outgoing message goes through), the Day 8 `session_start_failed` fix (wraps Whisper reload + `MeetingSession` construction, reports a real reason string, never leaves a half-initialized session), and the original scope (`claude` CLI crash detection + one restart via `_ask_with_restart_on_crash()`, with transcript context surviving because it lives on `MeetingSession` not `ClaudeCli`; `end_session()` consolidating every session-teardown path on the Windows side). A 4-parallel-agent `/code-review` pass found 7 real issues, all fixed — most notably an `attempt_id` race (a slow, now-stale `session_start_failed` from an abandoned Start attempt could tear down a newer, legitimately-running session), independently flagged by 3 of 4 review agents.

Live cross-boundary manual testing (5 of 6 scenarios, driven end-to-end via WSL→Windows interop the same way as every prior day) found a genuine bug the scripted checks had missed: a disconnect-during-transcription race could leave an orphaned `claude` subprocess running forever, because `SuggestionTrigger` only guarded against "is there a pending timer" rather than "has this trigger actually been stopped." Fixed by giving the trigger a permanent `_stopped` flag; re-tested live and confirmed no orphan the second time, plus two new regression checks. One test (disabling/switching the Windows audio device mid-session) turned out to have no scriptable path — Windows audio endpoints don't support the standard PnP disable verb via WMI — so it's flagged as the one item still needing a real by-hand test in Windows Sound settings; everything else in the checklist passed for real, including an orphan-process check after killing both sides. Committed as `920be49` plus a follow-up commit for the orphan-process fix, both pushed.

<details><summary>Original Day 9 scope (for reference)</summary>

## Day 9 — Reliability

Three carried-forward findings folded in alongside the original scope:

- **From Day 7:** the unhandled `ConnectionResetError` when a segment finishes transcribing after windows_app has already disconnected — currently only surfaces as an unretrieved-task-exception, needs to log-and-swallow instead.
- **From Day 8:** a failed Whisper model reload during `session_started` silently drops the connection while windows_app's Start/Stop buttons stay stuck showing the session as active — needs a `session_start_failed` message so windows_app can revert its UI and show a real error.
- **Original Day 9 scope:**
  - Detect and restart a dead/crashed `claude` CLI subprocess (WSL side) without losing the transcript already captured.
  - Detect and handle socket disconnects — one side restarting shouldn't force-kill the other.
  - Handle the Windows audio device disappearing/changing mid-session without a hard crash.
  - Clean shutdown on both sides (no orphaned subprocesses or sockets left open).

**Done when:** you can deliberately kill the WSL process, restart it, and have `windows_app` reconnect without a full restart of both sides; same test killing the `claude` subprocess specifically; a forced Whisper-reload failure shows a clear error in the Windows UI instead of a stuck session; and a segment that finishes transcribing after disconnect no longer produces an unretrieved-task-exception.

</details>

## Day 10 — End-to-End Real Test ✅ done (testing only — fixes carried to Day 11)

Actual outcomes: a real ~17-minute end-to-end session (scripted TTS meeting + interview dialogue over a looping ambient-noise background), evaluated directly against PRD §11 rather than from memory. 3 of 4 criteria met cleanly (live rolling transcript; no disk persistence; no crash/restart across active session time, modulo intentional mode-switch boundaries); the "suggestion within a few seconds" criterion is partially met — pause-triggered suggestions landed in 3-5s, but the hotkey path was never actually exercised (two dedicated test windows, zero `hotkey_triggered` messages reached wsl_app), so it needs a focused re-check rather than being counted as verified. Latency (PRD §9's long-open watch item): real measured pause-to-suggestion was ~5-8s, better than the ~8-12s+ estimate — accepted as the new target (see PRD §9, updated). No tuning changes were needed (silence threshold, Whisper model size, both prompts all performed well as-is).

Found one real, higher-priority bug: `AudioCaptureManager._capture_loop`'s blocking `stream.read()` can hang forever on a flaky device (reproduced live on a Bluetooth loopback device), and since Python can't interrupt a blocked native call, `stop_event.set()` does nothing — `capture_thread_finished` never fires, and both Stop Session and device-switching become silent permanent no-ops until the whole process is killed. This is a real instance of the "requires a restart" failure PRD §11 wants ruled out; it just doesn't show up on a normal wired device. Not fixed this session — needs an architecture change to the capture loop (a read timeout, or pyaudio's non-blocking callback API), not a one-line patch. Carried to Day 11 below, ahead of sign-off.

Also confirmed: the audio-device-disable manual test deferred from Day 9 now passes for real (disabling — not just switching default away from — the active device produces a clean `session_stopped`, not a crash).

<details><summary>Original Day 10 scope (for reference)</summary>

## Day 10 — End-to-End Real Test

- Run an actual 15–30 minute mock meeting or interview session start to finish, both processes running as they would for real.
- Tune silence threshold, Whisper model size, and prompt wording based on real output quality.
- Folded in from Day 9: manually disable or switch the Windows audio output device mid-session, by hand in Windows Sound settings (this couldn't be automated — Windows audio endpoints don't support the standard PnP disable verb via WMI), and confirm the session ends cleanly per Day 8/9's WASAPI teardown handling rather than crashing.
- Also revisit the still-open PRD §9 latency question here (STT ~2.4–6.1s + Claude CLI ~4–6s per call ⇒ realistic end-to-end likely 8–12s+ vs. the original 2–4s target) — decide whether to loosen the target, try a smaller Whisper model, tighten the prompt further, or accept the real number, based on how it actually feels in a real session.
- Fix whatever breaks.

**Done when:** PRD §11's MVP success criteria are all met in one uninterrupted real session.

</details>

## Day 11 — Fixes from Day 10, Then MVP Sign-off ✅ done — **MVP (Phase 1) complete**

Actual outcomes: both Day 10 carry-forwards genuinely closed, and PRD §11 re-run end-to-end with all four criteria met in one real ~15-minute session, hotkey included (4/4 real physical presses, 4/4 suggestions in 2-3s each).

Bug 1 (capture-thread hang): verified empirically that `pyaudiowpatch`'s blocking read has no timeout option, so the fix went with the non-blocking callback API instead — `_capture_loop` now waits on a bounded `queue.Queue` (`MAX_QUEUED_AUDIO_BLOCKS = 50`) fed by a PortAudio-driven callback, so `stop_event` is checked on a timeout rather than after a blocking call that might never return. Two `/code-review` passes (before and after live testing, since the diff changed in between) caught and fixed: an unbounded queue risk, `stream.open()` itself not being wrapped in try/except, an overflow policy that was dropping the *newest* audio instead of the oldest, and a hotkey-dispatch thread-per-press design that could pile up unboundedly under a genuine hang (replaced with one persistent worker thread + a `maxsize=1` queue). A self-caught near-miss worth remembering: an added "no callback for N seconds = failure" heuristic looked reasonable but false-positived hard against real hardware (a real USB headset's loopback stops delivering callbacks during any silence gap between discrete TTS renders, not just before the first sound) — removed entirely rather than chase thresholds, since it would have auto-killed real sessions on natural pauses. `stream.is_active()` alone is the only automatic failure signal now — a deliberate trade-off, not an oversight.

Bug 2 (hotkey): no actual regression found — Day 10's "zero hits" traced to the user simply not having pressed it in either test window, confirmed by asking directly. Hardened anyway: hotkey dispatch now runs on its own short-lived thread instead of pynput's listener thread, so it can never be delayed by an unrelated stalled socket write during the known CPU-contention windows (PRD §9).

**Known real gap going forward, not a blocker:** all live testing (Days 4-11) has used TTS/synthetic playback through a loopback device, never a real Zoom/Teams call. Tonight's finding that a real USB headset's loopback goes silent during *any* gap (not just before the first sound) is specific to how SAPI's `Speak()` opens/closes a render session per sentence — a real meeting app very likely holds a continuously-open stream instead, which should behave differently, but this has never been directly confirmed. Worth a first real-call test before relying on this for anything high-stakes (a real interview).

<details><summary>Original Day 11 scope (for reference)</summary>

## Day 11 — Fixes from Day 10, Then MVP Sign-off

Not a generic slack day — Day 10 found specific, real gaps that need closing before sign-off means anything:

1. **Fix the capture-thread-hang bug** (Day 10's real finding): rework `_capture_loop` so a stuck `stream.read()` can't permanently wedge Stop Session and device-switching — a read timeout on the stream, or moving to pyaudio's non-blocking callback API, are the two shapes worth trying. Whichever you pick, re-test against the same flaky/Bluetooth device that surfaced it originally, not just the normal wired one.
2. **Re-verify the hotkey path**: Day 10's two attempts both landed zero `hotkey_triggered` messages. Confirm whether this is a real regression (something since Day 7 broke it) or just bad timing in a scripted test, and fix or explain accordingly.
3. **Re-run PRD §11 end-to-end**, now with the hotkey genuinely exercised, and confirm all four criteria are met in one pass — not three-of-four with a caveat.
4. Once 1-3 are genuinely done: confirm the MVP is usable end-to-end, and we'll decide together whether Phase 2 (overlay) starts next or Phase 3 (summary) jumps the queue.

**Done when:** the capture-thread hang no longer reproduces on the device that triggered it, the hotkey is confirmed working (or its bug identified and fixed), and PRD §11's criteria are all met — including the hotkey — in one real session.

</details>

---

**MVP status: Phase 1 complete as of Day 11 (2026-09-03).** All 11 milestones done, PRD §11's success criteria met in a real end-to-end session including the hotkey path. One open follow-up worth doing early in whichever phase comes next: a first real Zoom/Teams call test, since every session so far has used synthetic TTS playback rather than a real meeting app's continuously-open audio stream.

**Next up: Phase 1.5 (suggestion & context refinements), then Phase 2 (overlay).** Based on real usage, three changes are needed to core suggestion logic before building the overlay — see PRD §8, Phase 1.5. Doing these first means the overlay gets built against the improved suggestion behavior rather than needing rework afterward.

## Day 12 — Suggestion & Context Refinements (Phase 1.5) ✅ done

Actual outcomes: all three changes landed. Context notes: `context_notes` field added to `settings_changed`/`default_settings()`/`settings_from_message()`, folded into the system prompt only when non-empty via a new `build_system_prompt(mode, context_notes)`. Verified live two ways — direct CLI comparison with/without notes (a JD's "saga pattern" only surfaced with notes), and a full end-to-end session with a real CV/JD pasted into the actual UI text box, producing suggestions that referenced specifics ("Celery task deduplication," "PCI-DSS scope") straight from the notes. Output style: switched to terms/concepts (`RESPOND_WITH_TERMS_INSTRUCTION`), tuned after an initial too-verbose pass, verified against 8 real questions across both modes. Auto-suggest toggle: `SuggestionTrigger` gained `auto_suggest_enabled` + live `set_auto_suggest_enabled()`, gating only the pause-timer path; new `auto_suggest_changed` message type; a new checkbox + "Generate Suggestion Now" button in the window, the button sharing the existing `hotkey_triggered` plumbing rather than adding a new message type. A `/code-review` pass caught a real bug manual testing hadn't hit — unbounded `context_notes` text could exceed asyncio's default 64KiB per-line read limit and crash the connection; fixed by raising `asyncio.start_server()`'s `limit` to 10MB.

Incidental finding during live testing: a real Microsoft Teams meeting happened to be running on the test machine and got captured via loopback, producing a real (if accidental) partial confirmation that real meeting-app audio behaves fine through this pipeline — encouraging, but still not a deliberate controlled test, so the Day 11 open item about a dedicated real-call test stays open. Also surfaced a new, undecided open item: real Filipino/Taglish speech in that same audio produced garbled transcript text, traced to the English-only `small.en`/`base.en` Whisper models in use — a multilingual model would fix it but trades off English-only accuracy, latency (bigger models), and reliable language auto-detection on code-switched speech. Flagged for a future decision, not yet actioned.

<details><summary>Original Day 12 scope (for reference)</summary>

## Day 12 — Suggestion & Context Refinements (Phase 1.5)

- **Pre-session context notes:** a multi-line text box in the Windows settings panel — paste a CV/JD for interviews, a PRD/agenda for meetings. Sent once as part of `settings_changed`, included in that session's prompt/context only (not hot-swapped mid-session).
- **Output style change, both modes:** suggestions switch from a ready-to-read spoken script to terms/concepts/key points to build an answer from (e.g. "OOP principles" → "Encapsulation, Inheritance, Polymorphism, Abstraction" rather than a full sentence). System-prompt change, not a new message type — verify with real questions in both modes, not just a spec read-through.
- **Auto-suggest on/off, toggleable mid-session:** on = today's behavior (pause-trigger + hotkey/button both fire). Off = pause-triggered generation suppressed entirely; only an explicit manual trigger (hotkey, plus a matching in-window button) generates, from the most recent transcript context. New control in the window to flip it live during a running session — needs a new message type (or reuse of an existing one — your call) so wsl_app knows the current state.

**Done when:** you can paste real context notes (a real CV/JD, a real PRD excerpt) before a session and see the suggestions actually reference them; a real technical question in both modes produces terms/concepts, not a spoken script; and you can flip auto-suggest off mid-session, confirm pause-triggered suggestions stop while the hotkey/button still works, then flip it back on and confirm pause-triggering resumes.

</details>

## Day 13 — Output & Display Adjustments (Phase 1.5b) ✅ done

Actual outcomes: both changes landed and verified live, not just against the diff. Output style: `RESPOND_WITH_SCRIPT_INSTRUCTION` replaced Day 12's terms instruction, verified against the real `claude` CLI with 6 varied real questions across both modes — all genuinely multi-sentence (3-4 sentences), substantive, sayable, no meta-commentary or markdown, no terms-list regression. Suggestion pane: `setPlainText` replaces `appendPlainText`, so a new suggestion now visibly replaces the previous one instead of appending.

Two `/code-review` passes, since live testing between them surfaced a real edge case: switching to `setPlainText` meant an empty `claude` CLI result (never actually observed, but not structurally ruled out) would now wipe the last good suggestion off the pane instead of harmlessly appending a blank line. Fixed by widening `_generate_and_send_suggestion()`'s guard to skip sending when the result is empty/whitespace-only, not just `None`. Committed as `aef886a` then a follow-up `9b10a12` for the edge-case fix. One judgment call left as-is: `LatestSuggestion` is now redundant since the pane always mirrors it 1:1 — not removed since it wasn't part of the ask, flagged for later cleanup.

<details><summary>Original Day 13 scope (for reference)</summary>

## Day 13 — Output & Display Adjustments (Phase 1.5b)

Small, targeted follow-up after one real session's use of Day 12's changes — not a new phase, just two adjustments before moving on to the overlay:

- **Revert the output style back to script-style, both modes**, but more detailed than Day 6's original bare single line: 2-4 sentences with real substance/reasoning, still short enough to skim and say in the moment. Replaces Day 12's terms/concepts instruction.
- **Suggestion pane auto-clears on each generation**: showing only the latest suggestion, replacing the previous one automatically rather than appending to a growing list — the running-list behavior from Phase 1 was getting crowded in real use.

**Done when:** a handful of real questions in both modes produce multi-sentence, sayable responses (not terms/concepts, not a single bare line); and generating a new suggestion visibly replaces the previous one in the pane rather than adding to a list.

</details>

## Day 14 — Overlay Core (Phase 2, part 1) ✅ done — one item still open

Actual outcomes: a frameless, always-on-top `QWidget` (`create_overlay_window()`) showing just the latest suggestion, running alongside the main window. `pynput`'s single-listener `GlobalHotKeys` now registers both the existing `Ctrl+Alt+Space` and a new `Ctrl+Alt+O` (overlay show/hide) — confirmed by reading pynput's own source that each combination tracks independent key-down state, so the two genuinely can't interfere with each other. Suggestion wiring fans out to both the main pane and the overlay from the same incoming `suggestion` message, no protocol change. Verified live: overlay renders above a real other window (not just the desktop), suggestion updates confirmed identical on both panes via a scripted double-emit check, and the full real pipeline (real TTS speech → real transcript → real suggestion, both trigger paths) produced correct output. A `/code-review` pass caught a real bug live testing had missed by construction: `OverlayToggle`'s signal was connected to a lambda, which has no owning `QObject` for Qt to key thread-affinity off of — a real hotkey press (cross-thread emit) would have called `setVisible()` unsafely off the GUI thread, even though every *test* emission (same-thread) happened to work fine either way. Fixed with a proper bound method, same pattern as `LatestSuggestion`.

**Still open, not yet confirmed:** a real physical keypress test of `Ctrl+Alt+O`, and both hotkeys pressed back-to-back in one session — synthetic key injection can't reach pynput's hook by design (it explicitly ignores OS-flagged injected events), so this needs the user's own hands, same as every prior hotkey milestone. Do this at the first opportunity, e.g. folded into Day 17's manual testing below, since that day already needs a real running session anyway.

<details><summary>Original Day 14 scope (for reference)</summary>

## Day 14 — Overlay Core (Phase 2, part 1)

- A second top-level window (`QWidget` with `Qt.WindowStaysOnTopHint`, frameless) showing just the latest suggestion — kept deliberately minimal, per the decision to show suggestion-only rather than transcript/controls too.
- Runs alongside the main window, not in place of it — both stay open independently; typically the main window handles session start/settings and the overlay is what you actually watch during the call.
- A global show/hide hotkey (separate combo from the existing `Ctrl+Alt+Space` suggestion-trigger hotkey — pick something that doesn't collide, e.g. `Ctrl+Alt+O`), toggling the overlay's visibility without affecting the main window or the running session.
- The overlay subscribes to the same `suggestion_received` signal the main window's pane already uses — no protocol change, both windows just render the same incoming `suggestion` messages.

**Done when:** during a real running session, the overlay shows the latest suggestion, stays on top of other windows (including a real browser/Zoom-like window, not just the desktop), and the show/hide hotkey toggles it live without disturbing the main window or the session.

</details>

---

**Inserted here, ahead of the remaining Phase 2 polish/persistence work (now Days 18-19 below): Phase 2.5, based on real usage after the overlay landed.** See PRD §8, Phase 2.5.

## Day 15 — Suggestion Format: Script + Bullets ✅ done

Actual outcomes: `RESPOND_WITH_LEAD_AND_BULLETS_INSTRUCTION` replaced Day 13's script instruction, spelling out the exact literal output shape (a lead, a blank line, then `"- "`-prefixed bullets) rather than just asking for "bullets" loosely — verified against the real `claude` CLI with 6 varied questions across both modes via `repr()`, confirming genuine newlines and dash markers, not prose describing bullets. Both panes render it correctly with zero widget code changes needed, since `suggestion_received` already piped straight through to `setPlainText` with no transformation and `QPlainTextEdit` renders literal `\n` as real line breaks — confirmed live in a full real session (main pane) and via a standalone script driving the real overlay at its actual 420×160 size (bullets that didn't fit scrolled correctly rather than clipping).

Three `/code-review` passes: pass 1 found the instruction's own "1./2." framing risked the model echoing numbering into the lead line (fixed, reworded to "First.../Then..."), plus flagged four pre-existing Day 14 overlay issues (all fixed at the user's request: a shared `create_readonly_text_pane()` factored out of three duplicated widget-creation blocks, the overlay window given `WA_QuitOnClose=False` so closing the main window now actually quits the app instead of leaving it running invisibly, a trimmed docstring). Pass 2 caught two instances of the fixes themselves overreaching — a factored-out `create_overlay_toggle()` had been inlined away, violating CLAUDE.md's own stated seam-before-you-need-it rule, and a docstring trim had deleted the one place documenting Day 14's actual "why a bound method, not a lambda" lesson (CLAUDE.md itself is gitignored and never reaches the repo, so the code's own docstring is the only surviving record) — both restored. Pass 3 clean. All fixes re-verified live, not just re-reviewed.

<details><summary>Original Day 15 scope (for reference)</summary>

## Day 15 — Suggestion Format: Script + Bullets

- Replace Day 13's plain multi-sentence script style with a short 1-2 sentence lead (the framing/"script" part) followed by 3-5 bullet points of supporting details/angles — closer to the real ParakeetAI's presentation, meant to be picked from rather than read verbatim. Applies to both Work Meeting and Interview modes.
- Lower-risk, contained change — a system-prompt/formatting rewrite, not an architecture change. Verify live with real varied questions across both modes, same as every prior prompt-wording change, and confirm the windows_app suggestion pane (and the overlay) render the bulleted structure readably (not just as one undifferentiated blob of text).

**Done when:** real questions in both modes produce a short lead line plus 3-5 distinct, readable bullets in both the main window's pane and the overlay.

</details>

## Day 16 — Real-Time Transcript: Feasibility Spike ✅ done

Actual outcomes: a real LocalAgreement-2 streaming implementation built and benchmarked against `faster-whisper` (not a docs read-through), using three SAPI-TTS test clips (clean English, fast English, Taglish/code-switched). Key findings: `faster-whisper` has no incremental/cached decoding — every re-transcribe call reprocesses the whole buffer from scratch — so a true ~1s word-level cadence isn't viable; `small.en` (today's default) structurally can't sustain faster than ~2.5s/call, `base.en` is the practical floor at a ~1.5-2s cadence, `tiny.en` hits 1s but visibly garbles content words even on clean audio. Found a real bug in the streaming technique itself, not just this implementation: with the default 15s buffer-trim threshold, a session that never grows past 15s re-includes the whole transcript in every new hypothesis, and the 5-word dedup window can't handle that much overlap — `small.en`/beam=5 committed duplicated sentences as a result. Revision rate (how often a tentative word changes before locking in) ran 68-95% across configurations — a real, honest ceiling: this will visibly flicker in a way Google Meet's purpose-built streaming model doesn't, not a bug to chase away. Even so, `base.en`'s ~2.3s first-glimpse / ~4.0s commit latency already beats the current batch approach's felt latency (nothing visible until a full segment closes and transcribes, 2.4-6.1s per Day 4). On the multilingual question: multilingual `small` matched `small.en` exactly on clean English at no latency cost, but didn't clearly fix the Taglish test clip either — only `medium` showed real improvement, at ~3.5x the latency, ruled out for the live path. Recommendation: build the streaming pipeline on `base.en` at a ~1.5-2s cadence, fix the buffer-trim bug first, and leave the multilingual/Taglish question open — the synthetic Taglish test (an English TTS voice reading Taglish text, not real Filipino-accented speech) is flagged as a weak proxy, not a real answer either way.

<details><summary>Original Day 16 scope (for reference)</summary>

## Day 16 — Real-Time Transcript: Feasibility Spike

No app code changes yet — this is a research/feasibility day, same shape as Day 0. The goal is to answer the open technical question before committing to an implementation, not to guess.

- Investigate incremental/streaming transcription approaches that can run locally, CPU-only (no cloud STT — stays consistent with PRD §2's "no per-token API billing, runs entirely locally" goal). Leading candidate to evaluate first: a `faster-whisper`-backed incremental-decoding approach with a stabilization policy for partial results (e.g. the local-agreement technique used by projects like `whisper_streaming`) — keeps the existing Whisper dependency and its accuracy characteristics, rather than swapping to a different, less accurate engine.
- Bundle in the multilingual/code-switching question here too (Day 12's open item): test whether a multilingual (non-`.en`) Whisper checkpoint meaningfully helps with real Filipino/Taglish code-switched audio, and what it costs in latency/English accuracy given CPU-only constraints.
- Measure real, empirical numbers on this actual hardware: how quickly can partial words realistically appear, how often do they get revised, and what's the practical gap against a "Google Meet" level of real-time feel — be honest about what's achievable locally vs. what a cloud captioning service can do.
- Report back with a recommendation (which streaming approach, which model) and the real numbers behind it, before writing any implementation code.

**Done when:** you have empirical latency/accuracy numbers for at least one real streaming-transcription approach and a model-choice recommendation (including the multilingual question), tested against real speech on this machine — not a decision made from documentation alone.

</details>

## Day 17 — Real-Time Transcript: Implementation ✅ done

Actual outcomes: the full streaming pipeline landed — `wsl_app/streaming_transcriber.py` (new module, promoted from Day 16's spike code), the `is_final` protocol flag on `transcript`, `windows_app`'s new `TranscriptDisplay`, and the Whisper-model-size setting removed entirely (the live path now always uses `base.en`, per Day 16's finding that nothing else is fast enough). Rigorously verified against real audio offline first: re-running the new streaming engine against all three Day 16 test clips found and fixed two *additional* real bugs beyond the one Day 16 already flagged (content silently vanishing on long uninterrupted sentences once the buffer grew unbounded; a trim-point calculation that could regress backward and re-include already-cut audio) — both found by actually running the code against real audio, not by inspection. A 4-pass `/code-review` (8 findings, 7 fixed) caught a genuine cross-thread race on the audio buffer between the event-loop and worker threads, a missing exception guard that would have silently killed transcription for the rest of a session on one transient error, and a doubled-space bug present in every one of the night's own verification output that had been misread as a formatting quirk rather than flagged. Committed as `d27ef5a`, pushed.

Live cross-boundary test run afterward on the real Windows machine, user-confirmed directly: no issues, partial captions updating live in the real transcript pane, overlay/main window handling the partial/final distinction sensibly. Closes out the "still needs a real machine" gap from the first report.

<details><summary>Original Day 17 scope (for reference)</summary>

## Day 17 — Real-Time Transcript: Implementation

Scoped per Day 16's recommendation:

- Fix the buffer-trim bug found in the spike (shorten the trim threshold and/or move to sentence-boundary trimming, plus a guard against locking in an early hallucinated repeat as committed) before wiring this into the live pipeline.
- Replace the current VAD-close-then-transcribe-whole-segment flow with LocalAgreement-2 streaming on `base.en`, targeting a ~1.5-2s partial-update cadence — the honest ceiling from Day 16, not a starting point to optimize down from.
- Protocol change: distinguish tentative/partial transcript text from finalized/committed text (extend `transcript` with a flag, or a new message type — your call), so windows_app can update the transcript pane in place for partials and lock text in once committed, rather than only appending on segment close.
- **Model checkpoint — overriding one ambiguous line in Day 16's own recommendation**: recommendation 1 says use `base.en` for the live path; recommendation 3 separately suggested switching the app's default checkpoint to multilingual `small`, but that's in tension with using `base.en` live, and Finding 5 itself concluded multilingual `small` didn't clearly fix the Taglish test anyway. Given that, stay on `base.en` only for the live path for now — don't adopt multilingual `small` yet, since Day 16's Taglish evidence is flagged as a weak synthetic proxy, not a real answer. Revisit if a real Taglish recording later shows a genuine case for switching.
- Settings UI: `WHISPER_MODEL_SIZES` currently offers `small.en`/`base.en` as a session setting — since the live path now structurally requires `base.en` (per Day 16 Finding 1), decide how to handle this sensibly (fixing it to `base.en` only, with the setting simplified or removed, is probably cleanest, but use your judgment) and tell me what you did and why.

<details><summary>Original Day 17 scope (for reference)</summary>

## Day 17 — Real-Time Transcript: Implementation

Scoped per Day 16's recommendation:

- Fix the buffer-trim bug found in the spike (shorten the trim threshold and/or move to sentence-boundary trimming, plus a guard against locking in an early hallucinated repeat as committed) before wiring this into the live pipeline.
- Replace the current VAD-close-then-transcribe-whole-segment flow with LocalAgreement-2 streaming on `base.en`, targeting a ~1.5-2s partial-update cadence — the honest ceiling from Day 16, not a starting point to optimize down from.
- Protocol change: distinguish tentative/partial transcript text from finalized/committed text (extend `transcript` with a flag, or a new message type — your call), so windows_app can update the transcript pane in place for partials and lock text in once committed, rather than only appending on segment close.
- **Model checkpoint — overriding one ambiguous line in Day 16's own recommendation**: recommendation 1 says use `base.en` for the live path; recommendation 3 separately suggested switching the app's default checkpoint to multilingual `small`, but that's in tension with using `base.en` live, and Finding 5 itself concluded multilingual `small` didn't clearly fix the Taglish test anyway. Given that, stay on `base.en` only for the live path for now — don't adopt multilingual `small` yet, since Day 16's Taglish evidence is flagged as a weak synthetic proxy, not a real answer. Revisit if a real Taglish recording later shows a genuine case for switching.
- Settings UI: `WHISPER_MODEL_SIZES` currently offers `small.en`/`base.en` as a session setting — since the live path now structurally requires `base.en` (per Day 16 Finding 1), decide how to handle this sensibly (fixing it to `base.en` only, with the setting simplified or removed, is probably cleanest, but use your judgment) and tell me what you did and why.

**Done when:** real speech at a normal-to-fast pace, in a real live session, produces visibly incremental captions (partial text appearing and updating within ~1.5-2s, not one big chunk after a pause), a long-running session (past 15s of continuous speech) doesn't reproduce the buffer-trim duplication bug, and the settings/model-checkpoint question above is resolved and explained, not left ambiguous.

</details>

## Day 18 — Overlay Polish + Visual Redesign (Phase 2, part 2)

Scope grew from the original polish list after the user shared a real ParakeetAI screenshot as a visual reference — see PRD §8 Phase 2 for the full note on what's in/out of scope.

- **Visual redesign**: restyle the overlay to match the reference screenshot's look — dark, semi-transparent, rounded panel, clean glanceable typography. Exact colors/spacing are your call, aim for the same feel rather than a pixel-exact clone.
- **Question text above the answer**: extend the `suggestion` protocol message with a new field carrying the transcript excerpt/context that prompted the suggestion (`MeetingSession` already tracks this rolling context for building the Claude prompt — reuse it rather than inventing a new tracking mechanism), and render it above the bulleted answer on the overlay, matching the reference screenshot's Question/Answer layout.
- **Explicitly out of scope for Day 18**, even though the reference screenshot shows them: a session timer, a manual "Clear" button, and the screenshot-capture/chat toolbar buttons — the latter two are genuinely new capabilities, not overlay polish, and are deliberately deferred as possible future features rather than folded in here.
- Draggable (click-and-drag to reposition) and resizable overlay.
- Adjustable opacity (a setting or a quick keyboard/UI control — your call on the exact mechanism).
- Optional click-through mode (mouse events pass through to whatever's underneath) — toggleable, since click-through and draggable are mutually exclusive while enabled.
- Fold in the still-open Day 14 item if it hasn't been confirmed yet by this point: a real physical keypress test of the overlay hotkey and both hotkeys back-to-back.

**Done when:** the overlay visually matches the reference style and shows both the question and the answer, can be dragged and resized, opacity is adjustable and visibly takes effect, click-through mode can be toggled on/off with mouse events passing through when it's on, and the Day 14 physical-hotkey test is confirmed.

**First attempt landed 2026-09-04, but real testing found it doesn't meet the bar.** Despite three `/code-review` rounds specifically targeting drag reliability (including a documented finding that `QTest`'s synthetic mouse events bypass Qt's real hit-testing, so earlier "verified" drag tests were actually invalid), live testing found the overlay was not movable at all and not scrollable — a real regression the review process's synthetic testing didn't catch. Continuing under this same Day 18 rather than a new day, since the overlay isn't functionally complete without working drag/scroll. Also folding in new requirements from a second reference example the user shared: a black semi-transparent box with bold white text, 💬/⭐️ section markers, and — Interview mode only — a longer, formal, narrative-plus-bullets suggestion format (a fuller lead paragraph, bulleted specifics, a closing line), replacing Day 15's shorter casual-script style for that mode specifically. See PRD §8 Phase 2 for the updated note.

**Continued 2026-09-04/05 — transcription pipeline reverted from real-time streaming back to segment-based.** Fixing a user-reported Whisper hallucination ("Thank you for watching" appended on session-stop) led through several rounds of live-tested fixes to the Day 17 streaming pipeline: a `no_speech_prob`/`avg_logprob` segment filter, a hard silence-amplitude gate, and — after the user asked to revisit `small.en` (this app's pre-Day-17 default, remembered as "almost always accurate" but pause-gated) — a hybrid attempt at live `base.en` captions plus a background `small.en` "polish" re-decode of each committed segment. Live testing repeatedly found the same root cause under different symptoms: decoding a partial/boundary-aligned buffer slice (rather than a complete, pause-bounded utterance) is inherently fragile, regardless of model size — a short polish slice was *worse* than the live decode without real context; giving it more context fixed that but then a too-early segment produced a confidently wrong same-length substitution; word-timestamp misalignment between the two independently-decoded models then duplicated a word at a segment boundary. Rather than keep patching that one failure class from different angles, reverted the whole live pipeline back to this app's original architecture (`VoiceSegmenter`, pause-then-transcribe-the-whole-utterance, single `small.en` model) — real testing confirmed materially better accuracy and no more hallucination artifacts. Kept the hallucination-defense filters found along the way (now applied to the segment-based flow via a trimmed `wsl_app/streaming_transcriber.py`, ~815 → ~164 lines). `MAX_SEGMENT_SECONDS` (the force-close safety net for one continuous run-on utterance) raised from 20s to 60s after live testing showed real interview answers routinely run past 20s uninterrupted, which forced mid-word segment splits and measurably hurt accuracy on the orphaned half. Also added: suggestion text now streams to windows_app as the `claude` CLI generates it (`--include-partial-messages`), rather than windows_app waiting 7-12s to see anything — real generation time is unchanged, but the perceived wait is much shorter. Full account, including the specific live-tested failure cases, in `project_notes.md` (gitignored, local only).

## Day 19 — Opt-in Session Persistence (Phase 2, part 3)

- Explicit, off-by-default setting to save transcript + suggestions from a session to a local file.
- Off unless turned on, per PRD §5/§9's privacy stance — no behavior change for anyone who leaves it alone.

**Done when:** with persistence off (default), nothing is written to disk, exactly as today; with it explicitly turned on, a real session's transcript and suggestions are saved locally and can be found/opened afterward.
