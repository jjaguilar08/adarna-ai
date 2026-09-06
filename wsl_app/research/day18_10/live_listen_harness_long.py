"""
Day 18.10 -- live-agent-listening experiment, real-duration follow-up to Day 18.8/18.9.
Both of those ran a single ~46s clip; the one thing they explicitly left open was whether the
notification-driven mechanism (a background task whose SEGMENT/TRIGGER output arrives as live
notifications, no polling, no human relay -- confirmed Day 18.9 to work fully unattended in a
bare terminal `claude` session) holds up over something closer to a real 30-60 minute meeting:
context growth over many turns, subscription rate-limit exposure (PRD S10's "chatty tool" risk),
and long idle/silent stretches, none of which a 46-second run can exercise at all.

Like Day 18.8's harness, this feeds real WAV audio through the REAL production pipeline --
VoiceSegmenter, transcribe_segment(), and (new here) the real SuggestionTrigger class too,
unmodified -- in real-time-paced 100ms chunks. Day 18.8's harness only ever fired one synthetic
trigger, by hand, after all its audio was done; this one runs an actual asyncio event loop so
SuggestionTrigger's own pause-timer logic decides when to fire, the same way it does in
production, which is what makes a real end-to-end turn-count estimate for a real meeting
possible.

Shape chosen for the ~30-60 minute target, without recording new audio (see README.md for the
full reasoning):
  - The 5 existing synthetic test clips across research/day16, day18_5, day18_6 (different
    voices/speeds/content: clean English, fast English, Taglish code-switched vocabulary) are
    played back-to-back as one ~193s "cycle", separated by short gaps sized to reliably let a
    trigger fire between clips instead of chaining straight into the next one's speech.
  - The cycle repeats FULL_RUN_CYCLE_REPEATS times.
  - After cycles 3, 6, and 9, a deliberate multi-minute stretch of pure silence stands in for a
    real meeting's quiet periods, instead of the short inter-clip gap.
  - Total: ~44 real minutes, ~34% of it deliberate silence.

This script only produces the log -- it does not call any LLM. A live agent session (the one
running this experiment, per Day 18.8/18.9's pattern) is the thing that reasons about the
stream; this just has to prove the stream itself holds up.

Standalone research script, run directly:
    python research/day18_10/live_listen_harness_long.py > /path/to/live_transcript_long.log

Pass --smoke-test for a ~70-second shrunken run (2 clips, 1 cycle, 1 short idle stretch) to
verify the harness's own mechanics -- segmenting, triggering, idle handling, logging -- work
correctly before committing to a real 30-60 minute unattended run.
"""
import argparse
import asyncio
import sys
import time
import traceback
import wave
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from faster_whisper import WhisperModel

from main import (
    PAUSE_SECONDS_BEFORE_SUGGESTION,
    SuggestionTrigger,
    VoiceSegmenter,
    transcribe_segment,
)

RESEARCH_DIR = Path(__file__).resolve().parent.parent
CLIP_PATHS = [
    RESEARCH_DIR / "day16" / "en_normal.wav",
    RESEARCH_DIR / "day16" / "en_fast.wav",
    RESEARCH_DIR / "day16" / "taglish.wav",
    RESEARCH_DIR / "day18_5" / "zira_clip.wav",
    RESEARCH_DIR / "day18_6" / "fast_long.wav",
]

CHUNK_SECONDS = 0.1  # matches the ~100ms real audio_chunk size noted elsewhere in this project
BYTES_PER_SECOND = 16000 * 2  # 16kHz, 16-bit samples

# Longer than PAUSE_SECONDS_BEFORE_SUGGESTION (1.2s) so a trigger reliably fires and settles
# *before* the next clip's speech starts, instead of the two overlapping.
GAP_BETWEEN_CLIPS_SECONDS = 3.0

HEARTBEAT_INTERVAL_SECONDS = 60.0

# Full real-duration run: ~44 minutes total (~29 min of clip/gap content across 9 cycles, plus
# 3 deliberate 5-minute silent stretches). See README.md for the exact arithmetic.
FULL_RUN_CYCLE_REPEATS = 9
FULL_RUN_IDLE_AFTER_CYCLES = {3, 6, 9}
FULL_RUN_IDLE_STRETCH_SECONDS = 300.0
FULL_RUN_CLIP_PATHS = CLIP_PATHS

