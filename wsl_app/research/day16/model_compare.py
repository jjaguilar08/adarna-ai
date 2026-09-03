"""
Day 16 spike: single-shot (non-streaming) transcription comparison across
Whisper checkpoints -- answers the Day 12 open question (does a multilingual
checkpoint meaningfully help real Filipino/Taglish code-switched audio, and
what does it cost in latency/English accuracy) independent of the streaming
question above.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from main import load_whisper_model  # noqa: E402 -- needs the sys.path insert above

from benchmark import load_wav_as_float32


def transcribe_once(model, audio, language, beam_size=5):
    """Runs one full-buffer transcribe() call and returns (text, elapsed_seconds, detected_language)."""
    t0 = time.perf_counter()
    segments, info = model.transcribe(
        audio, language=language, beam_size=beam_size, condition_on_previous_text=True,
    )
    text = "".join(seg.text for seg in segments).strip()
    elapsed = time.perf_counter() - t0
    return text, elapsed, getattr(info, "language", language)


def main():
    """Runs each (model, language-mode) pair against a wav file and prints results."""
    wav_path = sys.argv[1]
    audio = load_wav_as_float32(wav_path)

    configs = [
        ("small.en", "en"),
        ("small", "en"),
        ("small", None),  # auto-detect
        ("medium", None),  # auto-detect, multilingual only
    ]

    for model_size, language in configs:
        print(f"\n=== {model_size} (language={language or 'auto-detect'}) ===", file=sys.stderr)
        t_load0 = time.perf_counter()
        model = load_whisper_model(model_size)
        load_time = time.perf_counter() - t_load0
        text, elapsed, detected_lang = transcribe_once(model, audio, language)
        print(f"load={load_time:.2f}s  transcribe={elapsed:.2f}s  detected_lang={detected_lang}", file=sys.stderr)
        print(text)
        del model


if __name__ == "__main__":
    main()
