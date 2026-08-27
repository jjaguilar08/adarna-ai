import sys

from PySide6.QtWidgets import QApplication, QMainWindow


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


def main():
    """
    Entry point: creates the app and window, shows the window, and
    runs the event loop until the window is closed.
    """
    app = create_app()
    window = create_window()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
