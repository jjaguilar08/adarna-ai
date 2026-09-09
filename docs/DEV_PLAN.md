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

## Day 18 — Overlay Polish + Visual Redesign (Phase 2, part 2) ✅ done

Scope grew from the original polish list after the user shared a real ParakeetAI screenshot as a visual reference — see PRD §8 Phase 2 for the full note on what's in/out of scope.

- **Visual redesign**: restyle the overlay to match the reference screenshot's look — dark, semi-transparent, rounded panel, clean glanceable typography. Exact colors/spacing are your call, aim for the same feel rather than a pixel-exact clone.
- **Question text above the answer**: extend the `suggestion` protocol message with a new field carrying the transcript excerpt/context that prompted the suggestion (`MeetingSession` already tracks this rolling context for building the Claude prompt — reuse it rather than inventing a new tracking mechanism), and render it above the bulleted answer on the overlay, matching the reference screenshot's Question/Answer layout.
- **Explicitly out of scope for Day 18**, even though the reference screenshot shows them: a session timer, a manual "Clear" button, and the screenshot-capture/chat toolbar buttons — the latter two are genuinely new capabilities, not overlay polish, and are deliberately deferred as possible future features rather than folded in here.
- Draggable (click-and-drag to reposition) and resizable overlay.
- Adjustable opacity (a setting or a quick keyboard/UI control — your call on the exact mechanism).
- Optional click-through mode (mouse events pass through to whatever's underneath) — toggleable, since click-through and draggable are mutually exclusive while enabled.
- Fold in the still-open Day 14 item if it hasn't been confirmed yet by this point: a real physical keypress test of the overlay hotkey and both hotkeys back-to-back.

**Done when:** the overlay visually matches the reference style and shows both the question and the answer, can be dragged and resized, opacity is adjustable and visibly takes effect, click-through mode can be toggled on/off with mouse events passing through when it's on, and the Day 14 physical-hotkey test is confirmed.

**First attempt landed 2026-09-04, but real testing found it doesn't meet the bar.** Despite three `/code-review` rounds specifically targeting drag reliability (including a documented finding that `QTest`'s synthetic mouse events bypass Qt's real hit-testing, so earlier "verified" drag tests were actually invalid), live testing found the overlay was not movable at all and not scrollable — a real regression the review process's synthetic testing didn't catch. Continuing under this same Day 18 rather than a new day, since the overlay isn't functionally complete without working drag/scroll. Also folding in new requirements from a second reference example the user shared: a black semi-transparent box with bold white text, 💬/⭐️ section markers, and — Interview mode only — a longer, formal, narrative-plus-bullets suggestion format (a fuller lead paragraph, bulleted specifics, a closing line), replacing Day 15's shorter casual-script style for that mode specifically. See PRD §8 Phase 2 for the updated note. From this point, `/code-review` is capped to one pass per prompt (the user's explicit instruction, to manage token usage) rather than the multi-round practice used through Day 17.

**Day 18 continued, same date (2026-09-04) — drag/scroll root-caused and fixed, plus a much larger scope than originally planned.** Root cause: a real, documented Qt engine bug (QTBUG-8431) — `WA_TransparentForMouseEvents` cascaded across nested widgets is unreliable against real OS-level clicks despite passing synthetic checks, which is exactly why three prior review rounds gave false confidence. Fixed by avoiding the buggy mechanism entirely: a dedicated `_DragHandle` widget now owns dragging, a plain unmodified `QScrollArea` handles scrolling. Visual restyle (black semi-transparent, bold white text, 💬/⭐️ markers) and the two-shape Interview-mode format rewrite (see PRD §8 Phase 2/2.5) both landed. **Still open: the drag/scroll fix itself hasn't been confirmed by an actual physical click on the real Windows machine** — that session had no Windows access, same limitation as Day 14's still-open hotkey item. This needs the user's own hands before Day 18 can be marked done.

Also landed in the same continued session, all live-tested: two independent Whisper hallucination fixes (a `no_speech_prob > 0.6` filter for stale-silence fabrication on session stop, and a separately-calibrated `avg_logprob < -2.0` filter for a distinct "not enough signal, model free-associates" case on truncated audio tails); a CPU-waste fix for `process_tick()` needlessly re-transcribing unchanged audio for up to 6s after speech stopped; real token-level suggestion streaming via the `claude` CLI's `--include-partial-messages` flag; and — the most significant change — a full architectural reversal of Phase 2.5's real-time streaming transcript back to the original VAD-pause-then-transcribe-whole-segment approach on `small.en` (not `base.en`), after the streaming architecture's "polish hybrid" attempt hit a third real bug (cross-model word-timestamp misalignment causing duplicated words) sharing the same root cause as two earlier patched bugs: decoding audio slices/boundaries loses context that decoding a whole utterance keeps. The user's own memory of the pre-streaming architecture's better accuracy directly drove this call. `MAX_SEGMENT_SECONDS` raised from 20.0 to 60.0 (real interview answers often run 20-55s uninterrupted). Full detail in PRD §8 Phase 2.5.

**Confirmed for real, 2026-09-06.** Physical drag/scroll/hotkey test on the real Windows machine passed — recorded in `project_notes.md`. Day 18 is genuinely closed now, not just code-complete.

## Day 18.5 — STT Accuracy: Model Size Re-Evaluation (research/spike, no live pipeline changes yet) ✅ done — recommendation: keep `small.en`

