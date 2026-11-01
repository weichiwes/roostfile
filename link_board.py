"""Files that belong together, laid out as cards on a board.

Each set of linked files sits in its own dashed frame, drawn as a tidy tree from
the file with the most links. A card shows the file itself, so a set can be
recognised at a glance. Dragging one card onto another links them. Nothing here
touches the archive: it reports what was done, and the window decides.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from theme import ACCENT, BG, FONT_FAMILIES, HAIRLINE, HAIRLINE_STRONG, RAISED, RAISED_HOVER, SELECTED, TEXT, TEXT_2, TEXT_3

CARD_W, CARD_H = 248.0, 124.0
COVER_W, COVER_H = 78.0, 104.0
COLUMN_GAP, ROW_GAP = 76.0, 18.0
FRAME_PAD, FRAME_TOP = 24.0, 46.0
SHELF_GAP = 36.0
MIN_SHELF = 1200.0
MIN_ZOOM, MAX_ZOOM = .3, 1.6
OVERVIEW_ZOOM = .68  # The board is laid out to fill the window at about this size.
SIMPLE_ZOOM = .5  # Below this, covers and small print are too small to read, so they are left out.
BEND = 12.0
DOT_GAP = 26.0
FRAME_MS = 16
FLIGHT = .2  # How much of the way to a new view each frame covers.
OFFER_H, OFFER_W, OFFER_GAP = 58.0, 236.0, 8.0
TOUCH = 18.0  # World units of forgiveness when dropping one card on another.


def groups_of(ids, edges):
    """The sets of files reachable from one another, largest first."""
    around = {file_id: set() for file_id in ids}
    for one, other in edges:
        if one in around and other in around:
            around[one].add(other)
            around[other].add(one)
    seen, found = set(), []
    for file_id in ids:
        if file_id in seen:
            continue
        group, waiting = [], [file_id]
        seen.add(file_id)
        while waiting:
            current = waiting.pop()
            group.append(current)
            for neighbour in around[current] - seen:
                seen.add(neighbour)
                waiting.append(neighbour)
        found.append(sorted(group))
    return sorted(found, key=lambda group: (-len(group), group[0]))


def tree_of(group, edges):
    """Each file's place in a tidy tree: its column and row, with the most linked file as the root."""
    around = {file_id: [] for file_id in group}
    for one, other in edges:
        if one in around and other in around:
            around[one].append(other)
            around[other].append(one)
    root = max(group, key=lambda file_id: (len(around[file_id]), -file_id))
    placed = {}
    seen = {root}
    next_row = [0.0]

    def walk(file_id, depth):
        children = [item for item in sorted(around[file_id]) if item not in seen]
        seen.update(children)
        rows = [walk(child, depth + 1) for child in children]
        row = (rows[0] + rows[-1]) / 2 if rows else next_row[0]
        if not rows:
            next_row[0] += 1
        placed[file_id] = (depth, row)
        return row

    walk(root, 0)
    return root, placed


class Card:
    def __init__(self, row, x, y, frame):
        self.id, self.row = row["id"], row
        self.x, self.y = x, y
        self.frame = frame

    @property
    def rect(self):
        return QRectF(self.x, self.y, CARD_W, CARD_H)


class Frame:
    def __init__(self, members, root, rect):
        self.members, self.root, self.rect = members, root, rect


