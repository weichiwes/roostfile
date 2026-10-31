"""Roo, the small desktop assistant: a drop target with a bit of life in it.

It is drawn here, so it needs no image files, and any picture can replace it. The
same movement applies to every look, so a person's own image feels just as alive.
Name a folder's pictures idle, excited, working and happy to give each mood its own.
"""
from __future__ import annotations

import math
import random
import shutil
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMovie, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QWidget

from core import _assert_local_path
from pages import image_reader
from theme import BG, HAIRLINE_STRONG, TEXT

MOODS = ("idle", "excited", "working", "happy")
SIZES = {"small": 72, "medium": 96, "large": 128}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
BODY = "#7FB08F"
BELLY = "#E8F1E4"
BEAK = "#E9B872"
INK = "#1C2A20"
FRAME_MS = 33
BUBBLE_WIDTH = 340


def readable_image(path) -> bool:
    """Decodes it, because a file can start like a picture and still have nothing in it."""
    try:
        path = _assert_local_path(path)
        return path.suffix.lower() in IMAGE_TYPES and path.is_file() and path.stat().st_size <= MAX_IMAGE_BYTES and not image_reader(path).read().isNull()
    except (OSError, ValueError):
        return False


def install_image(source, folder):
    """Keeps a copy, so the look survives the original being moved. False if it is not usable."""
    if not readable_image(source):
        return False
    picture = Path(source).read_bytes()  # It may be the picture already in use.
    clear_images(folder)
    Path(folder).mkdir(parents=True, exist_ok=True)
    (Path(folder) / f"idle{Path(source).suffix.lower()}").write_bytes(picture)
    return True


def install_image_set(source, folder):
    """Copies idle, excited, working and happy pictures from a folder. Returns how many it found."""
    found = [path for path in Path(source).iterdir() if path.stem.casefold() in MOODS and readable_image(path)]
    if not found:
        return 0
    pictures = {f"{path.stem.casefold()}{path.suffix.lower()}": path.read_bytes() for path in found}
    clear_images(folder)
    Path(folder).mkdir(parents=True, exist_ok=True)
    for name, picture in pictures.items():
        (Path(folder) / name).write_bytes(picture)
    return len(pictures)


def clear_images(folder):
    shutil.rmtree(folder, ignore_errors=True)


