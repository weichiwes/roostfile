"""Render only this application's synthetic demo widgets to a short GIF."""
import sys
from pathlib import Path
import io
import desktop_app as ui
from demo import prepare_demo
from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice


def main():
    language = sys.argv[1] if len(sys.argv) > 1 else "en"
    app = ui.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet(ui.STYLE)
    prepare_demo(ui.BASE, language)
    window = ui.ArchiveWindow(demo=True, language_override=language)
    window.setAttribute(ui.Qt.WA_DontShowOnScreen, True)
    window.show()
    window.deck.set_reduced_motion(False)
    frames = []
    elapsed = [0]
    timer = ui.QTimer()
    def capture():
        elapsed[0] += 80
        if elapsed[0] == 560:
            window.toggle_deck_layout()
        if elapsed[0] == 1520:
            window.toggle_deck_layout()
        raw = QByteArray()
        buffer = QBuffer(raw)
        buffer.open(QIODevice.WriteOnly)
        window.deck.grab().save(buffer, "PNG")
        im = Image.open(io.BytesIO(bytes(raw))).convert("RGB")
        im.thumbnail((1000, 390))
        frames.append(im.copy())
        if elapsed[0] >= 2480:
            timer.stop()
            out = ui.BASE / "previews" / "archive-motion.gif"
            out.parent.mkdir(exist_ok=True)
            frames[0].save(out, save_all=True, append_images=frames[1:], duration=80, loop=0)
            window.close()
            app.quit()
    timer.timeout.connect(capture)
    timer.start(80)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
