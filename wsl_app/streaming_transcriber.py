"""
Streaming (partial + committed) transcription on top of faster-whisper,
built on the LocalAgreement-2 policy validated in Day 16's feasibility
spike (see wsl_app/research/day16/). faster-whisper has no incremental
decoding of its own -- every tick re-transcribes the whole current audio
buffer from scratch -- so this module's whole job is turning that
repeated, slightly-different-each-time output into a stable stream of
"this text is now final" plus "this is the current best guess, still
likely to change" for MeetingSession (wsl_app/main.py) to report to
windows_app.
"""
import numpy as np
import webrtcvad

TARGET_SAMPLE_RATE = 16000

# The speech detector (webrtcvad) can only judge audio in fixed-size
# slices, one at a time -- 30 milliseconds is a size it supports.
FRAME_DURATION_MS = 30
FRAME_DURATION_SECONDS = FRAME_DURATION_MS / 1000
BYTES_PER_FRAME = int(TARGET_SAMPLE_RATE * FRAME_DURATION_SECONDS) * 2  # 16-bit samples

# webrtcvad's own "how strict should speech-detection be" setting, on its
# own 0-3 scale: 0 accepts more borderline sound as speech, 3 rejects more
# of it. Matches the strictness the old segment-based pipeline used.
SPEECH_DETECTION_STRICTNESS = 2

# How often MeetingSession re-transcribes the buffer and reports whatever
# changed -- the ~1.5-2s cadence Day 16 measured as the honest ceiling for
# base.en on this hardware (see wsl_app/research/day16/README.md). Also
# passed to StreamingBufferProcessor as its own "wait for this much new
# audio" threshold, so that matches the cadence it's actually called at.
PARTIAL_UPDATE_INTERVAL_SECONDS = 1.75

# Once the audio buffer holding not-yet-committed audio grows past this
# many seconds, trim it back (see StreamingBufferProcessor.
# retranscribe_and_update). Shortened from whisper_streaming's own 15s
# default -- Day 16 found that on a session that never grows past 15s, the
# buffer never trims, so every new hypothesis re-includes the *entire*
# transcript so far, which the small dedup window below can't handle, and
# whole sentences get committed twice. This isn't a short-clip-only
# artifact either: the same regrowth-then-trim cycle repeats throughout a
# long real session, so a much shorter threshold has to hold up
# continuously, not just early on.
BUFFER_TRIM_SECONDS = 4.0

# Sentence-ending punctuation preferred as a trim point (see
# StreamingBufferProcessor._pick_trim_cut_time) -- cutting audio right
# after a finished sentence is a cleaner seam than cutting mid-thought.
SENTENCE_END_CHARS = (".", "!", "?")

# Beam width for decoding. base.en's micro-benchmark floor (~0.5-0.8s/call)
# was measured at beam=1 (Day 16); beam=5 measured meaningfully slower
# with no reported accuracy win worth trading against the ~1.5-2s cadence
# target.
STREAMING_BEAM_SIZE = 1

# How many seconds of "no speech heard at all" before the buffered audio
# is dropped outright, rather than being kept around (and eventually
# trimmed) for nothing -- both to bound memory during a long quiet stretch
# and because repeatedly re-transcribing pure silence risks Whisper
# hallucinating phantom text (this streaming setup runs with
# vad_filter=False, same as Day 16's spike, so nothing else guards against
# that).
MAX_SILENT_SECONDS_BEFORE_DROPPING_BUFFER = 6.0

# How many consecutive speech-flagged 30ms frames are needed before
# SpeechPresenceDetector reports "real speech seen" -- 10 frames is ~300ms,
# matching the old segment-based pipeline's MIN_SEGMENT_SECONDS_TO_TRANSCRIBE
# guard's intent: a single stray speech-detector false positive (a cough, a
# keyboard click) no longer counts as speech and resets the silence timer on
# its own, which used to risk several ticks of wasted (and, since
# vad_filter=False, hallucination-risking) transcription over the following
# MAX_SILENT_SECONDS_BEFORE_DROPPING_BUFFER for a single spurious frame.
# Found missing in code review, Day 17.
MIN_CONSECUTIVE_SPEECH_FRAMES_TO_COUNT = 10

# How many trailing/leading words a new hypothesis is checked against the
# already-committed text for, to strip out words Whisper re-emits every
# time it re-transcribes the whole buffer (see PartialTextTracker.
# add_new_guess). Matches whisper_streaming's own default.
COMMITTED_TEXT_OVERLAP_WORDS_TO_CHECK = 5

