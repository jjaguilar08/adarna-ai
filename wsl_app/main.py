import asyncio
# audioop is used below to convert captured audio into the one consistent
# format speech detection and transcription both need. It's deprecated and
# slated for removal in Python 3.13+;
# not an issue on this project's pinned 3.10.12, but flag it here so it's
# not a silent trap if the interpreter version ever changes.
import audioop
import base64
import json
import subprocess
import threading
import time
import traceback
from pathlib import Path

import numpy
import webrtcvad
from faster_whisper import WhisperModel

CONFIG_PATH = Path(__file__).resolve().parent.parent / "ipc_config.json"
STATS_WINDOW_SECONDS = 1.0

# asyncio's StreamReader defaults to a 64KiB limit on how long one
# newline-delimited message line can be before readline() gives up and
# raises -- fine for the small control messages this protocol used to
# carry, but the Phase 1.5 context_notes field explicitly invites pasting
# a whole CV/JD or PRD excerpt into one settings_changed line, which can
# realistically exceed that. Raised generously (well above any single
# audio_chunk message too) so a large paste doesn't crash the connection.
MAX_MESSAGE_LINE_BYTES = 10 * 1024 * 1024

# Speech detection and transcription both want 16kHz mono audio.
TARGET_SAMPLE_RATE = 16000

# The speech detector (webrtcvad) can only judge audio in fixed-size
# slices, one at a time — never a variable amount. 30 milliseconds is a
# size it supports; everything below is derived from that.
FRAME_DURATION_MS = 30
FRAME_DURATION_SECONDS = FRAME_DURATION_MS / 1000
BYTES_PER_FRAME = int(TARGET_SAMPLE_RATE * FRAME_DURATION_SECONDS) * 2  # 16-bit samples

# webrtcvad's own "how strict should speech-detection be" setting, on its
# own 0-3 scale: 0 accepts more borderline sound as speech, 3 rejects more
# of it. 2 is a reasonable middle ground to start from.
SPEECH_DETECTION_STRICTNESS = 2

# How long a pause has to last before we consider a sentence "done" and
# close the segment.
SILENCE_SECONDS_TO_CLOSE_SEGMENT = 0.4

# Safety net: force-close a segment after this long even without a pause,
# so one uninterrupted run-on sentence can't grow the buffer forever.
MAX_SEGMENT_SECONDS = 20.0

# Segments shorter than this are almost always a false positive from the
# speech detector (a brief noise blip, not real speech), so skip the
# (expensive) transcription call for them entirely rather than spending a
# full model call for nothing.
MIN_SEGMENT_SECONDS_TO_TRANSCRIBE = 0.3

# Whisper model size and assistant mode to use for a session that starts
# before windows_app has ever sent a settings_changed message.
DEFAULT_WHISPER_MODEL_SIZE = "small.en"
DEFAULT_MODE = "meeting"

# How long a pause has to last, with no further new transcript segment
# arriving during it, before a suggestion is generated automatically —
# used as the default until a session-specific value arrives via
# settings_changed.
PAUSE_SECONDS_BEFORE_SUGGESTION = 1.2

