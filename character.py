"""Roo, the assistant: a chibi illustration moved as layers, in the manner of Live2D.

This is not a Live2D model and uses none of its runtime. The artwork is a set of
transparent PNGs, and a few parameters move them: layers shift by their depth to
turn the head, the irises slide inside the eye whites, and a spring carries the
hair. Rig moves the parameters and Puppet paints them.
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
TURN = 11.0  # How far the head layers travel at a full turn, in artwork pixels.
NOD = 6.0
HAIR_STRIPS = 40
BEND_MARGIN = 16  # Device pixels of room for hair to swing into.
LINE = "#33211F"
MOUTH, TONGUE = "#A8423F", "#F0918C"
BLUSH = "#F58F86"
GLINT = ("#F3C569", "#F7A8B8", "#FFFFFF", "#BFE3CC")
FOLDER, FOLDER_DARK = "#F3C569", "#C98F35"


@dataclass
class Pose:
    """The parameters. Everything drawn comes from these numbers."""
    angle_x: float = 0.0  # Head turn, -1 to 1.
    angle_y: float = 0.0  # Head nod, -1 up to 1 down.
    tilt: float = 0.0  # Head lean in degrees.
    lean: float = 0.0  # Body lean in degrees.
    breath: float = 0.0
    eye_open: float = 1.0
    eye_x: float = 0.0
    eye_y: float = 0.0
    happy_eyes: float = 0.0
    mouth_open: float = 0.0
    blush: float = 0.0
    hold: float = 0.0  # 1 hugs a folder.
    sparkle: float = 0.0
    sway: float = 0.0  # Where the hair has swung to, in artwork pixels.
    clock: float = 0.0


class Puppet:
    """The artwork, scaled once for the screen it is on and then only moved."""

    def __init__(self, folder=ASSETS):
        meta = json.loads((Path(folder) / "layers.json").read_text(encoding="utf-8"))
        self.width, self.height = meta["width"], meta["height"]
        self.layers = {}
        for name, layer in meta["layers"].items():
            image = image_reader(Path(folder) / layer["file"]).read()
            self.layers[name] = (image, QRectF(layer["x"], layer["y"], image.width(), image.height()))
        self.scaled, self.bent = {}, {}
        self.pixels = 0
        rect = self.rect
        face, irises, eyes, mouth = rect("face"), rect("irides"), rect("eyewhite"), rect("mouth")
        self.pivot = QPointF(face.center().x(), face.bottom() - 6)
        self.eye_line = eyes.center().y()
        reach = irises.height() * .46
        self.eyes = (QPointF(irises.left() + reach, irises.center().y()), QPointF(irises.right() - reach, irises.center().y()))
        self.mouth = mouth.center()
        self.white_outline = QPainterPath()

    def rect(self, name):
        return self.layers[name][1]

    def prepare(self, pixels):
        """Scales everything to this many device pixels across. Moving them after that is cheap."""
        if pixels == self.pixels:
            return
        self.pixels = pixels
        scale = pixels / self.width
        self.scaled, self.bent = {}, {}
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

    def strands(self, p, name, sway, root, x=0.0, y=0.0):
        """Draws a layer in rows, each pushed further from the root, so it bends like hair.

        The rows are joined on whole pixels first. Drawn straight onto a scaled
        painter they would overlap slightly and show as lines across the hair.
        """
        key = (name, round(sway * 2))
        bent = self.bent.get(key)
        if bent is None:
            pixmap, scale = self.scaled[name], self.pixels / self.width
            bent = QPixmap(pixmap.width() + 2 * BEND_MARGIN, pixmap.height())
            bent.fill(Qt.transparent)
            with_rows = QPainter(bent)
            with_rows.setRenderHint(QPainter.SmoothPixmapTransform)
            rows = min(HAIR_STRIPS, pixmap.height())
            for row in range(rows):
                top, bottom = pixmap.height() * row // rows, pixmap.height() * (row + 1) // rows
                reach = max(0.0, (top / pixmap.height() - root) / (1 - root))
                shift = max(-BEND_MARGIN, min(BEND_MARGIN, key[1] / 2 * scale * reach * reach))
                with_rows.drawPixmap(QRectF(BEND_MARGIN + shift, top, pixmap.width(), bottom - top), pixmap, QRectF(0, top, pixmap.width(), bottom - top))
            with_rows.end()
            self.bent[key] = bent
        rect = self.rect(name).translated(x, y)
        margin = BEND_MARGIN * rect.width() / self.scaled[name].width()
        p.drawPixmap(rect.adjusted(-margin, 0, margin, 0), bent, QRectF(bent.rect()))

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

    def folder(self, p, pose):
        """Roo hugging the file she has been handed."""
        strength = min(1.0, pose.hold * 1.3)
        body = self.rect("body")
        centre = QPointF(body.center().x(), body.top() + body.height() * .52 + (1 - pose.hold) * 16)
        p.save()
        p.setOpacity(strength)
        p.translate(centre)
        p.rotate(-3 + pose.angle_x * 3)
        p.setPen(QPen(QColor(LINE), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(FOLDER_DARK))
        p.drawRoundedRect(QRectF(-78, -68, 58, 28), 8, 8)
        p.drawRoundedRect(QRectF(-78, -52, 156, 104), 10, 10)
        p.setBrush(QColor("#FFFFFF"))
        p.drawRoundedRect(QRectF(-64, -62, 128, 70), 5, 5)
        p.setPen(QPen(QColor("#C7D6CC"), 5, Qt.SolidLine, Qt.RoundCap))
        for y, end in ((-46, 44), (-30, 50), (-14, 24)):
            p.drawLine(QPointF(-48, y), QPointF(end, y))
        p.setPen(QPen(QColor(LINE), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(FOLDER))
        p.drawRoundedRect(QRectF(-78, -20, 156, 72), 10, 10)
        p.restore()

    def glints(self, p, pose):
        """Little stars around her head when she is pleased."""
        if pose.sparkle < .05:
            return
        face = self.rect("face")
        for index, (x, y) in enumerate(((-.62, -.12), (.66, .02), (-.5, .62), (.62, .66))):
            twinkle = .5 + .5 * math.sin(pose.clock * 5 + index * 1.9)
            size = (7 + 9 * twinkle) * min(pose.sparkle, 1)
            centre = face.center() + QPointF(x * face.width(), y * face.height())
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
        ax, ay = pose.angle_x * TURN, pose.angle_y * NOD
        feet = QPointF(self.width / 2, self.height)
        p.translate(feet)
        p.rotate(pose.lean)
        p.translate(-feet)
        lift = pose.breath * 1.8
        head_at = lambda: (p.translate(self.pivot), p.rotate(pose.tilt), p.translate(-self.pivot))
        p.save()
        head_at()
        self.strands(p, "back hair", pose.sway, .22, -ax * .55, -ay * .4 - lift * .4)
        p.restore()
        p.save()
        p.translate(feet)
        p.scale(1, 1 + pose.breath * .006)
        p.translate(-feet)
        self.put(p, "body")
        if pose.hold > .02:
            self.folder(p, pose)
            p.setOpacity(min(1.0, pose.hold * 1.3))
            self.put(p, "handwear")
            p.setOpacity(1)
        p.restore()
        p.save()
        head_at()
        p.translate(0, -lift)
        self.put(p, "ears", -ax * .3, ay * .2)
        self.put(p, "face", ax * .22, ay * .22)
        self.cheeks(p, pose, ax * .9, ay * .9)
        self.eyes_(p, pose, ax, ay)
        self.put(p, "eyebrow", ax * 1.05, ay + (pose.eye_open - 1) * -22 - pose.happy_eyes * 3)
        self.mouth_(p, pose, ax * 1.08, ay * 1.08)
        self.strands(p, "front hair", pose.sway * .6, .52, ax * 1.2, ay * .9)
        self.strands(p, "headwear", pose.sway * .3, .0, ax * 1.1, ay * .9)
        p.restore()
        self.glints(p, pose)
        p.restore()


# What each mood asks of the parameters. Rig eases towards these, so moods blend.
_MOODS = {
    "idle": Pose(),
    "excited": Pose(eye_open=1.1, mouth_open=.75, blush=.9, sparkle=1),
    "working": Pose(angle_y=.7, eye_y=.9, hold=1),
    "happy": Pose(happy_eyes=1, mouth_open=.6, blush=1, sparkle=.8),
}
_EASED = [field.name for field in fields(Pose) if field.name not in ("breath", "sway", "eye_open", "clock")]


class Rig:
    """Moves the parameters over time: eased moods, breathing, blinking, gaze and the hair's spring."""

    def __init__(self, reduced_motion=False):
        self.pose = replace(_MOODS["idle"])
        self.reduced_motion = reduced_motion
        self.velocity = 0.0
        self.next_blink = 2.5
        self.blinking = 0.0
        self.startle = 0.0

    def poke(self):
        self.startle = 1.0

    def step(self, seconds, mood="idle", look=(0.0, 0.0), talking=False):
        pose, goal = self.pose, _MOODS.get(mood, _MOODS["idle"])
        wanted = {name: getattr(goal, name) for name in _EASED}
        if self.reduced_motion:
            for name, value in wanted.items():
                setattr(pose, name, value)
            pose.eye_open, pose.breath, pose.sway = goal.eye_open, 0.0, 0.0
            return pose
        pose.clock += seconds
        wanted["eye_x"] = look[0]
        wanted["eye_y"] = goal.eye_y if mood == "working" else look[1] * .7
        wanted["angle_x"], wanted["angle_y"] = look[0] * .8, goal.angle_y + look[1] * .4
        wanted["tilt"] = look[0] * 2.5
        turned = pose.angle_x
        rate = 1 - math.exp(-seconds * 9)
        for name, value in wanted.items():
            setattr(pose, name, getattr(pose, name) + (value - getattr(pose, name)) * rate)
        pose.breath = math.sin(pose.clock * 1.9)
        pose.lean = math.sin(pose.clock * .7) * .9
        if mood == "working":  # Reading down the page.
            pose.eye_x = math.sin(pose.clock * 3.2) * .7
        elif mood == "excited":
            pose.lean += math.sin(pose.clock * 13) * 1.6
        elif mood == "happy":
            pose.tilt += math.sin(pose.clock * 6) * 3
        if talking:
            pose.mouth_open = max(pose.mouth_open, .16 + .3 * abs(math.sin(pose.clock * 13)))
        self.blink(seconds, goal)
        # Head movement and a light breeze push the hair, and the spring carries it on.
        push = -(pose.angle_x - turned) / max(seconds, 1e-3) * 7 + math.sin(pose.clock * 1.1) * 2.2
        self.velocity += ((push - pose.sway) * 34 - self.velocity * 6.5) * seconds
        pose.sway = max(-20.0, min(20.0, pose.sway + self.velocity * seconds))
        self.startle = max(0.0, self.startle - seconds * 2.4)
        return pose

    def blink(self, seconds, goal):
        self.next_blink -= seconds
        if self.next_blink <= 0:
            self.blinking, self.next_blink = .16, random.uniform(2.2, 5.8)
        self.blinking = max(0.0, self.blinking - seconds)
        wide = goal.eye_open + self.startle * .12
        self.pose.eye_open = wide * (abs(self.blinking / .08 - 1) if self.blinking else 1)
