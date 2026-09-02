import base64
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pyaudiowpatch as pyaudio
from pynput import keyboard
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "ipc_config.json"
PING_INTERVAL_SECONDS = 2
RECONNECT_DELAY_SECONDS = 2
CHUNK_SECONDS = 0.1

# Placeholder combination for the "generate a suggestion now" global
# hotkey; making this configurable is a later concern. Must work even
# while windows_app doesn't have focus (the user will be focused on their
# meeting app), which is why this uses pynput's system-wide hook rather
# than a plain Qt shortcut (Qt shortcuts only fire while their own window
# is focused).
#
# Chose pynput over the `keyboard` package for this: neither library's own
# documentation states a Windows administrator requirement (`keyboard`'s
# docs only call out needing root on Linux), and pynput's GlobalHotKeys is
# the more actively-maintained, purpose-built API for exactly this. See
# project_notes.md (Day 7) for the empirical testing done before picking
# one.
HOTKEY_COMBINATION = "<ctrl>+<alt>+<space>"


def load_port():
    """
    Reads the shared IPC config and returns the port both sides connect on.

    Returns:
        int: the TCP port from ipc_config.json.
    """
    with open(CONFIG_PATH) as config_file:
        config = json.load(config_file)
    return config["port"]


class WslConnection(QObject):
    """
    Talks to wsl_app over a socket on a background thread and reports
    connection status back to the Qt main thread via a signal. Also lets
    other background threads (audio capture) send messages over the same
    socket, guarded by a lock so writes never interleave. See wsl_app/main.py
    for the full set of message types this protocol carries: ping/pong,
    audio_chunk, session_started/session_stopped, hotkey_triggered,
    transcript, and suggestion.
    """

    connection_changed = Signal(bool)
    transcript_received = Signal(str)

    def __init__(self):
        """Sets up the (initially disconnected) state shared across threads."""
        super().__init__()
        self._connection = None
        self._write_lock = threading.Lock()

    def run(self):
        """
        Loops forever: connects to wsl_app and serves the connection until
        it drops or fails, then reconnects automatically after a short
        delay.
        """
        port = load_port()
        while True:
            try:
                self._connect_and_serve(port)
            except OSError:
                pass
            self._connection = None
            self.connection_changed.emit(False)
            time.sleep(RECONNECT_DELAY_SECONDS)

    def _connect_and_serve(self, port):
        """
        Opens one connection to wsl_app, starts a background thread that
        pings it periodically (the heartbeat used to notice a dead
        connection), and reads incoming messages on the current thread
        until the connection breaks.
        """
        with socket.create_connection(("127.0.0.1", port)) as sock:
            self._connection = sock.makefile("rwb")
            self.connection_changed.emit(True)
            stop_pinging = threading.Event()
            threading.Thread(target=self._ping_loop, args=(stop_pinging,), daemon=True).start()
            try:
                self._read_messages_until_disconnected()
            finally:
                stop_pinging.set()

    def _ping_loop(self, stop_event):
        """Sends a ping every couple seconds until told to stop (connection closed)."""
        while not stop_event.is_set():
            self.send_message({"type": "ping"})
            stop_event.wait(PING_INTERVAL_SECONDS)

    def _read_messages_until_disconnected(self):
        """
        Reads incoming messages from wsl_app one line at a time for as
        long as the connection stays open, dispatching each by its "type".
        Pongs are just the ping heartbeat's reply (nothing to do);
        transcripts are forwarded to transcript_received for the UI to
        display; suggestions are just printed for now — there's no
        dedicated suggestions UI pane yet (that's a later day's job).
        """
        while True:
            line = self._connection.readline()
            if not line:
                raise OSError("wsl_app closed the connection")
            message = json.loads(line)
            if message.get("type") == "transcript":
                self.transcript_received.emit(message["text"])
            elif message.get("type") == "suggestion":
                print(f"Suggestion: {message['text']}")

    def send_message(self, message):
        """
        Writes one JSON message to the active wsl_app connection. Guarded by
        a lock so the ping loop and the audio capture thread can never
        interleave two writes into a corrupted line.

        Returns:
            bool: True if the message was sent, False if there's no active
            connection right now (caller should just drop the message).
        """
        with self._write_lock:
            connection = self._connection
            if connection is None:
                return False
            try:
                connection.write((json.dumps(message) + "\n").encode())
                connection.flush()
                return True
            except OSError:
                return False


