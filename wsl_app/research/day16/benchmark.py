"""
Day 16 spike: drives streaming_asr.OnlineASRProcessor against a real WAV file
and reports empirical numbers -- per-chunk processing time (can we keep up
with real-time?), first-glimpse and commit latency per word (how fast do
partial words appear, relative to when they were actually spoken), and a
revision count (how often an early tentative word turns out wrong before it
locks in).

Usage:
    .venv/bin/python research/day16/benchmark.py <wav_file> <model_size> [language] [chunk_sec]

Example:
    .venv/bin/python research/day16/benchmark.py research/day16/en_normal.wav small.en en 1.0
"""
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from main import load_whisper_model  # noqa: E402 -- needs the sys.path insert above

from streaming_asr import SAMPLE_RATE, OnlineASRProcessor


def load_wav_as_float32(path):
    """
    Reads a 16kHz mono 16-bit PCM WAV file into a float32 array in [-1, 1],
    the format faster-whisper expects.

    Returns:
        np.ndarray: float32 samples.
    """
    with wave.open(path, "rb") as wav_file:
        assert wav_file.getframerate() == SAMPLE_RATE, "expected 16kHz audio"
        assert wav_file.getnchannels() == 1, "expected mono audio"
        assert wav_file.getsampwidth() == 2, "expected 16-bit PCM"
        raw = wav_file.readframes(wav_file.getnframes())
    int16 = np.frombuffer(raw, dtype=np.int16)
    return int16.astype(np.float32) / 32768.0


