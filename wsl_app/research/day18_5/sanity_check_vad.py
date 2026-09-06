"""
Day 18.5 sanity check #2: is the current webrtcvad/segmenting configuration
(SPEECH_DETECTION_STRICTNESS=2, SILENCE_SECONDS_TO_CLOSE_SEGMENT=0.4) a
plausible cause of the accuracy complaint, independent of model size?

Method: feed a real, clean test clip through the REAL production
VoiceSegmenter (same 30ms-frame loop, same silence-close threshold) exactly
as audio_chunk messages would arrive, exactly like the actual app does, and
see how many segments it produces and where the cuts land. Then transcribe
each of those real segments with small.en (same beam_size, same
transcribe_filtered call as production), concatenate the results, and
compare against decoding the entire clip in one uncut pass. If VAD
segmentation itself were introducing real damage (e.g. slicing a sentence
mid-word because a natural sentence pause is close to but not quite the
0.4s threshold), the segmented-and-concatenated transcript should visibly
diverge from the single-shot one -- same audio, same model, only the
cut points differ.
"""
import sys
import wave

import numpy as np
from faster_whisper import WhisperModel

sys.path.insert(0, ".")
from main import VoiceSegmenter, TARGET_SAMPLE_RATE
from streaming_transcriber import transcribe_filtered

CLIP_PATH = "research/day16/en_normal.wav"
BEAM_SIZE = 5
# Matches production's AudioCaptureManager: audio arrives in ~100ms chunks,
# not one giant blob -- exercising the real chunk-by-chunk buffering path
# in VoiceSegmenter.add_audio(), not just handing it the whole clip at once.
SIMULATED_CHUNK_SECONDS = 0.1


def load_wav_bytes(path):
    """
    Reads a WAV file's raw 16-bit PCM bytes and its sample rate.

    Returns:
        tuple[bytes, int]: (raw PCM bytes, sample rate).
    """
    with wave.open(path) as wav_file:
        return wav_file.readframes(wav_file.getnframes()), wav_file.getframerate()


def run_through_real_segmenter(audio_bytes):
    """
    Feeds audio through the actual production VoiceSegmenter in small
    chunks, the same way handle_audio_chunk() does, and collects every
    segment it closes.

    Returns:
        list[bytes]: each closed segment's raw 16-bit PCM audio.
    """
    segmenter = VoiceSegmenter()
    chunk_size_bytes = int(TARGET_SAMPLE_RATE * SIMULATED_CHUNK_SECONDS) * 2
    segments = []
    for offset in range(0, len(audio_bytes), chunk_size_bytes):
        chunk = audio_bytes[offset:offset + chunk_size_bytes]
        segments.extend(segmenter.add_audio(chunk))
    final_segment = segmenter.close_open_segment()
    if final_segment is not None:
        segments.append(final_segment)
    return segments


def transcribe_pcm(model, pcm_bytes):
    """
    Runs the same transcribe_filtered() call production uses, on raw
    16-bit PCM bytes.

    Returns:
        str: transcribed text.
    """
    int16_samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    audio_float = int16_samples.astype(np.float32) / 32768.0
    words = transcribe_filtered(model, audio_float, BEAM_SIZE)
    return "".join(word[2] for word in words).strip()


def main():
    """
    Compares small.en decoding the clip as one uncut segment against
    small.en decoding the same clip after being split by the real
    production VoiceSegmenter.
    """
    audio_bytes, sample_rate = load_wav_bytes(CLIP_PATH)
    assert sample_rate == TARGET_SAMPLE_RATE

    segments = run_through_real_segmenter(audio_bytes)
    print(f"VAD produced {len(segments)} segment(s) from a {len(audio_bytes) / 2 / TARGET_SAMPLE_RATE:.1f}s clip:")
    for i, segment in enumerate(segments):
        print(f"  segment {i}: {len(segment) / 2 / TARGET_SAMPLE_RATE:.2f}s")

    model = WhisperModel("small.en", device="cpu", compute_type="int8")

    single_shot_text = transcribe_pcm(model, audio_bytes)
    segmented_texts = [transcribe_pcm(model, segment) for segment in segments]
    segmented_joined = " ".join(t for t in segmented_texts if t)

    print(f"\nsingle-shot (no VAD cuts):     {single_shot_text}")
    print(f"\nsegmented-then-concatenated:   {segmented_joined}")


if __name__ == "__main__":
    main()
