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
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSizeGrip,
    QSlider,
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

SUGGESTION_PAUSE_PRESETS_SECONDS = [0.8, 1.2, 1.6, 2.0]
# Picked from the presets list itself (rather than a separate literal) so
# it can never drift out of sync with it -- 1.2s matches Day 7's default.
DEFAULT_SUGGESTION_PAUSE_SECONDS = SUGGESTION_PAUSE_PRESETS_SECONDS[1]

# Overlay visual redesign (Day 18), styled to match a real ParakeetAI
# screenshot shared as a reference: a dark, semi-transparent, rounded
# panel rather than a bare default Qt widget. Colors/spacing are a
# by-eye match for "the same feel," not a pixel-exact clone.
OVERLAY_BACKGROUND_COLOR = "rgba(24, 24, 28, 235)"
OVERLAY_BORDER_COLOR = "rgba(255, 255, 255, 30)"
OVERLAY_CAPTION_COLOR = "rgba(255, 255, 255, 140)"
OVERLAY_QUESTION_COLOR = "rgba(255, 255, 255, 205)"
OVERLAY_ANSWER_COLOR = "#F5F5F7"

# Range and default for the overlay opacity slider (see
# create_overlay_controls()), in whole percent. Floored well above 0 so
# the overlay can never be slid all the way to fully invisible with no
# obvious way back.
OVERLAY_OPACITY_MIN_PERCENT = 20
OVERLAY_OPACITY_MAX_PERCENT = 100
OVERLAY_OPACITY_DEFAULT_PERCENT = 90


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
    transcript_received = Signal(str, bool)
    suggestion_received = Signal(str, str)
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
        the UI to display -- a transcript's is_final flag (Day 17)
        distinguishes text that's now locked in from a still-changing
        partial guess, forwarded to TranscriptDisplay as a second signal
        argument so the pane can render each differently; a suggestion's
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
        current one.
        """
        while True:
            line = self._connection.readline()
            if not line:
                raise OSError("wsl_app closed the connection")
            message = json.loads(line)
            if message.get("type") == "transcript":
                self.transcript_received.emit(message["text"], bool(message.get("is_final", True)))
            elif message.get("type") == "suggestion":
                self.suggestion_received.emit(message.get("question") or "", message["text"])
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


def create_readonly_text_pane(layout):
    """
    Adds a scrolling, read-only text pane to layout -- the shared shape
    behind the transcript pane and the suggestions pane, so the two don't
    each hand-roll the same three lines. Not used by the overlay (Day 18):
    OverlayWindow's question/answer text needs its own styled QLabels
    instead, both for the visual redesign and so a click on top of the
    text still drags the window (see OverlayWindow._add_labeled_section).

    Returns:
        QPlainTextEdit: the pane, already added to layout.
    """
    pane = QPlainTextEdit()
    pane.setReadOnly(True)
    layout.addWidget(pane)
    return pane


def create_transcript_pane(layout):
    """
    Adds a labeled, scrolling, read-only pane that displays the transcript
    as it arrives from wsl_app. See TranscriptDisplay for how committed vs.
    tentative text (Day 17) are kept in sync with this pane.

    Returns:
        QPlainTextEdit: the pane to keep in sync with incoming transcript text.
    """
    layout.addWidget(QLabel("Transcript"))
    return create_readonly_text_pane(layout)


class TranscriptDisplay(QObject):
    """
    Keeps the transcript pane showing all committed (locked-in) text so
    far, plus whatever's currently tentative appended at the end --
    replaced in place each time a new partial arrives, rather than
    appended, so a still-settling partial doesn't pile up
    duplicate/superseded text on the pane (Phase 2.5, Day 17 -- see
    wsl_app/streaming_transcriber.py). A QObject (not a plain class) so
    wsl_connection.transcript_received -- emitted from WslConnection's
    background reader thread -- can be connected to update() as a real
    cross-thread queued connection, per this project's own hard-won lesson
    about lambdas having no owning QObject for Qt to marshal through (see
    OverlayToggle).
    """

    def __init__(self, pane):
        """Stores the pane to keep in sync, starting with nothing committed or tentative yet."""
        super().__init__()
        self._pane = pane
        self._committed_text = ""
        self._tentative_text = ""

    def update(self, text, is_final):
        """
        Folds one incoming transcript message into the running display: a
        final message's text is appended permanently to the committed
        transcript and the tentative text is cleared (it's now been
        superseded by a committed result); a non-final message's text
        replaces whatever tentative text was showing, in place.
        """
        if is_final:
            self._committed_text = f"{self._committed_text} {text}" if self._committed_text else text
            self._tentative_text = ""
        else:
            self._tentative_text = text
        self._render()

    def reset(self):
        """
        Clears all committed and tentative text -- call when a new session
        starts, so the pane doesn't keep showing a previous session's
        transcript with the new one appended right after it (found in code
        review, Day 17: this gap predates today's change, but streaming's
        continuous partial updates make it far more visible than the old
        sparse per-segment appends did).
        """
        self._committed_text = ""
        self._tentative_text = ""
        self._render()

    def _render(self):
        """Rewrites the pane's full text from committed + tentative state, and scrolls to the end so new/updating text stays visible."""
        full_text = self._committed_text
        if self._tentative_text:
            full_text = f"{full_text} {self._tentative_text}" if full_text else self._tentative_text
        self._pane.setPlainText(full_text)
        cursor = self._pane.textCursor()
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
    Fans out one incoming suggestion to everywhere it's shown: the main
    window's pane and the Copy button's tracker get the answer text only
    (see main()'s docstring for why the question stays overlay-only), while
    the overlay gets both the question and the answer, rendered in its
    separate labels (Day 18 -- see OverlayWindow.update_suggestion). A
    QObject with a bound-method slot, not a lambda, for the same
    cross-thread reason as LatestSuggestion above: suggestion_received is
    emitted from WslConnection's background reader thread, and a lambda has
    no owning QObject for Qt to marshal the call through safely (see
    OverlayToggle.toggle for the fuller explanation of that rule).
    """

    def __init__(self, suggestions_pane, latest_suggestion, overlay_window):
        """Stores the three things one incoming suggestion needs to update."""
        super().__init__()
        self._suggestions_pane = suggestions_pane
        self._latest_suggestion = latest_suggestion
        self._overlay_window = overlay_window

    def update(self, question, answer):
        """Updates the main pane and Copy-button tracker with the answer, and the overlay with both the question and the answer."""
        self._suggestions_pane.setPlainText(answer)
        self._latest_suggestion.update(answer)
        self._overlay_window.update_suggestion(question, answer)


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


class OverlayWindow(QWidget):
    """
    The always-on-top overlay: frameless, stays above other windows, and
    shows the transcript excerpt that prompted the latest suggestion
    (the "question") above the suggestion itself (the "answer") -- per
    PRD §8 Phase 2's Day 18 visual redesign, matching a real ParakeetAI
    screenshot shared as a reference (dark, semi-transparent, rounded
    panel; no transcript, no settings, no session controls -- those all
    stay on the main window). Starts hidden; create_overlay_toggle()
    wires up the hotkey that shows it.

    A real class (not a plain QWidget built by a factory function, like
    every other widget in this file) because dragging and the rounded/
    translucent look both need virtual methods overridden
    (mousePressEvent/mouseMoveEvent/mouseReleaseEvent, paintEvent's
    stylesheet painting) -- Qt's normal way of doing this is a subclass,
    not event wiring bolted onto a generic QWidget.

    WA_QuitOnClose is turned off specifically so this window doesn't
    count toward Qt's "quit once every counted window is closed" check --
    without this, closing the main window while the overlay happens to
    still be visible would leave the app running invisibly, since the
    overlay would still be an open, counted window.
    """

    def __init__(self):
        """Builds the frameless, translucent, rounded overlay panel and its question/answer labels, starting hidden with no drag in progress."""
        super().__init__()
        self.setWindowTitle("Adarna Overlay")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_QuitOnClose, False)
        # Both needed together for a rounded, see-through-cornered panel:
        # WA_TranslucentBackground makes the whole window surface support
        # alpha (so the corners outside the rounded rect are truly
        # see-through, not just black); WA_StyledBackground makes a plain
        # QWidget actually paint its stylesheet's background/border-radius
        # at all, which it otherwise skips by default.
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"OverlayWindow {{"
            f"  background-color: {OVERLAY_BACKGROUND_COLOR};"
            f"  border: 1px solid {OVERLAY_BORDER_COLOR};"
            f"  border-radius: 14px;"
            f"}}"
        )
        self.setMinimumSize(260, 140)
        self._drag_offset = None

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(16, 14, 16, 10)
        outer_layout.setSpacing(4)

        scroll_row = QHBoxLayout()
        scroll_row.setSpacing(2)
        scroll_area = self._build_scroll_area()
        scroll_row.addWidget(scroll_area)
        scroll_row.addWidget(self._build_external_scrollbar(scroll_area))
        outer_layout.addLayout(scroll_row)

        grip_row = QHBoxLayout()
        grip_row.addStretch()
        grip = QSizeGrip(self)
        grip.setStyleSheet("background: transparent;")
        grip_row.addWidget(grip)
        outer_layout.addLayout(grip_row)

        # One reused single-shot timer for _sync_scroll_content_width
        # (see resizeEvent()), rather than a fresh QTimer.singleShot(0, ...)
        # per resize event -- dragging the QSizeGrip fires many resizeEvent
        # calls in quick succession (one per intermediate geometry change),
        # and re-starting an already-pending single-shot timer just pushes
        # its fire time out rather than queuing a second one, so only the
        # last resize in a burst actually triggers a resync. Found by
        # /code-review, Day 18.
        self._scroll_width_sync_timer = QTimer(self)
        self._scroll_width_sync_timer.setSingleShot(True)
        self._scroll_width_sync_timer.setInterval(0)
        self._scroll_width_sync_timer.timeout.connect(self._sync_scroll_content_width)

        # Sized last, once self._scroll_area/self._scroll_content exist --
        # resize() fires resizeEvent() immediately, which reads both (see
        # resizeEvent()'s docstring).
        self.resize(440, 240)

    def _build_scroll_area(self):
        """
        Builds the scrollable content area holding the question and answer
        sections. A real QScrollArea, not just word-wrapped labels left to
        grow the window -- an earlier version tried growing the window to
        fit instead, but that had no ceiling (a long enough question could
        grow the overlay taller than the screen) and gave no way to recover
        text if the user shrank the window smaller than the current content
        needed (/code-review, Day 18: confirmed empirically that a plain
        QLabel just silently stops drawing text past its allocated rect,
        with no scrollbar or indicator anything is missing). A scroll area
        fixes both at once: content that doesn't fit is always reachable by
        scrolling, no matter how long the text or how small the user drags
        the window, and the window's own size goes back to being purely
        user-controlled (drag/resize), not something update_suggestion()
        also reaches in and changes.

        Its background and its viewport's background are set transparent
        so the dark rounded panel painted on the window itself (see
        __init__) shows through underneath. Both the scroll area itself
        and its viewport are set mouse-transparent for the same
        drag-anywhere reason the question and answer labels are (see
        _add_labeled_section) -- setting only the viewport is not enough:
        confirmed empirically (/code-review, Day 18) via childAt(), the
        same lookup Qt's real event dispatch uses to route a click, that
        (depending on the overlay's exact size and content at the time --
        it wasn't even consistent) a click landing on the
        viewport-transparent-but-not-itself-transparent QScrollArea could
        still resolve to the QScrollArea widget itself rather than the
        window underneath. QScrollArea has no drag handling of its own, so
        those clicks would have silently gone nowhere instead of starting
        a drag.

        Both its own scrollbars are turned off (including the vertical
        one, normally the whole point of a scroll area) -- see
        _build_external_scrollbar() for why, and for the real, always-
        clickable scrollbar that replaces it.

        Trade-off worth knowing about: making this whole subtree
        mouse-transparent means the mouse wheel no longer scrolls it
        either -- wheel events go through the same hit-testing as clicks,
        so they pass through untouched the same way a click does, rather
        than reaching the scroll area. The external scrollbar (see
        _build_external_scrollbar()) is what actually guarantees overflow
        content stays reachable, not the wheel -- a real test of "does
        long content stay reachable" should drag that scrollbar, not
        assume the wheel works.

        Note for anyone tempted to remove one of the WA_TransparentForMouseEvents
        calls in this method or _add_labeled_section() as apparent
        copy-paste: each is independently load-bearing. Qt's hit-testing
        recurses to the deepest widget under the cursor and only skips
        levels actually marked transparent -- a real widget (a label, the
        content container) still catches a click on its own area even
        when an ancestor above it (the viewport, the scroll area) is
        already transparent. Removing any one layer reintroduces a dead
        zone for drag right where that specific widget sits, not
        elsewhere -- confirmed by testing each layer's contribution via
        childAt() during Day 18's review, not assumed.

        Returns:
            QScrollArea: ready to add to the window's layout.
        """
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setStyleSheet("background: transparent; border: none;")
        scroll_area.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        scroll_area.viewport().setStyleSheet("background: transparent;")
        scroll_area.viewport().setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # setWidgetResizable(True) alone doesn't reliably let a
        # height-for-width widget (word-wrapped QLabels) grow taller than
        # the viewport -- confirmed empirically (/code-review, Day 18): it
        # just clamped the content widget to the viewport's exact size
        # instead of scrolling, even though the labels' own
        # heightForWidth() said they needed much more room. Pinning the
        # content widget's width to the viewport's current width (see
        # resizeEvent()) forces its height to come from its own
        # sizeHint() at that fixed width instead, which is what actually
        # lets the scroll area detect the overflow and become scrollable.
        self._scroll_area = scroll_area

        content = QWidget()
        content.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(2)

        self._question_label = self._add_labeled_section(content_layout, "QUESTION", OVERLAY_QUESTION_COLOR)
        content_layout.addSpacing(8)
        self._answer_label = self._add_labeled_section(
            content_layout, "SUGGESTED RESPONSE", OVERLAY_ANSWER_COLOR
        )
        content_layout.addStretch()

        scroll_area.setWidget(content)
        self._scroll_content = content
        return scroll_area

    def _build_external_scrollbar(self, scroll_area):
        """
        Builds a real, always-clickable vertical scrollbar to sit beside
        the scroll area, kept in sync with its scroll position in both
        directions. scroll_area's own built-in vertical scrollbar is
        turned off entirely (see _build_scroll_area()) rather than used
        directly, because it lives inside the now-fully-mouse-transparent
        scroll_area subtree and would be just as unreachable to a real
        click as everything else in there -- confirmed empirically via
        childAt(), the same lookup Qt's real event dispatch uses to route
        a click (/code-review, Day 18): making scroll_area itself
        mouse-transparent, needed for reliable drag-anywhere everywhere on
        the overlay (see _build_scroll_area()'s docstring), also makes
        Qt's hit-testing skip its own internal scrollbar, not just the
        content above it -- a visible scrollbar nobody could actually
        click. This one is a plain sibling widget instead, entirely
        outside that transparent subtree, so it's always independently
        draggable regardless of drag-through settings elsewhere on the
        overlay -- the actual guarantee behind "content that doesn't fit
        is always reachable," not just a scrollbar that merely looks like
        one.

        Returns:
            QScrollBar: ready to add next to the scroll area.
        """
        inner_scrollbar = scroll_area.verticalScrollBar()
        scrollbar = QScrollBar(Qt.Vertical)
        scrollbar.setRange(inner_scrollbar.minimum(), inner_scrollbar.maximum())
        scrollbar.setStyleSheet("background: transparent;")
        inner_scrollbar.rangeChanged.connect(scrollbar.setRange)
        inner_scrollbar.valueChanged.connect(scrollbar.setValue)
        scrollbar.valueChanged.connect(inner_scrollbar.setValue)
        return scrollbar

    def _add_labeled_section(self, layout, caption_text, text_color):
        """
        Adds one caption ("QUESTION" / "SUGGESTED RESPONSE") plus a
        word-wrapped text label beneath it to `layout`. Both are set
        mouse-transparent so a click anywhere on the overlay -- including
        directly on top of the question or answer text -- still starts a
        window drag (see mousePressEvent) instead of being swallowed by
        the label; these are read-only glanceable text, not meant to
        support text selection, so trading that away for drag-anywhere is
        the right call here.

        Returns:
            QLabel: the (initially empty) text label to keep updated.
        """
        caption = QLabel(caption_text)
        caption.setTextFormat(Qt.PlainText)
        caption.setStyleSheet(f"color: {OVERLAY_CAPTION_COLOR}; font-size: 10px; font-weight: 600;")
        caption.setAttribute(Qt.WA_TransparentForMouseEvents, True)
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
        text_label.setStyleSheet(f"color: {text_color}; font-size: 13px;")
        text_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(text_label)
        return text_label

    def update_suggestion(self, question, answer):
        """Updates the overlay's question and answer text to a newly received suggestion -- see _build_scroll_area() for how text longer than the window's current size stays reachable rather than getting clipped."""
        self._question_label.setText(question)
        self._answer_label.setText(answer)

    def set_opacity(self, opacity):
        """Sets the whole overlay's opacity (0.0-1.0), including its background and text -- see create_overlay_controls()'s slider."""
        self.setWindowOpacity(opacity)

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
        Whenever the window's own size actually changes (drag-resize via
        the grip, or the initial show), re-pins the scrollable content's
        width to the scroll area's current viewport width -- see
        _build_scroll_area()'s docstring for why this needs to happen
        explicitly rather than trusting setWidgetResizable(True) alone.

        Deferred one event-loop tick via self._scroll_width_sync_timer
        (a reused single-shot timer, not read synchronously right here:
        confirmed empirically (/code-review, Day 18) that reading
        self._scroll_area.viewport().width() immediately inside
        resizeEvent() returns a stale value -- Qt hadn't yet finished
        cascading the window's new size down into the scroll area's own
        child layout at that point, so every read kept returning an old
        (or, before the first real show, a not-yet-laid-out default) width
        instead of the current one. Giving Qt's event loop one more turn
        before reading it is the standard way around this class of Qt
        layout-timing gap. Restarting the same timer (rather than firing a
        fresh QTimer.singleShot(0, ...) each time) also debounces a rapid
        burst of resizeEvent calls -- e.g. dragging the QSizeGrip -- down
        to one actual resync instead of one per intermediate frame.
        """
        super().resizeEvent(event)
        self._scroll_width_sync_timer.start()

    def _sync_scroll_content_width(self):
        """
        Pins the scrollable content's width to the scroll area's own
        viewport width -- see resizeEvent()'s docstring for why this runs
        deferred rather than synchronously during the resize. Reading the
        viewport's width directly is safe here (rather than reserving
        space defensively the way an early version of this method had to):
        the scroll area's own vertical scrollbar is permanently off (see
        _build_scroll_area()), so nothing ever shrinks the viewport out
        from under this value the way a just-appearing internal scrollbar
        once did.
        """
        self._scroll_content.setFixedWidth(self._scroll_area.viewport().width())


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
    incoming committed/tentative transcript messages.

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
    pane = create_readonly_text_pane(layout)

    latest_suggestion = LatestSuggestion()

    def copy_latest_suggestion():
        """Copies the most recently received suggestion text to the clipboard."""
        if latest_suggestion.text:
            QApplication.clipboard().setText(latest_suggestion.text)

    copy_button = QPushButton("Copy Latest Suggestion")
    copy_button.clicked.connect(copy_latest_suggestion)
    layout.addWidget(copy_button)

    return pane, latest_suggestion


def create_suggestion_display(suggestions_pane, latest_suggestion, overlay_window):
    """
    Creates the SuggestionDisplay that fans out one incoming suggestion to
    the main pane, the Copy button's tracker, and the overlay.

    Returns:
        SuggestionDisplay: connect wsl_connection.suggestion_received to
        its update() method (see connect_incoming_messages_to_ui()).
    """
    return SuggestionDisplay(suggestions_pane, latest_suggestion, overlay_window)


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

    Returns:
        tuple[QComboBox, QComboBox, QPlainTextEdit]: the mode and
        suggestion pause dropdowns, plus the context notes text box, in
        that order.
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

    layout.addWidget(group)
    return mode_dropdown, pause_dropdown, context_notes_edit


def current_settings_message(mode_dropdown, pause_dropdown, context_notes_edit, auto_suggest_checkbox):
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


def connect_incoming_messages_to_ui(wsl_connection, transcript_display, suggestion_display):
    """
    Wires wsl_app's incoming transcript/suggestion messages to the UI: each
    transcript message updates the transcript pane via transcript_display
    (committed text appended permanently, tentative text replaced in place
    -- see TranscriptDisplay), and each suggestion is fanned out by
    suggestion_display to the main pane, the Copy button's tracker, and the
    overlay's question/answer labels (see SuggestionDisplay).
    """
    wsl_connection.transcript_received.connect(transcript_display.update)
    wsl_connection.suggestion_received.connect(suggestion_display.update)


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
    pause_dropdown,
    context_notes_edit,
    auto_suggest_checkbox,
    transcript_display,
    layout,
    window,
):
    """
    Adds the Start Session / Stop Session buttons and wires them up:
    starting clears the transcript pane (see TranscriptDisplay.reset —
    Day 17: otherwise a new session's transcript would appear appended
    right after whatever the previous one left on screen), sends the
    settings panel's current values (including the context notes and the
    auto-suggest checkbox's starting state) as settings_changed, then
    session_started (tagged with a fresh attempt_id — see
    handle_session_start_failed), and starts audio capture; stopping
    sends session_stopped and stops it. Button enabled-state tracks which
    action is currently valid.

    Also handles three ways a session can end itself, all reverting to the
    same clean pre-session UI state stop_session() reaches (see
    end_session()): wsl_app reporting it couldn't start the session at all
    (session_start_failed — e.g. the claude CLI process failing to start),
    the wsl_app connection dropping mid-session, and the local audio
    capture stream itself failing mid-session (e.g. the Windows audio
    device disappeared or changed).
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
        """Begins a session: clears the transcript pane, sends the current settings, notifies wsl_app, starts capture, and flips button state."""
        nonlocal current_attempt_id
        current_attempt_id += 1
        transcript_display.reset()
        wsl_connection.send_message(
            current_settings_message(mode_dropdown, pause_dropdown, context_notes_edit, auto_suggest_checkbox)
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
    audio capture, the settings panel, the overlay opacity/click-through
    controls, the auto-suggest toggle and manual trigger button, the
    session start/stop controls, and the global suggestion-trigger and
    overlay show/hide hotkeys, and runs the event loop until the main
    window is closed.

    The main window's suggestion pane stays answer-only (Day 18): the
    overlay is the one place the question/answer excerpt pairing actually
    matters, since it's what's glanced at live during a call, while the
    main window is mainly used for session start/stop and settings --
    adding the question there too didn't seem like a clear improvement,
    just more text in a pane already working fine as-is.
    """
    app = create_app()
    window = create_window()
    layout = create_central_widget(window)
    status_label = create_connection_status_label(layout)
    device_dropdown = create_device_dropdown(layout)
    capture_status_label = create_capture_status_label(layout)
    mode_dropdown, pause_dropdown, context_notes_edit = create_settings_panel(layout)
    overlay_window = create_overlay_window()
    create_overlay_controls(layout, overlay_window)
    auto_suggest_checkbox, generate_suggestion_button = create_suggestion_trigger_controls(layout)
    transcript_pane = create_transcript_pane(layout)
    transcript_display = create_transcript_display(transcript_pane)
    suggestions_pane, latest_suggestion = create_suggestions_section(layout)
    suggestion_display = create_suggestion_display(suggestions_pane, latest_suggestion, overlay_window)

    audio = pyaudio.PyAudio()
    devices = list_loopback_devices(audio)
    default_device = audio.get_default_wasapi_loopback()
    populate_device_dropdown(device_dropdown, devices, default_device)

    wsl_connection = start_wsl_connection(status_label)
    connect_incoming_messages_to_ui(wsl_connection, transcript_display, suggestion_display)
    connect_auto_suggest_toggle(wsl_connection, auto_suggest_checkbox)
    capture_manager = create_audio_capture_manager(
        audio, wsl_connection, device_dropdown, default_device, capture_status_label
    )
    create_session_controls(
        wsl_connection,
        capture_manager,
        mode_dropdown,
        pause_dropdown,
        context_notes_edit,
        auto_suggest_checkbox,
        transcript_display,
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
