import asyncio
# audioop is used below to convert captured audio into the one consistent
# format speech detection and transcription both need. It's deprecated and
# slated for removal in Python 3.13+;
# not an issue on this project's pinned 3.10.12, but flag it here so it's
# not a silent trap if the interpreter version ever changes.
import audioop
import base64
import bisect
import json
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import NamedTuple

import numpy
import webrtcvad
from faster_whisper import WhisperModel

from streaming_transcriber import TARGET_SAMPLE_RATE, prepare_audio_for_whisper, transcribe_filtered

CONFIG_PATH = Path(__file__).resolve().parent.parent / "ipc_config.json"
STATS_WINDOW_SECONDS = 1.0

# Where the opt-in live-agent-listening export log (see LiveAgentLog) is
# saved -- kept in wsl_app's own filesystem, not windows_app's (contrast
# with Day 22's SessionRecorder, which is deliberately Windows-side for
# Explorer visibility) -- the whole point of this file is to be `tail -f`'d
# by a separate `claude` terminal session run from WSL, so keeping it in
# WSL's filesystem avoids any cross-boundary path awkwardness for that.
#
# LIVE_AGENT_LOG_PATH is a single fixed filename, not one generated fresh
# per session (each start() truncates and rewrites it) -- deliberately, so
# start_live_agent_listening.sh can point a tail at a path known in advance
# rather than the operator having to copy a new timestamped path out of
# wsl_app's console every session. This log is a live feed meant to be
# actively tailed, not a record reviewed afterward (SessionRecorder, Day
# 22, already covers that need), so there's no real loss in not keeping
# every past session's copy around.
LIVE_AGENT_LOG_DIR = Path(__file__).resolve().parent / "live_agent_logs"
LIVE_AGENT_LOG_PATH = LIVE_AGENT_LOG_DIR / "live_agent_current.log"

# asyncio's StreamReader defaults to a 64KiB limit on how long one
# newline-delimited message line can be before readline() gives up and
# raises -- fine for the small control messages this protocol used to
# carry, but the Phase 1.5 context_notes field explicitly invites pasting
# a whole CV/JD or PRD excerpt into one settings_changed line, which can
# realistically exceed that. Raised generously (well above any single
# audio_chunk message too) so a large paste doesn't crash the connection.
MAX_MESSAGE_LINE_BYTES = 10 * 1024 * 1024

# The speech detector (webrtcvad) can only judge audio in fixed-size
# slices, one at a time -- 30 milliseconds is a size it supports;
# everything below is derived from that.
FRAME_DURATION_MS = 30
FRAME_DURATION_SECONDS = FRAME_DURATION_MS / 1000
BYTES_PER_FRAME = int(TARGET_SAMPLE_RATE * FRAME_DURATION_SECONDS) * 2  # 16-bit samples

# webrtcvad's own "how strict should speech-detection be" setting, on its
# own 0-3 scale: 0 accepts more borderline sound as speech, 3 rejects more
# of it. Used for loopback (see VoiceSegmenter's speech_detection_strictness
# param, and MeetingSession, which picks the right constant per source).
SPEECH_DETECTION_STRICTNESS = 2

# The mic's own, stricter strictness (Day 19). A real microphone has a
# genuine, continuous noise floor -- room tone, breathing, a fan, mic
# self-noise -- that loopback's clean digital output audio simply doesn't
# have; at loopback's strictness (2), live testing found this reliably
# opened short false-positive "speech" segments on quiet mic noise, and
# faster-whisper then hallucinated fabricated, out-of-context text for
# them (e.g. "It's a wig.", "if you're not safe.") rather than
# transcribing real content -- a classic Whisper failure mode (see
# streaming_transcriber.py's NO_SPEECH_PROBABILITY_THRESHOLD/AVG_LOGPROB_
# THRESHOLD docstrings for the general pattern), triggered here by VAD
# false positives feeding it noise to begin with rather than by anything
# those two filters are tuned to catch. 3 is webrtcvad's strictest/least
# permissive setting -- the most direct lever to cut down on noise-driven
# false positives at the source, before they ever reach Whisper. Needs
# real re-verification against an actual mic (see project conventions on
# live-verifying accuracy changes) -- this is a principled first attempt
# based on the real failure observed, not a tuned/confirmed value yet.
MIC_SPEECH_DETECTION_STRICTNESS = 3

# How long a pause has to last before a sentence is considered "done" and
# the segment closes.
SILENCE_SECONDS_TO_CLOSE_SEGMENT = 0.4

# Safety net: force-close a segment after this long even without a pause,
# so one uninterrupted run-on sentence can't grow the buffer forever --
# and, since Day 21, the main lever for how long a fast talker's speech
# sits transcribed-but-unshown before anything appears on screen at all.
#
# Originally 20.0, raised to 60.0 on Day 18 (user-reported live): a real
# interview answer routinely runs past 20s of continuous explanation
# without a pause long enough to close a segment naturally, and forcing a
# close that early split words mid-sentence ("Can you explain H2..." /
# "to be cashing." as the orphaned, context-less remainder) -- both
# visibly wrong on the transcript and measurably worse accuracy on
# whichever half lost its surrounding context.
#
# Lowered again here (Day 21) on the strength of Day 20's benchmark
# (research/day20/README.md, Track A): the accuracy loss above wasn't
# actually caused by the short cut itself -- it was
# NO_SPEECH_PROBABILITY_THRESHOLD spuriously dropping real, clearly-spoken
# audio because a forced mid-sentence boundary reads as "no speech" almost
# as strongly as true silence does. With that filter skipped specifically
# for forced closes (see ClosedSegment.closed_on_forced_timeout and
# transcribe_segment() below), Day 20 verified an 8-12s threshold back to
# near-baseline WER (7.2%/6.0% vs. the old 60s cap's own 6.8%, on the same
# clip that originally motivated raising this to 60) while cutting
# time-to-first-visible-text for one long unbroken sentence by roughly
# 80% (measured ~50s down to ~8-11s). 10.0 splits that verified 8-12s
# range. One residual risk Day 20 did not fully close: a mid-sentence cut
# can still occasionally make Whisper fabricate a short clause rather than
# drop content -- rarer and milder than the whole-segment drops this fix
# targets, not eliminated by it.
MAX_SEGMENT_SECONDS = 10.0

# Segments shorter than this are almost always a false positive from the
# speech detector (a brief noise blip, not real speech), so skip the
# (expensive) transcription call for them entirely rather than spending a
# full model call for nothing.
MIN_SEGMENT_SECONDS_TO_TRANSCRIBE = 0.3

# Beam width for transcribing a closed segment. Wider than a beam=1 live-
# streaming decode would use, since there's no tight per-tick cadence to
# protect here -- a segment only gets decoded once, after a real pause, so
# it's worth spending a bit more compute per call favoring accuracy.
TRANSCRIBE_BEAM_SIZE = 5

# The Whisper model size and assistant mode to use for a session that
# starts before windows_app has ever sent a settings_changed message.
# small.en -- this app's original default (Day 4) and, again, its current
# one: reverted here (Day 18) from Day 17's real-time-streaming rewrite,
# which had switched the live pipeline to base.en (continuously re-decoding
# a growing buffer) to sustain a tight partial-caption cadence. Real use
# after that rewrite -- including a same-day attempt at hybrid base.en-live
# plus small.en-"polish" correction -- kept surfacing the same root problem
# from different angles (misrecognized words, several distinct Whisper
# hallucination classes, and, for the polish hybrid specifically, boundary-
# alignment errors between two independently-decoded models) traceable to
# decoding partial/boundary-aligned buffer slices rather than complete,
# pause-bounded utterances. small.en decoding one whole utterance at a time
# -- this file's original architecture -- was "almost always accurate" by
# the user's own account of real use, at the cost of not showing live
# partial captions until a pause closes each segment. See project_notes.md,
# Day 18, "back to segment-based transcription" for the full account.
DEFAULT_WHISPER_MODEL_SIZE = "small.en"
DEFAULT_MODE = "meeting"

# The two independent audio sources a session can capture from (Day 19):
# the user's own microphone, and the existing WASAPI loopback (system
# audio, i.e. "everyone else"). Each gets its own VoiceSegmenter (see
# MeetingSession) so one source's speech and pauses never affect the
# other's segmentation. SOURCE_LABELS is what each source's lines are
# tagged with everywhere transcribed text shows up -- the live transcript
# and the rolling context sent to Claude -- so a suggestion prompt (and,
# later, a summary) can tell who said what.
AUDIO_SOURCES = ("mic", "loopback")
SOURCE_LABELS = {"mic": "You", "loopback": "Them"}

# Which VAD strictness (see SPEECH_DETECTION_STRICTNESS/MIC_SPEECH_
# DETECTION_STRICTNESS above) each source's VoiceSegmenter should use --
# read by MeetingSession when it builds one segmenter per source.
SPEECH_DETECTION_STRICTNESS_BY_SOURCE = {
    "mic": MIC_SPEECH_DETECTION_STRICTNESS,
    "loopback": SPEECH_DETECTION_STRICTNESS,
}

# How long a pause has to last, with no further new transcript segment
# arriving during it, before a suggestion is generated automatically —
# used as the default until a session-specific value arrives via
# settings_changed.
PAUSE_SECONDS_BEFORE_SUGGESTION = 1.2

# How much recent transcript text is kept and sent as context with each
# suggestion request. Originally (Day 7) a fixed count of 10 segments,
# sized around a single audio source -- Day 18.7's context-feed spike
# flagged that a fixed segment count is the wrong shape once a second
# source (Day 19's own mic capture, above) can add its own segments
# concurrently: two active sources fill a fixed count roughly twice as
# fast in real time, so real context would fall out of the window sooner
# than it used to, for no principled reason. A character budget scales
# naturally with however many sources are actually producing text instead.
# Day 18.7 also confirmed a wider context window is nearly free -- marginal
# prompt tokens only, no extra claude CLI calls -- so there's no real cost
# to sizing this generously. 4000 characters is roughly double the old
# 10-segment cap's typical size (segments commonly run 200-400 characters
# each), meant to give a two-source session about the same real
# conversational time-depth the old cap gave a one-source session.
CONTEXT_CHARACTER_BUDGET = 4000

