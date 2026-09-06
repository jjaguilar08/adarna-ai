"""
Day 18.7, question 1 (realistic pattern): uses the REAL production ClaudeCli
class (main.ClaudeCli) exactly the way MeetingSession would if each finished
transcript segment were fed in as its own turn -- one ask() call per segment,
waiting for each reply before sending the next, same as ClaudeCli.ask()'s own
documented call pattern. This is the realistic version of the probe (the
no-read burst in probe_silent_turns.py stalled on the 3rd turn, which isn't
how production ever calls ask() anyway).

Confirms/refutes: does every ask() call -- even one meant purely to deposit
context -- produce a real, full assistant generation? Reports per-call
latency too, since that's directly relevant to question 2 (recycling budget)
and question 3 (trigger-time latency).

Standalone research script, run directly:
    python research/day18_7/probe_sequential_turns.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from main import MEETING_SYSTEM_PROMPT, ClaudeCli

SEGMENTS = [
    "Segment 1: the weather today is sunny.",
    "Segment 2: I went for a walk in the park.",
    "Segment 3: then I bought a coffee.",
    "Segment 4: I ran into an old coworker.",
]


def main():
    claude_cli = ClaudeCli(MEETING_SYSTEM_PROMPT)
    try:
        for i, segment in enumerate(SEGMENTS):
            started = time.monotonic()
            reply = claude_cli.ask(segment)
            elapsed = time.monotonic() - started
            print(f"Turn {i}: sent {segment!r}")
            print(f"  -> got REAL reply in {elapsed:.2f}s: {reply!r}\n")
    finally:
        claude_cli.stop()


if __name__ == "__main__":
    main()
