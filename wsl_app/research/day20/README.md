# Day 20 — Rolling Transcript + STT Model Upgrade: Feasibility Spike

Research only. Nothing in `wsl_app/main.py` or `streaming_transcriber.py` changed — all code here
is standalone, under `research/day20/`. See `docs/DEV_PLAN.md` Day 20 for the handoff prompt
(including the WhisperX-architecture research done before this spike started) and for the actual
outcome summary this README backs up.

## What's here

- `wer.py` — dependency-free Levenshtein word-error-rate helper, shared by both tracks.
- `track_a_segmentation.py` — Track A's main benchmark: a trimmed copy of `VoiceSegmenter`
  parameterized on the force-close threshold, run at the current 60s default plus three shorter
  candidates (8s/12s/16s), transcribing each closed segment with the real production
  `transcribe_filtered()` call.
- `track_a_diagnose_boundary_loss.py` — root-causes the content-loss bug the main benchmark found
  (see Finding 2 below) by printing each segment's raw, unfiltered `no_speech_prob`/`avg_logprob`.
- `track_a_boundary_safe_fix.py` — tests the fix for that bug (skip `NO_SPEECH_PROBABILITY_THRESHOLD`
  specifically on forced-close segments) against all four clips.
- `track_b_baseline.py` — this app's real production path (`transcribe_filtered` on `small.en`),
  one whole-clip call per clip, run with the **main** `wsl_app` venv.
- `track_b_whisperx.py` — WhisperX's real pipeline (own Silero-VAD re-segmentation + batched
  `faster-whisper` decoding), same clips, same `small.en` checkpoint, run with an **isolated**
  `whisperx_venv` (see below).
- `whisperx_venv/` — a separate venv (not the main `wsl_app/.venv`) holding WhisperX and its heavy
  transitive deps (`torch` 2.8 CPU, `pyannote-audio`, `transformers`, etc. — ~3GB). Kept isolated
  deliberately: WhisperX pulls its own `faster-whisper`/`ctranslate2` pins, and installing them into
  the production venv risked destabilizing the app's existing, working STT path for a spike that
  might not even recommend adoption. **Not committed to git** (matches the project's existing
  `.venv/` exclusion) — anyone re-running Track B needs to recreate it (`python3 -m venv
  research/day20/whisperx_venv && research/day20/whisperx_venv/bin/pip install whisperx`).
- `track_b_baseline_results.json` / `track_b_whisperx_results.json` — raw output from each script,
  read by the comparison numbers below.