# Hard safety cap, found necessary via live testing against a real >15s
# continuous utterance (Day 17): a long run-on sentence with no natural
# pause can go many ticks without the two-consecutive-hypotheses agreement
# that normally triggers a commit (and, with it, the buffer trim above),
# and once the buffer grows large, Whisper's own hypothesis quality
# degrades badly -- observed live as earlier real content silently
# dropping out of later hypotheses entirely, well before its ~30s internal
# decoding window is even reached, worsening into outright garbled/
# hallucinated output the larger the buffer gets. A single word of
# genuine agreement is not enough to stop that growth either -- trimming
# only cuts up to the last *committed* word, so one committed word buried
# in an otherwise-uncommitted 15-word hypothesis barely shrinks the
# buffer. So this check applies independently of whatever the normal
# agreement policy already committed this tick: whenever the buffer is
# still over this many seconds afterward, most of the current tentative
# guess is force-committed by fiat (see PartialTextTracker.
# force_commit_stale_tentative_words) so the buffer is reliably pulled
# back down every tick, not just occasionally.
MAX_BUFFER_SECONDS_BEFORE_FORCED_COMMIT = 8.0

# How many trailing words a forced commit leaves uncommitted -- these are
# the ones most likely to still be revised, so they're kept tentative
# rather than locked in by fiat along with the rest.
FORCED_COMMIT_KEEP_LAST_N_WORDS = 2

# How long a repeated phrase is checked for before it's allowed to be
# committed -- Whisper occasionally echoes the same run of words twice in
# a row within one hypothesis (a hallucination, not real speech). See
# drop_immediate_repeated_phrase().
MAX_HALLUCINATED_REPEAT_PHRASE_WORDS = 8


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
    a chance to be committed as if it were real speech. Only collapses an
    EXACT immediate repeat (a phrase directly followed by itself), not any
    duplicate elsewhere in the hypothesis, so real speech that happens to
    repeat a word naturally is left alone. Found live in Day 16's spike --
    see wsl_app/research/day16/README.md, "Buffer-trim bug".

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


class PartialTextTracker:
    """
    Tracks committed (stable) words vs. the current tentative guess, and
    decides which words become "committed" by comparing consecutive
    Whisper hypotheses over the growing audio buffer -- the
    LocalAgreement-2 policy: a word is trusted once it agreed across two
    consecutive passes.
    """

    def __init__(self):
        """Starts empty: nothing committed, no prior guess yet."""
        self.committed_words = []  # list of (start, end, text), absolute time
        self._previous_guess_words = []  # last call's leftover (uncommitted) guess
        self._latest_guess_words = []  # this call's guess, absolute time

    def add_new_guess(self, new_words, offset):
        """
        Loads this tick's fresh hypothesis (word, start, end tuples
        relative to the current audio buffer) shifted to absolute time by
        `offset`, then strips off any leading words that duplicate the
        tail of what's already committed (Whisper re-emits already-
        committed words every time it re-transcribes the whole buffer).
        """
        self._latest_guess_words = [(start + offset, end + offset, text) for start, end, text in new_words]
        if self.committed_words and self._latest_guess_words:
            max_overlap = min(
                COMMITTED_TEXT_OVERLAP_WORDS_TO_CHECK, len(self.committed_words), len(self._latest_guess_words)
            )
            for overlap in range(max_overlap, 0, -1):
                committed_tail = [word[2] for word in self.committed_words[-overlap:]]
                guess_head = [word[2] for word in self._latest_guess_words[:overlap]]
                if committed_tail == guess_head:
                    self._latest_guess_words = self._latest_guess_words[overlap:]
                    break

    def commit_agreed_words(self):
        """
        Commits the longest common prefix between the previous guess and
        this tick's guess -- words that agreed across two consecutive
        passes are considered stable enough to lock in.

        Returns:
            list[(start, end, text)]: newly committed words this call.
        """
        newly_committed = []
        while (
            self._previous_guess_words
            and self._latest_guess_words
            and self._previous_guess_words[0][2] == self._latest_guess_words[0][2]
        ):
            newly_committed.append(self._previous_guess_words[0])
            self._previous_guess_words.pop(0)
            self._latest_guess_words.pop(0)
        self._previous_guess_words = self._latest_guess_words
        self._latest_guess_words = []
        self.committed_words.extend(newly_committed)
        return newly_committed

    def tentative_words(self):
        """Returns the current uncommitted (still-changing) word list."""
        return self._previous_guess_words

    def discard_tentative_words(self):
        """
        Throws away the current tentative guess without committing it --
        used when its underlying audio is about to be dropped outright
        (see StreamingBufferProcessor.drop_buffered_audio), so a stale
        guess from before a silence gap can't be compared against a
        completely unrelated hypothesis once speech resumes and
        coincidentally "agree" with it.
        """
        self._previous_guess_words = []
        self._latest_guess_words = []

    def force_commit_stale_tentative_words(self, keep_last_n_words=FORCED_COMMIT_KEEP_LAST_N_WORDS):
        """
        Locks in most of the current tentative guess even though it hasn't
        agreed across two consecutive passes yet -- a last resort used
        once the audio buffer has grown so large (see
        MAX_BUFFER_SECONDS_BEFORE_FORCED_COMMIT) that waiting for real
        agreement risks losing track of older content entirely before it
        ever gets confirmed. Keeps the last `keep_last_n_words` words
        tentative, since those are the ones most likely to still change.

        Returns:
            list[(start, end, text)]: the words just force-committed.
        """
        if len(self._previous_guess_words) <= keep_last_n_words:
            return []
        boundary = len(self._previous_guess_words) - keep_last_n_words
        to_commit = self._previous_guess_words[:boundary]
        self._previous_guess_words = self._previous_guess_words[boundary:]
        self.committed_words.extend(to_commit)
        return to_commit

    def flush_remaining_as_committed(self):
        """
        Moves everything still tentative into committed_words, for
        end-of-session -- there's no more audio coming to confirm it
        against, so it's the best answer there is.

        Returns:
            list[(start, end, text)]: whatever was just flushed.
        """
        remaining = self._previous_guess_words + self._latest_guess_words
        self.committed_words.extend(remaining)
        self._previous_guess_words = []
        self._latest_guess_words = []
        return remaining


