# Day 16 — Real-Time Transcript Feasibility Spike

Research only, nothing here is wired into the running app. See `docs/DEV_PLAN.md` (Day 16) and
`project_notes.md` for the full writeup. This file is the raw-numbers reference the writeup
summarizes.

## What's here

- `streaming_asr.py` — a minimal reimplementation of whisper_streaming's LocalAgreement-2 policy
  (`HypothesisBuffer` + `OnlineASRProcessor`), built directly against `faster-whisper`.
- `benchmark.py` — drives `streaming_asr.py` against a real WAV file in fixed-size chunks
  (simulating live audio arrival) and reports per-chunk processing time, first-glimpse/commit
  latency per word, and a revision-rate count.
- `microbench.py` — isolates `model.transcribe()`'s own per-call cost from the streaming loop's
  bookkeeping and this dev environment's CPU jitter, by timing fixed-length buffer slices
  directly, 3 repeats each.
- `model_compare.py` — single-shot (non-streaming) transcription across checkpoints, for the
  multilingual/Taglish question.
- `en_normal.wav`, `en_fast.wav`, `taglish.wav` — synthetic test audio (Windows SAPI TTS,
  16kHz mono, generated via `tts_render.ps1`-equivalent interop). `en_fast.wav` uses SAPI Rate=+6.
  **Caveat that matters for the Taglish numbers below:** `taglish.wav` is an English SAPI voice
  reading Taglish text — it has code-switched *vocabulary* but not authentic Filipino *phonetics*.
  It's a reasonable proxy for testing model capacity, but likely overstates how garbled a real
  Filipino-accented speaker would sound, and understates how much a multilingual checkpoint's
  actual Filipino training data would help. Not a substitute for real recorded Taglish speech.

## Micro-benchmark: per-call decode cost by model/beam/buffer length

Isolated `model.transcribe()` calls, no streaming loop, n=3 repeats, on `en_normal.wav` slices.

| model    | beam | 1s buf | 5s buf | 10s buf | 15s buf |
|----------|------|--------|--------|---------|---------|
| tiny.en  | 1    | 0.29s  | 0.37s  | 0.38s   | 0.44s   |
| tiny.en  | 5    | 0.30s  | 0.39s  | 0.51s   | 0.52s   |
| base.en  | 1    | 0.50s  | 0.69s  | 0.83s   | 0.84s   |
| base.en  | 5    | 2.96s* | 0.68s  | 1.35s   | 1.39s   |
| small.en | 1    | 1.85s  | 1.92s  | 2.17s   | 2.32s   |
| small.en | 5    | 1.72s  | 1.94s  | 2.64s   | 2.64s   |

\* likely a first-call warmup outlier, not representative.

Takeaway: cost is dominated by a **fixed per-call floor**, not buffer length — growing the buffer
from 1s to 15s only adds ~25-40% on top of the floor. `small.en` (the app's current default) has
an ~1.7-2.3s floor regardless of beam size, meaning it structurally cannot sustain re-decoding
faster than roughly every 2.5s. `base.en` at beam=1 has a ~0.5-0.8s floor, sustaining roughly a
1.5-2s cadence. `tiny.en` is the only model with a floor comfortably under 1s.

## Full streaming-loop test: 1s chunk cadence, `en_fast.wav` (16.4s clip)

| model    | beam | avg proc/iter | max proc/iter | avg RTF | first-glimpse avg/median | commit latency avg/median | revision rate |
|----------|------|----------------|----------------|---------|---------------------------|----------------------------|----------------|
| small.en | 5    | 5.45s          | 10.64s         | 5.4x    | n/a (see note)            | 14.6s / 15.9s              | 78%            |
| small.en | 1    | 3.54s          | 15.42s         | 3.5x    | 6.5s / 4.7s               | 10.1s / 10.0s              | 68%            |
| base.en  | 1    | 2.03s          | 4.91s          | 2.0x    | 2.3s / 2.0s               | 4.0s / 3.7s                | 88%            |
| tiny.en  | 1    | 3.66s          | 6.18s          | 3.7x    | 4.8s / 5.0s               | 3.1s / 2.1s                | 95%            |

RTF = realtime factor (proc time / 1s chunk); every configuration fell behind real-time on every
iteration in this test (`n_iterations_fell_behind_realtime` was 17/17 in every run).

`small.en` at beam=5's transcript came out with entire sentences duplicated 2-3x — a real bug,
not noise (see "Buffer-trim bug" below). Its first-glimpse latency isn't reported because the
duplication makes position-based word alignment meaningless for that run.

`tiny.en`'s numbers are noisier and don't beat `base.en` here despite winning the micro-benchmark
cleanly — consistent with this project's known CPU-contention/scheduling jitter in this dev
environment (see PRD §9 / Day 10's finding on `claude` CLI latency outliers). The micro-benchmark
numbers above are the more trustworthy signal for model selection; the full-loop numbers are more
trustworthy for the "does this feel real-time" and "how much do words flicker" questions.

## Buffer-trim bug found in the local-agreement approach itself

Not just an implementation slip — a real property of the technique. Whisper re-transcribes the
*entire* current buffer on every iteration (no incremental/cached decoding in faster-whisper).
The algorithm dedupes by matching up to 5 trailing/leading words between the already-committed
text and the new hypothesis. That's sufficient *only* if the buffer gets trimmed back close to
the last committed word reasonably often. On a short clip where the buffer never grows past the
15s trim threshold, the new hypothesis re-includes the *entire* transcript every time, the 5-word
window can't dedupe that much overlap, and duplicate sentences get committed. This isn't a
one-off short-clip artifact either — in a real long meeting, this same regrowth-then-trim cycle
repeats every ~15s throughout the session, so the failure mode recurs periodically, not just at
the start. Needs a shorter trim threshold (tested informally down to a few seconds) and/or
sentence-boundary trimming before Day 17 implementation, plus a guard against committing a
hallucinated repeat from an unstable early-buffer hypothesis (seen once with `small.en` beam=1 --
a phrase repeated itself in a hypothesis that then got locked in as committed).

## Multilingual/Taglish comparison (single-shot, cached model load)

| model               | language mode        | transcribe time (35.5s clip) | quality                                                             |
|----------------------|-----------------------|-------------------------------|----------------------------------------------------------------------|
| small.en             | forced `en`           | 6.4s                          | garbled ("Jung", "Natan", "S.I." for "Si")                          |
| small (multilingual) | forced `en`           | 6.8s                          | garbled, nearly identical to small.en                                |
| small (multilingual) | auto-detect (→ `en`)  | 7.9s                          | garbled, nearly identical                                            |
| medium                | auto-detect (→ `en`)  | 21.6s                         | clearly better ("Yung", "Si Maria", "tapos Si John", "kasi") — still imperfect, but a real step up |

English accuracy/latency parity check (46s clean English clip): `small.en` and multilingual
`small` produced **byte-identical transcripts** at effectively the same latency (6.72s vs 6.77s).
No measured cost to switching the app's default checkpoint from `small.en` to multilingual
`small`.

Model load time: first-ever load downloads weights from Hugging Face (70s for `small`, ~207s for
`medium`, one-time only). Cached load (every load after) is fast for all sizes tested: 0.8-3.0s.
