"""
Track A benchmark: Day 18.6's flagged-but-never-tried idea -- force a
shorter sub-segment boundary during continuous/fast speech (no natural
pause), so small.en gets called sooner on a smaller chunk, instead of
waiting for the current MAX_SEGMENT_SECONDS=60.0 safety net or a real
pause. Still one model, one decode per closed segment -- no streaming, no
buffer-trim, no cross-model reconciliation, so none of the Day 16-18
failure modes apply structurally. This script tests whether it actually
helps and what it costs in accuracy, with real numbers.

Segmentation logic is copied from wsl_app/main.py's VoiceSegmenter (not
imported) and parameterized on the force-close threshold, since this is a
research spike -- no pipeline changes yet, per Day 20 scope.

Usage: .venv/bin/python3 research/day20/track_a_segmentation.py
(run from wsl_app/, so streaming_transcriber.py is importable)
"""
import sys
import time
import wave
from pathlib import Path
from typing import NamedTuple

import numpy as np
import webrtcvad
from faster_whisper import WhisperModel

sys.path.insert(0, ".")
from streaming_transcriber import transcribe_filtered

sys.path.insert(0, "research/day20")
from wer import word_error_rate

TARGET_SAMPLE_RATE = 16000
FRAME_DURATION_SECONDS = 0.03
BYTES_PER_FRAME = int(TARGET_SAMPLE_RATE * FRAME_DURATION_SECONDS) * 2
SPEECH_DETECTION_STRICTNESS = 2  # matches main.py's loopback/TTS-clip strictness
SILENCE_SECONDS_TO_CLOSE_SEGMENT = 0.4  # matches main.py
CURRENT_MAX_SEGMENT_SECONDS = 60.0  # matches main.py's production default -- the "baseline" arm

TRANSCRIBE_BEAM_SIZE = 5  # matches main.py's TRANSCRIBE_BEAM_SIZE

# Candidate shorter force-close thresholds to test against the current 60s one.
FAST_SPEECH_SUB_SEGMENT_CANDIDATES = [8.0, 12.0, 16.0]

CLIPS = [
    ("en_normal", "research/day16/en_normal.wav", "research/day16/en_normal.txt"),
    ("en_fast", "research/day16/en_fast.wav", "research/day16/en_fast.txt"),
    ("zira_clip", "research/day18_5/zira_clip.wav", "research/day18_5/zira_clip.txt"),
    ("fast_long", "research/day18_6/fast_long.wav", "research/day18_6/fast_long.txt"),
]


class ClosedSegment(NamedTuple):
    """One finished speech segment: its audio and when it started, in seconds from clip start."""
    audio_bytes: bytes
    started_at_seconds: float
    ended_at_seconds: float


class TestSegmenter:
    """
    A trimmed copy of main.py's VoiceSegmenter, parameterized on
    max_segment_seconds so this script can compare the current 60s
    threshold against shorter fast-speech-aware candidates without
    touching production code. Same VAD-driven open/close logic otherwise.
    """

    def __init__(self, max_segment_seconds):
        """Starts with nothing recorded, force-closing an open segment after max_segment_seconds of continuous recording."""
        self._max_segment_seconds = max_segment_seconds
        self._speech_detector = webrtcvad.Vad(SPEECH_DETECTION_STRICTNESS)
        self._unsliced_audio = bytearray()
        self._current_segment_audio = bytearray()
        self._segment_is_open = False
        self._segment_started_at = None
        self._seconds_of_silence_in_a_row = 0.0
        self._seconds_recorded_in_current_segment = 0.0
        self._clip_seconds_elapsed = 0.0

    def add_audio(self, audio_bytes):
        """Feeds 16kHz mono audio in; returns zero or more ClosedSegments finished while processing it."""
        self._unsliced_audio.extend(audio_bytes)
        finished = []
        while len(self._unsliced_audio) >= BYTES_PER_FRAME:
            frame = bytes(self._unsliced_audio[:BYTES_PER_FRAME])
            del self._unsliced_audio[:BYTES_PER_FRAME]
            result = self._add_frame(frame)
            if result is not None:
                finished.append(result)
        return finished

    def _add_frame(self, frame):
        """Same per-frame open/close decision as production VoiceSegmenter, against this instance's own threshold."""
        is_speech = self._speech_detector.is_speech(frame, TARGET_SAMPLE_RATE)
        self._clip_seconds_elapsed += FRAME_DURATION_SECONDS

        if not is_speech and not self._segment_is_open:
            return None

        self._current_segment_audio.extend(frame)
        self._seconds_recorded_in_current_segment += FRAME_DURATION_SECONDS

        if is_speech:
            if not self._segment_is_open:
                self._segment_started_at = self._clip_seconds_elapsed - FRAME_DURATION_SECONDS
            self._segment_is_open = True
            self._seconds_of_silence_in_a_row = 0.0
        else:
            self._seconds_of_silence_in_a_row += FRAME_DURATION_SECONDS
            if self._seconds_of_silence_in_a_row >= SILENCE_SECONDS_TO_CLOSE_SEGMENT:
                return self._close()

        if self._seconds_recorded_in_current_segment >= self._max_segment_seconds:
            return self._close()
        return None

    def _close(self):
        """Packages up the current segment and resets for the next one."""
        audio = bytes(self._current_segment_audio)
        started_at = self._segment_started_at
        ended_at = self._clip_seconds_elapsed
        self._current_segment_audio = bytearray()
        self._segment_is_open = False
        self._segment_started_at = None
        self._seconds_of_silence_in_a_row = 0.0
        self._seconds_recorded_in_current_segment = 0.0
        return ClosedSegment(audio, started_at, ended_at)

    def close_open_segment(self):
        """Force-closes whatever's in progress at end-of-clip, same as session-stop in production."""
        if not self._segment_is_open:
            return None
        return self._close()


