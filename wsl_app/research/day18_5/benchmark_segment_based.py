"""
Day 18.5 benchmark: re-evaluates Whisper model size under the CURRENT
segment-based architecture (one whole-segment model.transcribe() call per
finished utterance), not the Day 16-17 streaming architecture that
repeatedly re-decoded a growing buffer. Reuses the same transcribe_filtered()
helper wsl_app/main.py actually calls in production, with the same
beam_size/compute_type, so results reflect the real code path -- not a
reimplementation someone has to trust matches production behavior.

Usage: .venv/bin/python3 research/day18_5/benchmark_segment_based.py
(run from wsl_app/, so streaming_transcriber.py is importable)
"""
import sys
import time
import wave

import numpy as np
from faster_whisper import WhisperModel

sys.path.insert(0, ".")
from streaming_transcriber import transcribe_filtered

MODELS_TO_TEST = ["small.en", "medium.en", "distil-large-v3"]
TRANSCRIBE_BEAM_SIZE = 5  # matches wsl_app/main.py's TRANSCRIBE_BEAM_SIZE

CLIPS = [
    ("en_normal", "research/day16/en_normal.wav", "research/day16/en_normal.txt"),
    ("en_fast", "research/day16/en_fast.wav", "research/day16/en_fast.txt"),
    ("zira_clip", "research/day18_5/zira_clip.wav", "research/day18_5/zira_clip.txt"),
]


def load_wav_as_float32(path):
    """
    Reads a 16kHz mono 16-bit PCM WAV file into the float32 array format
    faster-whisper expects, matching prepare_audio_for_whisper()'s own
    conversion in production.

    Returns:
        numpy.ndarray: mono samples in the range [-1.0, 1.0].
    """
    with wave.open(path) as wav_file:
        assert wav_file.getframerate() == 16000, f"{path} is not 16kHz"
        assert wav_file.getnchannels() == 1, f"{path} is not mono"
        raw_bytes = wav_file.readframes(wav_file.getnframes())
    int16_samples = np.frombuffer(raw_bytes, dtype=np.int16)
    return int16_samples.astype(np.float32) / 32768.0


def run_one_clip(model, audio, language):
    """
    Runs the exact same call production's transcribe_segment() makes, once,
    and times it end to end (including the generator being fully consumed --
    faster-whisper is lazy, so timing has to force iteration or the "call"
    finishes instantly with no work actually done).

    Returns:
        tuple[str, float]: (transcribed text, wall-clock seconds).
    """
    start = time.monotonic()
    words = transcribe_filtered(model, audio, TRANSCRIBE_BEAM_SIZE)
    elapsed = time.monotonic() - start
    text = "".join(word[2] for word in words).strip()
    return text, elapsed


def main():
    """
    Loads each candidate model once, runs every test clip through it via the
    real production call path, and prints latency + transcribed text so it
    can be compared side by side against the ground-truth script for each
    clip.
    """
    clips_loaded = [(name, load_wav_as_float32(path), open(txt).read().strip())
                     for name, path, txt in CLIPS]

    for model_size in MODELS_TO_TEST:
        print(f"\n{'=' * 70}\nMODEL: {model_size}\n{'=' * 70}")
        load_start = time.monotonic()
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        print(f"(model load: {time.monotonic() - load_start:.1f}s)")

        for name, audio, ground_truth in clips_loaded:
            text, elapsed = run_one_clip(model, audio, "en")
            duration_seconds = len(audio) / 16000
            print(f"\n--- clip: {name} ({duration_seconds:.1f}s audio) ---")
            print(f"latency: {elapsed:.2f}s (realtime factor {elapsed / duration_seconds:.2f}x)")
            print(f"ground truth : {ground_truth}")
            print(f"transcribed  : {text}")


if __name__ == "__main__":
    main()
