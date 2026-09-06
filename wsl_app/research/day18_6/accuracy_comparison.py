"""
Day 18.6 accuracy claim test: on long, fast, continuous speech (no natural
pause for the VoiceSegmenter to close on), is base.en's streaming/settled
output actually more accurate than small.en's eventual whole-segment decode?

Two independent code paths, deliberately not sharing any reconciliation
logic (mirrors the "never merge the two models' output" design constraint
for the real feature) -- this script only reads both outputs side by side
for comparison, it doesn't try to combine them:

- small.en: the exact transcribe_filtered() call wsl_app/main.py's
  transcribe_segment() uses in production, given the whole clip as one
  segment (matches how VoiceSegmenter actually hands it off, confirmed by
  segmenting these clips for real first -- see sanity checks in the day18_6
  README).
- base.en: research/day16/streaming_asr.py's OnlineASRProcessor
  (whisper_streaming-style LocalAgreement-2), fed the clip in small chunks
  to simulate live audio arrival, at Day 17's actual configured cadence
  (~1.5s). "Settled" output = committed words plus a final finish() flush,
  i.e. what the feature would end up showing once nothing more is going to
  change -- not a mid-stream tentative snapshot.
"""
import sys
import time
import wave

import numpy as np
from faster_whisper import WhisperModel

sys.path.insert(0, ".")
sys.path.insert(0, "research/day16")
from streaming_transcriber import transcribe_filtered
from streaming_asr import OnlineASRProcessor, SAMPLE_RATE

SMALL_EN_BEAM_SIZE = 5  # matches production's TRANSCRIBE_BEAM_SIZE
BASE_EN_BEAM_SIZE = 1  # matches Day 16/17's actual chosen live-streaming
# config for base.en specifically -- Day 16 found base.en only sustains a
# ~1.5-2s cadence at beam=1; beam=5 was never the real config this project
# used for live base.en streaming, so testing base.en at beam=5 (an
# apples-to-apples-looking but not historically-accurate choice) isn't a
# fair test of the actual feature being considered.
CHUNK_SECONDS = 1.5  # matches Day 17's actual configured live-cadence
BUFFER_TRIM_SECONDS = 8.0  # shortened from whisper_streaming's 15s default,
# per Day 16's own finding that 15s lets the buffer regrow enough for the
# 5-word dedup window to miss real duplication -- using the Day-16-informed
# value rather than the original buggy default for this test.

CLIPS = [
    ("en_fast (Day 16, 16.4s)", "research/day16/en_fast.wav", "research/day16/en_fast.txt"),
    ("fast_long (new, 39.5s)", "research/day18_6/fast_long.wav", "research/day18_6/fast_long.txt"),
]


def load_wav_float32(path):
    """
    Reads a 16kHz mono 16-bit PCM WAV file into float32 samples.

    Returns:
        numpy.ndarray: mono samples in the range [-1.0, 1.0].
    """
    with wave.open(path) as wav_file:
        assert wav_file.getframerate() == SAMPLE_RATE
        assert wav_file.getnchannels() == 1
        raw_bytes = wav_file.readframes(wav_file.getnframes())
    return np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0


def run_small_en_whole_segment(model, audio):
    """
    Times and runs the exact production whole-segment decode call.

    Returns:
        tuple[str, float]: (text, seconds elapsed).
    """
    start = time.monotonic()
    words = transcribe_filtered(model, audio, SMALL_EN_BEAM_SIZE)
    elapsed = time.monotonic() - start
    return "".join(w[2] for w in words).strip(), elapsed


def run_base_en_streaming(model, audio):
    """
    Feeds the clip through OnlineASRProcessor in fixed-size chunks (the
    same shape live audio would arrive in), then flushes whatever's left
    tentative at the end -- the "settled" output a viewer would end up
    seeing once the clip stops.

    Returns:
        tuple[str, float]: (settled text, total processing seconds --
        i.e. how long the model spent computing across every chunk, not
        wall-clock-during-real-time-playback).
    """
    processor = OnlineASRProcessor(model, language="en", min_chunk_size=CHUNK_SECONDS,
                                    buffer_trim_sec=BUFFER_TRIM_SECONDS, beam_size=BASE_EN_BEAM_SIZE)
    chunk_size_samples = int(CHUNK_SECONDS * SAMPLE_RATE)
    total_elapsed = 0.0
    committed_words = []
    for offset in range(0, len(audio), chunk_size_samples):
        processor.insert_audio_chunk(audio[offset:offset + chunk_size_samples])
        start = time.monotonic()
        committed, _tentative = processor.process_iter()
        total_elapsed += time.monotonic() - start
        committed_words.extend(committed)
    final = processor.finish()
    committed_words.extend(final)
    text = "".join(w[2] for w in committed_words).strip()
    return text, total_elapsed


def main():
    """
    Runs both code paths against each fast-speech clip and prints the
    ground truth alongside both transcripts for a real side-by-side read.
    """
    print("Loading small.en...")
    small_model = WhisperModel("small.en", device="cpu", compute_type="int8")
    print("Loading base.en...")
    base_model = WhisperModel("base.en", device="cpu", compute_type="int8")

    for name, wav_path, txt_path in CLIPS:
        audio = load_wav_float32(wav_path)
        ground_truth = open(txt_path).read().strip()
        duration = len(audio) / SAMPLE_RATE

        small_text, small_elapsed = run_small_en_whole_segment(small_model, audio)
        base_text, base_elapsed = run_base_en_streaming(base_model, audio)

        print(f"\n{'=' * 70}\nCLIP: {name} ({duration:.1f}s audio)\n{'=' * 70}")
        print(f"ground truth:\n  {ground_truth}")
        print(f"\nsmall.en whole-segment ({small_elapsed:.2f}s compute, "
              f"shown only after the full {duration:.1f}s + pause):\n  {small_text}")
        print(f"\nbase.en streaming/settled ({base_elapsed:.2f}s total compute across "
              f"{CHUNK_SECONDS}s-cadence chunks, live text visible throughout):\n  {base_text}")


if __name__ == "__main__":
    main()
