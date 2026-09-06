"""
Day 18.5 sanity check #1: is the 48kHz-stereo -> 16kHz-mono downsample step
(convert_audio_to_common_format() in wsl_app/main.py) a plausible cause of
the accuracy complaint, independent of model size?

Method: take a clean 16kHz mono test clip (already ground-truthed), fake it
up into the same shape real captured audio arrives in (48kHz, stereo,
float32 -- see windows_app's paFloat32 capture and the audio_chunk message
format), run it through the REAL production convert_audio_to_common_format()
function to bring it back down to 16kHz mono, and compare small.en's
transcription of the round-tripped audio against its transcription of the
untouched original. If downsampling were introducing real signal damage,
the round-tripped transcript should visibly degrade relative to the
original -- same model, same audio content, only the resample round-trip
differs.
"""
import sys
import wave

import numpy as np
from faster_whisper import WhisperModel

sys.path.insert(0, ".")
from main import convert_audio_to_common_format
from streaming_transcriber import transcribe_filtered

CLIP_PATH = "research/day16/en_normal.wav"
BEAM_SIZE = 5


def load_wav_int16(path):
    """
    Reads a WAV file's raw samples as int16, along with its sample rate.

    Returns:
        tuple[numpy.ndarray, int]: (int16 samples, sample rate).
    """
    with wave.open(path) as wav_file:
        raw_bytes = wav_file.readframes(wav_file.getnframes())
        sample_rate = wav_file.getframerate()
    return np.frombuffer(raw_bytes, dtype=np.int16), sample_rate


def upsample_to_fake_48k_stereo(samples_16k, source_rate):
    """
    Turns a clean 16kHz mono clip into a synthetic stand-in for what
    windows_app actually captures and sends over the wire: 48kHz, 2-channel
    (both channels identical -- a real stereo capture isn't perfectly
    identical L/R either, but that's not the thing being tested here),
    float32 samples in [-1.0, 1.0] (paFloat32, matching decode_audio_chunk's
    own output shape, which is what convert_audio_to_common_format actually
    receives in production -- not raw int16 bytes). Naive repeat-based
    upsampling (48000/16000 = exactly 3x) rather than a proper resampling
    filter, since the point is only to exercise the real production
    downsample function on a round trip, not to simulate realistic
    upsampling artifacts.

    Returns:
        numpy.ndarray: interleaved stereo float32 samples at 48kHz.
    """
    assert source_rate == 16000
    float_samples = samples_16k.astype(np.float32) / 32768.0
    upsampled = np.repeat(float_samples, 3)
    stereo = np.repeat(upsampled, 2)  # interleave L/R, both channels equal
    return stereo


def transcribe_pcm(model, int16_samples):
    """
    Runs the same transcribe_filtered() call production uses, on raw int16
    samples.

    Returns:
        str: transcribed text.
    """
    audio_float = int16_samples.astype(np.float32) / 32768.0
    words = transcribe_filtered(model, audio_float, BEAM_SIZE)
    return "".join(word[2] for word in words).strip()


def main():
    """
    Compares small.en's transcription of the original clip against its
    transcription of the same clip after a round trip through the real
    48kHz-stereo -> 16kHz-mono downsample function.
    """
    original_samples, source_rate = load_wav_int16(CLIP_PATH)
    fake_48k_stereo_samples = upsample_to_fake_48k_stereo(original_samples, source_rate)
    round_tripped_bytes = convert_audio_to_common_format(fake_48k_stereo_samples, 48000, 2)
    round_tripped_samples = np.frombuffer(round_tripped_bytes, dtype=np.int16)

    print(f"original sample count:      {len(original_samples)}")
    print(f"round-tripped sample count: {len(round_tripped_samples)}")

    model = WhisperModel("small.en", device="cpu", compute_type="int8")
    original_text = transcribe_pcm(model, original_samples)
    round_tripped_text = transcribe_pcm(model, round_tripped_samples)

    print(f"\noriginal transcript:       {original_text}")
    print(f"round-tripped transcript:  {round_tripped_text}")
    print(f"\nidentical: {original_text == round_tripped_text}")


if __name__ == "__main__":
    main()
