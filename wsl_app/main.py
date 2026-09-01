import asyncio
# audioop is used below to downmix/resample audio for speech detection and
# transcription. It's deprecated and slated for removal in Python 3.13+;
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

# Speech detection (VAD) and transcription (Whisper) both want 16kHz mono.
TARGET_SAMPLE_RATE = 16000
VAD_FRAME_MS = 30
VAD_FRAME_BYTES = int(TARGET_SAMPLE_RATE * VAD_FRAME_MS / 1000) * 2  # 16-bit samples
VAD_AGGRESSIVENESS = 2
SILENCE_CLOSE_SECONDS = 0.4
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
    Decodes one audio_chunk message's base64 PCM payload into a numpy array
    of samples, using the format fields included on the message.

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


def resample_to_16k_mono_pcm(samples, sample_rate, channels):
    """
    Converts a chunk of interleaved float32 audio samples to 16-bit PCM
    bytes, downmixed to mono and resampled to 16kHz — the format both
    webrtcvad and faster-whisper expect. sample_rate/channels are read from
    the message every time rather than assumed, since the capture device
    (and therefore the format) can change on the windows_app side.

    Returns:
        bytes: 16-bit PCM samples, mono, at TARGET_SAMPLE_RATE.
    """
    int16_samples = numpy.clip(samples, -1.0, 1.0)
    int16_samples = (int16_samples * 32767.0).astype(numpy.int16)

    if channels == 1:
        pcm_bytes = int16_samples.tobytes()
    elif channels == 2:
        pcm_bytes = audioop.tomono(int16_samples.tobytes(), 2, 0.5, 0.5)
    else:
        # audioop.tomono only understands stereo; average manually for
        # anything else (uncommon, but the device dropdown could pick one).
        frames = int16_samples.reshape(-1, channels)
        pcm_bytes = frames.mean(axis=1).astype(numpy.int16).tobytes()

    if sample_rate != TARGET_SAMPLE_RATE:
        pcm_bytes, _ = audioop.ratecv(pcm_bytes, 2, 1, sample_rate, TARGET_SAMPLE_RATE, None)

    return pcm_bytes


class VoiceSegmenter:
    """
    Buffers 16kHz mono PCM audio and slices it into speech segments using
    webrtcvad: a segment starts on the first speech frame and closes after
    roughly 400ms of continuous silence, or after a ~20s safety cutoff so
    one long uninterrupted sentence can't grow the buffer forever.
    """

    def __init__(self):
        """Starts with an empty buffer and no segment in progress."""
        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self._pending_bytes = bytearray()
        self._segment_frames = bytearray()
        self._in_speech = False
        self._silence_seconds = 0.0
        self._segment_seconds = 0.0

    def add_audio(self, pcm_bytes):
        """
        Feeds newly-arrived 16kHz mono PCM bytes into the segmenter, one
        30ms VAD frame at a time.

        Returns:
            list[bytes]: zero or more completed speech segments, each ready
            to hand to Whisper.
        """
        self._pending_bytes.extend(pcm_bytes)
        completed_segments = []
        while len(self._pending_bytes) >= VAD_FRAME_BYTES:
            frame = bytes(self._pending_bytes[:VAD_FRAME_BYTES])
            del self._pending_bytes[:VAD_FRAME_BYTES]
            segment = self._process_frame(frame)
            if segment is not None:
                completed_segments.append(segment)
        return completed_segments

    def _process_frame(self, frame):
        """
        Runs VAD on one 30ms frame and updates the in-progress segment,
        closing it if a silence gap or the max-length cutoff is reached.

        Returns:
            bytes | None: the completed segment, or None if still open.
        """
        is_speech = self._vad.is_speech(frame, TARGET_SAMPLE_RATE)
        if not is_speech and not self._in_speech:
            return None

        self._segment_frames.extend(frame)
        self._segment_seconds += VAD_FRAME_MS / 1000

        if is_speech:
            self._in_speech = True
            self._silence_seconds = 0.0
        else:
            self._silence_seconds += VAD_FRAME_MS / 1000
            if self._silence_seconds >= SILENCE_CLOSE_SECONDS:
                return self._close_segment()

        if self._segment_seconds >= MAX_SEGMENT_SECONDS:
            return self._close_segment()

        return None

    def _close_segment(self):
        """Finalizes and returns the current segment, resetting state for the next one."""
        segment = bytes(self._segment_frames)
        self._segment_frames = bytearray()
        self._in_speech = False
        self._silence_seconds = 0.0
        self._segment_seconds = 0.0
        return segment


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


def pcm_to_whisper_input(pcm_bytes):
    """
    Converts 16-bit PCM bytes into the normalized float32 numpy array
    faster-whisper expects as input.

    Returns:
        numpy.ndarray: mono float32 samples in the range [-1.0, 1.0].
    """
    int16_samples = numpy.frombuffer(pcm_bytes, dtype=numpy.int16)
    return int16_samples.astype(numpy.float32) / 32768.0


def transcribe_pcm(model, pcm_bytes):
    """
    Runs Whisper on one closed speech segment. This is blocking, CPU-bound
    work — always call it through asyncio.to_thread(), never directly on
    the event loop.

    Returns:
        str: the transcribed text, stripped of leading/trailing whitespace.
    """
    audio = pcm_to_whisper_input(pcm_bytes)
    segments, _ = model.transcribe(audio, language="en")
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe_and_log(model, pcm_bytes):
    """
    Transcribes one closed speech segment in a background thread (keeping
    the event loop free to keep reading incoming audio_chunk messages) and
    logs the result with a timestamp.
    """
    duration_seconds = len(pcm_bytes) / 2 / TARGET_SAMPLE_RATE
    started_at = time.monotonic()
    text = await asyncio.to_thread(transcribe_pcm, model, pcm_bytes)
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
    one-second stats, and feeds it (resampled to 16kHz mono) into the VAD
    segmenter. Any speech segment the segmenter just closed is handed off
    for background transcription.
    """
    samples = decode_audio_chunk(message)
    if samples is None:
        return
    tracker.record(samples)

    pcm_bytes = resample_to_16k_mono_pcm(samples, message["sample_rate"], message["channels"])
    for segment in segmenter.add_audio(pcm_bytes):
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
