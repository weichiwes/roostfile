"""Roo, the assistant: one whole painting of her for each pose, with a living face.

Nothing of her is cut apart. Each key in assets/roo is a full picture, painted with her
eyes and mouth left out, and the eyes and mouth go on top so she can blink, look about and
talk. The whole picture warps for a head turn, breathes, leans, and swings from her collar
when she is held up. An action is a few keys shown in turn. Rig moves the parameters and
Puppet paints them. This is not a Live2D model and uses none of its runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
import json
import math
from pathlib import Path
import random

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBitmap, QColor, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient, QRegion, QTransform

from pages import image_reader

ASSETS = Path(__file__).resolve().parent / "assets" / "roo"
TURN = 17.0  # How far the middle of her face travels at a full turn, in artwork pixels.
NOD = 10.0
TURN_STEPS, NOD_STEPS = 12, 6  # Warped layers are kept, so the angles come in steps too small to see.
WARP_ROWS, WARP_COLUMNS = 56, 20
WARP_ROOM = 14  # Artwork pixels around a warped layer, for what moves past its edge.
HEAD_REACH = (138.0, 125.0)  # The warp fades to nothing here, so the outline of her head stays put.
CACHED = 180  # Warped layers kept at once.
LINE = "#33211F"
MOUTH, TONGUE = "#A8423F", "#F0918C"
BLUSH = "#F58F86"
GLINT = ("#F3C569", "#F7A8B8", "#FFFFFF", "#BFE3CC")
MAX_SWING = 30  # Degrees, however hard she is shaken.
ARM_EASE = 5.0  # Her arms are eased twice, so each painting stays up long enough to read.
ARMS = ("hold", "wave", "stretch", "book", "write", "ball", "cup")
BOUNCE = 4.2
BALL_X, BALL_RISE = 362.0, 66.0  # Artwork pixels: where the ball bounces, and how high.


@dataclass
class Pose:
    """The parameters. Everything drawn comes from these numbers."""
    angle_x: float = 0.0  # Head turn, -1 to 1.
    angle_y: float = 0.0  # Head nod, -1 up to 1 down.
    lean: float = 0.0  # Body lean in degrees.
    breath: float = 0.0
    eye_open: float = 1.0
    eye_x: float = 0.0
    eye_y: float = 0.0
    happy_eyes: float = 0.0
    mouth_open: float = 0.0
    blush: float = 0.0
    hold: float = 0.0  # 1 hugs a folder.
    wave: float = 0.0  # 1 has her arm up in greeting.
    stretch: float = 0.0
    book: float = 0.0
    write: float = 0.0
    ball: float = 0.0
    cup: float = 0.0
    nap: float = 0.0
    lifted: float = 0.0  # 1 hangs from the back of her collar.
    swing: float = 0.0  # Degrees, swinging from the hand that holds her.
    sparkle: float = 0.0
    clock: float = 0.0


class Puppet:
    """The artwork, scaled once for the screen it is on and then only moved."""

    def __init__(self, folder=ASSETS):
        meta = json.loads((Path(folder) / "layers.json").read_text(encoding="utf-8"))
        self.width, self.height = meta["width"], meta["height"]
        self.layers = {}
        for name, layer in meta["layers"].items():
            image = image_reader(Path(folder) / layer["file"]).read()
            self.layers[name] = (image, QRectF(layer["x"], layer["y"], layer.get("width", image.width()), layer.get("height", image.height())))
        self.scaled, self.warps = {}, {}
        self.pixels = 0
        rect = self.rect
        irises, eyes, mouth = rect("irides"), rect("eyewhite"), rect("mouth")
        self.face = QRectF(*meta["face"])
        self.pivot = QPointF(*meta["pivot"])
        self.head = QPointF(*meta["head"])
        self.eye_line = eyes.center().y()
        reach = irises.height() * .46
        self.eyes = (QPointF(irises.left() + reach, irises.center().y()), QPointF(irises.right() - reach, irises.center().y()))
        self.mouth = mouth.center()
        self.grip = QPointF(*meta["collar"])  # Where she is held: the pointer pinches her here.
        self.white_outline = QPainterPath()

    def rect(self, name):
        return self.layers[name][1]

    def prepare(self, pixels):
        """Scales everything to this many device pixels across. Moving them after that is cheap."""
        if pixels == self.pixels:
            return
        self.pixels = pixels
        scale = pixels / self.width
        self.scaled, self.warps = {}, {}
        for name, (image, rect) in self.layers.items():
            size = (max(1, round(rect.width() * scale)), max(1, round(rect.height() * scale)))
            self.scaled[name] = QPixmap.fromImage(image.scaled(*size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        # The irises must stay inside the whites, so the whites' shape becomes a clip.
        whites = self.scaled["eyewhite"]
        rect = self.rect("eyewhite")
        outline = QPainterPath()
        outline.addRegion(QRegion(QBitmap(whites.mask())))
        self.white_outline = QTransform(rect.width() / whites.width(), 0, 0, rect.height() / whites.height(), rect.x(), rect.y()).map(outline)

    def put(self, p, name, x=0.0, y=0.0):
        p.drawPixmap(self.rect(name).translated(x, y), self.scaled[name], QRectF(self.scaled[name].rect()))

    def weight(self, x, y):
        """How much of a head turn moves this point of the artwork: all of it mid-face, none at her outline."""
        across = min(1.0, abs(x - self.head.x()) / HEAD_REACH[0])
        down = min(1.0, abs(y - self.head.y()) / HEAD_REACH[1])
        return (math.cos(across * math.pi / 2) * math.cos(down * math.pi / 2)) ** 2

    def moved(self, point, turn, nod):
        """Where a turn and nod carry a point. Everything on her head follows this, so nothing slips."""
        reach = self.weight(point.x(), point.y())
        return reach * turn / TURN_STEPS * TURN, reach * nod / NOD_STEPS * NOD

    def warped(self, name, turn, nod):
        """A layer curved around her head, drawn as stretched pieces, and kept for next time."""
        pixmap, rect = self.scaled[name], self.rect(name)
        if not turn and not nod:
            return pixmap, rect
        key = (name, turn, nod)
        kept = self.warps.get(key)
        if kept is None:
            scale_x, scale_y = pixmap.width() / rect.width(), pixmap.height() / rect.height()
            room_x, room_y = round(WARP_ROOM * scale_x), round(WARP_ROOM * scale_y)
            bent = QPixmap(pixmap.width() + 2 * room_x, pixmap.height() + 2 * room_y)
            bent.fill(Qt.transparent)
            with_rows = QPainter(bent)
            with_rows.setRenderHint(QPainter.SmoothPixmapTransform)
            rows, columns = min(WARP_ROWS, pixmap.height()), min(WARP_COLUMNS, pixmap.width())
            art_x = lambda x: rect.x() + x / scale_x
            art_y = lambda y: rect.y() + y / scale_y
            shifted = lambda y: round(y + self.moved(QPointF(self.head.x(), art_y(y)), turn, nod)[1] * scale_y)
            for row in range(rows):
                top, bottom = pixmap.height() * row // rows, pixmap.height() * (row + 1) // rows
                line = (top + bottom) / 2
                knots = [pixmap.width() * column // columns for column in range(columns + 1)]
                spots = [round(x + self.moved(QPointF(art_x(x), art_y(line)), turn, nod)[0] * scale_x) for x in knots]
                upper, lower = shifted(top) + room_y, shifted(bottom) + room_y
                for left, right, near, far in zip(knots, knots[1:], spots, spots[1:]):
                    if far > near and lower > upper:  # Whole pixels, so the pieces meet with no seams.
                        with_rows.drawPixmap(QRectF(near + room_x, upper, far - near, lower - upper), pixmap, QRectF(left, top, right - left, bottom - top))
            with_rows.end()
            kept = bent, rect.adjusted(-room_x / scale_x, -room_y / scale_y, room_x / scale_x, room_y / scale_y)
            self.remember(self.warps, key, kept)
        return kept

    def remember(self, cache, key, value):
        if len(cache) >= CACHED:
            del cache[next(iter(cache))]
        cache[key] = value

    def put_warped(self, p, name, turn, nod):
        pixmap, rect = self.warped(name, turn, nod)
        p.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))

    def eyes_(self, p, pose, dx, dy):
        openness = pose.eye_open * (1 - pose.happy_eyes)
        if openness < .28:
            for centre in self.eyes:
                self.shut(p, centre + QPointF(dx, dy), pose.happy_eyes > .5)
            return
        p.save()
        p.translate(0, self.eye_line + dy)
        p.scale(1, min(openness, 1.12))
        p.translate(0, -self.eye_line - dy)
        self.put(p, "eyewhite", dx, dy)
        p.save()
        p.setClipPath(self.white_outline.translated(dx, dy))
        self.put(p, "irides", dx + pose.eye_x * 7, dy + pose.eye_y * 5)
        p.restore()
        if pose.sparkle > .05:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, int(235 * min(pose.sparkle, 1))))
            for centre in self.eyes:
                p.drawEllipse(centre + QPointF(dx + 5, dy + 6), 2.6, 2.6)
        self.put(p, "eyelash", dx, dy)
        p.restore()

    def shut(self, p, centre, smiling):
        """Her eyes when closed: a line for a blink, an arch for a smile."""
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(LINE), 5.5, Qt.SolidLine, Qt.RoundCap))
        arc = QPainterPath(centre + QPointF(-22, 6 if smiling else 2))
        arc.quadTo(centre + QPointF(0, -22 if smiling else 8), centre + QPointF(22, 6 if smiling else 2))
        p.drawPath(arc)

    def mouth_(self, p, pose, dx, dy):
        if pose.mouth_open < .1:
            self.put(p, "mouth", dx, dy)
            return
        centre = self.mouth + QPointF(dx, dy - 2)
        width, height = 15.0, 5 + pose.mouth_open * 17
        mouth = QPainterPath(centre + QPointF(-width, 0))
        mouth.quadTo(centre + QPointF(0, 3), centre + QPointF(width, 0))
        mouth.quadTo(centre + QPointF(width * .8, height), centre + QPointF(0, height))
        mouth.quadTo(centre + QPointF(-width * .8, height), centre + QPointF(-width, 0))
        p.setBrush(QColor(MOUTH))
        p.setPen(QPen(QColor(LINE), 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(mouth)
        p.save()
        p.setClipPath(mouth)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(TONGUE))
        p.drawEllipse(centre + QPointF(0, height), width * .68, height * .5)
        p.restore()

    def cheeks(self, p, pose, dx, dy):
        if pose.blush < .03:
            return
        for centre in self.eyes:
            spot = QPointF(centre.x() + (-20 if centre.x() < self.pivot.x() else 20) + dx, self.mouth.y() - 22 + dy)
            colour = QColor(BLUSH)
            colour.setAlpha(int(165 * min(pose.blush, 1)))
            shade = QRadialGradient(spot, 30)
            shade.setColorAt(0, colour)
            colour.setAlpha(0)
            shade.setColorAt(1, colour)
            p.setPen(Qt.NoPen)
            p.setBrush(shade)
            p.drawEllipse(spot, 31, 19)

    @staticmethod
    def sip(pose):
        return max(0.0, math.sin(pose.clock * .95)) ** 3

    def key(self, pose):
        """Which painting this pose shows. Held up comes first: she can be picked up mid-action."""
        if pose.lifted > .3:
            return "key lifted swing" if abs(pose.swing) > 7 else "key lifted"
        if pose.hold > .05:
            return "key hold hug" if pose.hold > .6 else "key hold low"
        if pose.book > .05:
            if pose.book < .6:
                return "key read low"
            return "key read turn" if pose.clock / 4 % 1 < .16 else "key read up"
        if pose.write > .05:
            if pose.write < .6:
                return "key write low"
            return "key write a" if math.sin(pose.clock * 3) > 0 else "key write b"
        if pose.cup > .05:
            if pose.cup < .6:
                return "key tea low"
            return "key tea sip" if self.sip(pose) > .35 else "key tea hold"
        if pose.stretch > .05:
            return "key stretch top" if pose.stretch > .6 else "key stretch mid"
        if pose.ball > .2:
            return "key ball down" if abs(math.sin(pose.clock * BOUNCE)) > .72 else "key ball up"
        if pose.nap > .3:
            return "key nap"
        if pose.wave > .9:  # Up: her arm swings between the two highest paintings.
            return "key wave top" if math.sin(pose.clock * 7) > -.2 else "key wave high"
        for stop, name in ((.72, "key wave high"), (.48, "key wave side"), (.2, "key wave low")):
            if pose.wave > stop:
                return name
        return "key down"

    def bounce(self, p, pose):
        """The ball under her hand, leaving the floor and coming back to her palm."""
        height = abs(math.sin(pose.clock * BOUNCE))
        floor = QPointF(BALL_X, self.height - 6)
        p.save()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawEllipse(floor, 28 - height * 10, 7 - height * 2)
        p.translate(floor.x(), floor.y() - 30 - height * BALL_RISE)
        p.scale(1 + (1 - height) ** 6 * .16, 1 - (1 - height) ** 6 * .16)
        rect = self.rect("prop ball")
        p.drawPixmap(QRectF(-rect.width() / 2, -rect.height() / 2, rect.width(), rect.height()), self.scaled["prop ball"], QRectF(self.scaled["prop ball"].rect()))
        p.restore()

    def snooze(self, p, pose):
        """Letters drifting up while she dozes."""
        p.save()
        p.setPen(QPen(QColor(LINE), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        for index in range(3):
            rise = (pose.clock * .32 + index / 3) % 1
            size = 11 + rise * 14
            p.setOpacity(min(1.0, pose.nap * 1.2) * math.sin(rise * math.pi))
            at = QPointF(self.face.right() + 8 + rise * 34, self.face.top() + 54 - rise * 112)
            letter = QPainterPath(at + QPointF(-size, -size))
            letter.lineTo(at + QPointF(size, -size))
            letter.lineTo(at + QPointF(-size, size))
            letter.lineTo(at + QPointF(size, size))
            p.drawPath(letter)
        p.restore()

    def sweat(self, p, pose):
        drop = QPainterPath(QPointF(0, -16))
        drop.quadTo(QPointF(13, 4), QPointF(0, 9))
        drop.quadTo(QPointF(-13, 4), QPointF(0, -16))
        p.save()
        p.setOpacity(min(1.0, pose.lifted))
        p.translate(self.face.left() + 16, self.face.top() + 92 + math.sin(pose.clock * 5) * 4)
        p.setPen(QPen(QColor(LINE), 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor("#BFE0F2"))
        p.drawPath(drop)
        p.restore()

    def glints(self, p, pose):
        """Little stars around her head when she is pleased."""
        if pose.sparkle < .05:
            return
        for index, (x, y) in enumerate(((-.62, -.12), (.66, .02), (-.5, .62), (.62, .66))):
            twinkle = .5 + .5 * math.sin(pose.clock * 5 + index * 1.9)
            size = (7 + 9 * twinkle) * min(pose.sparkle, 1)
            centre = self.face.center() + QPointF(x * self.face.width(), y * self.face.height())
            star = QPainterPath(centre + QPointF(0, -size))
            for turn in (QPointF(size, 0), QPointF(0, size), QPointF(-size, 0), QPointF(0, -size)):
                star.quadTo(centre, centre + turn)
            p.setPen(QPen(QColor(LINE), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(QColor(GLINT[index]))
            p.drawPath(star)

    def draw(self, p, width, pose, device_ratio=1.0):
        """Paints her with her feet at the origin, `width` logical pixels across."""
        self.prepare(round(width * device_ratio))
        scale = width / self.width
        p.save()
        p.scale(scale, scale)
        p.translate(-self.width / 2, -self.height)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        turn, nod = round(pose.angle_x * TURN_STEPS), round(pose.angle_y * NOD_STEPS)
        feet = QPointF(self.width / 2, self.height)
        p.translate(feet)
        p.rotate(pose.lean)
        p.scale(1, 1 + pose.breath * .006)
        p.translate(-feet)
        p.translate(self.grip)
        p.rotate(pose.swing * pose.lifted)
        p.translate(-self.grip)
        key = self.key(pose)
        self.put_warped(p, key, turn, nod)
        eye = self.moved(QPointF(self.pivot.x(), self.eye_line), turn, nod)
        mouth = self.moved(self.mouth, turn, nod)
        self.cheeks(p, pose, *eye)
        self.eyes_(p, pose, *eye)
        if key != "key tea sip":  # There the cup is at her lips, and the painting already shows it.
            self.mouth_(p, pose, *mouth)
        if pose.ball > .2:
            self.bounce(p, pose)
        if pose.nap > .02:
            self.snooze(p, pose)
        self.glints(p, pose)
        if pose.lifted > .02:
            self.sweat(p, pose)
        p.restore()


# What each mood asks of the parameters. Rig eases towards these, so moods blend.
_MOODS = {
    "idle": Pose(),
    "excited": Pose(eye_open=1.1, mouth_open=.75, blush=.9, sparkle=1),
    "working": Pose(angle_y=.7, eye_y=.9, hold=1),
    "happy": Pose(happy_eyes=1, mouth_open=.6, blush=1, sparkle=.8),
    "greeting": Pose(happy_eyes=1, mouth_open=.6, blush=1, sparkle=.8, wave=1),
    "stretching": Pose(happy_eyes=1, mouth_open=.5, blush=.5, stretch=1, angle_y=-.5),
    "reading": Pose(angle_y=.6, eye_y=.9, book=1),
    "homework": Pose(angle_y=.8, eye_y=1, write=1, blush=.2),
    "playing": Pose(blush=.5, mouth_open=.3, sparkle=.3, ball=1),
    "tea": Pose(blush=.5, cup=1),
    "napping": Pose(eye_open=0, angle_y=.7, blush=.4, nap=1),
    "dangling": Pose(eye_open=1.12, mouth_open=.28, blush=.7, angle_y=.3, lifted=1),
}
_EASED = [field.name for field in fields(Pose) if field.name not in ("breath", "swing", "eye_open", "clock")]


class Rig:
    """Moves the parameters over time: eased moods, breathing, blinking, gaze and the swing when held."""

    def __init__(self, reduced_motion=False):
        self.pose = replace(_MOODS["idle"])
        self.reduced_motion = reduced_motion
        self.reaching = {name: 0.0 for name in ARMS}
        self.swinging = 0.0
        self.next_blink = 2.5
        self.blinking = 0.0
        self.startle = 0.0

    def poke(self):
        self.startle = 1.0

    def step(self, seconds, mood="idle", look=(0.0, 0.0), talking=False, pull=0.0):
        """`pull` is how fast whoever holds her is moving sideways, in pixels a second."""
        pose, goal = self.pose, _MOODS.get(mood, _MOODS["idle"])
        wanted = {name: getattr(goal, name) for name in _EASED}
        if self.reduced_motion:
            for name, value in wanted.items():
                setattr(pose, name, value)
            self.reaching = {name: wanted[name] for name in ARMS}
            pose.eye_open, pose.breath, pose.swing = goal.eye_open, 0.0, 0.0
            return pose
        pose.clock += seconds
        wanted["eye_x"] = look[0]
        wanted["eye_y"] = goal.eye_y if mood == "working" else look[1] * .7
        wanted["angle_x"], wanted["angle_y"] = look[0] * .8, goal.angle_y + look[1] * .4
        rate, arm_rate = 1 - math.exp(-seconds * 9), 1 - math.exp(-seconds * ARM_EASE)
        for name in ARMS:
            self.reaching[name] += (wanted.pop(name) - self.reaching[name]) * rate
            setattr(pose, name, getattr(pose, name) + (self.reaching[name] - getattr(pose, name)) * arm_rate)
        for name, value in wanted.items():
            setattr(pose, name, getattr(pose, name) + (value - getattr(pose, name)) * rate)
        pose.breath = math.sin(pose.clock * 1.9)
        pose.lean = math.sin(pose.clock * .7) * .9
        if mood == "working":  # Reading down the page.
            pose.eye_x = math.sin(pose.clock * 3.2) * .7
        elif mood == "excited":
            pose.lean += math.sin(pose.clock * 13) * 1.6
        elif mood == "reading":
            pose.eye_x = math.sin(pose.clock * 1.7) * .6
        elif mood == "homework":
            pose.eye_x = math.sin(pose.clock * 2.4) * .4
        elif mood == "playing":
            pose.eye_y = .8 - abs(math.sin(pose.clock * BOUNCE)) * 1.1
            pose.eye_x = .7
        if talking:
            pose.mouth_open = max(pose.mouth_open, .16 + .3 * abs(math.sin(pose.clock * 13)))
        self.blink(seconds, goal)
        # Held up, she swings like a pendulum, and the pull sends her trailing behind the hand.
        self.swinging += ((max(-24.0, min(24.0, -pull * .045)) - pose.swing) * 42 - self.swinging * 2.4) * seconds
        pose.swing = max(-MAX_SWING, min(MAX_SWING, pose.swing + self.swinging * seconds))
        self.startle = max(0.0, self.startle - seconds * 2.4)
        return pose

    def blink(self, seconds, goal):
        self.next_blink -= seconds
        if self.next_blink <= 0:
            self.blinking, self.next_blink = .16, random.uniform(2.2, 5.8)
        self.blinking = max(0.0, self.blinking - seconds)
        wide = goal.eye_open + self.startle * .12
        self.pose.eye_open = wide * (abs(self.blinking / .08 - 1) if self.blinking else 1)