class AudioCaptureManager(QObject):
    """
    Owns the background thread that captures WASAPI loopback audio and
    streams it to wsl_app as audio_chunk messages. Capture only runs
    during an explicit session (see start_capture()/stop_capture()), and
    restarts on a new device whenever the dropdown selection changes while
    a session is running.

    Stopping never blocks the calling thread: it just signals the capture
    thread and returns. The capture thread does its own (possibly slow)
    stream teardown, then reports back via the capture_thread_finished
    signal — the same cross-thread signal pattern WslConnection uses for
    connection_changed — so the UI updates only once capture has actually
    stopped, without freezing while it waits.
    """

    status_changed = Signal(str)
    capture_thread_finished = Signal()

    def __init__(self, audio, wsl_connection):
        """Stores the shared PyAudio instance and the connection to send chunks over."""
        super().__init__()
        self._audio = audio
        self._wsl_connection = wsl_connection
        self._device = None
        self._stop_event = None
        self._thread = None
        # Set while a capture thread is being torn down for a restart (device
        # switch), so handle_capture_thread_finished() knows to start capture
        # again on the new device once the old thread has fully exited.
        self._device_to_start_after_stop = None
        self.capture_thread_finished.connect(self.handle_capture_thread_finished)

    def set_device(self, device):
        """
        Selects `device` for future capture. If capture is currently
        running, restarts it on the new device right away.
        """
        if self._thread is not None:
            self._device_to_start_after_stop = device
            self._stop_event.set()
        else:
            self._device = device

    def start_capture(self):
        """Starts capture on the currently selected device, if not already running."""
        if self._device is None or self._thread is not None:
            return
        self._begin_capture_thread(self._device)

    def stop_capture(self):
        """
        Signals the running capture thread (if any) to stop and returns
        immediately. status_changed reports "Not capturing" once the
        thread has actually finished, via capture_thread_finished.
        """
        if self._thread is None:
            return
        self._device_to_start_after_stop = None
        self._stop_event.set()

    def handle_capture_thread_finished(self):
        """
        Runs on the GUI thread once a capture thread has fully closed its
        stream. Clears the finished thread's state, then either starts a
        fresh thread on a newly-selected device (if set_device() was
        called mid-capture) or reports that capture is stopped.
        """
        self._thread = None
        self._stop_event = None
        device = self._device_to_start_after_stop
        self._device_to_start_after_stop = None
        if device is not None:
            self._device = device
            self._begin_capture_thread(device)
        else:
            self.status_changed.emit("Not capturing")

    def _begin_capture_thread(self, device):
        """Starts a fresh capture thread on `device`."""
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._capture_loop, args=(device, self._stop_event), daemon=True)
        self._thread.start()
        self.status_changed.emit(f"Capturing: {device['name']}")

    def _capture_loop(self, device, stop_event):
        """
        Reads small blocks of audio from `device` and sends each one as an
        audio_chunk message, until `stop_event` is set. Emits
        capture_thread_finished once the stream is fully closed, so the
        GUI thread can safely react to the thread being done.
        """
        rate = int(device["defaultSampleRate"])
        channels = device["maxInputChannels"]
        chunk_frames = int(rate * CHUNK_SECONDS)
        stream = self._audio.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=chunk_frames,
        )
        try:
            while not stop_event.is_set():
                data = stream.read(chunk_frames, exception_on_overflow=False)
                self._wsl_connection.send_message(
                    {
                        "type": "audio_chunk",
                        "data": base64.b64encode(data).decode("ascii"),
                        "sample_rate": rate,
                        "channels": channels,
                        # Repeating the format fields on every chunk (instead of a one-time
                        # handshake message) is a little redundant, but simplest to reason
                        # about for now. Could be optimized later if it ever matters.
                        "sample_format": "float32",
                    }
                )
        finally:
            stream.stop_stream()
            stream.close()
            self.capture_thread_finished.emit()