def load_wav_bytes(path):
    """
    Returns:
        bytes: raw 16-bit PCM samples from a 16kHz mono WAV file.
    """
    with wave.open(path) as wav_file:
        assert wav_file.getframerate() == 16000, f"{path} is not 16kHz"
        assert wav_file.getnchannels() == 1, f"{path} is not mono"
        return wav_file.readframes(wav_file.getnframes())


def pcm_bytes_to_float32(audio_bytes):
    """
    Returns:
        numpy.ndarray: mono samples in [-1.0, 1.0], matching what
        transcribe_filtered expects.
    """
    int16 = np.frombuffer(audio_bytes, dtype=np.int16)
    return int16.astype(np.float32) / 32768.0


def run_segmenter(model, audio_bytes, max_segment_seconds):
    """
    Runs one full clip through a TestSegmenter at the given threshold,
    transcribes each closed segment with the real production
    transcribe_filtered() call, and times how long each segment took to
    decode.

    Returns:
        dict: {
            "segments": list of (started_at, ended_at, text, decode_seconds),
            "full_text": segment texts joined with a space (segment
                boundaries are real pause/force-close points, so a space
                between them is correct, unlike faster-whisper's own
                intra-segment word joining),
            "time_to_first_text_seconds": ended_at + decode_seconds of the
                first segment -- the real wall-clock moment a live viewer
                would see the first visible text, relative to clip start,
            "segment_count": how many segments this threshold produced.
        }
    """
    segmenter = TestSegmenter(max_segment_seconds)
    closed = list(segmenter.add_audio(audio_bytes))
    tail = segmenter.close_open_segment()
    if tail is not None:
        closed.append(tail)

    results = []
    for segment in closed:
        audio = pcm_bytes_to_float32(segment.audio_bytes)
        start = time.monotonic()
        words = transcribe_filtered(model, audio, TRANSCRIBE_BEAM_SIZE)
        decode_seconds = time.monotonic() - start
        text = "".join(w[2] for w in words).strip()
        results.append((segment.started_at_seconds, segment.ended_at_seconds, text, decode_seconds))

    full_text = " ".join(r[2] for r in results if r[2])
    first_text_at = (results[0][1] + results[0][3]) if results else float("nan")
    return {
        "segments": results,
        "full_text": full_text,
        "time_to_first_text_seconds": first_text_at,
        "segment_count": len(results),
    }


def print_boundary_context(results, window_chars=60):
    """Prints the tail of each segment except the last, and the head of the next, so a human can eyeball whether a forced cut landed mid-word/mid-sentence and cost visible context."""
    for i in range(len(results) - 1):
        this_tail = results[i][2][-window_chars:]
        next_head = results[i + 1][2][:window_chars]
        print(f"    boundary {i}->{i+1} (~{results[i][1]:.1f}s): ...{this_tail!r}  |  {next_head!r}...")


def main():
    """
    For each test clip, runs the current 60s threshold (baseline) and each
    fast-speech-aware candidate threshold, reporting segment count, WER
    against ground truth, and time-to-first-visible-text for each.
    """
    print("Loading small.en (matches production DEFAULT_WHISPER_MODEL_SIZE)...")
    model = WhisperModel("small.en", device="cpu", compute_type="int8")

    thresholds = [("baseline (60s, current)", CURRENT_MAX_SEGMENT_SECONDS)] + [
        (f"fast-speech-aware ({t:.0f}s)", t) for t in FAST_SPEECH_SUB_SEGMENT_CANDIDATES
    ]

    for name, wav_path, txt_path in CLIPS:
        ground_truth = Path(txt_path).read_text().strip()
        audio_bytes = load_wav_bytes(wav_path)
        duration = len(audio_bytes) / 2 / TARGET_SAMPLE_RATE
        print(f"\n{'=' * 78}\nCLIP: {name} ({duration:.1f}s)\n{'=' * 78}")

        for label, threshold in thresholds:
            result = run_segmenter(model, audio_bytes, threshold)
            wer, edits, ref_words = word_error_rate(ground_truth, result["full_text"])
            print(f"\n--- {label} ---")
            print(f"segments: {result['segment_count']}  "
                  f"time-to-first-text: {result['time_to_first_text_seconds']:.1f}s  "
                  f"WER: {wer:.1%} ({edits}/{ref_words} words)")
            print(f"text: {result['full_text']}")
            if result["segment_count"] > 1:
                print_boundary_context(result["segments"])


if __name__ == "__main__":
    main()
