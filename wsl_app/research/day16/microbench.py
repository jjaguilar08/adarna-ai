"""
Day 16 spike: isolates faster-whisper's per-call decode cost from the noisier
full streaming-loop numbers in benchmark.py. Loads each model once, then times
model.transcribe() directly on fixed-length audio slices, repeated a few times
each, to see how cost scales with buffer length, model size, and beam size --
without the streaming loop's own bookkeeping or WSL environment jitter mixed in.
"""
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from main import load_whisper_model  # noqa: E402 -- needs the sys.path insert above

from benchmark import load_wav_as_float32

SAMPLE_RATE = 16000


def time_transcribe(model, audio, beam_size, repeats=3):
    """
    Runs model.transcribe() on `audio` `repeats` times and returns the list
    of wall-clock durations in seconds.
    """
    durations = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        segments, _info = model.transcribe(
            audio, language="en", word_timestamps=True,
            condition_on_previous_text=False, vad_filter=False, beam_size=beam_size,
        )
        list(segments)  # force generator to actually decode
        durations.append(time.perf_counter() - t0)
    return durations


def main():
    """Sweeps model size x beam size x buffer length, printing a plain-text table of timings."""
    wav_path = sys.argv[1] if len(sys.argv) > 1 else "research/day16/en_normal.wav"
    audio = load_wav_as_float32(wav_path)

    model_sizes = ["tiny.en", "base.en", "small.en"]
    buffer_lens_sec = [1, 5, 10, 15]
    beam_sizes = [1, 5]

    print(f"{'model':10} {'beam':4} {'buf_sec':7} {'min':>6} {'median':>6} {'max':>6}  (seconds, n=3)")
    for model_size in model_sizes:
        model = load_whisper_model(model_size)
        for beam_size in beam_sizes:
            for buf_sec in buffer_lens_sec:
                n_samples = int(buf_sec * SAMPLE_RATE)
                if n_samples > len(audio):
                    continue
                clip = audio[:n_samples]
                durations = time_transcribe(model, clip, beam_size)
                print(f"{model_size:10} {beam_size:<4} {buf_sec:<7} "
                      f"{min(durations):6.2f} {statistics.median(durations):6.2f} {max(durations):6.2f}")
        del model


if __name__ == "__main__":
    main()
