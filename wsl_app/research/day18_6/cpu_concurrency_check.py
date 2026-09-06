"""
Day 18.6 CPU-concurrency check: if base.en streaming runs continuously in a
background thread (as an ephemeral live preview would, while a segment is
still open) at the same time small.en is doing its own whole-segment
decode (a DIFFERENT segment that just closed), do the two meaningfully
compete for CPU on this machine?

This matters because both would really run concurrently in production:
wsl_app is single-process, and both transcribe() calls go through
asyncio.to_thread -- i.e. two real OS threads in the same process, same as
this script's threading.Thread setup, not multiprocessing. If they
contend, a live preview would make small.en's own segment turnaround
(which the suggestion pipeline is downstream of) measurably slower, not
just add a "free" feature alongside it.

Method: measure small.en decoding a segment (a) completely alone, and (b)
while a base.en streaming loop is continuously chewing through fast_long.wav
in the background at the real ~1.5s cadence, for a stretch of wall-clock
time that fully overlaps small.en's decode. Compare the two small.en
timings directly.
"""
import sys
import threading
import time
import wave

import numpy as np
from faster_whisper import WhisperModel

sys.path.insert(0, ".")
sys.path.insert(0, "research/day16")
from streaming_transcriber import transcribe_filtered
from streaming_asr import OnlineASRProcessor, SAMPLE_RATE

BEAM_SIZE = 5
CHUNK_SECONDS = 1.5
SMALL_EN_CLIP = "research/day18_5/zira_clip.wav"  # a fresh, different clip
BASE_EN_CLIP = "research/day18_6/fast_long.wav"


def load_wav_float32(path):
    """
    Reads a 16kHz mono 16-bit PCM WAV file into float32 samples.

    Returns:
        numpy.ndarray: mono samples in the range [-1.0, 1.0].
    """
    with wave.open(path) as wav_file:
        raw_bytes = wav_file.readframes(wav_file.getnframes())
    return np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0


def run_base_en_streaming_loop_for(model, audio, stop_event):
    """
    Runs the base.en streaming loop repeatedly over `audio` (looping back
    to the start if it finishes) until stop_event is set -- simulates a
    live session's ongoing background preview work for as long as needed
    to fully overlap whatever's being measured in the main thread.
    """
    processor = OnlineASRProcessor(model, language="en", min_chunk_size=CHUNK_SECONDS,
                                    buffer_trim_sec=8.0, beam_size=BEAM_SIZE)
    chunk_size_samples = int(CHUNK_SECONDS * SAMPLE_RATE)
    offset = 0
    while not stop_event.is_set():
        chunk = audio[offset:offset + chunk_size_samples]
        if len(chunk) == 0:
            offset = 0
            continue
        processor.insert_audio_chunk(chunk)
        processor.process_iter()
        offset += chunk_size_samples


def time_small_en_decode(model, audio):
    """
    Times one whole-segment small.en decode, the same call production uses.

    Returns:
        float: seconds elapsed.
    """
    start = time.monotonic()
    transcribe_filtered(model, audio, BEAM_SIZE)
    return time.monotonic() - start


def main():
    """
    Measures small.en's segment-decode time alone, then again while
    base.en streaming runs concurrently in a background thread, and
    reports the slowdown.
    """
    print("Loading models...")
    small_model = WhisperModel("small.en", device="cpu", compute_type="int8")
    base_model = WhisperModel("base.en", device="cpu", compute_type="int8")

    small_audio = load_wav_float32(SMALL_EN_CLIP)
    base_audio = load_wav_float32(BASE_EN_CLIP)

    print("\n--- Baseline: small.en decoding alone ---")
    baseline_times = [time_small_en_decode(small_model, small_audio) for _ in range(3)]
    print(f"small.en alone: {[f'{t:.2f}s' for t in baseline_times]}")

    print("\n--- Concurrent: small.en decoding while base.en streams continuously ---")
    concurrent_times = []
    for _ in range(3):
        stop_event = threading.Event()
        bg_thread = threading.Thread(target=run_base_en_streaming_loop_for,
                                      args=(base_model, base_audio, stop_event), daemon=True)
        bg_thread.start()
        time.sleep(1.0)  # let the background loop actually get going first
        elapsed = time_small_en_decode(small_model, small_audio)
        concurrent_times.append(elapsed)
        stop_event.set()
        bg_thread.join(timeout=5.0)
    print(f"small.en concurrent: {[f'{t:.2f}s' for t in concurrent_times]}")

    avg_baseline = sum(baseline_times) / len(baseline_times)
    avg_concurrent = sum(concurrent_times) / len(concurrent_times)
    print(f"\naverage alone:      {avg_baseline:.2f}s")
    print(f"average concurrent: {avg_concurrent:.2f}s")
    print(f"slowdown factor:    {avg_concurrent / avg_baseline:.2f}x")


if __name__ == "__main__":
    main()
