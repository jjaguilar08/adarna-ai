import base64
import json
import queue
import socket
import sys
import threading
import time
from pathlib import Path

import pyaudiowpatch as pyaudio
from pynput import keyboard
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "ipc_config.json"
PING_INTERVAL_SECONDS = 2
RECONNECT_DELAY_SECONDS = 2
CHUNK_SECONDS = 0.1

# How long the capture loop waits for the next audio block before checking
# whether it's been told to stop. Keeps Stop Session/a device switch
# responsive even if the device stops delivering audio altogether -- see
# _capture_loop()'s docstring for the bug this constant exists to prevent.
CAPTURE_QUEUE_POLL_SECONDS = 0.5

# How many captured-but-not-yet-sent audio blocks _capture_loop will hold
# onto before it starts dropping the newest ones. At CHUNK_SECONDS-sized
# blocks, this is a few seconds of buffering -- enough to ride out a brief
# stall in sending (e.g. wsl_app briefly falling behind), without letting a
# longer one (the multi-second CPU-contention stalls noted in PRD §9) grow
# memory use without bound. The old stream.read()-based loop never needed
# this: a stalled send() there meant the next read() simply didn't happen
# yet, so audio piled up in the device driver's own bounded buffer instead
# of in our process. Callback mode hands blocks over on a fixed schedule
# regardless of whether anything is still keeping up, so this queue needs
# its own bound to reproduce that same graceful-degradation behavior.
MAX_QUEUED_AUDIO_BLOCKS = 50

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

# Global show/hide hotkey for the overlay window (see create_overlay_window()).
# Deliberately a different combination from HOTKEY_COMBINATION above -- pynput's
# GlobalHotKeys registers both from one shared listener (see start_global_hotkeys()),
# and dispatches each independently, so the two don't interfere with each other.
OVERLAY_HOTKEY_COMBINATION = "<ctrl>+<alt>+o"

# Settings panel choices, sent to wsl_app as settings_changed when a session
# starts. Keys are what's shown in the Mode dropdown; values are the wire
# protocol's mode strings.
MODE_LABELS_TO_VALUES = {
    "Work Meeting": "meeting",
    "Interview": "interview",
}

WHISPER_MODEL_SIZES = ["small.en", "base.en"]

