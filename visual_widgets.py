"""File covers and a keyboard-accessible, animated archive deck.

All imagery is rendered from the indexed file itself or its extracted text.
There are no remote images, fonts, requests, browser components or services.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import csv
import datetime as _dt
import html
import io
from pathlib import Path
import re
import stat
import threading

from PySide6.QtCore import (
    QEasingCurve, QObject, QPointF, Property, QRectF, QSize, Qt, QTimer,
    QParallelAnimationGroup, QPropertyAnimation, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QFont, QFontMetrics, QImage, QKeyEvent,
    QPainter, QPainterPath, QPen, QPixmap, QTransform,
)
from PySide6.QtWidgets import QApplication, QGraphicsObject, QGraphicsScene, QGraphicsView

from core import _assert_local_path, _cloud_placeholder
from i18n import language, t
from pages import image_reader, render_page
from search_ux import file_group
from theme import ACCENT, BG, FONT_FAMILIES, HAIRLINE, MONO_FAMILIES, RAISED, TEXT, TEXT_3


# Covers are painted at a fixed size and scaled, so resizing never repaints them.
COVER_SIZE = QSize(400, 520)
MAX_DECK_SCALE = 1.0
MAX_CARD_SCALE = 1.8
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
_CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".css", ".html", ".rs", ".go", ".c", ".cpp", ".cs", ".java", ".sql", ".sh", ".yaml", ".yml", ".toml", ".vue"}
_SPREADSHEET_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".ods"}
_MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mp3", ".wav", ".flac", ".m4a", ".ogg"}
# Covers that come from the file itself, read off the UI thread.
_DRAWN_FROM_FILE = _IMAGE_EXTENSIONS | {".pdf"}
_COVERS: OrderedDict[tuple, QPixmap] = OrderedDict()

_PALETTES = {
    "Documents": ("#E5E9E1", "#53705E"),
    "Spreadsheets": ("#E6DED0", "#927A58"),
    "Images": ("#DDDCEB", "#817596"),
    "Media": ("#DBE6E8", "#647F83"),
    "Code": ("#D6E2DB", "#547C68"),
    "Other": ("#DFE2DD", "#727B70"),
}


def as_plain(text) -> str:
    """For tooltips and message boxes, which guess at rich text.

    Rich text fetches images, including from \\\\server\\share paths, so nothing from
    a file or a connected tool may reach them as markup.
    """
    return f'<span style="white-space:pre-wrap">{html.escape(str(text))}</span>'


def kind_text(row):
    kind = row.get("kind", "File")
    ext = str(row.get("ext", "")).lstrip(".").upper()
    return t("{ext} file", ext=ext) if kind == "File" and ext else t(kind)


def _font(size: float, weight=QFont.Weight.Normal, families=None):
    f = QFont()
    f.setFamilies(families or FONT_FAMILIES)
    f.setPixelSize(round(size))
    f.setWeight(weight)
    return f


def safe_file_path(row: dict) -> Path | None:
    """Resolve a readable, local original or archive without hydrating placeholders."""
    for key in ("path", "original_path", "archive_path", "stored_path", "copy_path"):
        value = row.get(key)
        if not value:
            continue
        if key in {"path", "original_path"} and row.get("extraction_status") == "cloud_placeholder":
            continue
        try:
            path = _assert_local_path(value, check_links=True)
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and not _cloud_placeholder(info):
                return path
        except (OSError, ValueError):
            continue
    return None


def _file_key(row: dict, width: int, height: int):
    return (
        row.get("id"), row.get("path"), row.get("archive_path"),
        row.get("name"), row.get("ext"),
        row.get("mtime"), row.get("size"), row.get("extraction_status"),
        str(row.get("text", ""))[:500], str(row.get("summary", ""))[:200],
        language(), width, height,
    )


def _round_path(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def _draw_text(p, rect, text, font, color, flags=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop):
    p.setFont(font)
    p.setPen(QColor(color))
    p.drawText(rect, flags, str(text))


def _fit_text(text, font, width):
    return QFontMetrics(font).elidedText(str(text), Qt.TextElideMode.ElideRight, int(width))


def _title_from_row(row):
    text = str(row.get("text") or "")
    first = next((s.strip().lstrip("# ").strip() for s in text.splitlines() if s.strip()), "")
    # A code line is content rather than a readable cover headline.
    return first[:70] if first and len(first) < 90 else Path(str(row.get("name", "File"))).stem


def decode_cover(path, width, height) -> QImage:
    """Read a photo or a PDF's first page at cover size; safe off the UI thread. Null if unreadable."""
    if str(path).lower().endswith(".pdf"):
        return render_page(path, 0, width)
    try:
        reader = image_reader(path)
        reader.setAutoTransform(True)
        source_size = reader.size()
        if source_size.width() <= 0 or source_size.height() <= 0:
            return QImage()
        # Decode only at preview resolution; do not allocate full-size camera images.
        if source_size.width() * source_size.height() > 120_000_000:
            return QImage()
        reader.setScaledSize(source_size.scaled(QSize(width, height), Qt.AspectRatioMode.KeepAspectRatioByExpanding))
        return reader.read()
    except (OSError, ValueError, RuntimeError):
        return QImage()


