"""
Follow-up diagnostic for track_a_segmentation.py's headline finding: short
forced sub-segment thresholds (8s/12s) caused real content loss on
fast_long.wav, not just minor mid-word roughness. This confirms *why* --
prints each closed segment's raw no_speech_prob/avg_logprob (bypassing
transcribe_filtered's drop logic) for the 8s threshold run, so a segment
that silently vanished can be attributed to a specific filter rather than
guessed at.

Usage: .venv/bin/python3 research/day20/track_a_diagnose_boundary_loss.py
"""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "research/day20")
from faster_whisper import WhisperModel

from track_a_segmentation import TestSegmenter, load_wav_bytes, pcm_bytes_to_float32
from streaming_transcriber import (
    NO_SPEECH_PROBABILITY_THRESHOLD,
    AVG_LOGPROB_THRESHOLD,
    SILENCE_AMPLITUDE_THRESHOLD,
)
import numpy as np

TRANSCRIBE_BEAM_SIZE = 5


def main():
    """Re-segments fast_long.wav at the 8s threshold and prints each segment's raw Whisper confidence scores, unfiltered."""
    print("Loading small.en...")
    model = WhisperModel("small.en", device="cpu", compute_type="int8")

    audio_bytes = load_wav_bytes("research/day18_6/fast_long.wav")
    segmenter = TestSegmenter(8.0)
    closed = list(segmenter.add_audio(audio_bytes))
    tail = segmenter.close_open_segment()
    if tail is not None:
        closed.append(tail)

    for i, segment in enumerate(closed):
        audio = pcm_bytes_to_float32(segment.audio_bytes)
        print(f"\n=== segment {i}: {segment.started_at_seconds:.1f}s - {segment.ended_at_seconds:.1f}s "
              f"({len(audio) / 16000:.2f}s audio, peak amplitude {np.abs(audio).max():.4f}) ===")
        if audio.size == 0 or np.abs(audio).max() < SILENCE_AMPLITUDE_THRESHOLD:
            print("  -> dropped before decode: below SILENCE_AMPLITUDE_THRESHOLD")
            continue
        segments, _info = model.transcribe(
            audio, language="en", word_timestamps=True,
            condition_on_previous_text=False, vad_filter=False, beam_size=TRANSCRIBE_BEAM_SIZE,
        )
        for whisper_segment in segments:
            no_speech = whisper_segment.no_speech_prob
            avg_logprob = whisper_segment.avg_logprob
            dropped_reasons = []
            if no_speech > NO_SPEECH_PROBABILITY_THRESHOLD:
                dropped_reasons.append(f"no_speech_prob {no_speech:.3f} > {NO_SPEECH_PROBABILITY_THRESHOLD}")
            if avg_logprob < AVG_LOGPROB_THRESHOLD:
                dropped_reasons.append(f"avg_logprob {avg_logprob:.3f} < {AVG_LOGPROB_THRESHOLD}")
            status = f"DROPPED ({'; '.join(dropped_reasons)})" if dropped_reasons else "kept"
            print(f"  raw text: {whisper_segment.text!r}")
            print(f"  no_speech_prob={no_speech:.3f}  avg_logprob={avg_logprob:.3f}  -> {status}")


if __name__ == "__main__":
    main()
