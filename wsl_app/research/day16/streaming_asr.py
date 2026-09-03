"""
Day 16 spike: a minimal reimplementation of the LocalAgreement streaming policy
used by whisper_streaming (ufal), built directly against faster-whisper.

Not wired into the running app — this exists only to get real numbers for the
Day 16 feasibility question. See wsl_app/research/day16/README.md for findings.
"""
import numpy as np

SAMPLE_RATE = 16000


class HypothesisBuffer:
    """
    Tracks committed (stable) words vs. the current tentative hypothesis, and
    decides which words become "committed" by comparing consecutive Whisper
    hypotheses over the growing audio buffer (LocalAgreement-2 policy).
    """

    def __init__(self):
        """Starts empty: nothing committed, no prior hypothesis yet."""
        self.committed = []  # list of (start, end, text), absolute time
        self.buffer = []  # previous iteration's leftover (uncommitted) hypothesis
        self.new = []  # this iteration's hypothesis, absolute time

    def insert(self, new_words, offset):
        """
        Loads this iteration's fresh hypothesis (word, start, end tuples relative
        to the current audio buffer) shifted to absolute time by `offset`, then
        strips off any leading words that duplicate the tail of what's already
        committed (Whisper re-emits already-committed words each time it
        re-transcribes the whole buffer).
        """
        self.new = [(s + offset, e + offset, t) for s, e, t in new_words]
        if self.committed and self.new:
            max_ngram = min(5, len(self.committed), len(self.new))
            for i in range(max_ngram, 0, -1):
                tail = [w[2] for w in self.committed[-i:]]
                head = [w[2] for w in self.new[:i]]
                if tail == head:
                    self.new = self.new[i:]
                    break

    def flush(self):
        """
        Commits the longest common prefix between the previous hypothesis
        (self.buffer) and the current one (self.new) -- words that agreed
        across two consecutive passes are considered stable.

        Returns:
            list[(start, end, text)]: newly committed words this call.
        """
        commit = []
        while self.buffer and self.new and self.buffer[0][2] == self.new[0][2]:
            commit.append(self.buffer[0])
            self.buffer.pop(0)
            self.new.pop(0)
        self.buffer = self.new
        self.new = []
        self.committed.extend(commit)
        return commit

    def tentative(self):
        """Returns the current uncommitted (still-changing) word list."""
        return self.buffer


class OnlineASRProcessor:
    """
    Feeds a growing audio buffer to faster-whisper, re-transcribing on each
    new chunk and running the words through a HypothesisBuffer to separate
    stable (committed) text from tentative (may-still-change) text -- the
    "local agreement" streaming approach.
    """

    def __init__(self, model, language, min_chunk_size=1.0, buffer_trim_sec=15.0, beam_size=5):
        """
        Args:
            model: a loaded faster_whisper.WhisperModel.
            language: language code passed to transcribe(), or None for
                Whisper's own language auto-detection (multilingual models only).
            min_chunk_size: seconds of new audio to accumulate before re-running
                transcription (matches whisper_streaming's --min-chunk-size).
            buffer_trim_sec: once the audio buffer exceeds this many seconds,
                trim everything before the last committed word.
            beam_size: beam width for decoding (faster-whisper default is 5;
                smaller is faster but can hurt accuracy).
        """
        self.model = model
        self.language = language
        self.min_chunk_size = min_chunk_size
        self.buffer_trim_sec = buffer_trim_sec
        self.beam_size = beam_size
        self.audio_buffer = np.array([], dtype=np.float32)
        self.buffer_time_offset = 0.0
        self.hyp = HypothesisBuffer()

    def insert_audio_chunk(self, audio_chunk):
        """Appends newly-arrived audio samples to the working buffer."""
        self.audio_buffer = np.concatenate([self.audio_buffer, audio_chunk])

    def process_iter(self):
        """
        Re-transcribes the current audio buffer, updates the hypothesis
        buffer, and trims old audio once the buffer grows past
        buffer_trim_sec.

        Returns:
            (committed, tentative): both lists of (start, end, text) in
            absolute audio time.
        """
        prompt_words = [w[2] for w in self.hyp.committed[-50:]]
        prompt = " ".join(prompt_words) if prompt_words else None

        segments, _info = self.model.transcribe(
            self.audio_buffer,
            language=self.language,
            initial_prompt=prompt,
            word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=False,
            beam_size=self.beam_size,
        )
        words = []
        for seg in segments:
            if seg.words:
                for w in seg.words:
                    words.append((w.start, w.end, w.word))

        self.hyp.insert(words, self.buffer_time_offset)
        committed = self.hyp.flush()

        buffer_len_sec = self.audio_buffer.shape[0] / SAMPLE_RATE
        if buffer_len_sec > self.buffer_trim_sec and committed:
            cut_time = committed[-1][1]
            cut_samples = int((cut_time - self.buffer_time_offset) * SAMPLE_RATE)
            cut_samples = max(0, min(cut_samples, self.audio_buffer.shape[0]))
            self.audio_buffer = self.audio_buffer[cut_samples:]
            self.buffer_time_offset = cut_time

        return committed, self.hyp.tentative()

    def finish(self):
        """Flushes whatever is left in the tentative buffer as committed, for end-of-audio."""
        final = self.hyp.buffer + self.hyp.new
        self.hyp.committed.extend(final)
        self.hyp.buffer = []
        self.hyp.new = []
        return final