def _draw_image(p, image, w, h):
    if image.isNull():
        return False
    fit = min(image.width() / w, image.height() / h)
    source = QRectF((image.width() - w * fit) / 2, (image.height() - h * fit) / 2, w * fit, h * fit)
    p.drawImage(QRectF(0, 0, w, h), image, source)
    return True


def _draw_document(p, row, w, h, base, accent):
    p.fillRect(QRectF(0, 0, w, h), QColor(base))
    page = QRectF(w * .105, h * .07, w * .79, h * .96)
    p.fillRect(page.translated(3, 5), QColor(25, 45, 30, 16))
    p.fillRect(page, QColor("#FAFBF8"))
    margin = page.width() * .12
    left = page.left() + margin
    content_width = page.width() - 2 * margin
    ext = str(row.get("ext", "")).lstrip(".").upper() or "DOC"
    p.fillRect(QRectF(left, page.top() + 25, 26, 4), QColor(accent))
    _draw_text(p, QRectF(left + 34, page.top() + 17, 105, 21), ext,
               _font(9, QFont.Weight.Medium), accent)
    title = _title_from_row(row)
    title_font = _font(21 if len(title) < 14 else 17)
    _draw_text(p, QRectF(left, page.top() + 56, content_width, 76), title,
               title_font, "#273B30", Qt.TextFlag.TextWordWrap)
    text = str(row.get("text") or t(row.get("summary") or ""))
    lines = [re.sub(r"^[#*\->\s]+", "", line).strip() for line in text.splitlines()]
    body = "\n".join(line for line in lines[1:] if line) if len(lines) > 1 else text
    body_font = _font(10)
    _draw_text(p, QRectF(left, page.top() + 141, content_width, page.height() - 177), body[:1400],
               body_font, "#798478", Qt.TextFlag.TextWordWrap)
    # The tiny page footer is the real file type, not invented document metadata.
    _draw_text(p, QRectF(left, h - 25, content_width, 16), kind_text(row),
               _font(8), accent)