# Smoke test: ~70 seconds, just enough to exercise every code path (a clip, an inter-clip gap, a
# trigger firing, an idle stretch, a cycle boundary) before trusting the harness with a real
# 30-60 minute unattended run.
SMOKE_TEST_CYCLE_REPEATS = 1
SMOKE_TEST_IDLE_AFTER_CYCLES = {1}
SMOKE_TEST_IDLE_STRETCH_SECONDS = 5.0
SMOKE_TEST_CLIP_PATHS = CLIP_PATHS[:2]
SMOKE_TEST_GAP_SECONDS = 1.5


def load_wav_bytes(path):
    """Reads a 16kHz mono 16-bit PCM WAV file's raw bytes, unconverted (this is exactly the format VoiceSegmenter.add_audio expects)."""
    with wave.open(str(path)) as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        return wav_file.readframes(wav_file.getnframes())


def log(message):
    """Writes one wall-clock-and-monotonic-timestamped line to stdout and flushes immediately, so a tailing process sees it the instant it's written."""
    wall_clock = time.strftime("%H:%M:%S")
    print(f"{wall_clock} [{time.monotonic():.2f}] {message}", flush=True)


class RunStats:
    """
    Tallies everything the four questions this experiment needs to answer (degradation over
    time, total turn count, errors, idle behavior) require -- kept as one object so both the
    per-cycle log lines and the final summary read from the same running totals.
    """

    def __init__(self):
        """Starts every counter at zero and every latency list empty."""
        self.total_segments_with_text = 0
        self.total_segments_empty = 0
        self.total_triggers = 0
        self.total_errors = 0
        self.all_transcribe_latencies = []
        self._cycle_segments_with_text = 0
        self._cycle_segments_empty = 0
        self._cycle_triggers = 0
        self._cycle_latencies = []

    def record_segment(self, has_text, latency_seconds):
        """Records one finished segment's outcome (real text or empty) and how long transcribing it took."""
        self.all_transcribe_latencies.append(latency_seconds)
        self._cycle_latencies.append(latency_seconds)
        if has_text:
            self.total_segments_with_text += 1
            self._cycle_segments_with_text += 1
        else:
            self.total_segments_empty += 1
            self._cycle_segments_empty += 1

    def record_trigger(self):
        """Records one suggestion trigger having fired."""
        self.total_triggers += 1
        self._cycle_triggers += 1

    def record_error(self):
        """Records one caught-and-survived error."""
        self.total_errors += 1

    def cycle_summary_and_reset(self, cycle_number):
        """
        Builds a one-line summary of everything that happened during the cycle just finished,
        then resets the per-cycle counters for the next one. Kept separate from the running
        totals so this line always describes only the cycle that just ended, letting a reviewer
        compare cycle 1's line against cycle 9's line to spot degradation directly, without
        having to do that subtraction by hand.

        Returns:
            str: the log line to print for this cycle.
        """
        avg_latency = mean(self._cycle_latencies) if self._cycle_latencies else 0.0
        line = (
            f"CYCLE_END #{cycle_number} segments={self._cycle_segments_with_text} "
            f"empty={self._cycle_segments_empty} triggers={self._cycle_triggers} "
            f"avg_transcribe_latency={avg_latency:.2f}s"
        )
        self._cycle_segments_with_text = 0
        self._cycle_segments_empty = 0
        self._cycle_triggers = 0
        self._cycle_latencies = []
        return line

    def final_summary_lines(self, wall_elapsed_seconds):
        """
        Builds the end-of-run summary: total notification count (the real usage estimate PRD
        S10's rate-limit risk needs), and an early-run vs late-run latency comparison (the
        degradation check) alongside the raw error count.

        Returns:
            list[str]: log lines, one fact per line, ready to print in order.
        """
        lines = [
            f"total_wall_clock_seconds={wall_elapsed_seconds:.0f} ({wall_elapsed_seconds / 60:.1f} min)",
            f"total_segments_with_text={self.total_segments_with_text}",
            f"total_segments_empty={self.total_segments_empty}",
            f"total_triggers={self.total_triggers}",
            f"total_notifications_estimate={self.total_segments_with_text + self.total_triggers} "
            "(SEGMENT + TRIGGER lines only -- what a live agent session actually reacts to; "
            "SEGMENT_EMPTY isn't sent to a real session in production either, see "
            "transcribe_segment_and_report's `if text:` guard in main.py)",
            f"total_errors={self.total_errors}",
        ]
        if self.all_transcribe_latencies:
            first_ten = self.all_transcribe_latencies[:10]
            last_ten = self.all_transcribe_latencies[-10:]
            lines.append(f"transcribe_latency_first_10_avg={mean(first_ten):.2f}s")
            lines.append(f"transcribe_latency_last_10_avg={mean(last_ten):.2f}s")
            lines.append(f"transcribe_latency_overall_avg={mean(self.all_transcribe_latencies):.2f}s")
        return lines