# How many ask() calls a session's claude CLI process handles before being
# recycled (stopped and restarted fresh) to bound its own
# automatically-growing internal conversation history.
ASK_CALLS_BEFORE_RECYCLING_CLAUDE_CLI = 10


def load_port():
    """
    Reads the shared IPC config and returns the port both sides connect on.

    Returns:
        int: the TCP port from ipc_config.json.
    """
    with open(CONFIG_PATH) as config_file:
        config = json.load(config_file)
    return config["port"]


# Every message is one JSON object with a "type" field, newline-delimited.
# session_started/session_stopped, audio_chunk, hotkey_triggered,
# settings_changed, and auto_suggest_changed are all one-way messages
# handled directly in handle_client() below since they don't need a reply.
# This function only covers the ones that do (currently just ping/pong).
async def build_reply(message):
    """
    Decides how to respond to one incoming message, based on its "type".

    Returns:
        dict | None: the reply message to send back, or None if no reply is needed.
    """
    if message.get("type") == "ping":
        return {"type": "pong"}
    return None


def decode_audio_chunk(message):
    """
    Decodes one audio_chunk message's base64 audio payload into a numpy
    array of samples, using the format fields included on the message.

    Returns:
        numpy.ndarray | None: the decoded samples, or None if sample_format
        isn't one this side knows how to decode.
    """
    sample_format = message.get("sample_format")
    if sample_format != "float32":
        print(f"Audio: unsupported sample_format {sample_format!r}, dropping chunk")
        return None
    raw_bytes = base64.b64decode(message["data"])
    return numpy.frombuffer(raw_bytes, dtype=numpy.float32)


def convert_audio_to_common_format(samples, sample_rate, channels):
    """
    Converts one chunk of captured audio into the single consistent format
    the speech-detection and transcription steps both need, no matter what
    device or settings it was originally recorded with: a single channel
    (not stereo) and a fixed 16,000 samples per second. sample_rate/channels
    are read from the message every time rather than assumed, since the
    capture device (and therefore its format) can change on the windows_app
    side.

    Returns:
        bytes: the converted audio, ready for the next step.
    """
    int16_samples = numpy.clip(samples, -1.0, 1.0)
    int16_samples = (int16_samples * 32767.0).astype(numpy.int16)

    if channels == 1:
        audio_bytes = int16_samples.tobytes()
    elif channels == 2:
        audio_bytes = audioop.tomono(int16_samples.tobytes(), 2, 0.5, 0.5)
    else:
        # audioop.tomono only understands stereo; average manually for
        # anything else (uncommon, but the device dropdown could pick one).
        frames = int16_samples.reshape(-1, channels)
        audio_bytes = frames.mean(axis=1).astype(numpy.int16).tobytes()

    if sample_rate != TARGET_SAMPLE_RATE:
        audio_bytes, _ = audioop.ratecv(audio_bytes, 2, 1, sample_rate, TARGET_SAMPLE_RATE, None)

    return audio_bytes


def load_whisper_model(model_size):
    """
    Loads a faster-whisper model of the given size. Loading takes a few
    seconds (and may download model weights on first run), so this must
    never be called per-segment — see WhisperModelManager, which calls this
    only at startup and when a session asks for a different size.

    Returns:
        faster_whisper.WhisperModel: the loaded model, ready to transcribe.
    """
    print(f"Loading Whisper model ({model_size})... first run may download weights from Hugging Face.")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    print("Whisper model loaded.")
    return model


class WhisperModelManager:
    """
    Owns the one Whisper model shared by whichever session is active --
    always small.en, see DEFAULT_WHISPER_MODEL_SIZE -- and the lock that
    serializes every real transcribe() call against it (see
    transcribe_lock).
    """

    def __init__(self):
        """Loads the model right away, so it's ready before the first session, and creates the lock every transcribe call must hold."""
        self.model = load_whisper_model(DEFAULT_WHISPER_MODEL_SIZE)
        # Real transcribe() calls are CPU-bound and all share this one
        # loaded model. Day 19 live testing (mic capture, a second
        # concurrent source) found that letting two segments' transcribe
        # calls actually run at the same time causes severe CPU
        # contention, not real parallelism: individual segment transcribe
        # times observed up to 30+ seconds, for audio that normally
        # transcribes in 2-4s (Day 18.5's own benchmark) -- matching Day
        # 18.6's earlier, independent finding that concurrent Whisper
        # decode calls compete for the same cores and slow each other down
        # rather than genuinely parallelizing. Worse under mic capture
        # specifically, since a real mic's noise floor can trigger many
        # short false-positive segments in a burst (see VoiceSegmenter's
        # per-source speech_detection_strictness), each spawning its own
        # asyncio.create_task with no concurrency limit -- without this
        # lock, a burst like that pile up and fight for CPU all at once.
        # This lock serializes only the actual decode step across every
        # session and source; segmentation, capture, and everything else
        # about each source stays fully independent (see VoiceSegmenter/
        # AudioCaptureManager) -- a burst of segments still queues and
        # drains through transcription in order, just one at a time,
        # rather than all competing for the same CPU simultaneously.
        self.transcribe_lock = asyncio.Lock()


async def send_message(writer, message):
    """
    Writes one JSON message to a connected windows_app client. windows_app
    can disconnect while a segment is still transcribing or a suggestion is
    still being generated — a real, expected race, not a bug — in which
    case the write fails with a connection error. That's logged and
    swallowed here (the single place every outgoing message passes through)
    rather than left to blow up as an unhandled background-task exception,
    since the rest of the session may still be running fine for reasons
    unrelated to this one message.
    """
    try:
        writer.write((json.dumps(message) + "\n").encode())
        await writer.drain()
    except OSError as error:
        # Covers ConnectionResetError/BrokenPipeError, which is what a
        # disconnected client actually raises here.
        print(f"Couldn't send {message.get('type')!r} to client (already disconnected?): {error}")


class AudioLevelTracker:
    """
    Counts how many audio chunks arrive and how loud the loudest one is,
    over roughly one second at a time, so we can log a single summary line
    per second instead of spamming a line per chunk. One instance per audio
    source (see handle_client's audio_trackers) -- mic and loopback have
    very different typical gain/volume profiles, so a shared tracker would
    make the logged stats actively misleading rather than just imprecise.
    """

    def __init__(self, label):
        """Starts a fresh one-second tracking window for one audio source, identified as `label` in its printed summaries."""
        self._label = label
        self._window_start = time.monotonic()
        self._chunk_count = 0
        self._peak_amplitude = 0.0

    def record(self, samples):
        """
        Adds one decoded chunk to the current window: bumps the chunk count
        and updates the loudest volume seen so far. Once roughly a second
        has passed, logs a summary line and starts a new window.
        """
        self._chunk_count += 1
        if samples.size:
            self._peak_amplitude = max(self._peak_amplitude, float(numpy.abs(samples).max()))

        elapsed = time.monotonic() - self._window_start
        if elapsed >= STATS_WINDOW_SECONDS:
            print(
                f"Audio ({self._label}): {self._chunk_count} chunks in {elapsed:.1f}s, "
                f"peak amplitude {self._peak_amplitude:.4f}"
            )
            self._window_start = time.monotonic()
            self._chunk_count = 0
            self._peak_amplitude = 0.0


class ClosedSegment(NamedTuple):
    """
    One finished speech segment, as handed back by VoiceSegmenter: its
    recorded audio, and the wall-clock time (time.time(), not
    time.monotonic() -- this is sent to windows_app and compared against
    the other source's segments, so it needs to mean the same instant on
    both sides, which only an actual wall-clock timestamp does) its first
    speech frame arrived. started_at is what lets a transcript with two
    independent sources (mic + loopback) still land in the right
    chronological order relative to each other once both are transcribed
    (see MeetingSession.add_transcript_segment and windows_app's
    TranscriptDisplay) -- transcription finishing order isn't reliable for
    this, since two segments of different length or two pipelines under
    different CPU load can finish in a different order than they were
    actually spoken in.

    closed_on_forced_timeout tells transcribe_segment() whether this
    segment ended because someone actually paused (a real sentence
    boundary) or because MAX_SEGMENT_SECONDS was reached mid-speech (see
    that constant's docstring) -- the two cases need different
    hallucination-filtering treatment, since a forced boundary has no
    natural pause for NO_SPEECH_PROBABILITY_THRESHOLD to key off of.
    """

    audio_bytes: bytes
    started_at: float
    closed_on_forced_timeout: bool


