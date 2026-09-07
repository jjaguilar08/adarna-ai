"""
Track B: WhisperX's real batched pipeline (own VAD re-segmentation +
batched faster-whisper decoding), CPU-only, run against the same clips and
same small.en checkpoint as track_b_baseline.py's production-path number.
Uses Silero VAD (no HuggingFace auth token needed) rather than WhisperX's
pyannote default, since this is a plain CPU feasibility check, not a
diarization test (Day 19's dual-source capture already solves speaker
attribution structurally -- see docs/DEV_PLAN.md Day 20 finding (c)).

Must run with the ISOLATED whisperx_venv (heavy torch/pyannote deps kept
out of the main wsl_app venv to avoid destabilizing production):
    research/day20/whisperx_venv/bin/python3 research/day20/track_b_whisperx.py
(run from wsl_app/research/day20/)

Writes track_b_whisperx_results.json for the comparison report to read.
"""
import json
import time
import wave
from pathlib import Path

import numpy as np
import whisperx

WHISPER_ARCH = "small.en"  # matches wsl_app/main.py's DEFAULT_WHISPER_MODEL_SIZE
BATCH_SIZES_TO_TEST = [1, 4, 8]

CLIPS = [
    ("en_normal", "../day16/en_normal.wav", "../day16/en_normal.txt"),
    ("en_fast", "../day16/en_fast.wav", "../day16/en_fast.txt"),
    ("zira_clip", "../day18_5/zira_clip.wav", "../day18_5/zira_clip.txt"),
    ("fast_long", "../day18_6/fast_long.wav", "../day18_6/fast_long.txt"),
]


def load_wav_as_float32(path):
    """
    Returns:
        numpy.ndarray: 16kHz mono samples in [-1.0, 1.0], the format
        whisperx.load_audio would also produce from a file path.
    """
    with wave.open(path) as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        raw_bytes = wav_file.readframes(wav_file.getnframes())
    return np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0


def main():
    """Runs WhisperX's transcribe() at each candidate batch size on every clip, timing each and recording the joined segment text."""
    print(f"Loading WhisperX pipeline ({WHISPER_ARCH}, CPU, int8, Silero VAD)...")
    load_start = time.monotonic()
    pipeline = whisperx.load_model(
        WHISPER_ARCH, device="cpu", compute_type="int8", vad_method="silero", language="en",
    )
    print(f"(pipeline load: {time.monotonic() - load_start:.1f}s)")

    results = {}
    for name, wav_path, txt_path in CLIPS:
        audio = load_wav_as_float32(wav_path)
        duration = len(audio) / 16000
        results[name] = {"duration_seconds": duration, "ground_truth": Path(txt_path).read_text().strip(), "batch_sizes": {}}

        for batch_size in BATCH_SIZES_TO_TEST:
            start = time.monotonic()
            result = pipeline.transcribe(audio, batch_size=batch_size, language="en")
            elapsed = time.monotonic() - start
            text = " ".join(seg["text"].strip() for seg in result["segments"]).strip()
            print(f"\n--- {name} ({duration:.1f}s), batch_size={batch_size} --- "
                  f"latency {elapsed:.2f}s (RTF {elapsed/duration:.2f}x), {len(result['segments'])} internal VAD chunks")
            print(text)
            results[name]["batch_sizes"][str(batch_size)] = {"latency_seconds": elapsed, "text": text, "internal_chunks": len(result["segments"])}

    out_path = Path("track_b_whisperx_results.json")
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
