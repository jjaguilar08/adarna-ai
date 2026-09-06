"""
Day 18.8 — live-agent-listening experiment: Jon's idea (following Day 18.7's
"hold off" on feeding the scripted `claude -p` CLI) is different in kind —
have an actual interactive agent session (like a live Claude Code
conversation) attached to a real meeting, watching the transcript stream in,
and reasoning continuously, rather than routing every segment through the
scripted headless subprocess ClaudeCli wraps.

This harness feeds a real WAV clip through the REAL production pipeline --
the exact VoiceSegmenter class and transcribe_segment() function from
main.py, unmodified -- in real-time-paced 100ms chunks (matching how audio
actually arrives from windows_app over the network), so segments land at
genuine VAD-driven boundaries with genuine transcription content and
timing, not an instant dump of pre-computed text. Each closed segment (and,
after the clip ends and the same pause window production's SuggestionTrigger
uses elapses, a synthetic trigger line) is appended to a live log file the
moment it's ready.

A separate live agent session (the one running this experiment) tails that
log file in real time -- e.g. via the harness/Monitor pattern -- and reasons
about it as it arrives, producing a suggestion only when the trigger line
shows up. This script only produces the log; it does not itself call any
LLM -- the whole point is that a live *agent session* is the "brain",
not a subprocess this script spawns.

Standalone research script, run directly:
    python research/day18_8/live_listen_harness.py > /path/to/live_transcript.log
"""
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from faster_whisper import WhisperModel

from main import (
    PAUSE_SECONDS_BEFORE_SUGGESTION,
    TRANSCRIBE_BEAM_SIZE,
    VoiceSegmenter,
    transcribe_segment,
)

CLIP_PATH = Path(__file__).resolve().parent.parent / "day16" / "en_normal.wav"
CHUNK_SECONDS = 0.1  # matches the ~100ms real audio_chunk size noted elsewhere in this project


def load_wav_bytes(path):
    """Reads a 16kHz mono 16-bit PCM WAV file's raw bytes, unconverted (this is exactly the format VoiceSegmenter.add_audio expects)."""
    with wave.open(str(path)) as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        return wav_file.readframes(wav_file.getnframes())


def log(message):
    """Writes one timestamped line to stdout and flushes immediately, so a tailing process sees it the instant it's written."""
    print(f"{time.monotonic():.2f} {message}", flush=True)


def main():
    audio_bytes = load_wav_bytes(CLIP_PATH)
    bytes_per_second = 16000 * 2  # 16kHz, 16-bit samples
    chunk_bytes = int(bytes_per_second * CHUNK_SECONDS)

    log(f"HARNESS_START clip={CLIP_PATH.name} duration={len(audio_bytes) / bytes_per_second:.1f}s")

    print("Loading small.en...", file=sys.stderr, flush=True)
    model = WhisperModel("small.en", device="cpu", compute_type="int8")
    segmenter = VoiceSegmenter()

    last_segment_finished_at = time.monotonic()
    for offset in range(0, len(audio_bytes), chunk_bytes):
        chunk = audio_bytes[offset:offset + chunk_bytes]
        time.sleep(CHUNK_SECONDS)  # real-time pacing, matching how audio actually arrives live
        for finished_segment in segmenter.add_audio(chunk):
            text = transcribe_segment(model, finished_segment)
            last_segment_finished_at = time.monotonic()
            if text:
                log(f"SEGMENT {text}")
            else:
                log("SEGMENT_EMPTY (no speech detected in this closed segment)")

    log("CLIP_AUDIO_DONE (all chunks fed, waiting out the real pause window before triggering)")
    time.sleep(PAUSE_SECONDS_BEFORE_SUGGESTION)
    log("TRIGGER (pause elapsed with nothing new -- a real suggestion would fire now)")


if __name__ == "__main__":
    main()
