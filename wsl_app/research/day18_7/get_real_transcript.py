"""
Regenerates the real small.en transcript of research/day18_6/fast_long.wav
using the exact production transcribe_filtered() call, so this spike has
real, current model output (with real baked-in transcription errors) to
build the accuracy comparison on, per Jon's instruction to reuse the
base.en-era test clips rather than inventing synthetic errors.

Standalone research script, run directly:
    python research/day18_7/get_real_transcript.py
"""
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
from faster_whisper import WhisperModel

from streaming_transcriber import TARGET_SAMPLE_RATE, transcribe_filtered

TRANSCRIBE_BEAM_SIZE = 5  # matches main.py's production constant


def load_wav_float32(path):
    """Reads a 16kHz mono 16-bit PCM WAV file into float32 samples in [-1.0, 1.0]."""
    with wave.open(str(path)) as wav_file:
        assert wav_file.getframerate() == TARGET_SAMPLE_RATE
        assert wav_file.getnchannels() == 1
        raw_bytes = wav_file.readframes(wav_file.getnframes())
    return np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0


def main():
    clip_path = Path(__file__).resolve().parent.parent / "day18_6" / "fast_long.wav"
    audio = load_wav_float32(clip_path)
    print(f"Loaded {clip_path.name}: {len(audio) / TARGET_SAMPLE_RATE:.1f}s at {TARGET_SAMPLE_RATE}Hz")

    print("Loading small.en...")
    model = WhisperModel("small.en", device="cpu", compute_type="int8")

    print("Transcribing (production transcribe_filtered call)...")
    words = transcribe_filtered(model, audio, TRANSCRIBE_BEAM_SIZE)
    text = "".join(word_text for _start, _end, word_text in words).strip()
    print(f"\nREAL small.en transcript ({len(words)} words):\n{text}")

    out_path = Path(__file__).resolve().parent / "fast_long_real_transcript.txt"
    out_path.write_text(text)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