def run_streaming_benchmark(wav_path, model_size, language, chunk_sec,
                             beam_size=5, buffer_trim_sec=15.0):
    """
    Streams a WAV file through OnlineASRProcessor in fixed-size chunks
    (simulating live audio arrival) and records per-iteration timing plus
    per-word first-glimpse/commit latency and revision counts.

    Returns:
        dict: summary stats, plus the raw per-word and per-iteration records
        for closer inspection.
    """
    load_t0 = time.perf_counter()
    model = load_whisper_model(model_size)
    load_time = time.perf_counter() - load_t0
    print(f"Model loaded in {load_time:.2f}s", file=sys.stderr)

    audio = load_wav_as_float32(wav_path)
    total_audio_sec = len(audio) / SAMPLE_RATE

    processor = OnlineASRProcessor(
        model, language=language, min_chunk_size=chunk_sec,
        buffer_trim_sec=buffer_trim_sec, beam_size=beam_size,
    )

    chunk_samples = int(chunk_sec * SAMPLE_RATE)
    pos = 0
    iterations = []
    # position (cumulative committed word count) -> list of (iteration_idx, text) seen while tentative
    tentative_sightings = {}
    # position -> wall-clock seconds (from stream start, assuming real-time chunk
    # arrival and no backlog) at which it was first shown as tentative
    first_glimpse_available_at = {}

    committed_count = 0
    all_committed = []

    iteration_idx = 0
    while pos < len(audio):
        chunk = audio[pos:pos + chunk_samples]
        pos += chunk_samples
        processor.insert_audio_chunk(chunk)

        proc_t0 = time.perf_counter()
        committed, tentative = processor.process_iter()
        proc_time = time.perf_counter() - proc_t0
        # wall time this iteration's result would realistically be available:
        # when the chunk's audio finished arriving (real-time pace) plus how
        # long processing took. Understates latency for iterations after a
        # real-time-factor > 1 backlog -- see n_iterations_fell_behind_realtime.
        audio_time_sec = pos / SAMPLE_RATE
        available_at = audio_time_sec + proc_time

        for j, w in enumerate(tentative):
            position = committed_count + j
            tentative_sightings.setdefault(position, []).append((iteration_idx, w[2]))
            if position not in first_glimpse_available_at:
                first_glimpse_available_at[position] = available_at

        for w in committed:
            all_committed.append(w)
        committed_count += len(committed)

        iterations.append({
            "iteration": iteration_idx,
            "audio_time_sec": audio_time_sec,
            "available_at_sec": available_at,
            "buffer_len_sec": processor.audio_buffer.shape[0] / SAMPLE_RATE,
            "proc_time_sec": proc_time,
            "n_committed_this_iter": len(committed),
            "n_tentative": len(tentative),
        })
        iteration_idx += 1

    # flush whatever's left at end of audio
    final_words = processor.finish()
    all_committed.extend(final_words)

    # revision counting: for each final committed position, how many distinct
    # tentative texts did it show before locking in (excluding the final one)?
    revised_positions = 0
    total_positions_with_tentative_history = 0
    for position, sightings in tentative_sightings.items():
        if position >= len(all_committed):
            continue
        final_text = all_committed[position][2]
        seen_texts = [t for _, t in sightings]
        total_positions_with_tentative_history += 1
        distinct_wrong = {t for t in seen_texts if t != final_text}
        if distinct_wrong:
            revised_positions += 1

    # commit latency: each committed word's own start time in the audio vs.
    # the realistic wall-clock time (real-time chunk arrival + this
    # iteration's processing) at which it became committed.
    commit_latencies = []
    cumulative = 0
    for it in iterations:
        n = it["n_committed_this_iter"]
        if n:
            words_committed_here = all_committed[cumulative:cumulative + n]
            for w in words_committed_here:
                word_start_audio_sec = w[0]
                commit_latencies.append(it["available_at_sec"] - word_start_audio_sec)
            cumulative += n

    # first-glimpse latency: each word's own start time vs. the realistic
    # wall-clock time at which it first appeared as a tentative guess.
    first_glimpse_latencies = []
    for position, available_at in first_glimpse_available_at.items():
        if position < len(all_committed):
            word_start_audio_sec = all_committed[position][0]
            first_glimpse_latencies.append(available_at - word_start_audio_sec)

    proc_times = [it["proc_time_sec"] for it in iterations]
    realtime_factor = [it["proc_time_sec"] / chunk_sec for it in iterations]

    summary = {
        "wav_path": wav_path,
        "model_size": model_size,
        "language": language,
        "chunk_sec": chunk_sec,
        "total_audio_sec": total_audio_sec,
        "model_load_time_sec": load_time,
        "n_iterations": len(iterations),
        "n_words_committed": len(all_committed),
        "avg_proc_time_sec": float(np.mean(proc_times)) if proc_times else 0.0,
        "max_proc_time_sec": float(np.max(proc_times)) if proc_times else 0.0,
        "min_proc_time_sec": float(np.min(proc_times)) if proc_times else 0.0,
        "avg_realtime_factor": float(np.mean(realtime_factor)) if realtime_factor else 0.0,
        "max_realtime_factor": float(np.max(realtime_factor)) if realtime_factor else 0.0,
        "n_iterations_fell_behind_realtime": int(sum(1 for r in realtime_factor if r > 1.0)),
        "avg_commit_latency_sec": float(np.mean(commit_latencies)) if commit_latencies else None,
        "median_commit_latency_sec": float(np.median(commit_latencies)) if commit_latencies else None,
        "max_commit_latency_sec": float(np.max(commit_latencies)) if commit_latencies else None,
        "avg_first_glimpse_latency_sec": float(np.mean(first_glimpse_latencies)) if first_glimpse_latencies else None,
        "median_first_glimpse_latency_sec": float(np.median(first_glimpse_latencies)) if first_glimpse_latencies else None,
        "positions_with_tentative_history": total_positions_with_tentative_history,
        "positions_revised": revised_positions,
        "revision_rate": (revised_positions / total_positions_with_tentative_history) if total_positions_with_tentative_history else 0.0,
        "final_transcript": "".join(w[2] for w in all_committed).strip(),
    }
    return summary, iterations


def main():
    """Runs one streaming benchmark configuration from command-line args and prints a JSON summary."""
    import json

    wav_path = sys.argv[1]
    model_size = sys.argv[2]
    language = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "auto" else None
    chunk_sec = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
    beam_size = int(sys.argv[5]) if len(sys.argv) > 5 else 5
    buffer_trim_sec = float(sys.argv[6]) if len(sys.argv) > 6 else 15.0

    summary, iterations = run_streaming_benchmark(
        wav_path, model_size, language, chunk_sec, beam_size=beam_size, buffer_trim_sec=buffer_trim_sec,
    )
    print(json.dumps(summary, indent=2))
    print("--- per-iteration proc_time (sec) ---", file=sys.stderr)
    for it in iterations:
        print(f"  iter={it['iteration']:>3} audio_t={it['audio_time_sec']:6.2f} "
              f"buf_len={it['buffer_len_sec']:6.2f} proc={it['proc_time_sec']:6.2f}s "
              f"committed={it['n_committed_this_iter']}", file=sys.stderr)


if __name__ == "__main__":
    main()