def build_schedule(clip_paths, cycle_repeats, gap_seconds, idle_seconds, idle_after_cycles):
    """
    Builds the full run as a flat list of (kind, value) items to play back in order: "clip"
    (a Path), "gap" or "idle" (a duration in seconds), "cycle_start"/"cycle_end" (a cycle
    number). A gap always separates consecutive clips within a cycle; after the whole cycle,
    either an idle stretch (if this cycle's number is in idle_after_cycles) or a plain gap
    stands between it and the next cycle's speech -- either way, always something at least
    GAP_BETWEEN_CLIPS_SECONDS long, long enough for the previous clip's pending trigger to fire
    cleanly before new speech starts.

    Returns:
        list[tuple]: the ordered schedule for run_schedule() to play back.
    """
    schedule = []
    for cycle_number in range(1, cycle_repeats + 1):
        schedule.append(("cycle_start", cycle_number))
        for clip_index, clip_path in enumerate(clip_paths):
            schedule.append(("clip", clip_path))
            if clip_index < len(clip_paths) - 1:
                schedule.append(("gap", gap_seconds))
        if cycle_number in idle_after_cycles:
            schedule.append(("idle", idle_seconds))
        else:
            schedule.append(("gap", gap_seconds))
        schedule.append(("cycle_end", cycle_number))
    return schedule


async def transcribe_and_log(model, trigger, stats, segment_audio):
    """
    Transcribes one closed speech segment (via the real production transcribe_segment()),
    records it in stats, logs the result, and -- only if it contains real text, matching
    production's own `if text:` gate in transcribe_segment_and_report() -- notifies the real
    SuggestionTrigger so its pause timer can fire. Any exception here is caught and counted
    rather than allowed to kill a run that might otherwise still have 30+ minutes left to go.
    """
    started_at = time.monotonic()
    try:
        text = await asyncio.to_thread(transcribe_segment, model, segment_audio)
    except Exception:
        stats.record_error()
        log(f"ERROR transcribing segment:\n{traceback.format_exc()}")
        return
    elapsed_seconds = time.monotonic() - started_at
    stats.record_segment(has_text=bool(text), latency_seconds=elapsed_seconds)
    if text:
        log(f"SEGMENT ({elapsed_seconds:.1f}s to transcribe): {text}")
        trigger.notify_new_segment()
    else:
        log(f"SEGMENT_EMPTY ({elapsed_seconds:.1f}s to transcribe, no speech detected)")


async def stream_chunks(segmenter, model, trigger, stats, audio_bytes):
    """
    Feeds audio_bytes (real clip audio, or silence for a gap/idle stretch) into segmenter in
    real-time-paced CHUNK_SECONDS slices, exactly matching how audio actually arrives from
    windows_app. Uses asyncio.sleep (not time.sleep) so SuggestionTrigger's own pause-timer
    tasks, running concurrently on the same event loop, actually get to fire while this
    "plays back".
    """
    chunk_bytes = int(BYTES_PER_SECOND * CHUNK_SECONDS)
    for offset in range(0, len(audio_bytes), chunk_bytes):
        chunk = audio_bytes[offset:offset + chunk_bytes]
        await asyncio.sleep(CHUNK_SECONDS)
        for finished_segment in segmenter.add_audio(chunk):
            await transcribe_and_log(model, trigger, stats, finished_segment)


