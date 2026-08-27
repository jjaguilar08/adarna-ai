import asyncio
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "ipc_config.json"


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
# Today only "ping" / "pong" exist. Coming in later days: "audio_chunk"
# (base64-encoded PCM), "transcript", "suggestion", "hotkey", and
# "mode_change" — routed here the same way once they show up.
async def build_reply(message):
    """
    Decides how to respond to one incoming message, based on its "type".

    Returns:
        dict | None: the reply message to send back, or None if no reply is needed.
    """
    if message.get("type") == "ping":
        return {"type": "pong"}
    return None


async def handle_client(reader, writer):
    """
    Services one connected windows_app client for the lifetime of the
    connection: reads newline-delimited JSON messages, replies to each,
    and logs connect/disconnect.
    """
    peer = writer.get_extra_info("peername")
    print(f"Client connected: {peer}")
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            message = json.loads(line)
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