class VoiceSegmenter:
    """
    Watches a steady stream of incoming audio and figures out where each
    spoken sentence starts and ends, so each one can be sent to Whisper on
    its own instead of transcribing everything as one giant blob.

    How a segment opens and closes:
      1. While nothing is being said, incoming audio is thrown away.
      2. The moment speech is heard, a new segment starts recording.
      3. The segment keeps recording through speech AND short pauses.
      4. Once a pause has lasted SILENCE_SECONDS_TO_CLOSE_SEGMENT, the
         segment is considered finished ("that was one sentence") and is
         handed back to the caller.
      5. If speech runs on for MAX_SEGMENT_SECONDS without ever pausing
         that long, the segment is force-closed anyway, so one very long
         run-on sentence can't grow the recording forever.

    One instance covers exactly one audio source (see MeetingSession, which
    keeps a separate VoiceSegmenter per source) -- speech and pauses on one
    source never affect how the other source's segments open or close.
    """

    def __init__(self, speech_detection_strictness=SPEECH_DETECTION_STRICTNESS):
        """Starts with nothing recorded yet and no segment in progress, using `speech_detection_strictness` (see SPEECH_DETECTION_STRICTNESS/MIC_SPEECH_DETECTION_STRICTNESS) to judge what counts as speech."""
        self._speech_detector = webrtcvad.Vad(speech_detection_strictness)
        # Audio that has arrived but hasn't been sliced into a full frame yet.
        self._unsliced_audio = bytearray()
        # Audio recorded for the segment currently in progress, if any.
        self._current_segment_audio = bytearray()
        self._segment_is_open = False
        self._segment_started_at = None
        self._seconds_of_silence_in_a_row = 0.0
        self._seconds_recorded_in_current_segment = 0.0

    def add_audio(self, audio_bytes):
        """
        Feeds newly-arrived 16kHz mono audio into the segmenter.

        The speech detector can only judge one fixed-size 30ms slice of
        audio at a time, but audio arrives in whatever size chunks the
        network happens to deliver. So this keeps a small leftover buffer,
        cuts off exactly one 30ms slice at a time as enough audio piles
        up, and checks each slice in turn.

        Returns:
            list[ClosedSegment]: zero or more segments that finished
            (closed) while processing this batch of audio, each ready to
            hand to Whisper. Usually empty — most calls just add to an
            open segment without finishing it.
        """
        self._unsliced_audio.extend(audio_bytes)

        finished_segments = []
        while len(self._unsliced_audio) >= BYTES_PER_FRAME:
            one_frame = bytes(self._unsliced_audio[:BYTES_PER_FRAME])
            del self._unsliced_audio[:BYTES_PER_FRAME]
            finished_segment = self._add_frame_to_current_segment(one_frame)
            if finished_segment is not None:
                finished_segments.append(finished_segment)

        return finished_segments

    def _add_frame_to_current_segment(self, frame):
        """
        Asks the speech detector whether this one 30ms slice contains
        speech, then updates the segment currently being recorded (if
        any). Called once per slice, in order, by add_audio().

        Returns:
            ClosedSegment | None: the finished segment, if this slice was
            the one that closed it. None if the segment is still open, or
            if there's no segment in progress and this slice was silence.
        """
        this_frame_is_speech = self._speech_detector.is_speech(frame, TARGET_SAMPLE_RATE)

        if not this_frame_is_speech and not self._segment_is_open:
            # Plain silence and nothing recording yet — nothing to do.
            return None

        self._current_segment_audio.extend(frame)
        self._seconds_recorded_in_current_segment += FRAME_DURATION_SECONDS

        if this_frame_is_speech:
            if not self._segment_is_open:
                # The first speech frame of a brand new segment -- record
                # when it actually started, not when it happens to close
                # (see ClosedSegment's docstring for why this matters).
                self._segment_started_at = time.time()
            self._segment_is_open = True
            self._seconds_of_silence_in_a_row = 0.0
        else:
            self._seconds_of_silence_in_a_row += FRAME_DURATION_SECONDS
            if self._seconds_of_silence_in_a_row >= SILENCE_SECONDS_TO_CLOSE_SEGMENT:
                return self._close_current_segment(closed_on_forced_timeout=False)

        if self._seconds_recorded_in_current_segment >= MAX_SEGMENT_SECONDS:
            return self._close_current_segment(closed_on_forced_timeout=True)

        return None

    def _close_current_segment(self, closed_on_forced_timeout):
        """
        Packages up everything recorded for the current segment so it can
        be handed off for transcription, then resets so the next speech
        heard starts a brand new segment.

        Returns:
            ClosedSegment: the finished segment's audio, start time, and
            whether it closed via MAX_SEGMENT_SECONDS rather than a real
            pause (see ClosedSegment's docstring).
        """
        finished_segment_audio = bytes(self._current_segment_audio)
        started_at = self._segment_started_at
        self._current_segment_audio = bytearray()
        self._segment_is_open = False
        self._segment_started_at = None
        self._seconds_of_silence_in_a_row = 0.0
        self._seconds_recorded_in_current_segment = 0.0
        return ClosedSegment(finished_segment_audio, started_at, closed_on_forced_timeout)

    def close_open_segment(self):
        """
        Force-closes whatever segment is currently in progress, if any.
        Used when a session stops, so audio that hasn't hit a silence gap
        yet (someone stops the session right after finishing a sentence)
        isn't silently dropped. Reported as closed_on_forced_timeout=False
        even though nothing paused -- this is a session-ending flush, not
        a mid-run-on-sentence cut, so it's the same "trailing audio, maybe
        real silence" shape NO_SPEECH_PROBABILITY_THRESHOLD was actually
        tuned for (see that constant's docstring), not the forced-boundary
        false-positive case MAX_SEGMENT_SECONDS's cut needs to skip it for.

        Returns:
            ClosedSegment | None: the finished segment, or None if nothing
            was open.
        """
        if not self._segment_is_open:
            return None
        return self._close_current_segment(closed_on_forced_timeout=False)


def segment_duration_seconds(audio_bytes):
    """
    Returns:
        float: how many seconds of audio `audio_bytes` (16-bit mono PCM at
        TARGET_SAMPLE_RATE) represents.
    """
    return len(audio_bytes) / 2 / TARGET_SAMPLE_RATE


def transcribe_segment(model, audio_bytes, closed_on_forced_timeout):
    """
    Runs Whisper on one closed speech segment. This is blocking, CPU-bound
    work — always call it through asyncio.to_thread(), never directly on
    the event loop. Uses transcribe_filtered (see streaming_transcriber.py)
    rather than a bare model.transcribe() call, so a segment that's mostly
    silence or noise (a webrtcvad false positive, or a stretch of near-
    silence caught up in the same segment as real speech) can't have
    Whisper hallucinate a stock phrase into the transcript.
    `closed_on_forced_timeout` skips transcribe_filtered's
    NO_SPEECH_PROBABILITY_THRESHOLD check for a segment that closed via
    MAX_SEGMENT_SECONDS rather than a real pause -- see that constant's
    docstring for why a forced mid-sentence boundary needs this.

    Returns:
        str: the transcribed text, stripped of leading/trailing whitespace.
    """
    audio = prepare_audio_for_whisper(audio_bytes)
    words = transcribe_filtered(
        model, audio, TRANSCRIBE_BEAM_SIZE, skip_no_speech_filter=closed_on_forced_timeout
    )
    return "".join(word[2] for word in words).strip()


async def transcribe_segment_and_report(
    model, transcribe_lock, writer, audio_bytes, source, started_at, closed_on_forced_timeout, session=None
):
    """
    Transcribes one closed speech segment in a background thread (keeping
    the event loop free to keep reading incoming messages), logs the
    result with a timestamp, and — if it contains real text — sends it to
    windows_app as a transcript message tagged with which audio source
    (`source`, e.g. "mic"/"loopback") it came from and when it started
    (`started_at`), so windows_app can label it and place it in the right
    chronological spot relative to the other source's lines (see
    windows_app's TranscriptDisplay). If this segment belongs to a
    still-active session, also feeds the text into that session's rolling
    context and suggestion trigger (see MeetingSession). `session` is left
    as None for a segment transcribed after its session has already ended
    (the trailing bit of audio flushed on session_stopped), so it's
    reported but doesn't try to trigger a suggestion from a session that no
    longer exists. `transcribe_lock` (see WhisperModelManager) is held only
    around the actual transcribe call, so segments still queue up and wait
    their turn instead of fighting each other for CPU -- the logged
    "Xs to transcribe" duration includes any time spent waiting for the
    lock, which is real, user-facing latency either way. `closed_on_forced_
    timeout` is passed straight through to transcribe_segment() -- see its
    docstring.
    """
    duration_seconds = segment_duration_seconds(audio_bytes)
    transcribe_started_at = time.monotonic()
    async with transcribe_lock:
        text = await asyncio.to_thread(transcribe_segment, model, audio_bytes, closed_on_forced_timeout)
    elapsed_seconds = time.monotonic() - transcribe_started_at
    timestamp = time.strftime("%H:%M:%S")
    spoken_text = text if text else "(no speech detected)"
    print(
        f"[{timestamp}] Transcript ({source}, {duration_seconds:.1f}s segment, "
        f"{elapsed_seconds:.1f}s to transcribe): {spoken_text}"
    )
    if text:
        await send_message(writer, {"type": "transcript", "text": text, "source": source, "started_at": started_at})
        if session is not None:
            session.add_transcript_segment(source, text, started_at)


def handle_finished_segment(model, transcribe_lock, writer, closed_segment, source, session=None):
    """
    Decides what to do with one just-closed speech segment: skip
    transcription entirely if it's too short to plausibly be real speech
    (a false positive from the speech detector that would otherwise cost a
    full, slow model call for nothing), or kick off background
    transcription-and-reporting otherwise. `transcribe_lock`, `source`, and
    `session` are passed through to transcribe_segment_and_report() -- see
    its docstring.
    """
    duration_seconds = segment_duration_seconds(closed_segment.audio_bytes)
    if duration_seconds < MIN_SEGMENT_SECONDS_TO_TRANSCRIBE:
        print(f"Skipping {duration_seconds:.2f}s {source} segment: below minimum duration, likely not real speech")
        return
    asyncio.create_task(
        transcribe_segment_and_report(
            model,
            transcribe_lock,
            writer,
            closed_segment.audio_bytes,
            source,
            closed_segment.started_at,
            closed_segment.closed_on_forced_timeout,
            session,
        )
    )


def handle_audio_chunk(audio_trackers, transcribe_lock, session, message):
    """
    Decodes one audio_chunk message, adds its volume to the running
    one-second stats for whichever source it's tagged with, and feeds it
    (converted to the common 16kHz mono format) into that source's own
    speech segmenter (session.segmenters -- mic and loopback each get an
    independent VoiceSegmenter, so one source's speech/pauses never affect
    the other's segment boundaries). Any speech segment just closed is
    handed off to handle_finished_segment() along with `transcribe_lock`,
    the source, and the session, so a finished transcript can feed the
    suggestion trigger. Transcribes with whichever Whisper model this
    session was started with (session.model) -- never the manager's
    current model directly, since that could have moved on to a different
    size for a later session (not possible today, since the model size
    isn't session-configurable, but keeps this correct if that ever
    changes again). A message carrying an unrecognized `source` is logged
    and dropped, the same defensive treatment decode_audio_chunk() already
    gives an unrecognized sample_format -- windows_app is the only client
    and always sends one of AUDIO_SOURCES, so this should never trigger in
    practice.
    """
    source = message.get("source")
    segmenter = session.segmenters.get(source)
    if segmenter is None:
        print(f"Audio: unrecognized source {source!r}, dropping chunk")
        return

    samples = decode_audio_chunk(message)
    if samples is None:
        return
    audio_trackers[source].record(samples)

    audio_bytes = convert_audio_to_common_format(samples, message["sample_rate"], message["channels"])
    for closed_segment in segmenter.add_audio(audio_bytes):
        handle_finished_segment(session.model, transcribe_lock, session.writer, closed_segment, source, session)


