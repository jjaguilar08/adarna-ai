"""
Track B baseline: this app's real production transcription path
(transcribe_filtered on small.en, beam_size=5, int8, CPU), run once per
whole test clip -- treating each clip as a single already-closed VAD
segment, exactly like a real closed segment reaching transcribe_segment()
in wsl_app/main.py. This is the number track_b_whisperx.py's WhisperX
pipeline gets compared against.

Run with the MAIN wsl_app venv (not the isolated whisperx_venv):
    .venv/bin/python3 research/day20/track_b_baseline.py
(from wsl_app/, so streaming_transcriber.py is importable)

Writes research/day20/track_b_baseline_results.json for the comparison
report to read.
"""
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

sys.path.insert(0, ".")
from streaming_transcriber import transcribe_filtered

TRANSCRIBE_BEAM_SIZE = 5  # matches wsl_app/main.py's TRANSCRIBE_BEAM_SIZE

CLIPS = [
    ("en_normal", "research/day16/en_normal.wav", "research/day16/en_normal.txt"),
    ("en_fast", "research/day16/en_fast.wav", "research/day16/en_fast.txt"),
    ("zira_clip", "research/day18_5/zira_clip.wav", "research/day18_5/zira_clip.txt"),
    ("fast_long", "research/day18_6/fast_long.wav", "research/day18_6/fast_long.txt"),
]


def load_wav_as_float32(path):
    """
    Returns:
        numpy.ndarray: 16kHz mono samples in [-1.0, 1.0].
    """
    with wave.open(path) as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        raw_bytes = wav_file.readframes(wav_file.getnframes())
    return np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0


def main():
    """Transcribes every clip once via the real production call path, timing each, and writes results to JSON."""
    print("Loading small.en (matches production DEFAULT_WHISPER_MODEL_SIZE)...")
    model = WhisperModel("small.en", device="cpu", compute_type="int8")

    results = {}
    for name, wav_path, txt_path in CLIPS:
        audio = load_wav_as_float32(wav_path)
        duration = len(audio) / 16000
        start = time.monotonic()
        words = transcribe_filtered(model, audio, TRANSCRIBE_BEAM_SIZE)
        elapsed = time.monotonic() - start
        text = "".join(w[2] for w in words).strip()
        print(f"\n--- {name} ({duration:.1f}s) --- latency {elapsed:.2f}s (RTF {elapsed/duration:.2f}x)")
        print(text)
        results[name] = {
            "duration_seconds": duration,
            "latency_seconds": elapsed,
            "text": text,
            "ground_truth": Path(txt_path).read_text().strip(),
        }

    out_path = Path("research/day20/track_b_baseline_results.json")
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
