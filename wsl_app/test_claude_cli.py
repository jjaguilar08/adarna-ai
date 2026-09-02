"""
Standalone way to try out ClaudeCli by hand. Not part of the running
wsl_app server — run this file directly: python test_claude_cli.py
"""
import time

from main import MEETING_SYSTEM_PROMPT, ClaudeCli

# Real transcript excerpts from Day 4 manual testing (see project_notes.md),
# not invented text.
TRANSCRIPT_EXCERPTS = [
    "we understand that transitioning to the PPO industry can be a bit of a "
    "challenge especially if it is your first time",
    "interact with the quick round fox jumps over the lazy dog",
]


def send_example_prompts_and_report():
    """
    Starts one ClaudeCli, sends it each transcript excerpt in turn, and
    prints the excerpt used, the suggestion that came back, and how long it
    took. Finishes by comparing the first call's time to the second's, since
    the whole point of keeping one claude process running (rather than
    starting a new one per prompt) is that later calls should be
    meaningfully faster than the first.
    """
    claude_cli = ClaudeCli(MEETING_SYSTEM_PROMPT)
    seconds_taken = []

    for excerpt in TRANSCRIPT_EXCERPTS:
        started_at = time.monotonic()
        suggestion = claude_cli.ask(excerpt)
        elapsed_seconds = time.monotonic() - started_at
        seconds_taken.append(elapsed_seconds)

        print(f"Transcript excerpt: {excerpt!r}")
        print(f"Suggestion: {suggestion!r}")
        print(f"Took {elapsed_seconds:.2f}s\n")

    claude_cli.stop()

    first_call_seconds, second_call_seconds = seconds_taken[0], seconds_taken[1]
    comparison = "faster" if second_call_seconds < first_call_seconds else "SLOWER (unexpected)"
    print(
        f"First call: {first_call_seconds:.2f}s, second call: {second_call_seconds:.2f}s "
        f"— second call was {comparison}"
    )


if __name__ == "__main__":
    send_example_prompts_and_report()
