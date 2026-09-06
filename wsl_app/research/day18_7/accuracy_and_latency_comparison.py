"""
Day 18.7, questions 3 and 5: real side-by-side test of today's rolling-window
one-shot prompt vs the proposed "feed every segment as its own turn, then
trigger with a tiny nudge" design -- using real small.en transcription
errors (see get_real_transcript.py, run against research/day18_6/
fast_long.wav with the exact production transcribe_filtered() call), not
invented ones.

The real transcript contains three genuine small.en substitution errors on
key content words:
  - "one of it. 3 inventory replicas" (should be "one of the three...")
  - "a feature flat rollout"          (should be "the feature flag rollout")
  - "the uncalled handoff notes"      (should be "the on-call handoff notes")

The 39.5s clip was one continuous utterance in real life (see day18_6), so
it was never actually split into multiple VoiceSegmenter segments -- the
6-way split below is a deliberate simulation of "this content arrived as
several separate meeting utterances over time," used only to give the
turn-by-turn design something to accumulate. The WORDS AND ERRORS in each
chunk are 100% real model output; only the chunk boundaries are simulated.

Two comparisons, both using the real ClaudeCli class and MEETING_SYSTEM_PROMPT:

1. "Fair window" -- today's approach gets all 6 segments pasted in one
   prompt (fits comfortably under production's SEGMENTS_TO_KEEP_FOR_CONTEXT
   = 10, so this isolates "does moving context into the CLI's own native
   turn history change answer quality" from "does a bigger window help").
2. "Truncated window" -- today's approach only gets the LAST 2 segments,
   simulating a longer real meeting where the earlier context that would
   have explained "feature flag rollout" has already fallen out of the
   rolling window. This is the scenario the proposed design is actually
   meant to help with.

Both comparisons also report real wall-clock time for each approach's full
sequence of ask() calls, since the CLI's per-call latency plus the number of
calls needed is the real cost of each design -- see also probe_sequential_
turns.py, which already established every ask() call forces a full
generation (no free/silent turns).

Standalone research script, run directly:
    python research/day18_7/accuracy_and_latency_comparison.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from main import MEETING_SYSTEM_PROMPT, ClaudeCli

# Real small.en output on fast_long.wav (see fast_long_real_transcript.txt),
# split at natural clause boundaries into 6 simulated meeting utterances.
SEGMENTS = [
    "Ok so quick context dump before the call ends because I know we're "
    "almost out of time,",

    "the deploy this morning went fine on the payment service but right "
    "after that we saw the checkout latency spike from around 90ms up to "
    "almost 450ms for about 6 minutes before it settled back down,",

    "and when we dug into the traces it looked like the connection pool to "
    "the inventory service was getting exhausted because a retry storm "
    "kicked in when one of it. 3 inventory replicas got slow,",

    "and the circuit breaker config we have right now waits way too long "
    "before it actually trips, something like 30 consecutive failures, so "
    "by the time it opened the damage was already done,",

    "and separately I noticed a feature flat rollout for the new pricing "
    "engine is still sitting at 10% even though the ticket said we should "
    "be at 50% by end of day yesterday, so somebody needs to bump that or "
    "tell me why it's intentionally paused,",

    "and... Also, the uncalled handoff notes from last night mentioned a "
    "flaky test in the shipping calculator suite that's been failing "
    "intermittently for like a week now and nobody has actually looked at "
    "the root cause yet, they just keep rerunning the pipeline until it "
    "goes green, which is obviously not a real fix, so I'd like to get "
    "that assigned to someone today if at all possible before it actually "
    "breaks something in production.",
]

NUDGE = "Based on everything said so far, suggest how the user could respond to what's just been discussed."


def ask_rolling_window(segments_to_include, label):
    """Today's production pattern: one fresh-ish ask() with all included segments pasted as one prompt."""
    claude_cli = ClaudeCli(MEETING_SYSTEM_PROMPT)
    try:
        prompt_text = " ".join(segments_to_include)
        started = time.monotonic()
        answer = claude_cli.ask(prompt_text)
        elapsed = time.monotonic() - started
        print(f"\n--- {label}: rolling-window prompt ({len(segments_to_include)} segments) ---")
        print(f"prompt sent: {prompt_text!r}")
        print(f"1 real call, {elapsed:.2f}s total")
        print(f"answer: {answer!r}")
        return answer, elapsed, 1
    finally:
        claude_cli.stop()