# How many recent transcript segments are kept and sent as context with
# each suggestion request, and how many ask() calls a session's claude CLI
# process handles before being recycled (stopped and restarted fresh) to
# bound its own automatically-growing internal conversation history.
SEGMENTS_TO_KEEP_FOR_CONTEXT = 10
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
    """

    def __init__(self):
        """Starts with nothing recorded yet and no segment in progress."""
        self._speech_detector = webrtcvad.Vad(SPEECH_DETECTION_STRICTNESS)
        # Audio that has arrived but hasn't been sliced into a full frame yet.
        self._unsliced_audio = bytearray()
        # Audio recorded for the segment currently in progress, if any.
        self._current_segment_audio = bytearray()
        self._segment_is_open = False
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
            list[bytes]: zero or more segments that finished (closed)
            while processing this batch of audio, each ready to hand to
            Whisper. Usually empty — most calls just add to an
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
            bytes | None: the finished segment, if this slice was the one
            that closed it. None if the segment is still open, or if
            there's no segment in progress and this slice was silence.
        """
        this_frame_is_speech = self._speech_detector.is_speech(frame, TARGET_SAMPLE_RATE)

        if not this_frame_is_speech and not self._segment_is_open:
            # Plain silence and nothing recording yet — nothing to do.
            return None

        self._current_segment_audio.extend(frame)
        self._seconds_recorded_in_current_segment += FRAME_DURATION_SECONDS

        if this_frame_is_speech:
            self._segment_is_open = True
            self._seconds_of_silence_in_a_row = 0.0
        else:
            self._seconds_of_silence_in_a_row += FRAME_DURATION_SECONDS
            if self._seconds_of_silence_in_a_row >= SILENCE_SECONDS_TO_CLOSE_SEGMENT:
                return self._close_current_segment()

        if self._seconds_recorded_in_current_segment >= MAX_SEGMENT_SECONDS:
            return self._close_current_segment()

        return None

    def _close_current_segment(self):
        """
        Packages up everything recorded for the current segment so it can
        be handed off for transcription, then resets so the next speech
        heard starts a brand new segment.

        Returns:
            bytes: the finished segment's audio.
        """
        finished_segment_audio = bytes(self._current_segment_audio)
        self._current_segment_audio = bytearray()
        self._segment_is_open = False
        self._seconds_of_silence_in_a_row = 0.0
        self._seconds_recorded_in_current_segment = 0.0
        return finished_segment_audio

    def close_open_segment(self):
        """
        Force-closes whatever segment is currently in progress, if any.
        Used when a session stops, so audio that hasn't hit a silence gap
        yet (someone stops the session right after finishing a sentence)
        isn't silently dropped.

        Returns:
            bytes | None: the finished segment's audio, or None if nothing
            was open.
        """
        if not self._segment_is_open:
            return None
        return self._close_current_segment()


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
    Owns the one Whisper model currently loaded and shared by whichever
    session is active. A session's chosen model size only takes effect at
    session_started (see handle_client) — ensure_model_size() reloads the
    model then, but only if the requested size actually differs from what's
    already loaded, so starting several sessions in a row with the same
    setting doesn't pay the multi-second reload cost each time.
    """

    def __init__(self):
        """Loads the default-size model right away, so it's ready before the first session."""
        self.model_size = DEFAULT_WHISPER_MODEL_SIZE
        self.model = load_whisper_model(self.model_size)

    async def ensure_model_size(self, requested_model_size):
        """
        Reloads the model if `requested_model_size` differs from what's
        currently loaded. Runs the (blocking) load in a background thread
        so it doesn't stall the server while it happens.
        """
        if requested_model_size == self.model_size:
            return
        print(f"Reloading Whisper model: {self.model_size} -> {requested_model_size}")
        self.model = await asyncio.to_thread(load_whisper_model, requested_model_size)
        self.model_size = requested_model_size


def prepare_audio_for_whisper(audio_bytes):
    """
    Converts audio bytes into the number format faster-whisper expects to
    read them in directly.

    Returns:
        numpy.ndarray: mono samples in the range [-1.0, 1.0].
    """
    int16_samples = numpy.frombuffer(audio_bytes, dtype=numpy.int16)
    return int16_samples.astype(numpy.float32) / 32768.0


def transcribe_audio(model, audio_bytes):
    """
    Runs Whisper on one closed speech segment. This is blocking, CPU-bound
    work — always call it through asyncio.to_thread(), never directly on
    the event loop.

    Returns:
        str: the transcribed text, stripped of leading/trailing whitespace.
    """
    audio = prepare_audio_for_whisper(audio_bytes)
    segments, _ = model.transcribe(audio, language="en")
    return " ".join(segment.text.strip() for segment in segments).strip()


def segment_duration_seconds(audio_bytes):
    """
    Returns:
        float: how many seconds of audio `audio_bytes` (16-bit mono PCM at
        TARGET_SAMPLE_RATE) represents.
    """
    return len(audio_bytes) / 2 / TARGET_SAMPLE_RATE


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