Added 2026-09-05. Jon flagged that plain English transcription accuracy on `small.en` still isn't where he wants it, separate from the slice/boundary-decoding bug Day 18 already fixed and separate from the still-open Taglish question (Day 12/16). Decided to stay fully local rather than a cloud STT API (keeps PRD §2's no-billing goal) and investigate a bigger local checkpoint instead — see PRD §8 Phase 2.5 for the reasoning. Cloud STT (e.g. OpenAI's Whisper API) is explicitly parked as a Phase 4 nice-to-have, only worth revisiting later if local model upsizing doesn't close the accuracy gap — not in scope for this day or anything scheduled after it.

Same shape as Day 0/16 — a research day, not an implementation day, since the right call depends on real measured numbers, not assumption.

- Re-run a benchmark like Day 16's, but under the **current** segment-based (one-decode-per-finished-utterance) architecture, not the old streaming one — Day 16's "medium costs ~3.5x the latency" finding was measured under streaming's repeated whole-buffer re-decoding, which no longer applies now that Whisper only runs once per closed segment.
- Compare `small.en` (current default) against `medium.en` and a distilled model such as `distil-large-v3` (claims close to `large` accuracy at closer-to-`small` speed) — real per-segment latency and a real accuracy comparison, using genuine clean-English test audio (TTS is fine for a first pass, same as Day 16, but call out plainly if results look TTS-flattering rather than a fair test).
- Also sanity-check that the accuracy complaint isn't coming from somewhere other than model size: current VAD aggressiveness/beam-size settings, or the 48kHz-stereo-to-16kHz-mono downsample step, before concluding a bigger checkpoint is the fix.
- Report real numbers and a recommendation (keep `small.en`, or switch to a specific alternative) before touching the live pipeline — this is a decision day, the actual model swap (if any) is quick follow-up work once decided.

**Done when:** you have real latency and accuracy numbers for at least `small.en` vs. one larger/distilled alternative under the current architecture, and a clear recommendation with reasoning — not a decision made from Day 16's now-outdated numbers.

**Actual outcome, 2026-09-06: keep `small.en`, real numbers behind it.** Benchmarked `small.en` (current) against `medium.en` and `distil-large-v3`, one whole-segment `transcribe_filtered()` call per clip — the exact function and settings (`beam_size=5`, `compute_type="int8"`) the live app actually uses, not a synthetic proxy. Latency: both alternatives cost a real ~3-4x penalty (a ~6-8s wait becomes ~15-20s on the 20-55s segments real interview answers routinely produce). Accuracy: neither alternative fixed a single error `small.en` made — all three models hit the same genuine homophone misses ("read"/"red", "write"/"right" — identical pronunciation, not something model capacity resolves) — and both bigger models introduced new errors `small.en` didn't have. Two sanity checks (the actual production downsample function, the actual production `VoiceSegmenter`, both against real audio) came back clean, ruling out either as a contributing cause. **Decision: no model change.**

One correction surfaced and flagged rather than quietly followed: the premise this task started from (Day 16's "medium is ~3.5x slower" came from the old streaming architecture and no longer applied) wasn't quite right — that number was already a single-shot, one-decode-per-clip comparison, same shape as today's architecture. Re-testing was still the right call, just for different reasons (no `medium.en` number had existed, no distilled model had been tried, the old clip was Taglish-flavored rather than clean English) — worth remembering that a "the old number doesn't apply anymore" reasoning should itself be checked, not assumed correct just because it sounds plausible.

**Real caveat, not settled by this test:** all test audio was clean TTS — no background noise, no accent, no real mic/loopback capture artifacts. The errors that showed up (homophones, one word split at a segment boundary) are exactly the kind a bigger model doesn't fix, so this result doesn't rule out a bigger checkpoint helping on real noisy/accented meeting audio. Flagged as a real open question, not pursued further right now since Day 18.6 (fast-speech live preview) and Day 18.7 (context feed) address related accuracy concerns from different angles — a real recorded (non-TTS) sample is the actual next step if this needs revisiting later.

## Day 18.6 — Fast-Speech Live Preview: Bring Back `base.en` Streaming, Ephemeral Only (research/spike first) ✅ done — recommendation: hold off, not implemented

Added 2026-09-05, right after the day18.5 discussion. Jon's real-world observation: the current segment-based `small.en` pipeline is accurate when speech has natural pauses (short, clean segments), but on fast, continuous speech — which can now run up to `MAX_SEGMENT_SECONDS` (60s) with nothing shown until it closes — the eventual whole-segment transcribe "does not get anything close" to correct. The old `base.en` streaming pipeline, even though also imperfect and the thing Day 18 reverted away from, "actually gets something" during that same fast speech — a rough live result beats a long silence followed by a bad final one.

**Important distinction to nail down before building anything, not to assume:** there are two different claims bundled in "bring `base.en` back," and they need different treatment.
1. *UX claim* — showing *something* live during a long unbroken stretch of fast speech beats showing nothing until the segment finally closes. This is safe to restore.
2. *Accuracy claim* — that `base.en`'s streaming output, once settled, is actually more correct than `small.en`'s whole-segment decode specifically on fast/continuous speech. This is a real, testable claim, not a given — Day 18's revert was driven by real bugs in a hybrid that tried to *merge* the two models' output (lost context on isolated re-decodes, a session's-first-segment context floor, cross-model timestamp misalignment causing duplicated words). None of those bugs came from *displaying* a streaming preview — they came from trying to reconcile or polish across two independently-decoded models into one committed text. That distinction is exactly what keeps this restoration safe.

**Design constraint, to avoid reopening the bugs that caused the revert:** bring `base.en` streaming back as a strictly ephemeral, display-only preview. While a segment is still open/accumulating, show the live `base.en` partial text (visually marked tentative, same idea as Day 17's `is_final` flag). The instant the VAD-detected pause closes the segment and `small.en` finishes its authoritative whole-segment transcribe, that text *replaces* the ephemeral preview outright — never merged, blended, polished, or reconciled with it. The ephemeral text is never stored, never sent to Claude, never part of the transcript log used for suggestions or (later) persistence or Day 18.7's context feed. One model shown, then swapped for the other — no cross-model reconciliation, which is what actually broke last time.

Treat this as a research/spike day first, same shape as Day 16:

- Confirm real CPU headroom for running `base.en` streaming *concurrently* with `small.en` whole-segment decoding on this CPU-only AMD hardware — don't assume it's free just because both ran independently before; measure whether they compete for the same cores and whether that slows down `small.en`'s own segment turnaround.
- Directly test the accuracy claim above with real fast-speech audio: does `base.en`'s committed streaming output actually read closer to correct than `small.en`'s whole-segment result, specifically on long, fast, unbroken speech? Real side-by-side text comparison, not an impression. If `small.en`'s final accuracy on fast speech turns out to be the real problem (not just the wait), an ephemeral preview alone won't fix it — that would point back to Day 18.5's model-size work or a fast-speech-specific segmentation change (e.g. forcing shorter sub-segments on detected fast cadence) rather than to restoring streaming.
- If the ephemeral-preview design holds up: figure out how much of the pre-Day-18 streaming code (`wsl_app/streaming_transcriber.py` before it was trimmed to ~164 lines) is safe to reuse for display purposes only, versus what should be written fresh now that it's a UI-preview feature rather than the authoritative pipeline.

**Done when:** you have real numbers on the concurrent-CPU-load question, real evidence on whether `base.en`'s live output is actually more accurate on fast speech than `small.en`'s whole-segment result (not just "it shows something"), and — if you proceed — a live ephemeral preview that visibly updates during fast/long speech and cleanly gets replaced by the authoritative `small.en` text on segment close, with nothing from the preview leaking into what suggestions or the transcript log actually see.

**Actual outcome, 2026-09-06: hold off, not implemented.** All three checks came back against building this, not just inconclusive. Accuracy claim: false, and specifically worse on the exact scenario it targets — on a short clip `base.en` was fine, but on a 39.5s continuous fast-speech clip (a real single-VAD-segment clip, confirmed against the actual production `VoiceSegmenter` before trusting it as a test case), roughly half the settled transcript was simply missing, not just rougher. Real content loss in the LocalAgreement-2 commit step itself — the same structural buffer-trim defect Day 16 first found and Day 18 already reverted away from once, reproduced again here in the one scenario that seemed like it might be the exception. A live viewer would watch words appear and then get erased as the buffer trims past content that never committed, which is worse than showing nothing. Realtime-keeping: also failed on the same clip (1.41x realtime factor, falling further behind the longer a segment runs), matching Day 16's original finding. CPU cost: running `base.en` streaming concurrently slowed `small.en`'s own segment decode by a real ~1.53x (5.64s → 8.61s average) — a real tax on the pipeline that actually matters more, not free.

**Real caveat, flagged rather than glossed over:** the clean-TTS test of `small.en` on the same 39.5s fast clip stayed structurally complete with only minor word-substitution errors — it did not reproduce the severe "doesn't get anything close to correct" breakdown the original complaint described. Either that's a real-audio-quality gap this synthetic test can't capture (same standing caveat as Day 18.5), or it's specific to audio this test didn't happen to hit — worth keeping in mind if the underlying complaint gets revisited.

**Untried, more promising angle for the underlying "nothing shown for up to 60s" problem, flagged for a possible future spike, not committed to any day:** a fast-speech-aware *segmentation* change — force a shorter sub-segment boundary when fast/continuous speech is detected, so `small.en` itself gets called sooner on a smaller chunk — rather than a second streaming model reconciled or swapped against the first. This avoids all three problems found here since it's still one model, one decode per closed segment, just with an earlier, fast-speech-aware trigger for closing one. Not pursued now.

## Day 18.7 — Continuous Context Feed: Feasibility Spike (research/spike, no live pipeline changes yet) ✅ done — recommendation: hold off, not implemented

Added 2026-09-05, Jon's idea. Today `MeetingSession` reconstructs a rolling transcript window and pastes it into the prompt fresh at trigger time. The proposal: feed each finished transcript segment into the long-lived `claude` CLI session as it happens instead, so the model's own turn history holds the whole meeting by the time you click, and the trigger sends only a small fixed nudge rather than a reconstructed prompt. Jon's reasoning, worth testing rather than assuming: even where the literal transcript has real errors (base.en-era streaming was the sharpest example), it stays broadly readable, and a session that's accumulated the whole conversation should be able to recover the intended question with much better accuracy than any single noisy segment on its own. See PRD §8 Phase 2.5 for the full note, including the one real limit this doesn't fix (a confidently fluent but wrong transcription gives the model no signal anything's off — that's what Day 18's hallucination filters are for, not this).

Same shape as Day 16 — a research day first, because there are genuinely open questions here, not just an implementation choice:

- Confirm empirically whether the `claude` CLI's headless stream-json mode can ingest a user turn purely as context, without forcing a full assistant-response generation every time. If every fed segment forces a reply, this doesn't work as described and needs a different mechanism (e.g. batching several segments into one "silent" turn) — verify against the real CLI, the same way Day 6 verified stream-json itself rather than assuming.
- If it does work: redesign subprocess recycling. Today's "recycle every 10 `ask()` calls" is sized around suggestion-call volume only; transcript segments arriving continuously would blow past that far faster. Decide what a sensible recycling boundary looks like now (elapsed time? estimated context size?) and what — if anything — carries over into a freshly recycled process, since today nothing does and a long accumulated context would have much more to lose.
- Test the actual claim with real, imperfect transcript text (the base.en-era test clips are a reasonable starting point): does a long-accumulated session actually recover the intended question more reliably than the current one-shot rolling-window prompt does, on the same noisy input? Also deliberately test the failure mode this doesn't fix — a fluent-sounding hallucinated sentence — and confirm it's still as much of a problem here as it was before, not quietly "solved" by having more context.
- Measure real trigger-time latency against the current approach. A tiny nudge prompt should be cheaper to construct, but a much larger accumulated context per call could be slower for the CLI to process — get real numbers, don't assume the net effect either way.

**Done when:** you know for real whether the CLI can ingest context without forcing a reply each time, have a concrete recycling redesign if it can, and have real evidence — not just the concept — that this measurably improves suggestion accuracy on real noisy transcript segments versus today's rolling-window prompt, with the hallucination-caveat explicitly re-tested, not assumed fixed.

**Actual outcome, 2026-09-06: hold off, don't build.** Confirmed against the real `claude` CLI, not assumed: there is no flag or turn type that ingests context without a full, paid generation — every `ask()` call forces a real reply, verified both with a raw stream-json probe and with the actual production `ClaudeCli.ask()` pattern (4 of 4 segments each produced a full real response, 2.87-4.26s each). The task's own proposed fallback ("batch several segments into one silent turn") turns out, on inspection, to just be today's rolling-window design again with a bigger window — there's no way around paying for a generation per segment otherwise. This alone forecloses the core premise.

Worked through the rest anyway for real evidence: a side-by-side test using a real `small.en` transcript of the Day 18.6 fast-speech clip (containing 3 genuine transcription errors, e.g. "feature flat rollout" for "feature flag") found no accuracy advantage for feeding segments as native turns — if anything, today's rolling-window design correctly disambiguated the one clearly-testable term ("flag") where the accumulated-turns design's final answer did not — at a real ~6-8x compute cost (41.50s across 7 calls vs. 4.88-7.59s for one rolling-window call), a gap that widens the longer a real meeting runs rather than amortizing. The hallucination caveat was deliberately re-tested and confirmed still unfixed: neither approach showed any suspicion toward the 3 real transcription errors already in the test data, silently building fluent answers on top of wrong words in both cases — this spike's own accuracy test doubles as a live demonstration that more context doesn't touch this failure mode, which stays the sole job of Day 18's `no_speech_prob`/`avg_logprob` filters. Recycling was also worked through despite being moot: a native-turn-history design has no clean recycling story at all, since periodically restarting the process (needed to bound growth) would repeatedly destroy the exact accumulated context the whole feature exists to build — another symptom of the same underlying problem, not a separate one.

**Not implementing.** One cheap, different lever flagged for a possible future look, not committed to any day: `SEGMENTS_TO_KEEP_FOR_CONTEXT` is currently a fixed count (10), not a token/size budget — raising it, or making it size-based, adds real history to the one call that's already happening for nearly free (marginal prompt tokens only, no extra generation), if a long meeting losing early context ever turns out to matter in real use.

## Day 18.8 — Live-Agent-Listening: Feasibility Experiment ⏸ paused — real finding, but see the deployment-model caveat before Phase 2

Added 2026-09-06, Jon's follow-up idea right after Day 18.7's "hold off" verdict. Different in kind from Day 18.7: instead of feeding segments into the scripted, headless `claude -p` subprocess `ClaudeCli` wraps (rejected in Day 18.7 since every turn there forces a full paid generation), keep an actual interactive agent session — a live Claude Code conversation — attached to a real meeting, watching the transcript stream in and reasoning about it continuously, reacting to most segments with something trivial and only producing a real answer when triggered.

**Phase 1, done 2026-09-06: self-contained, real-time-paced.** `wsl_app/research/day18_8/live_listen_harness.py` fed a real WAV clip through the actual, unmodified production pipeline (`main.VoiceSegmenter`, `main.transcribe_segment`) in real-time-paced 100ms chunks, watched live via the Monitor tool. Real transcription errors (the same homophone class Day 18.5/18.7 already found) were silently recovered from context with no flagging needed, and the suggestion produced at trigger time was grounded and specific. Two findings: trigger-time latency was effectively zero (context was already live in the agent's own session, no subprocess/API call needed at trigger time) — but the real cost is scale and deployment model, not per-response price: ~7 turns for a 46s clip alone (dozens to hundreds for a real meeting), and this only works while an interactive agent session is actively attached and watching for the whole meeting, which `wsl_app` cannot run unattended. Full writeup: `wsl_app/research/day18_8/README.md`.

**Assessment before proceeding to Phase 2, 2026-09-06 — this needs a direction decision, not just "run the next test":** the mechanism Phase 1 actually exercised is not something the shipped `windows_app`/`wsl_app` pair can invoke on its own. It required a full-tool-access, human-attended Claude Code coding session watching output via the Monitor tool — a development-time technique, not a production integration point. Concretely, "Phase 2, run this live" as scoped would mean Jon keeping a second application (an interactive Claude Code session) open and attended for the entire length of every real meeting or interview, functioning as the actual suggestion engine, alongside the two processes that already run unattended today. That's a different product shape, not an increment: it conflicts with the reason `ClaudeCli` was built headless and non-interactive in the first place (Day 6, `--tools ""`, specifically to avoid needing a human in the loop), with the "low-friction, just works" goal in PRD §2, and with the "chatty tool could hit subscription rate limits" risk already flagged in PRD §10 — dozens to hundreds of turns per meeting is a much heavier real usage pattern than today's ~1 call per suggestion.

**A real, buildable alternative worth testing instead, before committing to the attended-session direction:** Day 18.7's "no accuracy benefit at ~6-8x cost" finding was measured with a system prompt that made every segment turn produce a full paragraph of reasoning (2.87-4.26s each) — that's what made it expensive, not the mere fact of sending a turn per segment. Day 18.8's own agent chose, on its own judgment, to react to most segments with something trivial and save real effort for the trigger — nothing structurally stops the existing headless `ClaudeCli` subprocess from being instructed to do the same thing via its system prompt (e.g. "segment update: reply with a single character only; question: now answer fully"). This would still be unattended and still fit the current architecture, and is worth a real test of its own before deciding this idea needs a fundamentally different deployment model. It would not reproduce Phase 1's "near-zero trigger latency" claim, though — that came specifically from the context already being live in an already-running agent's own reasoning, not from anything a discrete subprocess call can shortcut; a cheap-ack redesign of `ClaudeCli` would still cost roughly today's ~5-8s at trigger time, just potentially with better accumulated context behind it at a much lower ongoing cost than Day 18.7's expensive-per-segment version.

**Not proceeding to Phase 2 as scoped without a direction decision from Jon.** Three ways this could go, not yet decided: (a) pursue the attended-session mode as a genuinely new, second mode of the app, accepting the deployment-model change; (b) test the cheap-ack redesign of the existing unattended `ClaudeCli` instead, which stays inside the current architecture; (c) set this aside and proceed with the already-scoped plan (Day 19 onward) as-is.

**Direction decided, 2026-09-06: (a) — pursue the attended-session mode as a real second mode of the app.** Operational model confirmed: Jon starts a Claude Code session on his own machine before a real meeting and leaves it running fully hands-off for the whole thing — no typing into it during the meeting. Phase 1 already demonstrated this shape (every reaction was notification-driven, zero messages from Jon in between), so this isn't a new assumption, it's the thing that already ran.

Before committing further scope, two real open questions were raised about whether Phase 1's appeal (segments arriving as free/cheap background notifications rather than full paid turns) survives outside this exact setup:
1. Does a bare terminal `claude` session (no IDE) expose the same Monitor/background-task tooling Phase 1 used? Reported as "likely yes, same underlying SDK" but not yet independently confirmed by an actual bare-terminal run — that claim is the thing the next check is *for*, not something to treat as already settled just because it sounds plausible. Same standard this project has applied to every other claimed-but-unverified result.
2. Does it hold up fully unattended over real duration, not just ~60 seconds with someone narrating between notifications? Phase 1 is real evidence the mechanism doesn't need human input to advance, but it isn't evidence it survives 30-60 minutes with nobody watching at all — the two are different claims.

## Day 18.9 — Live-Agent-Listening: Local Unattended Feasibility Check (Jon runs this directly, not a Claude Code prompt) ✅ done — pass (question 1 only)

Added 2026-09-06. This one can't be handed off as a coding prompt — it requires physically starting a terminal and walking away, which only Jon can do. Answers question 1 above; question 2 (real-duration robustness) is explicitly **not** answered by this check and needs a separate, longer test afterward — don't read a pass here as closing both.

**What to do:**
1. Open a plain terminal (not VSCode, no IDE integration) and start a `claude` session pointed at `/home/jon/projects/adarna-ai`, the same project Day 18.8's Phase 1 ran against.
2. Re-run the same check: point it at `wsl_app/research/day18_8/live_listen_harness.py` the same way Phase 1 did, so it re-processes `research/day16/en_normal.wav` in real-time-paced chunks.
3. Once it starts, genuinely walk away — don't watch the terminal, don't type anything, don't check in partway through.
4. Come back after it should have finished and check what happened: did it produce the same quiet-ack-then-real-answer-at-trigger sequence with zero intervention, the same as Phase 1's result, or did it stall, need input, or behave differently outside the IDE?

**Done when:** you have a real answer, from an actual unattended bare-terminal run, on whether the same notification-driven behavior holds outside VSCode. If it does: question 2 (real-duration robustness) is the next thing to test, with a longer clip or eventually a real meeting, before this becomes a shipped second mode. If it doesn't: this direction needs rethinking, and the cheap-ack `ClaudeCli` redesign (option b, set aside for now) becomes the fallback worth testing instead.

**Actual outcome, 2026-09-06: pass, cleanly.** Jon started `claude --permission-mode bypassPermissions` (needed so the unattended run wouldn't stall on an approval prompt with nobody there to answer it — a setup detail, not a finding about the mechanism) in a plain terminal, pasted a self-contained prompt describing the check, and genuinely walked away. That session backgrounded `live_listen_harness.py` and attached a persistent `tail -f`-plus-grep Monitor on its log — a different specific technique than Phase 1's direct Monitor-on-the-command call, but the same class of capability: a background task whose output arrives as live notifications with no polling and no human relay. The harness ran to completion unattended (exit 0), the trigger fired, and a real suggestion was composed with zero intervention at any point, comparable in quality to Phase 1's, correctly built on the real content without needing the read/write/dual-writing homophone errors flagged first.

**Question 1: confirmed, not just "likely."** The free-background-notification mechanism is not a VSCode-extension artifact or anything cloud-environment-specific — it's genuinely available in an ordinary local terminal `claude` session on Jon's own machine.

**Question 2 (real-duration robustness) is still open, unchanged** — this run was still the same ~46s clip, just unattended instead of narrated. Next up: Day 18.10, a real-duration test.

## Day 18.10 — Live-Agent-Listening: Real-Duration Unattended Test ✅ done — pass

Added 2026-09-06. Answers the one question Day 18.9 explicitly left open: does the notification-driven mechanism hold up over something closer to a real 30-60 minute meeting, fully unattended, rather than a ~46-second clip? A pass here is what actually justifies calling this a viable second mode — everything so far has been short enough that context growth, subscription rate limits (PRD §10's "chatty tool" risk), and long idle stretches (a normal part of a real meeting) haven't been exercised at all.

This splits into a prep step (delegatable) and a manual step (Jon only, same as Day 18.9).

**Prep — hand this to your local Claude Code session:**

Build a longer-duration version of the Day 18.8/18.9 harness test. The existing single ~46s clip (`research/day16/en_normal.wav`) doesn't exercise real-meeting duration, so extend the setup to simulate one — looping that clip (or an equivalent-length concatenation of the existing test clips) back-to-back for a real 30-60 minutes of continuous segment/trigger activity is a reasonable, cheap way to get there without recording new audio, but use your judgment on the exact shape. Whatever you build needs to produce a clear, timestamped log that can be reviewed *after the fact*, without anyone having watched it live, covering: whether segments and triggers kept firing correctly at the end of the run the same as at the start (no degradation), the total number of turns/notifications the mechanism generated end to end (a real estimate of what a real meeting's actual usage would look like, tying back to PRD §10's subscription rate-limit risk), any errors, stalls, or silent failures anywhere in the run, and how the mechanism behaved across any deliberately-included silent/idle stretches (real meetings have them) — confirm it doesn't need continuous activity to stay attached and working correctly.

**Manual — Jon runs this, same shape as Day 18.9:** once the harness is ready, start it the same way (bare terminal, `claude --permission-mode bypassPermissions` or equivalent, background it, walk away) — this time for the real duration, not a quick check. Actually leave it alone for the full run. Come back after and read the log.

**Done when:** you have a real, timestamped log from an actual 30-60-minute unattended run showing whether the mechanism holds up end to end — no degradation, no silent failures, a real turn-count estimate for what a real meeting would cost — or a clear account of where and how it broke down if it didn't.

**Prep done and smoke-tested, 2026-09-06 — the real-duration run itself has not happened yet.** `wsl_app/research/day18_10/live_listen_harness_long.py` built: loops 5 existing synthetic clips (from `research/day16/`, `research/day18_5/`, `research/day18_6/` — no new audio recorded) back-to-back into one ~193s cycle, repeated 9 times for a ~44-minute simulated meeting, with 3 deliberate 5-minute silent stretches inserted after cycles 3/6/9 (~34% of the run silent) to exercise the idle-behavior question. Key upgrade over Day 18.8/18.9's harness: runs a real `asyncio` event loop against the actual, unmodified `main.SuggestionTrigger` class, so `TRIGGER` lines fire from genuine pause-timer logic rather than one hand-scripted line — needed for the turn-count question to mean anything. Also built: a 60s heartbeat (so a real stall is distinguishable from a deliberate idle stretch), per-cycle `CYCLE_END` summaries (segment/trigger counts, average transcribe latency — cycle 1 vs. cycle 9 gives the degradation check), a final `SUMMARY` block (total notification count, first-10-vs-last-10 latency comparison), per-segment error catching so one transient error can't kill a 30+ minute run, and an uncaught-crash path (`HARNESS_CRASHED`, non-zero exit). Verified via a `--smoke-test` flag (~70s shrunken run: 2 clips, 1 cycle, 5s idle stretch) — exit 0, 6/6 segments and triggers fired correctly (matching Day 18.8's ~7-turns-per-46s-clip finding almost exactly), heartbeat/cycle/summary lines all correct, zero errors.

**Real run done, 2026-09-07: pass, cleanly.** Jon ran the full ~44-minute harness unattended per the plan above; actual wall clock came in at 49.9 min (all 9 cycles plus the 3 five-minute idle stretches). The harness exited via its own `HARNESS_SCHEDULE_DONE` path, not a crash. All four questions answered from the log:

1. **Degradation:** none. 141 segments transcribed (0 empty), 141/141 triggers fired — a clean 1:1, the suggestion pipeline never missed a beat across the whole run. Per-cycle average transcribe latency stayed flat: 2.42s–2.51s across all 9 cycles (overall avg 2.46s), no monotonic climb from cycle 1 to cycle 9.
2. **Turn-count estimate (PRD §10 rate-limit question):** 141 real notifications (segments + triggers, 1:1) over ~50 minutes of a mixed clean/fast/Taglish simulated meeting — the real number to reason about subscription rate-limit exposure against for an actual meeting of similar length.
3. **Errors/stalls:** zero `ERROR` lines, zero `HARNESS_CRASHED`, clean exit.
4. **Idle behavior:** all 3 deliberate 5-minute silent stretches produced zero spurious `SEGMENT` lines — the Day 18 hallucination filters (`no_speech_prob`/`avg_logprob`) held up under real extended silence, exactly the scenario they were built for. `HEARTBEAT` kept firing on schedule throughout.

**Conclusion: the notification-driven live-agent-listening mechanism, and the underlying Whisper pipeline, are stable over a real ~50-minute unattended run.** This closes the one question Day 18.9 left open — question 2 (real-duration robustness) is now confirmed alongside question 1 (bare-terminal, Day 18.9). Full log: `wsl_app/research/day18_10/run.log`. This closes out the live-agent-listening investigation (Day 18.8–18.10) with a real pass; whether/when it becomes a shipped second mode of the app is a separate product decision, not yet scheduled as a day.

## Day 19 — Mic Capture (Phase 3 item, pulled forward) ✅ done — 3 real bugs found live, all fixed and re-verified where possible from this environment

Pulled forward from Phase 3, ahead of opt-in persistence (now Day 20), per Jon's 2026-09-05 request to capture his own voice sooner rather than waiting for the rest of the summary phase. See PRD §8 Phase 3. Day 18.7 concluded hold off on continuous context feed, so this builds dual-source labeling on the existing rolling-window approach (unchanged mechanism, just fed from two sources instead of one), with `SEGMENTS_TO_KEEP_FOR_CONTEXT` sizing revisited as part of this same day per Day 18.7's flagged lever.

- Own-mic audio capture (Windows-native, alongside the existing WASAPI loopback capture) — same device-picker/test-capture pattern as loopback got in Day 3.
- Feed mic audio through the same VAD-segment-then-transcribe pipeline as loopback (`small.en`, per Day 18.5's conclusion), as an independent audio source, not merged into the loopback stream.
- Transcript display and the rolling context sent to Claude both label the two sources separately (e.g. "You:" / "Them:") rather than merging them into one undifferentiated line — decided this way so a suggestion prompt and eventual summary can tell who said what.
- Handle both sources being active at once without one blocking or starving the other (two independent capture threads/VAD segmenters feeding into one shared transcript view, similar in shape to how loopback alone works today).
- **Bundled in from Day 18.7's flagged cheap lever:** while touching how context gets assembled for two sources anyway, revisit `SEGMENTS_TO_KEEP_FOR_CONTEXT` (currently a fixed count of 10) — a two-source session fills that count roughly twice as fast as a one-source one, so a fixed count now truncates real context sooner than it used to. Consider a size-based budget instead of a fixed segment count, since Day 18.7 confirmed this is nearly free (marginal prompt tokens only, no extra generation).

**Done when:** during a real session, speaking into the mic and playing audio through loopback both produce correctly labeled, separately attributed transcript lines, a suggestion generated from a mixed exchange references both sides correctly, and the context-window sizing has been reconsidered for two active sources rather than left at a value tuned for one.

**Implementation done, 2026-09-07.** Both files got a real dual-source rework, not just a source-tag bolted on:

- **wsl_app:** `MeetingSession` now keeps one `VoiceSegmenter` per source (`self.segmenters = {"mic": ..., "loopback": ...}` — a dict, not the old single `self.segmenter`) instead of one shared segmenter, so mic speech/pauses never affect loopback's segment boundaries or vice versa. `VoiceSegmenter` gained a `ClosedSegment(audio_bytes, started_at)` return type (`started_at` = wall-clock `time.time()` captured on a segment's first speech frame, not when it closes) — this is what makes correct chronological ordering possible at all, since two independently-transcribed sources can finish in a different order than they were actually spoken in (a longer segment on one side started first but takes longer to transcribe). `handle_audio_chunk` now routes each `audio_chunk` message to the right segmenter by its new `source` field, with an unrecognized source logged and dropped rather than crashing (verified — see below). `session_stopped` now flushes whatever's still open on *both* segmenters, not just one.
- **Rolling context (the Day 18.7 lever folded in per the prompt):** `SEGMENTS_TO_KEEP_FOR_CONTEXT` (a fixed count of 10) is gone, replaced by `CONTEXT_CHARACTER_BUDGET = 4000` — roughly double the old cap's typical character count, sized to give a two-source session about the same real conversational time-depth the old cap gave one source, since two sources now fill any fixed count twice as fast. `MeetingSession.add_transcript_segment(source, text, started_at)` inserts each new segment into `recent_transcript_segments` by `started_at` (`bisect.insort`, not append), so the context Claude actually sees stays in real spoken order even when both sides talk around the same time — not just each source's own order. Trimming (`_trim_context_to_budget`) always keeps at least one segment, even if that one alone exceeds budget. The formatted prompt (`_format_context`) is now labeled multi-line text (`"Them: ...\nYou: ..."`), replacing the old plain space-joined blob — this is what lets Claude's suggestion prompt tell who said what, and it's the exact same text reused for the overlay's "question" field (Day 18), so no separate change was needed there.
- **windows_app:** a second `AudioCaptureManager` for the mic, alongside the existing loopback one — same class, now parametrized by `source`, run as two fully independent capture threads (neither waits on the other). Two device dropdowns, each with a caption ("Loopback device (system audio, i.e. \"Them\"):" / "Microphone device (your own voice, i.e. \"You\"):") since the pre-existing single dropdown had no label at all and two side-by-side would otherwise be ambiguous. `list_mic_devices()` (new) enumerates real input devices via `get_device_info_by_index()`, filtering out anything with `isLoopbackDevice: True` (WASAPI loopback devices also report `maxInputChannels > 0`, so this exclusion is required, not decorative) — verified against the actual installed `pyaudiowpatch` source (`isLoopbackDevice`, `get_default_input_device_info()`, `get_device_count()` all confirmed present, not guessed) before writing it. `TranscriptDisplay` reworked the same way as wsl_app's context window: `bisect.insort` by `started_at` instead of append-only, one labeled line per segment ("You: ..." / "Them: ..."), rendered newline-joined instead of the old space-joined single blob. Session start/stop now iterate a list of capture managers instead of touching one directly; `end_session()` got simpler in the process — since `AudioCaptureManager.stop_capture()` is already a safe no-op on a manager that isn't capturing, unconditionally calling it on every manager replaced the old `capture_already_stopped` bool entirely.
- **Graceful no-mic fallback, found by a `/code-review` pass (one real finding) before this was called done:** the first version called `audio.get_default_input_device_info()` unguarded at startup, which raises `OSError` on any machine with no microphone (or a disabled default recording device) — a real regression for a loopback-only user, since the whole app would fail to launch. Fixed with `get_default_mic_device()`, which catches that and falls back to the first enumerated mic device if any exist, or `None` if the machine genuinely has none — `main()` then skips creating the mic capture manager entirely and disables the mic dropdown with a "No microphone available" status, rather than crashing. The rest of the review pass checked and ruled out several plausible concerns rather than assuming they were fine: `WslConnection`'s existing `_write_lock` already covers the two new concurrent capture threads both calling `send_message()`; the `bisect.insort` tuple ordering and per-source `VoiceSegmenter` isolation were both correct on inspection.
- **Verified offline, not just read through:** `wsl_app/main.py` imports cleanly end-to-end against its real venv (webrtcvad, faster-whisper, numpy all resolve, no `NameError`s from the rework). A standalone script (not checked into the repo, scratch-only) exercised the new logic directly: out-of-order-arriving segments land in `started_at` order in both the context window and (by the same `bisect.insort` pattern) the transcript display; the character budget trims from the oldest segment down to (and never below) 1 once over 4000 chars; `VoiceSegmenter` correctly captures a segment's start time from its *first* speech frame (not its close time) and gives the next segment its own fresh start time rather than leaking the prior one; an unrecognized `source` on an `audio_chunk` message is logged and dropped, not a crash. All passed.

**Real live test, 2026-09-07: dual-source labeling worked, but surfaced 3 real bugs — all found, root-caused, and fixed same session.** Jon ran a real session (real mic speech + real loopback playback) and confirmed the core ask worked (mic transcript showed up correctly as "You:" lines), but reported three concrete problems, not hypotheticals:

1. **Mic noise transcribed as gibberish** (e.g. "It's a wig.", "if you're not safe.") — a real mic's continuous room-noise floor (which loopback's clean digital audio never has) was tripping the speech detector at its existing loopback-tuned strictness, handing Whisper short noise clips it then hallucinated on. Fixed with a new `MIC_SPEECH_DETECTION_STRICTNESS = 3` (webrtcvad's strictest setting), applied only to the mic's `VoiceSegmenter` via a new `SPEECH_DETECTION_STRICTNESS_BY_SOURCE` mapping — loopback stays at the original strictness 2. **Confirmed fixed, 2026-09-08:** Jon re-tested on a real mic session, no issues reported.
2. **Transcription had gotten much slower** (segment "time to transcribe" values in the real log ran 15-35s, for audio that normally transcribes in 2-4s per Day 18.5's benchmark — same `small.en` model, nothing changed there). Root cause: the mic-noise false positives from bug 1 opened a *burst* of short segments in quick succession, and with no concurrency limit, `asyncio.create_task` let all of them try to run `model.transcribe()` at once on the one shared Whisper model — real CPU contention, not real parallelism, matching Day 18.6's earlier independent finding that concurrent Whisper decode calls compete for the same cores. Fixed with `WhisperModelManager.transcribe_lock` (`asyncio.Lock`), held only around the actual `transcribe()` call in `transcribe_segment_and_report` — segmentation and capture stay fully independent per source; only the CPU-bound decode step now queues instead of contending. **Confirmed fixed, 2026-09-08:** Jon re-tested on a real mic session, timing reported good.
3. **Suggestions came back reading like an instruction/outline, not a script + bullets** — the most serious finding, and root-caused live rather than guessed at. Two distinct causes, both confirmed directly against the real `claude` CLI:
   - **Root cause A (the big one): `ClaudeCli`'s subprocess was inheriting `wsl_app`'s own working directory (inside this project), so the `claude` CLI auto-loaded this project's own `CLAUDE.md` into context — a separate mechanism from `--system-prompt`, which only replaces the top-level framing and does not disable it.** Reproduced directly: the identical prompt, run from this project's directory, produced a suggestion that referenced "Day 18 note on INTERVIEW_SYSTEM_PROMPT" and "project_notes.md" — i.e. the model reasoning about its own development history instead of answering the question. Re-running from a neutral directory eliminated it completely. This has likely been a latent risk since Day 6 (`ClaudeCli` never set an explicit `cwd`), only surfacing now because real conversation content can resemble topics this now-29KB+ `CLAUDE.md` discusses at length. Fixed: `ClaudeCli.__init__`'s `subprocess.Popen` now passes `cwd=tempfile.gettempdir()`.
   - **Root cause B: even with A fixed, both mode instructions could still produce a lead/bullets that were meta-instructions about what to say ("Explain the basic idea...", "Cover cache-control headers like max-age, no-cache, and no-store") rather than the actual content.** Reported by Jon with a real example (an HTTP caching walkthrough question) and reproduced directly: meeting mode's lead came back as "Give a clear step-by-step walkthrough of..." — second-person meta-advice, not content. Fixed with a new shared `CONTENT_NOT_OUTLINE_INSTRUCTION`, appended to both `RESPOND_WITH_LEAD_AND_BULLETS_INSTRUCTION` and `INTERVIEW_RESPOND_INSTRUCTION`, using Jon's own real HTTP-caching example as a concrete in-prompt good/bad contrast (matching this project's established pattern of fixing prompt-shape bugs with a named example, not another abstract adjective — see `INTERVIEW_RESPOND_INSTRUCTION`'s own Day 18 history). Meeting mode's lead wording was also reworded from "the general framing of how the user could respond" (which invited meta-advice) to "the actual start of what the user could say out loud... not a description of how they should respond."
   - Also added `IGNORE_TRANSCRIPT_NOISE_INSTRUCTION` (both modes) after an early real suggestion broke format entirely to comment on transcript quality ("Ignore the noisy transcript lines above, that's just mic/transcription garbage") — a related but distinct symptom from B, addressed at the same time.
   - **Live-verified, not just read through:** re-ran the exact reported HTTP-caching case plus 3 varied inputs (a different technical topic, a behavioral interview question, a garbled-transcript case) directly against the real `claude` CLI after both fixes — every case came back in correct lead+bullets shape with real content in every line, no project-context leakage, no meta-outline bullets. One residual, disclosed rather than hidden: the noisy-input case can still occasionally open with a mild "that came out garbled" aside even though the format itself no longer breaks — a softer version of the original bug, not fully eliminated.

**What's confirmed:** all three bugs are now live-verified. The suggestion-format fix (bug 3) was verified against the real CLI with varied inputs the same session it was fixed; bugs 1 and 2 (mic VAD strictness, transcribe concurrency lock) were code-reviewed and offline-logic-tested then, and confirmed by Jon on a real mic session 2026-09-08 — no issues reported. This also closes the "still unconfirmed" felt-latency question below: whether `transcribe_lock`'s serialization meaningfully changes felt latency when both sources talk at once wasn't measured with numbers, but Jon's real-session confirmation is the practical answer this project has been waiting on.

## Day 20 — Rolling Transcript + STT Model Upgrade: Feasibility Spike (research only)

Added 2026-09-07, pulled forward ahead of persistence (now Day 21) per Jon's request right after Day 19 — same kind of reprioritization as mic capture getting pulled forward earlier. Two related but distinct real complaints from Day 19's live test, both about transcription quality/feel rather than the dual-source mechanism itself:

1. **Bring back a rolling transcript that doesn't wait for a pause to show text** — the current pipeline (unchanged since Day 18's revert) only shows a segment's text once `SILENCE_SECONDS_TO_CLOSE_SEGMENT` (0.4s) closes it, or after `MAX_SEGMENT_SECONDS` (60s) force-closes a long run-on segment. Jon wants the earlier, Day 17-era live-updating feel back.
2. **An improved/upgraded STT model** — Jon specifically flagged [`m-bain/whisperX`](https://github.com/m-bain/whisperX) as a candidate to evaluate.

**Important finding from research already done before handing this off, so tomorrow's session doesn't have to rediscover it — read this before doing anything else:** WhisperX does **not** solve problem 1. Confirmed directly against its real README (not assumed): WhisperX's actual architecture is `faster-whisper` (the same backend this app already uses) plus **VAD-segment-then-batch-transcribe** — "VAD-based segment transcription, unlike the buffered transcription of openai's" is WhisperX's own description of its approach. That's the same fundamental shape this app's `VoiceSegmenter` → `transcribe_segment` pipeline already has (segment on a pause, then transcribe the whole segment) — WhisperX doesn't do incremental/streaming decoding, word-by-word or otherwise. Its actual advantages are: (a) **batched inference** — processing many already-known segments through the model together for throughput, which is an offline/whole-file speedup technique, not something that helps a live stream where segments arrive one at a time in real time; the headline "70x realtime with large-v2" claim is specifically GPU-batch-size-dependent; (b) **wav2vec2 forced word-level alignment** — more accurate word *timestamps*, not more accurate transcribed *text* (the WhisperX paper itself notes "bigger alignment model not found to be that helpful"); (c) **pyannote speaker diarization** — solves a problem this app already solved structurally and more reliably via Day 19's separate mic/loopback capture channels (ground-truth speaker identity from which device captured it, not an ML guess from one mixed stream); (d) **VAD preprocessing + `condition_on_prev_text=False` for reduced hallucination** — this app already independently does both (`VoiceSegmenter` + `transcribe_filtered()`'s existing `condition_on_prev_text=False`, `NO_SPEECH_PROBABILITY_THRESHOLD`, `AVG_LOGPROB_THRESHOLD`). Also confirmed empirically: **this machine has no GPU** (`nvidia-smi` not found, `torch.cuda.is_available()` unreachable — no `torch` even installed) — WhisperX's marquee speed claim is unreachable here regardless of the batching-vs-streaming question. 16 CPU cores are available, so a CPU-only batching benefit isn't flatly impossible, just unproven and likely marginal for this app's actual usage pattern (near-continuously one segment in flight per source, rarely many at once) — worth one real measurement, not zero, but go in with calibrated expectations, not the README's headline number.

**So this needs two separately-evaluated tracks, not one "try whisperX" task:**

- **Track A — the actual "doesn't wait for pauses" goal.** WhisperX doesn't address this; the real candidates are: (a) revisit Day 16-18's LocalAgreement-2 streaming approach, now informed by everything learned reverting from it (buffer-trim duplication, content loss on long unbroken speech, cross-model boundary misalignment when trying to reconcile two models) — same known risk profile, don't re-attempt without a concrete plan for why it'd avoid those specific, well-documented bugs this time; or (b) Day 18.6's own flagged-but-never-tried idea: a **fast-speech-aware segmentation** change — force a shorter sub-segment boundary when continuous fast speech is detected, so `small.en` itself gets called sooner on a smaller chunk, rather than adding any second model or streaming technique. (b) is the more promising starting point precisely because it doesn't introduce any of the specific failure modes that killed the streaming attempts — still one model, one decode per closed segment, just an earlier, fast-speech-aware trigger for closing one.
- **Track B — real STT accuracy, now testable on real audio for the first time.** Day 18.5's "keep `small.en`" conclusion and Day 12/16's Taglish findings were both explicitly caveated as TTS-only, not a real test — Day 19 now means this app can capture **real mic audio**, closing that long-standing gap. Worth a real accuracy comparison (`small.en` vs. a couple of alternatives, WhisperX's batched pipeline included as one candidate since it's already been specifically asked about) on real recorded mic speech, not synthetic TTS, before concluding anything about model choice.

**Scope for tomorrow — research/spike only, same shape as Day 16/18.5/18.6, no live pipeline changes yet:**

- Track A: prototype and benchmark the fast-speech-aware segmentation idea (a shorter forced sub-segment boundary during detected continuous fast speech) against the current fixed-threshold `VoiceSegmenter`, using real recorded speech (TTS is fine for a first pass, flag plainly if it looks TTS-flattering). Does it meaningfully reduce the "nothing shown for up to 60s" gap without reintroducing the Day 16-18 buffer/boundary bugs (it shouldn't, structurally, but confirm rather than assume)?
- Track B: get a real CPU-only benchmark of WhisperX's batched pipeline (accuracy AND latency, this app's real `small.en`/`transcribe_filtered` as the baseline) — ideally against real recorded mic audio (Day 19 now makes this possible; if a real recording isn't available yet, say so plainly and fall back to TTS with the caveat flagged, same as every prior model comparison). Confirm or refute whether CPU-only batching offers any real throughput/latency win for this app's actual usage pattern, and whether forced alignment or WhisperX's hallucination handling catches anything this app's existing filters don't.
- Report a recommendation for each track independently before writing any implementation code — it's entirely possible the right call is "yes to A, no to B" or vice versa, they're not a package deal.

**Done when:** you have real numbers (not documentation-only claims) on both tracks — whether the fast-speech-segmentation approach measurably helps the "nothing shown for a long stretch" problem without the previous streaming attempts' failure modes, and whether WhisperX (or any real alternative) offers a measurable accuracy or latency win over `small.en` on real (not just TTS) audio — with a clear recommendation for each, before any pipeline code changes.

**Actual outcome, 2026-09-07: Track A — yes, build it, but only with an accompanying fix; Track B —
hold off.** Full numbers and methodology in `wsl_app/research/day20/README.md`; summary below.

**Track A.** The core idea (a shorter force-close threshold during continuous/fast speech) delivers
a real win at 16s: `fast_long.wav`'s time-to-first-text drops from 50.3s to 21.5s with *better* WER
(5.6% vs 6.8%) than the current 60s threshold, zero regression on clips with natural pauses. But
shorter, more responsive thresholds (8s/12s) reintroduced real, severe content loss on the exact
clip the change targets — WER up to 45.2% at 8s, whole clauses silently vanishing. Root-caused
directly (`track_a_diagnose_boundary_loss.py`): the missing audio is real, clearly-spoken speech
that Whisper transcribes correctly on its own, but `NO_SPEECH_PROBABILITY_THRESHOLD` (tuned Day 18
for a different problem — real silence at session-stop) spuriously fires on any chunk whose edges
don't land on a natural pause, and `transcribe_filtered()` drops the whole segment. This is a new,
fourth failure mode, not one of the three the original framing assumed this approach would avoid
structurally. The fix (skip that one filter specifically on forced-close segments, since the
segmenter already knows a given close wasn't a real pause) was implemented and verified
(`track_a_boundary_safe_fix.py`): `fast_long` at 8s recovers to 7.2% WER (vs. 6.8% baseline) while
keeping the ~80% latency win. One smaller residual risk remains even with the fix — a mid-sentence
cut can still occasionally make Whisper fabricate a short clause rather than drop content, a milder
version of the same known "boundary-decode fragility" class, not something this specific fix
addresses. **Recommendation: build it, with the boundary-safe filter change as a required part of
the same change, not a follow-up** — the naive version is a real regression on its own target case.

**Track B.** WhisperX (Silero VAD, no HuggingFace token needed) benchmarked directly against this
app's real production `transcribe_filtered()`/`small.en` call, same checkpoint, CPU-only, isolated
venv (`research/day20/whisperx_venv/`, kept out of the main `wsl_app` venv to avoid destabilizing
the working STT path). Accuracy: essentially identical (same WER on 3/4 clips, marginally better on
one). Batching: confirmed to not help this app's usage pattern, as the pre-spike research predicted
— WhisperX's own VAD only found 1-2 internal chunks per ~40s clip (this app hands it one
already-closed utterance at a time, not a backlog of many short clips), so `batch_size` 1 vs. 4 vs.
8 moved latency by only a few percent, within noise. There is a real, if modest, latency win
independent of batching (7-15% faster even at `batch_size=1`, most likely from WhisperX's own VAD
trimming silence at each chunk's edges before decoding) — but it's achievable directly in this app's
existing pipeline (trim each closed segment's leading/trailing near-silence before transcribing,
reusing `SILENCE_AMPLITUDE_THRESHOLD`'s existing approach) without adopting WhisperX's ~3GB
`torch`/`pyannote-audio`/`transformers` dependency tree for a benefit that doesn't need it.
**Recommendation: hold off** — real caveat, same as every prior model-comparison day: this was all
TTS audio, no real mic recording was available to test against (Day 19 makes real capture possible,
but none exists yet in the repo and this research session had no way to record one itself, same
class of gap as every prior real-physical-action item in this project) — provisional until a real
recording is tested, but not a reason to adopt WhisperX on what was actually measured here.

## Day 21 — Rolling Transcript: Implementation (fast-speech-aware segmentation + boundary-safe fix) ✅ implemented and live-verified

Actual outcomes: both pieces landed together in `wsl_app/main.py`/`streaming_transcriber.py`, ported
from the verified `research/day20/track_a_boundary_safe_fix.py` logic as real production code (real
docstrings, plain naming) rather than copy-pasted research-script style. `MAX_SEGMENT_SECONDS` lowered
60.0→10.0 (the midpoint of Day 20's verified 8-12s range), with the comment rewritten to explain both
why Day 18 raised it *and* why Day 20's fix makes lowering it safe again. `ClosedSegment` gained
`closed_on_forced_timeout: bool`, set explicitly (not inferred from timing) at each of its three close
sites in `VoiceSegmenter` — `True` only for the `MAX_SEGMENT_SECONDS` path, `False` for a real pause
close and for the session-stop flush. `transcribe_filtered()` gained `skip_no_speech_filter`, threaded
through `transcribe_segment()` → `transcribe_segment_and_report()` → `handle_finished_segment()`;
`AVG_LOGPROB_THRESHOLD`/`SILENCE_AMPLITUDE_THRESHOLD` stay active unconditionally.

Verified two ways: unit-level (`VoiceSegmenter` reports the right flag on all three close paths, and
it threads correctly into `skip_no_speech_filter`), and by running the actual production code (not a
reimplementation) against Day 20's four TTS benchmark clips with the real `small.en` model — `fast_long`
(the run-on-sentence stress case) reached 14.3s time-to-first-text at 6.8% WER, matching the old 60s
baseline with no severe content loss, reproducing Day 20's numbers through the real wiring. One real
finding surfaced during this: `en_normal` picked up a "Thank you for watching" hallucination at the new
threshold that doesn't occur at 60s. Traced it directly — it happened on a segment that closed via a
**real pause**, not a forced timeout, so the new filter skip wasn't even in play; a control run at the
old 60s threshold confirmed the same clip doesn't hallucinate there. Root cause: shifting segment
boundaries at the shorter threshold can occasionally isolate a short trailing-breath fragment as its
own ambiguous segment, hitting the pre-existing "short/ambiguous segment" hallucination class this
project already knew about (Day 18's ultra-short-tail garbled-artifact note) rather than a new failure
mode — just newly exposed by more frequent forced boundaries. Not fixed (matches the residual-risk
framing already carried into this day's scope below), but worth knowing about going in.

**Resolved 2026-09-08: live-verified by Jon on a real mic/loopback session.** Confirmed good, no
issues reported — closes the "TTS-only" caveat above. This was the last open item for Day 21.

Also folded into this session, ad hoc, not part of the original scope below: the "Auto-suggest on
pause" checkbox now defaults to unchecked (`windows_app/main.py`), kept in sync everywhere the old
`True` default appeared in `wsl_app/main.py` (`default_settings()`, `SuggestionTrigger.__init__`,
`MeetingSession.__init__`, and the `auto_suggest_changed` handler's defensive fallback) — a session
now starts with suggestions off until explicitly asked for. Committed together as `403b8cb`, no
co-author trailer per standing instruction (see `MEMORY.md`).

<details><summary>Original Day 21 scope (for reference)</summary>

## Day 21 — Rolling Transcript: Implementation (fast-speech-aware segmentation + boundary-safe fix)

Added 2026-09-07, scoped directly from Day 20's Track A recommendation — implementation, not
another research day. Bumped ahead of persistence (now Day 22), same kind of reprioritization as
Day 19/20 getting pulled forward earlier — Track A's fix is verified and ready to port, not worth
sitting on.

**The two pieces land together, not sequentially** — Day 20 found the naive version (a shorter
force-close threshold alone) is a real regression on its own target case (severe content loss on
continuous fast speech) unless paired with the filter fix. Shipping one without the other
reintroduces the exact bug Day 20 spent most of its time on.

- `VoiceSegmenter`'s force-close threshold moves from `MAX_SEGMENT_SECONDS = 60.0` down into the
  8-12s range Day 20 benchmarked as the responsive, "actually doesn't wait for a pause" target (16s
  is the zero-observed-risk fallback if 8-12s feels too aggressive once live — Day 20's own numbers
  cover both). **Read the history in `wsl_app/main.py`'s `MAX_SEGMENT_SECONDS` comment before
  touching it**: it was deliberately *raised* from 20.0 to 60.0 on Day 18 specifically because a
  short forced cut was hurting accuracy on real interview answers. Day 20 isn't blindly reverting
  that — it found and fixed the actual mechanism behind that harm (see the filter fix below), so
  lowering the threshold again is now safe in a way it wasn't on Day 18. Worth stating this
  explicitly in whatever comment replaces the current one, so a future reader doesn't see a lowered
  threshold and assume the Day 18 finding was forgotten.
- The segmenter needs to tell the caller whether a given closed segment ended on a real pause or a
  forced timeout (`ClosedSegment` gains a field, or equivalent) — `transcribe_segment()` needs that
  to decide whether to skip the filter below.
- `streaming_transcriber.py`'s `transcribe_filtered()` (or a variant) needs to skip
  `NO_SPEECH_PROBABILITY_THRESHOLD` specifically for forced-close segments, while keeping
  `AVG_LOGPROB_THRESHOLD` and `SILENCE_AMPLITUDE_THRESHOLD` active unconditionally — neither of
  those was the culprit Day 20 found, both still catch genuine hallucination/silence.
- **Working, verified reference implementation already exists** — `wsl_app/research/day20/
  track_a_boundary_safe_fix.py` (the segmenter-threshold change) and `track_a_diagnose_boundary_loss.py`
  (the root-cause diagnostic that explains *why*) — port the logic from there into
  `wsl_app/main.py`/`streaming_transcriber.py` properly rather than re-deriving it, but this is
  production code now: give it real docstrings and plain naming per this project's conventions,
  not a straight copy-paste of research-script style.
- One residual risk Day 20 flagged and did *not* fix, worth being aware of rather than surprised by:
  a mid-sentence forced cut can still occasionally make Whisper fabricate a short clause instead of
  dropping content (seen once, Day 20, `zira_clip` at an 8s threshold) — a milder instance of this
  project's standing "decoding a partial/boundary-aligned slice is inherently fragile" knowledge, not
  something this specific fix addresses. Not a blocker, just don't be surprised if it shows up in
  live testing.
- **Live verification required, not just a benchmark re-run** — per this project's own established
  rule, a pipeline behavior change needs real verification against actual behavior before it's done.
  Day 20's numbers are all TTS; test the real implemented change in a real running session, ideally
  using Day 19's real mic/loopback capture rather than only TTS, and specifically include at least
  one long, unbroken stretch of continuous speech (the exact scenario this whole change targets) —
  confirm text visibly appears well before the old 60s cap would have, with no dropped or fabricated
  content versus the prior whole-segment behavior.
- **Out of scope**: Track B (WhisperX) — Day 20's recommendation there was hold off, not part of
  this day's work.

**Done when:** a real session with a long, unbroken run of speech (mic or loopback, not just TTS)
shows transcript text appearing well before the old 60s cap would have, and a side-by-side check
against the prior whole-segment behavior on that same real speech shows no observable content loss
or fabrication introduced by the shorter threshold.

</details>

## Day 22 — Opt-in Session Persistence (Phase 2, part 3) ✅ implemented and live-verified

Implemented entirely in `windows_app/main.py`, exactly as scoped below: a new `SessionRecorder`
class, a "Save this session's transcript to a file" checkbox in `create_settings_panel()` (unchecked
by default, read once at Start Session), `windows_app/sessions/` added to `.gitignore`, and a
window-title indicator (`Adarna (saving session to disk)`) shown for the duration of a recorded
session.

**Live-verified against the real GUI, not just read through** — a real session needs real audio and
the real `claude` CLI, neither in scope for this change, so verification used a small scripted fake
`wsl_app` TCP server standing in for the real one (same wire protocol, sending real-shaped
`transcript`/`suggestion` messages on a timer) while driving the actual `windows_app/main.py` through
this project's established WSL→Windows interop (real Windows `python.exe`, PowerShell UI Automation
clicking Start/Stop Session for real — see `feedback_verify_interop_before_assuming_unavailable`
memory). Confirmed: with the checkbox off, a full session start/stop wrote nothing to
`windows_app/sessions/`; with it on, the window title switched to the recording indicator during the
session and reverted after Stop, and the resulting file was a complete, readable chronological log —
header (start time + mode), source-labeled/timestamped transcript lines in real arrival order, the
suggestion tagged with the question that prompted it, footer with the end time.

**Follow-up, same session, user-requested:** "You"/"Them" transcript lines are now right/left-aligned
respectively in the transcript pane (chat-bubble style), so the two sides of a conversation are
visually distinguishable without reading each line's label. A real, non-obvious finding surfaced
getting there: `QPlainTextEdit` (the pane's original widget) silently ignores `QTextCursor` per-block
paragraph alignment entirely. Confirmed directly with an isolated side-by-side test — the *exact
same* formatting code (`QTextBlockFormat.setAlignment()` + `QTextCursor.setBlockFormat()`) produces
correctly right-aligned text in a `QTextEdit`, but renders flush-left regardless of the format set in
a `QPlainTextEdit`, even though `document().findBlockByNumber(n).blockFormat().alignment()` confirms
the format *was* stored correctly — no error, no other visible sign anything was wrong, just silently
not applied at paint time. Fixed by switching only the transcript pane (not the suggestions pane,
which stays `QPlainTextEdit`) to `QTextEdit`, via a new `widget_class` parameter on
`create_readonly_text_pane()`. Worth remembering for any future per-line/per-paragraph formatting need
in this app: `QPlainTextEdit` exposes the same `QTextCursor`/block-format API as `QTextEdit` and looks
fully capable of it, but doesn't actually apply block-level formatting at render time — reach for
`QTextEdit` from the start whenever a plain-text pane needs anything beyond uniform whole-document
formatting.

<details><summary>Original Day 22 scope (for reference)</summary>

## Day 22 — Opt-in Session Persistence (Phase 2, part 3)

Scope per PRD §5/§9's privacy stance (RA 4200 anti-wiretapping consent law is the underlying reason):
off by default, transcript + suggestions only — **never raw audio**, that stays exclusively in-memory
regardless of this setting. No behavior change at all for anyone who leaves the setting alone.

**Design call, made here rather than left open:** implement this entirely on the `windows_app` side,
not `wsl_app`. `windows_app` already receives every `transcript` and `suggestion` message over the
existing socket protocol as they're generated — nothing needs to change in `wsl_app` or the wire
protocol to make the data available; this is purely "also write what's already arriving to a local
file." Keeping it Windows-side also means the saved file lands somewhere the user would actually look
for it (Windows Explorer), not buried in the WSL filesystem.

- New checkbox in `windows_app`'s settings panel (`create_settings_panel()`, alongside the existing
  mode/pause-delay/context-notes controls) — something like "Save this session's transcript to a
  file," unchecked by default. Read once at Start Session, same as mode/pause_seconds/context_notes
  (not live-toggleable mid-session — starting to persist partway through a session is a separate,
  more complex feature not asked for here).
- On Start Session, if checked: open a new local file for this session (e.g. under a
  `windows_app/sessions/` folder, filename including a timestamp so back-to-back sessions never
  collide) and write a small header (start time, mode). As `transcript` and `suggestion` messages
  arrive during the session (today these already reach `TranscriptDisplay.update()` and
  `SuggestionDisplay.update()` via `wsl_connection.transcript_received`/`suggestion_received`, wired
  up in `connect_incoming_messages_to_ui()`), also append each one to the open file, in real time
  rather than buffering everything to write at the end — a crash or force-quit mid-session shouldn't
  lose an otherwise-complete transcript.
- On Stop Session (and on disconnect/session-end via whatever path already tears a session down),
  close the file cleanly if one is open.
- Plain text is enough — no need for a structured format (JSON/markdown) unless it's genuinely no
  extra effort. A readable chronological log (source-labeled transcript lines interleaved with
  suggestions in the order they actually happened, timestamps included) is the goal; don't
  over-engineer the format for a feature whose whole point is "so I can glance back at it later."
- Add the new `sessions/` folder to `.gitignore` (real transcript content must never end up
  committed) — check whether `windows_app/` already has one or if it needs adding at the repo root.
- Worth a visible-while-active indicator that a session is being saved (e.g. something in the window
  title or near the checkbox, "Saving to <filename>") — the whole feature exists for a consent/privacy
  reason, so it shouldn't be silently invisible once turned on. Not a hard requirement of "done when"
  below, but keep it in mind; a small addition if it doesn't cost much.

**Live verification required, not just a read-through** — per this project's own established rule.
Run a real session with the setting off and confirm nothing new appears on disk; run one with it on,
speak/generate a few real transcript lines and at least one suggestion, stop the session, and actually
open the resulting file to confirm it's complete and readable — not just that a file got created.

**Done when:** with persistence off (default), nothing is written to disk, exactly as today; with it
explicitly turned on for a real session, that session's transcript and suggestions are saved locally
in a file that can be found and opened afterward, and reads as a complete, readable record of what
actually happened.

</details>

## Day 23 — Post-Meeting Summary Generation (Phase 3) ✅ implemented and live-verified

Implemented as scoped below: `generate_summary`/`summary`/`summary_failed` added to the wire
protocol, `wsl_app` generating the summary via a fresh one-shot `ClaudeCli` against a new shared
`SUMMARY_SYSTEM_PROMPT`, and `windows_app` adding a Summary pane with Generate/Save Summary buttons
sourced from `TranscriptDisplay.full_text()` — the full untrimmed session transcript, per this day's
own design call, not `wsl_app`'s trimmed suggestion context. Full implementation detail in the
collapsed original-scope section below (see its own 2026-09-09 update note).

**Live-verified against a real transcript, end to end, in the real GUI:** Jon ran a real ~30-line
transcript (a recorded team meeting — apologies, rotating minute-taker, multiple team-member status
updates, a reviewed planning document tied to appraisals, a website launch update, several workshop-
booking numbers with mixed attendance, and an any-other-business item), generated a summary from it,
and confirmed **"the saved summary is good."** The generated KEY POINTS/DECISIONS/ACTION ITEMS
correctly attributed items to the right people (Morgan/Amy/Charles), captured decisions genuinely
made in the meeting (extra Project Management workshop session, revisiting low-booking workshops next
week) rather than inventing any, and the action items list was complete against a meeting with several
different owners — a good real-world confirmation of the Day 23 design call, since this transcript
was long enough that a summary sourced from wsl_app's own trimmed rolling context (rather than
windows_app's full transcript) would very likely have dropped its early content (apologies, the first
few team updates). Closes out the one item flagged open in the 2026-09-09 update below.

<details><summary>Original Day 23 scope (for reference)</summary>

## Day 23 — Post-Meeting Summary Generation (Phase 3)

Scope per PRD §8 Phase 3: after a session ends, generate a written summary (key points, decisions,
action items) via the `claude` CLI, and let the user export it to a local markdown/txt file.

**Design call, made here rather than left open: source the summary from `windows_app`'s full
in-session transcript, not from `wsl_app`'s own rolling context window.** `MeetingSession.
recent_transcript_segments` (`wsl_app/main.py`) is deliberately a *bounded* window — trimmed by
`_trim_context_to_budget()` down to `CONTEXT_CHARACTER_BUDGET` on every new segment, sized for live
suggestion context, not a full-session record. By the time a real meeting ends, most of its early
content has already been dropped there; asking wsl_app for a summary using that state would silently
summarize only the last few minutes, not the whole meeting. `windows_app`'s `TranscriptDisplay.
_segments`, by contrast, keeps every segment for the whole session — only `reset()` clears it, at the
next Start Session — and, since Day 22, is the same complete record `SessionRecorder` already knows
how to write to disk. So: `windows_app` is the side that actually has the data this feature needs;
generation should still run through wsl_app's existing `ClaudeCli` (the only thing in this
architecture that talks to the `claude` CLI), but fed the full transcript `windows_app` sends it, not
anything wsl_app already had lying around.

- New message type `generate_summary` (`windows_app` → `wsl_app`): carries the full session
  transcript, formatted the same source-labeled, chronological way `_format_context()` already does.
  `wsl_app` runs it through `ClaudeCli.ask()` (reusing the existing CLI process and its
  `_claude_cli_lock` — not a second subprocess) against a new summary-specific system prompt (key
  points / decisions / action items) — decide during implementation whether this is one shared prompt
  or per-mode like `MEETING_SYSTEM_PROMPT`/`INTERVIEW_SYSTEM_PROMPT` already are. Sends the result
  back as a new `summary` message (`wsl_app` → `windows_app`).
- `windows_app`: a "Generate Summary" button — disabled during an active session (there's no complete
  transcript yet), enabled once one has ended and there's something to summarize, same enabled-state
  pattern Start/Stop Session already uses — plus a pane (or dialog) to show the result once it
  arrives.
- Export to markdown/txt: a "Save Summary" button/file dialog once a summary has been generated. A
  single one-shot write, not something arriving incrementally — no need to route it through
  `SessionRecorder`'s live-append machinery (Day 22), a plain file write is enough.
- Privacy, same stance as Day 22 (PRD §5/§9): the summary is a text artifact like the transcript,
  fine to save locally when the user explicitly asks, but nothing should be written automatically —
  saving still needs its own explicit action, not something that happens just because a session
  ended.

**Live verification required, not just a read-through** — same established rule as every prior day.
Run a real session with a few varied transcript lines — **varied by question/topic type, not just
varied wording** (see this project's own Day 12/18 lesson: a verification pass that only tests one
input shape can pass cleanly while a real different shape still breaks) — stop it, generate a summary,
and read it against what was actually said before trusting it's accurate; then export and open the
saved file to confirm it's complete and readable.

**Done when:** after a real session, a summary (key points/decisions/action items) can be generated on
demand from that session's actual full transcript (not a trimmed window), reviewed in-app, and
exported to a local markdown/txt file that reads as a correct, complete record of the session.

**Update, 2026-09-08:** the two real-mic live-verification items previously flagged here as open —
Day 19's mic-noise-hallucination and transcribe-concurrency fixes, and Day 21's fast-speech
segmentation change — are now resolved. Jon confirmed a real mic/loopback session works well, no
issues reported. See Day 19 and Day 21 above.

**Update, 2026-09-09 — implemented, partially live-verified:**

- `wsl_app/main.py`: new `generate_summary` handler in `handle_client()` (session-independent —
  windows_app normally sends this after a session has already ended and torn its own
  `MeetingSession` down, so it can't reuse a session's `_claude_cli`). `generate_and_send_summary()`
  spins up a fresh, one-shot `ClaudeCli` framed with a new shared `SUMMARY_SYSTEM_PROMPT` (one
  prompt for both modes, not per-mode — key points/decisions/action items fits either a meeting or
  an interview well enough that a second near-duplicate prompt didn't seem worth it), asks it with
  whatever transcript text windows_app sent, stops the process again, and replies with a `summary`
  message (or `summary_failed` with a reason — including an explicit empty-transcript guard, tested
  below). Guarded by a per-connection `summary_lock` so two rapid requests can't run two summary CLI
  processes at once.
- `windows_app/main.py`: `TranscriptDisplay.full_text()` returns the full, untrimmed, source-labeled
  session transcript (the actual per-Day-23-design-call data source — never wsl_app's trimmed
  `recent_transcript_segments`). New `SummaryDisplay` (a `QObject`, bound-method slots per this
  file's established cross-thread-signal rule) plus `create_summary_section()`/
  `create_summary_controls()` add a Summary pane with "Generate Summary" (disabled during an active
  session and while a request is in flight; also self-heals if the connection drops mid-request, so
  it can't get stuck disabled) and "Save Summary" (a plain one-shot `QFileDialog` + file write,
  defaulting into `SESSIONS_DIR` with a timestamped `.md` name — no `SessionRecorder` involvement,
  matching the design call that a summary doesn't need incremental-append machinery).
- **Live-verified (wsl_app side only):** ran `ClaudeCli`/`SUMMARY_SYSTEM_PROMPT` directly against the
  real `claude` CLI with two varied fabricated transcripts — one with a real decision + owned action
  items + a technical tangent + a garbled ASR-noise line, one with no decisions/action items at all
  (to exercise the "None recorded." fallback) — both read as accurate, complete summaries of their
  input, the garbled line was silently ignored rather than commented on, and the fallback branch
  fired correctly. Separately ran the actual wire protocol end-to-end (a real `handle_client()`
  instance on an isolated port, not the shared 8765 one — the user had a real windows_app session
  actively connected there at the time, so that instance was deliberately left untouched): a real
  `generate_summary` message produced a correct `summary` reply, and a `generate_summary` with an
  empty transcript correctly produced `summary_failed` instead of spawning a CLI process for nothing.
- **Not yet live-verified (needs a real Windows run, per this project's own established gap for
  anything GUI/OS-level — see the hotkey and combo-box entries above):** the actual Generate
  Summary/Save Summary buttons, the summary pane rendering, and opening the saved file to confirm
  it's a complete, readable record — none of this can be exercised from WSL (no PySide6 display, no
  `pyaudiowpatch`, no real session to produce a real full transcript). Needs a real session run
  end-to-end on Windows before this day is fully done-when-criteria-complete.

**Update, 2026-09-09 (later same day):** the item above is now resolved — see the live-verification
note at the top of this Day 23 section. Jon ran a real transcript through Generate Summary and Save
Summary in the actual GUI and confirmed the saved file was good.

</details>

## Day 24 — Live Mic Mute Toggle ✅ implemented and live-verified

Added 2026-09-09, Jon's request: real background noise (someone else's TV/conversation in the room)
sometimes bleeds into a real mic's noise floor mid-session, and stopping the whole session just to
avoid transcribing it was the only option. `windows_app` gained a "Mic enabled" checkbox (checked by
default), live-toggleable at any point during a running session — unchecking it immediately mutes mic
capture (`AudioCaptureManager.stop_capture()`) without touching loopback or ending the session;
re-checking it resumes capture. Also read once at Start Session, so a session can begin with the mic
already muted. Purely local to `windows_app` — no wire-protocol change, since muting just means
wsl_app stops receiving `audio_chunk` messages for that source; its `VoiceSegmenter` for mic simply
sees nothing in the meantime.

**Live-verified by Jon in the real app:** confirmed working — mid-session mute/unmute and starting a
session already muted both behaved as expected.

## Day 25 — Live-Agent-Listening Export (real, shippable second suggestion mode)

Turns the Day 18.8-18.10 research finding into an actual capability, per Jon's request after asking
what was next once Phase 3 (the last committed PRD phase) finished with Day 23. Days 18.8-18.10 proved
an interactive `claude` terminal session — watching a background task's stdout via the Monitor tool —
can react to transcript segments as cheap notifications and produce a real, grounded answer only when
triggered, with near-zero added latency at that point since the conversation is already live in the
agent's own context. That was only ever proven against synthetic WAV playback harnesses
(`wsl_app/research/day18_8/`, `day18_10/`), bypassing the real production pipeline entirely.

**Two design calls confirmed with Jon before building:**
1. **Data feed**: a new, dedicated live-export log purpose-built for this mode, not a reuse of Day
   22's opt-in session-recording feature — that file has no clean "decide now" signal, and conflates
   two conceptually different opt-in checkboxes.
2. **Answer surface**: the live agent's real answers stay in its own separate terminal only — no
   attempt to route them back into `windows_app`'s GUI/overlay, which would add a new hop and work
   against the mechanism's whole appeal.

**Implemented:**
- `wsl_app/main.py`: new `LiveAgentLog` class (mirrors `SessionRecorder`'s safe-no-op/flush-per-write
  idiom), writing a header (mode, auto_suggest state, suggestion_pause, context notes), one `SEGMENT`
  line per transcribed segment (`SOURCE_LABELS`-labeled, matching `_format_context()`'s own "You:"/
  "Them:" shape), one `TRIGGER` line every time the *existing* `SuggestionTrigger` mechanism actually
  fires (piggybacked on the shipped trigger, not a new independent cadence), and a footer on close.
  Wired into `MeetingSession` as the last constructor statement (after `ClaudeCli`, the one call there
  that can raise — avoids leaking an opened file if construction fails partway through) and a new
  `live_agent_export_enabled` settings field. Console prints the log's path when a session starts with
  it enabled.
- `windows_app/main.py`: new "Enable live-agent-listening export" checkbox (Session Settings, unchecked
  by default, read once at Start Session, crosses the wire via `settings_changed` — unlike Day 22's
  transcript checkbox, this feature lives on the wsl_app side). Window-title indicator generalized from
  a single `RECORDING_WINDOW_TITLE` constant to `session_recording_window_title()`, composing the title
  from whichever disk-writing opt-ins (this one, Day 22's) are actually active.
- New permanent runbook: `docs/LIVE_AGENT_LISTENING.md` — exactly how to point a second, manually-
  started interactive `claude` session at the real log for a real meeting, using the same Bash-
  background-task + Monitor pattern already proven, including the operating-instructions text to give
  that session (there's no `--system-prompt` flag for an interactive session).
- `wsl_app/live_agent_logs/` gitignored (real conversation content).

**Live-verified (wsl_app-side plumbing only)**: a real wire-protocol test (isolated port, not the
shared one) — checkbox off produced zero file/directory activity; checkbox on, with real WAV audio
streamed through the actual production pipeline (segmentation → transcription → the real
`SuggestionTrigger`), produced a correctly-formed header + context notes, `SEGMENT` lines with accurate
transcribed text and correct `You:`/`Them:` labels, `TRIGGER` lines at genuine suggestion-fire moments,
and a clean footer on `session_stopped`.

**Not yet live-verified — needs Jon's own hands, per this project's established "Jon runs this
directly" pattern (Day 18.9):** the actual `windows_app` checkbox/title behavior on real Windows, and
— the real point of this whole feature — a second, separate interactive `claude` session genuinely
tailing the real log during a real (or real-mic) meeting and producing useful answers, hands-off, the
way Day 18.8-18.10 proved for synthetic audio.

**Done when:** a real meeting, run with the export checkbox on, produces a correctly-formed live log;
a separately-started interactive `claude` session, following docs/LIVE_AGENT_LISTENING.md, tails it and
reacts to real `SEGMENT`/`TRIGGER` lines with no manual intervention during the meeting; and its
answers are genuinely useful, not just mechanically present.

**Update, 2026-09-09 (later same day) — the manual second-terminal setup automated down to one
command.** Jon flagged the original step-by-step (start `claude`, tell it to background a `tail -f`
of a console-printed path and attach Monitor, then paste it a paragraph of operating instructions)
as tedious. Fixed:
- `LIVE_AGENT_LOG_PATH`: the log is now a single fixed filename
  (`wsl_app/live_agent_logs/live_agent_current.log`, truncated and overwritten fresh each session)
  instead of one generated per session — a deliberate trade confirmed with Jon (losing a
  session-by-session history of past logs, which this live-tail-only feature never actually needed;
  `SessionRecorder`, Day 22, already covers "keep a record for later"), in exchange for a path a
  script can hardcode instead of a human copying a fresh one out of `wsl_app`'s console every time.
- New `start_live_agent_listening.sh` (repo root, mirrors `start_app.sh`'s style/doc-comment
  convention): one command, run in the second terminal, that starts
  `claude --permission-mode bypassPermissions` with `docs/live_agent_listening_prompt.txt` as its
  opening prompt.
- New `docs/live_agent_listening_prompt.txt`: the operating instructions (background a `tail -F`
  --capital F, so it retries if the session hasn't started writing the file yet-- of the fixed log
  path, attach Monitor, then the same SEGMENT/TRIGGER reaction rules docs/LIVE_AGENT_LISTENING.md
  already documented) as its own file, so the launcher script and a manual fallback both stay in
  sync with one source of truth instead of two copies of the same text drifting apart.
- `docs/LIVE_AGENT_LISTENING.md` updated: the 5-step manual dance is now "run one script," with the
  old manual sequence kept as an explicit fallback for anyone who'd rather drive it by hand.

Verified statically: `claude --help` confirms `--permission-mode bypassPermissions` and the
`claude [options] [prompt]` positional-argument shape the script relies on are both real, current
CLI flags — not just assumed from Day 18.9's own precedent. Not re-run against a real meeting yet;
the "needs Jon's own hands" item above is unchanged, just less tedious to actually go do now.

## Day 26 — Manual-Trigger Latency Fix (stale-context suggestions) ✅ implemented and live-verified

Real bug Jon found testing Day 25 live: pressing the hotkey/"Generate Suggestion Now" within a second
or two of a question finishing sometimes builds a suggestion missing that exact question. Root cause,
confirmed by tracing the real pipeline rather than guessed: a manual trigger fires `_generate_and_
send_suggestion()` instantly, but `_format_context()` only ever sees segments already in `recent_
transcript_segments` — and a segment only lands there after (a) `VoiceSegmenter` sees a real
`SILENCE_SECONDS_TO_CLOSE_SEGMENT` (0.4s) pause to close it, then (b) a full Whisper decode (2-4s+,
this project's own established floor). So the realistic gap between "question finished" and "its text
exists anywhere in the system" is several seconds — clicking inside that window means the segment's
text hasn't been transcribed yet, not that a faster/"rawer" form of it was available and skipped (Jon
asked specifically whether the raw pre-transcription Whisper output could be used instead — answered
directly: there isn't a faster form to reach for, Whisper's text output is the earliest point "what was
said" exists as text this text-only `claude` CLI architecture can use at all).

**Fix, not a redesign**: only the manual trigger path needed anything — the auto-suggest pause-timer
path is already immune by construction, since `notify_new_segment()` (which starts its countdown) is
only ever called *after* a segment is already fully transcribed. New `MeetingSession.
has_pending_transcription_work()` (a segment still open on either source, via new `VoiceSegmenter.
has_open_segment()`, OR a new per-session `_pending_transcriptions` counter, incremented/decremented
around every background transcribe task in `transcribe_segment_and_report`) tells `notify_hotkey_
pressed()`'s call into `_generate_and_send_suggestion(wait_for_pending_segments=True)` to wait —
polling, bounded at `MANUAL_TRIGGER_MAX_WAIT_SECONDS` (5.0s) so an unusual stuck case can't hang the
button — before building context, instead of generating on stale content. Deliberately waits for the
segment to close and transcribe *naturally* rather than force-cutting it early (which would just trade
"missing the question" for "truncating the question" mid-sentence). Sends one placeholder `suggestion`
message ("Still capturing the last few words…") the moment it starts waiting, reusing the exact
existing message shape/overwrite behavior `SuggestionDisplay` already has — no protocol or `windows_app`
change needed for that part.

**Live-verified** with two targeted wire-protocol tests against the real production pipeline (real WAV
audio, real segmentation, real Whisper transcription): (1) hotkey pressed 0.5s into a still-being-spoken
sentence — placeholder arrived instantly, no suggestion at all followed within the wait window (this
was the very first segment of the session, so once the wait's cap was hit, `_format_context()` was
still empty and it correctly no-op'd, same as pre-fix behavior for "nothing transcribed yet" — not a
regression); (2) hotkey pressed 6.5s in, timed so the pending segment resolves *within* the 5s cap —
placeholder arrived instantly, then a real suggestion followed once the segment closed and transcribed,
with its `question` field correctly containing that exact just-finished sentence, confirming the fix's
actual goal.

Also fixed same session, prompt-only: the live-agent-listening prompt (`docs/live_agent_listening_prompt.txt`)
was narrating "(silently noting the segments, no output — waiting for TRIGGER)" on every SEGMENT
notification despite already being told not to acknowledge them — the model explaining its own restraint
instead of actually staying silent. Tightened to explicitly require zero text output, naming that exact
line as the failure mode not to repeat.
