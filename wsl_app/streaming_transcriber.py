"""
Shared hallucination-defense helpers for calling faster-whisper safely.
Originally built out (Day 16-18) as part of a real-time LocalAgreement-2
streaming transcription pipeline -- that pipeline, and this module along
with it, has since been retired (Day 18: reverted back to the simpler,
pause-then-transcribe-the-whole-utterance approach this app used before
Day 17, after real live testing found the streaming architecture's own
accuracy/reliability trade-offs weren't worth it; see project_notes.md,
Day 18, "back to segment-based transcription"). What's left here is the
model-agnostic core that's still genuinely useful regardless of pipeline
shape: transcribe_filtered() wraps a single model.transcribe() call with
every hallucination defense discovered along the way, so wsl_app/main.py's
(now single) transcribe call gets that protection without re-deriving it.
"""
import numpy as np

TARGET_SAMPLE_RATE = 16000

# How long a repeated phrase is checked for before it's allowed through --
# Whisper occasionally echoes the same run of words twice in a row within
# one hypothesis (a hallucination, not real speech). See
# drop_immediate_repeated_phrase().
MAX_HALLUCINATED_REPEAT_PHRASE_WORDS = 8

# Above this, a segment is dropped outright rather than trusted as real
# transcribed text -- faster-whisper's own per-segment confidence that the
# segment contains no real speech at all. Whisper's well-documented
# "hallucinates a stock training-data phrase when fed silence" failure
# mode (classically a YouTube-style outro like "Thank you for watching")
# shows up most visibly right at session-stop or across any stretch of
# near-silent audio a segment happens to include. 0.6 matches
# faster-whisper's own internal no_speech_threshold default (used for its
# own decoding-fallback logic), not a value tuned separately here. Found
# live, Day 18: stopping a real mock-interview session mid-sentence
# consistently appended a hallucinated "Thank you for watching and see you
# next time" to the transcript.
#
# Day 20 found this threshold has a real false-positive class of its own,
# unrelated to genuine silence: a forced (non-pause) segment boundary --
# a mid-sentence cut with no natural pause at either edge -- reads as
# "probably no speech" almost as strongly as true silence does, because
# the score reacts to the audio having no natural utterance boundary at
# its edges, not just to the audio's actual content. On real, clearly-
# spoken audio this dropped an entire correctly-transcribable clause
# (no_speech_prob 0.799, comfortably above 0.6). See transcribe_filtered's
# skip_no_speech_filter parameter, used by wsl_app/main.py to skip this
# specific check for forced-close segments only -- AVG_LOGPROB_THRESHOLD
# and SILENCE_AMPLITUDE_THRESHOLD below still apply unconditionally, since
# neither was the culprit and both still catch genuine hallucination.
NO_SPEECH_PROBABILITY_THRESHOLD = 0.6

# Below this, a segment is dropped outright regardless of its no_speech_prob
# -- a second, independent hallucination signal for a different failure mode
# than the one above: a very short or truncated audio buffer (e.g. a word
# cut off mid-syllable, right where a real utterance ends) doesn't reliably
# read as "silence" to the model (no_speech_prob can stay low), but there's
# not enough real signal to constrain decoding, and Whisper free-associates
# a whole fabricated sentence instead -- confirmed live, Day 18, on this
# app's own real audio+model: the last 0.5s of a real clip, cut mid-word,
# produced "It is not just one of my favorite damp gases in the face."
# (avg_logprob -3.32, no_speech_prob only 0.57 -- under the threshold
# above) where slightly longer truncated tails (0.8-2.0s, avg_logprob
# roughly -0.76 to -1.83) correctly transcribed the real word ("First").
# -2.0 sits below that legitimate range specifically so a real short/quiet
# word isn't dropped as collateral damage -- it only catches the more
# severe, clearly-fabricated case.
AVG_LOGPROB_THRESHOLD = -2.0

# Below this peak amplitude (on a [-1.0, 1.0] scale), a buffer is treated
# as provably silent -- true digital silence, not just Whisper's own
# opinion of whether it's speech -- and skipped without spending a
# transcribe call on it at all. Added after NO_SPEECH_PROBABILITY_THRESHOLD
# and AVG_LOGPROB_THRESHOLD above both failed to catch a real live
# hallucination: watching a real session, wsl_app's own audio-level log
# showed several straight seconds of literal 0.0000 peak amplitude before
# Whisper still committed a garbled non-word ("S-crailing") -- neither
# confidence filter caught that specific hallucination's score profile.
# Real speech in the same session peaked at 0.15-0.27; 0.001 sits far
# enough below that gap to never mistake quiet real speech for silence,
# while catching true silence deterministically rather than trusting
# Whisper's post-hoc self-assessment, which this incident showed isn't
# reliable on its own.
SILENCE_AMPLITUDE_THRESHOLD = 0.001


