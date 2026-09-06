# Day 18.8 — Live-Agent-Listening: Feasibility Experiment (Phase 1 of 2)

Research only, nothing wired into the running app. `wsl_app/main.py` is unchanged (imported
read-only by the harness below). See `docs/DEV_PLAN.md` (Day 18.8) and `project_notes.md` for the
full writeup this README backs up.

## The idea, and how it differs from Day 18.7

Day 18.7 tested and rejected feeding transcript segments into the scripted, headless `claude -p`
subprocess `ClaudeCli` wraps — every turn forces a full paid generation there, no exceptions, so
"native turn history at near-zero cost" isn't possible through that interface.

Jon's follow-up idea is different in kind, not a retry of the same thing: instead of a scripted
subprocess, keep an actual **interactive agent session** (a live Claude Code conversation) attached
to a real meeting, watching the transcript stream in continuously and reasoning about it, the same
way an agent naturally did during real manual testing of this app in earlier days. An interactive
session isn't bound by the headless CLI's "every turn must return a full result" protocol — it can
react to most incoming segments with something trivial and only produce a real, elaborated answer
when actually triggered, which is exactly the behavior Day 18.7 couldn't get out of `claude -p`.

## What's here

- `live_listen_harness.py` — feeds a real WAV clip through the **actual, unmodified production
  pipeline** (`main.VoiceSegmenter`, `main.transcribe_segment`) in real-time-paced 100ms chunks
  (matching how audio actually arrives from `windows_app`), so segments land at genuine
  VAD-driven boundaries with genuine transcription content and genuine timing — not a pre-computed
  transcript dumped instantly. Each closed segment, and a synthetic trigger line once the same real
  pause window `SuggestionTrigger` uses elapses, is printed the instant it's ready.

This script only produces the live event stream — it does not call any LLM itself. The point of the
experiment is that a live **agent session** is the thing reasoning about the stream, not a
subprocess this script spawns.

## Phase 1 test: self-contained, real-time-paced, no live app needed

Ran the harness on `research/day16/en_normal.wav` (45.8s, a technical 3-phase database migration
explanation) via the Monitor tool, so each `SEGMENT`/`TRIGGER` line arrived as a live notification
to the agent session in real time, exactly as it would from a real meeting. The agent reacted to
each segment with a short acknowledgment only, then composed the real suggestion from scratch,
using nothing but what it had absorbed live, the moment the trigger line arrived.

**Real transcription errors showed up exactly as expected, and were silently recovered from context
without needing to be flagged:**
- "existing **red** paths" → read paths (homophone, same class as Day 18.5's read/red finding)
- "cut over the **right** path" → write path (homophone)
- "start **dual-riding**" → dual-writing (mishearing)

**The suggestion produced live, verbatim:**

> Solid three-phase plan — the sequencing (parallel schema, overnight backfill, verified cutover
> with a dual-write buffer) is a sound, low-risk shape. The real gap worth raising out loud is
> verification rigor during the backfill window itself.
>
> - Row-count monitoring: push for *continuous* watching during the backfill, not just a final
>   checksum match — a schema change mid-migration could drop rows on both sides in a way a
>   checksum comparison alone might not catch.
> - Dual-write duration: ask whether the one-week window is sized to real traffic cycles (e.g. an
>   end-of-month batch job) or just a round number — cutting it short risks retiring the old table
>   before a rare workflow ever exercises the new path.
> - Schema-change guardrail: propose a lock or alert on the source table's schema specifically
>   during the backfill window, turning "the source table gets a schema change mid-migration" into
>   a caught event instead of a silent-drop risk.
> - Ownership: "someone watching the row counts" needs a name attached now, not left implicit.

Grounded, specific, and correctly built on the *corrected* meaning of all three garbled words,
without any of them needing to be explicitly flagged or resolved first.

## Real findings

**Latency: effectively zero at trigger time**, strictly better than either alternative design.
By the time the trigger notification arrived, the agent already held the full context — no
subprocess spin-up, no separate API round-trip beyond reasoning already in flight. Compare: today's
`ClaudeCli` costs ~5-8s per suggestion call; Day 18.7's rejected native-turn design cost ~41.5s
across 7 real calls for a comparable amount of content. This design has neither cost, because
nothing is being called at trigger time — the answer is already assembled from context the agent
already holds.

**Real cost is scale, not per-turn price.** Each segment notification is a real turn in the agent's
own session — cheap individually (a short ack), but a 46-second clip alone produced ~7 turns. A real
30-60 minute meeting would mean dozens to hundreds of turns across the session's lifetime. This
also only works while an interactive agent session is actively attached and watching — it is not
something `wsl_app` can run unattended the way `ClaudeCli` does today. This is the real
architectural tradeoff, not a per-response cost problem: it's a different deployment model, not a
drop-in replacement for `ClaudeCli`.

## Open: Phase 2, needs a real live session

Phase 1 used a real audio clip through the real segmenting/transcription code, but the pacing and
the trigger were still scripted (a `time.sleep()` loop, a synthetic trigger line) rather than a
genuine live meeting with a real hotkey press. The next real test needs Jon to run an actual session
— real `windows_app` + `wsl_app` connected, real audio — while the agent tails the real transcript
output live (the same mechanism, pointed at the real running app instead of the harness), to confirm
this holds up over a real meeting's length and with a real trigger instead of a scripted one. Not
run yet — this is the next step, not a finding.