def _draw_code(p, row, w, h):
    p.fillRect(QRectF(0, 0, w, h), QColor("#20382E"))
    p.fillRect(QRectF(0, 0, w, 40), QColor("#1B3027"))
    for i, color in enumerate(["#9E9A82", "#B4B092", "#81A18A"]):
        p.setBrush(QColor(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(20 + i * 13, 21), 3, 3)
    ext = str(row.get("ext", "code")).lstrip(".").upper()
    _draw_text(p, QRectF(w - 70, 12, 50, 20), ext, _font(10), "#B4CCB9",
               Qt.AlignmentFlag.AlignRight)
    text = str(row.get("text") or t(row.get("summary") or ""))
    lines = text.splitlines()[:20]
    body_font = _font(11, families=MONO_FAMILIES)
    for i, line in enumerate(lines):
        y = 60 + i * 17
        if y > h - 24:
            break
        _draw_text(p, QRectF(12, y, 23, 17), str(i + 1), _font(9, families=MONO_FAMILIES), "#668471",
                   Qt.AlignmentFlag.AlignRight)
        stripped = line.strip()
        color = "#B9CFC0"
        if stripped.startswith(("#", "//", "/*")):
            color = "#87A38D"
        elif stripped.startswith(("def ", "class ", "function ", "import ", "from ", "const ", "let ", "return ")):
            color = "#D8CA96"
        _draw_text(p, QRectF(48, y, w - 62, 17), _fit_text(line, body_font, w - 62), body_font, color)
    if not lines:
        _draw_text(p, QRectF(24, 68, w - 48, h - 100), Path(row.get("name", "Code file")).stem,
                   _font(22, QFont.Weight.Medium), "#D3E2D6", Qt.TextFlag.TextWordWrap)


def _draw_table(p, row, w, h, base, accent):
    p.fillRect(QRectF(0, 0, w, h), QColor(base))
    page = QRectF(22, 22, w - 44, h - 8)
    p.fillRect(page, QColor("#FAFBF8"))
    _draw_text(p, QRectF(38, 43, w - 76, 45), Path(row.get("name", "Spreadsheet")).stem,
               _font(19), "#2B4235", Qt.TextFlag.TextWordWrap)
    raw = str(row.get("text") or "")
    sep = "\t" if row.get("ext") == ".tsv" else ","
    rows = list(csv.reader(io.StringIO(raw), delimiter=sep))[:10]
    if not any(len(r) > 1 for r in rows):
        # Parsed Office text is not falsely reconstructed into a table.
        _draw_text(p, QRectF(38, 108, w - 76, h - 130), raw[:800], _font(11), "#768474", Qt.TextFlag.TextWordWrap)
        return
    cols = min(3, max(len(r) for r in rows))
    top, left, cw, rh = 105, 38, (w - 76) / cols, 31
    for ri, values in enumerate(rows):
        y = top + ri * rh
        if y + rh > h - 10:
            break
        p.fillRect(QRectF(left, y, w - 76, rh), QColor(accent if ri == 0 else ("#F2F5EE" if ri % 2 else "#FAFBF8")))
        for ci in range(cols):
            value = values[ci] if ci < len(values) else ""
            font = _font(9, QFont.Weight.Medium if ri == 0 else QFont.Weight.Normal)
            _draw_text(p, QRectF(left + ci * cw + 6, y + 9, cw - 12, 16), _fit_text(value, font, cw - 12), font,
                       "#F2F7EF" if ri == 0 else "#70806F")


def _draw_media(p, row, w, h, base, accent):
    p.fillRect(QRectF(0, 0, w, h), QColor("#26383C"))
    p.setPen(QPen(QColor("#91A8A5"), 1.5))
    center = QPointF(w / 2, h * .44)
    p.drawEllipse(center, 43, 43)
    triangle = QPainterPath()
    triangle.moveTo(center.x() - 9, center.y() - 15)
    triangle.lineTo(center.x() + 17, center.y())
    triangle.lineTo(center.x() - 9, center.y() + 15)
    triangle.closeSubpath()
    p.fillPath(triangle, QColor("#B9CEC3"))
    _draw_text(p, QRectF(26, h - 103, w - 52, 60), Path(row.get("name", "Media")).stem,
               _font(21, QFont.Weight.Medium), "#DEE7DE", Qt.TextFlag.TextWordWrap)
    _draw_text(p, QRectF(26, h - 34, w - 52, 18), t("Audio or video"), _font(10), "#97AAA4")


def _row_ext(row: dict) -> str:
    ext = str(row.get("ext") or Path(row.get("name", "")).suffix).lower()
    return "." + ext if ext and not ext.startswith(".") else ext


def render_cover(row: dict, width=360, height=460, decoded: QImage | None = None) -> QPixmap:
    """Render and cache a real file cover; requires an existing QApplication.

    ``decoded`` is a photo already read by ``CoverLoader``. Without it a photo is
    read here, on the calling thread.
    """
    width, height = max(48, int(width)), max(64, int(height))
    key = _file_key(row, width, height)
    if key in _COVERS:
        _COVERS.move_to_end(key)
        return _COVERS[key]
    # A design space 300 wide, scaled evenly: a preview never stretches its cover.
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    w, h = 300, round(300 * height / width)
    p.scale(width / w, height / h)
    p.setClipPath(_round_path(QRectF(0, 0, w, h), 10))
    ext = _row_ext(row)
    base, accent = _PALETTES[file_group(ext)]
    if ext in _DRAWN_FROM_FILE:
        if decoded is None:
            path = safe_file_path(row)
            decoded = decode_cover(path, width, height) if path else QImage()
        if not _draw_image(p, decoded, w, h):
            _draw_document(p, row, w, h, base, accent)
    elif ext in _CODE_EXTENSIONS:
        _draw_code(p, row, w, h)
    elif ext in _SPREADSHEET_EXTENSIONS:
        _draw_table(p, row, w, h, base, accent)
    elif ext in _MEDIA_EXTENSIONS:
        _draw_media(p, row, w, h, base, accent)
    else:
        _draw_document(p, row, w, h, base, accent)
    p.end()
    _COVERS[key] = pixmap
    while len(_COVERS) > 160:
        _COVERS.popitem(last=False)
    return pixmap


class CoverLoader(QObject):
    """Reads photos off the UI thread, newest request first.

    Qt decoding releases the GIL, so scrolling stays smooth. Requests too old to
    still be on screen are dropped rather than decoded.
    """

    ready = Signal()
    _decoded = Signal(object, object)
    MAX_WAITING = 48

    def __init__(self, parent=None):
        super().__init__(parent)
        self._workers = ThreadPoolExecutor(3, thread_name_prefix="covers")
        self._lock = threading.Lock()
        self._waiting = OrderedDict()
        self._busy = set()
        self._decoded.connect(self._finish)

    def cover(self, row: dict, width: int, height: int) -> QPixmap | None:
        """The finished cover, or None while a photo is still being read."""
        key = _file_key(row, width, height)
        if key in _COVERS:
            _COVERS.move_to_end(key)
            return _COVERS[key]
        path = safe_file_path(row) if _row_ext(row) in _DRAWN_FROM_FILE else None
        if path is None:
            return render_cover(row, width, height)
        with self._lock:
            if key in self._waiting:
                self._waiting.move_to_end(key)
            elif key not in self._busy:
                self._waiting[key] = (row, path, width, height)
                while len(self._waiting) > self.MAX_WAITING:
                    self._waiting.popitem(last=False)
                self._workers.submit(self._decode_next)
        return None

    def _decode_next(self):
        with self._lock:
            if not self._waiting:
                return
            key, (row, path, width, height) = self._waiting.popitem()
            self._busy.add(key)
        image = decode_cover(path, width, height)
        try:
            self._decoded.emit(key, (row, width, height, image))
        except RuntimeError:
            pass  # The window closed while this photo was being read.

    def _finish(self, key, job):
        with self._lock:
            self._busy.discard(key)
        row, width, height, image = job
        render_cover(row, width, height, image)
        self.ready.emit()

    def stop(self):
        self._workers.shutdown(wait=False, cancel_futures=True)


def preview_pixmap(row: dict, max_width: int, max_height: int) -> QPixmap | None:
    """The image at its real proportions, without cover styling, or None."""
    path = safe_file_path(row)
    if path is None:
        return None
    if _row_ext(row) == ".pdf":
        ratio = QApplication.primaryScreen().devicePixelRatio()
        page = render_page(path, 0, round(max_width * ratio))
        if page.isNull():
            return None
        pixmap = QPixmap.fromImage(page.scaled(QSize(max_width, max_height) * ratio, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        pixmap.setDevicePixelRatio(ratio)
        return pixmap
    reader = image_reader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.width() <= 0 or size.height() <= 0 or size.width() * size.height() > 120_000_000:
        return None
    if size.width() > max_width or size.height() > max_height:
        reader.setScaledSize(size.scaled(QSize(max_width, max_height), Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    return None if image.isNull() else QPixmap.fromImage(image)


class FileTile(QGraphicsObject):
    clicked = Signal(int)
    activated = Signal(int)
    dragged = Signal(int)
    menu_requested = Signal(int, object)
    hovered = Signal(int)
    unhovered = Signal(int)

    WIDTH = 186
    HEIGHT = 260
    FOOTER = 50

    def __init__(self, row: dict, covers: CoverLoader, parent=None):
        super().__init__(parent)
        self.row = row
        self.covers = covers
        self.file_id = int(row["id"])
        self._selected = False
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(as_plain(row.get("name", "")))
        self.cover = None
        self.refresh_cover()

    def refresh_cover(self):
        if self.cover is None:
            self.cover = self.covers.cover(self.row, self.WIDTH * 2, (self.HEIGHT - self.FOOTER) * 2)
            self.update()

    def boundingRect(self):
        return QRectF(-self.WIDTH / 2 - 16, -self.HEIGHT / 2 - 10, self.WIDTH + 32, self.HEIGHT + 34)

    def shape(self):
        # Shadow bounds do not steal clicks from the real adjacent cards.
        return _round_path(QRectF(-self.WIDTH / 2, -self.HEIGHT / 2, self.WIDTH, self.HEIGHT), 11)

    def set_selected(self, selected: bool):
        self._selected = bool(selected)
        self.update()

    def paint(self, p, option, widget=None):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(-self.WIDTH / 2, -self.HEIGHT / 2, self.WIDTH, self.HEIGHT)
        for spread, alpha in ((13, 3), (10, 5), (7, 7), (4, 10)):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawRoundedRect(rect.adjusted(-spread, -spread + 7, spread, spread + 10), 14 + spread, 14 + spread)
        path = _round_path(rect, 11)
        p.setClipPath(path)
        p.fillRect(rect, QColor(RAISED))
        if self.cover is not None:
            p.drawPixmap(QRectF(rect.left(), rect.top(), rect.width(), rect.height() - self.FOOTER),
                         self.cover, QRectF(self.cover.rect()))
        footer_top = rect.bottom() - self.FOOTER
        title_font = _font(11)
        _draw_text(p, QRectF(rect.left() + 12, footer_top + 9, rect.width() - 24, 17),
                   _fit_text(self.row.get("name", "File"), title_font, rect.width() - 24), title_font, TEXT)
        _draw_text(p, QRectF(rect.left() + 12, footer_top + 29, rect.width() - 24, 14),
                   kind_text(self.row), _font(9), TEXT_3)
        if self.row.get("favorite"):
            p.setBrush(QColor(ACCENT))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(rect.right() - 16, rect.top() + 16), 3.5, 3.5)
        p.setClipping(False)
        p.setPen(QPen(QColor(ACCENT) if self._selected else QColor(HAIRLINE), 1.4 if self._selected else .8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect.adjusted(.6, .6, -.6, -.6), 11, 11)

    def hoverEnterEvent(self, event):
        self.hovered.emit(self.file_id)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.unhovered.emit(self.file_id)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.file_id)
            event.accept()
        else:
            super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        self.menu_requested.emit(self.file_id, event.screenPos())

    def mouseMoveEvent(self, event):
        travelled = event.screenPos() - event.buttonDownScreenPos(Qt.MouseButton.LeftButton)
        if event.buttons() & Qt.MouseButton.LeftButton and travelled.manhattanLength() >= QApplication.startDragDistance():
            self.dragged.emit(self.file_id)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.file_id)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class ArchiveDeck(QGraphicsView):
    selected = Signal(int)
    activated = Signal(int)
    dragged = Signal(int)
    menu_requested = Signal(int, object)

    def __init__(self, covers: CoverLoader, parent=None):
        super().__init__(parent)
        self.covers = covers
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setBackgroundBrush(QColor(BG))
        self.setStyleSheet(f"background: {BG}; border: none;")
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.TextAntialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("File deck")
        self.setAccessibleDescription("Use the left and right arrow keys to choose a file, Enter to preview, double-click to open.")
        self.setMinimumHeight(305)
        self._rows = []
        self._rows_signature = None
        self._tiles: list[FileTile] = []
        self._selected_id = None
        self._hover_id = None
        self._mode = "fan"
        self._reduced_motion = False
        self._animation = None
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(lambda: self._reposition(False))
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(45)
        self._hover_timer.timeout.connect(lambda: self._reposition(True))
        self._leave_timer = QTimer(self)
        self._leave_timer.setSingleShot(True)
        self._leave_timer.setInterval(90)
        self._leave_timer.timeout.connect(self._settle_hover)

    def set_files(self, rows: list[dict]):
        """Display up to nine recent files; efficient result lists live outside the deck."""
        next_rows = list(rows[:9])
        signature = tuple((_file_key(row, FileTile.WIDTH * 2, (FileTile.HEIGHT - 53) * 2),
                           bool(row.get("favorite"))) for row in next_rows)
        if signature == self._rows_signature:
            # Search/model refreshes can carry new snippets but identical covers.
            # Keep live graphics items, hover focus and any transition in flight.
            self._rows = next_rows
            for tile, row in zip(self._tiles, next_rows):
                tile.row = row
            return
        self._rows_signature = signature
        self._stop_animation()
        old_selected = self._selected_id
        self.scene().clear()
        self._rows = next_rows
        self._tiles = []
        self._hover_id = None
        for row in self._rows:
            item = FileTile(row, self.covers)
            item.clicked.connect(self._clicked)
            item.activated.connect(self.activated)
            item.dragged.connect(self.dragged)
            item.menu_requested.connect(self.menu_requested)
            item.hovered.connect(self._on_hover)
            item.unhovered.connect(self._on_unhover)
            self.scene().addItem(item)
            self._tiles.append(item)
        ids = [item.file_id for item in self._tiles]
        self._selected_id = old_selected if old_selected in ids else (ids[len(ids) // 2] if ids else None)
        self._reposition(False)

    def refresh_covers(self):
        for tile in self._tiles:
            tile.refresh_cover()

    def set_layout_mode(self, mode: str):
        if mode not in {"fan", "line"}:
            raise ValueError("Deck layout must be 'fan' or 'line'")
        if mode != self._mode:
            self._mode = mode
            self._reposition(True)

    def set_selected(self, file_id):
        file_id = int(file_id) if file_id is not None else None
        if file_id != self._selected_id and any(item.file_id == file_id for item in self._tiles):
            self._selected_id = file_id
            self._reposition(True)

    def set_reduced_motion(self, enabled):
        self._reduced_motion = bool(enabled)
        if enabled:
            self._reposition(False)

    def _clicked(self, file_id):
        self._selected_id = file_id
        self._hover_id = None
        self._reposition(True)
        self.selected.emit(file_id)

    def _on_hover(self, file_id):
        self._leave_timer.stop()
        self._hover_id = file_id
        self._hover_timer.start()

    def _on_unhover(self, file_id):
        if self._hover_id == file_id:
            # A raised card can move out from under a stationary pointer. Keep
            # its original hit region active, avoiding an up/down hover loop.
            self._leave_timer.start()

    def _settle_hover(self):
        point = self.mapToScene(self.viewport().mapFromGlobal(QCursor.pos()))
        hovered = next((item for item in self._tiles if item.file_id == self._hover_id), None)
        if hovered is not None:
            local = hovered.mapFromScene(point)
            base_region = QRectF(-FileTile.WIDTH / 2 - 8, -FileTile.HEIGHT / 2 - 8,
                                 FileTile.WIDTH + 16, FileTile.HEIGHT + 48)
            if base_region.contains(local):
                return
        self._hover_id = None
        self._hover_timer.start()

    def _stop_animation(self):
        if self._animation is not None:
            self._animation.stop()
            self._animation.deleteLater()
            self._animation = None

    def _reposition(self, animate=True):
        self._stop_animation()
        if not self._tiles:
            self.scene().setSceneRect(QRectF(-300, -140, 600, 280))
            return
        width = max(self.viewport().width(), 180)
        height = max(self.viewport().height(), 180)
        self.scene().setSceneRect(QRectF(-width / 2, -height / 2, width, height))
        active = next((i for i, item in enumerate(self._tiles) if item.file_id == self._selected_id), len(self._tiles) // 2)
        hover_index = next((i for i, item in enumerate(self._tiles) if item.file_id == self._hover_id), None)
        # Recenter the selected card while exposing enough of each neighbour to recognise it.
        max_distance = max(active, len(self._tiles) - 1 - active, 1)
        # Taller windows get larger cards, not more empty space around the same fan.
        card_scale = max(.85, min(MAX_CARD_SCALE, height / 430))
        available = max(50, width / 2 - 118)
        spacing = min((103 if self._mode == "fan" else 155) * max(1, card_scale), available / max_distance)
        targets = []
        baseline_bounds = QRectF()
        for i, item in enumerate(self._tiles):
            distance = i - active
            magnitude = abs(distance)
            x = distance * spacing
            if self._mode == "fan":
                angle = max(-22., min(22., distance * 6.2))
                # Rotation and diminishing size already make the fan arc.
                # A shallow vertical arc leaves room for shadows and hover lift.
                y = (7 + 2.5 * magnitude + .6 * magnitude * magnitude) * card_scale
                scale = max(.72, 1 - .065 * magnitude) * card_scale
            else:
                angle = 0.
                y = 17 + min(magnitude, 2) * 3
                scale = max(.72, 1 - .065 * magnitude) * card_scale
            opacity = max(.40, 1.0 - magnitude * .13)
            transform = QTransform()
            transform.translate(x, y - 14)
            transform.rotate(angle)
            transform.scale(scale, scale)
            bounds = transform.mapRect(item.boundingRect())
            baseline_bounds = bounds if baseline_bounds.isNull() else baseline_bounds.united(bounds)
            if hover_index is not None:
                if i == hover_index:
                    y -= 20
                    scale += .055
                    opacity = 1.
                    angle *= .65
                elif abs(i - hover_index) == 1:
                    x += -18 if i < hover_index else 18
                    y += 3
            item.setZValue(100 - magnitude + (30 if i == hover_index else 0))
            item.set_selected(i == active)
            targets.append((item, x, y - 14, angle, scale, opacity))
        # Fit the actual rotated bounding boxes in both dimensions. Reserve
        # hover space even at rest, so moving the pointer doesn't resize the
        # entire deck or clip the outer cards. FileTile bounds include shadows.
        baseline_bounds.adjust(-24, -31, 24, 10)
        horizontal_span = max(abs(baseline_bounds.left()), abs(baseline_bounds.right())) * 2
        factor = min(MAX_DECK_SCALE, (width - 16) / max(1, horizontal_span),
                     (height - 12) / max(1, baseline_bounds.height()))
        # Keep the chosen file horizontally centred even near either end.
        center = QPointF(0, baseline_bounds.center().y())
        group = QParallelAnimationGroup(self)
        for item, x, y, angle, scale, opacity in targets:
            destinations = {
                b"pos": QPointF((x - center.x()) * factor, (y - center.y()) * factor),
                b"rotation": angle,
                b"scale": scale * factor,
                b"opacity": opacity,
            }
            for prop, value in destinations.items():
                if animate and not self._reduced_motion:
                    animation = QPropertyAnimation(item, prop, group)
                    animation.setDuration(290)
                    animation.setStartValue(item.property(prop.decode()))
                    animation.setEndValue(value)
                    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
                    group.addAnimation(animation)
                else:
                    item.setProperty(prop.decode(), value)
        if animate and not self._reduced_motion:
            self._animation = group
            group.start()
        else:
            group.deleteLater()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_timer.start(20)

    def leaveEvent(self, event):
        self._leave_timer.stop()
        self._hover_id = None
        self._hover_timer.start()
        super().leaveEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in {Qt.Key.Key_Left, Qt.Key.Key_Right} and self._tiles:
            ids = [item.file_id for item in self._tiles]
            current = ids.index(self._selected_id) if self._selected_id in ids else 0
            step = -1 if event.key() == Qt.Key.Key_Left else 1
            self._clicked(ids[max(0, min(len(ids) - 1, current + step))])
            event.accept()
        elif event.key() in {Qt.Key.Key_Enter, Qt.Key.Key_Return} and self._selected_id is not None:
            self.selected.emit(self._selected_id)
            event.accept()
        else:
            super().keyPressEvent(event)

