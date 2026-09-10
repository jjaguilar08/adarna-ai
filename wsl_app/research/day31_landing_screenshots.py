"""
Standalone smoke test that drives the real main window, Suggestions window,
and OverlayWindow classes from windows_app/main.py with fabricated sample
content, so a separate PowerShell script (see
design_preferences/capture_screenshots.ps1) can screenshot each one for the
landing page. Never real transcript data -- a plausible fabricated
interview exchange only. Not part of the running app -- no wsl_app
connection, no real audio devices, no hotkeys -- run directly with
windows_app's own venv python on the real Windows side:

    windows_app\\.venv\\Scripts\\python.exe wsl_app\\research\\day31_landing_screenshots.py

Shows all three windows at known screen positions, prints READY once
they're up, then keeps the event loop alive for SCREENSHOT_WINDOW_SECONDS
before quitting on its own.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "windows_app"))

from PySide6.QtCore import QTimer

from main import (
    AUDIO_SOURCE_LOOPBACK,
    AUDIO_SOURCE_MIC,
    create_app,
    create_capture_status_label,
    create_central_widget,
    create_connection_status_label,
    create_device_dropdown,
    create_mic_toggle_checkbox,
    create_overlay_controls,
    create_overlay_window,
    create_settings_panel,
    create_suggestion_display,
    create_suggestion_trigger_controls,
    create_suggestions_window,
    create_summary_section,
    create_transcript_display,
    create_transcript_pane,
    create_window,
)

SCREENSHOT_WINDOW_SECONDS = 25

SAMPLE_TRANSCRIPT = [
    (AUDIO_SOURCE_LOOPBACK, "Walk me through how you'd approach the audio capture side of this.", 0.0),
    (AUDIO_SOURCE_MIC, "Sure -- the tricky part is that WSL can't see native Windows system audio directly.", 4.0),
    (AUDIO_SOURCE_LOOPBACK, "Right, so how did you get around that?", 9.5),
    (
        AUDIO_SOURCE_MIC,
        "I split it into two processes -- a native Windows side for capture and the GUI, talking to a WSL side "
        "over a local socket.",
        11.5,
    ),
]

SUGGESTION_TEXT = (
    "I'd split it into two processes: a native Windows process for WASAPI audio capture and the GUI, and a WSL "
    "process for speech-to-text and the Claude session, talking over a local TCP socket. I tested a "
    "single-process approach first and ruled it out once it was clear WSL can't see native Windows system audio."
)


def build_main_window():
    """
    Builds the real main window populated with fabricated sample transcript
    and settings content, for screenshotting.

    Returns:
        tuple[QMainWindow, OverlayWindow]: the populated main window, and
        the overlay window created alongside it (main.py always creates
        both together), so the caller can reuse the same overlay instance
        rather than building a second, disconnected one.
    """
    window = create_window()
    layout = create_central_widget(window)

    status_label = create_connection_status_label(layout)
    status_label.setText("Connected")

    loopback_dropdown = create_device_dropdown(layout, 'Loopback device (system audio, i.e. "Them"):')
    loopback_dropdown.addItem("Speakers (Realtek(R) Audio)")
    loopback_status = create_capture_status_label(layout)
    loopback_status.setText("Capturing")

    mic_dropdown = create_device_dropdown(layout, 'Microphone device (your own voice, i.e. "You"):')
    mic_dropdown.addItem("Microphone (Realtek(R) Audio)")
    mic_status = create_capture_status_label(layout)
    mic_status.setText("Capturing")

    create_mic_toggle_checkbox(layout)

    mode_dropdown, _pause_dropdown, _context_notes_edit, _save_checkbox, _export_checkbox = create_settings_panel(
        layout
    )
    mode_dropdown.setCurrentText("Interview")

    overlay_window = create_overlay_window()
    create_overlay_controls(layout, overlay_window)
    create_suggestion_trigger_controls(layout)

    transcript_pane = create_transcript_pane(layout)
    transcript_display = create_transcript_display(transcript_pane)
    for source, text, started_at in SAMPLE_TRANSCRIPT:
        transcript_display.update(source, text, started_at)

    create_summary_section(layout)

    window.resize(820, 1000)
    return window, overlay_window


def build_suggestions_window(overlay_window):
    """
    Builds the real Suggestions window populated with a fabricated sample
    suggestion, for screenshotting.

    Returns:
        QWidget: the populated Suggestions window.
    """
    window, pane, latest_suggestion = create_suggestions_window()
    suggestion_display = create_suggestion_display(pane, latest_suggestion, overlay_window)
    suggestion_display.update("unused", SUGGESTION_TEXT)
    return window


def main():
    """Shows the main window, Suggestions window, and overlay with fabricated sample content at known screen positions, then quits on its own after SCREENSHOT_WINDOW_SECONDS."""
    app = create_app()

    window, overlay_window = build_main_window()
    suggestions_window = build_suggestions_window(overlay_window)

    window.move(40, 40)
    suggestions_window.move(900, 40)
    overlay_window.move(900, 640)

    window.show()
    suggestions_window.show()
    overlay_window.show()

    # update_suggestion() re-fits the overlay's height to its text at the
    # window's *current* width (see OverlayWindow._resize_to_fit_content) --
    # called again here, after show() has already given the window its real
    # on-screen geometry, since a call beforehand fits against the
    # not-yet-realized pre-show width/font metrics instead.
    app.processEvents()
    overlay_window.update_suggestion(SUGGESTION_TEXT)
    app.processEvents()

    print("READY", flush=True)
    QTimer.singleShot(SCREENSHOT_WINDOW_SECONDS * 1000, app.quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