def default_settings():
    """
    Returns:
        dict: the settings to start a session with if it begins before any
        settings_changed message has ever arrived on this connection.
    """
    return {
        "mode": DEFAULT_MODE,
        "suggestion_pause_seconds": PAUSE_SECONDS_BEFORE_SUGGESTION,
        "context_notes": "",
        "auto_suggest_enabled": False,
        "live_agent_export_enabled": False,
    }


def settings_from_message(message):
    """
    Reads mode/suggestion_pause_seconds/etc. out of a settings_changed
    message, falling back to default_settings() for any field that's
    missing OR explicitly sent as null (a plain message.get(key, default)
    wouldn't catch the null case, since the key would still be present).

    Returns:
        dict: settings in the same shape default_settings() returns.
    """
    defaults = default_settings()
    settings = {}
    for key, default_value in defaults.items():
        value = message.get(key)
        settings[key] = value if value is not None else default_value
    return settings


async def handle_client(model_manager, reader, writer):
    """
    Services one connected windows_app client for the lifetime of the
    connection: reads newline-delimited JSON messages, replies to pings,
    tracks audio level stats, and manages the current session's speech
    segmentation and transcription.

    A "session" is bounded by explicit session_started/session_stopped
    messages from windows_app. audio_chunk, hotkey_triggered, and
    auto_suggest_changed messages are only processed while a session is
    active — either arriving outside a session is ignored (defensive:
    windows_app should only be sending them during a session anyway).
    settings_changed just remembers the values it carries (pending_settings)
    for whichever session starts next — windows_app sends it once, right
    before session_started, every time Start Session is pressed, so it's
    never applied to an already-running session; auto_suggest_changed is
    the one setting that's different, since it's meant to be flipped live
    mid-session (see SuggestionTrigger.set_auto_suggest_enabled) rather
    than only taking effect on the next session. Starting a session creates
    a fresh MeetingSession (fresh segmenters -- one per audio source, fresh
    claude CLI process, empty transcript context) using those settings, so
    no state bleeds
    across sessions; stopping one flushes whatever segment was still open
    (see the session_stopped branch below) so a sentence still being
    spoken right as the session stops isn't silently dropped, then tears
    the session down. If the connection itself drops mid-session (no
    explicit session_stopped), the `finally` block below still tears it
    down, so the claude CLI process it started is never left running with
    nothing using it.

    If starting a session fails (e.g. the claude CLI process can't be
    started), no MeetingSession is created and a session_start_failed
    message is sent back instead of just dropping the connection, so
    windows_app can revert its UI to a clean pre-session state rather than
    getting stuck showing a session as active.

    generate_summary (Day 23) is the one message type here that's NOT
    session-scoped -- handled the same way regardless of whether `session`
    is currently set, since windows_app normally sends it after a session
    has already ended (see generate_and_send_summary()).
    """
    peer = writer.get_extra_info("peername")
    print(f"Client connected: {peer}")
    audio_trackers = {source: AudioLevelTracker(source) for source in AUDIO_SOURCES}
    session = None
    pending_settings = None
    # Guards generate_and_send_summary() -- see its docstring. One per
    # connection, independent of `session`: a summary is normally requested
    # after a session has already ended (and torn its own session down), so
    # this can't just reuse a session's own _claude_cli_lock.
    summary_lock = asyncio.Lock()
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            message = json.loads(line)
            message_type = message.get("type")

            if message_type == "session_started":
                settings = pending_settings or default_settings()
                attempt_id = message.get("attempt_id")
                try:
                    new_session = MeetingSession(
                        writer,
                        model_manager.model,
                        settings["mode"],
                        settings["suggestion_pause_seconds"],
                        settings["context_notes"],
                        settings["auto_suggest_enabled"],
                        settings["live_agent_export_enabled"],
                    )
                except Exception as error:
                    print(f"Failed to start session: {error}")
                    traceback.print_exc()
                    await send_message(
                        writer,
                        {"type": "session_start_failed", "reason": str(error), "attempt_id": attempt_id},
                    )
                else:
                    session = new_session
                    print(
                        f"Session started (mode={settings['mode']}, "
                        f"suggestion_pause={settings['suggestion_pause_seconds']}s)"
                    )
                    if session.live_agent_log_path is not None:
                        print(f"Live-agent-listening log: {session.live_agent_log_path}")
            elif message_type == "session_stopped":
                if session is not None:
                    # Flushes any segment still open, on EITHER source,
                    # when the session stops (someone stops right after
                    # finishing a sentence, before a silence gap has closed
                    # it on its own) so it isn't silently dropped. Reported
                    # with session=None (the default): by the time this
                    # transcribes, the session below is already closing,
                    # so there's no live suggestion trigger left worth
                    # feeding it into -- it's still shown in the
                    # transcript pane either way.
                    for source, segmenter in session.segmenters.items():
                        leftover_segment = segmenter.close_open_segment()
                        if leftover_segment is not None:
                            handle_finished_segment(
                                session.model, model_manager.transcribe_lock, session.writer, leftover_segment, source
                            )
                    await session.close()
                    session = None
                print("Session stopped")
            elif message_type == "audio_chunk":
                if session is not None:
                    handle_audio_chunk(audio_trackers, model_manager.transcribe_lock, session, message)
            elif message_type == "hotkey_triggered":
                if session is not None:
                    print("Hotkey pressed: generating a suggestion now")
                    session.trigger.notify_hotkey_pressed()
            elif message_type == "settings_changed":
                pending_settings = settings_from_message(message)
            elif message_type == "auto_suggest_changed":
                if session is not None:
                    session.set_auto_suggest_enabled(bool(message.get("enabled", False)))
            elif message_type == "generate_summary":
                asyncio.create_task(
                    generate_and_send_summary(writer, message.get("transcript", ""), summary_lock)
                )
            else:
                reply = await build_reply(message)
                if reply is not None:
                    await send_message(writer, reply)
    finally:
        if session is not None:
            await session.close()
        print(f"Client disconnected: {peer}")
        writer.close()
        await writer.wait_closed()


async def run_server(model_manager):
    """
    Starts the asyncio TCP server on localhost and serves clients until stopped.
    """
    port = load_port()

    async def handle_client_connection(reader, writer):
        """Adapts handle_client to asyncio.start_server's (reader, writer) callback shape."""
        await handle_client(model_manager, reader, writer)

    server = await asyncio.start_server(
        handle_client_connection, "127.0.0.1", port, limit=MAX_MESSAGE_LINE_BYTES
    )
    print(f"wsl_app listening on 127.0.0.1:{port}")
    async with server:
        await server.serve_forever()


# Shared by both mode instructions below (RESPOND_WITH_LEAD_AND_BULLETS_
# INSTRUCTION and INTERVIEW_RESPOND_INSTRUCTION). Added Day 19, after real
# mic-capture testing produced a suggestion that abandoned the required
# lead+bullets shape entirely to comment on the input instead: "This is a
# direct technical/definitional question, so answer it straight — a clear
# definition first, not a story. Ignore the noisy transcript lines above,
# that's just mic/transcription garbage." That's a real, reproducible
# category of failure specific to feeding the model formatted, labeled
# transcript text (the new "You: ...\nThem: ..." shape both sources'
# rolling context is built from, see MeetingSession._format_context) rather
# than one continuous plain-text blob: the model apparently reads the
# labeled, multi-line shape as something closer to a document to react to
# than plain conversational context, and both mode instructions' existing
# generic "no meta-commentary" line wasn't specific enough to rule out
# commentary about the *input's own quality* as a special case worth
# surfacing anyway. The same session's very first suggestion showed a
# milder version of the same underlying looseness -- a bare single
# sentence with no bullets at all, for a genuinely thin/early exchange --
# so this also explicitly rules out that fallback. Naming the exact
# failure mode observed, rather than just generically re-emphasizing "no
# meta-commentary," matches this project's own established fix pattern for
# prompt-shape corner cases (see INTERVIEW_RESPOND_INSTRUCTION's own Day 18
# revision history below).
IGNORE_TRANSCRIPT_NOISE_INSTRUCTION = (
    "The transcript excerpt below is real-time automatic speech-to-text, "
    "labeled by who's speaking -- it will sometimes contain misheard "
    "words, garbled fragments, or lines that don't make sense in context. "
    "That's expected and not something to point out: silently work around "
    "anything that looks like a transcription error, use whatever real "
    "content is there, and never comment on transcript quality, mention "
    "noise, errors, garbled text, or transcription in your answer. This "
    "applies no matter how thin, early, or awkward the exchange looks -- "
    "always answer in the exact format below, never a bare sentence, "
    "single paragraph, or any other shape instead of it."
)