def create_app():
    """
    Creates the Qt application instance that owns the event loop.

    Returns:
        QApplication: the single application instance for this process.
    """
    return QApplication(sys.argv)


def create_window():
    """
    Builds the main window, titled and sized, but not yet shown.

    Returns:
        QMainWindow: the configured top-level window.
    """
    window = QMainWindow()
    window.setWindowTitle("Adarna")
    window.resize(800, 600)
    return window


def create_central_widget(window):
    """
    Creates the container widget that stacks the connection status,
    capture device dropdown, capture status, session buttons, and
    transcript pane vertically, and sets it as the window's central
    widget.

    Returns:
        QVBoxLayout: the layout new widgets should be added to.
    """
    widget = QWidget()
    layout = QVBoxLayout(widget)
    window.setCentralWidget(widget)
    return layout


def create_connection_status_label(layout):
    """
    Adds a label showing the wsl_app connection state.

    Returns:
        QLabel: the label to keep updated as the connection state changes.
    """
    label = QLabel("Disconnected")
    layout.addWidget(label)
    return label


def create_device_dropdown(layout):
    """
    Adds a dropdown for picking which WASAPI loopback device to capture from.

    Returns:
        QComboBox: the (still empty) dropdown to populate with devices.
    """
    dropdown = QComboBox()
    layout.addWidget(dropdown)
    return dropdown


def create_capture_status_label(layout):
    """
    Adds a label showing which device is currently being captured.

    Returns:
        QLabel: the label to keep updated as capture starts/stops.
    """
    label = QLabel("Not capturing")
    layout.addWidget(label)
    return label


def create_session_buttons(layout):
    """
    Adds side-by-side "Start Session" / "Stop Session" buttons. Stop
    starts disabled, since no session is running yet.

    Returns:
        tuple[QPushButton, QPushButton]: the start and stop buttons.
    """
    row = QHBoxLayout()
    start_button = QPushButton("Start Session")
    stop_button = QPushButton("Stop Session")
    stop_button.setEnabled(False)
    row.addWidget(start_button)
    row.addWidget(stop_button)
    layout.addLayout(row)
    return start_button, stop_button


def create_transcript_pane(layout):
    """
    Adds a scrolling, read-only pane that displays each transcript as it
    arrives from wsl_app.

    Returns:
        QPlainTextEdit: the pane to append new transcript text to.
    """
    pane = QPlainTextEdit()
    pane.setReadOnly(True)
    layout.addWidget(pane)
    return pane


def list_loopback_devices(audio):
    """
    Lists every WASAPI loopback-capable device available for capture.

    Returns:
        list[dict]: pyaudiowpatch device info dicts, one per loopback device.
    """
    return list(audio.get_loopback_device_info_generator())


def populate_device_dropdown(dropdown, devices, default_device):
    """Fills the dropdown with device names, preselecting the system default."""
    for device in devices:
        dropdown.addItem(device["name"], userData=device)
    default_index = next(
        (i for i, device in enumerate(devices) if device["index"] == default_device["index"]), 0
    )
    dropdown.setCurrentIndex(default_index)


def start_wsl_connection(status_label):
    """
    Starts the background thread that connects to wsl_app and keeps
    status_label in sync with the connection state.

    Returns:
        WslConnection: the connection object driving the background thread.
    """
    wsl_connection = WslConnection()
    wsl_connection.connection_changed.connect(
        lambda connected: status_label.setText("Connected" if connected else "Disconnected")
    )
    threading.Thread(target=wsl_connection.run, daemon=True).start()
    return wsl_connection


