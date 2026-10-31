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
SLEEVE, HAND = "#FBF1DC", "#FFE9DD"
BROW, BROW_Y = "#3C6652", 152
COVER, BALL = "#4F8A6E", "#E9A94B"
ARM_LENGTH = 92
MAX_SWING = 30  # Degrees, however hard she is shaken.
GRIP_ABOVE = 58  # Artwork pixels from the top of her hair up to the pinch.


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
        self.grip = QPointF(self.pivot.x(), rect("front hair").top() - GRIP_ABOVE)
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

    def brows(self, p, pose, dx, dy):
        """Drawn over the fringe, as anime does, or she looks as if she has none."""
        raised = (pose.eye_open - 1) * 40 + pose.happy_eyes * 5
        worry = pose.lifted * 7
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(BROW), 5, Qt.SolidLine, Qt.RoundCap))
        for centre in self.eyes:
            side = -1 if centre.x() < self.pivot.x() else 1
            middle = QPointF(centre.x() + dx + side * 2, BROW_Y + dy - raised)
            brow = QPainterPath(middle + QPointF(-side * 19, 3 - worry))
            brow.quadTo(middle + QPointF(0, -7), middle + QPointF(side * 19, 2 + worry * .5))
            p.drawPath(brow)

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

    def arm(self, p, side, angle, opacity=1.0):
        """A sleeve swung out from the shoulder: 0 hangs down, 180 reaches straight up."""
        body = self.rect("body")
        p.save()
        p.setOpacity(opacity)
        p.translate(body.center().x() + side * body.width() * .32, body.top() + body.height() * .3)
        p.rotate(-side * angle)
        p.setPen(QPen(QColor(LINE), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(HAND))
        p.drawEllipse(QPointF(0, ARM_LENGTH), 17, 18)
        sleeve = QPainterPath(QPointF(-17, -10))
        sleeve.quadTo(QPointF(0, -20), QPointF(17, -10))
        sleeve.quadTo(QPointF(24, 40), QPointF(19, 74))
        sleeve.quadTo(QPointF(0, 84), QPointF(-19, 74))
        sleeve.quadTo(QPointF(-24, 40), QPointF(-17, -10))
        p.setBrush(QColor(SLEEVE))
        p.drawPath(sleeve)
        p.restore()

    def chest(self):
        body = self.rect("body")
        return QPointF(body.center().x(), body.top() + body.height() * .52)

    def book(self, p, pose):
        """Reading, with a page turning now and then."""
        p.save()
        p.setOpacity(min(1.0, pose.book * 1.3))
        p.translate(self.chest())
        p.setPen(QPen(QColor(LINE), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(COVER))
        p.drawRoundedRect(QRectF(-88, -50, 176, 104), 8, 8)
        p.setBrush(QColor("#FFFDF6"))
        for side in (-1, 1):
            page = QPainterPath(QPointF(0, -38))
            page.quadTo(QPointF(side * 40, -50), QPointF(side * 80, -40))
            page.lineTo(QPointF(side * 80, 44))
            page.quadTo(QPointF(side * 40, 34), QPointF(0, 46))
            page.closeSubpath()
            p.drawPath(page)
        p.setPen(QPen(QColor("#C7D6CC"), 4, Qt.SolidLine, Qt.RoundCap))
        for side in (-1, 1):
            for y in (-20, -2, 16):
                p.drawLine(QPointF(side * 16, y), QPointF(side * 66, y))
        turn = pose.clock / 4 % 1
        if turn < .16:  # A page lifts and crosses the spine.
            across = math.cos(turn / .16 * math.pi) * 78
            leaf = QPainterPath(QPointF(0, -38))
            leaf.quadTo(QPointF(across * .5, -62), QPointF(across, -48))
            leaf.lineTo(QPointF(across, 36))
            leaf.quadTo(QPointF(across * .5, 24), QPointF(0, 46))
            leaf.closeSubpath()
            p.setPen(QPen(QColor(LINE), 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(QColor("#FFFFFF"))
            p.drawPath(leaf)
        p.restore()

    def notebook(self, p, pose):
        """Homework: writing a line, then the next."""
        p.save()
        p.setOpacity(min(1.0, pose.write * 1.3))
        p.translate(self.chest())
        p.rotate(-2)
        p.setPen(QPen(QColor(LINE), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor("#FFFEF9"))
        p.drawRoundedRect(QRectF(-78, -52, 156, 104), 6, 6)
        p.setPen(QPen(QColor("#BFD3E6"), 2))
        rows = (-30, -8, 14, 36)
        for y in rows:
            p.drawLine(QPointF(-62, y), QPointF(62, y))
        written = pose.clock / 2.6 % len(rows)
        line = int(written)
        p.setPen(QPen(QColor("#5C6B64"), 4, Qt.SolidLine, Qt.RoundCap))
        for index, y in enumerate(rows[:line]):
            p.drawLine(QPointF(-56, y - 5), QPointF(52, y - 5))
        tip = QPointF(-56 + 108 * (written - line), rows[line] - 5)
        p.drawLine(QPointF(-56, tip.y()), tip)
        p.translate(tip + QPointF(0, -2 + math.sin(pose.clock * 24) * 2))
        p.rotate(36)
        p.setPen(QPen(QColor(LINE), 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        pencil = QPainterPath(QPointF(0, 0))
        pencil.lineTo(QPointF(-7, -14))
        pencil.lineTo(QPointF(-7, -64))
        pencil.lineTo(QPointF(7, -64))
        pencil.lineTo(QPointF(7, -14))
        pencil.closeSubpath()
        p.setBrush(QColor(FOLDER))
        p.drawPath(pencil)
        p.restore()

    def ball(self, p, pose):
        """Bouncing a ball beside her, the arm following it."""
        body = self.rect("body")
        bounce = abs(math.sin(pose.clock * 4.2))
        p.save()
        p.setOpacity(min(1.0, pose.ball * 1.3))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 40))
        floor = QPointF(body.right() + 22, self.height - 6)
        p.drawEllipse(floor, 28 - bounce * 10, 7 - bounce * 2)
        p.translate(floor.x(), floor.y() - 30 - bounce * 150)
        p.scale(1 + (1 - bounce) ** 6 * .16, 1 - (1 - bounce) ** 6 * .16)
        p.setPen(QPen(QColor(LINE), 4))
        p.setBrush(QColor(BALL))
        p.drawEllipse(QPointF(0, 0), 30, 30)
        p.setPen(QPen(QColor("#FFF3D6"), 5))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(-30, -14, 60, 28), 0, 180 * 16)
        p.restore()
        self.arm(p, 1, 28 + bounce * 34, min(1.0, pose.ball * 1.3))

    def cup(self, p, pose):
        """Tea held in both hands, lifted for a sip, with steam."""
        sip = max(0.0, math.sin(pose.clock * .95)) ** 3
        centre = self.chest() + QPointF(0, -18 - sip * 62)
        p.save()
        p.setOpacity(min(1.0, pose.cup * 1.3))
        p.translate(centre)
        p.setPen(QPen(QColor(LINE), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor("#FFFDF6"))
        p.drawRoundedRect(QRectF(-38, -32, 76, 66), 14, 14)
        p.setBrush(QColor(COVER))
        p.drawEllipse(QRectF(-34, -40, 68, 18))
        p.setBrush(QColor(HAND))
        for side in (-1, 1):
            p.drawEllipse(QPointF(side * 42, 8), 16, 17)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 215), 5, Qt.SolidLine, Qt.RoundCap))
        for side in (-1, 1):
            steam = QPainterPath(QPointF(side * 12, -46))
            drift = math.sin(pose.clock * 2.4 + side) * 9
            steam.cubicTo(QPointF(side * 12 + drift, -60), QPointF(side * 12 - drift, -72), QPointF(side * 12, -88))
            p.drawPath(steam)
        p.restore()

    def snooze(self, p, pose):
        """Letters drifting up while she dozes."""
        face = self.rect("face")
        p.save()
        p.setPen(QPen(QColor(LINE), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        for index in range(3):
            rise = (pose.clock * .32 + index / 3) % 1
            size = 11 + rise * 14
            p.setOpacity(min(1.0, pose.nap * 1.2) * math.sin(rise * math.pi))
            at = QPointF(face.right() + 8 + rise * 34, face.top() + 54 - rise * 112)
            letter = QPainterPath(at + QPointF(-size, -size))
            letter.lineTo(at + QPointF(size, -size))
            letter.lineTo(at + QPointF(-size, size))
            letter.lineTo(at + QPointF(size, size))
            p.drawPath(letter)
        p.restore()

    def scruff(self, p, pose):
        """The back of her collar, pulled up behind her head towards the hand."""
        grip = self.grip
        p.save()
        p.setOpacity(min(1.0, pose.lifted * 1.6))
        p.setPen(QPen(QColor(LINE), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(SLEEVE))
        cloth = QPainterPath(grip + QPointF(-14, 2))
        cloth.cubicTo(grip + QPointF(-20, 44), grip + QPointF(-62, 70), grip + QPointF(-92, 132))
        cloth.lineTo(grip + QPointF(92, 132))
        cloth.cubicTo(grip + QPointF(62, 70), grip + QPointF(20, 44), grip + QPointF(14, 2))
        cloth.closeSubpath()
        p.drawPath(cloth)
        p.setPen(QPen(QColor("#CDBFA6"), 3, Qt.SolidLine, Qt.RoundCap))
        for x in (-9, 0, 9):  # Creases where the cloth is gathered in the fingers.
            p.drawLine(grip + QPointF(x * .7, 8), grip + QPointF(x * 3.4, 66))
        p.restore()

    def hand(self, p, pose):
        """The hand that has picked her up: finger and thumb pinched on the cloth, the rest curled."""
        p.save()
        p.setOpacity(min(1.0, pose.lifted * 1.6))
        p.translate(self.grip)
        p.setPen(QPen(QColor(LINE), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(HAND))
        wrist = QPainterPath(QPointF(-24, -112))
        wrist.quadTo(QPointF(-30, -86), QPointF(-34, -70))
        wrist.lineTo(QPointF(40, -70))
        wrist.quadTo(QPointF(34, -92), QPointF(30, -112))
        p.drawPath(wrist)
        for x, y in ((46, -52), (52, -33), (44, -16)):  # The curled fingers.
            p.drawEllipse(QPointF(x, y), 15, 12)
        thumb = QPainterPath(QPointF(-52, -40))
        thumb.quadTo(QPointF(-44, -8), QPointF(-9, 7))
        thumb.quadTo(QPointF(0, 8), QPointF(-1, -7))
        thumb.quadTo(QPointF(-14, -24), QPointF(-12, -46))
        p.drawPath(thumb)
        finger = QPainterPath(QPointF(40, -42))
        finger.quadTo(QPointF(34, -8), QPointF(9, 7))
        finger.quadTo(QPointF(0, 8), QPointF(1, -7))
        finger.quadTo(QPointF(12, -24), QPointF(10, -48))
        p.drawPath(finger)
        palm = QPainterPath(QPointF(-52, -40))
        palm.quadTo(QPointF(-52, -82), QPointF(-6, -84))
        palm.quadTo(QPointF(44, -84), QPointF(40, -42))
        palm.quadTo(QPointF(10, -28), QPointF(-12, -38))
        palm.quadTo(QPointF(-30, -32), QPointF(-52, -40))
        p.setPen(Qt.NoPen)
        p.drawPath(palm)
        p.setPen(QPen(QColor(LINE), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        top = QPainterPath(QPointF(-52, -40))
        top.quadTo(QPointF(-52, -82), QPointF(-6, -84))
        top.quadTo(QPointF(44, -84), QPointF(40, -42))
        p.setBrush(Qt.NoBrush)
        p.drawPath(top)
        p.restore()

    def sweat(self, p, pose):
        face = self.rect("face")
        drop = QPainterPath(QPointF(0, -16))
        drop.quadTo(QPointF(13, 4), QPointF(0, 9))
        drop.quadTo(QPointF(-13, 4), QPointF(0, -16))
        p.save()
        p.setOpacity(min(1.0, pose.lifted))
        p.translate(face.left() + 16, face.top() + 92 + math.sin(pose.clock * 5) * 4)
        p.setPen(QPen(QColor(LINE), 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor("#BFE0F2"))
        p.drawPath(drop)
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
        p.translate(self.grip)
        p.rotate(pose.swing * pose.lifted)
        p.scale(1, 1 + pose.lifted * .035)
        p.translate(-self.grip)
        lift = pose.breath * 1.8
        head_at = lambda: (p.translate(self.pivot), p.rotate(pose.tilt), p.translate(-self.pivot))
        p.save()
        head_at()
        self.strands(p, "back hair", pose.sway, .22, -ax * .55, -ay * .4 - lift * .4)
        p.restore()
        if pose.lifted > .02:
            self.scruff(p, pose)
        p.save()
        p.translate(feet)
        p.scale(1, 1 + pose.breath * .006)
        p.translate(-feet)
        if pose.lifted > .02:  # Her legs trail behind the swing.
            self.strands(p, "body", -pose.swing * pose.lifted * .7, .46)
        else:
            self.put(p, "body")
        if pose.hold > .02:
            self.folder(p, pose)
        if pose.book > .02:
            self.book(p, pose)
        if pose.write > .02:
            self.notebook(p, pose)
        hands = max(pose.hold, pose.book, pose.write)
        if hands > .02:
            p.setOpacity(min(1.0, hands * 1.3))
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
        self.mouth_(p, pose, ax * 1.08, ay * 1.08)
        self.strands(p, "front hair", pose.sway * .6, .52, ax * 1.2, ay * .9)
        self.strands(p, "headwear", pose.sway * .3, .0, ax * 1.1, ay * .9)
        self.brows(p, pose, ax * 1.05, ay)
        p.restore()
        if pose.wave > .02:
            self.arm(p, 1, 40 + pose.wave * 92 + math.sin(pose.clock * 11) * 15, min(1.0, pose.wave * 1.6))
        if pose.stretch > .02:
            for side in (-1, 1):
                self.arm(p, side, 20 + pose.stretch * 142 + math.sin(pose.clock * 3) * 4, min(1.0, pose.stretch * 1.6))
        if pose.ball > .02:
            self.ball(p, pose)
        if pose.cup > .02:
            self.cup(p, pose)
        if pose.nap > .02:
            self.snooze(p, pose)
        self.glints(p, pose)
        if pose.lifted > .02:
            self.sweat(p, pose)
            self.hand(p, pose)
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
_EASED = [field.name for field in fields(Pose) if field.name not in ("breath", "sway", "swing", "eye_open", "clock")]


class Rig:
    """Moves the parameters over time: eased moods, breathing, blinking, gaze and the hair's spring."""

    def __init__(self, reduced_motion=False):
        self.pose = replace(_MOODS["idle"])
        self.reduced_motion = reduced_motion
        self.velocity = 0.0
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
            pose.eye_open, pose.breath, pose.sway, pose.swing = goal.eye_open, 0.0, 0.0, 0.0
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
        elif mood in ("happy", "greeting"):
            pose.tilt += math.sin(pose.clock * 6) * 3
        elif mood == "reading":
            pose.eye_x = math.sin(pose.clock * 1.7) * .6
        elif mood == "homework":
            pose.eye_x = math.sin(pose.clock * 2.4) * .4
        elif mood == "playing":
            pose.eye_y = .8 - abs(math.sin(pose.clock * 4.2)) * 1.1
            pose.eye_x = .7
        elif mood == "napping":
            pose.tilt += math.sin(pose.clock * .9) * 3
        if talking:
            pose.mouth_open = max(pose.mouth_open, .16 + .3 * abs(math.sin(pose.clock * 13)))
        self.blink(seconds, goal)
        # Head movement and a light breeze push the hair, and the spring carries it on.
        # Held up, she swings like a pendulum, and the pull sends her trailing behind the hand.
        self.swinging += ((max(-24.0, min(24.0, -pull * .045)) - pose.swing) * 42 - self.swinging * 2.4) * seconds
        pose.swing = max(-MAX_SWING, min(MAX_SWING, pose.swing + self.swinging * seconds))
        push = -(pose.angle_x - turned) / max(seconds, 1e-3) * 7 + math.sin(pose.clock * 1.1) * 2.2 + pose.swing * pose.lifted
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
