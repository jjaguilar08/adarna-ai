# Day 18.5 — STT Accuracy: Model Size Re-Evaluation

Research only, nothing here is wired into the running app (`wsl_app/main.py` still loads
`small.en`, unchanged). See `docs/DEV_PLAN.md` (Day 18.5) and `project_notes.md` for the
handoff prompt and the summary this README backs up.

## What's here

- `benchmark_segment_based.py` — the main comparison. Loads each candidate model once, then
  calls `transcribe_filtered()` (the exact function `wsl_app/main.py`'s `transcribe_segment()`
  calls in production, same `beam_size=5`, same `compute_type="int8"`) once per test clip —
  one whole-segment decode, matching the app's real current architecture, not Day 16's
  repeated-re-decode streaming loop.
- `sanity_check_downsample.py` — rules the 48kHz-stereo→16kHz-mono conversion step in/out as a
  contributing cause.
- `sanity_check_vad.py` — rules webrtcvad's segment-cut placement in/out as a contributing
  cause.
- `zira_clip.wav` / `zira_clip.txt` — a third test clip (Windows SAPI "Zira" voice, different
  from Day 16's clips and from each other in content), added for speaker/content variety.
  `en_normal.wav`/`en_fast.wav` (Day 16's clips) are reused from `../day16/`.

## Correction to the premise this task started from

The task assumed Day 16's "`medium` costs ~3.5x `small`'s latency" finding came from the old
*streaming* architecture (repeated whole-buffer re-decoding) and therefore didn't apply to
today's segment-based (one-decode-per-utterance) architecture. **That's not quite right** —
checking Day 16's own README, that specific 3.5x number came from a *single-shot* batch
comparison (`model_compare.py`, the Taglish/multilingual test), the same one-decode-per-clip
shape this app uses today. It just used `medium` (multilingual) on one Taglish-flavored TTS
clip, not `medium.en` on clean English audio across multiple clips.

So the real gaps worth closing were: no `medium.en` number (only multilingual `medium`), no
distilled-model number at all, and no accuracy comparison focused on clean English. Re-running
was still the right call — just for those reasons, not because the old number was measured
under a since-abandoned architecture. Worth being upfront about this since the original
handoff prompt stated the premise as settled fact.

## Latency results (real, `compute_type="int8"`, CPU-only, this dev machine)

One `transcribe_filtered()` call per clip, timed end-to-end (including consuming the lazy
segment generator):

| model            | en_normal (45.8s) | en_fast (16.4s) | zira_clip (41.2s) | avg RTF vs small.en |
|------------------|--------------------|-----------------|--------------------|-----------------------|
| small.en (current) | 5.98s (0.13x)    | 3.70s (0.23x)   | 5.68s (0.14x)      | 1.0x (baseline)       |
| medium.en        | 19.98s (0.44x)     | 14.44s (0.88x)  | 16.44s (0.40x)     | ~3.3x slower          |
| distil-large-v3  | 21.21s (0.46x)     | 8.68s (0.53x)   | 15.78s (0.38x)     | ~2.9x slower          |

RTF = realtime factor (decode time / audio duration) — all three stay well under 1.0x, so none
of them would fall behind real-time on a single segment. But `MAX_SEGMENT_SECONDS` is 60s, and
Day 18's own notes say real interview answers routinely run 20–55s before a natural pause —
at the long end of that range, `medium.en`/`distil-large-v3` mean a **~15-20s wait** for a
suggestion to even start generating, vs. `small.en`'s ~6-8s. That's a real, user-visible cost,
not a rounding error. **The old 3.5x-ish latency penalty holds up under the current
architecture too** — this wasn't a streaming-only artifact.

## Accuracy results (real transcripts, side by side against ground truth)

Full transcripts are in the raw benchmark output; the pattern across all three clips:

- **`small.en` and `medium.en` made nearly identical errors** on `en_normal`: both wrote "red
  paths" for "read paths" and "right path" for "write path" — genuine English homophones
  (identical pronunciation), not something audio fidelity or model capacity can disambiguate
  without a much stronger language-modeling signal than beam=5 default decoding provides.
  `medium.en` didn't fix either one, and introduced its own new artifact ("checksums match,
  which, we cut over...") not present in `small.en`'s output.
- **`distil-large-v3` did not clearly outperform either** — same "red paths"/"right path"
  homophone misses, plus its own new errors not present in `small.en`'s output: "uncall
  rotation" for "on call rotation" (worse than `small.en`'s correct "on-call rotation" on the
  same clip), and "stress-tested" for "stress test it" (changes the grammar, not just spelling).
- On `en_fast` and `zira_clip`, all three models produced transcripts that were essentially
  equivalent in substance, differing mainly in cosmetic punctuation/hyphenation choices, not
  in words correctly recovered vs. missed.

**Headline finding: on this test set, neither `medium.en` nor `distil-large-v3` showed a real
accuracy advantage over `small.en` — each larger model fixed nothing `small.en` got wrong and
introduced at least one new error of its own.**

**Important caveat, stated plainly rather than glossed over:** this is TTS audio — clean,
studio-quality, no background noise, no accent, no mic artifacts, no cross-talk. The specific
errors that showed up (homophones, one word split across a segment/sentence boundary) are
exactly the kind of error a bigger model is *least* likely to help with, since they're not
"the model mis-heard unclear audio," they're "the audio is genuinely ambiguous or the words
happen to be spelled differently." **This benchmark cannot rule out that a bigger model would
help on real, messier audio** (real accents, real background noise, real mic/loopback capture
quality) — it only shows that on clean synthetic speech, there's no free accuracy win sitting on
the table from switching model size. If the user's real accuracy complaint is coming from noisy
real-world audio rather than this class of clean-audio error, this test wouldn't have detected
an improvement either way, and a next step (a real recorded sample, not synthetic) would be the
way to actually settle it — same caveat Day 16 already flagged for its own TTS-based testing.

## Sanity checks: ruling out other causes before blaming model size

**Downsample step (`convert_audio_to_common_format`, 48kHz-stereo → 16kHz-mono):** built a
round-trip test — took the clean 16kHz test clip, upsampled it to a synthetic 48kHz-stereo
float32 buffer (the same shape real captured audio arrives in), ran it through the *actual*
production `convert_audio_to_common_format()`, and transcribed the round-tripped result with
`small.en`. **Result: byte-identical transcript to the untouched original.** The downsample step
is not contributing any measurable degradation — ruled out.

**VAD segmentation (`webrtcvad`, `SPEECH_DETECTION_STRICTNESS=2`, 0.4s silence-close):** fed the
45.8s test clip through the actual production `VoiceSegmenter`, in the same ~100ms chunks audio
really arrives in, and compared `small.en`'s transcript of the 5 real resulting segments
(concatenated) against `small.en` transcribing the same clip as one uncut segment. **Result:
functionally identical** — segmentation landed cleanly on natural sentence pauses (4.9–12.3s
segments), and the only difference was one compound word split across a segment boundary
("dual-writing" → "dual writing" / "dual-riding" in one run) — a small, real, boundary-adjacent
cost, but not close to explaining a general "accuracy isn't good enough" complaint. The same
"red paths"/"right path" homophone misses showed up identically in both the segmented and
single-shot runs, confirming those are a model/decoding-level limitation, not a segmentation
artifact.

## Recommendation

**Keep `small.en`.** On real measured numbers under the actual current architecture: switching
to `medium.en` or `distil-large-v3` costs a real ~3-4x latency penalty (turning a ~6-8s wait
into a ~15-20s one on realistically long segments) and, on this test set, bought zero
measurable accuracy improvement — both alternatives reproduced `small.en`'s errors and added
new ones of their own. Neither the downsample step nor the current VAD/segmentation settings
are plausible causes of an accuracy shortfall either, based on direct round-trip tests against
the real production code paths.

**This isn't a final "accuracy is fine, nothing to do" verdict, though** — it's a "model size
isn't the lever, and here's real evidence why" verdict. If the real complaint is about actual
meeting/interview audio (background noise, accents, real mic/loopback quality) rather than the
homophone/boundary-level errors this clean-audio test surfaced, the next step worth doing before
concluding anything further isn't a bigger checkpoint — it's a real recorded sample (not TTS) to
see what kind of errors actually show up on it, since that would settle whether a bigger model's
theoretical advantage (stronger language modeling, better noise robustness) would even apply
here.
