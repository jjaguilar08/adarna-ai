import base64
import bisect
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
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPen, QTextBlockFormat
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizeGrip,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "ipc_config.json"
PING_INTERVAL_SECONDS = 2
RECONNECT_DELAY_SECONDS = 2
CHUNK_SECONDS = 0.1

# App icon (Day 29, the Adarna bird logo) -- .ico specifically, not the
# .png sitting alongside it, since .ico natively bundles multiple
# resolutions (16/24/32/48/64/128/256px) for crisp rendering at every size
# Windows actually uses one (title bar, taskbar, Alt+Tab); a single PNG
# would just get blurrily rescaled at the sizes that aren't its native
# one. icon.png is kept alongside as the plain source image, in case it's
# ever needed for something that isn't a Windows icon.
ICON_PATH = Path(__file__).resolve().parent / "icon.ico"

# Where opt-in session transcripts (Day 22, see SessionRecorder) are saved --
# under windows_app/ itself, not wsl_app, so the file lands somewhere the
# user would actually look for it (Windows Explorer), not the WSL filesystem.
SESSIONS_DIR = Path(__file__).resolve().parent / "sessions"

# Default window title, and the title shown for as long as any opt-in
# disk-writing feature is active for the current session (see
# SessionRecorder, LiveAgentLog on the wsl_app side, and
# session_recording_window_title() below) -- each such feature exists for
# a consent/privacy reason (PRD §5/§9, RA 4200), so none of them should be
# silently invisible once turned on.
DEFAULT_WINDOW_TITLE = "Adarna"

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

SUGGESTION_PAUSE_PRESETS_SECONDS = [0.8, 1.2, 1.6, 2.0]
# Picked from the presets list itself (rather than a separate literal) so
# it can never drift out of sync with it -- 1.2s matches Day 7's default.
DEFAULT_SUGGESTION_PAUSE_SECONDS = SUGGESTION_PAUSE_PRESETS_SECONDS[1]

# Overlay visual redesign (Day 18), styled to match reference screenshots
# of a real ParakeetAI-style overlay: a black, semi-transparent, rounded
# panel with bold white text, and a static ⭐️ marker (not model output) in
# place of a "SUGGESTED RESPONSE" caption. Colors/spacing are a by-eye
# match for "the same feel," not a pixel-exact clone.
#
# Day 27: these are now plain (R, G, B, A) tuples, not CSS rgba() strings --
# OverlayWindow paints its own background directly in paintEvent() instead
# of through a QSS stylesheet (see that class's docstring for why: the
# stylesheet-declared background never actually rendered on the real
# Windows machine, confirmed live -- the panel was fully see-through at
# every opacity setting, only the text visibly responded to the opacity
# slider). The alpha values here are the panel's own base transparency;
# OVERLAY_OPACITY_* below scales them further via the slider.
OVERLAY_BACKGROUND_RGBA = (0, 0, 0, 200)
OVERLAY_BORDER_RGBA = (255, 255, 255, 30)
OVERLAY_ANSWER_MARKER = "⭐️"  # ⭐️
OVERLAY_TEXT_COLOR = "#FFFFFF"

# Range and default for the overlay opacity slider (see
# create_overlay_controls()), in whole percent. Floored well above 0 so
# the overlay can never be slid all the way to fully invisible with no
# obvious way back.
OVERLAY_OPACITY_MIN_PERCENT = 20
OVERLAY_OPACITY_MAX_PERCENT = 100
OVERLAY_OPACITY_DEFAULT_PERCENT = 90

# The two independent audio sources this app captures and streams to
# wsl_app (Day 19): the user's own microphone, alongside the pre-existing
# WASAPI loopback (system audio -- "everyone else"). Values must match
# wsl_app's own AUDIO_SOURCES strings exactly, since they're sent as-is on
# every audio_chunk/transcript message -- the two sides don't share a
# Python module, so this is kept in sync by hand, the same way the wire
# protocol's message "type" strings already are.
AUDIO_SOURCE_MIC = "mic"
AUDIO_SOURCE_LOOPBACK = "loopback"

# What each source's transcript lines are labeled with in the transcript
# pane (see TranscriptDisplay) -- matches wsl_app's own SOURCE_LABELS.
TRANSCRIPT_SOURCE_LABELS = {
    AUDIO_SOURCE_MIC: "You",
    AUDIO_SOURCE_LOOPBACK: "Them",
}

