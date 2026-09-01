import asyncio
# audioop is used below to convert captured audio into the one consistent
# format speech detection and transcription both need. It's deprecated and
# slated for removal in Python 3.13+;
# not an issue on this project's pinned 3.10.12, but flag it here so it's
# not a silent trap if the interpreter version ever changes.
import audioop
import base64
import json
import time
from pathlib import Path

import numpy
import webrtcvad
from faster_whisper import WhisperModel

CONFIG_PATH = Path(__file__).resolve().parent.parent / "ipc_config.json"
STATS_WINDOW_SECONDS = 1.0

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

WHISPER_MODEL_NAME = "small.en"


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
# "ping" / "pong" and "audio_chunk" are handled today. Coming in later days:
# "transcript", "suggestion", "hotkey", and "mode_change" — routed here the
# same way once they show up.
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


def load_whisper_model():
    """
    Loads the faster-whisper model once at startup. Loading takes a few
    seconds (and may download model weights on first run), so this must
    never be called per-segment.

    Returns:
        faster_whisper.WhisperModel: the loaded model, ready to transcribe.
    """
    print(f"Loading Whisper model ({WHISPER_MODEL_NAME})... first run may download weights from Hugging Face.")
    model = WhisperModel(WHISPER_MODEL_NAME, device="cpu", compute_type="int8")
    print("Whisper model loaded.")
    return model


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


async def transcribe_and_log(model, audio_bytes):
    """
    Transcribes one closed speech segment in a background thread (keeping
    the event loop free to keep reading incoming audio_chunk messages) and
    logs the result with a timestamp.
    """
    duration_seconds = len(audio_bytes) / 2 / TARGET_SAMPLE_RATE
    started_at = time.monotonic()
    text = await asyncio.to_thread(transcribe_audio, model, audio_bytes)
    elapsed_seconds = time.monotonic() - started_at
    timestamp = time.strftime("%H:%M:%S")
    spoken_text = text if text else "(no speech detected)"
    print(
        f"[{timestamp}] Transcript ({duration_seconds:.1f}s segment, "
        f"{elapsed_seconds:.1f}s to transcribe): {spoken_text}"
    )


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


def handle_audio_chunk(model, tracker, segmenter, message):
    """
    Decodes one audio_chunk message, adds its volume to the running
    one-second stats, and feeds it (converted to the common 16kHz mono
    format) into the VAD segmenter. Any speech segment the segmenter just
    closed is handed off for background transcription.
    """
    samples = decode_audio_chunk(message)
    if samples is None:
        return
    tracker.record(samples)

    audio_bytes = convert_audio_to_common_format(samples, message["sample_rate"], message["channels"])
    for segment in segmenter.add_audio(audio_bytes):
        asyncio.create_task(transcribe_and_log(model, segment))


async def handle_client(model, reader, writer):
    """
    Services one connected windows_app client for the lifetime of the
    connection: reads newline-delimited JSON messages, replies to pings,
    tracks audio level stats and runs speech segmentation/transcription
    for audio_chunk messages, and logs connect/disconnect.
    """
    peer = writer.get_extra_info("peername")
    print(f"Client connected: {peer}")
    audio_tracker = AudioLevelTracker()
    segmenter = VoiceSegmenter()
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            message = json.loads(line)
            if message.get("type") == "audio_chunk":
                handle_audio_chunk(model, audio_tracker, segmenter, message)
                continue
            reply = await build_reply(message)
            if reply is not None:
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
    finally:
        print(f"Client disconnected: {peer}")
        writer.close()
        await writer.wait_closed()


async def run_server(model):
    """
    Starts the asyncio TCP server on localhost and serves clients until stopped.
    """
    port = load_port()

    async def handle_client_connection(reader, writer):
        """Adapts handle_client to asyncio.start_server's (reader, writer) callback shape."""
        await handle_client(model, reader, writer)

    server = await asyncio.start_server(handle_client_connection, "127.0.0.1", port)
    print(f"wsl_app listening on 127.0.0.1:{port}")
    async with server:
        await server.serve_forever()


def main():
    """
    Entry point: loads the Whisper model, then runs the IPC server until interrupted.
    """
    model = load_whisper_model()
    asyncio.run(run_server(model))


if __name__ == "__main__":
    main()
