import base64
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pyaudiowpatch as pyaudio
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QMainWindow, QVBoxLayout, QWidget

CONFIG_PATH = Path(__file__).resolve().parent.parent / "ipc_config.json"
PING_INTERVAL_SECONDS = 2
RECONNECT_DELAY_SECONDS = 2
CHUNK_SECONDS = 0.1


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
    for the full set of message types this protocol carries: today that's
    ping/pong and audio_chunk; transcript/suggestion messages are still to
    come.
    """

    connection_changed = Signal(bool)

    def __init__(self):
        """Sets up the (initially disconnected) state shared across threads."""
        super().__init__()
        self._connection = None
        self._write_lock = threading.Lock()

    def run(self):
        """
        Loops forever: connects to wsl_app, pings it every couple seconds,
        and reconnects automatically if the connection drops or fails.
        """
        port = load_port()
        while True:
            try:
                self._ping_until_disconnected(port)
            except OSError:
                pass
            self._connection = None
            self.connection_changed.emit(False)
            time.sleep(RECONNECT_DELAY_SECONDS)

    def _ping_until_disconnected(self, port):
        """
        Opens one connection to wsl_app and pings it every couple seconds
        until the connection breaks.
        """
        with socket.create_connection(("127.0.0.1", port)) as sock:
            self._connection = sock.makefile("rwb")
            while True:
                if not self.send_message({"type": "ping"}):
                    raise OSError("wsl_app closed the connection")
                line = self._connection.readline()
                if not line:
                    raise OSError("wsl_app closed the connection")
                self.connection_changed.emit(True)
                time.sleep(PING_INTERVAL_SECONDS)

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
    streams it to wsl_app as audio_chunk messages. Starts/stops capture in
    step with the wsl_app connection state, and restarts on a new device
    whenever the dropdown selection changes.
    """

    status_changed = Signal(str)

    def __init__(self, audio, wsl_connection):
        """Stores the shared PyAudio instance and the connection to send chunks over."""
        super().__init__()
        self._audio = audio
        self._wsl_connection = wsl_connection
        self._device = None
        self._stop_event = None
        self._thread = None

    def set_device(self, device):
        """
        Selects `device` for future capture. If capture is currently
        running, restarts it on the new device right away.
        """
        self._device = device
        if self._thread is not None:
            self._start_capture()

    def handle_connection_changed(self, connected):
        """Starts capture once wsl_app is connected, stops it when the connection drops."""
        if connected:
            self._start_capture()
        else:
            self._stop_capture()

    def _start_capture(self):
        """Stops any capture in progress, then starts a fresh thread on the current device."""
        self._stop_capture()
        if self._device is None:
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._capture_loop, args=(self._device, self._stop_event), daemon=True
        )
        self._thread.start()
        self.status_changed.emit(f"Capturing: {self._device['name']}")

    def _stop_capture(self):
        """Signals the running capture thread (if any) to stop and waits for it to exit."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self._stop_event = None
        self.status_changed.emit("Not capturing")

    def _capture_loop(self, device, stop_event):
        """
        Reads small blocks of audio from `device` and sends each one as an
        audio_chunk message, until `stop_event` is set.
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
    capture device dropdown, and capture status vertically, and sets it
    as the window's central widget.

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


def start_audio_capture(audio, wsl_connection, device_dropdown, default_device, capture_status_label):
    """
    Wires up audio capture: starts/stops with the wsl_app connection,
    restarts on device dropdown changes, and keeps capture_status_label
    in sync.

    Returns:
        AudioCaptureManager: the manager driving the capture thread.
    """
    capture_manager = AudioCaptureManager(audio, wsl_connection)
    capture_manager.status_changed.connect(capture_status_label.setText)
    capture_manager.set_device(default_device)
    device_dropdown.currentIndexChanged.connect(
        lambda index: capture_manager.set_device(device_dropdown.itemData(index))
    )
    wsl_connection.connection_changed.connect(capture_manager.handle_connection_changed)
    return capture_manager


def main():
    """
    Entry point: creates the app and window, starts the background
    connection to wsl_app, wires up WASAPI loopback audio capture, and
    runs the event loop until the window is closed.
    """
    app = create_app()
    window = create_window()
    layout = create_central_widget(window)
    status_label = create_connection_status_label(layout)
    device_dropdown = create_device_dropdown(layout)
    capture_status_label = create_capture_status_label(layout)

    audio = pyaudio.PyAudio()
    devices = list_loopback_devices(audio)
    default_device = audio.get_default_wasapi_loopback()
    populate_device_dropdown(device_dropdown, devices, default_device)

    wsl_connection = start_wsl_connection(status_label)
    start_audio_capture(audio, wsl_connection, device_dropdown, default_device, capture_status_label)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