def ask_accumulated_turns(segments):
    """Proposed design: feed every segment as its own turn (real reply discarded each time), then a tiny nudge."""
    claude_cli = ClaudeCli(MEETING_SYSTEM_PROMPT)
    try:
        total_elapsed = 0.0
        call_count = 0
        print(f"\n--- Accumulated turn history: feeding {len(segments)} segments one at a time ---")
        for i, segment in enumerate(segments):
            started = time.monotonic()
            discarded_reply = claude_cli.ask(segment)
            elapsed = time.monotonic() - started
            total_elapsed += elapsed
            call_count += 1
            print(f"  turn {i} ({elapsed:.2f}s, discarded): sent {segment[:60]!r}... "
                  f"-> got {discarded_reply[:60]!r}...")
        started = time.monotonic()
        answer = claude_cli.ask(NUDGE)
        elapsed = time.monotonic() - started
        total_elapsed += elapsed
        call_count += 1
        print(f"  trigger turn ({elapsed:.2f}s): sent nudge {NUDGE!r}")
        print(f"{call_count} real calls, {total_elapsed:.2f}s total")
        print(f"answer: {answer!r}")
        return answer, total_elapsed, call_count
    finally:
        claude_cli.stop()


def check_terms(label, answer):
    """Reports whether the answer's own wording suggests it correctly inferred the garbled key terms."""
    lower = answer.lower()
    print(f"\n{label} — term check:")
    print(f"  mentions 'flag' (correct term, garbled input said 'flat'): {'flag' in lower}")
    print(f"  mentions 'on call'/'on-call' (correct term, garbled input said 'uncalled'): "
          f"{'on call' in lower or 'on-call' in lower}")
    print(f"  mentions 'three replicas'/'3 replicas' (correct term, garbled input said 'it. 3'): "
          f"{'three replicas' in lower or '3 replicas' in lower}")


def main():
    print("=" * 70)
    print("COMPARISON 1: fair window (today's approach sees all 6 segments)")
    print("=" * 70)
    answer_fair, elapsed_fair, calls_fair = ask_rolling_window(SEGMENTS, "fair-window")
    check_terms("fair-window (rolling, all 6 segments)", answer_fair)

    print("\n" + "=" * 70)
    print("COMPARISON 2: truncated window (today's approach only sees last 2 segments,")
    print("simulating a longer real meeting where earlier context already fell out)")
    print("=" * 70)
    answer_truncated, elapsed_truncated, calls_truncated = ask_rolling_window(SEGMENTS[-2:], "truncated-window")
    check_terms("truncated-window (rolling, last 2 segments only)", answer_truncated)

    print("\n" + "=" * 70)
    print("Accumulated native turn history (proposed design)")
    print("=" * 70)
    answer_accumulated, elapsed_accumulated, calls_accumulated = ask_accumulated_turns(SEGMENTS)
    check_terms("accumulated (native turn history + tiny nudge)", answer_accumulated)

    print("\n" + "=" * 70)
    print("LATENCY SUMMARY (real wall-clock, real CLI calls)")
    print("=" * 70)
    print(f"fair-window rolling:      {calls_fair} call(s),      {elapsed_fair:.2f}s total")
    print(f"truncated-window rolling: {calls_truncated} call(s),      {elapsed_truncated:.2f}s total")
    print(f"accumulated turns:        {calls_accumulated} call(s), {elapsed_accumulated:.2f}s total "
          f"(only the LAST call is the real user-facing suggestion; the other "
          f"{calls_accumulated - 1} are discarded replies paid for just to deposit context)")


if __name__ == "__main__":
    main()