class StreamingBufferProcessor:
    """
    Feeds a growing audio buffer to faster-whisper, re-transcribing on
    each call and running the words through a PartialTextTracker to
    separate stable (committed) text from tentative (may-still-change)
    text.

    add_audio() runs on the caller's thread (the asyncio event loop, via
    MeetingSession.handle_audio_chunk) every time new audio arrives, while
    every other method here runs on whichever single worker thread a tick
    happens to execute on (via asyncio.to_thread -- see MeetingSession.
    _run_streaming_ticks). Those two things can genuinely run at the same
    moment, so add_audio() only ever queues into _pending_chunks rather
    than touching _audio_buffer directly -- see _drain_pending_audio() for
    why that's safe without an explicit lock. Found missing (an
    unsynchronized cross-thread mutation of _audio_buffer that could
    silently drop or corrupt in-flight audio) in code review, Day 17.
    """

    def __init__(self, model, beam_size=STREAMING_BEAM_SIZE):
        """Stores the loaded Whisper model to transcribe with and starts with an empty buffer."""
        self._model = model
        self._beam_size = beam_size
        self._audio_buffer = np.array([], dtype=np.float32)
        self._pending_chunks = []
        self._buffer_time_offset = 0.0
        self._text_tracker = PartialTextTracker()

    def add_audio(self, samples):
        """
        Queues newly-arrived audio samples to be folded into the working
        buffer on the next call to a method that reads _audio_buffer (see
        _drain_pending_audio). Runs on the event-loop thread; list.append()
        is a single atomic operation under the GIL, so this needs no lock
        even though _drain_pending_audio reads/clears the same list from a
        different thread.
        """
        self._pending_chunks.append(samples)

    def _drain_pending_audio(self):
        """
        Folds any samples queued by add_audio() since the last drain into
        _audio_buffer. Safe without an explicit lock: swapping
        _pending_chunks for a fresh empty list is one atomic attribute
        assignment, so a concurrent add_audio() call either lands in the
        list this method already captured a reference to (still included,
        since that object isn't mutated further after being read here) or
        in the fresh list for next time -- never lost, never double-
        counted. Every method below that reads or mutates _audio_buffer
        calls this first.
        """
        pending = self._pending_chunks
        self._pending_chunks = []
        if pending:
            self._audio_buffer = np.concatenate([self._audio_buffer] + pending)

    def has_buffered_audio(self):
        """Returns True if there's any audio -- queued or already folded in -- not yet transcribed."""
        return bool(self._pending_chunks) or self._audio_buffer.shape[0] > 0

    def drop_buffered_audio(self):
        """
        Discards all buffered audio -- used once a stretch has been pure
        silence for a while (see StreamingTranscriber) instead of growing
        a buffer that holds nothing worth transcribing. Advances the time
        offset by however much audio is being dropped, so word timestamps
        stay monotonic for whatever's transcribed after this, and discards
        any leftover tentative guess along with it (see PartialTextTracker.
        discard_tentative_words) -- it was never confirmed, its audio is
        gone, and letting it linger risks a false "agreement" against
        whatever unrelated hypothesis comes back once speech resumes.
        """
        self._drain_pending_audio()
        self._buffer_time_offset += self._audio_buffer.shape[0] / TARGET_SAMPLE_RATE
        self._audio_buffer = np.array([], dtype=np.float32)
        self._text_tracker.discard_tentative_words()

    def retranscribe_and_update(self):
        """
        Re-transcribes the current audio buffer, folds the result into the
        committed/tentative text tracker, and trims old audio once the
        buffer grows past BUFFER_TRIM_SECONDS.

        Returns:
            (committed, tentative): both lists of (start, end, text) in
            absolute audio time.
        """
        self._drain_pending_audio()
        recent_committed_words = [word[2] for word in self._text_tracker.committed_words[-50:]]
        # faster-whisper's own word.word strings already carry a leading
        # space (confirmed empirically -- e.g. " So", " the") -- "".join()
        # here, not " ".join(), or every word gets doubled whitespace.
        prompt = "".join(recent_committed_words).strip() if recent_committed_words else None

        segments, _info = self._model.transcribe(
            self._audio_buffer,
            language="en",
            initial_prompt=prompt,
            word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=False,
            beam_size=self._beam_size,
        )
        words = []
        for segment in segments:
            if segment.words:
                for word in segment.words:
                    # faster-whisper's word-level alignment occasionally
                    # emits a pure-whitespace "word" (a real artifact seen
                    # live, Day 17) -- worth dropping outright rather than
                    # letting it participate in agreement/commit matching
                    # as if it were real content.
                    if word.word.strip():
                        words.append((word.start, word.end, word.word))
        words = drop_immediate_repeated_phrase(words)

        self._text_tracker.add_new_guess(words, self._buffer_time_offset)
        committed = self._text_tracker.commit_agreed_words()

        buffer_length_seconds = self._audio_buffer.shape[0] / TARGET_SAMPLE_RATE
        if buffer_length_seconds > MAX_BUFFER_SECONDS_BEFORE_FORCED_COMMIT:
            committed = committed + self._text_tracker.force_commit_stale_tentative_words()
        if buffer_length_seconds > BUFFER_TRIM_SECONDS and committed:
            self._trim_buffer_to(self._pick_trim_cut_time(committed))

        return committed, self._text_tracker.tentative_words()

    def _pick_trim_cut_time(self, latest_committed):
        """
        Picks where to cut the audio buffer: prefers the end of the most
        recent word *in this tick's own newly committed batch* that
        finished a sentence -- a cleaner seam than cutting mid-thought --
        falling back to the plain end of the latest committed word if none
        of this batch's words end a sentence.

        Deliberately only looks within `latest_committed`, not the full
        committed history: searching further back risks finding an
        earlier sentence-ending word from a previous tick's commit and
        returning ITS end time instead, which would move the cut point
        backward and re-include audio that a previous trim had already
        cut away -- found live (Day 17) testing against a real >15s
        utterance, where exactly this regression cascaded into the buffer
        never shrinking and eventually feeding Whisper a buffer so large
        its own hypotheses degraded into hallucinated garbage.

        Returns:
            float: the absolute time to cut the buffer at.
        """
        for _start, end, text in reversed(latest_committed):
            if text.strip().endswith(SENTENCE_END_CHARS):
                return end
        return latest_committed[-1][1]

    def _trim_buffer_to(self, cut_time):
        """Drops buffered audio before `cut_time`, advancing the time offset to match."""
        cut_samples = int((cut_time - self._buffer_time_offset) * TARGET_SAMPLE_RATE)
        cut_samples = max(0, min(cut_samples, self._audio_buffer.shape[0]))
        self._audio_buffer = self._audio_buffer[cut_samples:]
        self._buffer_time_offset = cut_time

    def finish(self):
        """Flushes whatever tentative text is left as committed, for end-of-session."""
        return self._text_tracker.flush_remaining_as_committed()


