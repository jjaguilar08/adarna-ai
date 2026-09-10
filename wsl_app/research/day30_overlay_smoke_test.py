"""
Standalone smoke test for OverlayWindow's Day 30 auto-height-to-content
change (scrollbar removed). Not part of the running app -- run directly
with windows_app's own venv python on the real Windows side:

    windows_app\\.venv\\Scripts\\python.exe wsl_app\\research\\day30_overlay_smoke_test.py

Drives the real OverlayWindow class (no mocks) through a short, a long,
and a very-long suggestion, and prints the window's resulting height each
time plus whether it stayed within the screen's available height.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "windows_app"))

from PySide6.QtWidgets import QApplication
from main import OverlayWindow

SHORT_ANSWER = "Sounds good, let's go with that."

LONG_ANSWER = (
    "That's a fair point, and here's how I'd think about it. "
    "The main tradeoff is between shipping something quickly and making "
    "sure it actually holds up once real users start relying on it every "
    "day, so I'd want to spend a little more time up front on the parts "
    "that are hardest to change later, like the data model and the public "
    "API shape, while keeping everything else easy to revisit. "
    "In my experience, teams that rush the foundation end up paying for "
    "it many times over in rework, so a short investment now usually pays "
    "for itself within a month or two."
)

VERY_LONG_ANSWER = "\n".join(f"Line {i}: some supporting detail goes here." for i in range(1, 60))


def main():
    """Runs the real OverlayWindow through short/long/very-long suggestions and reports the resulting window height each time."""
    app = QApplication(sys.argv)
    overlay = OverlayWindow()
    overlay.show()

    for label, text in [("SHORT", SHORT_ANSWER), ("LONG", LONG_ANSWER), ("VERY_LONG", VERY_LONG_ANSWER)]:
        overlay.update_suggestion(text)
        app.processEvents()
        screen = overlay.screen()
        available_height = screen.availableGeometry().height() if screen else None
        print(
            f"{label}: window size = {overlay.width()}x{overlay.height()}, "
            f"screen available height = {available_height}, "
            f"within screen = {available_height is None or overlay.height() <= available_height}"
        )

    overlay.update_suggestion(LONG_ANSWER)
    app.processEvents()
    print(f"LONG again (before width change): window size = {overlay.width()}x{overlay.height()}")

    print("Resizing width to 300 (narrower) to check re-wrap/re-fit grows height...")
    overlay.resize(300, overlay.height())
    app.processEvents()
    print(f"AFTER_NARROWER_WIDTH_RESIZE: window size = {overlay.width()}x{overlay.height()}")

    print("Back to a short suggestion -- height should shrink back down.")
    overlay.update_suggestion(SHORT_ANSWER)
    app.processEvents()
    print(f"BACK_TO_SHORT: window size = {overlay.width()}x{overlay.height()}")

    sys.exit(0)


if __name__ == "__main__":
    main()