class Bubble(QFrame):
    """What Roo has to say, beside it. Never takes the keyboard."""

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 8, 14, 8)
        self.text = QLabel()
        self.text.setTextFormat(Qt.PlainText)
        self.text.setStyleSheet(f"color:{TEXT}; font-size:13px; background:transparent;")
        self.text.setWordWrap(True)
        row.addWidget(self.text)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)

    def say(self, text, anchor: QRectF, seconds=5):
        self.text.setText(text)
        # Left to itself a wrapping label breaks short sentences well before it runs out of room.
        self.text.setFixedWidth(min(BUBBLE_WIDTH, self.text.fontMetrics().horizontalAdvance(text) + 2))
        self.text.setFixedHeight(self.text.heightForWidth(self.text.width()))
        self.adjustSize()
        screen = (QApplication.screenAt(anchor.center().toPoint()) or QApplication.primaryScreen()).availableGeometry()
        x = min(max(screen.left() + 8, int(anchor.center().x() - self.width() / 2)), screen.right() - self.width() - 8)
        y = int(anchor.top()) - self.height() - 6
        self.move(x, y if y > screen.top() else int(anchor.bottom()) + 6)
        self.show()
        self.timer.start(seconds * 1000)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(BG))
        painter.setPen(QPen(QColor(HAIRLINE_STRONG), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 12, 12)


class Assistant(QWidget):
    clicked = Signal()
    dropped = Signal(list)
    menu_requested = Signal(QPoint)
    moved = Signal(QPoint)

    def __init__(self, images_folder, size="medium", reduced_motion=False):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.setAcceptDrops(True)
        self.setToolTip("")
        self.images_folder = Path(images_folder)
        self.mood = "idle"
        self.reduced_motion = reduced_motion
        self.frame = 0
        self.blink = 0
        self.hop = 0.0
        self.pressed_at = None
        self.travelled = False
        self.pictures = {}
        self.movies = {}
        self.bubble = Bubble()
        self.set_size(size)
        self.load_images()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(FRAME_MS)
        self.mood_timer = QTimer(self)
        self.mood_timer.setSingleShot(True)
        self.mood_timer.timeout.connect(lambda: self.set_mood("idle"))

    def set_size(self, name):
        self.size_name = name if name in SIZES else "medium"
        side = SIZES[self.size_name]
        self.setFixedSize(side, round(side * 1.2))

    def release_images(self):
        """A playing GIF keeps its file open, and Windows will not replace an open file."""
        for movie in self.movies.values():
            movie.stop()
            movie.setFileName("")
        self.pictures, self.movies = {}, {}

    def load_images(self):
        self.release_images()
        if not self.images_folder.is_dir():
            return
        for path in sorted(self.images_folder.iterdir()):
            if path.stem not in MOODS or not readable_image(path):
                continue
            if path.suffix.lower() == ".gif":
                movie = QMovie(str(path))
                movie.start()
                self.movies[path.stem] = movie
            else:
                self.pictures[path.stem] = QPixmap(str(path))

    @property
    def custom(self):
        return bool(self.pictures or self.movies)

    def picture(self):
        """The image for this mood, falling back to the main one."""
        for mood in (self.mood, "idle", *MOODS):
            if mood in self.movies:
                return self.movies[mood].currentPixmap()
            if mood in self.pictures:
                return self.pictures[mood]
        return None

    def set_mood(self, mood, seconds=0):
        self.mood = mood
        self.mood_timer.stop()
        if seconds:
            self.mood_timer.start(seconds * 1000)
        if mood == "happy":
            self.hop = 1.0
        self.update()

    def say(self, text, seconds=5):
        if self.isVisible():  # Hidden for a game or a presentation, so it keeps quiet too.
            self.bubble.say(text, QRectF(self.geometry()), seconds)

    def tick(self):
        if not self.isVisible():
            return
        self.frame += 1
        if self.blink:
            self.blink -= 1
        elif random.random() < .006:
            self.blink = 5
        if not self.reduced_motion and random.random() < .0008:
            self.hop = 1.0
        self.hop = max(0.0, self.hop - .03)
        # Sitting still needs few frames, and this runs all day.
        if self.mood != "idle" or self.hop or self.blink or not self.frame % 3:
            self.update()

    def pose(self):
        """(lift, squash, tilt) for this moment, shared by Roo and any picture."""
        if self.reduced_motion:
            return 0.0, 1.0, 0.0
        now = self.frame * FRAME_MS / 1000
        if self.mood == "working":
            return 2 * abs(math.sin(now * 7)), 1 + .03 * math.sin(now * 14), 5 * math.sin(now * 7)
        if self.mood == "excited":
            return 5 * abs(math.sin(now * 6)), 1 + .06 * math.sin(now * 12), 4 * math.sin(now * 12)
        hop = math.sin(self.hop * math.pi * 3) * self.hop
        return 2 * math.sin(now * 2) + 14 * abs(hop), 1 + .015 * math.sin(now * 2) - .06 * hop, 0.0

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        side = float(self.width())
        lift, squash, tilt = self.pose()
        painter.setPen(Qt.NoPen)
        # Fully clear pixels let clicks and drops through to the desktop, so a near miss would be lost.
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
        painter.setBrush(QColor(0, 0, 0, 46))
        painter.drawEllipse(QRectF(side * .2, self.height() - side * .14, side * .6, side * .09))
        painter.translate(side / 2, self.height() - side * .1 - lift)
        painter.rotate(tilt)
        painter.scale(2 - squash, squash)
        picture = self.picture()
        if picture is not None and not picture.isNull():
            scaled = picture.size().scaled(round(side), round(self.height() * .92), Qt.KeepAspectRatio)
            painter.drawPixmap(QRectF(-scaled.width() / 2, -scaled.height(), scaled.width(), scaled.height()), picture, QRectF(picture.rect()))
        else:
            self.draw_roo(painter, side)

    def draw_roo(self, p, side):
        """A small round bird. Everything is proportional, so every size stays crisp."""
        unit = side / 118  # Room for the wings at full stretch.
        p.scale(unit, unit)
        p.setPen(QPen(QColor(BEAK), 3, Qt.SolidLine, Qt.RoundCap))
        for x in (-11, 11):
            p.drawLine(QPointF(x, -6), QPointF(x, 0))
            p.drawLine(QPointF(x, 0), QPointF(x + 5, 1))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(BODY))
        p.drawEllipse(QRectF(-36, -80, 72, 76))
        tail = QPainterPath(QPointF(32, -30))
        tail.lineTo(50, -38)
        tail.lineTo(35, -18)
        p.drawPath(tail)
        p.setBrush(QColor(BELLY))
        p.drawEllipse(QRectF(-24, -50, 48, 44))
        flap = 0.0
        if not self.reduced_motion and self.mood in ("excited", "working", "happy"):
            flap = 18 * math.sin(self.frame * .5)
        p.setBrush(QColor("#5F9273"))
        for x, turn in ((-34, flap), (34, -flap)):
            p.save()
            p.translate(x, -42)
            p.rotate(turn + (28 if x < 0 else -28) if self.mood == "excited" else turn)
            p.drawEllipse(QRectF(-8, -3, 16, 30))
            p.restore()
        p.setBrush(QColor(INK))
        pen = QPen(QColor(INK), 3, Qt.SolidLine, Qt.RoundCap)
        for x in (-14, 14):
            if self.mood == "happy":
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawArc(QRectF(x - 6, -66, 12, 11), 0, 180 * 16)
                p.setBrush(QColor(INK))
                p.setPen(Qt.NoPen)
            elif self.blink:
                p.setPen(pen)
                p.drawLine(QPointF(x - 5, -60), QPointF(x + 5, -60))
                p.setPen(Qt.NoPen)
            else:
                look = 2.5 * math.sin(self.frame * .2) if self.mood == "working" and not self.reduced_motion else 0
                p.drawEllipse(QPointF(x + look, -60), 4.5, 5 if self.mood != "excited" else 6.5)
                p.setBrush(QColor(255, 255, 255))
                p.drawEllipse(QPointF(x + look + 1.5, -62), 1.5, 1.5)
                p.setBrush(QColor(INK))
        beak = QPainterPath(QPointF(-7, -54))
        beak.lineTo(7, -54)
        beak.lineTo(0, -44 if self.mood != "excited" else -40)
        beak.closeSubpath()
        p.setBrush(QColor(BEAK))
        p.drawPath(beak)
        if self.mood == "working":
            p.setBrush(QColor(BEAK))
            for n in range(3):
                angle = (0 if self.reduced_motion else self.frame * .18) + n * 2.1
                p.drawEllipse(QPointF(42 * math.cos(angle), -84 + 6 * math.sin(angle)), 2.6, 2.6)

    def enterEvent(self, event):
        self.hop = max(self.hop, .5)

    @staticmethod
    def take_as_copy(event):
        """File Explorer defaults to a move within a drive, and removes the original if a drop agrees."""
        if not event.possibleActions() & Qt.CopyAction:
            event.ignore()
            return False
        event.setDropAction(Qt.CopyAction)
        event.accept()
        return True

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()) and self.take_as_copy(event) and self.mood != "working":
            self.set_mood("excited")

    def dragMoveEvent(self, event):
        self.take_as_copy(event)

    def dragLeaveEvent(self, event):
        if self.mood == "excited":
            self.set_mood("idle")

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths and self.take_as_copy(event):
            self.dropped.emit(paths)
        else:
            self.dragLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.pressed_at = event.globalPosition().toPoint() - self.pos()
            self.travelled = False

    def mouseMoveEvent(self, event):
        if self.pressed_at is not None and event.buttons() & Qt.LeftButton:
            target = event.globalPosition().toPoint() - self.pressed_at
            if (target - self.pos()).manhattanLength() > 3:
                self.travelled = True
            if self.travelled:
                self.move(target)
                self.bubble.hide()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.pressed_at is not None:
            self.pressed_at = None
            if self.travelled:
                self.moved.emit(self.pos())
            else:
                self.hop = 1.0
                self.clicked.emit()

    def contextMenuEvent(self, event):
        self.menu_requested.emit(event.globalPos())

    def closeEvent(self, event):
        self.bubble.close()
        super().closeEvent(event)
