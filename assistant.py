"""Roo, the small desktop assistant: a drop target with a bit of life in it.

She is the puppet in character.py, and any picture can replace her. Hops and
bounces apply to every look, so a person's own image feels alive too. Name a
folder's pictures idle, excited, working and happy to give each mood its own.
"""
from __future__ import annotations

import math
import random
import shutil
from pathlib import Path

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QMovie, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QWidget

from character import Puppet, Rig
from core import _assert_local_path
from pages import image_reader
from theme import BG, HAIRLINE_STRONG, TEXT

MOODS = ("idle", "excited", "working", "happy")
SIZES = {"small": 72, "medium": 96, "large": 128}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
FRAME_MS = 33
# What she gets up to on her own, and for how many seconds.
PASTIMES = {"reading": 24, "homework": 24, "playing": 12, "tea": 14, "stretching": 3}
PLAY_AFTER = (15, 40)  # Seconds between one thing and the next.
NAP_AFTER = 10 * 60  # Seconds nobody has touched her.
GAZE_SOFTNESS = 260  # Logical pixels. Her eyes lean towards the pointer rather than snap to it.
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
        self.puppet = Puppet()
        self.rig = Rig(reduced_motion)
        self.frame = 0
        self.talk_until = 0
        self.hop = 0.0
        self.pressed_at = None
        self.travelled = False
        self.lifting = False
        self.margin = QPoint(0, 0)  # While she is held her window grows, so she can swing.
        self.grip = QPoint(0, 0)
        self.pull = 0.0
        self.last_x = 0
        self.after_lift = "idle"
        self.plays = True
        self.pinned = False
        self.last_touch = 0
        self.last_pastime = ""
        self.next_play = random.uniform(*PLAY_AFTER) * 1000 / FRAME_MS
        self.setCursor(Qt.OpenHandCursor)
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

    @property
    def reduced_motion(self):
        return self.rig.reduced_motion

    @reduced_motion.setter
    def reduced_motion(self, value):
        self.rig.reduced_motion = value

    def set_size(self, name):
        self.size_name = name if name in SIZES else "medium"
        side = SIZES[self.size_name]
        self.setFixedSize(side, round(side * 1.25))

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
        if self.lifting and mood != "dangling":
            self.after_lift = mood  # She can only hang there until she is put down.
            return
        self.mood = mood
        self.mood_timer.stop()
        if seconds:
            self.mood_timer.start(seconds * 1000)
        if mood in ("happy", "greeting"):
            self.hop = 1.0
        if self.reduced_motion:
            self.rig.step(0, mood)
        self.update()

    def say(self, text, seconds=5):
        if self.isVisible():  # Hidden for a game or a presentation, so it keeps quiet too.
            self.bubble.say(text, QRectF(self.geometry()), seconds)
            self.talk_until = self.frame + min(90, 12 + len(text) * 2)

    def tick(self):
        if not self.isVisible():
            return
        self.frame += 1
        if self.lifting and not QApplication.mouseButtons() & Qt.LeftButton:
            self.pressed_at = None  # The release went elsewhere, such as during Alt Tab. She must not hang there.
            self.put_down()
        if not self.reduced_motion and random.random() < .0008:
            self.hop = 1.0
        self.hop = max(0.0, self.hop - .03)
        talking = self.frame < self.talk_until
        self.pull += ((self.x() - self.last_x) * 1000 / FRAME_MS - self.pull) * .35 if self.lifting else -self.pull
        self.last_x = self.x()
        pose = self.rig.step(FRAME_MS / 1000, self.mood, self.gaze(), talking, self.pull)
        self.maybe_play()
        # Sitting still needs few frames, and this runs all day.
        calm = self.mood == "idle" or self.mood in PASTIMES or self.mood == "napping"
        if not calm or self.hop or talking or pose.eye_open < 1 or not self.frame % 2:
            self.update()

    def maybe_play(self):
        """Left alone, she finds something to do where she stands. This is a timer: it watches nothing."""
        if self.mood != "idle" or not self.plays or self.custom or self.reduced_motion or self.frame < self.next_play:
            return
        if (self.frame - self.last_touch) * FRAME_MS / 1000 > NAP_AFTER:
            self.set_mood("napping")
            return
        self.last_pastime = random.choice([name for name in PASTIMES if name != self.last_pastime])
        seconds = PASTIMES[self.last_pastime]
        self.set_mood(self.last_pastime, seconds)
        self.next_play = self.frame + (seconds + random.uniform(*PLAY_AFTER)) * 1000 / FRAME_MS

    def touched(self):
        """Someone has noticed her, so she looks up from whatever she was doing."""
        self.last_touch = self.frame
        self.next_play = self.frame + random.uniform(*PLAY_AFTER) * 1000 / FRAME_MS
        if self.mood == "napping":
            self.set_mood("stretching", PASTIMES["stretching"])
        elif self.mood in PASTIMES:
            self.set_mood("idle")

    def gaze(self):
        """Which way the pointer is, from -1 to 1, so her eyes can follow it. It is not kept."""
        if self.reduced_motion:
            return 0.0, 0.0
        offset = QCursor.pos() - self.geometry().center()
        return offset.x() / (abs(offset.x()) + GAZE_SOFTNESS), offset.y() / (abs(offset.y()) + GAZE_SOFTNESS)

    def pose(self):
        """(lift, squash, tilt) for this moment, shared by Roo and any picture."""
        if self.reduced_motion or self.lifting:
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
        side = float(SIZES[self.size_name])
        height = round(side * 1.25)
        lift, squash, tilt = self.pose()
        painter.setPen(Qt.NoPen)
        # Fully clear pixels let clicks and drops through to the desktop, so a near miss would be lost.
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
        painter.translate(self.margin)
        if not self.lifting:
            painter.setBrush(QColor(0, 0, 0, 46))
            painter.drawEllipse(QRectF(side * .24, height - side * .1, side * .52, side * .08))
        picture = self.picture()
        drawn = picture is not None and not picture.isNull()
        if not drawn:  # She leans and breathes for herself, so the hops stay small.
            lift, squash, tilt = lift * side / 110, 1 + (squash - 1) * .6, 0.0
        painter.translate(side / 2, height - side * .06 - lift)
        painter.rotate(tilt)
        painter.scale(2 - squash, squash)
        if drawn:
            scaled = picture.size().scaled(round(side), round(height * .92), Qt.KeepAspectRatio)
            painter.drawPixmap(QRectF(-scaled.width() / 2, -scaled.height(), scaled.width(), scaled.height()), picture, QRectF(picture.rect()))
        else:
            self.puppet.draw(painter, side * .98, self.rig.pose, self.devicePixelRatioF())

    def enterEvent(self, event):
        self.hop = max(self.hop, .5)
        self.touched()

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
            self.touched()
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
            self.grip = self.pressed_at
            self.travelled = False
            self.touched()

    def mouseMoveEvent(self, event):
        if self.pressed_at is None or self.pinned or not event.buttons() & Qt.LeftButton:
            return
        where = event.globalPosition().toPoint()
        if not self.travelled:
            if (where - self.pos() - self.pressed_at).manhattanLength() <= 3:
                return
            self.travelled = True
            self.bubble.hide()
            self.pick_up()
        self.move(where - self.grip)

    def pick_up(self):
        """By her collar. A picture has no collar, so it just follows the pointer."""
        if self.custom:
            return
        side = SIZES[self.size_name]
        height = round(side * 1.25)
        scale = side * .98 / self.puppet.width
        at = self.puppet.grip
        self.margin = QPoint(round(side * .5), round(side * .6))
        self.after_lift = "working" if self.mood == "working" else "idle"
        self.mood_timer.stop()
        self.setFixedSize(side + self.margin.x() * 2, height + self.margin.y() + round(side * .3))
        self.grip = self.margin + QPoint(round(side / 2 + (at.x() - self.puppet.width / 2) * scale),
                                         round(height - side * .06 + (at.y() - self.puppet.height) * scale))
        self.set_mood("dangling")
        self.lifting = True
        self.last_x = self.x()
        self.setCursor(Qt.ClosedHandCursor)  # The pointer is the hand that holds her.

    def put_down(self):
        if self.lifting:
            spot = self.pos() + self.margin
            self.lifting = False
            self.margin = QPoint(0, 0)
            self.set_size(self.size_name)
            self.move(spot)
            self.setCursor(Qt.OpenHandCursor)
            self.set_mood(self.after_lift) if self.after_lift != "idle" else self.set_mood("happy", 1)
        self.moved.emit(self.pos())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.pressed_at is not None:
            self.pressed_at = None
            if self.travelled:
                self.put_down()
            else:
                self.hop = 1.0
                self.rig.poke()
                self.clicked.emit()

    def contextMenuEvent(self, event):
        self.menu_requested.emit(event.globalPos())

    def closeEvent(self, event):
        self.bubble.close()
        super().closeEvent(event)
