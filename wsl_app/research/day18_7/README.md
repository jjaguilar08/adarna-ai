# Day 18.7 — Continuous Context Feed: Feasibility Spike

Research only, nothing wired into the running app. `wsl_app/main.py` is unchanged. See
`docs/DEV_PLAN.md` (Day 18.7) for the full task framing this README backs up.

## What's here

- `probe_silent_turns.py` — raw stream-json probe: writes 3 `"user"` lines to a real `claude`
  process's stdin back-to-back with no reads in between, then reads everything that comes back.
- `probe_sequential_turns.py` — the realistic version: uses the actual production `ClaudeCli`
  class (`main.ClaudeCli`) and calls `ask()` once per segment, waiting for each reply, exactly the
  call pattern any real usage would follow.
- `get_real_transcript.py` — regenerates a real small.en transcript of
  `research/day18_6/fast_long.wav` using the exact production `transcribe_filtered()` call, so
  this spike has real, current, baked-in transcription errors to test with (day18_6 printed its
  own run's transcript but didn't save it to a file).
- `fast_long_real_transcript.txt` — that regenerated transcript.
- `accuracy_and_latency_comparison.py` — real side-by-side test: today's rolling-window one-shot
  prompt vs. the proposed accumulated-native-turn-history design, on the same real garbled
  transcript text, with real wall-clock timing.
- `hallucination_still_a_problem.py` — deliberately re-tests the one failure mode the proposal
  doesn't fix, using the real, previously-documented Day 18 fabrication text.

## Question 1: can the CLI ingest a turn purely as context, without forcing a reply?

**No — confirmed empirically, twice, not assumed from docs.** `claude -p --help`'s full flag list
has nothing resembling a silent/context-only turn (`--replay-user-messages` just echoes the input
back for acknowledgment, it doesn't suppress generation).

- `probe_silent_turns.py`: sent 3 user lines before reading anything back. Every line that got
  processed produced a full `"assistant"` message and a `"result"` line — 2 of 3 completed within
  the test window (the burst-write pattern isn't one production ever uses; the 3rd may just need
  more real time, not evidence of anything different).
- `probe_sequential_turns.py` (the realistic pattern — `ask()` per segment, wait for the reply,
  exactly as `ClaudeCli.ask()` already documents): **all 4 of 4 segments sent got a full real
  reply**, 2.87s–4.26s each, no exceptions:

  ```
  Turn 0: sent 'Segment 1: the weather today is sunny.'
    -> got REAL reply in 4.26s: "It sounds like this snippet is just small talk..."
  Turn 1: sent 'Segment 2: I went for a walk in the park.'
    -> got REAL reply in 2.92s: "This still reads as casual small talk..."
  ...
  ```

**Every `ask()` call forces a full generation, with no exception found.** The task's own fallback
idea — "batching several segments into one silent turn before triggering a real response" — turns
out, on inspection, to not be a new mechanism at all: if you must accumulate segments as plain text
and only send when you want a real reply, that's exactly today's rolling-window design (paste
accumulated text, get one real reply), just potentially with a bigger window. There's no way to get
segments into the model's own native turn history without paying for a real generation on each one.
This forecloses the core premise of the proposal as described.

Minor, non-central oddity noticed in the raw-burst probe: `--tools ""` was honored on the first
turn's `system`/`init` line (`tools_count=0`) but not the second (`tools_count=29`, MCP tools
included) in that specific write-3-before-reading-any pattern. Not investigated further since
production never uses that pattern (`ClaudeCli.ask()` always reads before writing again) — flagged
here in case it matters for something else later, not a finding this spike depended on.

## Question 3 + 5: real accuracy and latency comparison

Real small.en transcription of `research/day18_6/fast_long.wav` (regenerated via
`get_real_transcript.py`, exact production `transcribe_filtered()` call) contains three genuine
substitution errors on key content words:

