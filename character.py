"""Roo, the assistant: finished paintings, one or a few per action, brought to life in code.

Each painting in assets/roo is the whole of her with her face already drawn, all cut from
one canvas at one scale, so her feet and her size never change between poses. Code adds
the life: she sways from her feet, breathes, blinks, dips her head over her folder, fades
from one painting into the next, and swings from her collar when she is held up. Rig
moves the numbers over time and Puppet paints them. This is not a Live2D model and uses
none of its runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import random

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPixmap

from pages import image_reader

ASSETS = Path(__file__).resolve().parent / "assets" / "roo"
STRIPS = 48  # Rows she is cut into to bend: enough that no step shows.
ROOM = .04  # Room around her for what moves past her outline, as a share of her height.
MAX_SWING = 30  # Degrees, however hard she is shaken.
BLINK = .13  # Seconds her eyes stay shut.
NOD_EASE = 6.0  # How quickly her head reaches a new height: most of the way in half a second.


@dataclass(frozen=True)
class Action:
    """What one mood looks like. Lengths are shares of her height, so they hold at every size."""
    keys: tuple[str, ...]
    holds: tuple[float, ...] = ()  # Seconds each painting stays up, for an action of several.
    nods: tuple[float, ...] = ()  # How far her head dips with each painting.
    sway: float = .009  # At the top of her head; her feet stay put.
    sway_period: float = 3.6
    breath: float = .0045
    breath_period: float = 2.9
    blink: str = ""  # The same painting with her eyes shut.
    fade: float = .2  # Seconds for one painting to fade into the next.


ACTIONS = {
    "idle": Action(("idle",), blink="idle-blink"),
    "greeting": Action(("greet",), sway=.004, breath=.003),
    # Pull a sheet out, hold it up to read, tuck it back in: each stays up long enough to see.
    "working": Action(("busy", "busy-look", "busy-tuck"), holds=(1.3, 1.9, 1.5), nods=(.0062, 0, .0054),
                      sway=.0045, sway_period=3.4, breath=.0036, fade=.27),
    "happy": Action(("done",), sway=.0054, breath=.0036),
    "excited": Action(("found",), sway=.0054, breath=.0036),
    "reading": Action(("read",), sway=.0045, sway_period=4.4),
    "tea": Action(("tea",), sway=.0045, sway_period=4.4),
    "napping": Action(("nap",), sway=.007, sway_period=5.0, breath=.007, breath_period=4.0),
    "dangling": Action(("lifted",), sway=0, breath=.0027),
}


def smooth(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


@dataclass
class Pose:
    """One moment of her. Everything drawn comes from these numbers."""
    key: str = "idle"  # The painting.
    before: str = "idle"  # The painting fading out under it.
    mix: float = 1.0  # How far the painting has faded in.
    shut: str = ""  # Shown instead while she blinks.
    sway: float = 0.0
    breath: float = 0.0
    nod: float = 0.0
    swing: float = 0.0  # Degrees, swinging from the hand that holds her.
    clock: float = 0.0


class Puppet:
    """The paintings, scaled once for the screen they are on and then only bent and moved."""

    def __init__(self, folder=ASSETS):
        folder = Path(folder)
        meta = json.loads((folder / "roo.json").read_text(encoding="utf-8"))
        self.width, self.height = meta["width"], meta["height"]
        self.grip = QPointF(*meta["collar"])  # Where she is held: the pointer pinches her here.
        self.neck = meta["neck"]
        self.paintings = {path.stem: image_reader(path).read() for path in sorted(folder.glob("*.png"))}
        self.scaled = {}
        self.pixels = 0

    def prepare(self, pixels):
        """Scales every painting to this many device pixels tall. Moving them after that is cheap."""
        if pixels == self.pixels:
            return
        self.pixels = pixels
        width = max(1, round(self.width * pixels / self.height))
        self.scaled = {name: QPixmap.fromImage(image.scaled(width, pixels, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
                       for name, image in self.paintings.items()}

    def painting(self, pose):
        """The picture for this moment: one painting, or two mid-fade."""
        key = pose.shut or pose.key
        if pose.mix >= 1 or pose.before == key:
            return self.scaled[key]
        under, over = self.scaled[pose.before], self.scaled[key]
        both = QPixmap(over.size())
        both.fill(Qt.transparent)
        p = QPainter(both)
        # Source with an opacity mixes the two, colour and transparency alike: a true blend, outline included.
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.drawPixmap(0, 0, under)
        p.setOpacity(pose.mix)
        p.drawPixmap(0, 0, over)
        p.end()
        return both

    def bent(self, pose):
        """Her picture with the sway, breath and nod in it, and where it goes in artwork pixels.

        She is cut into rows, and each row is moved: sideways more the higher it is, up with a
        breath, down with a nod above her neck. Rows overlap by one pixel, so they meet with no seams.
        """
        flat = self.painting(pose)
        if not (pose.sway or pose.breath or pose.nod):
            return flat, QRectF(0, 0, self.width, self.height)
        scale = flat.height() / self.height
        room = round(ROOM * flat.height())
        out = QPixmap(flat.width() + 2 * room, flat.height() + room)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setCompositionMode(QPainter.CompositionMode_Source)  # The overlap row is replaced, not laid twice.
        h = self.height
        neck = self.neck

        def lean(y):
            return pose.sway * h * max(0.0, 1 - y / h) ** 1.6

        def rise(y):
            head = smooth((neck + .025 * h - y) / (.06 * h))  # All of her head, easing out across her neck.
            return -pose.breath * h * max(0.0, 1 - y / (.62 * h)) ** 1.2 + pose.nod * h * head

        rows = flat.height()
        for row in range(STRIPS):
            # Cut on whole rows of the painting, so nothing is resampled until it moves.
            top, bottom = rows * row // STRIPS, rows * (row + 1) // STRIPS
            if bottom <= top:
                continue
            reach = min(rows, bottom + 1)  # One row of overlap, so no seam opens between rows.
            upper = top + rise(top / scale) * scale + room
            lower = reach + rise(reach / scale) * scale + room
            x = room + lean((top + bottom) / 2 / scale) * scale
            p.drawPixmap(QRectF(x, upper, flat.width(), lower - upper), flat, QRectF(0, top, flat.width(), reach - top))
        p.end()
        return out, QRectF(-room / scale, -room / scale, out.width() / scale, out.height() / scale)

    def draw(self, p, height, pose, device_ratio=1.0):
        """Paints her with her feet at the origin, `height` logical pixels tall."""
        self.prepare(round(height * device_ratio))
        p.save()
        p.scale(height / self.height, height / self.height)
        p.translate(-self.width / 2, -self.height)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if pose.swing:
            p.translate(self.grip)
            p.rotate(pose.swing)
            p.translate(-self.grip)
        picture, where = self.bent(pose)
        p.drawPixmap(where, picture, QRectF(picture.rect()))
        p.restore()


class Rig:
    """Moves the numbers over time: the action's paintings in turn, fades, sway, breath, blinks and the swing when held."""

    def __init__(self, reduced_motion=False):
        self.pose = Pose()
        self.reduced_motion = reduced_motion
        self.mood = "idle"
        self.since = 0.0  # Seconds in this mood.
        self.fading = 0.0
        self.fade = 1.0
        self.sway = self.breath = 0.0  # Eased, so a change of mood never jerks her.
        self.sway_turn = self.breath_turn = 0.0  # Kept as angles, so a change of pace never jerks her either.
        self.swinging = 0.0
        self.next_blink = 2.5
        self.blinking = 0.0
        self.again = False

    def poke(self):
        """Touched, she blinks."""
        self.next_blink = 0.0

    def painting(self, action):
        """Which of the action's paintings is up now, and how far her head dips with it."""
        if not action.holds:
            return action.keys[0], action.nods[0] if action.nods else 0.0
        at = self.since % sum(action.holds)
        for index, hold in enumerate(action.holds):
            if at < hold:
                break
            at -= hold
        return action.keys[index], action.nods[index] if action.nods else 0.0

    def step(self, seconds, mood="idle", pull=0.0):
        """`pull` is how fast whoever holds her is moving sideways, in pixels a second."""
        action = ACTIONS.get(mood, ACTIONS["idle"])
        pose = self.pose
        if mood != self.mood:
            self.mood, self.since = mood, 0.0
        else:
            self.since += seconds
        key, nod = self.painting(action)
        if self.reduced_motion:
            pose.key = pose.before = action.keys[0]
            pose.mix, pose.shut = 1.0, ""
            pose.sway = pose.breath = pose.nod = pose.swing = 0.0
            return pose
        pose.clock += seconds
        if key != pose.key:
            pose.before = pose.key if pose.mix >= .5 else pose.before  # Whichever showed more.
            pose.key, self.fading, self.fade = key, 0.0, action.fade
        else:
            self.fading += seconds
        pose.mix = smooth(self.fading / self.fade)
        rate = 1 - math.exp(-seconds * NOD_EASE)
        pose.nod += (nod - pose.nod) * rate
        self.sway += (action.sway - self.sway) * rate
        self.breath += (action.breath - self.breath) * rate
        self.sway_turn = (self.sway_turn + 2 * math.pi * seconds / action.sway_period) % (2 * math.pi)
        self.breath_turn = (self.breath_turn + 2 * math.pi * seconds / action.breath_period) % (2 * math.pi)
        pose.sway = self.sway * math.sin(self.sway_turn)
        pose.breath = self.breath * (.5 + .5 * math.sin(self.breath_turn))
        self.blink(seconds, action)
        if mood == "dangling":
            # Held up, she swings like a pendulum, trailing behind the hand, and never quite stops.
            rest = max(-24.0, min(24.0, -pull * .045)) + 5 * math.sin(pose.clock * 4.2)
            self.swinging += ((rest - pose.swing) * 42 - self.swinging * 2.4) * seconds
            pose.swing = max(-MAX_SWING, min(MAX_SWING, pose.swing + self.swinging * seconds))
        else:
            self.swinging = 0.0
            pose.swing *= 1 - rate
            if abs(pose.swing) < .05:
                pose.swing = 0.0
        return pose

    def blink(self, seconds, action):
        self.next_blink -= seconds
        if self.next_blink <= 0:
            self.blinking = BLINK
            if self.again:
                self.again, self.next_blink = False, random.uniform(2.2, 5.8)
            else:  # Now and then she blinks twice.
                self.again = random.random() < .25
                self.next_blink = .3 if self.again else random.uniform(2.2, 5.8)
        self.blinking = max(0.0, self.blinking - seconds)
        open_eyes = not self.blinking or not action.blink or self.pose.key != action.keys[0] or self.pose.mix < 1
        self.pose.shut = "" if open_eyes else action.blink