async def transcribe_segment_and_report(model, writer, audio_bytes, session=None):
    """
    Transcribes one closed speech segment in a background thread (keeping
    the event loop free to keep reading incoming messages), logs the
    result with a timestamp, and — if it contains real text — sends it to
    windows_app as a transcript message. If this segment belongs to a
    still-active session, also feeds the text into that session's rolling
    context and suggestion trigger (see MeetingSession). `session` is left
    as None for a segment transcribed after its session has already ended
    (the trailing bit of audio flushed on session_stopped), so it's
    reported but doesn't try to trigger a suggestion from a session that no
    longer exists.
    """
    duration_seconds = segment_duration_seconds(audio_bytes)
    started_at = time.monotonic()
    text = await asyncio.to_thread(transcribe_audio, model, audio_bytes)
    elapsed_seconds = time.monotonic() - started_at
    timestamp = time.strftime("%H:%M:%S")
    spoken_text = text if text else "(no speech detected)"
    print(
        f"[{timestamp}] Transcript ({duration_seconds:.1f}s segment, "
        f"{elapsed_seconds:.1f}s to transcribe): {spoken_text}"
    )
    if text:
        await send_message(
            writer, {"type": "transcript", "text": text, "duration_seconds": duration_seconds}
        )
        if session is not None:
            session.add_transcript_segment(text)


def handle_finished_segment(model, writer, audio_bytes, session=None):
    """
    Decides what to do with one just-closed speech segment: skip
    transcription entirely if it's too short to plausibly be real speech
    (a false positive from the speech detector that would otherwise cost a
    full, slow model call for nothing), or kick off background
    transcription-and-reporting otherwise. `session` is passed through to
    transcribe_segment_and_report() — see its docstring.
    """
    duration_seconds = segment_duration_seconds(audio_bytes)
    if duration_seconds < MIN_SEGMENT_SECONDS_TO_TRANSCRIBE:
        print(f"Skipping {duration_seconds:.2f}s segment: below minimum duration, likely not real speech")
        return
    asyncio.create_task(transcribe_segment_and_report(model, writer, audio_bytes, session))


class AudioLevelTracker:
    """
    Counts how many audio chunks arrive and how loud the loudest one is,
    over roughly one second at a time, so we can log a single summary line
    per second instead of spamming a line per chunk.
    """

    def __init__(self):
        """Starts a fresh one-second tracking window."""
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
                f"Audio: {self._chunk_count} chunks in {elapsed:.1f}s, "
                f"peak amplitude {self._peak_amplitude:.4f}"
            )
            self._window_start = time.monotonic()
            self._chunk_count = 0
            self._peak_amplitude = 0.0


def handle_audio_chunk(tracker, session, message):
    """
    Decodes one audio_chunk message, adds its volume to the running
    one-second stats, and feeds it (converted to the common 16kHz mono
    format) into the session's speech segmenter. Any speech segment just
    closed is handed off to handle_finished_segment() along with the
    session, so a finished transcript can feed the suggestion trigger.
    Transcribes with whichever Whisper model this session was started with
    (session.model) — never the manager's current model directly, since
    that could have moved on to a different size for a later session.
    """
    samples = decode_audio_chunk(message)
    if samples is None:
        return
    tracker.record(samples)

    audio_bytes = convert_audio_to_common_format(samples, message["sample_rate"], message["channels"])
    for segment in session.segmenter.add_audio(audio_bytes):
        handle_finished_segment(session.model, session.writer, segment, session)


def default_settings():
    """
    Returns:
        dict: the settings to start a session with if it begins before any
        settings_changed message has ever arrived on this connection.
    """
    return {
        "mode": DEFAULT_MODE,
        "whisper_model_size": DEFAULT_WHISPER_MODEL_SIZE,
        "suggestion_pause_seconds": PAUSE_SECONDS_BEFORE_SUGGESTION,
        "context_notes": "",
        "auto_suggest_enabled": True,
    }