def start_global_hotkey(wsl_connection):
    """
    Registers the global "generate a suggestion now" hotkey (see
    HOTKEY_COMBINATION) and sends wsl_app a hotkey_triggered message
    whenever it's pressed. wsl_app decides whether to actually act on it
    (it's ignored there if no session is running).

    Returns:
        pynput.keyboard.GlobalHotKeys: the running hotkey listener. Must be
        kept referenced by the caller for as long as the app runs, or it
        would be garbage-collected and stop listening.
    """

    def notify_wsl_app_hotkey_was_pressed():
        """Sends wsl_app the hotkey_triggered message."""
        wsl_connection.send_message({"type": "hotkey_triggered"})

    hotkey_listener = keyboard.GlobalHotKeys({HOTKEY_COMBINATION: notify_wsl_app_hotkey_was_pressed})
    hotkey_listener.start()
    return hotkey_listener


def create_audio_capture_manager(audio, wsl_connection, device_dropdown, default_device, capture_status_label):
    """
    Creates the audio capture manager: preselects the default device,
    restarts capture on a new device whenever the dropdown changes (if a
    session is running), and keeps capture_status_label in sync. Capture
    itself only starts/stops via start_session()/stop_session() — see
    create_session_controls().

    Returns:
        AudioCaptureManager: the manager driving the capture thread.
    """
    capture_manager = AudioCaptureManager(audio, wsl_connection)
    capture_manager.status_changed.connect(capture_status_label.setText)
    capture_manager.set_device(default_device)
    device_dropdown.currentIndexChanged.connect(
        lambda index: capture_manager.set_device(device_dropdown.itemData(index))
    )
    return capture_manager


def create_session_controls(wsl_connection, capture_manager, layout):
    """
    Adds the Start Session / Stop Session buttons and wires them up:
    starting sends session_started to wsl_app and starts audio capture;
    stopping sends session_stopped and stops it. Button enabled-state
    tracks which action is currently valid.
    """
    start_button, stop_button = create_session_buttons(layout)

    def start_session():
        """Begins a session: notifies wsl_app, starts capture, and flips button state."""
        wsl_connection.send_message({"type": "session_started"})
        capture_manager.start_capture()
        start_button.setEnabled(False)
        stop_button.setEnabled(True)

    def stop_session():
        """Ends a session: notifies wsl_app, stops capture, and flips button state."""
        wsl_connection.send_message({"type": "session_stopped"})
        capture_manager.stop_capture()
        start_button.setEnabled(True)
        stop_button.setEnabled(False)

    start_button.clicked.connect(start_session)
    stop_button.clicked.connect(stop_session)


def main():
    """
    Entry point: creates the app and window, starts the background
    connection to wsl_app, wires up WASAPI loopback audio capture, the
    session start/stop controls, and the global suggestion hotkey, and
    runs the event loop until the window is closed.
    """
    app = create_app()
    window = create_window()
    layout = create_central_widget(window)
    status_label = create_connection_status_label(layout)
    device_dropdown = create_device_dropdown(layout)
    capture_status_label = create_capture_status_label(layout)
    transcript_pane = create_transcript_pane(layout)

    audio = pyaudio.PyAudio()
    devices = list_loopback_devices(audio)
    default_device = audio.get_default_wasapi_loopback()
    populate_device_dropdown(device_dropdown, devices, default_device)

    wsl_connection = start_wsl_connection(status_label)
    wsl_connection.transcript_received.connect(transcript_pane.appendPlainText)
    capture_manager = create_audio_capture_manager(
        audio, wsl_connection, device_dropdown, default_device, capture_status_label
    )
    create_session_controls(wsl_connection, capture_manager, layout)
    # Kept referenced for the app's lifetime -- see start_global_hotkey()'s docstring.
    hotkey_listener = start_global_hotkey(wsl_connection)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
