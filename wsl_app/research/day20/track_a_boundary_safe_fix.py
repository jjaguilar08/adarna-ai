"""
Tests a specific fix for the content-loss bug track_a_diagnose_boundary_loss.py
confirmed: a forced (non-pause) segment boundary produces real, correctly-
transcribed speech that NO_SPEECH_PROBABILITY_THRESHOLD spuriously drops,
because that filter's score reacts to "this chunk has no natural utterance
boundary" almost as much as to genuine silence -- a false-positive class
this filter was never tuned for (it was tuned for real silence at session-
stop, see streaming_transcriber.py). Since the segmenter already knows
whether a given close was a real pause or a forced timeout, a forced
segment can safely skip the no_speech_prob check (avg_logprob and the
silence-amplitude gate still apply -- neither was the culprit here, and
both still catch genuine hallucination).

Usage: .venv/bin/python3 research/day20/track_a_boundary_safe_fix.py
"""
import sys
import time
from pathlib import Path
from typing import NamedTuple

import numpy as np
from faster_whisper import WhisperModel

sys.path.insert(0, ".")
sys.path.insert(0, "research/day20")
from streaming_transcriber import (
    AVG_LOGPROB_THRESHOLD,
    SILENCE_AMPLITUDE_THRESHOLD,
    drop_immediate_repeated_phrase,
)
from track_a_segmentation import TestSegmenter, load_wav_bytes, pcm_bytes_to_float32
from wer import word_error_rate

TRANSCRIBE_BEAM_SIZE = 5


class ForcedAwareSegmenter(TestSegmenter):
    """Same as TestSegmenter, but each returned segment also says whether it closed on a real pause or a forced timeout."""

    def _close(self):
        was_forced = self._seconds_recorded_in_current_segment >= self._max_segment_seconds and \
            self._seconds_of_silence_in_a_row < 0.4  # SILENCE_SECONDS_TO_CLOSE_SEGMENT
        segment = super()._close()
        return segment, was_forced


def transcribe_boundary_safe(model, audio, beam_size, skip_no_speech_filter):
    """
    Same as transcribe_filtered(), except the no_speech_prob check is
    skipped when skip_no_speech_filter is True (used for forced-boundary
    segments only) -- avg_logprob and the silence-amplitude gate still
    apply unconditionally.

    Returns:
        list[(start, end, text)]
    """
    if audio.size == 0 or np.abs(audio).max() < SILENCE_AMPLITUDE_THRESHOLD:
        return []
    segments, _info = model.transcribe(
        audio, language="en", word_timestamps=True,
        condition_on_previous_text=False, vad_filter=False, beam_size=beam_size,
    )
    words = []
    for segment in segments:
        if not skip_no_speech_filter and segment.no_speech_prob > 0.6:
            continue
        if segment.avg_logprob < AVG_LOGPROB_THRESHOLD:
            continue
        if segment.words:
            for word in segment.words:
                if word.word.strip():
                    words.append((word.start, word.end, word.word))
    return drop_immediate_repeated_phrase(words)


def run(model, audio_bytes, max_segment_seconds):
    """Segments audio_bytes at max_segment_seconds, transcribing forced-close segments with the no_speech_prob filter skipped."""
    segmenter = ForcedAwareSegmenter(max_segment_seconds)
    closed = list(segmenter.add_audio(audio_bytes))
    tail = segmenter.close_open_segment()
    if tail is not None:
        closed.append(tail)

    texts = []
    for segment, was_forced in closed:
        audio = pcm_bytes_to_float32(segment.audio_bytes)
        words = transcribe_boundary_safe(model, audio, TRANSCRIBE_BEAM_SIZE, skip_no_speech_filter=was_forced)
        text = "".join(w[2] for w in words).strip()
        texts.append((text, was_forced))
    return texts


CLIPS = [
    ("en_normal", "research/day16/en_normal.wav", "research/day16/en_normal.txt"),
    ("en_fast", "research/day16/en_fast.wav", "research/day16/en_fast.txt"),
    ("zira_clip", "research/day18_5/zira_clip.wav", "research/day18_5/zira_clip.txt"),
    ("fast_long", "research/day18_6/fast_long.wav", "research/day18_6/fast_long.txt"),
]


def main():
    """Re-runs the 8s and 12s thresholds on every test clip with the fix applied, comparing WER before/after."""
    print("Loading small.en...")
    model = WhisperModel("small.en", device="cpu", compute_type="int8")

    for name, wav_path, txt_path in CLIPS:
        audio_bytes = load_wav_bytes(wav_path)
        ground_truth = Path(txt_path).read_text().strip()
        for threshold in [8.0, 12.0]:
            texts = run(model, audio_bytes, threshold)
            full_text = " ".join(t for t, _ in texts if t)
            wer, edits, ref_words = word_error_rate(ground_truth, full_text)
            forced_count = sum(1 for _, forced in texts if forced)
            print(f"\n=== {name}, {threshold:.0f}s threshold, boundary-safe fix ===")
            print(f"segments: {len(texts)}  forced closes: {forced_count}  WER: {wer:.1%} ({edits}/{ref_words} words)")
            print(f"text: {full_text}")


if __name__ == "__main__":
    main()