Test clips reused from prior days (all CPU-only, `int8`, this dev machine): `en_normal.wav`,
`en_fast.wav` (Day 16), `zira_clip.wav` (Day 18.5), `fast_long.wav` (Day 18.6, Day 20's Track A
target — a real 39.5s single continuous VAD segment with no natural pause, exactly the "nothing
shown for a long stretch" scenario Track A addresses). All TTS, not real recorded speech — see the
real-audio caveat under Track B below.

---

## Track A — rolling transcript (fast-speech-aware segmentation)

**Recommendation: yes, worth building — but not the naive version. It needs one specific
accompanying fix, which is now itself verified, not just implied.**

### Finding 1 — the core idea works: real, substantial latency win, no accuracy cost from
segmentation alone

At a 16s force-close threshold, `fast_long.wav`'s time-to-first-visible-text drops from **50.3s**
(current 60s threshold — nothing shown until the entire clip finally closes) to **21.5s**, a ~57%
cut, with WER *slightly better* than the current baseline (5.6% vs 6.8%). On the other three clips
(which all have natural pauses inside them), the 16s threshold never fires differently from a real
pause — zero regression, because it's a no-op there.

### Finding 2 — but shorter thresholds (8s/12s) reintroduce real, severe content loss — a new
failure mode, not the old streaming bugs, but just as bad

At 8s, `fast_long`'s WER jumped to **45.2%** and at 12s to **34.4%** — not minor roughness, whole
clauses vanished:

> ...for about six minutes **[gap — "before it settled back down and when we dug into the traces it
> looked like the connection pool to the inventory service was getting exhausted because a retry
> storm kicked in when one of it three inventory replicas got slow, and the circuit breaker config
> we have right now waits" is entirely missing]** way too long before it actually trips...

`track_a_diagnose_boundary_loss.py` traced this to a specific, confirmed cause: the missing segment
(8.1s–16.1s) is **real, clearly-articulated speech** (peak amplitude 0.93, not remotely silent) that
Whisper itself transcribes correctly when shown its raw output — but `no_speech_prob` on that
segment reads **0.799** (well above the existing `NO_SPEECH_PROBABILITY_THRESHOLD` of 0.6), so
`transcribe_filtered()`'s existing hallucination-defense filter drops the whole segment. That filter
was tuned (Day 18) for a genuinely different problem — real silence at session-stop — and it turns
out to react almost as strongly to "this audio chunk has no natural utterance boundary at its edges"
as it does to actual silence. A forced mid-sentence cut, by construction, always produces exactly
that shape of chunk. This is a new failure mode distinct from the Day 16-18 streaming bugs (buffer
trim, cross-model reconciliation) — but the DEV_PLAN.md framing that this approach structurally
avoids "all three problems found here" was too optimistic on that specific point; it introduces a
new, fourth one.

### Finding 3 — the fix works: verified, not just diagnosed

Since the segmenter already knows whether a given close was a real pause or a forced timeout, a
forced-close segment can safely skip `NO_SPEECH_PROBABILITY_THRESHOLD` specifically (keeping
`AVG_LOGPROB_THRESHOLD` and the true-silence amplitude gate active — neither was the culprit here,
and both still catch genuine hallucination). `track_a_boundary_safe_fix.py` re-ran every clip at
8s/12s with this fix applied:

| clip | threshold | WER before fix | WER after fix | baseline (60s) WER |
|---|---|---|---|---|
| fast_long | 8s | 45.2% | **7.2%** | 6.8% |
| fast_long | 12s | 34.4% | **6.0%** | 6.8% |
| en_normal | 8s | 4.0% | 4.0%* | 3.2% |
| zira_clip | 8s | 4.1% | 4.1%* | 0.0% |

\* `en_normal`/`zira_clip` at 8s still show one residual real risk the fix does *not* cover: one
`zira_clip` boundary hallucinated a fabricated clause ("because they did not know what happened.")
rather than dropping content — `avg_logprob` was fine there (-0.202, well above the -2.0 cutoff), so
this wasn't a `no_speech_prob` false-positive, it's a genuine case of Whisper free-associating past a
mid-sentence cut with no silence padding. Matches the standing project knowledge
("decoding a partial/boundary-aligned audio slice with Whisper is inherently fragile") — the fix
closes the *severe* failure mode (whole-segment drops) but not this milder, harder-to-eliminate one.

With the fix, `fast_long` at 8s reaches near-baseline accuracy (7.2% vs 6.8%) while cutting
time-to-first-text from 50.3s to roughly **8-11s** — an ~80% reduction, actually delivering on the
original "doesn't wait for a pause" goal rather than just softening the wait.

### Recommendation

Build the fast-speech-aware segmentation change, **with the boundary-safe filter fix as a required
part of it, not a follow-up** — the naive version (shorter threshold alone) is a real regression on
exactly the audio it's meant to help. Suggested shape for implementation:
- `VoiceSegmenter` gains a shorter force-close threshold (something in the 8-16s range — 8-12s gets
  the most responsive live feel; 16s is the more conservative, zero-observed-risk option if the
  team wants to ship the segmentation change alone first and add the filter fix separately).
- `ClosedSegment` (or the equivalent in production) needs to carry whether it closed via a real
  pause or a forced timeout, so `transcribe_segment()` can pass that through to skip
  `NO_SPEECH_PROBABILITY_THRESHOLD` accordingly.
- Test set here is TTS, same caveat as every prior model-comparison day — flagged, not fatal, since
  the mechanism (a filter reacting to boundary shape, not audio content) isn't TTS-specific.

---

## Track B — STT accuracy (small.en vs. WhisperX's batched pipeline)

**Recommendation: not worth adopting for this app, on the numbers found — a modest, real latency
win from WhisperX's own VAD pre-trimming, but its headline batching advantage doesn't materialize
here, at a real, heavy dependency cost.**

CPU-only, `small.en` checkpoint in both engines, WhisperX using Silero VAD (not its pyannote
default, which needs a HuggingFace auth token this environment doesn't have — a reasonable
substitution for a plain accuracy/latency check, not the diarization use case anyway, since Day 19's
dual-source capture already solves speaker attribution structurally).

| clip | duration | small.en (prod path) latency | small.en WER | WhisperX (batch=1) latency | WhisperX WER | latency delta |
|---|---|---|---|---|---|---|
| en_normal | 45.8s | 11.80s | 2.4% | 10.00s | 2.4% | -15.2% |
| en_fast | 16.4s | 7.36s | 6.5% | 6.71s | 6.5% | -8.8% |
| zira_clip | 41.2s | 11.94s | 0.0% | 11.07s | 0.0% | -7.2% |
| fast_long | 39.5s | 16.56s | 8.8% | 15.10s | 7.2% | -8.8% |

### Finding 1 — accuracy: essentially identical, one small win

Same WER on 3/4 clips, marginally better on `fast_long` (7.2% vs 8.8%). No evidence of a real
accuracy advantage from WhisperX's alignment/hallucination handling on this test set — consistent
with the pre-spike research (the WhisperX paper's own "bigger alignment model not found to be that
helpful" note), now confirmed directly rather than taken from the paper alone.

### Finding 2 — the headline batching speedup does not materialize for this app's usage pattern,
confirmed directly

`batch_size` 1 vs 4 vs 8 moved latency by only a few percent, within noise, on every clip. Root
cause, also directly confirmed: WhisperX's own VAD re-segmented each ~40s clip into only **1-2
internal chunks** — there's nothing to meaningfully batch. This is exactly what the pre-spike
research flagged as the risk: this app hands WhisperX one already-VAD-closed utterance at a time
(one segment in flight per audio source, same as today), not a backlog of many short clips from an
offline file — the scenario batching actually helps. Confirmed, not just predicted.

### Finding 3 — a real, if modest, latency win independent of batching

Even at `batch_size=1`, WhisperX was **7-15% faster** than the current production call on every
clip, with `faster-whisper`/`ctranslate2` versions verified nearly identical between the two venvs
(1.2.1/4.8.1 vs 1.2.1/4.8.2 — ruling out "WhisperX just ships a newer decoder" as the explanation).
The most likely real cause: WhisperX's own VAD trims silence at each chunk's edges before decoding,
so less audio reaches the model per call than this app's current segment (which can carry a bit of
leading/trailing near-silence inside `SILENCE_SECONDS_TO_CLOSE_SEGMENT`'s pause window). Real, but a
7-15% win on a 7-17s call is roughly 0.5-2.5s off perceived latency — genuinely marginal against the
cost of adopting it (see below).

### Real caveat, not settled by this test

All test audio is TTS, same standing limitation as every prior model-comparison day (Day 18.5 in
particular) — no real accent, mic noise, or capture artifacts. Day 19 now makes real mic capture
possible, but no real recording exists yet in this repo or from Day 19's own session to test
against, and this research session has no way to capture one itself (same class of gap as every
prior "needs a real physical action" item in this project — hotkey presses, drag tests). Per Day
20's own scope note, falling back to TTS here with the caveat flagged plainly, rather than blocking,
is the agreed-on move — but this result should be treated as provisional until validated against a
real recording.

### Recommendation

**Hold off.** The one real advantage found (7-15% latency from VAD pre-trimming) is real but small,
and is not exclusive to WhisperX — the same effect is achievable directly in this app's own
pipeline without adopting WhisperX's dependency stack at all: trim each closed segment's own
leading/trailing near-silence (using the amplitude-based approach `streaming_transcriber.py`
already has, via `SILENCE_AMPLITUDE_THRESHOLD`) before calling `transcribe_filtered()`, instead of
passing the VAD-closed segment through as-is. That would be a small, targeted, low-risk change
inside the existing pipeline, versus WhisperX's real integration cost: `torch` 2.8 CPU (~800MB+),
`pyannote-audio`, `transformers`, and the rest of a ~3GB dependency tree this app has no other use
for, none of which is offset by a batching win that this app's one-segment-at-a-time usage pattern
structurally can't realize. If a real-mic-audio test later surfaces an accuracy gap this TTS test
set didn't catch, that would be a different, better-justified reason to revisit — not the batching
or general-accuracy story tested here.