class LinkBoard(QWidget):
    selected = Signal(int)
    opened = Signal(int)
    tied = Signal(int, int)
    untied = Signal(int, int)
    menu_requested = Signal(int, QPoint)
    offer_taken = Signal(int, int)
    cleared = Signal()

    def __init__(self, covers=None, parent=None):
        super().__init__(parent)
        self.covers = covers
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.nodes, self.edges, self.frames, self.rows = {}, [], [], []
        self.offers = []
        self.zoom, self.pan = 1.0, QPointF(0, 0)
        self.hover = self.hover_edge = self.hover_offer = None
        self.focused = None
        self.members = set()
        self.matches = None
        self.pressed = self.dragging = self.drag_at = self.target = self.panning = None
        self.moved_far = False
        self.reduced_motion = False
        self.fitted = False
        self.flight = None
        self.laid_for = 0.0
        self.describe = lambda row: (str(row.get("ext", "")).lstrip(".").upper(), "")
        self.title_of = lambda name, count: f"{name}  ·  {count}"
        self.cut_hint = "Right-click to remove this link"
        self.drop_hint = "Release to link"
        self.offers_title = "Might belong here"
        self.timer = QTimer(self)
        self.timer.setInterval(FRAME_MS)
        self.timer.timeout.connect(self.tick)

    def set_reduced_motion(self, reduced):
        self.reduced_motion = reduced

    def tick(self):
        if self.flight is None:
            self.timer.stop()
            return
        zoom, pan = self.flight
        self.zoom += (zoom - self.zoom) * FLIGHT
        self.pan += (pan - self.pan) * FLIGHT
        if abs(zoom - self.zoom) < .002 and (pan - self.pan).manhattanLength() < .6:
            self.zoom, self.pan, self.flight = zoom, pan, None
            self.timer.stop()
        self.update()

    def fly_to(self, zoom, pan):
        if self.reduced_motion or not self.isVisible():
            self.zoom, self.pan, self.flight = zoom, pan, None
            self.update()
            return
        self.flight = zoom, pan
        self.timer.start()

    def set_graph(self, rows, edges):
        self.rows = rows
        known = {row["id"] for row in rows}
        self.edges = [edge for edge in edges if edge[0] in known and edge[1] in known]
        self.lay_out()
        self.hover = self.target = self.hover_edge = None
        if self.focused not in self.nodes:
            self.focused = None
            self.offers = []
        self.refresh_members()
        if not self.fitted:
            self.fit()
        self.update()

    def lay_out(self):
        """Frames sit on shelves, reading left to right, and the board scrolls down."""
        by_id = {row["id"]: row for row in self.rows}
        self.laid_for = float(self.width())
        shelf_width = max(MIN_SHELF, self.width() / OVERVIEW_ZOOM)
        self.nodes, self.frames = {}, []
        for group in groups_of(sorted(by_id), self.edges):
            root, places = tree_of(group, self.edges)
            columns = max(column for column, _ in places.values()) + 1
            rows = max(row for _, row in places.values()) + 1
            width = columns * CARD_W + (columns - 1) * COLUMN_GAP + 2 * FRAME_PAD
            height = rows * CARD_H + (rows - 1) * ROW_GAP + FRAME_TOP + FRAME_PAD
            x, y = self.free_spot(width, shelf_width)
            frame = Frame(set(group), root, QRectF(x, y, width, height))
            self.frames.append(frame)
            for file_id, (column, row) in places.items():
                self.nodes[file_id] = Card(by_id[file_id], x + FRAME_PAD + column * (CARD_W + COLUMN_GAP), y + FRAME_TOP + row * (CARD_H + ROW_GAP), frame)

    def free_spot(self, width, shelf_width):
        """The highest place a frame fits, so small sets fill the gaps beside large ones."""
        best = None
        for x in [0.0] + [frame.rect.right() + SHELF_GAP for frame in self.frames]:
            if x and x + width > shelf_width:
                continue
            y = max((frame.rect.bottom() + SHELF_GAP for frame in self.frames if frame.rect.left() < x + width and frame.rect.right() > x), default=0.0)
            if best is None or (y, x) < (best[1], best[0]):
                best = x, y
        return best

    def resizeEvent(self, event):
        if self.rows and abs(self.width() - self.laid_for) > 120:
            self.lay_out()
            self.fitted = False
        if not self.fitted and self.nodes:
            self.fit()
        super().resizeEvent(event)

    def extent(self):
        frames = [frame.rect for frame in self.frames]
        return min(r.left() for r in frames), min(r.top() for r in frames), max(r.right() for r in frames), max(r.bottom() for r in frames)

    def view_of(self, left, top, right, bottom, most=1.0):
        """The zoom and pan that show this part of the board, from its top if it is too tall."""
        room_h = self.height() - (OFFER_H + 34 if self.offers else 24)
        zoom = max(MIN_ZOOM, min(most, (self.width() - 40) / max(1.0, right - left), max(OVERVIEW_ZOOM, room_h / max(1.0, bottom - top))))
        y = (room_h - (bottom - top) * zoom) / 2 if (bottom - top) * zoom < room_h else 12.0
        return zoom, QPointF((self.width() - (right - left) * zoom) / 2 - left * zoom, y - top * zoom)

    def fit(self):
        if not self.frames or self.width() < 40:
            self.zoom, self.pan = 1.0, QPointF(20, 12)
            return
        self.zoom, self.pan = self.view_of(*self.extent())
        self.flight = None
        self.fitted = True
        self.update()

    def group_of(self, file_id):
        return self.nodes[file_id].frame.members if file_id in self.nodes else set()

    def refresh_members(self):
        self.members = set(self.group_of(self.focused))

    def focus(self, file_id, centred=False):
        self.focused = file_id if file_id in self.nodes else None
        if self.focused is None:
            self.offers = []
        self.refresh_members()
        if centred and self.focused is not None:
            rect = self.nodes[self.focused].frame.rect
            self.fly_to(*self.view_of(rect.left(), rect.top(), rect.right(), rect.bottom()))
        self.update()

    def set_offers(self, offers):
        """Files that might belong with the focused one, shown along the bottom until someone takes one."""
        self.offers = list(offers)
        self.update()

    def set_matches(self, ids):
        """Lights the cards found by a search. None shows everything."""
        self.matches = None if ids is None else set(ids) & set(self.nodes)
        self.update()

    def to_world(self, point):
        return QPointF((point.x() - self.pan.x()) / self.zoom, (point.y() - self.pan.y()) / self.zoom)

    def card_at(self, point, skip=None):
        spot = self.to_world(point)
        return next((card.id for card in self.nodes.values() if card.id != skip and card.rect.contains(spot)), None)

    def offer_rects(self):
        """Screen positions of the suggestions, in a strip along the bottom of the board."""
        y = self.height() - OFFER_H - 12
        fit = max(1, int((self.width() - 24) // (OFFER_W + OFFER_GAP)))
        return [(offer, QRectF(12 + index * (OFFER_W + OFFER_GAP), y, OFFER_W, OFFER_H)) for index, offer in enumerate(self.offers[:fit])]

    def offer_at(self, point):
        return next((offer for offer, rect in self.offer_rects() if rect.contains(point)), None)

    def connector(self, one, other):
        """A thin elbow from one card to the next, with rounded corners, as on a whiteboard."""
        a, b = (one, other) if one.x <= other.x else (other, one)
        if a.x == b.x:  # In the same column, so it loops out to the right of both.
            start, end = QPointF(a.x + CARD_W, a.y + CARD_H / 2), QPointF(b.x + CARD_W, b.y + CARD_H / 2)
            path = QPainterPath(start)
            out = start.x() + COLUMN_GAP * .45
            path.cubicTo(QPointF(out, start.y()), QPointF(out, end.y()), end)
            return path
        start, end = QPointF(a.x + CARD_W, a.y + CARD_H / 2), QPointF(b.x, b.y + CARD_H / 2)
        middle = end.x() - COLUMN_GAP / 2
        path = QPainterPath(start)
        rise = end.y() - start.y()
        if abs(rise) < 1:
            path.lineTo(end)
            return path
        bend = min(BEND, abs(rise) / 2, COLUMN_GAP / 2)
        way = 1 if rise > 0 else -1
        path.lineTo(middle - bend, start.y())
        path.quadTo(QPointF(middle, start.y()), QPointF(middle, start.y() + way * bend))
        path.lineTo(middle, end.y() - way * bend)
        path.quadTo(QPointF(middle, end.y()), QPointF(middle + bend, end.y()))
        path.lineTo(end)
        return path

    def edge_at(self, point):
        spot = self.to_world(point)
        stroker = QPainterPathStroker()
        stroker.setWidth(12 / self.zoom)
        return next((edge for edge in self.edges if stroker.createStroke(self.connector(self.nodes[edge[0]], self.nodes[edge[1]])).contains(spot)), None)

    def mousePressEvent(self, event):
        point = event.position()
        self.pressed, self.moved_far = point, False
        if event.button() == Qt.LeftButton:
            if self.offer_at(point) is not None:
                return
            hit = self.card_at(point)
            if hit is not None:
                self.dragging = hit
            else:
                self.panning = point - self.pan
                self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        point = event.position()
        if self.pressed is not None and (point - self.pressed).manhattanLength() > 5:
            self.moved_far = True
        if self.dragging is not None and self.moved_far:
            self.drag_at = self.to_world(point)
            self.target = self.landing(self.dragging, self.drag_at)
            self.update()
        elif self.panning is not None:
            self.pan = point - self.panning
            self.flight = None
            self.update()
        else:
            self.look_at(point)

    def landing(self, file_id, spot):
        """The card a dragged one would be linked to, if it is not linked already."""
        reach = QRectF(spot.x() - TOUCH, spot.y() - TOUCH, 2 * TOUCH, 2 * TOUCH)
        for card in self.nodes.values():
            if card.id != file_id and card.rect.intersects(reach) and (min(file_id, card.id), max(file_id, card.id)) not in self.edges:
                return card.id
        return None

    def look_at(self, point):
        offer = self.offer_at(point)
        hover = self.card_at(point) if offer is None else None
        edge = self.edge_at(point) if hover is None and offer is None else None
        if (hover, offer, edge) != (self.hover, self.hover_offer, self.hover_edge):
            self.hover, self.hover_offer, self.hover_edge = hover, offer, edge
            self.setCursor(Qt.PointingHandCursor if hover is not None or offer is not None else Qt.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, event):
        point = event.position()
        dragged, target, moved = self.dragging, self.target, self.moved_far
        self.dragging = self.target = self.panning = self.pressed = self.drag_at = None
        self.setCursor(Qt.ArrowCursor)
        self.update()
        if event.button() != Qt.LeftButton or (moved and dragged is None):
            return
        if dragged is not None and moved:
            if target is not None:
                self.tied.emit(dragged, target)
            return
        offer = self.offer_at(point)
        if offer is not None and self.focused is not None:
            self.offer_taken.emit(self.focused, offer["id"])
        elif dragged is not None:
            self.selected.emit(dragged)
        else:
            self.focus(None)
            self.cleared.emit()

    def mouseDoubleClickEvent(self, event):
        hit = self.card_at(event.position())
        if hit is not None:
            self.opened.emit(hit)
        else:
            self.fit()

    def wheelEvent(self, event):
        """Scrolling moves through the board. Ctrl and the wheel zoom, as in a browser."""
        self.flight = None
        if event.modifiers() & Qt.ControlModifier:
            before = self.to_world(event.position())
            self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom * 1.0016 ** event.angleDelta().y()))
            self.pan = event.position() - before * self.zoom
        else:
            self.pan += QPointF(event.angleDelta().x(), event.angleDelta().y()) * .8
        self.update()

    def contextMenuEvent(self, event):
        point = QPointF(event.pos())
        hit = self.card_at(point)
        if hit is not None:
            self.menu_requested.emit(hit, event.globalPos())
            return
        edge = self.edge_at(point)
        if edge:
            self.untied.emit(*edge)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.focused is not None:
            self.focus(None)
            self.cleared.emit()
        else:
            super().keyPressEvent(event)

    def leaveEvent(self, event):
        self.hover = self.hover_edge = self.hover_offer = None
        self.update()

    def faded(self, file_id):
        if self.matches is not None:
            return file_id not in self.matches
        return bool(self.members) and file_id not in self.members

    def font_of(self, size):
        font = QFont()
        font.setFamilies(FONT_FAMILIES)
        font.setPixelSize(size)
        return font

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(BG))
        self.draw_dots(p)
        p.save()
        p.translate(self.pan)
        p.scale(self.zoom, self.zoom)
        view = QRectF(self.to_world(QPointF(0, 0)), self.to_world(QPointF(self.width(), self.height())))
        for frame in self.frames:
            if frame.rect.intersects(view):
                self.draw_frame(p, frame)
        for one, other in self.edges:
            self.draw_connector(p, self.nodes[one], self.nodes[other])
        for card in self.nodes.values():
            if card.rect.intersects(view) and card.id != self.dragging:
                self.draw_card(p, card)
        if self.dragging is not None and self.drag_at is not None:
            self.draw_drag(p)
        if self.hover_edge:
            middle = self.connector(self.nodes[self.hover_edge[0]], self.nodes[self.hover_edge[1]]).pointAtPercent(.5)
            self.draw_hint(p, middle, self.cut_hint)
        p.restore()
        self.draw_offers(p)
        p.end()

    def draw_dots(self, p):
        """A faint grid, so the board reads as a surface and panning can be felt."""
        gap = DOT_GAP * self.zoom
        if gap < 11:
            return
        tone = QColor(HAIRLINE)
        p.setPen(QPen(tone, 1.4))
        x = self.pan.x() % gap
        while x < self.width():
            y = self.pan.y() % gap
            while y < self.height():
                p.drawPoint(QPointF(x, y))
                y += gap
            x += gap

    def draw_frame(self, p, frame):
        lit = frame.members == self.members
        dim = all(self.faded(file_id) for file_id in frame.members)
        p.setOpacity(.3 if dim else 1)
        pen = QPen(QColor(HAIRLINE_STRONG if lit else HAIRLINE), 1.2, Qt.DashLine)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(frame.rect, 14, 14)
        p.setFont(self.font_of(14))
        p.setPen(QColor(TEXT if lit else TEXT_2))
        title = self.title_of(self.nodes[frame.root].row.get("name", ""), len(frame.members))
        line = p.fontMetrics().elidedText(title, Qt.ElideMiddle, int(frame.rect.width() - 2 * FRAME_PAD))
        p.drawText(QRectF(frame.rect.left() + FRAME_PAD, frame.rect.top() + 12, frame.rect.width() - 2 * FRAME_PAD, 22), Qt.AlignLeft | Qt.AlignVCenter, line)
        p.setOpacity(1)

    def draw_connector(self, p, one, other):
        lit = (self.hover_edge in ((one.id, other.id), (other.id, one.id))) or (self.focused in (one.id, other.id)) or (self.hover in (one.id, other.id))
        p.setOpacity(.25 if self.faded(one.id) and self.faded(other.id) else 1)
        pen = QPen(QColor(ACCENT if lit else HAIRLINE_STRONG), 1.6 if lit else 1.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        path = self.connector(one, other)
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(ACCENT if lit else HAIRLINE_STRONG))
        for point in (path.pointAtPercent(0), path.pointAtPercent(1)):
            p.drawEllipse(point, 3, 3)
        p.setOpacity(1)

    def draw_card(self, p, card, lifted=False):
        row, rect = card.row, card.rect
        chosen = card.id == self.focused
        landing = card.id == self.target
        hovered = card.id == self.hover
        p.setOpacity(.3 if self.faded(card.id) and not landing else .92 if lifted else 1)
        p.setBrush(QColor(SELECTED if chosen else RAISED_HOVER if hovered or landing else RAISED))
        pen = QPen(QColor(ACCENT if chosen or landing else HAIRLINE_STRONG if hovered else HAIRLINE), 1.6 if chosen or landing else 1)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.drawRoundedRect(rect, 10, 10)
        tag, meta = self.describe(row)
        if self.zoom < SIMPLE_ZOOM:
            p.setFont(self.font_of(22))
            p.setPen(QColor(TEXT))
            p.drawText(rect.adjusted(14, 10, -14, -10), Qt.AlignVCenter | Qt.TextWordWrap, p.fontMetrics().elidedText(row.get("name", ""), Qt.ElideMiddle, int(2 * CARD_W - 60)))
            p.setOpacity(1)
            return
        left = rect.left() + 10
        cover = self.covers.cover(row, int(COVER_W * 2), int(COVER_H * 2)) if self.covers else None
        place = QRectF(left, rect.top() + 10, COVER_W, COVER_H)
        clip = QPainterPath()
        clip.addRoundedRect(place, 5, 5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(HAIRLINE))
        p.drawPath(clip)
        if cover is not None and not cover.isNull():
            p.save()
            p.setClipPath(clip, Qt.IntersectClip)
            p.drawPixmap(place, cover, QRectF(cover.rect()))
            p.restore()
        text = rect.adjusted(COVER_W + 22, 11, -12, -10)
        p.setFont(self.font_of(14))
        p.setPen(QColor(TEXT))
        name = row.get("name", "")
        metrics = p.fontMetrics()
        p.drawText(QRectF(text.left(), text.top(), text.width(), 40), Qt.AlignLeft | Qt.AlignTop | Qt.TextWrapAnywhere, self.two_lines(metrics, name, text.width()))
        p.setFont(self.font_of(11))
        chip = p.fontMetrics().horizontalAdvance(tag) + 14
        place = QRectF(text.left(), text.bottom() - 40, chip, 20)
        p.setPen(QPen(QColor(HAIRLINE_STRONG), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(place, 5, 5)
        p.setPen(QColor(TEXT_2))
        p.drawText(place, Qt.AlignCenter, tag)
        p.setPen(QColor(TEXT_3))
        p.drawText(QRectF(text.left(), text.bottom() - 17, text.width(), 17), Qt.AlignLeft | Qt.AlignVCenter, meta)
        p.setOpacity(1)

    @staticmethod
    def two_lines(metrics, name, width):
        """Breaks a long name once, keeping the end of it, which often tells versions apart."""
        if metrics.horizontalAdvance(name) <= width:
            return name
        cut = len(name)
        while cut > 1 and metrics.horizontalAdvance(name[:cut]) > width:
            cut -= 1
        return name[:cut] + "\n" + metrics.elidedText(name[cut:], Qt.ElideMiddle, int(width))

    def draw_drag(self, p):
        card = self.nodes[self.dragging]
        ghost = Card(card.row, self.drag_at.x() - CARD_W / 2, self.drag_at.y() - CARD_H / 2, card.frame)
        pen = QPen(QColor(ACCENT if self.target is not None else HAIRLINE_STRONG), 1.4, Qt.DashLine)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(card.rect, 10, 10)
        self.draw_card(p, ghost, lifted=True)
        if self.target is not None:
            self.draw_hint(p, QPointF(self.drag_at.x(), ghost.y), self.drop_hint)

    def draw_hint(self, p, at, text):
        p.setFont(self.font_of(13))
        width = p.fontMetrics().horizontalAdvance(text)
        box = QRectF(at.x() - width / 2 - 9, at.y() - 34, width + 18, 26)
        p.setPen(QColor(HAIRLINE_STRONG))
        p.setBrush(QColor(RAISED))
        p.drawRoundedRect(box, 6, 6)
        p.setPen(QColor(TEXT))
        p.drawText(box, Qt.AlignCenter, text)

    def draw_offers(self, p):
        spots = self.offer_rects()
        if not spots:
            return
        p.setFont(self.font_of(12))
        p.setPen(QColor(TEXT_3))
        p.drawText(QRectF(14, spots[0][1].top() - 22, 300, 18), Qt.AlignLeft | Qt.AlignVCenter, self.offers_title)
        for offer, rect in spots:
            lit = offer is self.hover_offer
            p.setBrush(QColor(RAISED_HOVER if lit else BG))
            p.setPen(QPen(QColor(ACCENT if lit else HAIRLINE_STRONG), 1.2, Qt.DashLine))
            p.drawRoundedRect(rect, 10, 10)
            p.setPen(QPen(QColor(ACCENT if lit else TEXT_3), 1.8, Qt.SolidLine, Qt.RoundCap))
            mark = QPointF(rect.left() + 20, rect.center().y())
            p.drawLine(mark + QPointF(-6, 0), mark + QPointF(6, 0))
            p.drawLine(mark + QPointF(0, -6), mark + QPointF(0, 6))
            text = rect.adjusted(38, 9, -12, -9)
            p.setFont(self.font_of(13))
            p.setPen(QColor(TEXT))
            p.drawText(QRectF(text.left(), text.top(), text.width(), 20), Qt.AlignLeft | Qt.AlignVCenter, p.fontMetrics().elidedText(offer["name"], Qt.ElideMiddle, int(text.width())))
            p.setFont(self.font_of(11))
            p.setPen(QColor(TEXT_3))
            p.drawText(QRectF(text.left(), text.top() + 21, text.width(), 18), Qt.AlignLeft | Qt.AlignVCenter, offer.get("why_text", ""))
