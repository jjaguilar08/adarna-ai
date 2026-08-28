import asyncio
import base64
import json
import time
from pathlib import Path

import numpy

CONFIG_PATH = Path(__file__).resolve().parent.parent / "ipc_config.json"
STATS_WINDOW_SECONDS = 1.0


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


def handle_audio_chunk(tracker, message):
    """Decodes one audio_chunk message and adds its volume to the running one-second count."""
    samples = decode_audio_chunk(message)
    if samples is not None:
        tracker.record(samples)


async def handle_client(reader, writer):
    """
    Services one connected windows_app client for the lifetime of the
    connection: reads newline-delimited JSON messages, replies to pings,
    tracks audio level stats for audio_chunk messages, and logs
    connect/disconnect.
    """
    peer = writer.get_extra_info("peername")
    print(f"Client connected: {peer}")
    audio_tracker = AudioLevelTracker()
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            message = json.loads(line)
            if message.get("type") == "audio_chunk":
                handle_audio_chunk(audio_tracker, message)
                continue
            reply = await build_reply(message)
            if reply is not None:
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
    finally:
        print(f"Client disconnected: {peer}")
        writer.close()
        await writer.wait_closed()


async def run_server():
    """
    Starts the asyncio TCP server on localhost and serves clients until stopped.
    """
    port = load_port()
    server = await asyncio.start_server(handle_client, "127.0.0.1", port)
    print(f"wsl_app listening on 127.0.0.1:{port}")
    async with server:
        await server.serve_forever()


def main():
    """
    Entry point: runs the IPC server until interrupted.
    """
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