def prepare_audio_for_whisper(audio_bytes):
    """
    Converts 16-bit PCM audio bytes into the float32 sample format
    faster-whisper expects to read directly.

    Returns:
        numpy.ndarray: mono samples in the range [-1.0, 1.0].
    """
    int16_samples = np.frombuffer(audio_bytes, dtype=np.int16)
    return int16_samples.astype(np.float32) / 32768.0


def drop_immediate_repeated_phrase(words, max_phrase_words=MAX_HALLUCINATED_REPEAT_PHRASE_WORDS):
    """
    Drops an immediately-repeated run of words from one Whisper hypothesis
    -- an occasional hallucination where the model echoes the same phrase
    twice in a row (e.g. "the cat sat the cat sat down") -- before it has
    a chance to be trusted as real speech. Only collapses an EXACT
    immediate repeat (a phrase directly followed by itself), not any
    duplicate elsewhere in the hypothesis, so real speech that happens to
    repeat a word naturally is left alone. Found live in Day 16's
    real-time-streaming feasibility spike -- see
    wsl_app/research/day16/README.md, "Buffer-trim bug" -- but the failure
    mode itself (Whisper echoing a phrase) isn't specific to that
    architecture, so this stayed in the pipeline after the revert.

    Args:
        words: list of (start, end, text) tuples for one hypothesis.

    Returns:
        list[(start, end, text)]: the same words, with any immediate
        repeated phrase collapsed to a single occurrence.
    """
    texts = [word[2] for word in words]
    result = []
    index = 0
    while index < len(texts):
        longest_possible_match = min(max_phrase_words, (len(texts) - index) // 2)
        collapsed = False
        for phrase_length in range(longest_possible_match, 0, -1):
            first_occurrence = texts[index:index + phrase_length]
            second_occurrence = texts[index + phrase_length:index + 2 * phrase_length]
            if first_occurrence == second_occurrence:
                result.extend(words[index:index + phrase_length])
                index += 2 * phrase_length
                collapsed = True
                break
        if not collapsed:
            result.append(words[index])
            index += 1
    return result


def transcribe_filtered(model, audio, beam_size, initial_prompt=None, skip_no_speech_filter=False):
    """
    Runs one model.transcribe() call and returns only the words Whisper
    itself seems confident are real speech -- every hallucination defense
    this module has (SILENCE_AMPLITUDE_THRESHOLD, NO_SPEECH_PROBABILITY_
    THRESHOLD, AVG_LOGPROB_THRESHOLD, drop_immediate_repeated_phrase)
    applied in one place. Use this instead of calling model.transcribe()
    directly anywhere in this app.

    `skip_no_speech_filter` leaves NO_SPEECH_PROBABILITY_THRESHOLD out of
    the check entirely -- see that constant's docstring for why: it's a
    real false-positive risk specifically on a forced (non-pause) segment
    boundary, which has no natural utterance boundary for the model to key
    off of. AVG_LOGPROB_THRESHOLD and the SILENCE_AMPLITUDE_THRESHOLD gate
    above still apply either way, since neither is the source of that
    false-positive and both still catch genuine hallucination.

    Returns:
        list[(start, end, text)]: filtered words, timed relative to the
        start of `audio` itself (i.e. 0.0 is audio's first sample) -- the
        caller is responsible for shifting to whatever absolute time base
        it needs.
    """
    if audio.size == 0 or np.abs(audio).max() < SILENCE_AMPLITUDE_THRESHOLD:
        return []
    segments, _info = model.transcribe(
        audio,
        language="en",
        initial_prompt=initial_prompt,
        word_timestamps=True,
        condition_on_previous_text=False,
        vad_filter=False,
        beam_size=beam_size,
    )
    words = []
    for segment in segments:
        if not skip_no_speech_filter and segment.no_speech_prob > NO_SPEECH_PROBABILITY_THRESHOLD:
            continue
        if segment.avg_logprob < AVG_LOGPROB_THRESHOLD:
            continue
        if segment.words:
            for word in segment.words:
                # faster-whisper's word-level alignment occasionally emits
                # a pure-whitespace "word" (a real artifact seen live, Day
                # 17) -- worth dropping outright rather than letting it
                # participate in the transcript as if it were real content.
                if word.word.strip():
                    words.append((word.start, word.end, word.word))
    return drop_immediate_repeated_phrase(words)