def settings_from_message(message):
    """
    Reads mode/whisper_model_size/suggestion_pause_seconds out of a
    settings_changed message, falling back to default_settings() for any
    field that's missing OR explicitly sent as null (a plain message.get(
    key, default) wouldn't catch the null case, since the key would still
    be present).

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
    segmentation/transcription.

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
    a fresh
    MeetingSession (fresh speech segmenter, fresh claude CLI process, empty
    transcript context) using those settings, so no state bleeds across
    sessions; stopping one force-closes whatever segment is still open so a
    sentence finished right before stopping isn't silently dropped, then
    tears the session down. If the connection itself drops mid-session (no
    explicit session_stopped), the `finally` block below still tears it
    down, so the claude CLI process it started is never left running with
    nothing using it.

    If starting a session fails (e.g. the requested Whisper model size
    can't load), no MeetingSession is created and a session_start_failed
    message is sent back instead of just dropping the connection, so
    windows_app can revert its UI to a clean pre-session state rather than
    getting stuck showing a session as active.
    """
    peer = writer.get_extra_info("peername")
    print(f"Client connected: {peer}")
    audio_tracker = AudioLevelTracker()
    session = None
    pending_settings = None
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
                    await model_manager.ensure_model_size(settings["whisper_model_size"])
                    new_session = MeetingSession(
                        writer,
                        model_manager.model,
                        settings["mode"],
                        settings["suggestion_pause_seconds"],
                        settings["context_notes"],
                        settings["auto_suggest_enabled"],
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
                        f"whisper_model={settings['whisper_model_size']}, "
                        f"suggestion_pause={settings['suggestion_pause_seconds']}s)"
                    )
            elif message_type == "session_stopped":
                if session is not None:
                    leftover_audio = session.segmenter.close_open_segment()
                    if leftover_audio is not None:
                        handle_finished_segment(session.model, session.writer, leftover_audio)
                    await session.close()
                    session = None
                print("Session stopped")
            elif message_type == "audio_chunk":
                if session is not None:
                    handle_audio_chunk(audio_tracker, session, message)
            elif message_type == "hotkey_triggered":
                if session is not None:
                    print("Hotkey pressed: generating a suggestion now")
                    session.trigger.notify_hotkey_pressed()
            elif message_type == "settings_changed":
                pending_settings = settings_from_message(message)
            elif message_type == "auto_suggest_changed":
                if session is not None:
                    session.set_auto_suggest_enabled(bool(message.get("enabled", True)))
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


# The framing given to every prompt sent through a session's ClaudeCli,
# chosen per session by mode (see MeetingSession). Both end with the same
# instruction. Originally (Day 6) this asked for a single ready-to-read
# spoken line, after testing showed the model otherwise answering with
# multiple options and meta-commentary ("Here's a natural way to continue:
# ... Or shorter/more neutral: ..."). Changed (Phase 1.5, Day 12) after real
# usage showed a full scripted line wasn't what was actually wanted live —
# the user wants terms/concepts/key points to build their own answer from,
# not a script to read verbatim.
RESPOND_WITH_TERMS_INSTRUCTION = (
    "Respond with only a short, comma-separated list of the key terms, "
    "concepts, or points the user could build their own answer from — not "
    "a full sentence or a script to read out loud, and not a structured "
    "breakdown or bulleted essay either. Something glanceable in a couple "
    "seconds during a live conversation, e.g. \"Encapsulation, Inheritance, "
    "Polymorphism, Abstraction\" rather than a paragraph. At most 5-8 terms. "
    "Add a clause of context after a term only if it's genuinely necessary "
    "to disambiguate — a few words, not a sentence. No options, no "
    "meta-commentary, no explanation, no markdown formatting."
)

MEETING_SYSTEM_PROMPT = (
    "You are assisting the user live during a work meeting. Given a snippet "
    "of recent conversation, suggest the key terms, concepts, or points "
    "relevant to what's being discussed. " + RESPOND_WITH_TERMS_INSTRUCTION
)

INTERVIEW_SYSTEM_PROMPT = (
    "You are assisting the user live during a job interview, in which the "
    "user is the candidate being interviewed. Given a snippet of the "
    "interviewer's most recent question or remark, suggest the key terms, "
    "concepts, or points the user could use to build their own answer. "
    + RESPOND_WITH_TERMS_INSTRUCTION
)