class SpeechPresenceDetector:
    """
    Answers "was there sustained speech in the audio just added" -- much
    lighter than the old segment-based VoiceSegmenter, since
    StreamingTranscriber doesn't need to know where a sentence starts or
    ends, only whether the current buffer is worth spending a transcribe
    call on at all.
    """

    def __init__(self):
        """Starts with no leftover audio, no speech streak yet, and loads the speech detector."""
        self._speech_detector = webrtcvad.Vad(SPEECH_DETECTION_STRICTNESS)
        self._unsliced_audio = bytearray()
        self._consecutive_speech_frames = 0

    def add_audio_and_check_for_speech(self, audio_bytes):
        """
        Feeds newly-arrived 16kHz mono audio in and checks it for
        sustained speech. The speech detector can only judge one
        fixed-size 30ms slice at a time, but audio arrives in whatever
        size chunks the network happens to deliver, so this keeps a small
        leftover buffer between calls, the same way the old VoiceSegmenter
        did. A non-speech frame resets the running streak, so a single
        stray speech-flagged frame (a brief noise blip) can't count on its
        own -- see MIN_CONSECUTIVE_SPEECH_FRAMES_TO_COUNT.

        Returns:
            bool: True if this call's audio contained at least
            MIN_CONSECUTIVE_SPEECH_FRAMES_TO_COUNT consecutive
            speech-flagged frames.
        """
        self._unsliced_audio.extend(audio_bytes)
        heard_sustained_speech = False
        while len(self._unsliced_audio) >= BYTES_PER_FRAME:
            frame = bytes(self._unsliced_audio[:BYTES_PER_FRAME])
            del self._unsliced_audio[:BYTES_PER_FRAME]
            if self._speech_detector.is_speech(frame, TARGET_SAMPLE_RATE):
                self._consecutive_speech_frames += 1
                if self._consecutive_speech_frames >= MIN_CONSECUTIVE_SPEECH_FRAMES_TO_COUNT:
                    heard_sustained_speech = True
            else:
                self._consecutive_speech_frames = 0
        return heard_sustained_speech


