# Day 18.6 — Fast-Speech Live Preview: base.en Streaming, Feasibility Check

Research only, nothing wired into the running app. `wsl_app/main.py` is unchanged. See
`docs/DEV_PLAN.md` (Day 18.6) and `project_notes.md` for the handoff prompt and summary this
README backs up.

## What's here

- `accuracy_comparison.py` — small.en whole-segment decode (the real production
  `transcribe_filtered()` call) vs. base.en LocalAgreement-2 streaming (`research/day16/
  streaming_asr.py`'s `OnlineASRProcessor`, unchanged), on fast/continuous speech.
- `cpu_concurrency_check.py` — measures whether base.en streaming running in a background
  thread slows down small.en's own segment decode.
- `fast_long.wav`/`fast_long.txt` — new test clip: 39.5s, SAPI TTS Rate=+6, written as one
  continuous run-on (commas and "and" instead of sentence breaks) specifically so it stays open
  as a single VAD segment the whole way through — confirmed this with the real production
  `VoiceSegmenter` before using it (see "Segmentation check" below). `en_fast.wav` (Day 16,
  16.4s, also confirmed to stay as one segment) is reused from `../day16/`.

## The two claims, tested separately, as asked

### Claim 1 (UX): showing something beats showing nothing during a long wait

Not directly re-litigated here — Day 16 already measured base.en's first-glimpse/commit latency
(~2.3s/~4.0s avg) against showing nothing until segment-close, and that comparison doesn't change
based on anything in this spike. Left as "plausible and probably true in isolation," but see the
recommendation below for why it doesn't end up mattering on its own.

### Claim 2 (accuracy): base.en's settled streaming output is more accurate than small.en's whole-segment result on fast speech

**False, and not just "a little false" — on the long clip it's dramatically less complete, not
just less polished.**

First run used `beam_size=5` for base.en (matching production's small.en beam, for an
apples-to-apples-*looking* setup) and it was badly broken — settled output for the 39.5s clip
cut off after ~15 words. That's not a fair test, though: Day 16 already established base.en only
sustains a real-time-ish cadence at `beam_size=1`; beam=5 was never the config this project
actually used for live base.en streaming. Re-ran at the historically-correct `beam_size=1`:

| clip | ground truth (excerpt) | small.en whole-segment | base.en streaming/settled |
|---|---|---|---|
| en_fast (16.4s) | "...retry logic is still using the old backoff curve..." | "...retry logic is still using the old back off curve..." (correct, cosmetic hyphenation only) | "...retry logic is still using the old back off curve..." (correct) — **this short clip came out essentially complete and comparable to small.en** |
| fast_long (39.5s) | full 250-word paragraph, no gaps | full paragraph present, a handful of real word-substitution errors ("feature flat rollout" for "flag", "the uncalled handoff notes" for "on call handoff notes") but structurally complete | **roughly half the transcript is simply missing** — real committed words are followed by long stretches of blank/whitespace where entire sentences should be, then a few words reappear near the end |

Full transcripts are in the raw script output; the gap in the long-clip row isn't a
transcription-quality difference, it's real content loss in the LocalAgreement-2 *committing*
step itself — the same structural buffer-trim/regrowth issue Day 16 first found and Day 17/18
never fully resolved before the revert (`project_notes.md`, Day 16, "Buffer-trim bug found in
the local-agreement technique itself"). A shorter clip (16s) mostly avoids it because the buffer
never needs to trim; a 39.5s clip triggers exactly the failure mode that technique has always had.
This isn't a new bug introduced by this spike — it's the same one, reproduced again, specifically
in the "long, fast, continuous speech" scenario this feature is meant to target.

**A live viewer watching this preview wouldn't see "rougher but present" text — they'd see real
words appear and then apparently get erased** as the buffer trims past content that was never
successfully committed. That's a worse experience than showing nothing, not a better one.

**Real-time keeping-up, also checked and also failing on this exact scenario**: base.en's own
streaming loop took 55.83s of total compute to process the 39.5s clip at the standard 1.5s
cadence — an 1.41x realtime factor, meaning it falls *further* behind live speech the longer a
segment runs, not just "a bit slower than ideal." This matches Day 16's own finding that every
tested configuration fell behind realtime on every iteration of its full-loop benchmark — not a
new discovery, but a confirmation that it still holds specifically for this feature's target case.

**Note on the original motivating claim**: the task's own framing was that small.en's eventual
result on long fast speech "doesn't come out very accurate either." This test doesn't reproduce
that — small.en's whole-segment result on the 39.5s clip stayed structurally complete with a
handful of real but non-catastrophic word-substitution errors, not a severe breakdown. Worth
saying plainly rather than quietly working around: either the real degradation Jon's noticed
lives in real audio conditions this clean-TTS test can't reproduce (same standing caveat as Day
18.5 — no background noise, no accent, no mic/loopback artifacts), or it's specific to audio this
test didn't happen to capture. Either way, a live ephemeral preview wouldn't be the fix for
"small.en itself is inaccurate on fast speech" even if that turns out to be real — see the
recommendation below.

## Sanity check: does the test clip actually reproduce the target scenario?

Fed both clips through the real production `VoiceSegmenter` (100ms chunks, same as real audio
arrival) before trusting them as test cases: both stayed open as exactly one continuous segment
(16.4s and 39.5s respectively, no early VAD-triggered pause) — confirming these really do
represent "long, fast, continuous speech with nothing shown until close," not an accidental
multi-segment clip that would understate the wait.

## CPU concurrency: real cost, not free

Measured small.en's own segment-decode time (a) alone and (b) with base.en streaming running
continuously in a background thread (the real production concurrency shape — both would go
through `asyncio.to_thread`, i.e. real OS threads in one process, not separate processes):

| condition | small.en decode time (3 runs) | average |
|---|---|---|
| alone | 5.70s, 5.59s, 5.63s | 5.64s |
| base.en streaming concurrently | 8.53s, 8.70s, 8.59s | 8.61s |

**~1.53x slowdown** to small.en's own segment turnaround — the thing the whole suggestion
pipeline is downstream of — just from running the live preview alongside it. Not a free
addition; a real, measurable tax on the pipeline that already matters more (the authoritative
transcript and the suggestion it feeds).

## Recommendation: hold off

All three questions the task asked me to check came back against building this, not just
inconclusive:

1. **Accuracy claim: false**, and specifically worse in the exact scenario (long, fast,
   continuous speech) this feature targets — real content loss, not just rougher text.
2. **Can't keep up with realtime** on that same scenario, so the preview would visibly lag
   further behind live speech the longer a segment runs.
3. **Real CPU cost** (~1.53x slower small.en decoding) to the pipeline that actually matters more.

The UX claim (something beats nothing) is probably still true in isolation, but it doesn't
rescue this specific design — a live preview that visibly loses content and gets more stale
over time isn't "rough but useful," it actively risks looking broken. This is the same
LocalAgreement-2 technique, same root defect class, that Day 18 already reverted away from once;
this spike reproduces that same structural issue again in the one scenario (long/fast speech)
that seemed like it might be the exception, and it isn't.

**Not implementing the ephemeral preview.** If Jon still wants to address the "nothing shown for
up to 60s" problem, the more promising angle per Day 18.6's own text is a fast-speech-specific
*segmentation* change (e.g., force a shorter sub-segment boundary when fast/continuous speech is
detected, so small.en itself gets called sooner on a smaller chunk) rather than a second streaming
model reconciled or swapped against the first — that doesn't carry any of the three problems found
here, since it's still just small.en, still one decode per closed segment, only with a
fast-speech-aware trigger for closing one sooner. Worth a future day's research spike if this is
still a priority, not something to build as a quick follow-up to this one.