SUGGESTION_PAUSE_PRESETS_SECONDS = [0.8, 1.2, 1.6, 2.0]
# Picked from the presets list itself (rather than a separate literal) so
# it can never drift out of sync with it -- 1.2s matches Day 7's default.
DEFAULT_SUGGESTION_PAUSE_SECONDS = SUGGESTION_PAUSE_PRESETS_SECONDS[1]


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
    audio_chunk, session_started/session_stopped, session_start_failed,
    hotkey_triggered, settings_changed, auto_suggest_changed, transcript,
    and suggestion.
    """

    connection_changed = Signal(bool)
    transcript_received = Signal(str)
    suggestion_received = Signal(str)
    session_start_failed_received = Signal(str, int)

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
        transcripts and suggestions are forwarded to their own signal for
        the UI to display; session_start_failed is forwarded so the UI can
        revert out of the "session active" state it optimistically entered
        when Start Session was pressed — its attempt_id is the same value
        sent on the session_started message it's responding to, echoed back
        so the UI can tell a stale failure (for an attempt already
        abandoned in favor of a newer one) from a current one.
        """
        while True:
            line = self._connection.readline()
            if not line:
                raise OSError("wsl_app closed the connection")
            message = json.loads(line)
            if message.get("type") == "transcript":
                self.transcript_received.emit(message["text"])
            elif message.get("type") == "suggestion":
                self.suggestion_received.emit(message["text"])
            elif message.get("type") == "session_start_failed":
                self.session_start_failed_received.emit(
                    message.get("reason", "Unknown error"), message.get("attempt_id", -1)
                )

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

    If the stream itself fails mid-capture (e.g. the Windows audio device
    was unplugged or changed), that's different from a requested stop: the
    session is still running on the wsl_app side with nothing left feeding
    it audio. capture_thread_finished's bool tells the caller which case
    happened; capture_failed is emitted (on the GUI thread) only for the
    unrequested-failure case, so a caller can end the whole session rather
    than just updating the capture status label.
    """

    status_changed = Signal(str)
    capture_thread_finished = Signal(bool)
    capture_failed = Signal()

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

    def handle_capture_thread_finished(self, stream_failed):
        """
        Runs on the GUI thread once a capture thread has fully closed its
        stream. Clears the finished thread's state, then: restarts on a
        newly-selected device if set_device() was called mid-capture — even
        if the old stream also failed around the same time, since the user
        is already leaving that device, so the switch should win rather
        than reporting a failure for a device they're abandoning anyway;
        otherwise reports the failure via capture_failed if the stream
        broke on its own; otherwise reports that capture is stopped
        normally.
        """
        self._thread = None
        self._stop_event = None
        device = self._device_to_start_after_stop
        self._device_to_start_after_stop = None
        if device is not None:
            self._device = device
            self._begin_capture_thread(device)
        elif stream_failed:
            self.status_changed.emit("Not capturing (device error)")
            self.capture_failed.emit()
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
        Captures audio from `device` and sends each block as an audio_chunk
        message, until `stop_event` is set or the stream itself stops
        unexpectedly (e.g. the device was unplugged or changed).

        Uses pyaudiowpatch's non-blocking "callback" mode rather than
        calling stream.read() in a loop on this thread. That used to be a
        blocking loop guarded only by checking stop_event between reads --
        but a blocked native read can't be interrupted from Python, and on
        a flaky device (reproduced live on a Bluetooth loopback device,
        where the driver just stopped delivering audio mid-capture)
        stream.read() hung forever, so stop_event.set() did nothing and
        Stop Session/device-switching became permanent no-ops. Verified
        against the installed pyaudiowpatch that its blocking read() has no
        timeout parameter to fall back on instead (see project_notes.md,
        Day 11) -- the callback API is the one it actually supports for
        this.

        With callback mode, PortAudio invokes on_audio_block on its own
        thread whenever a block is ready, and this thread never makes a
        blocking call into the device at all -- it just waits on the queue
        that callback feeds, re-checking stop_event every time that wait
        times out. So a stop request is noticed within
        CAPTURE_QUEUE_POLL_SECONDS no matter what the device is doing, even
        if it has stopped delivering audio entirely. That queue is bounded
        (MAX_QUEUED_AUDIO_BLOCKS) so a slow-to-send stretch degrades the
        same way the old blocking loop did -- newest audio wins, oldest gets
        dropped -- instead of growing memory use without bound.

        A stream going quiet without ever calling on_audio_block again is
        deliberately NOT treated as a failure on its own -- only
        stream.is_active() going False is. An earlier version of this fix
        also tried to infer failure from "no data for N seconds," but
        testing live against a Logi USB headset (Day 11) showed that
        device's WASAPI loopback delivers nothing at all -- not even
        near-silent blocks -- during any silence, including a completely
        normal pause between sentences in a real conversation, not just
        before the first one. There's no reliable way from here to tell
        "the device died" apart from "nobody's talking right now," so this
        only acts on the signal PortAudio itself actually gives for that
        (is_active() going False); the rest of the fix -- a stop request
        always being noticed within CAPTURE_QUEUE_POLL_SECONDS -- already
        closes the actual bug regardless.

        Emits capture_thread_finished once the stream is fully closed (or,
        if the stream couldn't even be opened, right away), so the GUI
        thread can safely react to the thread being done — its bool
        argument tells the caller whether this was a real failure rather
        than a requested stop.
        """
        rate = int(device["defaultSampleRate"])
        channels = device["maxInputChannels"]
        chunk_frames = int(rate * CHUNK_SECONDS)
        audio_blocks = queue.Queue(maxsize=MAX_QUEUED_AUDIO_BLOCKS)

        def on_audio_block(in_data, frame_count, time_info, status_flags):
            """
            Runs on PortAudio's own thread: hands one captured block to the
            queue and asks for more. If the queue is already full, makes
            room by dropping the OLDEST queued block rather than this new
            one -- a /code-review catch: dropping the incoming block instead
            would keep sending increasingly stale audio for the entire
            length of a stall and then permanently lose whatever was said
            during it, the opposite of the graceful degradation
            MAX_QUEUED_AUDIO_BLOCKS is meant to give.
            """
            try:
                audio_blocks.put_nowait(in_data)
            except queue.Full:
                try:
                    audio_blocks.get_nowait()
                except queue.Empty:
                    pass
                try:
                    audio_blocks.put_nowait(in_data)
                except queue.Full:
                    pass  # lost a race with the consumer thread; fine to just drop this one
            return (None, pyaudio.paContinue)

        try:
            stream = self._audio.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=chunk_frames,
                stream_callback=on_audio_block,
            )
        except Exception as error:
            print(f"Could not open audio capture stream, stopping: {error}")
            self.capture_thread_finished.emit(True)
            return

        stream_failed = False
        try:
            while not stop_event.is_set():
                try:
                    data = audio_blocks.get(timeout=CAPTURE_QUEUE_POLL_SECONDS)
                except queue.Empty:
                    if not stream.is_active():
                        print("Audio capture stream is no longer active, stopping")
                        stream_failed = True
                        break
                    continue
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
            # A stream that already failed can raise again here on teardown.
            # Without this try/except, that second exception would stop
            # capture_thread_finished from ever being emitted, leaving
            # AudioCaptureManager stuck believing capture is still running.
            try:
                stream.stop_stream()
                stream.close()
            except Exception as error:
                # Deliberately broad: whatever this raises must not prevent
                # the emit below from running.
                print(f"Error while closing audio stream (ignoring, already unusable): {error}")
            self.capture_thread_finished.emit(stream_failed)


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
    Adds a labeled, scrolling, read-only pane that displays each transcript
    as it arrives from wsl_app.

    Returns:
        QPlainTextEdit: the pane to append new transcript text to.
    """
    layout.addWidget(QLabel("Transcript"))
    pane = QPlainTextEdit()
    pane.setReadOnly(True)
    layout.addWidget(pane)
    return pane


class LatestSuggestion(QObject):
    """
    Remembers the most recently received suggestion text, so the "Copy
    Latest Suggestion" button can read it without needing its own
    connection to wsl_app. A QObject (not a plain class) specifically so
    that connecting suggestion_received.update to it is a proper
    cross-thread queued connection, same as every other signal in this
    file that crosses from a background thread to the GUI thread — a plain
    object would make Qt call update() directly on WslConnection's
    background reader thread instead.
    """

    def __init__(self):
        """Starts with no suggestion received yet."""
        super().__init__()
        self.text = ""

    def update(self, text):
        """Stores `text` as the latest suggestion."""
        self.text = text


class OverlayToggle(QObject):
    """
    Lets the global show/hide hotkey (fired from pynput's own listener
    thread) request the overlay window's visibility be flipped, without
    touching a Qt widget off the GUI thread directly -- Qt widgets may only
    be shown/hidden from the thread that owns them. toggle_requested is
    connected to this object's own toggle() method (a real bound method,
    not a lambda -- see toggle()'s docstring for why that distinction
    matters), the same cross-thread queued-connection pattern
    LatestSuggestion above uses.
    """

    toggle_requested = Signal()

    def __init__(self, overlay_window):
        """Stores the overlay window this toggle shows/hides, and connects the signal to toggle() below."""
        super().__init__()
        self._overlay_window = overlay_window
        self.toggle_requested.connect(self.toggle)

    def toggle(self):
        """
        Shows overlay_window if it's hidden, or hides it if it's shown.

        Connected to toggle_requested as a bound method rather than a
        lambda specifically so Qt has a real QObject (this one) to read
        thread affinity from -- a plain lambda has no owning QObject for
        Qt to key off of, so a cross-thread emit (from pynput's listener
        thread) would run the lambda directly on that thread instead of
        marshaling it to the GUI thread that owns overlay_window, which
        Qt widgets aren't safe against.
        """
        self._overlay_window.setVisible(not self._overlay_window.isVisible())


def create_overlay_window():
    """
    Builds the always-on-top overlay window: frameless, stays above other
    windows, and shows only the latest suggestion text -- deliberately
    minimal, per PRD §8 Phase 2 (no transcript, no settings, no session
    controls here; those all stay on the main window). Starts hidden;
    create_overlay_toggle() below wires up the hotkey that shows it.

    Returns:
        tuple[QWidget, QPlainTextEdit]: the overlay window itself, and the
        read-only pane inside it to keep in sync with the latest
        suggestion (see connect_incoming_messages_to_ui()).
    """
    window = QWidget()
    window.setWindowTitle("Adarna Overlay")
    window.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
    window.resize(420, 160)

    layout = QVBoxLayout(window)
    pane = QPlainTextEdit()
    pane.setReadOnly(True)
    layout.addWidget(pane)

    return window, pane


def create_overlay_toggle(overlay_window):
    """
    Creates the OverlayToggle that the show/hide hotkey uses to flip
    overlay_window's visibility on the GUI thread.

    Returns:
        OverlayToggle: emit its toggle_requested signal (safe from any
        thread) to show the overlay if hidden, or hide it if shown.
    """
    return OverlayToggle(overlay_window)


def create_suggestions_section(layout):
    """
    Adds a labeled, read-only pane that displays the latest suggestion from
    wsl_app — a new one replaces whatever was shown before, rather than
    appending to a growing list — plus a "Copy Latest Suggestion" button
    that copies the pane's current text to the system clipboard.

    Returns:
        tuple[QPlainTextEdit, LatestSuggestion]: the pane to set new
        suggestion text on, and the tracker the Copy button reads from —
        the caller should also connect this to whatever emits new
        suggestion text (see main()).
    """
    layout.addWidget(QLabel("Suggestions"))
    pane = QPlainTextEdit()
    pane.setReadOnly(True)
    layout.addWidget(pane)

    latest_suggestion = LatestSuggestion()

    def copy_latest_suggestion():
        """Copies the most recently received suggestion text to the clipboard."""
        if latest_suggestion.text:
            QApplication.clipboard().setText(latest_suggestion.text)

    copy_button = QPushButton("Copy Latest Suggestion")
    copy_button.clicked.connect(copy_latest_suggestion)
    layout.addWidget(copy_button)

    return pane, latest_suggestion


def create_suggestion_trigger_controls(layout):
    """
    Adds a row with the "Auto-suggest on pause" checkbox (checked by
    default, matching today's pre-Phase-1.5 behavior) and a "Generate
    Suggestion Now" button. Unlike the settings panel above, the checkbox
    is meant to be flipped live during a running session — see
    create_session_controls() and wsl_app's SuggestionTrigger — so it lives
    outside "Session Settings" rather than inside it. The button gives
    manual triggering an in-window equivalent of the global hotkey, for
    anyone who'd rather click than reach for a key combination.

    Returns:
        tuple[QCheckBox, QPushButton]: the auto-suggest checkbox and the
        manual-trigger button.
    """
    row = QHBoxLayout()
    auto_suggest_checkbox = QCheckBox("Auto-suggest on pause")
    auto_suggest_checkbox.setChecked(True)
    generate_button = QPushButton("Generate Suggestion Now")
    row.addWidget(auto_suggest_checkbox)
    row.addWidget(generate_button)
    layout.addLayout(row)
    return auto_suggest_checkbox, generate_button


def create_settings_panel(layout):
    """
    Adds a labeled settings panel with the mode, Whisper model size,
    suggestion pause delay, and pre-session context notes controls used to
    configure the next session. These values are only read (and sent to
    wsl_app) when Start Session is pressed — see create_session_controls()
    — so changing them mid-session has no effect until the next session
    starts.

    Returns:
        tuple[QComboBox, QComboBox, QComboBox, QPlainTextEdit]: the mode,
        Whisper model size, and suggestion pause dropdowns, plus the
        context notes text box, in that order.
    """
    group = QGroupBox("Session Settings")
    form = QFormLayout(group)

    mode_dropdown = QComboBox()
    for label in MODE_LABELS_TO_VALUES:
        mode_dropdown.addItem(label)
    form.addRow("Mode:", mode_dropdown)

    whisper_model_dropdown = QComboBox()
    for size in WHISPER_MODEL_SIZES:
        whisper_model_dropdown.addItem(size)
    form.addRow("Whisper model:", whisper_model_dropdown)

    pause_dropdown = QComboBox()
    for seconds in SUGGESTION_PAUSE_PRESETS_SECONDS:
        pause_dropdown.addItem(f"{seconds}s", userData=seconds)
    pause_dropdown.setCurrentIndex(SUGGESTION_PAUSE_PRESETS_SECONDS.index(DEFAULT_SUGGESTION_PAUSE_SECONDS))
    form.addRow("Suggestion pause:", pause_dropdown)

    context_notes_edit = QPlainTextEdit()
    context_notes_edit.setPlaceholderText(
        "Optional: paste anything relevant for this session — a CV/job "
        "description for an interview, a PRD excerpt or agenda for a "
        "meeting."
    )
    context_notes_edit.setFixedHeight(80)
    form.addRow("Context notes:", context_notes_edit)

    layout.addWidget(group)
    return mode_dropdown, whisper_model_dropdown, pause_dropdown, context_notes_edit


def current_settings_message(mode_dropdown, whisper_model_dropdown, pause_dropdown, context_notes_edit, auto_suggest_checkbox):
    """
    Reads the settings panel's current values and packages them into the
    settings_changed message to send wsl_app.

    Returns:
        dict: the settings_changed message.
    """
    return {
        "type": "settings_changed",
        "mode": MODE_LABELS_TO_VALUES[mode_dropdown.currentText()],
        "whisper_model_size": whisper_model_dropdown.currentText(),
        "suggestion_pause_seconds": pause_dropdown.currentData(),
        "context_notes": context_notes_edit.toPlainText().strip(),
        "auto_suggest_enabled": auto_suggest_checkbox.isChecked(),
    }


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


def connect_auto_suggest_toggle(wsl_connection, auto_suggest_checkbox):
    """
    Sends wsl_app an auto_suggest_changed message every time the checkbox
    is flipped, live, so a running session's SuggestionTrigger can turn
    pause-triggered suggestions on/off immediately — unlike every other
    settings panel control, this one isn't just read once at Start Session
    (see current_settings_message(), which also sends its starting value
    for the next session). wsl_app simply ignores this message if no
    session is currently active.
    """
    auto_suggest_checkbox.toggled.connect(
        lambda enabled: wsl_connection.send_message({"type": "auto_suggest_changed", "enabled": enabled})
    )


def connect_incoming_messages_to_ui(
    wsl_connection, transcript_pane, suggestions_pane, latest_suggestion, overlay_pane
):
    """
    Wires wsl_app's incoming transcript/suggestion messages to the UI: each
    transcript segment is appended to the transcript pane, each suggestion
    replaces whatever the suggestions pane and the overlay pane currently
    show (both render the same incoming suggestion text -- no protocol
    change needed for the overlay), and latest_suggestion is kept in sync
    so the Copy button always has something current to copy.
    """
    wsl_connection.transcript_received.connect(transcript_pane.appendPlainText)
    wsl_connection.suggestion_received.connect(suggestions_pane.setPlainText)
    wsl_connection.suggestion_received.connect(overlay_pane.setPlainText)
    wsl_connection.suggestion_received.connect(latest_suggestion.update)


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


def create_manual_suggestion_trigger(wsl_connection):
    """
    Creates the shared plumbing behind every way of manually asking for a
    suggestion right now — the global hotkey and the in-window "Generate
    Suggestion Now" button both end up calling the function this returns.
    wsl_app decides whether to actually act on it (it's ignored there if no
    session is running).

    A single persistent worker thread sends every hotkey_triggered message,
    rather than the caller's own thread sending it directly, and rather
    than spawning a fresh thread per request. A /code-review catch (Day 7):
    spawning a fresh thread per request is fine if send_message() returns
    quickly, but if wsl_app were ever genuinely hung without closing the
    socket, send_message()'s flush() could block indefinitely -- and a
    thread per request would then mean an unbounded pile of permanently
    stuck threads for as long as requests kept coming in during the hang.
    One dedicated worker bounds that to a single stuck thread at most; the
    maxsize=1 queue means extra requests while a send is in flight are
    simply treated as duplicates of the same "generate a suggestion now"
    request rather than queuing up.

    Returns:
        Callable[[], None]: call this to request a suggestion right now.
        Safe to call from any thread, including pynput's listener thread —
        see the returned function's own docstring for why that matters.
    """
    pending_requests = queue.Queue(maxsize=1)

    def send_trigger_messages_to_wsl_app():
        """Runs for the app's lifetime: sends one hotkey_triggered message to wsl_app each time the returned function records one."""
        while True:
            pending_requests.get()
            wsl_connection.send_message({"type": "hotkey_triggered"})

    threading.Thread(target=send_trigger_messages_to_wsl_app, daemon=True).start()

    def request_suggestion_now():
        """
        Records a request for a suggestion, for send_trigger_messages_to_wsl_app()
        to actually send. Never sends directly from here: this can be
        called from pynput's own listener thread, which also pumps the
        low-level keyboard hook Windows delivers every key event through,
        so anything that blocks here would delay it from noticing the next
        key press for as long as the block lasts -- and send_message() can
        itself block briefly on the network socket if wsl_app falls behind
        reading it (see project_notes.md, Day 11). Routing the in-window
        button's click through the same queue keeps it just as safe, and
        means a click and a hotkey press pressed at nearly the same moment
        collapse into one request instead of two.
        """
        try:
            pending_requests.put_nowait(None)
        except queue.Full:
            pass  # a send is already pending; this request is a duplicate of that one

    return request_suggestion_now


def start_global_hotkeys(hotkey_actions):
    """
    Registers every global hotkey the app listens for -- currently the
    "generate a suggestion now" hotkey (HOTKEY_COMBINATION) and the
    overlay show/hide hotkey (OVERLAY_HOTKEY_COMBINATION) -- from one
    shared pynput.keyboard.GlobalHotKeys listener. Must work even while
    windows_app doesn't have focus (the user will be focused on their
    meeting app), which is why this uses pynput's system-wide hook rather
    than plain Qt shortcuts (Qt shortcuts only fire while their own window
    is focused). GlobalHotKeys dispatches each registered combination to
    its own callback independently, so multiple bindings on one listener
    don't interfere with each other -- no need for a separate listener per
    hotkey.

    Returns:
        pynput.keyboard.GlobalHotKeys: the running hotkey listener. Must be
        kept referenced by the caller for as long as the app runs, or it
        would be garbage-collected and stop listening.
    """
    hotkey_listener = keyboard.GlobalHotKeys(hotkey_actions)
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


def create_session_controls(
    wsl_connection,
    capture_manager,
    mode_dropdown,
    whisper_model_dropdown,
    pause_dropdown,
    context_notes_edit,
    auto_suggest_checkbox,
    layout,
    window,
):
    """
    Adds the Start Session / Stop Session buttons and wires them up:
    starting sends the settings panel's current values (including the
    context notes and the auto-suggest checkbox's starting state) as
    settings_changed, then session_started (tagged with a fresh
    attempt_id — see handle_session_start_failed), and starts audio
    capture; stopping sends session_stopped and stops it. Button
    enabled-state tracks which action is currently valid.

    Also handles three ways a session can end itself, all reverting to the
    same clean pre-session UI state stop_session() reaches (see
    end_session()): wsl_app reporting it couldn't start the session at all
    (session_start_failed — e.g. a Whisper model reload failure), the
    wsl_app connection dropping mid-session, and the local audio capture
    stream itself failing mid-session (e.g. the Windows audio device
    disappeared or changed).
    """
    start_button, stop_button = create_session_buttons(layout)
    current_attempt_id = 0

    def revert_to_pre_session_state():
        """Resets the Start/Stop buttons to look like no session is running."""
        start_button.setEnabled(True)
        stop_button.setEnabled(False)

    def end_session(notify_wsl_app, capture_already_stopped):
        """
        Shared teardown for every way a session can end — an explicit Stop
        Session click, wsl_app failing to start one, the wsl_app connection
        dropping, or the capture stream failing — so each caller only has
        to say which parts of that teardown it still needs to do:
        notify_wsl_app is False when wsl_app already knows the session
        isn't running (it never started one, or the connection to it is
        already dead); capture_already_stopped is True when the capture
        thread has already finished on its own (a stream failure) rather
        than needing to be told to stop.
        """
        if notify_wsl_app:
            wsl_connection.send_message({"type": "session_stopped"})
        if not capture_already_stopped:
            capture_manager.stop_capture()
        revert_to_pre_session_state()

    def start_session():
        """Begins a session: sends the current settings, notifies wsl_app, starts capture, and flips button state."""
        nonlocal current_attempt_id
        current_attempt_id += 1
        wsl_connection.send_message(
            current_settings_message(
                mode_dropdown, whisper_model_dropdown, pause_dropdown, context_notes_edit, auto_suggest_checkbox
            )
        )
        wsl_connection.send_message({"type": "session_started", "attempt_id": current_attempt_id})
        capture_manager.start_capture()
        start_button.setEnabled(False)
        stop_button.setEnabled(True)

    def stop_session():
        """Ends a session the user explicitly asked to stop."""
        end_session(notify_wsl_app=True, capture_already_stopped=False)

    def handle_session_start_failed(reason, attempt_id):
        """
        wsl_app couldn't start the session it was just asked to (e.g. a
        Whisper model reload failed). Ignored unless attempt_id matches the
        most recent Start Session click: wsl_app processes messages on one
        connection strictly in order, so if the user hits Stop and then
        Start again before this (slow) failure response for an earlier,
        already-abandoned attempt arrives, reverting now would incorrectly
        tear down the newer session that's actually running rather than the
        old one this failure is actually about.
        """
        if attempt_id != current_attempt_id:
            return
        end_session(notify_wsl_app=False, capture_already_stopped=False)
        QMessageBox.warning(window, "Session Failed to Start", reason)

    def handle_connection_changed(connected):
        """
        If a session was active when the wsl_app connection drops (not at
        session_started — see handle_session_start_failed for that case),
        ends the session the same way Stop Session would, instead of
        leaving capture running against a dead socket. Ignored while no
        session is active (including every reconnect attempt before the
        first successful connection).
        """
        if connected or not stop_button.isEnabled():
            return
        end_session(notify_wsl_app=False, capture_already_stopped=False)

    def handle_capture_failed():
        """
        The local audio capture stream itself failed mid-session (e.g. the
        Windows audio device disappeared or changed) — ends the session the
        same way Stop Session would, since wsl_app would otherwise be left
        waiting for audio that's never coming. Capture has already stopped
        by the time this fires.
        """
        if not stop_button.isEnabled():
            return
        end_session(notify_wsl_app=True, capture_already_stopped=True)

    start_button.clicked.connect(start_session)
    stop_button.clicked.connect(stop_session)
    wsl_connection.session_start_failed_received.connect(handle_session_start_failed)
    wsl_connection.connection_changed.connect(handle_connection_changed)
    capture_manager.capture_failed.connect(handle_capture_failed)


def main():
    """
    Entry point: creates the app, the main window, and the overlay window,
    starts the background connection to wsl_app, wires up WASAPI loopback
    audio capture, the settings panel, the auto-suggest toggle and manual
    trigger button, the session start/stop controls, and the global
    suggestion-trigger and overlay show/hide hotkeys, and runs the event
    loop until the main window is closed.
    """
    app = create_app()
    window = create_window()
    layout = create_central_widget(window)
    status_label = create_connection_status_label(layout)
    device_dropdown = create_device_dropdown(layout)
    capture_status_label = create_capture_status_label(layout)
    mode_dropdown, whisper_model_dropdown, pause_dropdown, context_notes_edit = create_settings_panel(layout)
    auto_suggest_checkbox, generate_suggestion_button = create_suggestion_trigger_controls(layout)
    transcript_pane = create_transcript_pane(layout)
    suggestions_pane, latest_suggestion = create_suggestions_section(layout)
    overlay_window, overlay_pane = create_overlay_window()

    audio = pyaudio.PyAudio()
    devices = list_loopback_devices(audio)
    default_device = audio.get_default_wasapi_loopback()
    populate_device_dropdown(device_dropdown, devices, default_device)

    wsl_connection = start_wsl_connection(status_label)
    connect_incoming_messages_to_ui(wsl_connection, transcript_pane, suggestions_pane, latest_suggestion, overlay_pane)
    connect_auto_suggest_toggle(wsl_connection, auto_suggest_checkbox)
    capture_manager = create_audio_capture_manager(
        audio, wsl_connection, device_dropdown, default_device, capture_status_label
    )
    create_session_controls(
        wsl_connection,
        capture_manager,
        mode_dropdown,
        whisper_model_dropdown,
        pause_dropdown,
        context_notes_edit,
        auto_suggest_checkbox,
        layout,
        window,
    )
    # Kept referenced for the app's lifetime -- see start_global_hotkeys()'s docstring.
    request_suggestion_now = create_manual_suggestion_trigger(wsl_connection)
    generate_suggestion_button.clicked.connect(request_suggestion_now)
    overlay_toggle = create_overlay_toggle(overlay_window)
    hotkey_listener = start_global_hotkeys(
        {
            HOTKEY_COMBINATION: request_suggestion_now,
            OVERLAY_HOTKEY_COMBINATION: overlay_toggle.toggle_requested.emit,
        }
    )

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
