"""
Day 18.7, question 1: can the claude CLI's headless stream-json mode ingest a
user turn purely as context, without forcing a full assistant-response
generation every single time?

Starts one real `claude -p --input-format stream-json --output-format
stream-json ...` process (same flags as production ClaudeCli.__init__),
sends several "user" turns back-to-back without waiting for a response in
between, then reads everything the process actually produced and reports,
per turn sent, whether a matching "assistant"/"result" pair came back.

Not part of the running app -- standalone research script, run directly:
    python research/day18_7/probe_silent_turns.py
"""
import json
import subprocess
import threading
import time

SYSTEM_PROMPT = "You are a helpful assistant. Reply with one short sentence."

TURNS = [
    "Segment 1: the weather today is sunny.",
    "Segment 2: I went for a walk in the park.",
    "Segment 3: then I bought a coffee.",
]


def start_process():
    """Starts the claude CLI the same way production ClaudeCli does."""
    return subprocess.Popen(
        [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--system-prompt", SYSTEM_PROMPT,
            "--tools", "",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def drain_stderr(process, label):
    """Prints stderr lines with a label prefix so they're visible but distinguishable."""
    for line in process.stderr:
        print(f"[{label} stderr] {line.rstrip()}")


def main():
    process = start_process()
    threading.Thread(target=drain_stderr, args=(process, "probe"), daemon=True).start()

    print("Sending all 3 user turns back-to-back with NO reads in between...")
    sent_at = {}
    for i, text in enumerate(TURNS):
        line = {"type": "user", "message": {"role": "user", "content": text}}
        sent_at[i] = time.monotonic()
        process.stdin.write(json.dumps(line) + "\n")
        process.stdin.flush()
        print(f"  sent turn {i}: {text!r} at t={sent_at[i]:.2f}")

    # Now read whatever comes back for a fixed window, logging every line's
    # type and rough size so we can see whether 1, 3, or some other number
    # of assistant/result pairs come out.
    print("\nReading all output for up to 150s...")
    deadline = time.monotonic() + 150
    result_count = 0
    assistant_count = 0
    lines_seen = []
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            print("  (stdout closed / process exited)")
            break
        try:
            output = json.loads(line)
        except json.JSONDecodeError:
            print(f"  RAW (non-JSON): {line.rstrip()}")
            continue
        output_type = output.get("type")
        lines_seen.append(output_type)
        if output_type == "assistant":
            assistant_count += 1
            text = "".join(
                block.get("text", "") for block in output.get("message", {}).get("content", [])
                if isinstance(block, dict)
            )
            print(f"  [{time.monotonic():.2f}] assistant message #{assistant_count}: {text!r}")
        elif output_type == "result":
            result_count += 1
            print(f"  [{time.monotonic():.2f}] result #{result_count}: {output.get('result', '')!r}")
            if result_count >= len(TURNS):
                print("  (got one result per turn sent -- stopping early)")
                break
        elif output_type == "stream_event":
            pass  # too noisy to log individually; counted implicitly via assistant text above
        elif output_type == "system":
            print(f"  [{time.monotonic():.2f}] system line: subtype={output.get('subtype')} "
                  f"tools_count={len(output.get('tools', []))} session_id={output.get('session_id')}")
        else:
            print(f"  [{time.monotonic():.2f}] other line type: {output_type} -> "
                  f"{json.dumps(output)[:300]}")

    print(f"\nSummary: sent {len(TURNS)} user turns, got back {assistant_count} assistant "
          f"messages and {result_count} result lines.")
    print(f"Line type sequence (excluding stream_event spam): "
          f"{[t for t in lines_seen if t != 'stream_event']}")

    if result_count == len(TURNS):
        print("\n==> Each user turn forced its own full assistant response. "
              "No 'silent context ingestion' mode observed with this flag set.")
    elif result_count == 1:
        print("\n==> Only ONE result came back for 3 turns sent -- the CLI may have "
              "only processed the first (or merged/dropped others). Needs closer reading.")
    else:
        print(f"\n==> Unexpected: {result_count} results for {len(TURNS)} turns sent. Read the "
              "full line sequence above.")

    process.stdin.close()
    process.wait(timeout=10)


if __name__ == "__main__":
    main()
