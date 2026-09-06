"""
Day 18.7, question 4: deliberately test the ONE failure mode the accumulated-
context design does NOT fix -- a fluent-sounding but wrong transcription
(exactly what Day 18's no_speech_prob/avg_logprob filters in
transcribe_filtered() were built to catch, see streaming_transcriber.py).

Uses the real, previously-confirmed hallucination text from Day 18's own
live testing (documented in CLAUDE.md): transcribing just the last 0.5s of a
real clip produced the fabricated sentence "It is not just one of my
favorite damp gases in the face." (avg_logprob -3.32) in place of real
trailing audio -- a case transcribe_filtered() itself would normally filter
out via the avg_logprob threshold, but this script feeds it in as if it
already got past that filter (e.g. a slightly-less-confident fabrication
that still clears the threshold), because the whole point here is to check
whether MORE CONTEXT helps a downstream suggestion step notice a
fabrication filtering already missed -- not to re-test the filter itself.

Real accumulated context (the same 6 real, garbled-but-real segments from
accuracy_and_latency_comparison.py) is fed in first via real sequential
ask() turns, then the fabricated final segment is sent as the last segment
before the trigger nudge -- compared against the current rolling-window
approach seeing the same fabricated segment.

Standalone research script, run directly:
    python research/day18_7/hallucination_still_a_problem.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from main import MEETING_SYSTEM_PROMPT, ClaudeCli

REAL_SEGMENTS_BEFORE_HALLUCINATION = [
    "Ok so quick context dump before the call ends because I know we're "
    "almost out of time,",

    "the deploy this morning went fine on the payment service but right "
    "after that we saw the checkout latency spike from around 90ms up to "
    "almost 450ms for about 6 minutes before it settled back down,",

    "and when we dug into the traces it looked like the connection pool to "
    "the inventory service was getting exhausted because a retry storm "
    "kicked in when one of it. 3 inventory replicas got slow,",
]

# Real, documented Day 18 fabrication text (see CLAUDE.md, the
# avg_logprob-hallucination entry) -- fluent, grammatically fine, and
# entirely disconnected in meaning from the real preceding context.
HALLUCINATED_SEGMENT = "It is not just one of my favorite damp gases in the face."

NUDGE = "Based on everything said so far, suggest how the user could respond to what's just been discussed."


def run_rolling_window():
    """Today's approach: rolling window text includes the real segments plus the fabricated one, pasted fresh."""
    claude_cli = ClaudeCli(MEETING_SYSTEM_PROMPT)
    try:
        prompt_text = " ".join(REAL_SEGMENTS_BEFORE_HALLUCINATION + [HALLUCINATED_SEGMENT])
        started = time.monotonic()
        answer = claude_cli.ask(prompt_text)
        elapsed = time.monotonic() - started
        print(f"\n--- Rolling window (real segments + fabricated final segment) ---")
        print(f"prompt: {prompt_text!r}")
        print(f"({elapsed:.2f}s)")
        print(f"answer: {answer!r}")
        return answer
    finally:
        claude_cli.stop()


def run_accumulated_turns():
    """Proposed design: real segments fed as real turns first, then the fabricated one, then the trigger nudge."""
    claude_cli = ClaudeCli(MEETING_SYSTEM_PROMPT)
    try:
        print(f"\n--- Accumulated turns (real segments as turns, then fabricated segment as a turn, then nudge) ---")
        for i, segment in enumerate(REAL_SEGMENTS_BEFORE_HALLUCINATION):
            reply = claude_cli.ask(segment)
            print(f"  turn {i} (real, discarded): {segment[:60]!r}... -> {reply[:60]!r}...")
        reply = claude_cli.ask(HALLUCINATED_SEGMENT)
        print(f"  turn {len(REAL_SEGMENTS_BEFORE_HALLUCINATION)} (FABRICATED, discarded): "
              f"{HALLUCINATED_SEGMENT!r} -> {reply!r}")
        started = time.monotonic()
        answer = claude_cli.ask(NUDGE)
        elapsed = time.monotonic() - started
        print(f"  trigger turn ({elapsed:.2f}s)")
        print(f"answer: {answer!r}")
        return answer
    finally:
        claude_cli.stop()


def main():
    rolling_answer = run_rolling_window()
    accumulated_answer = run_accumulated_turns()

    print("\n" + "=" * 70)
    print("Does either approach's final answer show any sign it noticed the")
    print("fabricated segment was disconnected from the real conversation?")
    print("=" * 70)
    print(f"rolling window answer:   {rolling_answer!r}")
    print(f"accumulated turns answer: {accumulated_answer!r}")


if __name__ == "__main__":
    main()