class StreamingTranscriber:
    """
    The live pipeline's streaming transcription, one per active meeting
    session: audio arrives continuously via add_audio_chunk(), and
    process_tick() -- called roughly every PARTIAL_UPDATE_INTERVAL_SECONDS
    by MeetingSession -- re-transcribes the buffer and returns whatever
    text just became newly committed vs. what's still tentative. Skips the
    (relatively expensive) transcribe call entirely once a stretch has had
    no speech at all for a while, both to save CPU and because repeatedly
    transcribing pure silence risks Whisper hallucinating phantom text.
    """

    def __init__(self, model):
        """Sets up the buffer processor and speech-presence check, starting idle (no speech seen yet)."""
        self._processor = StreamingBufferProcessor(model)
        self._speech_presence = SpeechPresenceDetector()
        self._seconds_since_speech_seen = MAX_SILENT_SECONDS_BEFORE_DROPPING_BUFFER

    def add_audio_chunk(self, audio_bytes):
        """Feeds newly-arrived 16kHz mono PCM audio into the streaming buffer and updates the speech-presence check."""
        if self._speech_presence.add_audio_and_check_for_speech(audio_bytes):
            self._seconds_since_speech_seen = 0.0
        self._processor.add_audio(prepare_audio_for_whisper(audio_bytes))

    def process_tick(self):
        """
        Blocking -- always call via asyncio.to_thread(). Re-transcribes
        the current buffer, unless it's been pure silence for a while (in
        which case the buffer is simply dropped and nothing is
        transcribed this tick).

        Returns:
            (str, str): (newly_committed_text, current_tentative_text).
            Either may be empty. newly_committed_text is only the text
            that just became final this tick, not the running total.
        """
        self._seconds_since_speech_seen += PARTIAL_UPDATE_INTERVAL_SECONDS
        if self._seconds_since_speech_seen >= MAX_SILENT_SECONDS_BEFORE_DROPPING_BUFFER:
            self._processor.drop_buffered_audio()
            return "", ""
        committed, tentative = self._processor.retranscribe_and_update()
        committed_text = "".join(word[2] for word in committed).strip()
        tentative_text = "".join(word[2] for word in tentative).strip()
        return committed_text, tentative_text

    def finish(self):
        """
        Blocking -- always call via asyncio.to_thread(). Runs one last
        retranscription pass first to catch any audio that arrived since
        the most recent regular tick -- up to just under
        PARTIAL_UPDATE_INTERVAL_SECONDS worth would otherwise never be fed
        to Whisper at all and silently vanish (found in code review, Day
        17) -- then flushes whatever's still tentative as committed.

        Returns:
            str: the flushed text, or "" if there was nothing left to flush.
        """
        final_committed = []
        if self._processor.has_buffered_audio():
            final_committed, _tentative = self._processor.retranscribe_and_update()
        remaining = self._processor.finish()
        return "".join(word[2] for word in final_committed + remaining).strip()