SYSTEM_PROMPT_BY_MODE = {
    "meeting": MEETING_SYSTEM_PROMPT,
    "interview": INTERVIEW_SYSTEM_PROMPT,
}


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
                "--verbose",
                "--system-prompt", system_prompt,
                "--tools", "",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
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

    def ask(self, prompt_text):
        """
        Sends one short text prompt to the running claude process and waits
        for its complete answer. Safe to call more than once on the same
        ClaudeCli — each call continues the same ongoing conversation.

        Raises:
            RuntimeError: if the claude process has crashed — either its
            stdin is already closed (can't send the prompt at all) or it
            exits before sending back a complete answer. Callers that want
            to survive a crash (see MeetingSession) should catch this and
            create a fresh ClaudeCli.

        Returns:
            str: the answer text.
        """
        input_line = {"type": "user", "message": {"role": "user", "content": prompt_text}}
        try:
            self._process.stdin.write(json.dumps(input_line) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError) as error:
            raise RuntimeError("claude CLI process is not running") from error
        return self._read_next_answer()

    def _read_next_answer(self):
        """
        Reads output lines from the claude process one at a time, skipping
        everything that isn't the final line of a reply, until that final
        line — marked with "type": "result" — arrives.

        Returns:
            str: the answer text carried on the result line.
        """
        while True:
            line = self._process.stdout.readline()
            if not line:
                raise RuntimeError("claude CLI process ended unexpectedly")
            output = json.loads(line)
            if output.get("type") == "result":
                return output.get("result", "")

    def stop(self):
        """Shuts the claude process down cleanly and waits for it to exit."""
        self._process.stdin.close()
        self._process.wait()


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

    def __init__(self, generate_suggestion, pause_seconds, auto_suggest_enabled=True):
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
    each time and cleanly torn down together: speech segmentation, the
    running claude CLI process behind it, the pause/hotkey suggestion
    trigger, and a short rolling history of recent transcript segments to
    give suggestions context. If the claude CLI process crashes mid-session,
    one restart is attempted automatically (see _ask_with_restart_on_crash)
    — recent_transcript_segments lives here, not in ClaudeCli, so a restart
    doesn't lose the transcript context built up so far.
    """

    def __init__(self, writer, model, mode, pause_seconds, context_notes="", auto_suggest_enabled=True):
        """
        Starts a fresh session using the settings captured for it at
        session_started (see handle_client): new segmenter, `model` as the
        Whisper model this session transcribes with for its entire
        lifetime, a new claude CLI process framed for `mode` and
        `context_notes`, empty transcript history, and a suggestion trigger
        using `pause_seconds` and starting with auto-suggest set to
        `auto_suggest_enabled`.
        """
        self.writer = writer
        self.model = model
        self.segmenter = VoiceSegmenter()
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

    def set_auto_suggest_enabled(self, enabled):
        """Turns this session's pause-triggered suggestions on or off, live. See SuggestionTrigger.set_auto_suggest_enabled."""
        self.trigger.set_auto_suggest_enabled(enabled)

    def add_transcript_segment(self, text):
        """
        Adds one newly-transcribed segment to the rolling context window,
        dropping the oldest once there are more than
        SEGMENTS_TO_KEEP_FOR_CONTEXT, and lets the suggestion trigger know
        new content has arrived.
        """
        self.recent_transcript_segments.append(text)
        if len(self.recent_transcript_segments) > SEGMENTS_TO_KEEP_FOR_CONTEXT:
            self.recent_transcript_segments.pop(0)
        self.trigger.notify_new_segment()

    async def _generate_and_send_suggestion(self):
        """
        Asks claude for one suggestion based on the recent transcript
        context and sends it to windows_app as a "suggestion" message. Runs
        ask() on a background thread via asyncio.to_thread, since it blocks
        on the subprocess and must not stall audio_chunk handling while a
        suggestion is being generated.

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
        """
        if self._claude_cli_lock.locked():
            return
        async with self._claude_cli_lock:
            prompt_text = " ".join(self.recent_transcript_segments)
            if not prompt_text:
                return
            suggestion_text = await self._ask_with_restart_on_crash(prompt_text)
            if suggestion_text is None:
                return
            timestamp = time.strftime("%H:%M:%S")
            print(f"[{timestamp}] Suggestion: {suggestion_text}")
            await send_message(self.writer, {"type": "suggestion", "text": suggestion_text})
            await self._count_ask_call_and_recycle_if_due()

    async def _ask_with_restart_on_crash(self, prompt_text):
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
                return await asyncio.to_thread(self._claude_cli.ask, prompt_text)
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
        ask()), then stops the process on a background thread.
        """
        self.trigger.stop()
        async with self._claude_cli_lock:
            await asyncio.to_thread(self._claude_cli.stop)


def main():
    """
    Entry point: loads the default Whisper model, then runs the IPC server until interrupted.
    """
    model_manager = WhisperModelManager()
    asyncio.run(run_server(model_manager))


if __name__ == "__main__":
    main()
