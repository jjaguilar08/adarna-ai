# Day 18.10 — Live-Agent-Listening: Real-Duration Unattended Test (Phase 1 — prep)

Research only, nothing wired into the running app. `wsl_app/main.py` is unchanged (imported
read-only by the harness below). See `docs/DEV_PLAN.md` (Day 18.10) and `project_notes.md` for
the full writeup this README backs up.

## What this answers

Day 18.9 confirmed the free-background-notification mechanism (a background task whose
SEGMENT/TRIGGER output arrives as live notifications, no polling, no human relay) works fully
unattended in a bare terminal `claude` session — but only on the same ~46s clip Day 18.8 used.
That leaves the actual bar for calling this a viable second mode of the app untested: does it
hold up over something closer to a real 30-60 minute meeting? A 46-second run can't exercise
context growth over many turns, subscription rate-limit exposure (PRD §10's "chatty tool" risk),
or long idle/silent stretches (a normal part of real meetings) — this harness is built to
exercise all three, on purpose, in one run.

## What's here

- `live_listen_harness_long.py` — the real-duration harness. Run it, then have a live agent
  session tail its log the same way Day 18.8/18.9 did.

## Shape chosen, and why

**No new audio was recorded.** The 5 existing synthetic test clips (`research/day16/en_normal.wav`,
`en_fast.wav`, `taglish.wav`, `research/day18_5/zira_clip.wav`, `research/day18_6/fast_long.wav`)
are played back-to-back as one ~193s "cycle" (their durations plus short inter-clip gaps), and
the cycle repeats 9 times. This gets real variety essentially for free — two SAPI voices,
clean and fast English speech, Taglish code-switched vocabulary — rather than looping one clip
20+ times, which would make any "did quality degrade" read harder to trust (identical content
repeating wouldn't distinguish real degradation from just seeing the same thing again).

**A short gap (3.0s) separates every clip**, deliberately longer than
`PAUSE_SECONDS_BEFORE_SUGGESTION` (1.2s), so a real trigger reliably fires and settles between
clips instead of chaining straight into the next clip's speech mid-pause-timer.

**After cycles 3, 6, and 9, a 5-minute stretch of pure silence** stands in for a real meeting's
quiet periods, instead of the short inter-clip gap — spread through the run (roughly a third,
two-thirds, and the very end) rather than bunched together, so idle-during-a-meeting and
idle-as-the-meeting-winds-down are both represented.

**Total: ~44 real minutes** (9 × ~193s ≈ 29 minutes of clip/gap content, plus 3 × 5 minutes =
15 minutes of deliberate silence), comfortably inside the 30-60 minute target, entirely real-time
paced (this genuinely takes 44 minutes of wall-clock time to run, same as a real meeting would).

**A real `SuggestionTrigger`, not a scripted one-off.** Day 18.8's harness only ever printed a
single synthetic `TRIGGER` line, by hand, after all its audio was done — fine for a 46-second
proof of concept, but not representative of how many times a real meeting would actually
trigger a suggestion. This harness instead runs a real `asyncio` event loop and imports the
actual, unmodified `main.SuggestionTrigger` class, feeding it real `notify_new_segment()` calls
exactly as `main.py`'s own `add_transcript_segment()` does. Its pause-timer logic (real
`asyncio.sleep`, not a stand-in) is what decides when each `TRIGGER` line fires — this is what
makes the harness's turn-count estimate a real one instead of a guess.

**A heartbeat every 60 seconds, independent of everything else.** Without it, a deliberate
5-minute silent stretch — which is *supposed* to produce no `SEGMENT`/`TRIGGER` lines — would
look identical after the fact to a real stall: several minutes with nothing in the log either
way. A `HEARTBEAT` line that keeps appearing on schedule straight through an idle stretch is
how a reviewer tells "working correctly through a quiet period" apart from "hung", without
having watched it live.

**A `CYCLE_END` summary line after every cycle** (segment count, empty-segment count, trigger
count, average transcribe latency for just that cycle) — lets a reviewer compare cycle 1's line
against cycle 9's directly for the degradation question, instead of eyeballing 40+ minutes of
raw `SEGMENT` lines by hand.

**A final `SUMMARY` block** with the real total notification count (`SEGMENT` + `TRIGGER` lines
only — `SEGMENT_EMPTY` is never sent to a real session in production either, see
`transcribe_segment_and_report`'s `if text:` guard in `main.py`), first-10-vs-last-10 transcribe
latency (the degradation check), and a running error count.

**Errors are caught per-segment, not fatal.** One bad `transcribe()` call logs an `ERROR` line
and is counted, but doesn't end a run that might otherwise have 30+ minutes left to go. Anything
that still escapes uncaught is logged as `HARNESS_CRASHED` with its traceback and the process
exits non-zero, so a crash is never silently indistinguishable from a clean finish.

## Smoke-test verification (done before handing this off)

Ran `python research/day18_10/live_listen_harness_long.py --smoke-test` — a ~70-second shrunken
version (2 clips, 1 cycle, a 5s idle stretch) that exercises every code path (clip playback,
inter-clip gap, trigger firing, idle stretch, cycle boundary, heartbeat, final summary) without
committing to the full 44-minute run. Real result, unattended, exit code 0:

- 6 real segments transcribed, 6 triggers fired (roughly one trigger per sentence-length pause,
  matching Day 18.8's own ~7-turns-per-46s-clip finding almost exactly — `en_normal.wav` alone
  produced 5 segments and 5 triggers here, same as Day 18.8's Phase 1 run of the same clip).
- `HEARTBEAT` printed correctly during the idle stretch.
- `CYCLE_END #1` and the final `SUMMARY` block both printed the correct counts.
- Zero errors, zero crashes.

This confirms the harness's own mechanics (not the real 30-60 minute question, which only a
real run can answer) work correctly before Jon commits 44 minutes of wall-clock time to it.

## How to run the real test (Jon, same shape as Day 18.9)

1. Open a plain terminal (no IDE) and start `claude --permission-mode bypassPermissions`
   pointed at `/home/jon/projects/adarna-ai`, the same as Day 18.9.
2. Paste a self-contained prompt describing the check: background
   `wsl_app/research/day18_10/live_listen_harness_long.py` (no `--smoke-test` flag, for the
   real ~44-minute run), redirecting stdout to a log file, and attach a persistent Monitor/tail
   on that log the same way Day 18.9's session did.
3. Genuinely walk away for the real duration — don't watch, don't type anything, don't check in
   partway through.
4. Come back after and read the log for:
   1. Whether `SEGMENT`/`TRIGGER` activity and `CYCLE_END` quality held up at the end of the run
      the same as at the start (compare early vs. late `CYCLE_END` lines, and the `SUMMARY`
      block's first-10-vs-last-10 latency comparison).
   2. The `SUMMARY` block's `total_notifications_estimate` — the real turn-count number for
      what an equivalent real meeting would cost, for PRD §10's rate-limit question.
   3. Any `ERROR` or `HARNESS_CRASHED` lines, and whether the process exited 0.
   4. How the log reads across the three `IDLE_START`/`IDLE_END` stretches — confirm
      `HEARTBEAT` kept appearing on schedule and nothing wasteful or broken happened while idle.

**Not run here** — the real 30-60 minute run is Jon's manual step, matching Day 18.9's pattern;
this phase only built and verified the harness.