# Shared by both mode instructions, same as IGNORE_TRANSCRIPT_NOISE_
# INSTRUCTION above. Added Day 19, from a real user-reported example: asked
# to walk through HTTP caching, the suggestion's lead and every one of its
# bullets were meta-instructions about what to cover ("Explain the basic
# idea...", "Cover cache-control headers like max-age, no-cache, and
# no-store", "Mention validation methods such as ETag and Last-Modified")
# rather than the actual explanation content itself -- technically the
# right shape (a lead plus bullets), but each line told the user what to
# say instead of giving them something to say. Reproduced directly against
# the real CLI afterward (meeting mode specifically): the lead came back as
# "Give a clear step-by-step walkthrough of..." / "Explain that
# invalidation is more about..." -- second-person meta-advice, not content
# in the user's own voice, even though the existing "specific details... "
# bullet wording was already fairly explicit. The real fix needed to be
# concrete (a named good/bad example, using the user's own real report)
# rather than another abstract adjective, matching this project's own
# established pattern for prompt-shape fixes -- see INTERVIEW_RESPOND_
# INSTRUCTION's Day 18 revision history below for the same lesson learned
# earlier.
CONTENT_NOT_OUTLINE_INSTRUCTION = (
    "Every line of the answer -- the lead and every bullet -- must BE "
    "actual content: a real fact, term, number, or example, in the user's "
    "own voice, as if they were saying it out loud. Never write a line "
    "that just names a topic or instructs the user what to say -- that's "
    "an outline, not an answer, and it leaves the user to supply the real "
    "content themselves instead of giving it to them. For example, if "
    "asked to explain HTTP caching, do NOT write a bullet like \"Cover "
    "cache-control headers like max-age, no-cache, and no-store\" -- "
    "write the real content directly instead, e.g. \"max-age sets how "
    "many seconds a response stays fresh; no-cache forces revalidation "
    "with the server before reuse; no-store disables caching entirely.\" "
    "A line that starts with a verb like \"Explain\", \"Cover\", "
    "\"Mention\", \"Discuss\", \"Note\", or \"Describe\", and could be "
    "rewritten by deleting that verb without losing any information, is "
    "an outline line -- rewrite it to contain the actual substance "
    "instead."
)

# The framing given to every prompt sent through a session's ClaudeCli,
# chosen per session by mode (see MeetingSession). Both end with the same
# instruction. Originally (Day 6) this asked for a single ready-to-read
# spoken line, after testing showed the model otherwise answering with
# multiple options and meta-commentary ("Here's a natural way to continue:
# ... Or shorter/more neutral: ..."). Changed (Phase 1.5, Day 12) to ask for
# terms/concepts instead of a script, but that didn't hold up in real use —
# reverted (Phase 1.5b, Day 13) back to a script. Changed again (Phase 2.5,
# Day 15) to a short lead plus bullets — a fixed script read back verbatim
# didn't match how the user actually used it in real sessions; a lead the
# user skims plus bullets they pick from fits real use better, closer to
# how the real ParakeetAI presents suggestions (PRD §8, Phase 2.5). The
# literal "- " bullet markers and one-point-per-line instruction are
# deliberate, not decorative — windows_app's suggestion panes render
# whatever text comes back as-is (see create_suggestions_section() and
# create_overlay_window()), so the CLI's raw output has to already be in a
# genuinely renderable bullet shape, not prose that merely mentions bullets.
RESPOND_WITH_LEAD_AND_BULLETS_INSTRUCTION = (
    "Respond in plain text only — no markdown headers, bold, or numbered "
    "lists — in exactly this shape:\n\n"
    "First, a lead of 1-2 sentences: the actual start of what the user "
    "could say out loud, in their own voice, as if they were speaking it "
    "right now -- not a description of how they should respond or what "
    "they should cover.\n"
    "Then, a blank line, followed by 3-5 bullet points, each on its own "
    "line starting with \"- \" — specific details, angles, reasons, or "
    "examples the user could pull from to build their actual answer. One "
    "point per line, no sub-bullets, no further punctuation before the "
    "dash.\n\n"
    "The user will skim the lead, then pick whichever bullets actually fit "
    "what they want to say — this is not a fixed script to read back "
    "verbatim. Keep the lead and each bullet short enough to skim in a few "
    "seconds; no meta-commentary, no multiple alternative versions.\n\n"
    + CONTENT_NOT_OUTLINE_INSTRUCTION + "\n\n"
    + IGNORE_TRANSCRIPT_NOISE_INSTRUCTION
)

MEETING_SYSTEM_PROMPT = (
    "You are assisting the user live during a work meeting. Given a snippet "
    "of recent conversation, suggest how the user could respond to what's "
    "being discussed. " + RESPOND_WITH_LEAD_AND_BULLETS_INSTRUCTION
)

# Interview mode's own shape, separate from RESPOND_WITH_LEAD_AND_BULLETS_INSTRUCTION
# above (Meeting mode keeps that shorter, casual shape unchanged). Revised
# Day 18 after a real example (a full behavioral-interview answer) showed
# the short lead-plus-bullets format read as too terse for this use case --
# interview answers, especially behavioral ones, are expected to be a real
# told-as-a-story example, not a menu of talking points.
#
# Revised again same day after live testing (a plain "What is an SQL
# injection attack?" question) showed the first version of this prompt
# forced *every* interview question into that narrative-story shape,
# including ones that aren't behavioral at all -- it fabricated an
# unrelated personal anecdote about a slow database query instead of just
# explaining what SQL injection is. Interview questions aren't uniformly
# behavioral: a definitional/conceptual/technical one calls for a direct
# explanation, the way a real reference answer (or ParakeetAI's own
# example for this exact question, shared by the user) actually looks --
# a short definition lead, then bullets grouped by category (how it
# works, impact, prevention), not a fictional project story. So this now
# gives the model both shapes and asks it to pick the one that actually
# fits the question, rather than a single fixed template.
#
# Same plain-text constraint as the shared instruction (no markdown
# headers/bold/numbered lists): windows_app renders whatever comes back
# as-is (see create_overlay_window() / create_suggestions_section()), so
# "- " bullets still have to be literal, renderable bullets, not prose
# that mentions them, and a bullet's category name is plain "Label:" text
# rather than markdown bold.
INTERVIEW_RESPOND_INSTRUCTION = (
    "Respond in plain text only — no markdown headers, bold, or numbered "
    "lists. First decide what kind of question this is, then answer in "
    "the matching shape below -- don't force a personal story onto a "
    "question that isn't asking for one, and don't give a bare dictionary "
    "definition to one that is.\n\n"
    "If it's a behavioral or experience question (asks about a past "
    "situation, a time you did something, or how you'd work with "
    "someone), answer as a real-sounding narrative:\n"
    "- A lead of 3-5 sentences, formal tone, setting up a specific example "
    "-- the situation, who was involved, what the user did -- as if "
    "recounting an actual past experience, not a hypothetical.\n"
    "- A blank line, then 3-5 bullet points, each on its own line starting "
    "with \"- \", each leading with a short label naming the skill or "
    "approach being shown (e.g. \"Active listening:\", \"Data-driven "
    "evaluation:\") followed by the specific detail from the example.\n"
    "- A blank line, then one closing sentence: the lesson or takeaway "
    "from the example.\n\n"
    "If it's a technical, conceptual, or definitional question (\"what is "
    "X\", \"how does Y work\", a coding or system-design question) -- "
    "answer directly, with no invented personal story:\n"
    "- A lead of 1-3 sentences giving a direct, correct explanation of "
    "what's being asked.\n"
    "- A blank line, then 3-6 bullet points, each on its own line starting "
    "with \"- \", grouping the explanation into the categories a strong "
    "answer would actually cover (e.g. how it works, why it matters or "
    "its impact, how to prevent or solve it) -- each bullet leading with "
    "a short label for its category, followed by the specific detail.\n\n"
    "Either way: one point per line, no sub-bullets, no further "
    "punctuation before the dash, formal tone, complete but not sprawling "
    "-- meant to be read nearly as-is, not a skimmable list of options. No "
    "meta-commentary, no multiple alternative versions.\n\n"
    + CONTENT_NOT_OUTLINE_INSTRUCTION + "\n\n"
    + IGNORE_TRANSCRIPT_NOISE_INSTRUCTION
)

INTERVIEW_SYSTEM_PROMPT = (
    "You are assisting the user live during a job interview, in which the "
    "user is the candidate being interviewed. Given a snippet of the "
    "interviewer's most recent question or remark, suggest how the user "
    "could answer it. " + INTERVIEW_RESPOND_INSTRUCTION
)

SYSTEM_PROMPT_BY_MODE = {
    "meeting": MEETING_SYSTEM_PROMPT,
    "interview": INTERVIEW_SYSTEM_PROMPT,
}

# Framing for a post-meeting summary (Day 23, PRD §8 Phase 3) -- one shared
# prompt for both modes rather than per-mode (MEETING_SYSTEM_PROMPT/
# INTERVIEW_SYSTEM_PROMPT), since key points/decisions/action items are a
# sensible shape for either a work meeting or an interview and the scope
# here doesn't call for two near-duplicate prompts. Unlike a suggestion,
# there's no strict "one skimmable line" or "renderable bullet markup"
# constraint from a Qt overlay -- the summary is read in its own pane and
# exported to a plain-text/markdown file -- but the literal "- " bullets
# and no-markdown-headers rule are kept anyway, matching this project's
# established, already-verified-renderable shape (see
# RESPOND_WITH_LEAD_AND_BULLETS_INSTRUCTION) rather than inventing a new
# one just for this.
SUMMARY_SYSTEM_PROMPT = (
    "You are generating a written summary of a work meeting or job "
    "interview that just finished, from its full transcript below. The "
    "transcript is real-time automatic speech-to-text, labeled by who's "
    "speaking (\"You\" is the user, \"Them\" is everyone else) -- it will "
    "sometimes contain misheard words or garbled fragments; silently work "
    "around anything that looks like a transcription error rather than "
    "commenting on it.\n\n"
    "Respond in plain text only -- no markdown headers, bold, or numbered "
    "lists -- using exactly these three section labels, each on its own "
    "line, in this order:\n\n"
    "KEY POINTS\n"
    "3-8 bullet points, each on its own line starting with \"- \", "
    "covering the main topics actually discussed.\n\n"
    "DECISIONS\n"
    "Bullet points in the same \"- \" shape for anything the conversation "
    "actually settled or agreed on. If nothing was decided, write a "
    "single bullet: \"- None recorded.\"\n\n"
    "ACTION ITEMS\n"
    "Bullet points in the same \"- \" shape for concrete next steps or "
    "follow-ups mentioned, naming who owns each one if that was said. If "
    "none were mentioned, write a single bullet: \"- None recorded.\"\n\n"
    "One point per line, no sub-bullets. Base every bullet only on what's "
    "actually in the transcript below -- never invent a point, decision, "
    "or action item that wasn't really said, and never comment on "
    "transcript quality or transcription errors in the summary itself."
)