# Which side of the transcript pane each source's lines are aligned to (Day
# 22 follow-up, user-requested): "You" on the right, "Them" on the left, so
# the two sides of a conversation read visually distinct at a glance, the
# same left/right convention chat apps use for "me" vs. "everyone else."
TRANSCRIPT_SOURCE_ALIGNMENT = {
    AUDIO_SOURCE_MIC: Qt.AlignRight,
    AUDIO_SOURCE_LOOPBACK: Qt.AlignLeft,
}


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
    suggestion, generate_summary, summary, and summary_failed.
    """

    connection_changed = Signal(bool)
    transcript_received = Signal(str, str, float)  # source, text, started_at
    suggestion_received = Signal(str, str)
    session_start_failed_received = Signal(str, int)
    summary_received = Signal(str)
    summary_failed_received = Signal(str)

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
        the UI to display -- each transcript message is one finished,
        pause-bounded sentence from wsl_app's speech segmenter for one
        audio source (Day 19: "mic" or "loopback", see AUDIO_SOURCE_MIC/
        AUDIO_SOURCE_LOOPBACK), tagged with which source it came from and
        when it started being spoken (`started_at`, a wall-clock
        timestamp), both forwarded to TranscriptDisplay so it can label
        the line and place it in the right chronological spot relative to
        the other source's lines -- defensively defaulted (source to
        loopback, started_at to 0.0) the same way "question" below is, in
        case a field is ever missing or explicitly null; a suggestion's
        "question" field (Day 18) carries the transcript excerpt that
        prompted it, forwarded as suggestion_received's first argument
        (empty string if absent OR explicitly null) so the overlay can
        show it above the answer -- QLabel.setText() requires a real str,
        so a bare `or ""` guard is needed, not just .get()'s own default,
        since .get()'s default only kicks in when the key is missing
        entirely, not when it's present but explicitly null; session_start_failed
        is forwarded so the UI can revert out of the "session active" state
        it optimistically entered when Start Session was pressed — its
        attempt_id is the same value sent on the session_started message
        it's responding to, echoed back so the UI can tell a stale failure
        (for an attempt already abandoned in favor of a newer one) from a
        current one. summary/summary_failed (Day 23) are forwarded the same
        defensive way as suggestion's "question" field above, in case a
        field is ever missing or explicitly null.
        """
        while True:
            line = self._connection.readline()
            if not line:
                raise OSError("wsl_app closed the connection")
            message = json.loads(line)
            if message.get("type") == "transcript":
                self.transcript_received.emit(
                    message.get("source") or AUDIO_SOURCE_LOOPBACK, message["text"], message.get("started_at") or 0.0
                )
            elif message.get("type") == "suggestion":
                self.suggestion_received.emit(message.get("question") or "", message["text"])
            elif message.get("type") == "session_start_failed":
                self.session_start_failed_received.emit(
                    message.get("reason", "Unknown error"), message.get("attempt_id", -1)
                )
            elif message.get("type") == "summary":
                self.summary_received.emit(message.get("text") or "")
            elif message.get("type") == "summary_failed":
                self.summary_failed_received.emit(message.get("reason") or "Unknown error")

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
    Owns the background thread that captures audio from one device and
    streams it to wsl_app as audio_chunk messages, tagged with `source`
    (Day 19: AUDIO_SOURCE_MIC or AUDIO_SOURCE_LOOPBACK) so wsl_app can feed
    it into that source's own independent segmenter. One instance covers
    exactly one source -- main() creates two (see create_audio_capture_manager()),
    a mic one and a loopback one, run entirely independently: each owns its
    own capture thread, device selection, and status label, and neither
    blocks or waits on the other. Capture only runs during an explicit
    session (see start_capture()/stop_capture()), and restarts on a new
    device whenever the dropdown selection changes while a session is
    running.

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

    def __init__(self, audio, wsl_connection, source):
        """Stores the shared PyAudio instance, the connection to send chunks over, and which audio source (`source`) this manager captures and tags every chunk with."""
        super().__init__()
        self._audio = audio
        self._wsl_connection = wsl_connection
        self._source = source
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
                        "source": self._source,
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
    Creates the Qt application instance that owns the event loop, with the
    Adarna icon (see ICON_PATH) set application-wide -- every window this
    process creates (main window, the Suggestions window, the overlay)
    inherits it as their own icon unless they set one of their own, so this
    one call covers all of them rather than needing setWindowIcon() on
    each window individually. Covers the title bar, taskbar, and Alt+Tab
    icon on Windows.

    Returns:
        QApplication: the single application instance for this process.
    """
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    return app


def create_window():
    """
    Builds the main window, titled and sized, but not yet shown.

    Returns:
        QMainWindow: the configured top-level window.
    """
    window = QMainWindow()
    window.setWindowTitle(DEFAULT_WINDOW_TITLE)
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


def create_device_dropdown(layout, caption):
    """
    Adds a labeled dropdown for picking which audio device to capture from
    -- `caption` distinguishes which of the two sources (Day 19: loopback
    or mic) this particular dropdown picks a device for, since main() now
    adds two of these stacked in the same window.

    Returns:
        QComboBox: the (still empty) dropdown to populate with devices.
    """
    layout.addWidget(QLabel(caption))
    dropdown = QComboBox()
    layout.addWidget(dropdown)
    return dropdown


def create_capture_status_label(layout):
    """
    Adds a label showing which device is currently being captured. main()
    adds one of these per audio source (Day 19), so each source's capture
    state is visible independently.

    Returns:
        QLabel: the label to keep updated as capture starts/stops.
    """
    label = QLabel("Not capturing")
    layout.addWidget(label)
    return label


def create_mic_toggle_checkbox(layout):
    """
    Adds a "Mic enabled" checkbox, checked by default -- lets the user mute
    their own mic mid-session (e.g. someone else's TV/conversation bleeding
    into a real mic's noise floor) without stopping the whole session, the
    way Stop Session would. Only meaningful for the mic source -- loopback
    has no equivalent real-world noise problem to mute around, so there's
    no matching checkbox for it. Applies live, immediately, like the
    overlay's click-through checkbox (see create_overlay_controls()) rather
    than being read once at Start Session like the rest of "Session
    Settings" -- see create_session_controls(), which wires this up and
    also reads its starting state when a session begins.

    Returns:
        QCheckBox: the checkbox to wire up.
    """
    checkbox = QCheckBox("Mic enabled")
    checkbox.setChecked(True)
    layout.addWidget(checkbox)
    return checkbox


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


def create_readonly_text_pane(layout, widget_class=QPlainTextEdit):
    """
    Adds a scrolling, read-only text pane to layout -- the shared shape
    behind the transcript pane and the suggestions pane, so the two don't
    each hand-roll the same three lines. Not used by the overlay (Day 18):
    OverlayWindow's question/answer text needs its own styled QLabels
    instead, both for the visual redesign and so a click on top of the
    text still drags the window (see OverlayWindow._add_labeled_section).

    `widget_class` defaults to QPlainTextEdit (used by the suggestions
    pane); create_transcript_pane() passes QTextEdit instead, since only
    QTextEdit actually honors per-line paragraph alignment (see
    TranscriptDisplay._render()) -- confirmed directly, live, that
    QPlainTextEdit silently ignores QTextCursor block-alignment formatting
    entirely (its own QPlainTextDocumentLayout doesn't apply it, unlike
    QTextEdit's QTextDocumentLayout), rather than erroring or applying it
    incorrectly. Both classes share the read-only/scrolling/textCursor()
    API this app uses, so the swap is otherwise a drop-in one.

    Returns:
        QWidget: the pane (an instance of `widget_class`), already added to layout.
    """
    pane = widget_class()
    pane.setReadOnly(True)
    layout.addWidget(pane)
    return pane


def create_transcript_pane(layout):
    """
    Adds a labeled, scrolling, read-only pane that displays the transcript
    as it arrives from wsl_app. See TranscriptDisplay for how it's kept in
    sync with this pane. A QTextEdit specifically, not the shared default
    QPlainTextEdit (see create_readonly_text_pane()) -- needed so "You"/
    "Them" lines can be right/left-aligned.

    Returns:
        QTextEdit: the pane to keep in sync with incoming transcript text.
    """
    layout.addWidget(QLabel("Transcript"))
    return create_readonly_text_pane(layout, widget_class=QTextEdit)


class TranscriptDisplay(QObject):
    """
    Keeps the transcript pane showing every finished sentence transcribed
    so far, one labeled line per pause-bounded segment (see wsl_app/main.py's
    VoiceSegmenter -- Day 18, reverted back to this pause-then-transcribe-
    the-whole-utterance approach from Day 17's real-time streaming
    rewrite), from either audio source (Day 19: mic or loopback, see
    AUDIO_SOURCE_MIC/AUDIO_SOURCE_LOOPBACK).

    Each source is captured and transcribed by its own fully independent
    pipeline (see AudioCaptureManager, wsl_app's VoiceSegmenter-per-source),
    so segments don't arrive in strict spoken order: a longer mic segment
    started before a short loopback one can still finish transcribing
    after it. update() is inserted by `started_at` (bisect.insort) rather
    than just appended, so the rendered pane still reads in a sane
    chronological order even when both sides are talking around the same
    time -- not just correct within each source on its own.

    A QObject (not a plain class) so wsl_connection.transcript_received --
    emitted from WslConnection's background reader thread -- can be
    connected to update() as a real cross-thread queued connection, per
    this project's own hard-won lesson about lambdas having no owning
    QObject for Qt to marshal through (see OverlayToggle).
    """

    def __init__(self, pane):
        """Stores the pane to keep in sync, starting with nothing transcribed yet."""
        super().__init__()
        self._pane = pane
        # Each entry is (started_at, source, text), kept sorted by
        # started_at so _render() can just walk it in order.
        self._segments = []

    def update(self, source, text, started_at):
        """Inserts one newly-transcribed, source-tagged sentence into the transcript in chronological order (see class docstring), permanently."""
        bisect.insort(self._segments, (started_at, source, text))
        self._render()

    def reset(self):
        """
        Clears the transcript -- call when a new session starts, so the
        pane doesn't keep showing a previous session's transcript with the
        new one appended right after it.
        """
        self._segments = []
        self._render()

    def full_text(self):
        """
        Returns:
            str: every segment transcribed so far this session, one
            source-labeled line per segment (e.g. "You: ...", "Them: ...",
            same shape as _render()'s pane text), in chronological order --
            the full, untrimmed record generate_summary sends to wsl_app
            (Day 23, see docs/DEV_PLAN.md), as opposed to wsl_app's own
            MeetingSession.recent_transcript_segments, which is a bounded
            rolling window sized for live suggestion context, not a full-
            session record. Empty string if nothing's been transcribed yet.
        """
        return "\n".join(
            f"{TRANSCRIPT_SOURCE_LABELS[source]}: {text}" for _started_at, source, text in self._segments
        )

    def _render(self):
        """
        Rewrites the pane's full text from the segment list, one labeled
        line per segment, and scrolls to the end so newly-appended text
        stays visible. "You" lines are right-aligned and "Them" lines
        left-aligned (see TRANSCRIPT_SOURCE_ALIGNMENT), so the two sides of
        a conversation are visually distinct at a glance rather than
        needing to read each line's label. Built block-by-block via a
        QTextCursor rather than a single setPlainText() call, since
        setPlainText() has no per-line formatting of its own and each line
        here needs its own alignment depending on which source it's from.
        """
        self._pane.clear()
        cursor = self._pane.textCursor()
        block_format = QTextBlockFormat()
        for index, (_started_at, source, text) in enumerate(self._segments):
            if index > 0:
                cursor.insertBlock()
            block_format.setAlignment(TRANSCRIPT_SOURCE_ALIGNMENT[source])
            cursor.setBlockFormat(block_format)
            cursor.insertText(f"{TRANSCRIPT_SOURCE_LABELS[source]}: {text}")
        cursor.movePosition(cursor.MoveOperation.End)
        self._pane.setTextCursor(cursor)


class LatestSuggestion(QObject):
    """
    Remembers the most recently received suggestion text, so the "Copy
    Latest Suggestion" button can read it without needing its own
    connection to wsl_app. update() is called from SuggestionDisplay.update
    (Day 18), itself the direct target of WslConnection's cross-thread
    suggestion_received signal -- by the time it reaches here, the call is
    already running on the GUI thread, so this class no longer needs to be
    a QObject for thread-safety on its own account. Left as one anyway:
    it's a trivial, harmless thing to be, and downgrading it to a plain
    class would be a change worth making for its own sake, not one this
    diff should fold in incidentally.
    """

    def __init__(self):
        """Starts with no suggestion received yet."""
        super().__init__()
        self.text = ""

    def update(self, text):
        """Stores `text` as the latest suggestion."""
        self.text = text


class SuggestionDisplay(QObject):
    """
    Fans out one incoming suggestion to everywhere it's shown: the
    Suggestions window's pane, the Copy button's tracker, and the overlay
    (Day 27: answer-only now, see OverlayWindow.update_suggestion -- the
    transcript-excerpt "question" this class still receives on every
    update is no longer displayed anywhere; kept in the method signature
    only because it still arrives on the same suggestion_received signal
    other consumers, e.g. SessionRecorder, still use). A QObject with a
    bound-method slot, not a lambda, for the same cross-thread reason as
    LatestSuggestion above: suggestion_received is emitted from
    WslConnection's background reader thread, and a lambda has no owning
    QObject for Qt to marshal the call through safely (see
    OverlayToggle.toggle for the fuller explanation of that rule).
    """

    def __init__(self, suggestions_pane, latest_suggestion, overlay_window):
        """Stores the three things one incoming suggestion needs to update."""
        super().__init__()
        self._suggestions_pane = suggestions_pane
        self._latest_suggestion = latest_suggestion
        self._overlay_window = overlay_window

    def update(self, question, answer):
        """Updates the Suggestions window's pane and Copy-button tracker, and the overlay, all with the answer text -- question is unused, see class docstring."""
        self._suggestions_pane.setPlainText(answer)
        self._latest_suggestion.update(answer)
        self._overlay_window.update_suggestion(answer)


class SummaryDisplay(QObject):
    """
    Keeps the summary pane, the Generate Summary button, and the Save
    Summary button in sync with wsl_app's summary/summary_failed messages
    (Day 23, PRD §8 Phase 3). A QObject with real bound methods (update()/
    handle_failed()), not lambdas, for the same cross-thread reason as
    TranscriptDisplay/SuggestionDisplay above -- summary_received and
    summary_failed_received are emitted from WslConnection's background
    reader thread (see OverlayToggle.toggle for the fuller explanation of
    that rule).

    Generate Summary's own enabled/disabled state as a function of session
    status (no session running yet, or nothing transcribed) is owned by
    create_session_controls(), not here -- this class only re-enables it
    once a request it disabled (see create_summary_controls()) has actually
    finished, one way or another.
    """

    def __init__(self, pane, generate_button, save_button):
        """Stores the widgets to keep in sync, starting with no summary generated yet."""
        super().__init__()
        self._pane = pane
        self._generate_button = generate_button
        self._save_button = save_button
        self.text = ""

    def update(self, text):
        """Shows a newly generated summary, enables Save Summary, and re-enables Generate Summary now that this request has finished."""
        self.text = text
        self._pane.setPlainText(text)
        self._save_button.setEnabled(True)
        self._generate_button.setEnabled(True)

    def handle_failed(self, reason):
        """Reports a summary generation that failed on the wsl_app side (e.g. the claude CLI process couldn't start), and re-enables Generate Summary so the user can retry."""
        self._generate_button.setEnabled(True)
        QMessageBox.warning(self._pane, "Summary Failed", reason)

    def reset(self):
        """
        Clears the summary pane and text and disables Save Summary -- call
        when a new session starts, so a previous session's summary doesn't
        linger and look like it belongs to the new one.
        """
        self.text = ""
        self._pane.clear()
        self._save_button.setEnabled(False)


class SessionRecorder(QObject):
    """
    Writes an opt-in, plain-text, human-readable log of one session's
    transcript and suggestions to a local file under SESSIONS_DIR (Day 22,
    PRD §5/§9's privacy stance -- RA 4200 anti-wiretapping consent). Off by
    default and no behavior change at all unless the user checks "Save this
    session's transcript to a file" (see create_settings_panel()); raw audio
    is never written here or anywhere else, only the same transcript/
    suggestion text that already reaches this app over the wire.

    record_transcript()/record_suggestion() match
    WslConnection.transcript_received/suggestion_received's own signal
    signatures exactly, so create_session_recording() can connect them
    directly without an intermediate lambda -- consistent with this file's
    rule that a cross-thread signal must reach a bound method, not a lambda
    (see OverlayToggle.toggle). Both are safe no-ops while no file is open,
    so they can stay connected for the app's whole lifetime rather than
    being wired/unwired per session.

    Each line is flushed to disk as it's written, not buffered up to be
    written at the end, so a crash mid-session doesn't lose an otherwise-
    complete transcript.
    """

    def __init__(self):
        """Starts with no file open -- start() opens one when a recorded session begins."""
        super().__init__()
        self._file = None

    def start(self, mode_label):
        """Opens a new timestamped file under SESSIONS_DIR and writes its header line."""
        SESSIONS_DIR.mkdir(exist_ok=True)
        started_at = time.localtime()
        filename = f"session_{time.strftime('%Y-%m-%d_%H%M%S', started_at)}.txt"
        self._file = open(SESSIONS_DIR / filename, "w", encoding="utf-8")
        self._write(f"=== Adarna session started {time.strftime('%Y-%m-%d %H:%M:%S', started_at)} (mode: {mode_label}) ===")

    def stop(self):
        """Writes a footer line and closes the file, if one is open. A safe no-op otherwise."""
        if self._file is None:
            return
        self._write(f"=== Session ended {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        self._file.close()
        self._file = None

    def record_transcript(self, source, text, started_at):
        """Appends one source-labeled, timestamped transcript line, if a session is currently being recorded."""
        if self._file is None:
            return
        timestamp = time.strftime("%H:%M:%S", time.localtime(started_at)) if started_at else time.strftime("%H:%M:%S")
        self._write(f"[{timestamp}] {TRANSCRIPT_SOURCE_LABELS[source]}: {text}")

    def record_suggestion(self, question, answer):
        """Appends one timestamped suggestion (with the transcript excerpt that prompted it, if any), if a session is currently being recorded."""
        if self._file is None:
            return
        timestamp = time.strftime("%H:%M:%S")
        label = f'Suggestion (re: "{question}")' if question else "Suggestion"
        self._write(f"[{timestamp}] {label}: {answer}")

    def _write(self, line):
        """Writes one line to the open file and flushes immediately, so partial content already on disk survives a crash."""
        self._file.write(line + "\n")
        self._file.flush()


class OverlayToggle(QObject):
    """
    Lets the global show/hide hotkey (fired from pynput's own listener
    thread) request the overlay window's visibility be flipped, without
    touching a Qt widget off the GUI thread directly -- Qt widgets may only
    be shown/hidden from the thread that owns them. A QObject for the same
    reason LatestSuggestion above is one (see that class's docstring); see
    toggle()'s own docstring for the separate reason it's connected to a
    real bound method rather than a lambda.
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


class _DragHandle(QWidget):
    """
    A slim strip docked at the top of the overlay, dedicated to starting/
    updating/ending a window drag -- the only way to move the overlay, as
    of the Day 18 redesign. Forwards its own mouse events straight to
    `overlay_window`'s mousePressEvent/mouseMoveEvent/mouseReleaseEvent
    (safe to call directly: those methods only read event.globalPosition()/
    event.button()/event.buttons(), which don't depend on which widget
    actually received the event) rather than duplicating the drag math
    here.

    Exists because the first Day 18 attempt tried to make the *entire*
    panel draggable, including on top of the scrollable question/answer
    text, by cascading Qt.WA_TransparentForMouseEvents down through the
    QScrollArea, its viewport, the content widget, and both labels so
    clicks would "pass through" to this window's own mousePressEvent.
    Live testing found that didn't work at all -- neither dragging nor
    scrolling did anything. That matches a long-documented Qt quirk
    (QTBUG-8431: WA_TransparentForMouseEvents is checked too early in
    QWidgetPrivate::childAt_helper and doesn't reliably keep working once
    it's cascaded through several nested widgets) rather than one
    particular bug in this file's version of the pattern -- multiple
    Qt Forum threads report the same "only works one level deep, not
    reliably through nested children" behavior. A dedicated drag handle
    sidesteps the whole class of problem: dragging is its own widget,
    fully separate from the answer text underneath (which, since Day 30,
    doesn't scroll at all any more -- see _resize_to_fit_content()). The
    trade-off -- the panel is now draggable only from this strip and its
    margins, not from anywhere you click -- is the standard pattern for
    overlay/HUD-style windows anyway.
    """

    def __init__(self, overlay_window):
        """Stores the window this handle drags and gives itself a fixed height and a visible grip glyph."""
        super().__init__()
        self._overlay_window = overlay_window
        self.setFixedHeight(18)
        self.setCursor(Qt.SizeAllCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        grip_label = QLabel("⋯")  # ⋯
        grip_label.setAlignment(Qt.AlignCenter)
        grip_label.setStyleSheet("color: rgba(255, 255, 255, 90); font-size: 12px;")
        grip_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(grip_label)

    def mousePressEvent(self, event):
        """Forwards to overlay_window.mousePressEvent() to start a drag."""
        self._overlay_window.mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Forwards to overlay_window.mouseMoveEvent() to continue a drag in progress."""
        self._overlay_window.mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """Forwards to overlay_window.mouseReleaseEvent() to end a drag."""
        self._overlay_window.mouseReleaseEvent(event)


class OverlayWindow(QWidget):
    """
    The always-on-top overlay: frameless, stays above other windows, and
    shows the latest suggested answer -- per PRD §8 Phase 2's Day 18 visual
    redesign, continued Day 27: a black, semi-transparent, rounded panel
    with bold white text and a static ⭐️ marker (no transcript excerpt/
    "question" any more -- dropped per direct user feedback that it was
    just noise once you're glancing at this live during a call; no
    settings, no session controls either -- those all stay on the main
    window). Starts hidden; create_overlay_toggle() wires up the hotkey
    that shows it. Dragged only via the _DragHandle strip docked at its
    top (see that class's docstring for why).

    No scrollbar (removed Day 30, per direct user feedback that it wasn't
    wanted): the window's own height instead always grows or shrinks to
    exactly fit the current suggestion at whatever width the user has
    dragged it to, so the black panel painted in paintEvent() visibly
    "follows" the length of the text rather than clipping it or making it
    scrollable -- see _resize_to_fit_content().

    A real class (not a plain QWidget built by a factory function, like
    every other widget in this file) because dragging and the rounded/
    translucent look both need virtual methods overridden
    (mousePressEvent/mouseMoveEvent/mouseReleaseEvent, paintEvent) -- Qt's
    normal way of doing this is a subclass, not event wiring bolted onto a
    generic QWidget.

    Day 27: the rounded black panel is now painted directly in
    paintEvent() (see that method) instead of through a QSS stylesheet +
    WA_StyledBackground -- confirmed live on the real Windows machine that
    the stylesheet approach, despite being the textbook-documented way to
    give a plain QWidget a styled background, never actually painted
    anything: the panel was fully see-through at every opacity setting,
    with only the text responding to the slider. Rather than keep
    debugging exactly which of three interacting mechanisms (QSS,
    WA_StyledBackground, setWindowOpacity()) was misbehaving on this
    machine without being able to see it directly, this removes all three
    in favor of one direct, fully-controlled QPainter fill -- see
    paintEvent() and set_opacity().

    WA_QuitOnClose is turned off specifically so this window doesn't
    count toward Qt's "quit once every counted window is closed" check --
    without this, closing the main window while the overlay happens to
    still be visible would leave the app running invisibly, since the
    overlay would still be an open, counted window.
    """

    def __init__(self):
        """Builds the frameless, translucent, rounded overlay panel and its answer label, starting hidden with no drag in progress."""
        super().__init__()
        self.setWindowTitle("Adarna Overlay")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_QuitOnClose, False)
        # Makes the window surface support real alpha at all, so the
        # corners outside the rounded rect paintEvent() draws are truly
        # see-through rather than an opaque black/system-colored box --
        # still needed even though the background itself is now hand-
        # painted rather than QSS-styled (see class docstring).
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(260, 140)
        self._drag_offset = None
        # Guards against _resize_to_fit_content()'s own self.resize() call
        # re-entering resizeEvent() -- see that method's docstring.
        self._fitting_content = False
        # Multiplies OVERLAY_BACKGROUND_RGBA/BORDER_RGBA's own alpha in
        # paintEvent() -- see set_opacity(). Text is deliberately NOT
        # affected by this (see that method's docstring): an assistive
        # overlay you're glancing at live during a call should never fade
        # into illegibility just because the panel itself was made more
        # see-through.
        self._opacity = OVERLAY_OPACITY_DEFAULT_PERCENT / 100

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(16, 8, 16, 10)
        outer_layout.setSpacing(4)

        outer_layout.addWidget(_DragHandle(self))
        outer_layout.addWidget(self._build_answer_section())

        grip_row = QHBoxLayout()
        grip_row.addStretch()
        grip = QSizeGrip(self)
        grip.setStyleSheet("background: transparent;")
        grip_row.addWidget(grip)
        outer_layout.addLayout(grip_row)

        # Sized last -- resize() fires resizeEvent() immediately, which
        # calls _resize_to_fit_content() (see that method's docstring),
        # so the layout above needs to already exist.
        self.resize(440, 240)

    def _build_answer_section(self):
        """
        Builds the plain (non-scrolling) content area holding the answer
        section: a marker caption plus the word-wrapped answer label.
        Nothing here scrolls or clips -- see _resize_to_fit_content() for
        how the window's own height is kept matching whatever this needs
        to show in full.

        Returns:
            QWidget: ready to add to the window's layout.
        """
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(2)

        self._answer_label = self._add_labeled_section(content_layout, OVERLAY_ANSWER_MARKER)
        return content

    def _add_labeled_section(self, layout, marker_text):
        """
        Adds the static ⭐️ section marker (per PRD §8's Day 18 revision --
        an icon this app draws itself, never model output) plus a
        word-wrapped text label beneath it to `layout`. Only one section
        now (Day 27 -- the "question"/transcript-excerpt section was
        dropped), but kept as its own method rather than inlined into
        _build_answer_section(), since a second section is a plausible
        future addition and the marker+label pairing is a distinct enough
        unit to stay named.

        Returns:
            QLabel: the (initially empty) text label to keep updated.
        """
        caption = QLabel(marker_text)
        # Explicit color, not just relying on the emoji's own color glyph --
        # a font/platform that only has a monochrome fallback for ⭐️ would
        # otherwise inherit QLabel's default palette color instead of a
        # guaranteed-visible one against the near-black panel. Found by
        # /code-review, Day 18.
        caption.setStyleSheet(f"color: {OVERLAY_TEXT_COLOR}; font-size: 14px;")
        layout.addWidget(caption)

        text_label = QLabel("")
        # Live transcript text and claude's own output are both arbitrary
        # content this app doesn't control -- forced to PlainText (rather
        # than the QLabel default of AutoText, which sniffs content and
        # renders anything that looks like HTML as markup) so a stray
        # "<" from spoken text or a pasted URL can never get silently
        # parsed/dropped instead of shown verbatim. Found by /code-review,
        # Day 18.
        text_label.setTextFormat(Qt.PlainText)
        text_label.setWordWrap(True)
        # Fully opaque regardless of the overlay's own opacity slider --
        # see set_opacity()'s docstring for why text deliberately doesn't
        # fade.
        text_label.setStyleSheet(f"color: {OVERLAY_TEXT_COLOR}; font-size: 13px; font-weight: 700;")
        layout.addWidget(text_label)
        return text_label

    def update_suggestion(self, answer):
        """Updates the overlay's answer text to a newly received suggestion, then re-fits the window's height to it -- see _resize_to_fit_content()."""
        self._answer_label.setText(answer)
        self._resize_to_fit_content()

    def set_opacity(self, opacity):
        """
        Sets how see-through the overlay's black background panel is
        (0.0-1.0, scaling OVERLAY_BACKGROUND_RGBA/BORDER_RGBA's own alpha
        -- see create_overlay_controls()'s slider), then repaints.

        Day 27: deliberately does NOT touch text opacity at all, a change
        from the pre-Day-27 behavior (that used QWidget.setWindowOpacity(),
        which faded the whole composited window uniformly, text included).
        An assistive overlay you're glancing at live during a call should
        stay fully legible no matter how transparent you've made the panel
        behind it -- fading the text along with the background was never
        something anyone asked for, just a side effect of how opacity used
        to be implemented, and it's what made the underlying background
        bug (see class docstring) easy to misread as "opacity does
        something, just not what it's supposed to."
        """
        self._opacity = opacity
        self.update()

    def paintEvent(self, event):
        """
        Paints the rounded, translucent black panel and its border
        directly, in place of the QSS-stylesheet approach every other
        widget in this file uses (see class docstring for why: confirmed
        live on the real Windows machine that the stylesheet background
        never actually rendered, at any opacity setting). self._opacity
        (see set_opacity()) scales OVERLAY_BACKGROUND_RGBA/BORDER_RGBA's
        own alpha component -- text is untouched by it, painted separately
        by the QLabels in _add_labeled_section() at full opacity always.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        background_r, background_g, background_b, background_a = OVERLAY_BACKGROUND_RGBA
        border_r, border_g, border_b, border_a = OVERLAY_BORDER_RGBA
        painter.setBrush(QColor(background_r, background_g, background_b, int(background_a * self._opacity)))
        painter.setPen(QPen(QColor(border_r, border_g, border_b, int(border_a * self._opacity)), 1))
        # -1 on the right/bottom so the 1px border is fully inside the
        # widget's own bounds rather than half-clipped at the edge.
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 14, 14)

    def set_click_through(self, enabled):
        """
        Toggles click-through mode: while enabled, mouse events (including
        drag and the resize grip) pass straight through the overlay to
        whatever's underneath it instead of reaching this window at all,
        via Qt.WindowTransparentForInput. Mutually exclusive with
        dragging/resizing by construction -- there's no separate flag to
        turn those off, since a window that isn't receiving mouse events
        can't act on them either way. Changing a window's flags hides it
        on most platforms, so this re-shows it afterward, but only if it
        was actually visible beforehand -- toggling click-through while
        the overlay is hidden (via the show/hide hotkey) shouldn't force
        it to appear.
        """
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowTransparentForInput, enabled)
        if was_visible:
            self.show()

    def mousePressEvent(self, event):
        """Starts a drag if the left button was pressed, recording the click's offset from the window's current position."""
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        """While a drag is in progress, moves the window so it stays under the cursor at the same offset recorded in mousePressEvent."""
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        """
        Ends the current drag, if any -- only on releasing the left button,
        the one that can start a drag (see mousePressEvent). Checked
        because a release event fires for any button, not just the one
        that started the drag: without this check, releasing an unrelated
        button (e.g. a touchpad's right-click) while still holding the
        left button down mid-drag would end the drag early, even though
        the left button -- the one mouseMoveEvent actually watches -- was
        never released. Found by /code-review, Day 18.
        """
        if event.button() != Qt.LeftButton:
            return
        self._drag_offset = None

    def resizeEvent(self, event):
        """
        Whenever the window's width changes (drag-resize via the grip, or
        the initial show), re-fits the window's height to the current
        answer text at that new width -- see _resize_to_fit_content() for
        why height always follows content now that there's no scrollbar.
        """
        super().resizeEvent(event)
        if not self._fitting_content:
            self._resize_to_fit_content()

    def _resize_to_fit_content(self):
        """
        Grows or shrinks the window so its height exactly fits the answer
        text at the window's current width -- no scrollbar, no clipped
        text. Replaces the earlier QScrollArea-based approach (removed
        Day 30) per direct user feedback that the scrollbar wasn't
        wanted: the black panel painted in paintEvent() just follows
        whatever size this method settles on, so a long suggestion visibly
        grows the panel instead of becoming scrollable.

        Measures the wrapped answer text's own height directly with
        QFontMetrics (no width-dependent caching quirks, confirmed by a
        throwaway probe script against the real running overlay -- unlike
        QLayout.sizeHint(), which is reliable for everything else here but
        specifically lags one call behind for a size-hint change coming
        purely from a child's setFixedHeight(), see below), then adds that
        to a freshly-measured "everything except the answer label" chrome
        height (drag handle, caption, margins, spacing, grip row) to get
        the window's true target height.

        Clamped to the current screen's available height so one very long
        suggestion can't grow the overlay taller than the screen; unlike
        the old scroll-free attempt this replaces, there's no stuck state
        if the clamp kicks in -- the next suggestion re-fits normally.

        self._fitting_content guards the self.resize() call below from
        re-entering resizeEvent(), which calls this method again.
        """
        margins = self.layout().contentsMargins()
        content_width = max(self.width() - margins.left() - margins.right(), 0)

        # Measures the layout's currently-settled total height and the
        # answer label's currently-settled height BEFORE changing
        # anything below, giving "everything except the answer label" as
        # a height, valid right now since nothing's been mutated yet this
        # call. Reading self.layout().sizeHint() AFTER changing the
        # label's own setFixedHeight() instead hits a real Qt staleness
        # bug, confirmed directly (a throwaway probe script against the
        # real running overlay): it lags exactly one call behind, so a
        # longer suggestion arriving right after a shorter one measured
        # as if it were still the shorter one's height. Doing the
        # subtraction up front, before mutating the label, sidesteps that
        # entirely -- self._answer_label.height() here is the label's
        # actual current geometry, not a cached hint, so it's always
        # accurate.
        self.layout().activate()
        chrome_height = self.layout().sizeHint().height() - self._answer_label.height()

        metrics = QFontMetrics(self._answer_label.font())
        wrapped_rect = metrics.boundingRect(
            0, 0, content_width, 0, Qt.TextWordWrap, self._answer_label.text()
        )
        self._answer_label.setFixedWidth(content_width)
        self._answer_label.setFixedHeight(wrapped_rect.height())

        target_height = chrome_height + wrapped_rect.height()
        screen = self.screen()
        if screen is not None:
            target_height = min(target_height, screen.availableGeometry().height())
        self._fitting_content = True
        try:
            self.resize(self.width(), target_height)
        finally:
            self._fitting_content = False


def create_overlay_window():
    """
    Creates the overlay window.

    Returns:
        OverlayWindow: the overlay, ready to be shown by create_overlay_toggle()'s hotkey.
    """
    return OverlayWindow()


def create_overlay_controls(layout, overlay_window):
    """
    Adds an "Overlay" settings group to the main window with an opacity
    slider and a click-through checkbox -- both apply live, immediately,
    regardless of whether a session is running, unlike "Session Settings"
    above (which is only read once when Start Session is pressed), so
    this lives in its own group rather than inside that one.
    """
    group = QGroupBox("Overlay")
    form = QFormLayout(group)

    opacity_row = QHBoxLayout()
    opacity_slider = QSlider(Qt.Horizontal)
    opacity_slider.setRange(OVERLAY_OPACITY_MIN_PERCENT, OVERLAY_OPACITY_MAX_PERCENT)
    opacity_slider.setValue(OVERLAY_OPACITY_DEFAULT_PERCENT)
    opacity_value_label = QLabel(f"{OVERLAY_OPACITY_DEFAULT_PERCENT}%")

    def handle_opacity_changed(percent):
        """Applies a new opacity slider value to the overlay and updates the percentage label next to it."""
        overlay_window.set_opacity(percent / 100)
        opacity_value_label.setText(f"{percent}%")

    opacity_slider.valueChanged.connect(handle_opacity_changed)
    overlay_window.set_opacity(OVERLAY_OPACITY_DEFAULT_PERCENT / 100)
    opacity_row.addWidget(opacity_slider)
    opacity_row.addWidget(opacity_value_label)
    form.addRow("Opacity:", opacity_row)

    click_through_checkbox = QCheckBox("Click-through (mouse passes to the window underneath)")
    click_through_checkbox.toggled.connect(overlay_window.set_click_through)
    form.addRow(click_through_checkbox)

    layout.addWidget(group)


def create_transcript_display(transcript_pane):
    """
    Creates the TranscriptDisplay that keeps transcript_pane in sync with
    incoming transcript messages.

    Returns:
        TranscriptDisplay: connect wsl_connection.transcript_received to
        its update() method (see connect_incoming_messages_to_ui()).
    """
    return TranscriptDisplay(transcript_pane)


def create_overlay_toggle(overlay_window):
    """
    Creates the OverlayToggle that the show/hide hotkey uses to flip
    overlay_window's visibility on the GUI thread.

    Returns:
        OverlayToggle: emit its toggle_requested signal (safe from any
        thread) to show the overlay if hidden, or hide it if shown.
    """
    return OverlayToggle(overlay_window)


def create_suggestions_window():
    """
    Creates a separate, ordinary top-level window dedicated to showing the
    latest suggestion — not part of the main window's layout at all (Day
    27, direct user request: the main window stacks connection status,
    both device pickers, session settings, overlay controls, and the
    transcript pane all in one non-scrolling column above it, squeezing
    the actual suggestion text down to a couple of visible lines). A
    normal, independently resizable/movable window — unlike OverlayWindow,
    nothing frameless, translucent, or click-through here — so it can be
    made as large as needed, or moved to a second monitor, without
    fighting the main window's layout at all. A bigger font than the main
    window's other panes, since readability at a glance is the entire
    point of pulling this out on its own.

    Same content as the old inline section it replaces: a read-only pane
    that shows the latest suggestion (a new one replaces whatever was
    shown before, not appended to a growing list) plus a "Copy Latest
    Suggestion" button.

    WA_QuitOnClose is turned off, same reasoning as OverlayWindow's own
    (see that class's docstring): without it, closing the main window
    while this one happens to still be open would leave the app running
    orphaned -- no main window, no session controls, nothing usable left
    -- instead of quitting cleanly.

    Returns:
        tuple[QWidget, QPlainTextEdit, LatestSuggestion]: the window
        itself (call .show() on it — see main()), the pane to set new
        suggestion text on, and the tracker the Copy button reads from —
        the caller should also connect this pane to whatever emits new
        suggestion text (see create_suggestion_display()).
    """
    window = QWidget()
    window.setWindowTitle("Adarna Suggestions")
    window.setAttribute(Qt.WA_QuitOnClose, False)
    window.resize(640, 520)
    layout = QVBoxLayout(window)

    pane = create_readonly_text_pane(layout)
    pane.setStyleSheet("font-size: 15px;")

    latest_suggestion = LatestSuggestion()

    def copy_latest_suggestion():
        """Copies the most recently received suggestion text to the clipboard."""
        if latest_suggestion.text:
            QApplication.clipboard().setText(latest_suggestion.text)

    copy_button = QPushButton("Copy Latest Suggestion")
    copy_button.clicked.connect(copy_latest_suggestion)
    layout.addWidget(copy_button)

    return window, pane, latest_suggestion


def create_summary_section(layout):
    """
    Adds a labeled, read-only pane that shows the post-meeting summary once
    generated (Day 23, PRD §8 Phase 3), plus "Generate Summary" and "Save
    Summary" buttons. Both start disabled: Generate Summary has nothing to
    summarize until a session has actually run and ended (see
    create_session_controls(), which owns enabling/disabling it based on
    session state), and Save Summary has nothing to save until a summary
    has actually arrived (see SummaryDisplay.update()).

    Returns:
        tuple[QPlainTextEdit, QPushButton, QPushButton]: the pane summary
        text is shown in, the Generate Summary button, and the Save
        Summary button.
    """
    layout.addWidget(QLabel("Summary"))
    pane = create_readonly_text_pane(layout)

    row = QHBoxLayout()
    generate_button = QPushButton("Generate Summary")
    generate_button.setEnabled(False)
    save_button = QPushButton("Save Summary")
    save_button.setEnabled(False)
    row.addWidget(generate_button)
    row.addWidget(save_button)
    layout.addLayout(row)

    return pane, generate_button, save_button


def create_session_recorder():
    """
    Creates the SessionRecorder that optionally saves a session's transcript
    and suggestions to disk (Day 22).

    Returns:
        SessionRecorder: call .start()/.stop() from session start/stop (see
        create_session_controls()); connect wsl_connection's
        transcript_received/suggestion_received to its record_transcript()/
        record_suggestion() methods (see connect_session_recorder()).
    """
    return SessionRecorder()


def connect_session_recorder(wsl_connection, session_recorder):
    """
    Wires wsl_app's incoming transcript/suggestion messages to
    session_recorder, so they're saved to disk whenever a session is
    actively being recorded. Kept connected for the app's whole lifetime,
    same as connect_incoming_messages_to_ui() -- record_transcript()/
    record_suggestion() are safe no-ops while no session is being recorded.
    """
    wsl_connection.transcript_received.connect(session_recorder.record_transcript)
    wsl_connection.suggestion_received.connect(session_recorder.record_suggestion)


def create_suggestion_display(suggestions_pane, latest_suggestion, overlay_window):
    """
    Creates the SuggestionDisplay that fans out one incoming suggestion to
    the Suggestions window's pane, the Copy button's tracker, and the
    overlay.

    Returns:
        SuggestionDisplay: connect wsl_connection.suggestion_received to
        its update() method (see connect_incoming_messages_to_ui()).
    """
    return SuggestionDisplay(suggestions_pane, latest_suggestion, overlay_window)


def create_summary_display(summary_pane, generate_button, save_button):
    """
    Creates the SummaryDisplay that keeps summary_pane and the Generate/Save
    Summary buttons in sync with wsl_app's summary/summary_failed messages.

    Returns:
        SummaryDisplay: connect wsl_connection.summary_received to its
        update() method, and summary_failed_received to handle_failed()
        (see connect_incoming_messages_to_ui()).
    """
    return SummaryDisplay(summary_pane, generate_button, save_button)


def create_summary_controls(wsl_connection, transcript_display, summary_display, generate_button, save_button, window):
    """
    Wires the Generate Summary and Save Summary buttons.

    Generate sends wsl_app the full in-session transcript (see
    TranscriptDisplay.full_text -- the whole session, not wsl_app's own
    trimmed rolling suggestion context, see docs/DEV_PLAN.md Day 23) as a
    generate_summary message, and disables itself until SummaryDisplay
    reports the request has finished (summary or summary_failed -- see
    SummaryDisplay.update()/handle_failed()), so two rapid clicks can't fire
    two overlapping requests. If the wsl_app connection drops while a
    request is in flight, summary/summary_failed will never arrive to
    re-enable it on its own, so this also re-enables it on disconnect
    (rather than leaving the button stuck disabled) whenever there's still
    a transcript to retry with.

    Save opens a file dialog and writes whatever text is currently in the
    summary pane to disk in one shot (Day 23) -- unlike SessionRecorder's
    live-append transcript recording, a summary is generated once, in full,
    so a plain single write is enough; no need to route it through
    SessionRecorder's incremental-flush machinery.
    """

    def generate_summary():
        """Sends wsl_app the full transcript and asks it to generate a summary, disabling the button until a response arrives."""
        generate_button.setEnabled(False)
        wsl_connection.send_message({"type": "generate_summary", "transcript": transcript_display.full_text()})

    def save_summary():
        """Opens a save dialog and writes the current summary text to the chosen file."""
        if not summary_display.text:
            return
        default_name = f"summary_{time.strftime('%Y-%m-%d_%H%M%S')}.md"
        path, _selected_filter = QFileDialog.getSaveFileName(
            window, "Save Summary", str(SESSIONS_DIR / default_name), "Markdown/Text Files (*.md *.txt);;All Files (*)"
        )
        if not path:
            return
        Path(path).write_text(summary_display.text, encoding="utf-8")

    def handle_connection_dropped_while_generating(connected):
        """Re-enables Generate Summary if the connection drops while a request might be in flight, so the user isn't stuck unable to retry."""
        if connected or generate_button.isEnabled():
            return
        if transcript_display.full_text():
            generate_button.setEnabled(True)

    generate_button.clicked.connect(generate_summary)
    save_button.clicked.connect(save_summary)
    wsl_connection.connection_changed.connect(handle_connection_dropped_while_generating)


def create_suggestion_trigger_controls(layout):
    """
    Adds a row with the "Auto-suggest on pause" checkbox (unchecked by
    default — Phase 3, user preference: suggestions should only appear when
    asked for, via this checkbox or the manual button/hotkey, not fire on
    every pause unless the user opts in) and a "Generate Suggestion Now"
    button. Unlike the settings panel above, the checkbox is meant to be
    flipped live during a running session — see create_session_controls()
    and wsl_app's SuggestionTrigger — so it lives outside "Session Settings"
    rather than inside it. The button gives manual triggering an in-window
    equivalent of the global hotkey, for anyone who'd rather click than
    reach for a key combination.

    Returns:
        tuple[QCheckBox, QPushButton]: the auto-suggest checkbox and the
        manual-trigger button.
    """
    row = QHBoxLayout()
    auto_suggest_checkbox = QCheckBox("Auto-suggest on pause")
    auto_suggest_checkbox.setChecked(False)
    generate_button = QPushButton("Generate Suggestion Now")
    row.addWidget(auto_suggest_checkbox)
    row.addWidget(generate_button)
    layout.addLayout(row)
    return auto_suggest_checkbox, generate_button


def create_settings_panel(layout):
    """
    Adds a labeled settings panel with the mode, suggestion pause delay,
    and pre-session context notes controls used to configure the next
    session. These values are only read (and sent to wsl_app) when Start
    Session is pressed — see create_session_controls() — so changing them
    mid-session has no effect until the next session starts.

    No Whisper model size control here anymore (Day 17): the live
    streaming transcript path is structurally tied to base.en (see
    wsl_app/streaming_transcriber.py) -- small.en's own per-call decode
    floor is already slower than the cadence streaming needs, so there's
    no real choice left to expose, and wsl_app no longer reads a
    whisper_model_size setting at all.

    Also includes the "Save this session's transcript to a file" checkbox
    (Day 22, unchecked by default) that opts a session into local, on-disk
    recording via SessionRecorder, and the "Enable live-agent-listening
    export" checkbox (unchecked by default) that opts a session into
    wsl_app writing its own separate, tailable log for a manually-started
    live-agent-listening session (see docs/LIVE_AGENT_LISTENING.md) --
    unlike the transcript checkbox, this one crosses the wire (see
    current_settings_message()), since the file it controls lives on the
    wsl_app side, not here. Both live here, not their own group, since they
    share this panel's rule of being read once at Start Session rather than
    live-toggleable mid-session, same as mode/pause/context notes.

    Returns:
        tuple[QComboBox, QComboBox, QPlainTextEdit, QCheckBox, QCheckBox]:
        the mode and suggestion pause dropdowns, the context notes text
        box, the save-transcript checkbox, and the live-agent-listening
        export checkbox, in that order.
    """
    group = QGroupBox("Session Settings")
    form = QFormLayout(group)

    mode_dropdown = QComboBox()
    for label in MODE_LABELS_TO_VALUES:
        mode_dropdown.addItem(label)
    form.addRow("Mode:", mode_dropdown)

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

    save_transcript_checkbox = QCheckBox("Save this session's transcript to a file")
    save_transcript_checkbox.setChecked(False)
    form.addRow(save_transcript_checkbox)

    live_agent_export_checkbox = QCheckBox(
        "Enable live-agent-listening export (writes a tailable log for a separate claude session)"
    )
    live_agent_export_checkbox.setChecked(False)
    form.addRow(live_agent_export_checkbox)

    layout.addWidget(group)
    return mode_dropdown, pause_dropdown, context_notes_edit, save_transcript_checkbox, live_agent_export_checkbox


def current_settings_message(
    mode_dropdown, pause_dropdown, context_notes_edit, auto_suggest_checkbox, live_agent_export_checkbox
):
    """
    Reads the settings panel's current values and packages them into the
    settings_changed message to send wsl_app.

    Returns:
        dict: the settings_changed message.
    """
    return {
        "type": "settings_changed",
        "mode": MODE_LABELS_TO_VALUES[mode_dropdown.currentText()],
        "suggestion_pause_seconds": pause_dropdown.currentData(),
        "context_notes": context_notes_edit.toPlainText().strip(),
        "live_agent_export_enabled": live_agent_export_checkbox.isChecked(),
        "auto_suggest_enabled": auto_suggest_checkbox.isChecked(),
    }


def list_loopback_devices(audio):
    """
    Lists every WASAPI loopback-capable device available for capture --
    each one captures system audio (everyone else's side of a call), not a
    real microphone. See list_mic_devices() for the other source Day 19
    added.

    Returns:
        list[dict]: pyaudiowpatch device info dicts, one per loopback device.
    """
    return list(audio.get_loopback_device_info_generator())


def list_mic_devices(audio):
    """
    Lists every real microphone/input device available for capture (Day
    19) -- every input-capable device except WASAPI loopback devices.
    Loopback devices also report maxInputChannels > 0 (that's how
    pyaudiowpatch exposes "capture what's currently playing" at all), but
    each one represents a system output being captured as input, not
    someone's actual microphone, so they're excluded here and listed
    separately by list_loopback_devices() instead -- verified against the
    installed pyaudiowpatch that every device dict carries its own
    isLoopbackDevice flag, which is exactly the distinction needed.

    Returns:
        list[dict]: pyaudiowpatch device info dicts, one per real
        microphone/input device.
    """
    devices = []
    for index in range(audio.get_device_count()):
        device = audio.get_device_info_by_index(index)
        if device["maxInputChannels"] > 0 and not device.get("isLoopbackDevice", False):
            devices.append(device)
    return devices


def populate_device_dropdown(dropdown, devices, default_device):
    """Fills the dropdown with device names, preselecting the system default."""
    for device in devices:
        dropdown.addItem(device["name"], userData=device)
    default_index = next(
        (i for i, device in enumerate(devices) if device["index"] == default_device["index"]), 0
    )
    dropdown.setCurrentIndex(default_index)


def get_default_mic_device(audio, mic_devices):
    """
    Finds the system's default microphone, if it has one it can actually
    use (Day 19). Unlike loopback -- every machine that can run this app
    has a default output device to loop back -- a working default input
    device isn't something to assume: pyaudiowpatch's
    get_default_input_device_info() raises OSError on a machine with no
    microphone plugged in, or whose default recording device is disabled
    in Windows -- a real, unexceptional case this app shouldn't crash on
    startup over, since mic capture just isn't available there. Falls back
    to the first device list_mic_devices() found, if any, so a machine
    with a real mic that simply isn't set as "default" still gets one
    preselected rather than being treated the same as having none at all.

    Returns:
        dict | None: the mic device to preselect, or None if this machine
        has no usable microphone at all -- main() disables mic capture
        entirely in that case rather than failing to start.
    """
    try:
        return audio.get_default_input_device_info()
    except OSError:
        return mic_devices[0] if mic_devices else None


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


def connect_incoming_messages_to_ui(wsl_connection, transcript_display, suggestion_display, summary_display):
    """
    Wires wsl_app's incoming transcript/suggestion/summary messages to the
    UI: each transcript message appends one finished sentence to the
    transcript pane via transcript_display (see TranscriptDisplay), each
    suggestion is fanned out by suggestion_display to the main pane, the
    Copy button's tracker, and the overlay's question/answer labels (see
    SuggestionDisplay), and each summary (or summary_failed) is handled by
    summary_display (Day 23, see SummaryDisplay).
    """
    wsl_connection.transcript_received.connect(transcript_display.update)
    wsl_connection.suggestion_received.connect(suggestion_display.update)
    wsl_connection.summary_received.connect(summary_display.update)
    wsl_connection.summary_failed_received.connect(summary_display.handle_failed)


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


def create_audio_capture_manager(audio, wsl_connection, source, device_dropdown, default_device, capture_status_label):
    """
    Creates the audio capture manager for one audio source (`source` --
    Day 19: AUDIO_SOURCE_MIC or AUDIO_SOURCE_LOOPBACK): preselects the
    default device, restarts capture on a new device whenever the dropdown
    changes (if a session is running), and keeps capture_status_label in
    sync. Capture itself only starts/stops via start_session()/
    stop_session() — see create_session_controls(), which now starts/stops
    both this and the other source's manager together.

    Returns:
        AudioCaptureManager: the manager driving the capture thread.
    """
    capture_manager = AudioCaptureManager(audio, wsl_connection, source)
    capture_manager.status_changed.connect(capture_status_label.setText)
    capture_manager.set_device(default_device)
    device_dropdown.currentIndexChanged.connect(
        lambda index: capture_manager.set_device(device_dropdown.itemData(index))
    )
    return capture_manager


def session_recording_window_title(save_transcript_active, live_agent_export_active):
    """
    Builds the window title from whichever opt-in disk-writing features are
    active for the current session -- DEFAULT_WINDOW_TITLE if neither is,
    otherwise DEFAULT_WINDOW_TITLE with each active feature named, so it's
    never silently invisible that something is being written to disk (see
    DEFAULT_WINDOW_TITLE's own comment). Composed from a list rather than a
    fixed set of title constants (the old RECORDING_WINDOW_TITLE) so a
    future third disk-writing opt-in doesn't need its own combinatorial set
    of title strings.

    Returns:
        str: the window title to set.
    """
    reasons = []
    if save_transcript_active:
        reasons.append("saving session to disk")
    if live_agent_export_active:
        reasons.append("live-agent export active")
    if not reasons:
        return DEFAULT_WINDOW_TITLE
    return f"Adarna ({', '.join(reasons)})"


def create_session_controls(
    wsl_connection,
    capture_managers,
    mic_capture_manager,
    mic_enabled_checkbox,
    mode_dropdown,
    pause_dropdown,
    context_notes_edit,
    auto_suggest_checkbox,
    save_transcript_checkbox,
    live_agent_export_checkbox,
    session_recorder,
    transcript_display,
    summary_display,
    generate_summary_button,
    layout,
    window,
):
    """
    Adds the Start Session / Stop Session buttons and wires them up:
    starting clears the transcript pane (see TranscriptDisplay.reset —
    Day 17: otherwise a new session's transcript would appear appended
    right after whatever the previous one left on screen), sends the
    settings panel's current values (including the context notes, the
    auto-suggest checkbox's starting state, and the live-agent-listening
    export checkbox's starting state) as settings_changed, then
    session_started (tagged with a fresh attempt_id — see
    handle_session_start_failed), starts capture on every manager in
    `capture_managers` (Day 19: one for mic, one for loopback — each is
    already its own independent capture thread, so starting/stopping both
    here just means neither has to wait on the other), starts
    session_recorder if save_transcript_checkbox is checked (Day 22), and
    sets the window title via session_recording_window_title() to reflect
    whichever of that and live_agent_export_checkbox are on -- wsl_app
    owns the live-agent-listening log itself (see docs/LIVE_AGENT_LISTENING.md),
    this side just sends the flag and shows the same kind of visible
    indicator Day 22 already established; stopping sends session_stopped,
    stops every manager, stops session_recorder (a safe no-op if it was
    never started), and restores the window title. Button enabled-state
    tracks which action is currently valid.

    Also handles three ways a session can end itself, all reverting to the
    same clean pre-session UI state stop_session() reaches (see
    end_session()): wsl_app reporting it couldn't start the session at all
    (session_start_failed — e.g. the claude CLI process failing to start),
    the wsl_app connection dropping mid-session, and either source's local
    audio capture stream failing mid-session (e.g. its Windows audio
    device disappeared or changed) — either one ends the whole session,
    since a suggestion built from only one side of the conversation for
    the rest of it isn't something to silently fall back to.

    Also owns Generate Summary's enabled state (Day 23): starting a session
    disables it and clears any leftover summary from a previous session
    (see summary_display.reset()), since there's no complete transcript to
    summarize yet; every way a session can end enables it again, but only
    if the session actually produced a transcript worth summarizing.

    Also reads mic_enabled_checkbox's starting state when a session begins
    (Day 24) -- mic capture is skipped entirely at Start Session if it's
    already unchecked -- and wires it to mute/unmute mic capture live for
    the rest of the session (see handle_mic_enabled_toggled()), so
    background noise picked up on a real mic (e.g. someone else's TV) can
    be muted out without stopping the whole session. `mic_capture_manager`/
    `mic_enabled_checkbox` are both None on a machine with no usable
    microphone (see main()'s get_default_mic_device() branch) -- every
    mic-toggle code path here is a no-op in that case, the same defensive
    shape create_audio_capture_manager()'s own optional mic branch already
    uses.
    """
    start_button, stop_button = create_session_buttons(layout)
    current_attempt_id = 0

    def revert_to_pre_session_state():
        """Resets the Start/Stop buttons to look like no session is running."""
        start_button.setEnabled(True)
        stop_button.setEnabled(False)

    def end_session(notify_wsl_app):
        """
        Shared teardown for every way a session can end — an explicit Stop
        Session click, wsl_app failing to start one, the wsl_app connection
        dropping, or either source's capture stream failing — so each
        caller only has to say whether wsl_app still needs to be told
        (notify_wsl_app is False when it already knows the session isn't
        running: it never started one, or the connection to it is already
        dead). stop_capture() is called unconditionally on every manager,
        including whichever one (if any) already stopped itself after a
        stream failure — AudioCaptureManager.stop_capture() is a safe
        no-op on a manager that isn't currently capturing, so there's no
        need to track which manager, if any, already stopped.
        session_recorder.stop() is likewise called unconditionally — a
        safe no-op if this session was never being recorded. Generate
        Summary (Day 23) is enabled here only if there's actually a
        transcript to summarize — a session that failed to start, or ended
        before anything was ever transcribed, leaves it disabled.
        """
        if notify_wsl_app:
            wsl_connection.send_message({"type": "session_stopped"})
        for capture_manager in capture_managers:
            capture_manager.stop_capture()
        session_recorder.stop()
        window.setWindowTitle(DEFAULT_WINDOW_TITLE)
        revert_to_pre_session_state()
        generate_summary_button.setEnabled(bool(transcript_display.full_text()))

    def start_session():
        """Begins a session: clears the transcript pane and any previous summary, sends the current settings, notifies wsl_app, starts capture on every source (skipping the mic if Mic Enabled is already unchecked), starts session_recorder if the user opted in, sets the window title to reflect whichever disk-writing opt-ins are active, and flips button state."""
        nonlocal current_attempt_id
        current_attempt_id += 1
        transcript_display.reset()
        summary_display.reset()
        generate_summary_button.setEnabled(False)
        wsl_connection.send_message(
            current_settings_message(
                mode_dropdown, pause_dropdown, context_notes_edit, auto_suggest_checkbox, live_agent_export_checkbox
            )
        )
        wsl_connection.send_message({"type": "session_started", "attempt_id": current_attempt_id})
        mic_starts_muted = (
            mic_capture_manager is not None
            and mic_enabled_checkbox is not None
            and not mic_enabled_checkbox.isChecked()
        )
        for capture_manager in capture_managers:
            if capture_manager is mic_capture_manager and mic_starts_muted:
                continue
            capture_manager.start_capture()
        if save_transcript_checkbox.isChecked():
            session_recorder.start(mode_dropdown.currentText())
        window.setWindowTitle(
            session_recording_window_title(save_transcript_checkbox.isChecked(), live_agent_export_checkbox.isChecked())
        )
        start_button.setEnabled(False)
        stop_button.setEnabled(True)

    def stop_session():
        """Ends a session the user explicitly asked to stop."""
        end_session(notify_wsl_app=True)

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
        end_session(notify_wsl_app=False)
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
        end_session(notify_wsl_app=False)

    def handle_capture_failed():
        """
        One source's local audio capture stream itself failed mid-session
        (e.g. its Windows audio device disappeared or changed) — ends the
        whole session the same way Stop Session would, since wsl_app would
        otherwise be left waiting for audio that's never coming from that
        source. That source's capture has already stopped by the time this
        fires; end_session() also stops the other, still-healthy source's
        capture as part of the same teardown.
        """
        if not stop_button.isEnabled():
            return
        end_session(notify_wsl_app=True)

    def handle_mic_enabled_toggled(enabled):
        """
        Mutes or unmutes mic capture live (Day 24), the moment the
        checkbox is flipped, if a session is currently running -- a safe
        no-op otherwise (there's no capture to start/stop yet; start_session()
        reads the checkbox's current state itself when the next session
        begins, see above). A no-op on a machine with no usable microphone
        too, where mic_capture_manager is None.
        """
        if mic_capture_manager is None or not stop_button.isEnabled():
            return
        if enabled:
            mic_capture_manager.start_capture()
        else:
            mic_capture_manager.stop_capture()

    start_button.clicked.connect(start_session)
    stop_button.clicked.connect(stop_session)
    wsl_connection.session_start_failed_received.connect(handle_session_start_failed)
    wsl_connection.connection_changed.connect(handle_connection_changed)
    for capture_manager in capture_managers:
        capture_manager.capture_failed.connect(handle_capture_failed)
    if mic_enabled_checkbox is not None:
        mic_enabled_checkbox.toggled.connect(handle_mic_enabled_toggled)


def main():
    """
    Entry point: creates the app, the main window, the overlay window, and
    the separate Suggestions window (Day 27, see create_suggestions_window
    -- pulled out of the main window entirely so suggestion text has real
    room to be readable, independently resizable/movable rather than
    squeezed into the main window's single stacked column), starts the
    background connection to wsl_app, wires up both audio sources' capture
    (Day 19: WASAPI loopback for system audio, plus the user's own
    microphone -- each with its own device picker and capture status
    label, each its own independent AudioCaptureManager; mic capture is
    skipped entirely, loopback-only, on a machine with no usable
    microphone -- see get_default_mic_device()) including the live Mic
    Enabled mute toggle (Day 24, see create_mic_toggle_checkbox), the
    settings panel (including the opt-in "save transcript to a file"
    checkbox -- Day 22, see SessionRecorder), the overlay opacity/
    click-through controls, the auto-suggest toggle and manual trigger
    button, the session start/stop controls, the post-meeting summary
    controls (Day 23, see SummaryDisplay/create_summary_controls), and the
    global suggestion-trigger and overlay show/hide hotkeys, and runs the
    event loop until the main window is closed.

    Both the overlay and the Suggestions window are answer-only now (Day
    27) -- the overlay used to also show the transcript excerpt that
    prompted a suggestion (the "question"), but that was dropped per
    direct user feedback that it read as noise once you're actually
    glancing at this live during a call; nothing shows the question
    anywhere any more.
    """
    app = create_app()
    window = create_window()
    layout = create_central_widget(window)
    status_label = create_connection_status_label(layout)
    loopback_device_dropdown = create_device_dropdown(layout, "Loopback device (system audio, i.e. \"Them\"):")
    loopback_capture_status_label = create_capture_status_label(layout)
    mic_device_dropdown = create_device_dropdown(layout, "Microphone device (your own voice, i.e. \"You\"):")
    mic_capture_status_label = create_capture_status_label(layout)
    mic_enabled_checkbox = create_mic_toggle_checkbox(layout)
    mode_dropdown, pause_dropdown, context_notes_edit, save_transcript_checkbox, live_agent_export_checkbox = (
        create_settings_panel(layout)
    )
    overlay_window = create_overlay_window()
    create_overlay_controls(layout, overlay_window)
    auto_suggest_checkbox, generate_suggestion_button = create_suggestion_trigger_controls(layout)
    transcript_pane = create_transcript_pane(layout)
    transcript_display = create_transcript_display(transcript_pane)
    suggestions_window, suggestions_pane, latest_suggestion = create_suggestions_window()
    suggestion_display = create_suggestion_display(suggestions_pane, latest_suggestion, overlay_window)
    summary_pane, generate_summary_button, save_summary_button = create_summary_section(layout)
    summary_display = create_summary_display(summary_pane, generate_summary_button, save_summary_button)
    session_recorder = create_session_recorder()

    audio = pyaudio.PyAudio()
    loopback_devices = list_loopback_devices(audio)
    default_loopback_device = audio.get_default_wasapi_loopback()
    populate_device_dropdown(loopback_device_dropdown, loopback_devices, default_loopback_device)
    mic_devices = list_mic_devices(audio)
    default_mic_device = get_default_mic_device(audio, mic_devices)
    if default_mic_device is not None:
        populate_device_dropdown(mic_device_dropdown, mic_devices, default_mic_device)
    else:
        # No usable microphone on this machine (see get_default_mic_device)
        # -- run loopback-only rather than fail to start. Left visibly
        # disabled instead of hidden, so it's clear mic capture exists but
        # isn't available, not silently missing.
        mic_device_dropdown.setEnabled(False)
        mic_capture_status_label.setText("No microphone available")
        mic_enabled_checkbox.setEnabled(False)

    wsl_connection = start_wsl_connection(status_label)
    connect_incoming_messages_to_ui(wsl_connection, transcript_display, suggestion_display, summary_display)
    connect_session_recorder(wsl_connection, session_recorder)
    connect_auto_suggest_toggle(wsl_connection, auto_suggest_checkbox)
    loopback_capture_manager = create_audio_capture_manager(
        audio, wsl_connection, AUDIO_SOURCE_LOOPBACK, loopback_device_dropdown, default_loopback_device,
        loopback_capture_status_label,
    )
    capture_managers = [loopback_capture_manager]
    mic_capture_manager = None
    if default_mic_device is not None:
        mic_capture_manager = create_audio_capture_manager(
            audio, wsl_connection, AUDIO_SOURCE_MIC, mic_device_dropdown, default_mic_device, mic_capture_status_label
        )
        capture_managers.append(mic_capture_manager)
    create_session_controls(
        wsl_connection,
        capture_managers,
        mic_capture_manager,
        mic_enabled_checkbox,
        mode_dropdown,
        pause_dropdown,
        context_notes_edit,
        auto_suggest_checkbox,
        save_transcript_checkbox,
        live_agent_export_checkbox,
        session_recorder,
        transcript_display,
        summary_display,
        generate_summary_button,
        layout,
        window,
    )
    create_summary_controls(
        wsl_connection, transcript_display, summary_display, generate_summary_button, save_summary_button, window
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

    suggestions_window.show()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
