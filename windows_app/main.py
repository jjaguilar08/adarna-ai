import json
import socket
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

CONFIG_PATH = Path(__file__).resolve().parent.parent / "ipc_config.json"
PING_INTERVAL_SECONDS = 2
RECONNECT_DELAY_SECONDS = 2


def load_port():
    """
    Reads the shared IPC config and returns the port both sides connect on.

    Returns:
        int: the TCP port from ipc_config.json.
    """
    with open(CONFIG_PATH) as config_file:
        config = json.load(config_file)
    return config["port"]


class IpcClient(QObject):
    """
    Talks to wsl_app over a socket on a background thread and reports
    connection status back to the Qt main thread via a signal. See
    wsl_app/main.py for the full set of message types this protocol
    will carry as later days add audio/transcript/suggestion messages.
    """

    connection_changed = Signal(bool)

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
            self.connection_changed.emit(False)
            time.sleep(RECONNECT_DELAY_SECONDS)

    def _ping_until_disconnected(self, port):
        """
        Opens one connection to wsl_app and pings it every couple seconds
        until the connection breaks.
        """
        with socket.create_connection(("127.0.0.1", port)) as sock:
            connection = sock.makefile("rwb")
            while True:
                connection.write((json.dumps({"type": "ping"}) + "\n").encode())
                connection.flush()
                line = connection.readline()
                if not line:
                    raise OSError("wsl_app closed the connection")
                self.connection_changed.emit(True)
                time.sleep(PING_INTERVAL_SECONDS)


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


def create_status_label(window):
    """
    Adds a status label to the window showing the wsl_app connection state.

    Returns:
        QLabel: the label to keep updated as the connection state changes.
    """
    label = QLabel("Disconnected")
    window.setCentralWidget(label)
    return label


def start_ipc_client(status_label):
    """
    Starts the background thread that connects to wsl_app and keeps
    status_label in sync with the connection state.

    Returns:
        IpcClient: the client object driving the background thread.
    """
    client = IpcClient()
    client.connection_changed.connect(
        lambda connected: status_label.setText("Connected" if connected else "Disconnected")
    )
    threading.Thread(target=client.run, daemon=True).start()
    return client


def main():
    """
    Entry point: creates the app and window, starts the background IPC
    client, and runs the event loop until the window is closed.
    """
    app = create_app()
    window = create_window()
    status_label = create_status_label(window)
    start_ipc_client(status_label)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