async def heartbeat_loop(stats, stop_event):
    """
    Logs one HEARTBEAT line every HEARTBEAT_INTERVAL_SECONDS, independent of anything else
    happening, for the whole run. Without this, a multi-minute IDLE stretch (deliberately quiet,
    by design) would look, after the fact, identical to a real stall -- both are "several
    minutes with no log lines". A heartbeat that keeps appearing on schedule throughout an idle
    stretch is how a reviewer tells "working correctly through a quiet period" apart from "hung".
    Runs until stop_event is set, at which point it exits promptly instead of waiting out its
    own remaining sleep window.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            log(
                f"HEARTBEAT segments={stats.total_segments_with_text + stats.total_segments_empty} "
                f"triggers={stats.total_triggers} errors={stats.total_errors}"
            )


async def run_schedule(schedule, segmenter, model, trigger, stats):
    """Plays back every item in schedule in order, logging cycle/clip/idle boundaries as it goes."""
    clip_audio_cache = {}
    for kind, value in schedule:
        if kind == "cycle_start":
            log(f"CYCLE_START #{value}")
        elif kind == "cycle_end":
            log(stats.cycle_summary_and_reset(value))
        elif kind == "clip":
            if value not in clip_audio_cache:
                clip_audio_cache[value] = load_wav_bytes(value)
            audio_bytes = clip_audio_cache[value]
            log(f"CLIP_START {value.name} duration={len(audio_bytes) / BYTES_PER_SECOND:.1f}s")
            await stream_chunks(segmenter, model, trigger, stats, audio_bytes)
            log(f"CLIP_END {value.name}")
        elif kind == "gap":
            await stream_chunks(segmenter, model, trigger, stats, bytes(int(BYTES_PER_SECOND * value)))
        elif kind == "idle":
            log(f"IDLE_START duration={value:.0f}s (simulating a quiet stretch of a real meeting)")
            await stream_chunks(segmenter, model, trigger, stats, bytes(int(BYTES_PER_SECOND * value)))
            log("IDLE_END")


async def main_async(smoke_test):
    """Loads the model, builds the schedule for the requested run size, plays it back, and prints the final summary."""
    if smoke_test:
        clip_paths = SMOKE_TEST_CLIP_PATHS
        cycle_repeats = SMOKE_TEST_CYCLE_REPEATS
        idle_after_cycles = SMOKE_TEST_IDLE_AFTER_CYCLES
        idle_seconds = SMOKE_TEST_IDLE_STRETCH_SECONDS
        gap_seconds = SMOKE_TEST_GAP_SECONDS
    else:
        clip_paths = FULL_RUN_CLIP_PATHS
        cycle_repeats = FULL_RUN_CYCLE_REPEATS
        idle_after_cycles = FULL_RUN_IDLE_AFTER_CYCLES
        idle_seconds = FULL_RUN_IDLE_STRETCH_SECONDS
        gap_seconds = GAP_BETWEEN_CLIPS_SECONDS

    schedule = build_schedule(clip_paths, cycle_repeats, gap_seconds, idle_seconds, idle_after_cycles)
    estimated_seconds = sum(
        len(load_wav_bytes(value)) / BYTES_PER_SECOND if kind == "clip" else value
        for kind, value in schedule
        if kind in ("clip", "gap", "idle")
    )
    log(
        f"HARNESS_START mode={'smoke_test' if smoke_test else 'full_run'} "
        f"cycles={cycle_repeats} clips_per_cycle={len(clip_paths)} "
        f"pause_seconds_before_suggestion={PAUSE_SECONDS_BEFORE_SUGGESTION} "
        f"estimated_duration_seconds={estimated_seconds:.0f} ({estimated_seconds / 60:.1f} min)"
    )

    print("Loading small.en...", file=sys.stderr, flush=True)
    model = WhisperModel("small.en", device="cpu", compute_type="int8")

    segmenter = VoiceSegmenter()
    stats = RunStats()

    async def on_trigger_fired():
        """SuggestionTrigger's generate_suggestion callback: logs and counts only -- no LLM is called here, see module docstring."""
        stats.record_trigger()
        log(f"TRIGGER #{stats.total_triggers} (pause elapsed with new content since the last trigger -- a real suggestion would fire now)")

    trigger = SuggestionTrigger(on_trigger_fired, PAUSE_SECONDS_BEFORE_SUGGESTION)

    stop_heartbeat = asyncio.Event()
    heartbeat_task = asyncio.create_task(heartbeat_loop(stats, stop_heartbeat))

    started_at = time.monotonic()
    try:
        await run_schedule(schedule, segmenter, model, trigger, stats)
    finally:
        # Lets a trigger that was still pending when the schedule ran out (the trailing idle
        # stretch's own pause timer, for example) actually fire before the run ends, instead of
        # being silently cancelled by the process exiting underneath it.
        await asyncio.sleep(PAUSE_SECONDS_BEFORE_SUGGESTION + 0.5)
        trigger.stop()
        stop_heartbeat.set()
        await heartbeat_task

    wall_elapsed_seconds = time.monotonic() - started_at
    log("HARNESS_SCHEDULE_DONE")
    for line in stats.final_summary_lines(wall_elapsed_seconds):
        log(f"SUMMARY {line}")


def main():
    """Entry point: parses --smoke-test and runs the async harness, exiting non-zero (with a logged crash line) if anything escapes uncaught."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Run a ~70-second shrunken version to verify the harness's own mechanics before a real 30-60 minute run.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args.smoke_test))
    except Exception:
        log(f"HARNESS_CRASHED\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