def build_system_prompt(mode, context_notes):
    """
    Builds the full system prompt for a session: the mode's base framing,
    plus the user's pre-session context notes (a CV/job description, a
    PRD/agenda excerpt) appended if they provided any. Left off entirely
    when context_notes is empty, so a session with nothing pasted in
    behaves exactly as before this was added.

    Returns:
        str: the system prompt to start this session's ClaudeCli with.
    """
    prompt = SYSTEM_PROMPT_BY_MODE.get(mode, MEETING_SYSTEM_PROMPT)
    if context_notes:
        prompt += (
            "\n\nThe user has also provided the following context notes for "
            "this session — use them to inform your suggestions where "
            "relevant:\n" + context_notes
        )
    return prompt


class ClaudeCli:
    """
    Keeps one `claude` command-line process running in the background and
    lets short text prompts be sent to it one at a time, getting a text
    answer back for each. Starting the process takes a few seconds, so it's
    started once and reused for every prompt rather than restarted each
    time — later prompts answer noticeably faster than the first because of
    this. The same running process also remembers earlier prompts on its
    own, so later prompts can refer back to earlier ones without this class
    needing to resend the earlier conversation itself.

    Used by MeetingSession, one per active meeting session (see below) —
    not shared across sessions, and not run until a session actually
    starts. wsl_app/test_claude_cli.py is still there as a standalone way
    to try this class out on its own, outside the live pipeline.
    """

    def __init__(self, system_prompt):
        """Starts the claude command-line process running in the background, framed by `system_prompt`, ready for prompts."""
        self._process = subprocess.Popen(
            [
                "claude",
                "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--system-prompt", system_prompt,
                "--tools", "",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            # Found live, Day 19: without an explicit cwd, this subprocess
            # inherits wsl_app's own working directory, which sits inside
            # this project -- and the claude CLI auto-discovers and loads
            # this project's own CLAUDE.md into context regardless of
            # --system-prompt (that flag only replaces the top-level system
            # framing; CLAUDE.md auto-discovery is a separate mechanism it
            # doesn't disable). Confirmed directly: the exact same prompts
            # sent from this project's own directory produced suggestions
            # that referenced this project's own dev history ("Day 18
            # note on INTERVIEW_SYSTEM_PROMPT", "project_notes.md",
            # "live-verification gap") instead of answering the actual
            # question -- a real, reproducible contamination bug, not a
            # one-off. Re-running the identical prompts with cwd pointed
            # outside the project (a plain temp directory, verified to have
            # no CLAUDE.md of its own anywhere up its path) eliminated it
            # completely across every case tested. This has likely been a
            # latent risk since Day 6 (ClaudeCli has always run without an
            # explicit cwd), only surfacing visibly now because real
            # conversation content can resemble topics this now-large (29KB+)
            # CLAUDE.md happens to discuss (e.g. "SQL injection",
            # "interview question") closely enough to get pulled in.
            cwd=tempfile.gettempdir(),
        )
        threading.Thread(target=self._log_error_output, daemon=True).start()

    def _log_error_output(self):
        """
        Runs for the lifetime of the process on its own background thread:
        reads anything the claude command-line process writes to its error
        output and logs it. Error output is a separate stream from the one
        answers come back on, so this can't block or interfere with ask()
        — it just means an unexpected error is never silently lost.
        """
        for line in self._process.stderr:
            print(f"claude CLI error output: {line.rstrip()}")

    def ask(self, prompt_text, on_partial_text=None):
        """
        Sends one short text prompt to the running claude process and waits
        for its complete answer. Safe to call more than once on the same
        ClaudeCli — each call continues the same ongoing conversation.

        If `on_partial_text` is given, it's called from this same thread
        with the answer-so-far every time more of it streams in, before
        the complete answer is ready -- see --include-partial-messages in
        __init__ and _read_next_answer(). A suggestion can otherwise take
        7-12s to generate in full (confirmed live, Day 18: real answers
        this long, using the real claude CLI, not a guess) with nothing
        shown the whole time; letting the caller forward each growing
        snapshot as it arrives fixes the *perceived* wait without changing
        how long generation actually takes.

        Raises:
            RuntimeError: if the claude process has crashed — either its
            stdin is already closed (can't send the prompt at all) or it
            exits before sending back a complete answer. Callers that want
            to survive a crash (see MeetingSession) should catch this and
            create a fresh ClaudeCli.

        Returns:
            str: the complete answer text.
        """
        input_line = {"type": "user", "message": {"role": "user", "content": prompt_text}}
        try:
            self._process.stdin.write(json.dumps(input_line) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError) as error:
            raise RuntimeError("claude CLI process is not running") from error
        return self._read_next_answer(on_partial_text)

    def _read_next_answer(self, on_partial_text=None):
        """
        Reads output lines from the claude process one at a time. Most
        lines (session init, rate-limit events, and -- with
        --include-partial-messages -- one "stream_event" line per text
        chunk as the answer is generated) are intermediate; this calls
        on_partial_text (if given) with the answer-so-far each time a
        "stream_event" line carries a real text_delta chunk, and keeps
        reading until the final line -- marked "type": "result" -- arrives.
        The accumulated stream-event text is a live preview only; the
        "result" line's own "result" field (not the accumulated text) is
        what's actually returned, since it's the CLI's own authoritative
        final answer.

        Returns:
            str: the answer text carried on the result line.
        """
        accumulated_text = ""
        while True:
            line = self._process.stdout.readline()
            if not line:
                raise RuntimeError("claude CLI process ended unexpectedly")
            output = json.loads(line)
            output_type = output.get("type")
            if output_type == "result":
                return output.get("result", "")
            if output_type == "stream_event" and on_partial_text is not None:
                event = output.get("event", {})
                if event.get("type") == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
                    accumulated_text += event["delta"]["text"]
                    on_partial_text(accumulated_text)

    def stop(self):
        """Shuts the claude process down cleanly and waits for it to exit."""
        self._process.stdin.close()
        self._process.wait()


class LiveAgentLog:
    """
    Writes an opt-in, plain-text, real-time-flushed log of one session's
    transcript segments and real suggestion-trigger moments to LIVE_AGENT_LOG_PATH
    -- a single fixed file, not one generated fresh per session (see that
    constant's own comment) -- the data feed for "live-agent-listening"
    (see docs/LIVE_AGENT_LISTENING.md): a separate `claude` terminal
    session, `tail -f`-ing this file via a background Bash task and
    Monitor (the same mechanism proven in docs/DEV_PLAN.md's Day
    18.8-18.10, now startable with one command -- see
    start_live_agent_listening.sh), can react to SEGMENT lines as cheap
    background notifications and produce one real, grounded answer
    whenever a TRIGGER line arrives -- with near-zero latency at that
    point, since the conversation is already live in that agent's own
    context rather than reconstructed per call the way ClaudeCli's
    one-shot suggestions are. Off by default and no behavior change at all
    unless the user opts in -- same consent stance as SessionRecorder
    (windows_app/main.py, Day 22): this also continuously writes real
    conversation content to disk.

    Deliberately NOT the same file as SessionRecorder's session recording:
    that file has no "decide now" signal in it (a TRIGGER moment there is
    indistinguishable from an already-generated suggestion line) and its
    privacy checkbox means something different to the user than opting
    into this. record_segment()/record_trigger() are safe no-ops while no
    file is open, matching SessionRecorder's own idiom, and each line is
    flushed immediately so a crash mid-session doesn't lose an otherwise-
    complete log.
    """

    def __init__(self):
        """Starts with no file open -- start() opens one when an exporting session begins."""
        self._file = None

    def start(self, mode, context_notes, auto_suggest_enabled, pause_seconds):
        """
        Opens LIVE_AGENT_LOG_PATH -- truncating and overwriting whatever an
        earlier session may have left there, since it's a single fixed
        filename (see that constant's own comment for why) -- and writes
        its header: start time, mode, and the auto_suggest/pause_seconds
        settings that govern whether and how often TRIGGER lines can
        actually appear (see class docstring -- TRIGGER lines are entirely
        piggybacked on the existing SuggestionTrigger, there's no
        independent cadence for this feature), plus context_notes verbatim
        if any were given, since a tailing agent's own operating
        instructions should match the same mode/context framing
        MEETING_SYSTEM_PROMPT/INTERVIEW_SYSTEM_PROMPT already use.

        Returns:
            Path: the opened file's path (always LIVE_AGENT_LOG_PATH), so
            the caller (handle_client) can still print it for anyone not
            using start_live_agent_listening.sh's hardcoded copy of it.
        """
        LIVE_AGENT_LOG_DIR.mkdir(exist_ok=True)
        started_at = time.localtime()
        path = LIVE_AGENT_LOG_PATH
        self._file = open(path, "w", encoding="utf-8")
        self._write(
            f"=== live-agent-listening session started {time.strftime('%Y-%m-%d %H:%M:%S', started_at)} "
            f"(mode: {mode}, auto_suggest: {'on' if auto_suggest_enabled else 'off'}, "
            f"suggestion_pause: {pause_seconds}s) ==="
        )
        if context_notes:
            self._write("--- context notes begin ---")
            self._write(context_notes)
            self._write("--- context notes end ---")
        return path

    def stop(self):
        """Writes a footer line and closes the file, if one is open. A safe no-op otherwise."""
        if self._file is None:
            return
        self._write(f"=== live-agent-listening session ended {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        self._file.close()
        self._file = None

    def record_segment(self, source, text):
        """Appends one timestamped, source-labeled transcript segment ("You:"/"Them:", matching _format_context()'s own shape), if a session is currently exporting."""
        if self._file is None:
            return
        timestamp = time.strftime("%H:%M:%S")
        self._write(f"{timestamp} SEGMENT {SOURCE_LABELS[source]}: {text}")

    def record_trigger(self):
        """Appends one timestamped TRIGGER line -- the cue for a tailing agent to produce one real answer -- if a session is currently exporting."""
        if self._file is None:
            return
        timestamp = time.strftime("%H:%M:%S")
        self._write(f"{timestamp} TRIGGER")

    def _write(self, line):
        """Writes one line to the open file and flushes immediately, so partial content already on disk survives a crash."""
        self._file.write(line + "\n")
        self._file.flush()


class SuggestionTrigger:
    """
    Decides when to ask claude for a new suggestion. Built on top of the
    transcript segments already being produced, rather than watching raw
    audio silence with a second, separate watcher:

      1. Every time a new transcribed segment comes in (notify_new_segment),
         the pause timer (re)starts from zero, and this segment is
         remembered as "new since the last suggestion."
      2. If the pause timer ever finishes — meaning its configured pause
         duration passed with no further new segment — a suggestion is generated,
         but only if step 1 happened at least once since the last
         suggestion. This is what stops a long stretch of silence with
         nothing new said from generating a suggestion over and over.
      3. Pressing the hotkey (notify_hotkey_pressed) generates a suggestion
         immediately, no matter what the pause timer is doing, and counts
         as a suggestion having just been generated (same bookkeeping as
         step 2) — so a normal pause right afterward, with nothing new
         said, won't immediately fire again for the same content.

    Once stopped, permanently ignores both notify_new_segment() and
    notify_hotkey_pressed() — not just the pause timer that happened to be
    running at the moment of stop(). This matters because a segment that
    was already mid-transcription when the session's connection dropped
    can still call notify_new_segment() *after* stop() has already run (see
    MeetingSession.close()); without this, that late call would start a
    brand new pause timer, which would go on to generate a suggestion (and
    spawn a fresh claude CLI process to do it) for a session that no longer
    has anywhere to send it — an orphaned process with nothing left to stop
    it.

    Auto-suggest (see set_auto_suggest_enabled) is a separate, live-
    toggleable on/off switch for step 2 only: while disabled, a new segment
    is still noted (so a suggestion can still reflect it once re-enabled or
    once the hotkey is pressed), but no pause timer is ever scheduled for
    it, so a pause during a real, ongoing meeting genuinely produces no
    suggestion rather than one that's merely discarded when the timer
    fires. Step 3 (the hotkey) is never affected by this switch — the
    hotkey/button path always works, on or off.
    """

    def __init__(self, generate_suggestion, pause_seconds, auto_suggest_enabled=False):
        """
        Stores the async function to call when the trigger fires, and how
        long the pause timer (step 2 above) should wait. Starts idle: no
        segment seen yet, no pause timer running.
        """
        self._generate_suggestion = generate_suggestion
        self._pause_seconds = pause_seconds
        self._auto_suggest_enabled = auto_suggest_enabled
        self._new_segment_since_last_suggestion = False
        self._pause_timer_task = None
        self._stopped = False

    def notify_new_segment(self):
        """
        Call once for every newly-transcribed segment. See class docstring,
        step 1. A no-op once stopped. Only schedules the pause timer while
        auto-suggest is enabled (see set_auto_suggest_enabled) — otherwise
        the segment is remembered, but nothing is scheduled to fire from it.
        """
        if self._stopped:
            return
        self._new_segment_since_last_suggestion = True
        self._cancel_pause_timer()
        if self._auto_suggest_enabled:
            self._pause_timer_task = asyncio.create_task(self._wait_then_fire())

    def set_auto_suggest_enabled(self, enabled):
        """
        Turns pause-triggered suggestions on or off, live, without touching
        the hotkey/button path (see class docstring). Turning it off cancels
        whatever pause timer is currently running, if any, so a pause
        already in progress at the moment it's switched off doesn't still
        fire.
        """
        self._auto_suggest_enabled = enabled
        if not enabled:
            self._cancel_pause_timer()

    def notify_hotkey_pressed(self):
        """Call when windows_app reports the hotkey was pressed. See class docstring, step 3. A no-op once stopped."""
        if self._stopped:
            return
        self._cancel_pause_timer()
        self._new_segment_since_last_suggestion = False
        asyncio.create_task(self._generate_suggestion())

    def stop(self):
        """
        Cancels any pause timer in flight and permanently disables future
        triggers. Call when the session ends, so nothing — including a
        transcript segment that finishes after this call — can fire a
        suggestion for it again.
        """
        self._stopped = True
        self._cancel_pause_timer()

    def _cancel_pause_timer(self):
        """Stops the currently running pause timer, if one is running."""
        if self._pause_timer_task is not None:
            self._pause_timer_task.cancel()
            self._pause_timer_task = None

    async def _wait_then_fire(self):
        """Waits out the pause duration, then fires. See class docstring, step 2."""
        await asyncio.sleep(self._pause_seconds)
        if not self._new_segment_since_last_suggestion:
            return
        self._new_segment_since_last_suggestion = False
        await self._generate_suggestion()


class MeetingSession:
    """
    Everything that lives for the span of one meeting session — from
    session_started to session_stopped — and needs to be created fresh
    each time and cleanly torn down together: speech segmentation (one
    independent VoiceSegmenter per audio source -- see self.segmenters),
    the running claude CLI process behind it, the pause/hotkey suggestion
    trigger, a short rolling history of recent transcript segments (from
    both sources, chronologically ordered -- see add_transcript_segment) to
    give suggestions context, and the opt-in live-agent-listening export
    log (see LiveAgentLog). If the claude CLI process crashes mid-session,
    one restart is attempted automatically (see _ask_with_restart_on_crash)
    — recent_transcript_segments lives here, not in ClaudeCli, so a restart
    doesn't lose the transcript context built up so far.
    """

    def __init__(
        self, writer, model, mode, pause_seconds, context_notes="", auto_suggest_enabled=False,
        live_agent_export_enabled=False,
    ):
        """
        Starts a fresh session using the settings captured for it at
        session_started (see handle_client): a fresh VoiceSegmenter per
        audio source, each using that source's own VAD strictness (see
        SPEECH_DETECTION_STRICTNESS_BY_SOURCE -- mic and loopback each need
        their own segmenter and strictness, so one source's speech/pauses
        never affect the other's segment boundaries), `model` as the
        Whisper model this session transcribes
        with for its entire lifetime, a new claude CLI process framed for
        `mode` and `context_notes`, empty transcript history, a
        suggestion trigger using `pause_seconds` and starting with
        auto-suggest set to `auto_suggest_enabled`, and -- if
        `live_agent_export_enabled` -- a started LiveAgentLog (self.
        live_agent_log_path is None otherwise, for handle_client to check).

        self.live_agent_log is started as the LAST statement here,
        deliberately after self.trigger -- ClaudeCli(...) above is the one
        call in this constructor that can raise (a subprocess start
        failure; handle_client's existing try/except around this whole
        constructor already turns that into a session_start_failed message
        to windows_app), and starting the log first would leave an opened-
        but-never-stop()'d file behind if that raised.
        """
        self.writer = writer
        self.model = model
        self.segmenters = {
            source: VoiceSegmenter(SPEECH_DETECTION_STRICTNESS_BY_SOURCE[source]) for source in AUDIO_SOURCES
        }
        # Each entry is (started_at, source, text), kept sorted by
        # started_at -- see add_transcript_segment().
        self.recent_transcript_segments = []
        self._system_prompt = build_system_prompt(mode, context_notes)
        print(f"Starting claude CLI process for this session (mode: {mode})...")
        self._claude_cli = ClaudeCli(self._system_prompt)
        self._ask_call_count = 0
        # Guards every use of self._claude_cli's ask()/stop(): only one of
        # those may run at a time, since the CLI process has no way to tell
        # two concurrent requests' answers apart on its shared stdin/stdout,
        # and close() must never stop the process while an ask() started by
        # the pause timer or the hotkey is still using it.
        self._claude_cli_lock = asyncio.Lock()
        self.trigger = SuggestionTrigger(self._generate_and_send_suggestion, pause_seconds, auto_suggest_enabled)
        self.live_agent_log = LiveAgentLog()
        self.live_agent_log_path = (
            self.live_agent_log.start(mode, context_notes, auto_suggest_enabled, pause_seconds)
            if live_agent_export_enabled else None
        )

    def set_auto_suggest_enabled(self, enabled):
        """Turns this session's pause-triggered suggestions on or off, live. See SuggestionTrigger.set_auto_suggest_enabled."""
        self.trigger.set_auto_suggest_enabled(enabled)

    def add_transcript_segment(self, source, text, started_at):
        """
        Adds one newly-transcribed segment -- tagged with which audio
        source it came from and when it started being spoken -- to the
        rolling context window, then lets the suggestion trigger know new
        content has arrived. Inserted in started_at order (bisect.insort)
        rather than just appended, since mic and loopback transcribe
        independently and can finish in a different order than they were
        actually spoken in (e.g. a longer segment on one source started
        first but takes longer to transcribe than a shorter segment on the
        other) -- this keeps the context Claude sees in the same
        chronological order the conversation actually happened in, not
        transcription-completion order. See _trim_context_to_budget() for
        how the window is kept bounded. Also records this segment to the
        live-agent-listening export log, if one is open -- a safe no-op
        otherwise.
        """
        bisect.insort(self.recent_transcript_segments, (started_at, source, text))
        self._trim_context_to_budget()
        self.trigger.notify_new_segment()
        self.live_agent_log.record_segment(source, text)

    def _trim_context_to_budget(self):
        """
        Drops the oldest kept segments, one at a time, until the formatted
        context text (see _format_context) fits within
        CONTEXT_CHARACTER_BUDGET -- always leaves at least one segment,
        even if that one segment alone exceeds the budget, so a single
        long segment can't empty the context entirely.
        """
        while len(self.recent_transcript_segments) > 1 and len(self._format_context()) > CONTEXT_CHARACTER_BUDGET:
            self.recent_transcript_segments.pop(0)

    def _format_context(self):
        """
        Returns:
            str: the rolling transcript context formatted for the claude
            prompt -- one labeled line per kept segment (e.g. "Them: ...",
            "You: ...", see SOURCE_LABELS), in chronological order, so the
            model can tell who said what.
        """
        return "\n".join(f"{SOURCE_LABELS[source]}: {text}" for _started_at, source, text in self.recent_transcript_segments)

    async def _generate_and_send_suggestion(self):
        """
        Asks claude for one suggestion based on the recent transcript
        context and sends it to windows_app as a "suggestion" message,
        carrying both the answer text and the "question" field (Day 18,
        Phase 2 visual redesign) -- the same rolling transcript context
        (prompt_text) that was actually sent to claude, reused as-is rather
        than tracked separately, so the overlay can show what prompted a
        suggestion right above it. Runs ask() on a background thread via
        asyncio.to_thread, since it blocks on the subprocess and must not
        stall audio_chunk handling while a suggestion is being generated.

        Also sends the same "suggestion" message shape repeatedly, once
        per streamed chunk, as the answer is still generating (see
        ClaudeCli.ask()'s on_partial_text) -- windows_app's SuggestionDisplay
        already just overwrites the pane with whatever text it's given, so
        each growing snapshot naturally renders as the answer filling in
        live, with no protocol or UI change needed beyond sending more
        often. A real suggestion can take 7-12s to generate in full
        (confirmed live, Day 18); this doesn't make that faster, only
        visible sooner. _generate_and_send_suggestion runs on the event
        loop, but ClaudeCli.ask()'s partial-text calls happen from its own
        worker thread (see asyncio.to_thread below) -- run_coroutine_threadsafe
        is what actually gets each partial send back onto the loop safely
        from there.

        Skipped (not queued) if a suggestion is already being generated —
        the pause timer and the hotkey are two independent ways to reach
        this method, and running two ask() calls on the same claude CLI
        process at once would have no way to tell which answer belongs to
        which request.

        If the claude CLI process has crashed, one restart is attempted
        (see _ask_with_restart_on_crash) before giving up on this
        particular suggestion — the transcript context this session has
        built up lives in recent_transcript_segments, not in ClaudeCli, so
        a restart doesn't lose anything and the next trigger can try again.

        Also records a TRIGGER line to the live-agent-listening export log
        (if one is open) right here, once there's real context to answer
        from -- not any earlier (e.g. past the lock-check above) -- so a
        hotkey press before any segment has ever arrived, which this method
        already silently no-ops on via the prompt_text check below, doesn't
        also tell a tailing agent "answer now" with nothing yet in the log
        to answer from.
        """
        if self._claude_cli_lock.locked():
            return
        async with self._claude_cli_lock:
            prompt_text = self._format_context()
            if not prompt_text:
                return
            self.live_agent_log.record_trigger()
            loop = asyncio.get_running_loop()

            def send_partial_suggestion(partial_text):
                """Runs on ClaudeCli's worker thread; schedules the partial-text message back onto the event loop."""
                asyncio.run_coroutine_threadsafe(
                    send_message(self.writer, {"type": "suggestion", "text": partial_text, "question": prompt_text}),
                    loop,
                )

            suggestion_text = await self._ask_with_restart_on_crash(prompt_text, send_partial_suggestion)
            if not suggestion_text or not suggestion_text.strip():
                return
            timestamp = time.strftime("%H:%M:%S")
            print(f"[{timestamp}] Suggestion: {suggestion_text}")
            await send_message(
                self.writer,
                {"type": "suggestion", "text": suggestion_text, "question": prompt_text},
            )
            await self._count_ask_call_and_recycle_if_due()

    async def _ask_with_restart_on_crash(self, prompt_text, on_partial_text=None):
        """
        Sends prompt_text to this session's claude CLI process. If the
        process has crashed (ClaudeCli.ask() raises RuntimeError), restarts
        it once and retries the same prompt on the fresh process, so one
        crashed process doesn't end the whole meeting session. If even
        restarting the process itself fails (e.g. the claude command can't
        be spawned right now), that's logged and treated the same as a
        still-failing process — this suggestion is skipped rather than
        letting the error escape as an unhandled background-task exception.

        Returns:
            str | None: the answer text, or None if the process couldn't be
            used even after one restart attempt (this suggestion is
            skipped, but the session keeps running and the next trigger
            tries again).
        """
        for attempt in (1, 2):
            try:
                return await asyncio.to_thread(self._claude_cli.ask, prompt_text, on_partial_text)
            except RuntimeError as error:
                if attempt == 2:
                    print(f"claude CLI still failing after restart, skipping this suggestion: {error}")
                    return None
                print(f"claude CLI process crashed ({error}); restarting and retrying once")
                try:
                    await self._restart_claude_cli()
                except Exception as restart_error:
                    print(f"Couldn't restart claude CLI process, skipping this suggestion: {restart_error}")
                    return None
        return None

    async def _restart_claude_cli(self):
        """
        Replaces this session's claude CLI process with a fresh one, framed
        with the same system prompt, and resets the call-count used to
        decide when the next routine recycle is due (a freshly-started
        process, whether from crash recovery or a routine recycle, hasn't
        made any calls yet either way). Used both for the normal call-count
        recycling and for recovering from a crashed process.
        """
        try:
            await asyncio.to_thread(self._claude_cli.stop)
        except Exception as error:
            # The old process is already dead or misbehaving — nothing to
            # do about that, and it must not stop the fresh one from
            # starting.
            print(f"Error stopping crashed claude CLI process (ignoring): {error}")
        self._claude_cli = ClaudeCli(self._system_prompt)
        self._ask_call_count = 0

    async def _count_ask_call_and_recycle_if_due(self):
        """
        Counts one more completed ask() call, and recycles the claude CLI
        process (stops it, then starts a fresh one) once
        ASK_CALLS_BEFORE_RECYCLING_CLAUDE_CLI is reached, so a long
        session's own internal conversation history doesn't grow the
        subprocess forever. Runs stop() on a background thread since it
        blocks waiting for the old process to exit, and this must not
        stall the server while a session recycles.
        """
        self._ask_call_count += 1
        if self._ask_call_count < ASK_CALLS_BEFORE_RECYCLING_CLAUDE_CLI:
            return
        print("Recycling claude CLI process for this session (reached call limit)")
        await self._restart_claude_cli()

    async def close(self):
        """
        Ends the session: cancels any pending suggestion timer, waits for
        any suggestion currently being generated to finish (so the claude
        CLI process's stdin is never closed out from under an in-flight
        ask()), then stops the process on a background thread. Any segment
        still open in self.segmenters (either source) is flushed by the
        caller (see handle_client's session_stopped branch) before this is
        called, not here -- flushing kicks off its own background
        transcribe-and-report task, which doesn't need to (and shouldn't)
        block session teardown; that trailing segment will not reach the
        live-agent-listening export log below either, for the same reason
        it doesn't reach the suggestion trigger (see that branch's own
        docstring) -- there's no live session left to record it into by the
        time it finishes transcribing, and closing the log's file here
        first means it's very likely already closed by then regardless.
        """
        self.live_agent_log.stop()
        self.trigger.stop()
        async with self._claude_cli_lock:
            await asyncio.to_thread(self._claude_cli.stop)


async def generate_and_send_summary(writer, transcript_text, summary_lock):
    """
    Generates a post-meeting summary (key points/decisions/action items,
    see SUMMARY_SYSTEM_PROMPT) from `transcript_text` and sends it back as a
    "summary" message -- or a "summary_failed" message, with a reason, if
    it couldn't be generated. `transcript_text` is windows_app's own full,
    untrimmed in-session transcript (TranscriptDisplay), sent on the
    generate_summary message -- deliberately not this connection's
    MeetingSession.recent_transcript_segments, which is a bounded rolling
    window sized for live suggestion context and would silently summarize
    only the last few minutes of a real meeting (see docs/DEV_PLAN.md, Day
    23).

    Spins up a fresh, one-shot ClaudeCli scoped to SUMMARY_SYSTEM_PROMPT
    rather than reusing a session's own ClaudeCli: by the time a session
    has actually ended (see MeetingSession.close()), its own claude CLI
    process -- framed by that session's mode, not a summary -- is already
    stopped, so there's no still-running, session-scoped process left to
    reuse here anyway. Stopped again once the summary is back, since this
    process is only ever used for this one call.

    `summary_lock` (one per connection, created in handle_client) guards
    against two rapid generate_summary requests on the same connection
    running two summary CLI processes at once -- skipped (silently, not
    queued) if one is already in flight, since a second click while the
    first is still generating would just be a duplicate request for the
    same transcript.
    """
    if summary_lock.locked():
        return
    async with summary_lock:
        if not transcript_text.strip():
            await send_message(writer, {"type": "summary_failed", "reason": "Transcript is empty."})
            return
        claude_cli = ClaudeCli(SUMMARY_SYSTEM_PROMPT)
        try:
            summary_text = await asyncio.to_thread(claude_cli.ask, transcript_text)
        except RuntimeError as error:
            print(f"Couldn't generate summary: {error}")
            await send_message(writer, {"type": "summary_failed", "reason": str(error)})
            summary_text = None
        try:
            await asyncio.to_thread(claude_cli.stop)
        except Exception as error:
            # The process is already dead or misbehaving -- nothing to do
            # about that, and it must not stop the summary (if any) from
            # still being sent below.
            print(f"Error stopping summary claude CLI process (ignoring): {error}")
        if summary_text is None:
            return
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] Summary generated ({len(summary_text)} chars)")
        await send_message(writer, {"type": "summary", "text": summary_text})


def main():
    """
    Entry point: loads the default Whisper model, then runs the IPC server until interrupted.
    """
    model_manager = WhisperModelManager()
    asyncio.run(run_server(model_manager))


if __name__ == "__main__":
    main()