- "when one of **it. 3** inventory replicas got slow" (should be "one of the **three**...")
- "a **feature flat** rollout for the new pricing engine" (should be "the **feature flag**
  rollout")
- "the **uncalled** handoff notes from last night" (should be "the **on-call** handoff notes")

The 39.5s clip was recorded as one continuous utterance (day18_6 confirmed it never actually
segments), so for this test it was split at clause boundaries into 6 simulated meeting utterances
— **the words and errors are 100% real model output; only the chunk boundaries are a simulation**,
needed to give the turn-by-turn design something to accumulate.

Three conditions, same `MEETING_SYSTEM_PROMPT`, same real `ClaudeCli`:

| condition | calls | real wall-clock | got "flag" right | got "on-call"/"3 replicas" right |
|---|---|---|---|---|
| rolling window, all 6 segments (today, fits under `SEGMENTS_TO_KEEP_FOR_CONTEXT`=10) | 1 | 7.59s | **yes** ("who owns the pricing engine **flag**") | no |
| rolling window, last 2 segments only (simulates a longer meeting where earlier context already fell out of the window) | 1 | 4.88s | **yes** | no |
| accumulated native turns (6 segments fed as turns + 1 nudge trigger) | 7 | **41.50s** | **no** (never says "flag", talks around it as "pricing rollout status check") | no |

**No accuracy advantage found for the accumulated design in this real test — if anything, both
rolling-window variants correctly disambiguated the clearest test term where the accumulated
design's own final answer did not.** None of the three approaches restated "on-call" or "three
replicas" correctly in their own wording, but that's inconclusive either way — a suggestion isn't
required to repeat back the interviewer/speaker's jargon verbatim, so silence on a term isn't proof
of misunderstanding for any of the three.

**Latency: ~6-8x more real compute for the accumulated design, for the exact same underlying
content** — 41.50s of real CLI time across 7 calls vs. 4.88-7.59s for a single rolling-window call.
This is not a one-time cost that amortizes: every additional transcript segment in a real meeting
adds one more forced real generation before the trigger nudge even runs, so the gap widens the
longer a meeting goes, not narrows.

## Question 4: does more context fix a fluent-but-wrong transcription? (No, and this spike's own accuracy test already shows it)

Used the real, previously-documented Day 18 fabrication text (`CLAUDE.md`'s avg_logprob
hallucination entry): `"It is not just one of my favorite damp gases in the face."`, appended as a
final segment after 3 real segments, tested against both approaches.

Neither approach's final, user-facing answer showed contamination from the fabricated sentence —
both just quietly built their answer from the real surrounding content and left it out. One
genuinely interesting wrinkle: in the accumulated design, the fabricated segment's own (discarded)
turn reply explicitly flagged it —

> "That line doesn't parse as real meeting content — it reads like a garbled transcription artifact
> rather than something anyone actually said... don't build a response off the literal text"

— but that recognition never reaches the user, since the final trigger turn's own answer also just
silently drops the sentence without telling the user anything was garbled. So even where the model
internally "notices," today's design (discard every turn but the last) throws that signal away
either way.

**The important caveat the task asked not to gloss over, and it holds:** this specific fabrication
is easy for any single call to implicitly set aside because it's *completely* disconnected from the
surrounding topic — that's typical of Whisper's classic hallucination filler. The genuinely
dangerous class is a **fluent, on-topic, plausible-but-wrong substitution**, and this spike didn't
have to construct one artificially — the accuracy test above already has three real ones ("feature
flat rollout", "the uncalled handoff notes", "one of it. 3 inventory replicas"). Neither approach
showed any sign of suspicion about those; both silently treated the garbled words as correct and
built fluent answers on top of them. **This spike's own accuracy comparison is a live demonstration
that the exact failure mode Day 18's `no_speech_prob`/`avg_logprob` filters exist for is not
touched by adding more context** — those filters catch it at the transcription layer, which is
still the only real defense.

## Question 2: recycling redesign

Moot for the literal design, since question 1 already rules it out — but worked through anyway,
because it's informative about why this doesn't work even in principle. If every segment must be a
real turn, the recycle counter fills far faster: this 6-segment/1-suggestion slice alone burned 7
real `ask()` calls against `ASK_CALLS_BEFORE_RECYCLING_CLAUDE_CLI` (10), vs. 1 today — at that rate
`_count_ask_call_and_recycle_if_due` would fire roughly every 1-2 suggestion-worthy pauses instead
of every 10, a 5-10x higher recycle rate. Worse, `_restart_claude_cli()` today just spins up a fresh
`ClaudeCli(self._system_prompt)` with nothing carried over — exactly correct for today's design,
since `recent_transcript_segments` lives in `MeetingSession`, independent of the CLI process, by
design (see the class's own docstring). A native-turn-history design has no equivalent: recycling
it would repeatedly destroy the entire accumulated meeting context the feature exists to build, far
more often than today's design ever loses anything. The only way to preserve context across a
recycle would be to re-seed the fresh process with a text summary of everything so far — which
folds back into paying for a rolling-window-style text paste anyway, undermining the reason to
build this in the first place. There's no clean recycling design here; it's another symptom of the
same underlying problem, not a separate one to solve.

## Recommendation: hold off, don't build

Every real check came back against this design, not just inconclusive:

1. **The literal mechanism doesn't exist.** No CLI flag or turn type ingests context without a
   full, paid generation — confirmed against the real CLI, not assumed.
2. **The stated fallback isn't a different mechanism** — batching segments into one turn before
   triggering a reply is just today's rolling-window design with a bigger window.
3. **Real measured cost is ~6-8x higher** for equivalent suggestion output on a real 6-segment test,
   and gets worse the longer a real meeting runs, not better.
4. **No accuracy benefit found** in a real side-by-side test with genuine transcription errors — if
   anything, today's rolling window got the one clearly-testable disambiguation right where the
   accumulated design's final answer did not.
5. **The hallucination failure mode is untouched**, exactly as anticipated, and this spike's own
   accuracy test is itself a live example of it happening unnoticed by both approaches.
6. **No sensible recycling design exists for it** — the mechanism structurally conflicts with the
   need to periodically restart the process, which would repeatedly destroy the very thing it
   builds.

**Not implementing the continuous context feed.** If the real underlying concern (a long meeting
losing early context that would explain a later garbled term) is still worth addressing, the
"truncated window" row above points at a much cheaper real lever: `SEGMENTS_TO_KEEP_FOR_CONTEXT` is
currently a fixed count (10), not a token/character budget, and including more real history in a
single already-happening call is nearly free (marginal prompt tokens only, no extra generation) —
raising that cap, or switching it to a size-based budget so it scales safely, is worth a much
smaller future look if this specific truncation scenario turns out to matter in real use. That's a
different, far simpler change than anything tested in this spike.
