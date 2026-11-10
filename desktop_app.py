"""Native Qt archive UI. All assets, indexing and rendering stay on this PC."""
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
# An embeddable Python (runtime\python.exe) leaves the script's folder off sys.path.
sys.path[:0] = [str(BASE / ".vendor"), str(BASE)]
from offline import enforce_offline
enforce_offline()

import argparse
from collections import OrderedDict
import datetime as dt
import json
import os
import sqlite3
import stat
import subprocess
import threading
import time

from PySide6.QtCore import Qt, QEasingCurve, QEvent, QLibraryInfo, QPropertyAnimation, QSize, QPoint, QRect, QRectF, QObject, QStandardPaths, QTranslator, Signal, QTimer, QAbstractListModel, QMimeData, QModelIndex, QUrl
from PySide6.QtGui import QColor, QCursor, QDrag, QFont, QFontDatabase, QGuiApplication, QIcon, QKeyEvent, QPainter, QPen, QPixmap, QKeySequence, QShortcut, QTextCursor, QTextCharFormat
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QLineEdit,
    QVBoxLayout, QHBoxLayout, QStackedWidget, QSplitter, QListView,
    QAbstractItemView, QStyledItemDelegate, QStyle, QComboBox, QDialog, QDialogButtonBox,
    QTextEdit, QFileDialog, QMessageBox, QCheckBox, QRadioButton, QButtonGroup,
    QHeaderView, QInputDialog, QKeySequenceEdit, QListWidget, QListWidgetItem, QMenu, QProgressDialog, QScrollArea, QSizePolicy, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem,
)

from background import (
    AUTOSTART_AVAILABLE, DEFAULT_HOTKEY, FolderWatcher, GlobalHotkey, SingleInstance, autostart_command, autostart_enabled,
    foreground_window, give_keyboard_to, key_label, native_keys, open_with_default, set_autostart, show_in_folder, user_is_busy,
)
from core import (
    ArchiveCore, MAX_PREVIEW_ROWS, TABLE_EXTS, _assert_local_path, _cloud_placeholder, archive_folder_in, data_location, forget_location, has_archive, is_noise,
    read_table, remember_location,
)
from demo import demo_data_dir, prepare_demo
from i18n import LANGUAGES, app_name, condition_label, language, set_language, t
import ocr
from assistant import FRAME_MS, NAP_AFTER, PASTIMES, SIZES, Assistant, clear_images, install_image, install_image_set
import character
from link_board import LinkBoard
from pages import page_count, render_page
from search_ux import KIND_EXTS, example_word, explain_matches, file_group, highlight_spans, parse_query, related_queries, resolve_filters, topic_of
from theme import (
    ACCENT, BG, FONT_FAMILIES, HAIRLINE, HAIRLINE_STRONG, ON_ACCENT, RAISED, RAISED_HOVER,
    SELECTED, TEXT, TEXT_2, TEXT_3, WARNING,
)
from visual_widgets import COVER_SIZE, ArchiveDeck, CoverLoader, as_plain, kind_text, preview_pixmap

PAGE_SIZE = 120
SCAN_REFRESH_SECONDS = 0.7
PREVIEW_PAGES = 10
MAX_COLLECTED_AT_ONCE = 5000
QT_WORDS = QTranslator()
OCR_PAUSE_SECONDS = 0.05
OCR_BATCH = 20
READ_ON_DROP = 40  # The rest of a large folder is read in the background.
MAX_ROWS_READ_ON_DROP = 2000
SORT_TEXT_CHARS = 20000
MAX_VERSIONS_SHOWN = 4
# Folder change notices replace polling, leaving a slow check for what they cannot
# see, such as a disk plugged in again. A busy folder still only scans once a minute.
SAFETY_SCAN_MS = 30 * 60 * 1000
CHANGE_SETTLE_MS = 3000
# More changes than this at once are quicker to find with a full look.
MAX_CHANGED_PATHS = 500
MIN_SCAN_GAP_SECONDS = 60
MIN_CELL_WIDTH = 188
GRID_TEXT_HEIGHT = 52
# Double-clicking any of these would run code, so the folder opens instead.
LAUNCHING_EXTS = {
    ".exe", ".msi", ".msp", ".mst", ".bat", ".cmd", ".com", ".scr", ".pif", ".cpl", ".msc", ".lnk", ".url",
    ".hta", ".reg", ".inf", ".scf", ".application", ".appx", ".msix", ".jar", ".ps1", ".psm1", ".py", ".pyw",
    ".js", ".jse", ".vbs", ".vbe", ".wsf", ".wsh", ".ws",
    ".app", ".command", ".pkg", ".dmg", ".sh",
}
PICKER_ROWS = 40
MAP_OFFERS = 5
MAX_LINKS_SHOWN = 3
MATCH_LABELS = {"name": "Filename", "tags": "Tags", "body": "Text", "location": "Location", "info": "File details"}
HOME_FILES = 9
HOME_USED = 5
FILE_TYPES = (("Documents", "file"), ("Images", "image"), ("Spreadsheets", "table"), ("Media", "music"), ("Code", "code"), ("Other", "folder"))
_fonts_loaded = False


def load_local_fonts():
    global _fonts_loaded
    if _fonts_loaded:
        return
    fonts_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "seguisb.ttf"):
        path = fonts_dir / name
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    _fonts_loaded = True


def ui_font(size, weight=QFont.Weight.Normal):
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPixelSize(size)
    font.setWeight(weight)
    return font


STROKES = {
    "search": '<circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 4.5 4.5"/>',
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/>',
    "list": '<path d="M8 5h13M8 12h13M8 19h13M3 5h.1M3 12h.1M3 19h.1"/>',
    "deck": '<rect x="7" y="4" width="10" height="16" rx="2"/><path d="M3.5 6 1 7l3 13 3-1M20.5 6 23 7l-3 13-3-1"/>',
    "folder": '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    "star": '<path d="m12 3 2.8 5.7 6.3.9-4.5 4.4 1.1 6.2-5.7-3-5.7 3 1.1-6.2L3.2 9.6l6.2-.9Z"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h6"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.3"/><path d="m3 17 6-6 4 4 3-3 5 5"/>',
    "code": '<path d="m8 6-6 6 6 6m8-12 6 6-6 6M14 3 10 21"/>',
    "book": '<path d="M3 4c4-1 7 0 9 2 2-2 5-3 9-2v15c-4-1-7 0-9 2-2-2-5-3-9-2ZM12 6v15"/>',
    "home": '<path d="m3 10 9-8 9 8v11h-7v-7h-4v7H3Z"/>',
    "music": '<path d="M10 17V4l11-2v13M10 7l11-2"/><ellipse cx="6" cy="18" rx="4" ry="3"/><ellipse cx="17" cy="16" rx="4" ry="3"/>',
    "table": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18M10 4v16"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "link": '<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3.2-3.2a4.5 4.5 0 0 0-6.4-6.4L11.8 5.800"/><path d="M14 10a4.500 4.500 0 0 0-6.400 0l-3.200 3.200a4.500 4.500 0 0 0 6.400 6.400l1.400-1.400"/>',
    "back": '<path d="m14 6-6 6 6 6"/>',
    "close": '<path d="m6 6 12 12M18 6 6 18"/>',
    "refresh": '<path d="M20 8a8 8 0 1 0 0 9M20 3v5h-5"/>',
    "settings": '<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="9" cy="18" r="2"/>',
    "external": '<path d="M14 3h7v7m0-7-11 11M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/>',
    "copy": '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
}


def icon(name, color=TEXT_2, size=20):
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">{STROKES.get(name, STROKES["file"])}</svg>'
    pix = QPixmap(size * 2, size * 2)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    QSvgRenderer(svg.encode()).render(painter)
    painter.end()
    pix.setDevicePixelRatio(2)
    return QIcon(pix)


def app_icon():
    """Solid, so the mark stays readable on a light taskbar."""
    pix = QPixmap(256, 256)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(BG))
    painter.setPen(QPen(QColor(HAIRLINE_STRONG), 6))
    painter.drawRoundedRect(QRectF(3, 3, 250, 250), 56, 56)
    icon("book", ACCENT, 80).paint(painter, QRect(48, 48, 160, 160))
    painter.end()
    return QIcon(pix)


def file_glyph(row):
    ext = str(row.get("ext", "")).lower()
    kind = row.get("kind", "")
    if kind == "Image":
        return "image"
    if kind in {"Audio", "Video"}:
        return "music"
    if kind == "Spreadsheet":
        return "table"
    if kind == "Code file":
        return "code"
    return "folder" if ext in {".zip", ".7z", ".rar", ".tar", ".gz"} else "file"


def show_qt_words(code):
    """Yes, No and the other words Qt draws itself."""
    if code == "zh" and QT_WORDS.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        QApplication.installTranslator(QT_WORDS)
    else:
        QApplication.removeTranslator(QT_WORDS)


def literal(text):
    """Buttons and menus read a lone & as a keyboard shortcut. In a folder's name it is just an &."""
    return str(text).replace("&", "&&")


def popup(menu, position):
    menu.exec(position)
    menu.deleteLater()


def label(text, name="", parent=None):
    result = QLabel(text, parent)
    # Names and text from files land here, and rich text would fetch any image they mention.
    result.setTextFormat(Qt.PlainText)
    if name:
        result.setObjectName(name)
    return result


def button(text, callback=None, glyph=None, name="", checkable=False):
    b = QPushButton(literal(text))
    b.setObjectName(name or "button")
    b.setCursor(Qt.PointingHandCursor)
    b.setCheckable(checkable)
    if glyph:
        b.setIcon(icon(glyph, ON_ACCENT if name == "primary" else TEXT_2, 16))
        b.setIconSize(QSize(16, 16))
    if callback:
        b.clicked.connect(callback)
    return b


def repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def readable_path(row):
    for key in ("path", "archive_path"):
        value = row.get(key)
        if not value or (key == "path" and row.get("extraction_status") == "cloud_placeholder"):
            continue
        try:
            path = _assert_local_path(value, check_links=True)
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and not _cloud_placeholder(info):
                return path
        except (OSError, ValueError):
            pass
    return None


def suggested_folders():
    """The usual places people keep things, if they exist here. Nothing is read until chosen."""
    found = {}
    for title, location in (("Documents", QStandardPaths.DocumentsLocation), ("Desktop", QStandardPaths.DesktopLocation),
                            ("Downloads", QStandardPaths.DownloadLocation), ("Pictures", QStandardPaths.PicturesLocation)):
        path = QStandardPaths.writableLocation(location)
        if path and os.path.isdir(path):
            found.setdefault(os.path.normpath(path), title)
    return [(title, path) for path, title in found.items()]


def center_out(rows):
    """The first row in the middle of the deck, with the rest fanning out from it."""
    return rows[1::2][::-1] + rows[::2]


def file_mime(rows):
    """Hands other apps the file itself, as Explorer does. None if it is not on this computer."""
    paths = [path for path in map(readable_path, rows) if path]
    if not paths:
        return None
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return mime


def folder_name(text):
    """A subject made safe to name a folder with."""
    return "".join("_" if character in '<>:"/\\|?*' or ord(character) < 32 else character for character in text).strip(" .") or "Files"


def load_results(core, args, offset=0, limit=PAGE_SIZE):
    """One page of results, explained, in the interface language."""
    page = core.search_page(**args, offset=offset, limit=limit)
    bodies = core.bodies(row["id"] for row in page["rows"]) if page["tokens"] else {}
    page["rows"] = explain_matches(page["rows"], bodies, args["query"])
    links = core.linked_counts([row["id"] for row in page["rows"]])
    for row in page["rows"]:
        row["links"] = links.get(row["id"], 0)
    for row in page["rows"]:
        # With no text, the summary is a stored message rather than the file's own words.
        if not row.get("text") and row.get("summary"):
            row["snippet"] = t(row["summary"])
    families = core.version_counts(row["version_key"] for row in page["rows"])
    for row in page["rows"]:
        total, newest = families.get(row["version_key"], (1, None))
        row["version_count"] = total
        row["is_newest"] = row["id"] == newest
    return page


def related_search(core, args, keywords):
    """Another wording that has results, for a search that found nothing."""
    return next((other for other in related_queries(keywords) if core.search_page(**{**args, "query": other}, limit=1)["total"]), None)


def size_text(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def date_text(value):
    try:
        return dt.datetime.fromtimestamp(float(value)).strftime("%Y.%m.%d")
    except (TypeError, ValueError, OSError):
        return "—"


def file_count(n):
    return t("{count} file", count=f"{n:,}") if n == 1 else t("{count} files", count=f"{n:,}")


class FileModel(QAbstractListModel):
    """Holds loaded pages only; Qt asks for the next page as the view nears the end."""

    def __init__(self, load_more, dragged=None, parent=None):
        super().__init__(parent)
        self.rows = []
        self.total = 0
        self.load_more = load_more
        self.dragged = dragged

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.DisplayRole:
            return row["name"]
        if role == Qt.UserRole:
            return row
        if role == Qt.AccessibleTextRole:
            return f"{row['name']}, {kind_text(row)}"
        if role == Qt.ToolTipRole:
            return as_plain(f"{row['name']}, {kind_text(row)}")

    def flags(self, index):
        flags = super().flags(index)
        return flags | Qt.ItemIsDragEnabled if index.isValid() else flags

    def mimeTypes(self):
        return ["text/uri-list"]

    def mimeData(self, indexes):
        rows = [index.data(Qt.UserRole) for index in indexes]
        if self.dragged:
            for row in rows:
                self.dragged(row["id"])
        return file_mime(rows)

    def supportedDragActions(self):
        # Never a move: originals stay where they are.
        return Qt.CopyAction

    def replace(self, rows, total):
        self.beginResetModel()
        self.rows = rows
        self.total = total
        self.endResetModel()

    def canFetchMore(self, parent=QModelIndex()):
        return not parent.isValid() and len(self.rows) < self.total

    def fetchMore(self, parent=QModelIndex()):
        if parent.isValid() or len(self.rows) >= self.total:
            return
        rows = self.load_more(len(self.rows))
        if not rows:
            self.total = len(self.rows)
            return
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows) + len(rows) - 1)
        self.rows.extend(rows)
        self.endInsertRows()


class FileDelegate(QStyledItemDelegate):
    def __init__(self, mode, covers, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.covers = covers
        self.cell = QSize(MIN_CELL_WIDTH, 300)

    def sizeHint(self, option, index):
        return self.cell if self.mode == "grid" else QSize(400, 66)

    def paint(self, painter, option, index):
        row = index.data(Qt.UserRole)
        if not row:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        if self.mode == "grid":
            self.paint_tile(painter, QRectF(option.rect).adjusted(5, 5, -5, -5), row, selected, hover)
        else:
            self.paint_row(painter, QRectF(option.rect), row, selected, hover)
        painter.restore()

    def paint_tile(self, painter, r, row, selected, hover):
        target = QRectF(r.x(), r.y(), r.width(), r.width() * COVER_SIZE.height() / COVER_SIZE.width())
        cover = self.covers.cover(row, COVER_SIZE.width(), COVER_SIZE.height())
        if cover is None:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(RAISED))
            painter.drawRoundedRect(target, 9, 9)
        else:
            painter.drawPixmap(target, cover, QRectF(0, 0, COVER_SIZE.width(), COVER_SIZE.height()))
        if selected or hover:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(ACCENT if selected else HAIRLINE_STRONG), 1.4))
            painter.drawRoundedRect(target.adjusted(.5, .5, -.5, -.5), 9, 9)
        painter.setFont(ui_font(13))
        painter.setPen(QColor(TEXT_3 if row.get("missing") else TEXT))
        name = painter.fontMetrics().elidedText(row["name"], Qt.ElideMiddle, int(r.width() - 4))
        painter.drawText(QRectF(r.x() + 2, target.bottom() + 6, r.width() - 4, 20), Qt.AlignVCenter, name)
        painter.setFont(ui_font(12))
        painter.setPen(QColor(TEXT_3))
        painter.drawText(QRectF(r.x() + 2, target.bottom() + 25, r.width() - 4, 18), Qt.AlignVCenter,
                         f"{kind_text(row)}   {date_text(row.get('mtime'))}")
        if row.get("favorite"):
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(BG))
            painter.drawEllipse(QRectF(target.right() - 30, target.y() + 8, 22, 22))
            icon("star", ACCENT, 14).paint(painter, QRect(int(target.right() - 26), int(target.y() + 12), 14, 14))

    def paint_row(self, painter, rect, row, selected, hover):
        r = rect.adjusted(2, 2, -6, -2)
        if selected or hover:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(SELECTED if selected else RAISED))
            painter.drawRoundedRect(r, 8, 8)
        else:
            painter.setPen(QPen(QColor(HAIRLINE), 1))
            painter.drawLine(int(r.x() + 12), int(r.bottom() + 1), int(r.right() - 12), int(r.bottom() + 1))
        icon(file_glyph(row), ACCENT if selected else TEXT_3, 20).paint(painter, QRect(int(r.x() + 13), int(r.y() + 19), 20, 20))
        text_x = r.x() + 46
        meta_w = 150 if r.width() > 620 else 0
        text_w = max(120, r.width() - 60 - meta_w)
        painter.setFont(ui_font(14))
        painter.setPen(QColor(TEXT_3 if row.get("missing") else TEXT))
        name_rect = QRectF(text_x, r.y() + 8, text_w, 22)
        terms = row.get("match_terms", [])
        version = version_tag(row)
        reserved = (20 if row.get("favorite") else 0) + (100 if version else 0)
        name = painter.fontMetrics().elidedText(row["name"], Qt.ElideMiddle, int(text_w) - reserved)
        name_end = text_x + painter.fontMetrics().horizontalAdvance(name)
        draw_marked(painter, name_rect, name, terms, QColor(TEXT_3 if row.get("missing") else TEXT))
        painter.setFont(ui_font(12))
        if version:
            painter.setPen(QColor(ACCENT if row.get("is_newest") else TEXT_3))
            painter.drawText(QRectF(name_end + 8, r.y() + 8, 92, 22), Qt.AlignVCenter, version)
            name_end += 8 + painter.fontMetrics().horizontalAdvance(version)
        snippet = t("Not at its original location") if row.get("missing") else (row.get("snippet") or t(row.get("summary") or "") or kind_text(row))
        snippet = " ".join(snippet.split())
        color = QColor(WARNING if row.get("missing") else TEXT_3)
        draw_marked(painter, QRectF(text_x, r.y() + 32, text_w, 19), painter.fontMetrics().elidedText(snippet, Qt.ElideRight, int(text_w)), terms, color)
        painter.setPen(QColor(TEXT_3))
        if meta_w:
            source = t(MATCH_LABELS[row["match_source"]]) if row.get("match_terms") and row.get("match_source") in MATCH_LABELS else kind_text(row)
            painter.drawText(QRectF(r.right() - 150, r.y() + 8, 138, 22), Qt.AlignRight | Qt.AlignVCenter, source)
            painter.drawText(QRectF(r.right() - 150, r.y() + 32, 138, 19), Qt.AlignRight | Qt.AlignVCenter, date_text(row.get("mtime")))
        if row.get("favorite"):
            icon("star", ACCENT, 14).paint(painter, QRect(int(name_end + 6), int(r.y() + 12), 13, 13))
            name_end += 22
        if row.get("links"):
            icon("link", ACCENT, 14).paint(painter, QRect(int(name_end + 8), int(r.y() + 12), 13, 13))
            painter.setPen(QColor(TEXT_3))
            painter.drawText(QRectF(name_end + 24, r.y() + 8, 30, 22), Qt.AlignVCenter, str(row["links"]))


def version_tag(row):
    if row.get("version_count", 1) < 2:
        return ""
    return t("Latest of {count}", count=row["version_count"]) if row.get("is_newest") else t("Older version")


def table_widget(rows, terms=()):
    """A read-only table of a sheet's first rows, with the first row as its header."""
    widget = QTableWidget()
    widget.setEditTriggers(QAbstractItemView.NoEditTriggers)
    widget.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    widget.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    widget.verticalHeader().hide()
    widget.horizontalHeader().setMaximumSectionSize(240)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    fill_table(widget, rows, terms)
    return widget


def fill_table(widget, rows, terms=()):
    widget.clear()
    columns = max(len(row) for row in rows)
    widget.setColumnCount(columns)
    widget.setRowCount(len(rows) - 1)
    widget.setHorizontalHeaderLabels(rows[0] + [""] * (columns - len(rows[0])))
    first = None
    wanted = [term.casefold() for term in terms]
    for y, row in enumerate(rows[1:]):
        for x, value in enumerate(row):
            item = QTableWidgetItem(value)
            widget.setItem(y, x, item)
            if value and any(term in value.casefold() for term in wanted):
                item.setBackground(QColor("#2F4436"))
                item.setForeground(QColor(ACCENT))
                first = first or item
    widget.resizeColumnsToContents()
    if first:
        widget.scrollToItem(first)


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()


def draw_marked(painter, rect, text, terms, color):
    metrics = painter.fontMetrics()
    x = rect.x()
    for part, hit in highlight_spans(text, terms):
        painter.setPen(QColor(ACCENT) if hit else color)
        painter.drawText(QRectF(x, rect.y(), rect.right() - x, rect.height()), Qt.AlignVCenter, part)
        x += metrics.horizontalAdvance(part)


class GridView(QListView):
    """Fills the width with whole columns instead of leaving a gap on the right."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_cells()

    def fit_cells(self):
        delegate = self.itemDelegate()
        # Always leave room for the scroll bar: it can appear after this runs,
        # and a resize of the viewport alone does not call resizeEvent here.
        scroll_bar = self.style().pixelMetric(QStyle.PM_ScrollBarExtent, None, self.verticalScrollBar())
        width = self.width() - 2 * self.frameWidth() - scroll_bar - 1
        columns = max(2, width // MIN_CELL_WIDTH)
        cell_width = width // columns
        cover_height = round((cell_width - 10) * COVER_SIZE.height() / COVER_SIZE.width())
        cell = QSize(cell_width, cover_height + GRID_TEXT_HEIGHT + 10)
        if cell != delegate.cell:
            delegate.cell = cell
            self.setGridSize(cell)


class QuickSearch(QFrame):
    """Find a file from anywhere, act on it, and go straight back to what you were doing."""

    ROWS = 6
    ROW_HEIGHT = 66

    def __init__(self, archive):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.archive = archive
        self.args = {}
        self.previous_window = None
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(6)
        box = QFrame()
        box.setObjectName("searchBox")
        row = QHBoxLayout(box)
        row.setContentsMargins(12, 4, 12, 4)
        magnify = QLabel()
        magnify.setPixmap(icon("search", TEXT_3, 18).pixmap(18, 18))
        row.addWidget(magnify)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.installEventFilter(self)
        row.addWidget(self.search, 1)
        layout.addWidget(box)
        self.model = FileModel(lambda offset: load_results(self.archive.core, self.args, offset)["rows"], archive.mark_used, self)
        self.list = QListView()
        self.list.setModel(self.model)
        self.list.setItemDelegate(FileDelegate("list", archive.covers, self.list))
        self.list.setUniformItemSizes(True)
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setDragEnabled(True)
        self.list.setDragDropMode(QAbstractItemView.DragOnly)
        self.list.setDefaultDropAction(Qt.CopyAction)
        # Typing always stays in the search box.
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.activated.connect(lambda index: self.open())
        layout.addWidget(self.list)
        self.hint = label("", "caption")
        self.hint.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(self.hint)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.timer.start())

    def toggle(self):
        if self.isVisible():
            self.dismiss()
            return
        dialog = QApplication.activeModalWidget()
        if dialog:  # It blocks every other window, so a search bar here could not be typed in.
            dialog.raise_()
            dialog.activateWindow()
            return
        self.previous_window = foreground_window()
        self.search.setPlaceholderText(t("Search your files"))
        self.search.clear()
        self.refresh()
        area = (QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()).availableGeometry()
        self.move(area.center().x() - self.width() // 2, area.top() + area.height() // 4)
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()

    def refresh(self):
        self.timer.stop()
        query = self.search.text().strip()
        parsed = parse_query(query)
        self.args = {"query": parsed["text"], "sort": "recent", "relevance": True, **resolve_filters(parsed)}
        core = self.archive.core
        page = load_results(core, self.args)
        replaced = related_search(core, self.args, parsed["text"]) if parsed["text"] and not page["total"] else None
        if replaced:
            self.args["query"] = replaced
            page = load_results(core, self.args)
        rows = page["rows"]
        self.model.replace(rows, page["total"])
        if rows:
            self.list.setCurrentIndex(self.model.index(0))
        self.list.setFixedHeight(min(len(rows), self.ROWS) * self.ROW_HEIGHT)
        self.list.setVisible(bool(rows))
        if replaced:
            self.hint.setText(t('No results for "{query}". Showing "{other}" instead.', query=parsed["text"], other=replaced))
        elif rows:
            self.hint.setText(native_keys(t("Enter to open  ·  Ctrl C to copy  ·  Ctrl Enter to show in folder  ·  Esc to close")))
        elif query:
            self.hint.setText(t('Nothing found for "{query}"', query=query) + "  ·  " + t("Enter to look in the main window"))
        else:
            self.hint.setText(t("Enter to add a folder in the main window"))
        self.adjustSize()

    def dismiss(self):
        """Back to the window that was in use, so a paste lands there straight away."""
        give_keyboard_to(self.previous_window)
        self.hide()

    def paintEvent(self, event):
        # Painted here: a stylesheet background does not fill a translucent window.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(BG))
        painter.setPen(QPen(QColor(HAIRLINE_STRONG), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 14, 14)

    def step(self, by):
        row = self.list.currentIndex().row() + by
        if 0 <= row < self.model.rowCount():
            self.list.setCurrentIndex(self.model.index(row))

    def current_id(self):
        row = self.list.currentIndex().data(Qt.UserRole)
        return row["id"] if row else None

    def open(self):
        file_id = self.current_id()
        self.hide()
        if file_id:
            self.archive.open_file(file_id)
        else:
            self.archive.show_main(self.search.text().strip())

    def copy(self):
        file_id = self.current_id()
        if file_id is None:
            return  # Nothing is showing, so the clipboard stays as it was.
        if self.archive.copy_file(file_id):
            self.dismiss()
            self.archive.confirm_copied(file_id)
        else:
            self.hint.setText(t("File unavailable"))

    def copy_words(self):
        if self.current_id() is not None and self.archive.copy_text(self.current_id()):
            self.dismiss()

    def reveal(self):
        file_id = self.current_id()
        if file_id:
            self.hide()
            self.archive.reveal_file(file_id)

    def eventFilter(self, watched, event):
        if watched is self.search and event.type() == QEvent.KeyPress:
            key = event.key()
            if self.timer.isActive() and (key in (Qt.Key_Down, Qt.Key_Up, Qt.Key_Return, Qt.Key_Enter) or event.matches(QKeySequence.Copy) or key == Qt.Key_C and event.modifiers() & Qt.ControlModifier):
                self.refresh()  # Act on what is in the box, not on what was there a moment ago.
            if key == Qt.Key_Escape:
                self.dismiss()
            elif key in (Qt.Key_Down, Qt.Key_Up):
                self.step(1 if key == Qt.Key_Down else -1)
            elif key in (Qt.Key_Return, Qt.Key_Enter):
                self.reveal() if event.modifiers() & Qt.ControlModifier else self.open()
            elif key == Qt.Key_C and event.modifiers() & Qt.ControlModifier and event.modifiers() & Qt.ShiftModifier:
                self.copy_words()
            elif event.matches(QKeySequence.Copy) and not self.search.hasSelectedText():
                self.copy()
            else:
                return super().eventFilter(watched, event)
            return True
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        super().changeEvent(event)
        # A drag leaves the window without being finished with it.
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow() and self.list.state() != QAbstractItemView.DraggingState:
            self.hide()


class LinkPicker(QDialog):
    """Pick files to tie together. Search and tick as many as you like, across searches."""

    def __init__(self, archive, base=None, chosen=()):
        super().__init__(archive)
        self.core = archive.core
        self.base = base
        self.chosen = {row["id"]: row["name"] for row in chosen}
        base_row = self.core.get_file(base) if base else None
        self.setWindowTitle(t("Link files"))
        self.setMinimumSize(520, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(10)
        layout.addWidget(label(t("Link {name} with", name=base_row["name"]) if base_row else t("Choose files to link"), "title"))
        self.box = QLineEdit()
        self.box.setPlaceholderText(t("Search for a file"))
        self.box.textChanged.connect(self.fill)
        self.box.returnPressed.connect(self.tick_first)
        layout.addWidget(self.box)
        self.heading = label("", "caption")
        layout.addWidget(self.heading)
        self.results = QListWidget()
        self.results.itemChanged.connect(self.ticked)
        self.results.itemClicked.connect(self.clicked)
        layout.addWidget(self.results, 1)
        end = QHBoxLayout()
        self.summary = label("", "caption")
        end.addWidget(self.summary, 1)
        end.addWidget(button(t("Cancel"), self.reject, None, "quiet"))
        self.confirm = button(t("Link"), self.accept, "link", "primary")
        end.addWidget(self.confirm)
        layout.addLayout(end)
        self.fill()
        self.box.setFocus()

    def fill(self):
        text = self.box.text().strip()
        rows = []
        if text:
            self.heading.setText(t("Results"))
            rows = [dict(row, why="") for row in self.core.search(text, limit=PICKER_ROWS)]
        else:
            offers = self.core.suggest_links(self.base, PICKER_ROWS) if self.base else []
            self.heading.setText(t("Suggested") if offers else t("Recent files"))
            rows = offers or [dict(row, why="") for row in self.core.search("", limit=PICKER_ROWS)]
        linked = {row["id"] for row in self.core.bundle(self.base)} if self.base else set()
        self.results.blockSignals(True)
        self.results.clear()
        for row in rows:
            if row["id"] == self.base or row["id"] in linked:
                continue
            item = QListWidgetItem(f"{row['name']}   ·   {t(row['why'])}" if row.get("why") else row["name"])
            item.setData(Qt.UserRole, (row["id"], row["name"]))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if row["id"] in self.chosen else Qt.Unchecked)
            self.results.addItem(item)
        self.results.blockSignals(False)
        self.update_summary()

    def clicked(self, item):
        """A click anywhere on the row counts, since the box itself is small."""
        item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)

    def ticked(self, item):
        file_id, name = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self.chosen[file_id] = name
        else:
            self.chosen.pop(file_id, None)
        self.update_summary()

    def tick_first(self):
        if self.results.count():
            self.clicked(self.results.item(0))
            self.box.selectAll()

    def update_summary(self):
        count = len(self.chosen) + (1 if self.base else 0)
        self.summary.setText(t("{files} selected", files=file_count(len(self.chosen))))
        self.confirm.setEnabled(count >= 2)

    def ids(self):
        return ([self.base] if self.base else []) + list(self.chosen)


class Island(QFrame):
    """A small bar at the top of the screen for news worth a glance. It never takes the keyboard."""

    WIDTH = 480
    HEIGHT = 56

    def __init__(self, archive):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.archive = archive
        self.file_id = None
        self.growing = None
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        row = QHBoxLayout(self)
        row.setContentsMargins(20, 6, 12, 6)
        row.setSpacing(10)
        self.glyph = QLabel()
        row.addWidget(self.glyph)
        words = QVBoxLayout()
        words.setSpacing(0)
        self.title = label("", "detailTitle")
        self.detail = label("", "caption")
        words.addWidget(self.title)
        words.addWidget(self.detail)
        row.addLayout(words, 1)
        self.copy_button = button(t("Copy file"), self.copy, None, "quiet")
        self.open_button = button(t("Open"), self.open, None, "quiet")
        for action in (self.copy_button, self.open_button):
            action.setFocusPolicy(Qt.NoFocus)
            row.addWidget(action)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)

    def present(self, title, detail, file_id=None, seconds=6):
        self.file_id = file_id
        self.title.setText(self.title.fontMetrics().elidedText(title, Qt.ElideMiddle, 250))
        self.detail.setText(detail)
        self.glyph.setPixmap(icon("book", ACCENT, 20).pixmap(20, 20))
        self.copy_button.setText(t("Copy file"))
        self.open_button.setText(t("Open"))
        self.copy_button.setVisible(file_id is not None)
        self.open_button.setVisible(file_id is not None)
        area = (QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()).availableGeometry()
        width = max(self.WIDTH, self.minimumSizeHint().width())
        end = QRect(area.center().x() - width // 2, area.top() + 10, width, self.HEIGHT)
        if self.archive.reduced_motion:
            self.setGeometry(end)
            self.show()
        else:
            # Grows out of a small pill, so it reads as one thing arriving.
            self.setGeometry(QRect(area.center().x() - 90, end.y(), 180, self.HEIGHT))
            self.show()
            self.growing = QPropertyAnimation(self, b"geometry", self)
            self.growing.setDuration(220)
            self.growing.setEndValue(end)
            self.growing.setEasingCurve(QEasingCurve.OutCubic)
            self.growing.start()
        self.timer.start(seconds * 1000)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(BG))
        painter.setPen(QPen(QColor(HAIRLINE_STRONG), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), self.HEIGHT / 2, self.HEIGHT / 2)

    def enterEvent(self, event):
        self.timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.timer.start(2000)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        file_id = self.file_id
        self.hide()
        if file_id:
            self.archive.show_main()
            self.archive.select_file(file_id)

    def copy(self):
        self.hide()
        self.archive.copy_file(self.file_id)

    def open(self):
        self.hide()
        self.archive.open_file(self.file_id)


class NavButton(QPushButton):
    def __init__(self, title, glyph, callback):
        super().__init__(literal(title))
        self.title = title
        self.setObjectName("nav")
        self.setCursor(Qt.PointingHandCursor)
        self.setCheckable(True)
        self.setIcon(icon(glyph, TEXT_2, 16))
        self.setIconSize(QSize(16, 16))
        self.clicked.connect(callback)
        self.count = label("", "navCount", self)
        self.count.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.addStretch()
        layout.addWidget(self.count)

    def set_count(self, value):
        self.count.setText("" if value is None else f"{value:,}")
        empty = value == 0
        if self.property("empty") != empty:
            self.setProperty("empty", empty)
            repolish(self)


class GhostDeck(QWidget):
    """Dashed outlines of the deck: the shape of what will be here."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(226)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(HAIRLINE_STRONG), 1, Qt.CustomDashLine)
        pen.setDashPattern([3, 4])
        painter.setPen(pen)
        painter.setBrush(QColor(BG))
        center = self.width() / 2
        for offset in (-2, 2, -1, 1, 0):
            painter.save()
            painter.translate(center + offset * 62, self.height() - 118 + offset * offset * 5)
            painter.rotate(offset * 7)
            scale = 1 - abs(offset) * .07
            painter.drawRoundedRect(QRectF(-55 * scale, -75 * scale, 110 * scale, 150 * scale), 8, 8)
            painter.restore()


class WorkerSignals(QObject):
    progress = Signal(object)
    finished = Signal(object)
    failed = Signal(str)
    changed = Signal(str, object)
    reading = Signal(int)
    sorted = Signal(object)
    copying = Signal(float)
    copied = Signal(object)


_QSS = """
* { font-family:@fonts; font-size:13px; color:@text; }
QMainWindow,QDialog { background:@bg; }
QWidget#root,QWidget#main,QWidget#content,QScrollArea,QWidget#holder { background:@bg; }
QFrame#sidebar { background:@bg; border-right:1px solid @hairline; }
QFrame#details { background:@bg; border-left:1px solid @hairline; }
QLabel { background:transparent; }
QLabel#brand { font-size:19px; font-weight:400; }
QLabel#brandSub,QLabel#caption { font-size:12px; color:@text3; }
QLabel#section { font-size:12px; color:@text3; }
QLabel#navCount { font-size:12px; color:@text3; }
QLabel#title { font-size:19px; font-weight:400; }
QLabel#hint { font-size:13px; color:@text2; }
QLabel#keycap { font-size:11px; color:@text3; border:1px solid @hairline; border-radius:4px; padding:2px 6px; }
QLabel#detailTitle { font-size:16px; font-weight:400; }
QLabel#chip { color:@accent; border:1px solid @hairlineStrong; border-radius:6px; padding:3px 8px; font-size:12px; }
QLabel#match { color:@accent; font-size:12px; }
QLabel#warning { color:@warning; font-size:12px; }
QPushButton { background:transparent; border:1px solid @hairline; border-radius:6px; padding:6px 11px; color:@text; }
QPushButton:hover { background:@raised; border-color:@hairlineStrong; }
QPushButton:pressed { background:@raisedHover; }
QPushButton:focus { border-color:@text3; }
QPushButton:disabled { color:@text3; }
QPushButton#primary { background:@accent; color:@onAccent; border:none; padding:7px 13px; }
QPushButton#primary:hover { background:#D5EBDC; }
QPushButton#nav { text-align:left; background:transparent; border:none; border-radius:6px; color:@text2; padding:7px 9px; }
QPushButton#nav:hover { background:@raised; color:@text; }
QPushButton#nav:checked { background:@raisedHover; color:@text; }
QPushButton#nav[empty="true"] { color:@text3; }
QPushButton#quiet { background:transparent; border:none; color:@text2; padding:6px 8px; }
QPushButton#quiet:hover { color:@text; background:@raised; }
QPushButton#view { padding:5px 9px; background:transparent; border:none; border-radius:5px; color:@text3; }
QPushButton#view:hover { color:@text; }
QPushButton#view:checked { background:@raisedHover; color:@text; }
QPushButton#recentQuery { background:transparent; border:1px solid @hairline; border-radius:6px; padding:3px 8px; font-size:12px; color:@text2; }
QFrame#viewSwitch { border:1px solid @hairline; border-radius:7px; }
QLineEdit { background:@raised; border:1px solid @hairline; border-radius:6px; padding:7px 10px; selection-background-color:#3E5543; }
QLineEdit:focus { border-color:@hairlineStrong; }
QFrame#tip { background:@selected; border:1px solid @hairline; border-radius:8px; }
QFrame#searchBox { background:@raised; border:1px solid @hairline; border-radius:10px; }
QLineEdit#search { border:none; background:transparent; padding:8px 2px; font-size:15px; }
QComboBox { background:transparent; border:1px solid @hairline; border-radius:6px; padding:4px 10px; min-height:20px; color:@text2; }
QComboBox:hover { border-color:@hairlineStrong; color:@text; }
QComboBox[active="true"] { border-color:@hairlineStrong; color:@text; }
QComboBox::drop-down { border:none; width:18px; }
QComboBox QAbstractItemView { background:@raised; selection-background-color:@raisedHover; border:1px solid @hairlineStrong; padding:4px; outline:none; }
QListView { background:transparent; border:none; outline:none; }
QListView::item { background:transparent; }
QTableWidget { background:transparent; border:none; gridline-color:@hairline; color:@text2; selection-background-color:@raisedHover; outline:none; }
QHeaderView { background:@bg; border:none; }
QTableCornerButton::section { background:@bg; border:none; }
QHeaderView::section { background:@raised; color:@text; border:none; border-right:1px solid @hairline; border-bottom:1px solid @hairline; padding:4px 8px; }
QTextEdit { background:transparent; border:none; padding:0; selection-background-color:#3E5543; color:@text2; }
QScrollArea { border:none; }
QScrollBar:vertical { background:transparent; width:6px; margin:0; }
QScrollBar::handle:vertical { background:@hairlineStrong; border-radius:3px; min-height:28px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical { background:transparent; }
QSplitter::handle { background:transparent; }
QCheckBox { spacing:8px; color:@text2; }
QCheckBox::indicator { width:14px; height:14px; border:1px solid @hairlineStrong; border-radius:3px; background:transparent; }
QCheckBox::indicator:checked { background:@accent; border-color:@accent; }
QRadioButton { padding:4px; spacing:8px; }
QRadioButton::indicator { width:14px; height:14px; border:1px solid @hairlineStrong; border-radius:7px; background:transparent; }
QRadioButton::indicator:checked { background:@accent; border:3px solid @bg; }
QMenu { background:@raised; border:1px solid @hairlineStrong; padding:4px; }
QMenu::item { padding:7px 18px; border-radius:4px; }
QMenu::item:selected { background:@raisedHover; }
QToolTip { background:@raised; border:1px solid @hairlineStrong; padding:6px; color:@text; }
"""
STYLE = _QSS
for _token, _value in (("@fonts", ",".join(f"'{family}'" for family in FONT_FAMILIES)), ("@bg", BG), ("@raisedHover", RAISED_HOVER), ("@raised", RAISED), ("@selected", SELECTED),
                       ("@hairlineStrong", HAIRLINE_STRONG), ("@hairline", HAIRLINE), ("@text2", TEXT_2), ("@text3", TEXT_3),
                       ("@text", TEXT), ("@accent", ACCENT), ("@onAccent", ON_ACCENT), ("@warning", WARNING)):
    STYLE = STYLE.replace(_token, _value)


class ArchiveWindow(QMainWindow):
    def __init__(self, demo=False, language_override=None):
        super().__init__()
        load_local_fonts()
        self.demo = demo
        self.data_root = BASE / ".demo-data" if demo else data_location(BASE)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.config_file = self.data_root / "ui-state.json"
        self.config = self.load_config()
        set_language(language_override or self.config.get("language", "en"))
        show_qt_words(language())
        self.core = ArchiveCore(demo_data_dir(BASE, language()) if demo else self.data_root)
        self.setWindowTitle(app_name())
        self.resize(1360, 850)
        self.setMinimumSize(1100, 700)
        self.place = "all"
        self.used_total = 0
        self.home_stale = False
        self.view_mode = "fan"
        self.view_before_search = None
        self.selected_id = None
        self.rows = []
        self.search_args = {}
        self.scanning = False
        self.scan_automatic = False
        self.scan_thread = None
        self.scan_refreshed = 0.0
        self.closing = False
        self.quitting = False
        self.last_scan_end = 0.0
        self.tray = None
        self.hotkey = None
        self.quick = None
        self.island = None
        self.folders = None
        self.replaced_query = None
        self.empty_adds_folder = False
        self.starter_boxes = []
        self.status_timer = QTimer(self)
        self.status_timer.setSingleShot(True)
        self.status_timer.timeout.connect(lambda: self.show_status(""))
        self.source_dialog_open = False
        self.cancel = threading.Event()
        self.covers = CoverLoader(self)
        self.covers.ready.connect(self.update_covers)
        self.signals = WorkerSignals(self)
        self.signals.progress.connect(self.scan_progress)
        self.signals.finished.connect(self.scan_finished)
        self.signals.failed.connect(self.scan_failed)
        self.signals.changed.connect(self.note_change)
        self.signals.copying.connect(lambda share: self.copy_box.setValue(round(share * 1000)))
        self.signals.copied.connect(self.archive_copied)
        self.changed_paths = {}
        self.signals.reading.connect(self.reading_progress)
        self.signals.sorted.connect(self.finish_sorting)
        self.assistant = None
        self.assistant_greeted = False
        self.last_sorted = []
        self.scan_partial = False
        self.sorting = []
        self.ocr_thread = None
        self.ocr_stop = threading.Event()
        self.change_timer = QTimer(self)
        self.change_timer.setSingleShot(True)
        self.change_timer.timeout.connect(self.scan_changes)
        self.reduced_motion = bool(self.config.get("reduced_motion", False))
        self.query_timer = QTimer(self)
        self.query_timer.setSingleShot(True)
        self.query_timer.setInterval(120)
        self.query_timer.timeout.connect(self.refresh_results)
        self.shortcuts = []
        self.build()
        self.refresh()
        self.setup_shortcuts()
        self.watch = QTimer(self)
        self.watch.timeout.connect(self.auto_scan)
        self.watch.start(30000)

    def load_config(self):
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_config(self):
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")

    def build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(self.build_sidebar())

        main = QWidget()
        main.setObjectName("main")
        shell.addWidget(main, 1)
        body = QVBoxLayout(main)
        body.setContentsMargins(26, 18, 26, 12)
        body.setSpacing(0)
        body.addLayout(self.build_toolbar())
        body.addSpacing(12)
        self.filter_row = QWidget()
        self.filter_row.setLayout(self.build_filters())
        self.filter_row.layout().setContentsMargins(0, 0, 0, 0)
        body.addWidget(self.filter_row)
        body.addSpacing(8)
        self.chip_row = QWidget()
        self.query_chips = QHBoxLayout(self.chip_row)
        self.query_chips.setContentsMargins(0, 4, 0, 4)
        self.query_chips.setSpacing(6)
        body.addWidget(self.chip_row)
        body.addSpacing(8)
        self.tip = QFrame()
        self.tip.setObjectName("tip")
        tip_row = QHBoxLayout(self.tip)
        tip_row.setContentsMargins(14, 8, 10, 8)
        self.tip_text = label("", "hint")
        tip_row.addWidget(self.tip_text, 1)
        self.tip_action = button("", self.accept_tip, None, "primary")
        tip_row.addWidget(self.tip_action)
        tip_row.addWidget(button(t("Not now"), self.dismiss_tip, None, "quiet"))
        self.tip.hide()
        self.tip_id = None
        self.tip_word = ""
        body.addWidget(self.tip)
        body.addSpacing(6)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(1)
        self.content = QWidget()
        self.content.setObjectName("content")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        content_header = QHBoxLayout()
        self.section_title = label("", "title")
        self.result_count = label("", "caption")
        content_header.addWidget(self.section_title)
        content_header.addSpacing(10)
        content_header.addWidget(self.result_count, 0, Qt.AlignBottom)
        content_header.addStretch()
        self.collect_all = button(t("Add all to collection"), self.collect_results, None, "quiet")
        content_header.addWidget(self.collect_all)
        self.clear_filters = button(t("Clear filters"), self.reset_filters, None, "quiet")
        content_header.addWidget(self.clear_filters)
        self.content_layout.addLayout(content_header)
        self.content_layout.addSpacing(8)
        self.pages = QStackedWidget()
        self.content_layout.addWidget(self.pages, 1)
        self.model = FileModel(self.load_more, self.mark_used, self)
        self.build_fan_page()
        self.grid = self.make_view("grid")
        self.list = self.make_view("list")
        self.pages.addWidget(self.grid)
        self.pages.addWidget(self.list)
        self.pages.addWidget(self.build_empty_page())
        self.pages.addWidget(self.build_links_page())
        self.splitter.addWidget(self.content)
        self.build_details()
        self.splitter.addWidget(self.details)
        self.details.hide()
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        body.addWidget(self.splitter, 1)
        self.status = label("", "caption")
        self.status.setContentsMargins(0, 8, 0, 0)
        self.status.hide()
        body.addWidget(self.status)
        self.search.installEventFilter(self)

    def build_sidebar(self):
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(208)
        nav = QVBoxLayout(side)
        nav.setContentsMargins(14, 22, 14, 14)
        nav.setSpacing(1)
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(9, 0, 0, 0)
        mark = label("")
        mark.setPixmap(icon("book", ACCENT, 20).pixmap(20, 20))
        brand_row.addWidget(mark)
        brand_row.addSpacing(4)
        brand_row.addWidget(label(app_name(), "brand"))
        brand_row.addStretch()
        nav.addLayout(brand_row)
        subtitle = label(t("Personal archive"), "brandSub")
        subtitle.setContentsMargins(9, 2, 0, 0)
        nav.addWidget(subtitle)
        nav.addSpacing(22)
        self.nav_buttons = {}
        for place, title, glyph in (("all", "All files", "grid"), ("used", "Recently used", "external"), ("recent", "Last 7 days", "clock"), ("favorites", "Favorites", "star"), ("links", "Links", "link")):
            self.add_nav(nav, place, t(title), glyph)
        nav.addSpacing(16)
        # Folders vary in number, so this part scrolls on a short window.
        places = QWidget()
        places.setObjectName("content")
        column = QVBoxLayout(places)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        collections_heading = label(t("Collections"), "section")
        collections_heading.setContentsMargins(9, 0, 0, 6)
        column.addWidget(collections_heading)
        self.collection_nav = QVBoxLayout()
        self.collection_nav.setSpacing(1)
        column.addLayout(self.collection_nav)
        self.new_collection_button = NavButton(t("New collection"), "plus", lambda checked=False: self.new_collection())
        self.new_collection_button.setCheckable(False)
        column.addWidget(self.new_collection_button)
        column.addSpacing(16)
        self.type_heading = label(t("Types"), "section")
        self.type_heading.setContentsMargins(9, 0, 0, 6)
        column.addWidget(self.type_heading)
        self.type_nav = QVBoxLayout()
        self.type_nav.setSpacing(1)
        column.addLayout(self.type_nav)
        column.addSpacing(16)
        self.folder_heading = label(t("Folders"), "section")
        self.folder_heading.setContentsMargins(9, 0, 0, 6)
        column.addWidget(self.folder_heading)
        self.folder_nav = QVBoxLayout()
        self.folder_nav.setSpacing(1)
        column.addLayout(self.folder_nav)
        column.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(places)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        scroll.viewport().setAutoFillBackground(False)
        nav.addWidget(scroll, 1)
        self.sources_button = NavButton(t("Sources"), "folder", self.show_sources)
        self.sources_button.setCheckable(False)
        nav.addWidget(self.sources_button)
        settings = NavButton(t("Settings"), "settings", self.show_settings)
        settings.setCheckable(False)
        nav.addWidget(settings)
        return side

    def add_nav(self, layout, place, title, glyph):
        b = NavButton(title, glyph, lambda checked=False, p=place: self.select_place(p))
        self.nav_buttons[place] = b
        layout.addWidget(b)

    def build_toolbar(self):
        top = QHBoxLayout()
        top.setSpacing(10)
        search_box = QFrame()
        search_box.setObjectName("searchBox")
        search_box.setFixedHeight(44)
        search_layout = QHBoxLayout(search_box)
        search_layout.setContentsMargins(13, 0, 10, 0)
        magnify = label("")
        magnify.setPixmap(icon("search", TEXT_3, 18).pixmap(18, 18))
        search_layout.addWidget(magnify)
        search_layout.addSpacing(6)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setAccessibleName(t("Search names, text and tags"))
        self.search.setPlaceholderText(t("Search names, text or tags, e.g. last 7 days pdfs contract"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.query_changed)
        self.search.returnPressed.connect(self.commit_query)
        search_layout.addWidget(self.search, 1)
        search_layout.addWidget(label(native_keys("Ctrl K"), "keycap"), 0, Qt.AlignVCenter)
        top.addWidget(search_box, 1)
        self.scan_button = button(t("Update"), self.start_scan, "refresh", "quiet")
        top.addWidget(self.scan_button)
        self.add_button = button(t("Add folder"), self.add_source, "plus")
        top.addWidget(self.add_button)
        return top

    def build_filters(self):
        filters = QHBoxLayout()
        filters.setSpacing(6)
        self.period = QComboBox()
        for value in ("Any time", "Last 7 days", "Last 30 days", "This year"):
            self.period.addItem(t(value), value)
        self.sort = QComboBox()
        for value, title in (("recent", "Recently updated"), ("name", "Name"), ("size", "Size")):
            self.sort.addItem(t(title), value)
        for combo in (self.period, self.sort):
            combo.currentIndexChanged.connect(self.refresh_results)
            filters.addWidget(combo)
        filters.addStretch()
        switch = QFrame()
        switch.setObjectName("viewSwitch")
        switch_layout = QHBoxLayout(switch)
        switch_layout.setContentsMargins(2, 2, 2, 2)
        switch_layout.setSpacing(2)
        self.view_group = QButtonGroup(self)
        self.view_buttons = {}
        for mode, title, glyph in (("fan", "Deck", "deck"), ("grid", "Grid", "grid"), ("list", "List", "list")):
            b = button(t(title), lambda checked=False, m=mode: self.set_view(m), glyph, "view", True)
            self.view_buttons[mode] = b
            self.view_group.addButton(b)
            switch_layout.addWidget(b)
        self.view_buttons[self.view_mode].setChecked(True)
        filters.addWidget(switch)
        return filters

    def build_fan_page(self):
        page = QWidget()
        page.setObjectName("content")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.deck = ArchiveDeck(self.covers)
        self.deck.setMinimumHeight(230)
        self.deck.installEventFilter(self)
        self.deck.set_reduced_motion(self.reduced_motion)
        self.deck.selected.connect(self.select_file)
        self.deck.activated.connect(self.open_file)
        self.deck.dragged.connect(self.drag_file)
        self.deck.menu_requested.connect(self.show_file_menu)
        layout.addWidget(self.deck, 1)
        self.deck_focus_label = label("", "detailTitle")
        self.deck_focus_label.setAlignment(Qt.AlignCenter)
        self.deck_focus_label.setWordWrap(True)
        layout.addWidget(self.deck_focus_label)
        self.deck_focus_meta = label("", "caption")
        self.deck_focus_meta.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.deck_focus_meta)
        layout.addSpacing(4)
        deck_controls = QHBoxLayout()
        deck_controls.addStretch()
        self.deck_mode = button(t("Spread out"), self.toggle_deck_layout, None, "quiet")
        deck_controls.addWidget(self.deck_mode)
        deck_controls.addStretch()
        layout.addLayout(deck_controls)
        self.pages.addWidget(page)

    def build_empty_page(self):
        self.starter_boxes = []
        self.empty = QWidget()
        self.empty.setObjectName("content")
        layout = QVBoxLayout(self.empty)
        layout.addStretch()
        self.ghost_deck = GhostDeck()
        layout.addWidget(self.ghost_deck)
        layout.addSpacing(18)
        self.empty_title = label("", "title")
        self.empty_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.empty_title)
        layout.addSpacing(6)
        self.empty_hint = label("", "hint")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)
        layout.addSpacing(18)
        self.starter = QWidget()
        starter_row = QHBoxLayout(self.starter)
        starter_row.addStretch()
        self.starter_list = QVBoxLayout()
        self.starter_list.setSpacing(8)
        starter_row.addLayout(self.starter_list)
        starter_row.addStretch()
        layout.addWidget(self.starter)
        self.suggestion_row = QWidget()
        self.suggestions = QHBoxLayout(self.suggestion_row)
        self.suggestions.setContentsMargins(0, 0, 0, 10)
        self.suggestions.setSpacing(6)
        layout.addWidget(self.suggestion_row)
        row = QHBoxLayout()
        row.addStretch()
        self.empty_action = button(t("Add folder"), self.empty_clicked, "plus", "primary")
        row.addWidget(self.empty_action)
        self.empty_other = button(t("Choose another folder"), lambda checked=False: self.add_source(), None, "quiet")
        row.addWidget(self.empty_other)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        return self.empty

    def build_links_page(self):
        self.links_page = QWidget()
        self.links_page.setObjectName("content")
        layout = QVBoxLayout(self.links_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.map_find = QLineEdit()
        self.map_find.setPlaceholderText(t("Find on this board"))
        self.map_find.setClearButtonEnabled(True)
        self.map_find.setMaximumWidth(280)
        self.map_find.textChanged.connect(self.find_on_map)
        self.map_find.returnPressed.connect(self.go_to_match)
        tools.addWidget(self.map_find)
        tools.addWidget(label(t("Drag one file onto another to link them. Double-click to open."), "caption"), 1)
        tools.addWidget(button(t("Show all"), lambda: self.map.fit(), "grid", "quiet"))
        tools.addWidget(button(t("Link files"), lambda: self.link_files(), "link", "primary"))
        layout.addLayout(tools)
        self.map_pages = QStackedWidget()
        self.map = LinkBoard(self.covers)
        self.covers.ready.connect(self.map.update)
        self.map.cut_hint = t("Right-click to remove this link")
        self.map.drop_hint = t("Release to link")
        self.map.offers_title = t("Might belong here")
        self.map.describe = lambda row: (str(row.get("ext", "")).lstrip(".").upper() or t("File"), date_text(row.get("mtime")))
        self.map.title_of = lambda name, count: t("{name} and {count} more", name=name, count=count - 1)
        self.map.set_reduced_motion(self.reduced_motion)
        self.map.selected.connect(self.select_file)
        self.map.opened.connect(self.open_file)
        self.map.tied.connect(lambda one, other: self.tie([one, other]))
        self.map.offer_taken.connect(lambda one, other: self.tie([one, other]))
        self.map.untied.connect(self.offer_untie)
        self.map.menu_requested.connect(self.show_file_menu)
        self.map_pages.addWidget(self.map)
        waiting = QWidget()
        column = QVBoxLayout(waiting)
        column.addStretch()
        for text, name in ((t("Link the files that belong together"), "title"),
                           (t("A quote, its contract and the photos from site: link them once, and finding one finds them all."), "hint"),
                           (t("Select files in any list and press Ctrl+L, or right-click a file and choose Link."), "hint")):
            line = label(text, name)
            line.setAlignment(Qt.AlignCenter)
            line.setWordWrap(True)
            column.addWidget(line)
        column.addSpacing(14)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button(t("Link files"), lambda: self.link_files(), "link", "primary"))
        row.addStretch()
        column.addLayout(row)
        column.addStretch()
        self.map_pages.addWidget(waiting)
        layout.addWidget(self.map_pages, 1)
        return self.links_page

    def show_map(self):
        graph = self.core.link_graph()
        self.map.set_graph(graph["nodes"], graph["edges"])
        self.map_pages.setCurrentIndex(0 if graph["nodes"] else 1)
        self.pages.setCurrentWidget(self.links_page)
        self.section_title.setText(t("Links"))
        self.section_title.show()
        self.result_count.setText(t("{files} linked", files=file_count(len(graph["nodes"]))))
        self.result_count.show()
        self.collect_all.hide()
        self.clear_filters.hide()
        if self.selected_id:
            self.point_map_at(self.selected_id)

    def point_map_at(self, file_id):
        """Shows a file's group, and what might belong in it, with nothing tied until someone says so."""
        if file_id not in self.map.nodes:
            self.map.focus(None)
            return
        self.map.set_offers([dict(offer, why_text=t(offer["why"])) for offer in self.core.suggest_links(file_id, MAP_OFFERS)])
        self.map.focus(file_id, centred=True)

    def find_on_map(self, text):
        text = text.strip()
        self.map.set_matches([row["id"] for row in self.core.search(text, limit=300)] if text else None)

    def go_to_match(self):
        found = sorted(self.map.matches or ())
        if found:
            self.select_file(found[0])

    def tie(self, file_ids):
        """Ties files together and shows the result wherever the person is."""
        ids = list(dict.fromkeys(file_ids))
        if len(ids) < 2:
            return
        self.core.link(ids)
        self.links_changed()
        self.show_status(t("Linked {files}", files=file_count(len(ids))), 4)

    def links_changed(self):
        if self.place == "links":
            self.show_map()
        else:
            self.refresh_results()
        if self.selected_id:
            row = self.core.get_file(self.selected_id)
            if row:
                self.show_linked(row)

    def link_files(self, base=None, chosen=()):
        picker = LinkPicker(self, base, chosen)
        if picker.exec() == QDialog.Accepted:
            self.tie(picker.ids())
        picker.deleteLater()

    def link_selection(self):
        rows = self.selected_rows()
        if len(rows) > 1:
            self.tie([row["id"] for row in rows])
        elif self.selected_id:
            self.link_files(self.selected_id)
        else:
            self.link_files()

    def active_view(self):
        return {"grid": self.grid, "list": self.list}.get(self.view_mode)

    def selected_rows(self):
        """The files picked in the list or grid, or just the one being looked at."""
        view = self.active_view()
        rows = [index.data(Qt.UserRole) for index in view.selectionModel().selectedIndexes()] if view else []
        if len(rows) > 1:
            return rows
        row = self.core.get_file(self.selected_id) if self.selected_id else None
        return [row] if row else []

    def offer_untie(self, one, other):
        menu = QMenu(self)
        menu.addAction(t("Remove this link"), lambda: self.untie(one, other))
        popup(menu, QCursor.pos())

    def untie(self, one, other):
        self.core.unlink(one, other)
        self.links_changed()

    def untie_all(self, file_id):
        self.core.unlink_all(file_id)
        self.links_changed()

    def show_linked(self, row):
        """The files tied to this one, and what can be done with all of them at once."""
        clear_layout(self.linked)
        direct = self.core.links_of(row["id"])
        head = QHBoxLayout()
        head.setSpacing(4)
        head.addWidget(label(t("Linked files"), "caption"))
        head.addStretch()
        head.addWidget(button(t("Link a file"), lambda: self.link_files(row["id"]), "plus", "quiet"))
        self.linked.addLayout(head)
        if not direct:
            return
        for item in direct[:MAX_LINKS_SHOWN]:
            name = self.path_label.fontMetrics().elidedText(item["name"], Qt.ElideMiddle, 260)
            choice = button(name, lambda checked=False, i=item["id"]: self.select_file(i), "link", "quiet")
            choice.setStyleSheet("text-align:left;")
            self.linked.addWidget(choice)
        family = self.core.bundle(row["id"])
        if len(direct) > MAX_LINKS_SHOWN:
            self.linked.addWidget(label(t("and {count} more", count=len(direct) - MAX_LINKS_SHOWN), "caption"))
        actions = QHBoxLayout()
        actions.setSpacing(2)
        actions.addWidget(button(t("Copy all ({count})", count=len(family)), lambda: self.copy_rows(self.core.bundle(row["id"])), "copy", "quiet"))
        actions.addWidget(button(t("Save as collection"), lambda: self.new_collection([item["id"] for item in self.core.bundle(row["id"])]), "book", "quiet"))
        if self.place != "links":
            actions.addWidget(button(t("See on board"), lambda: self.see_on_map(row["id"]), "link", "quiet"))
        actions.addStretch()
        self.linked.addLayout(actions)

    def see_on_map(self, file_id):
        self.selected_id = file_id
        self.select_place("links")

    def copy_rows(self, rows):
        mime = file_mime(rows)
        if not mime:
            self.show_status(t("File unavailable"))
            return False
        QApplication.clipboard().setMimeData(mime)
        self.show_status(t("{files} copied. Paste them into a chat, an email or a folder.", files=file_count(len(mime.urls()))), 5)
        return True

    def make_view(self, mode):
        view = GridView() if mode == "grid" else QListView()
        view.setModel(self.model)
        view.setItemDelegate(FileDelegate(mode, self.covers, view))
        view.setMouseTracking(True)
        view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        if mode == "grid":
            view.setViewMode(QListView.IconMode)
            view.setResizeMode(QListView.Adjust)
            view.setMovement(QListView.Static)
            view.setSpacing(0)
        # After setMovement, which turns dragging off.
        view.setDragEnabled(True)
        view.setDragDropMode(QAbstractItemView.DragOnly)
        view.setDefaultDropAction(Qt.CopyAction)
        view.setUniformItemSizes(True)
        view.setContextMenuPolicy(Qt.CustomContextMenu)
        view.customContextMenuRequested.connect(lambda position: self.list_menu(view, position))
        view.clicked.connect(lambda index: self.select_file(index.data(Qt.UserRole)["id"]))
        view.activated.connect(lambda index: self.open_file(index.data(Qt.UserRole)["id"]))
        view.selectionModel().currentChanged.connect(lambda current, previous: self.select_file(current.data(Qt.UserRole)["id"]) if current.isValid() else None)
        view.installEventFilter(self)
        return view

    def build_details(self):
        self.details = QFrame()
        self.details.setObjectName("details")
        self.details.setMinimumWidth(300)
        self.details.setMaximumWidth(380)
        detail = QVBoxLayout(self.details)
        detail.setContentsMargins(20, 6, 8, 8)
        detail.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(label(t("Preview"), "caption"))
        head.addStretch()
        head.addWidget(button(t("Close"), self.close_details, "close", "quiet"))
        detail.addLayout(head)
        self.detail_title = label("", "detailTitle")
        self.detail_title.setWordWrap(True)
        self.detail_title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail.addWidget(self.detail_title)
        self.detail_meta = label("", "caption")
        detail.addWidget(self.detail_meta)
        self.availability = label("", "warning")
        self.availability.setWordWrap(True)
        self.availability.hide()
        detail.addWidget(self.availability)
        detail.addSpacing(2)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addWidget(button(t("Open"), lambda: self.open_file(), "external", "primary"))
        actions.addWidget(button(t("Copy file"), lambda: self.copy_file(), "copy"))
        self.favorite_button = button(t("Favorite"), self.toggle_favorite, "star")
        actions.addWidget(self.favorite_button)
        actions.addStretch()
        detail.addLayout(actions)
        self.match_label = label("", "match")
        detail.addWidget(self.match_label)
        self.preview_image = QLabel()
        self.preview_image.setAlignment(Qt.AlignCenter)
        self.preview_image.hide()
        detail.addWidget(self.preview_image)
        self.version_panel = QWidget()
        self.versions = QVBoxLayout(self.version_panel)
        self.versions.setContentsMargins(0, 0, 0, 0)
        self.versions.setSpacing(0)
        self.version_panel.hide()
        detail.addWidget(self.version_panel)
        self.linked_panel = QWidget()
        self.linked = QVBoxLayout(self.linked_panel)
        self.linked.setContentsMargins(0, 0, 0, 0)
        self.linked.setSpacing(0)
        detail.addWidget(self.linked_panel)
        self.preview_table = table_widget([[""]])
        self.preview_table.hide()
        detail.addWidget(self.preview_table, 1)
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMinimumHeight(120)
        self.preview_text.setAcceptRichText(False)
        self.preview_text.setAccessibleName(t("Preview"))
        self.preview_text.document().setDocumentMargin(0)
        detail.addWidget(self.preview_text, 1)
        self.metadata_editor = QWidget(self.details)
        self.metadata_editor.hide()
        self.tags_edit = QLineEdit(self.metadata_editor)
        self.tags_edit.setPlaceholderText(t("Projects, topics, or your own clues"))
        self.tags_edit.returnPressed.connect(self.save_tags)
        self.tags_edit.editingFinished.connect(self.save_tags)
        self.tags_file_id = None
        self.path_label = label("", "caption")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail.addWidget(self.path_label)
        bottom_actions = QHBoxLayout()
        bottom_actions.setSpacing(2)
        bottom_actions.addWidget(button(t("Tags"), self.edit_metadata, "settings", "quiet"))
        bottom_actions.addWidget(button(t("Show in folder"), self.reveal_file, "folder", "quiet"))
        bottom_actions.addWidget(button(t("Collections"), self.show_collect_menu, "book", "quiet"))
        bottom_actions.addWidget(button("", self.copy_path, "copy", "quiet"))
        bottom_actions.itemAt(3).widget().setToolTip(t("Copy path"))
        bottom_actions.itemAt(3).widget().setAccessibleName(t("Copy path"))
        self.put_back_button = button(t("Put back"), self.put_back, "back", "quiet")
        self.put_back_button.setToolTip(t("Return this file to the folder it came from"))
        self.put_back_button.hide()
        bottom_actions.addWidget(self.put_back_button)
        bottom_actions.addStretch()
        detail.addLayout(bottom_actions)

    def setup_shortcuts(self):
        for shortcut in self.shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self.shortcuts = []
        for key, callback in (("Ctrl+K", self.focus_search), ("Ctrl+F", self.focus_search), ("Ctrl+R", self.start_scan), ("Escape", self.escape)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        for view in (self.grid, self.list, self.deck):
            for key, callback in (("Space", self.open_preview), ("Ctrl+L", self.link_selection)):
                shortcut = QShortcut(QKeySequence(key), view)
                shortcut.setContext(Qt.WidgetWithChildrenShortcut)
                shortcut.activated.connect(callback)
                self.shortcuts.append(shortcut)

    def eventFilter(self, watched, event):
        """Arrow keys move between the search box and the results."""
        if event.type() == QEvent.KeyPress:
            if event.matches(QKeySequence.Copy) and watched in (self.grid, self.list, self.deck):
                rows = self.selected_rows()
                self.copy_rows(rows) if len(rows) > 1 else self.copy_file()
                return True
            if watched is self.search and event.key() == Qt.Key_Down:
                return self.focus_results()
            if watched is self.list and event.key() == Qt.Key_Up and self.list.currentIndex().row() <= 0:
                self.focus_search()
                return True
        return super().eventFilter(watched, event)

    def focus_results(self):
        if not self.rows:
            return False
        if self.view_mode == "fan":
            self.deck.setFocus()
            return True
        view = self.grid if self.view_mode == "grid" else self.list
        view.setFocus()
        if not view.currentIndex().isValid():
            view.setCurrentIndex(self.model.index(0))
        return True

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def escape(self):
        if self.details.isVisible():
            self.close_details()
        elif self.search.text():
            self.search.clear()

    def query_changed(self):
        text = self.search.text().strip()
        if text and self.view_mode == "fan":
            self.view_before_search = "fan"
            self.view_mode = "list"
            self.view_buttons["list"].setChecked(True)
        elif not text and self.view_before_search:
            self.view_mode = self.view_before_search
            self.view_before_search = None
            self.view_buttons[self.view_mode].setChecked(True)
        self.query_timer.start()

    def commit_query(self):
        query = self.search.text().strip()
        if query:
            history = self.config.get("queries", [])
            self.config["queries"] = [query] + [q for q in history if q != query][:3]
            self.save_config()
        self.query_timer.stop()
        self.refresh_results()
        self.focus_results()
        self.search.setFocus()

    def refresh(self):
        self.refresh_stats()
        self.refresh_results()
        self.refresh_tip()

    def refresh_stats(self):
        stats = self.core.stats()
        week_ago = time.time() - 7 * 86400
        groups = {}
        for ext, count in self.core.ext_counts().items():
            groups[file_group(ext)] = groups.get(file_group(ext), 0) + count
        types = [(f"type:{name}", t(name), glyph, groups[name]) for name, glyph in FILE_TYPES if name in groups]
        folders = self.folder_rows()
        collections = [(f"collection:{row['id']}", row["name"], "book", row["count"]) for row in self.core.collections()]
        self.fill_nav(self.collection_nav, collections)
        self.fill_nav(self.type_nav, types)
        self.fill_nav(self.folder_nav, folders)
        self.type_heading.setVisible(bool(types))
        self.folder_heading.setVisible(bool(folders))
        if self.place not in self.nav_buttons and not self.scanning:
            self.place = "all"
        self.used_total = stats["used"]
        counts = {"all": stats["total"], "used": stats["used"], "recent": self.core.count_since(week_ago), "favorites": stats["favorites"]}
        counts.update({place: count for place, _, _, count in collections + types + folders})
        for place, b in self.nav_buttons.items():
            b.set_count(counts.get(place, 0))
            b.setChecked(place == self.place)
        self.sources_button.set_count(stats["sources"] or None)
        has_sources = bool(stats["sources"])
        self.filter_row.setVisible(has_sources)
        self.scan_button.setVisible(has_sources)

    def folder_rows(self):
        """The top folders, or once inside one, its contents and the way back."""
        if self.place.startswith("folder:"):
            source, folder = self.place.split(":", 2)[1:]
            match = next((row for row in self.core.sources() if str(row["id"]) == source), None)
            count = self.core.search_page(source_id=int(source), folder=folder or None, limit=1)["total"] if match else 0
            if count:
                parent = os.path.dirname(folder)
                title = os.path.basename(folder) or Path(match["path"]).name or match["path"]
                up = f"up:folder:{source}:{parent}" if parent else "up:all"
                inside = self.core.subfolders(int(source), folder)
                return [(up, t("Up one level"), "back", None), (self.place, title, "folder", count)] + [
                    (f"folder:{row['source_id']}:{row['folder']}", row["name"], "folder", row["count"]) for row in inside]
        return [(f"folder:{row['source_id']}:{row['folder']}", row["name"], "folder", row["count"]) for row in self.core.folders()]

    def fill_nav(self, layout, rows):
        """Types and folders come and go with the files, so these rows are rebuilt to match."""
        wanted = [row[:2] for row in rows]
        current = [(widget.place, widget.title) for widget in (layout.itemAt(index).widget() for index in range(layout.count()))]
        if wanted == current:
            return
        for place, _ in current:
            del self.nav_buttons[place]
        clear_layout(layout)
        for place, title, glyph, _ in rows:
            self.add_nav(layout, place, title, glyph)
            row = self.nav_buttons[place]
            row.place = place
            if place.startswith("collection:"):
                row.setContextMenuPolicy(Qt.CustomContextMenu)
                row.customContextMenuRequested.connect(lambda position, p=place, b=row: popup(self.collection_menu(int(p[11:]), b.title), b.mapToGlobal(position)))

    def select_place(self, place):
        self.save_tags()
        self.place = place.removeprefix("up:")
        self.refresh()

    def ask_name(self, title, name=""):
        text, accepted = QInputDialog.getText(self, title, t("Collection name"), text=name)
        return text if accepted else None

    def new_collection(self, file_ids=()):
        name = self.ask_name(t("New collection"))
        if name is None:
            return None
        try:
            collection = self.core.create_collection(name)
        except ValueError as exc:
            QMessageBox.information(self, t("New collection"), t(str(exc)))
            return None
        self.core.collect(collection, file_ids)
        if file_ids:
            self.refresh_stats()
            self.show_status(t("Added {files} to {name}", files=file_count(len(file_ids)), name=name.strip()), 5)
        else:
            self.select_place(f"collection:{collection}")
        return collection

    def collect_files(self, collection, file_ids, name):
        self.core.collect(collection, file_ids)
        self.refresh_stats()
        self.show_status(t("Added {files} to {name}", files=file_count(len(file_ids)), name=name), 5)

    def collection_choices(self, menu, file_ids, members=()):
        """One entry per collection, then a way to start a new one."""
        for collection in self.core.collections():
            if collection["id"] in members:
                choice = menu.addAction(literal(collection["name"]), lambda c=collection: self.leave_collection(c["id"], file_ids[0]))
                choice.setCheckable(True)
                choice.setChecked(True)
            else:
                menu.addAction(literal(collection["name"]), lambda c=collection: self.collect_files(c["id"], file_ids, c["name"]))
        if self.core.collections():
            menu.addSeparator()
        menu.addAction(t("New collection"), lambda: self.new_collection(file_ids))

    def leave_collection(self, collection, file_id):
        self.core.uncollect(collection, file_id)
        self.refresh()

    def show_collect_menu(self):
        if self.selected_id:
            menu = QMenu(self)
            self.collection_choices(menu, [self.selected_id], self.core.collections_of(self.selected_id))
            popup(menu, QCursor.pos())

    def collect_results(self):
        rows = self.core.search_page(**self.search_args, limit=MAX_COLLECTED_AT_ONCE)["rows"]
        menu = QMenu(self)
        self.collection_choices(menu, [row["id"] for row in rows])
        popup(menu, QCursor.pos())

    def collection_menu(self, collection, name):
        menu = QMenu(self)
        menu.addAction(t("Rename"), lambda: self.rename_collection(collection, name))
        menu.addAction(t("Export list"), lambda: self.export_collection(collection, name))
        menu.addSeparator()
        menu.addAction(t("Delete collection"), lambda: self.delete_collection(collection, name))
        return menu

    def rename_collection(self, collection, name):
        wanted = self.ask_name(t("Rename"), name)
        if wanted is None:
            return
        try:
            self.core.rename_collection(collection, wanted)
        except ValueError as exc:
            QMessageBox.information(self, t("Rename"), t(str(exc)))
            return
        self.refresh()

    def export_collection(self, collection, name):
        path, _ = QFileDialog.getSaveFileName(self, t("Export list"), f"{name}.csv", "CSV (*.csv)")
        if path:
            try:
                self.show_status(t("Exported {files}", files=file_count(self.core.export_collection(collection, path))), 5)
            except OSError as exc:  # Most often the list is open in Excel.
                QMessageBox.warning(self, t("Could not export"), as_plain(exc))

    def delete_collection(self, collection, name):
        asked = t('Delete "{name}"? Your files stay where they are.', name=name)
        if QMessageBox.question(self, t("Delete collection"), as_plain(asked)) == QMessageBox.Yes:
            self.core.delete_collection(collection)
            self.refresh()

    def next_tip(self):
        """One suggestion at a time, each offered once, when it has just become useful."""
        if not self.config.get("words_tip_done") and ocr.available():
            word = example_word(self.core.ocr_texts())
            if word and self.core.search_page(word, limit=1)["total"]:
                return "words", t('Words in your photos can be searched now. Try "{word}".', word=word), t("Try it"), word
        if self.hotkey and self.hotkey.active and AUTOSTART_AVAILABLE and not self.demo and self.used_total \
                and not self.config.get("autostart_asked") and not autostart_enabled():
            key = key_label(self.config.get("hotkey", DEFAULT_HOTKEY))
            return "autostart", t("Start when you sign in, so {key} is always ready?", key=key), t("Start when I sign in"), ""
        return None

    def refresh_tip(self):
        tip = self.next_tip()
        self.tip.setVisible(tip is not None)
        if tip:
            self.tip_id, text, action, self.tip_word = tip
            self.tip_text.setText(text)
            self.tip_action.setText(action)

    def accept_tip(self):
        tip, word = self.tip_id, self.tip_word
        self.dismiss_tip()
        if tip == "words":
            self.search.setText(word)
        elif tip == "autostart":
            set_autostart(True, autostart_command(BASE / "desktop_app.py"))

    def dismiss_tip(self):
        self.save_setting("words_tip_done" if self.tip_id == "words" else "autostart_asked", True)
        self.refresh_tip()

    def load_page(self, offset):
        return load_results(self.core, self.search_args, offset)

    def matches(self, **changes):
        return self.core.search_page(**{**self.search_args, **changes}, limit=1)["total"]

    def has_filters(self):
        return bool(self.period.currentIndex() or self.place != "all")

    def start_with_folders(self):
        chosen = [box.property("path") for box in self.starter_boxes if box.isChecked()]
        if not chosen:
            self.add_source()
            return
        problems = []
        for path in chosen:
            try:
                self.core.add_source(path)
            except (ValueError, OSError, RuntimeError) as exc:
                problems.append(t(str(exc)))
        if len(problems) == len(chosen):
            QMessageBox.warning(self, t("Could not add folder"), as_plain(problems[0]))
            return
        self.refresh_stats()
        self.start_scan()

    def show_starter(self, folders):
        wanted = [path for _, path in folders]
        if wanted != [box.property("path") for box in self.starter_boxes]:
            clear_layout(self.starter_list)
            self.starter_boxes = []
            for title, path in folders:
                box = QCheckBox(literal(f"{t(title)}   {path}"))
                box.setProperty("path", path)
                box.setChecked(True)
                self.starter_list.addWidget(box)
                self.starter_boxes.append(box)
        self.starter.setVisible(bool(folders))
        self.empty_other.setVisible(bool(folders))

    def place_filters(self, parsed):
        """What the place chosen in the sidebar means as search arguments."""
        place = self.place
        period = "Last 7 days" if place == "recent" else self.period.currentData()
        kind = place[5:] if place.startswith("type:") else "All"
        filters = resolve_filters(parsed, kind, period)
        if kind == "Other":
            other = {ext for ext in self.core.ext_counts() if file_group(ext) == "Other"}
            filters["exts"] = sorted(other & set(filters["exts"]) if filters["exts"] is not None else other)
        if place.startswith("folder:"):
            source, folder = place.split(":", 2)[1:]
            filters.update(source_id=int(source), folder=folder or None)
        if place.startswith("collection:"):
            filters["collection"] = int(place[11:])
        return {"view": place if place in ("favorites", "used") else "all", **filters}

    def place_title(self):
        place = self.place
        if place == "all" and self.view_mode == "fan" and self.used_total:
            return t("Pick up where you left off")
        titles = {"all": "Recent files", "used": "Recently used", "recent": "Last 7 days", "favorites": "Favorites"}
        if place in titles:
            return t(titles[place])
        return t(place[5:]) if place.startswith("type:") else self.nav_buttons[place].text() if place in self.nav_buttons else ""

    def home_rows(self):
        """What the deck opens on: recent use in the middle, new arrivals around it."""
        used = load_results(self.core, {**self.search_args, "view": "used", "sort": "used", "relevance": False}, limit=HOME_USED)["rows"]
        known = {row["id"] for row in used}
        fresh = [row for row in self.rows if row["id"] not in known]
        return center_out((used + fresh)[:HOME_FILES])

    def mark_used(self, file_id):
        self.core.mark_used(file_id)
        self.home_stale = True

    def load_more(self, offset):
        return self.load_page(offset)["rows"]

    def refresh_results(self, *args):
        query = self.search.text().strip()
        if self.place == "links":
            if not query:
                self.filter_row.hide()
                self.show_map()
                return
            self.place = "all"  # Searching means looking through everything.
            self.refresh_stats()
        self.filter_row.show()
        parsed = parse_query(query)
        sort = self.sort.currentData()
        previous = self.search_args
        self.search_args = {
            "query": parsed["text"], "sort": "used" if self.place == "used" and sort == "recent" else sort,
            "relevance": sort == "recent", **self.place_filters(parsed),
        }
        page = self.load_page(0)
        self.replaced_query = related_search(self.core, self.search_args, parsed["text"]) if parsed["text"] and not page["total"] else None
        if self.replaced_query:
            self.search_args["query"] = self.replaced_query
            page = self.load_page(0)
        self.rows = page["rows"]
        total = page["total"]
        self.model.replace(self.rows, total)
        self.home_stale = False
        at_home = self.place == "all" and not query and not self.has_filters() and sort == "recent"
        visible = self.home_rows() if at_home else self.rows[:HOME_FILES]
        self.deck.set_files(visible)
        focus_row = next((r for r in visible if r["id"] == self.selected_id), None)
        if focus_row is None and visible:
            focus_row = visible[len(visible) // 2]
        if focus_row:
            self.deck.set_selected(focus_row["id"])
        self.update_deck_caption(focus_row)
        self.section_title.setText(t("Results") if query else self.place_title())
        self.result_count.setText(file_count(total))
        first_run = not self.rows and (not self.core.sources() or (self.scanning and not self.scan_automatic and not query))
        self.section_title.setVisible(not first_run)
        self.result_count.setVisible(not first_run)
        self.update_chips(query, parsed)
        active = self.period.currentIndex() != 0
        if self.period.property("active") != active:
            self.period.setProperty("active", active)
            repolish(self.period)
        # The empty state has its own button for this.
        self.clear_filters.setVisible(bool(self.rows) and bool(query or self.has_filters()))
        self.collect_all.setVisible(bool(self.rows) and bool(query or self.has_filters()) and not self.place.startswith("collection:"))
        if not self.rows:
            self.show_empty(query, parsed)
            self.pages.setCurrentWidget(self.empty)
        else:
            self.pages.setCurrentIndex({"fan": 0, "grid": 1, "list": 2}[self.view_mode])
        if self.selected_id and self.selected_id not in {r["id"] for r in self.rows} and (len(self.rows) >= total or self.search_args != previous):
            self.close_details()

    def show_empty(self, query, parsed):
        """Says what actually happened, and offers the one step most likely to help."""
        has_sources = bool(self.core.sources())
        reading = self.scanning and not self.scan_automatic
        filtered = self.has_filters()
        clear_layout(self.suggestions)
        folders = suggested_folders() if not has_sources else []
        self.show_starter(folders)
        if not has_sources:
            title, hint = t("Start with a folder"), t("Files stay where they are. You can also keep a copy, and change that at any time.")
            action = t("Start with these folders") if folders else t("Add folder")
        elif self.place.startswith("collection:") and not query:
            title, hint, action = t("This collection is empty"), t("Right-click a file to add it, or search and add all the results."), None
        elif query or filtered:
            title = t('Nothing found for "{query}"', query=query) if query else t("Nothing found")
            without = self.matches(view="all", source_id=None, folder=None, collection=None, **resolve_filters(parsed)) if filtered else 0
            if reading:
                hint = t("Still reading your files, so more may appear.")
            elif without:
                hint = t("Without these filters: {files}", files=file_count(without))
            elif filtered:
                hint = t("Try a shorter keyword, or look in all files.")
            else:
                hint = t("Photos are found by the words in them, their name or date, not by what they show.") if ocr.available() else t("Photos and scanned pages are found by name or date, not by what is in them.")
            action = t("Clear filters") if filtered else t("Clear search")
            words = parsed["text"].split()
            for word, count in ((word, self.matches(query=word)) for word in (words if len(words) > 1 else ())):
                if count:
                    self.suggestions.addWidget(button(f"{word}  ·  {file_count(count)}", lambda checked=False, w=word: self.search.setText(w), None, "recentQuery"))
        elif reading:
            title, hint, action = t("Reading your files"), t("The first ones appear here in a moment. You can start searching now."), None
        else:
            title, hint, action = t("No files here yet"), t("Add another folder, or press Update after adding files."), t("Add folder")
        self.suggestions.insertStretch(0)
        self.suggestions.addStretch()
        self.suggestion_row.setVisible(self.suggestions.count() > 2)
        self.ghost_deck.setVisible(not folders and (not has_sources or (reading and not query)))
        self.empty_title.setText(title)
        self.empty_hint.setText(hint)
        self.empty_adds_folder = action == t("Add folder")
        self.empty_action.setVisible(action is not None)
        self.empty_action.setText(action or "")
        self.empty_action.setObjectName("primary" if self.empty_adds_folder or folders else "button")
        self.empty_action.setIcon(icon("plus", ON_ACCENT, 16) if self.empty_adds_folder else QIcon())
        repolish(self.empty_action)

    def update_chips(self, query, parsed):
        clear_layout(self.query_chips)
        chips = parsed.get("labels", [])
        history = self.config.get("queries", [])
        if self.replaced_query:
            self.query_chips.addWidget(label(t('No results for "{query}". Showing "{other}" instead.', query=parsed["text"], other=self.replaced_query), "caption"))
        if chips:
            for text in chips:
                self.query_chips.addWidget(label(condition_label(text), "chip"))
        elif not query and history:
            self.query_chips.addWidget(label(t("Recent searches"), "caption"))
            for text in history[:3]:
                self.query_chips.addWidget(button(text, lambda checked=False, q=text: self.search.setText(q), None, "recentQuery"))
        self.query_chips.addStretch()
        self.chip_row.setVisible(bool(chips or self.replaced_query) or (not query and bool(history)))

    def set_view(self, mode):
        self.view_mode = mode
        self.view_before_search = None
        self.view_buttons[mode].setChecked(True)
        self.refresh_results()

    def toggle_deck_layout(self):
        line = self.deck_mode.text() == t("Spread out")
        self.deck.set_layout_mode("line" if line else "fan")
        self.deck_mode.setText(t("Fan out") if line else t("Spread out"))

    def set_reduced_motion(self, checked):
        self.reduced_motion = checked
        self.deck.set_reduced_motion(checked)
        self.map.set_reduced_motion(checked)
        if self.assistant:
            self.assistant.reduced_motion = checked
        self.config["reduced_motion"] = checked
        self.save_config()

    def reset_filters(self):
        self.place = "all"
        self.search.clear()
        self.period.setCurrentIndex(0)
        self.refresh()

    def empty_clicked(self):
        if self.starter_boxes and self.starter.isVisibleTo(self.empty):
            self.start_with_folders()
        elif self.empty_adds_folder:
            self.add_source()
        elif self.has_filters() and self.search.text().strip():
            self.place = "all"
            self.period.setCurrentIndex(0)
            self.refresh()
        else:
            self.reset_filters()

    def select_file(self, file_id):
        if file_id != self.selected_id:
            self.save_tags()
        row = self.core.get_file(file_id)
        if not row:
            return
        self.selected_id = file_id
        self.deck.set_selected(file_id)
        self.update_deck_caption(row)
        self.tags_file_id = None
        self.detail_title.setText(row["name"])
        self.detail_meta.setText(f"{kind_text(row)}   {size_text(row['size'])}   {date_text(row['mtime'])}")
        self.favorite_button.setText(t("Favorited") if row.get("favorite") else t("Favorite"))
        displayed = next((r for r in self.rows if r["id"] == file_id), row)
        source = displayed.get("match_source") if displayed.get("match_terms") else None
        self.match_label.setText(t("Found in {place}", place=t(MATCH_LABELS[source])) if source in MATCH_LABELS else "")
        self.match_label.setVisible(bool(self.match_label.text()))
        path = readable_path(row)
        self.update_availability(row, path)
        self.show_versions(row)
        self.show_linked(row)
        self.put_back_button.setVisible(bool(self.core.moved_from(file_id)))
        pix = preview_pixmap(row, 340, 300) if path and (row.get("kind") == "Image" or row.get("ext") == ".pdf") else None
        self.preview_image.setVisible(pix is not None)
        if pix is not None:
            self.preview_image.setPixmap(pix)
        body = row.get("text") or t(row.get("summary") or "")
        self.preview_text.setPlainText(body or t("No text preview for this format. Open the file, or find it again by name and tags."))
        table = read_table(path, row.get("ext")) if path and row.get("ext") in TABLE_EXTS else []
        if table:
            fill_table(self.preview_table, table, displayed.get("match_terms", []))
        self.preview_table.setVisible(bool(table))
        self.preview_text.setVisible(not table and (bool(body) or pix is None))
        self.mark_preview(displayed.get("match_terms", []))
        self.tags_edit.setText(", ".join(row.get("tags", [])))
        self.tags_file_id = file_id
        path_text = row.get("path", "")
        self.path_label.setText(self.path_label.fontMetrics().elidedText(path_text, Qt.ElideMiddle, 320))
        self.path_label.setToolTip(as_plain(path_text))
        self.details.show()
        if self.splitter.sizes()[1] < 280:
            self.splitter.setSizes([max(400, self.splitter.width() - 345), 345])
        self.show_status("")
        if self.place == "links":
            self.point_map_at(file_id)

    def show_versions(self, row):
        family = self.core.versions(row.get("version_key"))
        clear_layout(self.versions)
        others = [item for item in family if item["id"] != row["id"]]
        self.version_panel.setVisible(bool(others))
        if not others:
            return
        self.versions.addWidget(label(t("Other versions"), "caption"))
        for item in others[:MAX_VERSIONS_SHOWN]:
            tag = "  ·  " + t("Latest") if item["id"] == family[0]["id"] else ""
            name = self.path_label.fontMetrics().elidedText(item["name"], Qt.ElideMiddle, 200)
            choice = button(f"{name}  {date_text(item['mtime'])}{tag}", lambda checked=False, i=item["id"]: self.select_file(i), None, "quiet")
            choice.setStyleSheet("text-align:left;")
            self.versions.addWidget(choice)

    def update_availability(self, row, path):
        message = ""
        if row.get("missing"):
            message = t("Not at its original location")
            if path and str(path) != row.get("path"):
                message += "  ·  " + t("A saved copy is available")
        elif path is None:
            message = t("File unavailable")
        self.availability.setText(message)
        self.availability.setVisible(bool(message))

    def mark_preview(self, terms):
        """Highlight every match, and scroll to the first."""
        document = self.preview_text.document()
        marker = QTextCharFormat()
        marker.setBackground(QColor("#2F4436"))
        marker.setForeground(QColor(ACCENT))
        selections = []
        first = None
        for term in terms:
            cursor = QTextCursor(document)
            while len(selections) < 300:
                cursor = document.find(term, cursor)
                if cursor.isNull():
                    break
                selection = QTextEdit.ExtraSelection()
                selection.cursor = cursor
                selection.format = marker
                selections.append(selection)
                if first is None or cursor.position() < first.position():
                    first = QTextCursor(cursor)
        self.preview_text.setExtraSelections(selections)
        if first is not None:
            first.clearSelection()
            self.preview_text.setTextCursor(first)
            self.preview_text.ensureCursorVisible()

    def update_deck_caption(self, row):
        self.deck_focus_label.setText(row["name"] if row else "")
        if row and row.get("last_used"):
            self.deck_focus_meta.setText(f"{kind_text(row)}   {t('Used {date}', date=date_text(row['last_used']))}")
        else:
            self.deck_focus_meta.setText(f"{kind_text(row)}   {date_text(row.get('mtime'))}" if row else "")

    def close_details(self):
        self.save_tags()
        self.details.hide()
        self.selected_id = None
        self.tags_file_id = None

    def show_status(self, text, seconds=0):
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.status_timer.stop()
        if seconds:
            self.status_timer.start(seconds * 1000)

    def save_tags(self):
        if getattr(self, "tags_file_id", None):
            row = self.core.get_file(self.tags_file_id)
            if row:
                tags = [x.strip() for x in self.tags_edit.text().replace("，", ",").split(",") if x.strip()]
                if tags != row.get("tags", []):
                    self.core.set_tags(self.tags_file_id, tags)
                    self.show_status(t("Tags saved"))

    def edit_metadata(self):
        if not self.selected_id:
            return
        file_id = self.selected_id
        row = self.core.get_file(file_id)
        dialog = QDialog(self)
        dialog.setWindowTitle(t("Tags"))
        dialog.setMinimumWidth(440)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 22, 22, 18)
        layout.setSpacing(10)
        title = label(row["name"], "detailTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(label(t("Tags stay with the file when it changes, and can be searched."), "hint"))
        layout.addSpacing(4)
        layout.addWidget(label(t("Tags, separated by commas"), "caption"))
        tags = QLineEdit(", ".join(row.get("tags", [])))
        tags.setPlaceholderText(t("Projects, topics, or your own clues"))
        layout.addWidget(tags)
        actions = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        actions.button(QDialogButtonBox.Save).setText(t("Save"))
        actions.button(QDialogButtonBox.Cancel).setText(t("Cancel"))
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        accepted = dialog.exec() == QDialog.Accepted
        text = tags.text()
        dialog.deleteLater()
        if accepted:
            self.core.set_tags(file_id, text)
            self.tags_file_id = None
            self.select_file(file_id)
            self.refresh_stats()
            self.refresh_results()
            self.show_status(t("Tags saved"))

    def toggle_favorite(self):
        if self.selected_id:
            row = self.core.get_file(self.selected_id)
            self.core.set_favorite(self.selected_id, not row["favorite"])
            self.select_file(self.selected_id)
            self.refresh_stats()
            self.refresh_in_place()

    def open_preview(self):
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QTextEdit)) or not self.selected_id:
            return
        row = self.core.get_file(self.selected_id)
        dialog = QDialog(self)
        dialog.setWindowTitle(row["name"])
        dialog.resize(840, 760)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.addWidget(label(row["name"], "detailTitle"))
        pages = self.preview_pages(row)
        sheet = read_table(readable_path(row), row.get("ext")) if not pages and readable_path(row) and row.get("ext") in TABLE_EXTS else []
        if sheet:
            pages = table_widget(sheet)
        if pages:
            layout.addWidget(pages, 3)
        if len(sheet) >= MAX_PREVIEW_ROWS:
            layout.addWidget(label(t("Showing the first {rows} rows", rows=MAX_PREVIEW_ROWS), "caption"))
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(row.get("text") or t(row.get("summary") or "") or t("No text preview for this format. Open the file, or find it again by name and tags."))
        text.setVisible(not sheet and (not pages or bool(row.get("text"))))
        layout.addWidget(text, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button(t("Open"), lambda: self.open_file(row["id"]), "external", "primary"))
        actions.addWidget(button(t("Close preview"), dialog.accept))
        layout.addLayout(actions)
        dialog.exec()
        dialog.deleteLater()  # It holds rendered pages.

    def preview_pages(self, row):
        """The file as it looks: PDF pages to scroll through, or the photo."""
        path = readable_path(row)
        if not path or not (row.get("kind") == "Image" or row.get("ext") == ".pdf"):
            return None
        holder = QWidget()
        holder.setObjectName("content")
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        ratio = self.devicePixelRatioF()
        if row.get("ext") == ".pdf":
            total = page_count(path)
            for index in range(min(total, PREVIEW_PAGES)):
                image = render_page(path, index, round(760 * ratio))
                if not image.isNull():
                    image.setDevicePixelRatio(ratio)
                    page = QLabel()
                    page.setPixmap(QPixmap.fromImage(image))
                    column.addWidget(page, 0, Qt.AlignHCenter)
            if total > PREVIEW_PAGES:
                column.addWidget(label(t("Showing the first {shown} of {total} pages", shown=PREVIEW_PAGES, total=total), "caption"), 0, Qt.AlignHCenter)
        else:
            photo = preview_pixmap(row, 780, 620)
            if photo:
                page = QLabel()
                page.setPixmap(photo)
                column.addWidget(page, 0, Qt.AlignHCenter)
        if not column.count():
            return None
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(holder)
        return scroll

    def open_file(self, file_id=None):
        file_id = file_id if isinstance(file_id, int) and not isinstance(file_id, bool) else self.selected_id
        row = self.core.get_file(file_id) if file_id else None
        if not row:
            return
        path = readable_path(row)
        if not path:
            QMessageBox.information(self, t("File unavailable"), t("Connect the disk that holds this file. Cloud placeholders are not downloaded automatically. A saved copy is used when there is one."))
            return
        if path.suffix.lower() in LAUNCHING_EXTS:
            self.reveal_file(row["id"])
            self.show_status(t("Scripts and apps are shown in their folder instead of being run."))
            return
        self.mark_used(row["id"])
        try:
            open_with_default(path)
        except (OSError, subprocess.SubprocessError) as exc:
            QMessageBox.warning(self, t("Could not open"), as_plain(exc))

    def reveal_file(self, file_id=None):
        file_id = file_id if isinstance(file_id, int) and not isinstance(file_id, bool) else self.selected_id
        row = self.core.get_file(file_id) if file_id else None
        path = readable_path(row) if row else None
        if path:
            self.mark_used(row["id"])
            show_in_folder(path)
        else:
            QMessageBox.information(self, t("Location unavailable"), t("Connect the disk that holds this file."))

    def list_menu(self, view, position):
        index = view.indexAt(position)
        row = index.data(Qt.UserRole)
        if not row:
            return
        picked = [item.data(Qt.UserRole) for item in view.selectionModel().selectedIndexes()]
        where = view.viewport().mapToGlobal(position)
        if len(picked) > 1 and view.selectionModel().isSelected(index):
            popup(self.many_menu(picked), where)
        else:
            self.show_file_menu(row["id"], where)

    def many_menu(self, rows):
        ids = [row["id"] for row in rows]
        menu = QMenu(self)
        menu.addAction(t("Link these {files}", files=file_count(len(ids))), lambda: self.tie(ids))
        menu.addAction(t("Copy these {files}", files=file_count(len(ids))), lambda: self.copy_rows(rows))
        self.collection_choices(menu.addMenu(t("Add to collection")), ids, set())
        return menu

    def file_menu(self, file_id):
        self.select_file(file_id)
        row = self.core.get_file(file_id)
        menu = QMenu(self)
        menu.addAction(t("Open"), lambda: self.open_file(file_id))
        menu.addAction(t("Copy file"), lambda: self.copy_file(file_id))
        if row.get("text"):
            menu.addAction(t("Copy text"), lambda: self.copy_text(file_id))
        menu.addAction(t("Show in folder"), lambda: self.reveal_file(file_id))
        menu.addAction(t("Copy path"), self.copy_path)
        menu.addSeparator()
        menu.addAction(t("Favorited") if row.get("favorite") else t("Favorite"), self.toggle_favorite)
        menu.addAction(t("Tags"), self.edit_metadata)
        self.collection_choices(menu.addMenu(t("Add to collection")), [file_id], self.core.collections_of(file_id))
        if self.place.startswith("collection:"):
            menu.addAction(t("Remove from this collection"), lambda: self.leave_collection(int(self.place[11:]), file_id))
        menu.addSeparator()
        menu.addAction(t("Link a file"), lambda: self.link_files(file_id))
        if self.core.links_of(file_id):
            menu.addAction(t("Remove all its links"), lambda: self.untie_all(file_id))
        if self.core.moved_from(file_id):
            menu.addAction(t("Put back"), lambda: self.put_back(file_id))
        return menu

    def put_back(self, file_id=None):
        file_id = file_id if isinstance(file_id, int) and not isinstance(file_id, bool) else self.selected_id
        try:
            self.core.move_back(file_id)
        except (ValueError, OSError) as exc:
            QMessageBox.information(self, t("Put back"), t(str(exc)))
            return
        self.refresh()
        self.select_file(file_id)
        self.show_status(t("Back in its original folder"), 5)

    def show_file_menu(self, file_id, position):
        popup(self.file_menu(file_id), position)

    def copy_file(self, file_id=None):
        file_id = file_id if isinstance(file_id, int) and not isinstance(file_id, bool) else self.selected_id
        row = self.core.get_file(file_id) if file_id else None
        if not row:
            return False
        mime = file_mime([row])
        if not mime:
            self.show_status(t("File unavailable"))
            return False
        QApplication.clipboard().setMimeData(mime)
        self.mark_used(row["id"])
        self.show_status(t("File copied. Paste it into a chat, an email or a folder."), 5)
        return True

    def copy_text(self, file_id=None):
        """Just the words: a quote from a PDF, or what a screenshot said."""
        row = self.core.get_file(file_id or self.selected_id) if file_id or self.selected_id else None
        if not row or not row.get("text"):
            return False
        QApplication.clipboard().setText(row["text"])
        self.mark_used(row["id"])
        self.show_status(t("Text copied"), 5)
        return True

    def confirm_copied(self, file_id):
        row = self.core.get_file(file_id)
        if self.island and row and not user_is_busy():
            self.island.present(row["name"], t("Copied. Ready to paste."), seconds=2)

    def announce_arrivals(self, file_ids):
        """Only when it is news: not to someone already here, or in a game or presentation."""
        if not self.island or not file_ids or not self.config.get("island", True) or self.isActiveWindow() or user_is_busy():
            return
        rows = [row for row in map(self.core.get_file, file_ids[-50:]) if row and not is_noise(row["relative_path"])]
        if rows:
            count = len(rows) + max(0, len(file_ids) - 50)
            detail = t("{count} new files", count=count) if count > 1 else t("New file")
            self.island.present(rows[-1]["name"], detail, rows[-1]["id"])

    def drag_file(self, file_id):
        row = self.core.get_file(file_id)
        mime = file_mime([row]) if row else None
        if mime:
            self.mark_used(file_id)
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.CopyAction)

    def copy_path(self):
        row = self.core.get_file(self.selected_id) if self.selected_id else None
        if row:
            QApplication.clipboard().setText(row.get("path", ""))
            self.show_status(t("Path copied"))

    def add_source(self):
        if self.scanning and not self.scan_automatic:
            self.show_status(t("You can add a folder once the update finishes."))
            return
        path = QFileDialog.getExistingDirectory(self, t("Choose a folder"))
        if not path:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(t("Add a folder"))
        dialog.setMinimumWidth(520)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(10)
        layout.addWidget(label(t("How should these files be archived?"), "detailTitle"))
        path_label = label(path, "caption")
        path_label.setWordWrap(True)
        layout.addWidget(path_label)
        layout.addSpacing(6)
        index = QRadioButton(t("Index in place"))
        index.setChecked(True)
        copy = QRadioButton(t("Keep a copy"))
        layout.addWidget(index)
        layout.addWidget(label(t("Keep files where they are and make them searchable. Nothing is copied."), "hint"))
        layout.addWidget(copy)
        layout.addWidget(label(t("Also copy files into the archive. Originals stay put. This uses extra disk space."), "hint"))
        layout.addSpacing(4)
        layout.addWidget(label(t("You can change this later in Sources."), "caption"))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(t("Add and index"))
        buttons.button(QDialogButtonBox.Cancel).setText(t("Cancel"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        accepted = dialog.exec() == QDialog.Accepted
        mode = "copy" if copy.isChecked() else "index"
        dialog.deleteLater()
        if accepted:
            try:
                self.core.add_source(path, mode=mode)
            except (ValueError, OSError, RuntimeError) as exc:
                QMessageBox.warning(self, t("Could not add folder"), t(str(exc)))
                return
            self.refresh_stats()
            self.start_scan()

    def source_status(self, source):
        if source["status"] == "unavailable":
            return t("Unavailable. Existing records are kept.")
        if source.get("last_error"):
            return t(source["last_error"])
        return t("Updated {when}", when=date_text(source["last_scan"])) if source.get("last_scan") else t("Not updated yet")

    def show_sources(self):
        if self.scanning and not self.scan_automatic:
            self.show_status(t("You can manage sources once the update finishes. Use Stop update to stop it safely."))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(t("Sources"))
        dialog.resize(760, 480)
        self.source_dialog_open = True
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(12)
        layout.addWidget(label(t("Sources"), "title"))
        layout.addWidget(label(t("Choose how each folder is archived. Original files and saved copies are always kept."), "hint"))
        area = QScrollArea()
        area.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("holder")
        entries = QVBoxLayout(holder)
        entries.setContentsMargins(0, 0, 8, 0)
        entries.setSpacing(6)
        changed = [False]
        for source in self.core.sources():
            line = QFrame()
            line.setObjectName("sourceRow")
            line.setStyleSheet(f"QFrame#sourceRow{{border:1px solid {HAIRLINE};border-radius:8px;}}")
            row = QHBoxLayout(line)
            texts = QVBoxLayout()
            texts.setSpacing(2)
            title = label(source["path"])
            title.setWordWrap(True)
            texts.addWidget(title)
            if source["picks"]:
                texts.addWidget(label(t("Only {files} from this folder", files=file_count(len(json.loads(source["picks"])))), "caption"))
            texts.addWidget(label(self.source_status(source), "warning" if source["status"] == "unavailable" else "caption"))
            row.addLayout(texts, 1)
            mode = QComboBox()
            mode.addItems([t("Index in place"), t("Keep a copy")])
            mode.setCurrentIndex(1 if source["mode"] == "copy" else 0)
            def change_mode(value, sid=source["id"], box=mode):
                try:
                    self.core.set_source_mode(sid, "copy" if value == 1 else "index")
                    changed[0] = True
                except (ValueError, RuntimeError) as exc:
                    QMessageBox.warning(dialog, t("Could not switch"), t(str(exc)))
                    box.setCurrentIndex(1 - value)
            mode.activated.connect(change_mode)
            row.addWidget(mode)
            def remove_source(checked=False, sid=source["id"], widget=line):
                if QMessageBox.question(dialog, t("Remove source"), t("This removes the index for this source only. Original files and saved copies stay. Continue?")) == QMessageBox.Yes:
                    try:
                        self.core.remove_source(sid)
                        widget.hide()
                        changed[0] = True
                    except (ValueError, RuntimeError) as exc:
                        QMessageBox.warning(dialog, t("Could not remove"), t(str(exc)))
            row.addWidget(button(t("Remove"), remove_source, None, "quiet"))
            entries.addWidget(line)
        entries.addStretch()
        area.setWidget(holder)
        layout.addWidget(area, 1)
        location = label(t("Archive location: {path}", path=str(self.core.data_dir)), "caption")
        location.setWordWrap(True)
        layout.addWidget(location)
        end = QHBoxLayout()
        end.addStretch()
        end.addWidget(button(t("Done"), dialog.accept, None, "primary"))
        layout.addLayout(end)
        dialog.exec()
        dialog.deleteLater()
        self.source_dialog_open = False
        self.watch_sources()
        self.refresh()
        if changed[0] and self.core.sources():
            self.start_scan()

    def start_background(self, app):
        """Everything that works with the window closed. Tests and --render stay without it."""
        self.quick = QuickSearch(self)
        self.island = Island(self)
        self.start_assistant()
        self.hotkey = GlobalHotkey(self.quick.toggle)
        app.installNativeEventFilter(self.hotkey)
        key = self.config.get("hotkey", DEFAULT_HOTKEY)
        if not self.hotkey.register(key) and FolderWatcher.available:
            self.show_status(t("{key} is used by another program. Choose another shortcut in Settings.", key=key_label(key)))
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(app_icon(), self)
            self.tray.setToolTip(app_name())
            self.tray.activated.connect(self.tray_activated)
            self.build_tray_menu()
            self.tray.show()
            app.setQuitOnLastWindowClosed(False)
        app.commitDataRequest.connect(self.session_ending)
        app.aboutToQuit.connect(self.stop_background)
        self.folders = FolderWatcher(self.folder_changed)
        self.watch_sources()
        if FolderWatcher.available:
            self.watch.start(SAFETY_SCAN_MS)
        self.start_reading()
        # Catches what changed while the app was not running.
        QTimer.singleShot(10000, self.auto_scan)

    def start_reading(self):
        """Reads the words in photos and scans, one file at a time, behind everything else."""
        if not ocr.available() or (self.ocr_thread and self.ocr_thread.is_alive()):
            return
        self.ocr_thread = threading.Thread(target=self.read_waiting_files, args=(self.core,), daemon=True, name="ocr")
        self.ocr_thread.start()

    def read_waiting_files(self, core):
        last_report = 0.0
        skipped = set()  # Unreadable for now, on a disk that is unplugged say. They stay in the queue for next time.
        while not self.ocr_stop.is_set():
            waiting = [row for row in core.ocr_waiting(OCR_BATCH + len(skipped)) if row["id"] not in skipped]
            if not waiting:
                break
            for row in waiting:
                if self.ocr_stop.is_set():
                    return
                if not self.read_one(core, row):
                    skipped.add(row["id"])
                if time.monotonic() - last_report >= 1:
                    last_report = time.monotonic()
                    self.report_reading(core.ocr_remaining())
                time.sleep(OCR_PAUSE_SECONDS)
        self.report_reading(0)

    @staticmethod
    def read_one(core, row):
        """False if it could not be read this time, which is not the same as having no words."""
        path = readable_path(row)
        words = ocr.read_file(path) if path else None
        if words is None:
            return False
        core.save_ocr(row["id"], words)
        return True

    def report_reading(self, remaining):
        try:
            self.signals.reading.emit(remaining)
        except RuntimeError:
            pass  # The window has gone.

    def reading_progress(self, remaining):
        if self.scanning:
            return
        if remaining:
            self.show_status(t("Reading the words in photos and scans  ·  {count} left", count=f"{remaining:,}"), 3)
        self.refresh_tip()

    def assistant_folder(self):
        return self.data_root / "assistant"

    def start_assistant(self):
        self.assistant = Assistant(self.assistant_folder(), self.config.get("assistant_size", "medium"), self.reduced_motion)
        self.assistant.clicked.connect(self.quick.toggle)
        self.assistant.dropped.connect(self.organise_drop)
        self.assistant.menu_requested.connect(lambda position: popup(self.assistant_menu(), position))
        self.assistant.moved.connect(lambda position: self.save_setting("assistant_at", [position.x(), position.y()]))
        self.assistant.plays = self.config.get("assistant_play", True)
        self.assistant.pinned = self.config.get("assistant_pinned", False)
        self.assistant_watch = QTimer(self)
        self.assistant_watch.timeout.connect(self.place_assistant)
        self.assistant_watch.start(3000)
        self.place_assistant()

    def place_assistant(self):
        """On show unless it was turned off, and out of the way during games and presentations."""
        if not self.assistant:
            return
        if not self.config.get("assistant", True) or user_is_busy():
            self.assistant.hide()
            self.assistant.bubble.hide()
        elif not self.assistant.isVisible():
            spot = self.config.get("assistant_at")
            area = QGuiApplication.primaryScreen().availableGeometry()
            start = QPoint(*spot) if spot else QPoint(area.right() - self.assistant.width() - 24, area.bottom() - self.assistant.height() - 24)
            if not any(screen.availableGeometry().contains(start) for screen in QGuiApplication.screens()):
                start = QPoint(area.right() - self.assistant.width() - 24, area.bottom() - self.assistant.height() - 24)
            self.assistant.move(start)
            self.assistant.show()
            self.greet()

    def greet(self):
        """She bows each time she arrives, and introduces herself the first time."""
        if self.assistant_greeted:
            return
        self.assistant_greeted = True
        self.assistant.set_mood("greeting", 3)
        if not self.config.get("greeted"):
            self.assistant.say(t("Hi! I'm {name}.", name=t("Roo")), 4)
            self.save_setting("greeted", True)

    def assistant_menu(self):
        menu = QMenu(self)
        menu.addAction(t("Search"), lambda: QTimer.singleShot(0, self.quick.toggle))
        menu.addAction(t("Open {name}", name=app_name()), self.show_main)
        menu.addSeparator()
        menu.addAction(t("Change image"), self.choose_assistant_image)
        menu.addAction(t("Use an image for each mood"), self.choose_assistant_images)
        if self.assistant.custom:
            menu.addAction(t("Back to {name}", name=t("Roo")), self.reset_assistant)
        plays = menu.addAction(t("Let {name} play on her own", name=t("Roo")), self.toggle_assistant_play)
        plays.setCheckable(True)
        plays.setChecked(self.assistant.plays)
        pinned = menu.addAction(t("Pin {name} here", name=t("Roo")), self.toggle_assistant_pinned)
        pinned.setCheckable(True)
        pinned.setChecked(self.assistant.pinned)
        sizes = menu.addMenu(t("Size on screen"))
        for name, title in (("small", "Small"), ("medium", "Medium"), ("large", "Large")):
            choice = sizes.addAction(t(title), lambda checked=False, n=name: self.resize_assistant(n))
            choice.setCheckable(True)
            choice.setChecked(self.assistant.size_name == name)
        menu.addSeparator()
        if len(self.last_sorted) > 1:
            menu.addAction(t("Link the {files} I just sorted", files=file_count(len(self.last_sorted))), lambda: self.tie(self.last_sorted))
        menu.addAction(t("Hide {name}", name=t("Roo")), lambda: self.show_assistant(False))
        return menu

    def toggle_assistant_play(self):
        self.assistant.plays = not self.assistant.plays
        self.save_setting("assistant_play", self.assistant.plays)

    def toggle_assistant_pinned(self):
        self.assistant.pinned = not self.assistant.pinned
        self.assistant.setCursor(Qt.ArrowCursor if self.assistant.pinned else Qt.OpenHandCursor)
        self.save_setting("assistant_pinned", self.assistant.pinned)

    def show_assistant(self, shown):
        self.save_setting("assistant", shown)
        self.place_assistant()

    def resize_assistant(self, name):
        self.assistant.set_size(name)
        area = self.assistant.screen().availableGeometry()
        self.assistant.move(min(self.assistant.x(), area.right() - self.assistant.width()), min(self.assistant.y(), area.bottom() - self.assistant.height()))
        self.save_setting("assistant_size", name)

    def choose_assistant_image(self):
        path, _ = QFileDialog.getOpenFileName(self, t("Change image"), "", "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp)")
        if path:
            self.set_assistant_image(path)

    def set_assistant_image(self, path):
        self.assistant.release_images()
        changed = install_image(path, self.assistant_folder())
        self.assistant.load_images()
        if changed:
            self.assistant.set_mood("happy", 2)
            self.assistant.say(t("New look!"), 3)
        else:
            self.assistant.say(t("That picture cannot be used. Try a PNG, JPG, WebP or GIF under 20 MB."))

    def choose_assistant_images(self):
        folder = QFileDialog.getExistingDirectory(self, t("Use an image for each mood"))
        if folder:
            self.set_assistant_images(folder)

    def set_assistant_images(self, folder):
        self.assistant.release_images()
        changed = install_image_set(folder, self.assistant_folder())
        self.assistant.load_images()
        if changed:
            self.assistant.set_mood("happy", 2)
            self.assistant.say(t("New look!"), 3)
        else:
            self.assistant.say(t("Name the pictures idle, excited, working and happy."))

    def reset_assistant(self):
        self.assistant.release_images()
        clear_images(self.assistant_folder())
        self.assistant.load_images()
        self.assistant.set_mood("happy", 2)

    def organise_drop(self, paths):
        """Files handed to the assistant: read them, then group them. They move only if that was chosen."""
        self.assistant.set_mood("working")
        self.assistant.say(t("Let me have a look..."), 60)
        worker = threading.Thread(target=self.sort_dropped, args=(self.core, paths), daemon=True, name="sorting")
        self.sorting.append(worker)
        worker.start()

    def sort_dropped(self, core, paths):
        moving = self.config.get("drop_action") == "move"
        try:
            only = core.adopt(paths, "index" if moving else "copy")
            while not self.ocr_stop.is_set():
                try:
                    core.scan_all(cancel=self.ocr_stop, only=only)
                    break
                except RuntimeError:  # Another scan is finishing. These can wait a moment.
                    time.sleep(.3)
            if ocr.available():
                ids = [row["id"] for row in core.files_at(paths)][:MAX_ROWS_READ_ON_DROP]
                for row in core.ocr_waiting(READ_ON_DROP, ids):
                    if self.ocr_stop.is_set():
                        break
                    self.read_one(core, row)
            rows = core.files_at(paths)
            result = self.sort_rows(core, rows)
            if moving:
                result.update(self.move_sorted(core, paths, rows, result["topics"]))
        except Exception as exc:  # Whatever it was, Roo must not stay stuck working.
            result = {"problem": str(exc)}
        try:
            self.signals.sorted.emit(result)
        except RuntimeError:
            pass  # The window has gone.

    @staticmethod
    def sort_rows(core, rows):
        """Each file goes to a collection named for its subject, or its type if that is unclear."""
        english = language() == "en"
        groups, topics = {}, {}
        for row in rows:
            topic = topic_of(row["name"], english) or topic_of(row["text"][:SORT_TEXT_CHARS], english) or t(file_group(row["ext"]))
            groups.setdefault(topic, []).append(row["id"])
            topics[row["id"]] = topic
        existing = {row["name"].casefold(): row["id"] for row in core.collections()}
        for name, ids in groups.items():
            core.collect(existing.get(name.casefold()) or core.create_collection(name), ids)
        return {"groups": {name: len(ids) for name, ids in groups.items()}, "files": len(rows), "topics": topics, "ids": [row["id"] for row in rows]}

    def library_folder(self):
        return Path(self.config.get("library_folder") or BASE / "library")

    def move_sorted(self, core, paths, rows, topics):
        """Last of all, so reading and sorting are done before anything leaves its folder.

        A loose file goes under its subject. A folder keeps its shape.
        """
        folders = [Path(path) for path in paths if Path(path).is_dir()]
        moves = []
        for row in rows:
            place = Path(row["path"])
            holder = next((folder for folder in folders if place.is_relative_to(folder)), None)
            moves.append((row["id"], Path(holder.name) / place.relative_to(holder) if holder else Path(folder_name(topics[row["id"]])) / place.name))
        return core.move_files(moves, self.library_folder(), folders)

    def finish_sorting(self, result):
        if self.sorting:
            self.sorting.pop(0)
        if not self.assistant:
            return
        self.refresh()
        self.watch_sources()
        self.start_reading()
        self.scan_new_sources()
        groups = result.get("groups")
        if not groups:
            self.assistant.set_mood("idle")
            self.assistant.say(t(result["problem"]) if result.get("problem") else t("I could not find a file to keep there."))
            return
        self.assistant.set_mood("happy", 3)
        names = "、".join(f"“{name}”" for name in list(groups)[:3]) if language() == "zh" else ", ".join(f'"{name}"' for name in list(groups)[:3])
        self.last_sorted = result.get("ids", [])
        if "moved" not in result:
            self.assistant.say(t("Sorted {files} into {names}. The originals stayed put.", files=file_count(result["files"]), names=names), 6)
        elif result["problems"]:
            self.assistant.say(t("Moved {files} into your library. {count} could not move and stayed where they were.", files=file_count(len(result["moved"])), count=len(result["problems"])), 8)
        else:
            self.assistant.say(t("Moved {files} into your library, sorted into {names}.", files=file_count(len(result["moved"])), names=names), 6)

    def stop_background(self):
        if self.assistant:
            self.assistant_watch.stop()
            self.assistant.timer.stop()
            self.assistant.close()
            self.assistant = None
        self.ocr_stop.set()
        if self.hotkey:
            self.hotkey.unregister()
        if self.folders:
            self.folders.stop()
        if self.tray:
            self.tray.hide()

    def build_tray_menu(self):
        self.tray_menu = QMenu()
        key = key_label(self.config.get("hotkey", DEFAULT_HOTKEY))
        self.tray_menu.addAction(f"{t('Search')}\t{key}", lambda: QTimer.singleShot(0, self.quick.toggle))
        self.tray_menu.addAction(t("Open {name}", name=app_name()), self.show_main)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(t("Quit"), self.quit_app)
        self.tray.setContextMenu(self.tray_menu)

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_main()

    def show_main(self, query=""):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        if query:
            self.search.setText(query)
        self.focus_search()

    def quit_app(self):
        self.quitting = True
        self.close()

    def session_ending(self, manager):
        # An ignored close here would stop Windows from signing out.
        self.quitting = True

    def watch_sources(self):
        if self.folders:
            self.folders.watch(source["path"] for source in self.core.sources() if not source["picks"])

    def folder_changed(self, folder, paths):
        try:
            self.signals.changed.emit(folder, paths)
        except RuntimeError:
            pass  # The window has gone; the notice no longer matters.

    def note_change(self, folder, paths):
        known = self.changed_paths.get(folder, set())
        self.changed_paths[folder] = None if paths is None or known is None else known | set(paths)
        self.change_timer.start(CHANGE_SETTLE_MS)

    def scan_changes(self):
        """Files often arrive in a burst, so wait for a quiet moment, then look only where they changed."""
        if self.scanning or self.sorting or self.source_dialog_open:
            self.change_timer.start(CHANGE_SETTLE_MS)
            return
        if any(paths is None or len(paths) > MAX_CHANGED_PATHS for paths in self.changed_paths.values()):
            wait = MIN_SCAN_GAP_SECONDS - (time.monotonic() - self.last_scan_end)
            if wait > 0:
                self.change_timer.start(int(wait * 1000))
                return
            self.changed_paths = {}
            self.auto_scan()
        elif self.changed_paths:
            only, self.changed_paths = self.changed_paths, {}
            self.start_scan(automatic=True, only={folder: sorted(paths) for folder, paths in only.items()})

    def show_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(t("Settings"))
        dialog.setMinimumWidth(420)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(10)
        layout.addWidget(label(t("Settings"), "title"))
        layout.addSpacing(6)
        layout.addWidget(label(t("Language"), "caption"))
        choice = QComboBox()
        for code, name in LANGUAGES.items():
            choice.addItem(name, code)
        choice.setCurrentIndex(choice.findData(language()))
        layout.addWidget(choice)
        layout.addSpacing(6)
        motion = QCheckBox(t("Reduce motion"))
        motion.setChecked(self.reduced_motion)
        motion.toggled.connect(self.set_reduced_motion)
        layout.addWidget(motion)
        layout.addSpacing(6)
        if self.hotkey:
            self.add_background_settings(layout)
        location = label(t("Archive location: {path}", path=str(self.core.data_dir)), "caption")
        location.setWordWrap(True)
        location.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(location)
        if not self.demo:
            def change():
                dialog.accept()
                QTimer.singleShot(0, self.change_archive_location)
            layout.addWidget(button(t("Change location"), change, "folder", "quiet"), 0, Qt.AlignLeft)
        end = QHBoxLayout()
        end.addStretch()
        end.addWidget(button(t("Done"), dialog.accept, None, "primary"))
        layout.addLayout(end)
        dialog.exec()
        dialog.deleteLater()
        if choice.currentData() != language():
            self.apply_language(choice.currentData())

    def change_archive_location(self):
        if self.scanning or self.sorting:
            QMessageBox.information(self, t("Change location"), t("Wait for the current update or sorting to finish, then try again."))
            return
        chosen = QFileDialog.getExistingDirectory(self, t("Choose where to keep your archive"), str(self.data_root.parent))
        if not chosen:
            return
        try:
            target = self.core.check_new_folder(archive_folder_in(_assert_local_path(chosen)))  # Before anything looks inside it.
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, t("Could not move the archive"), as_plain(t(str(exc))))
            return
        if has_archive(target):
            if QMessageBox.question(self, t("Change location"), as_plain(t("{path} already holds an archive. Switch to it? Your current archive stays as it is.", path=str(target)))) == QMessageBox.Yes:
                self.use_archive(target, t("Your archive is now in {path}.", path=str(target)))
        elif QMessageBox.question(self, t("Change location"), as_plain(t("Copy your archive to {path}? Your original files do not move.", path=str(target)))) == QMessageBox.Yes:
            self.copy_archive(target)

    def copy_archive(self, target):
        """A large archive takes a while, so it copies in the background. Scans wait, as they do for sorting."""
        self.save_tags()
        self.copy_box = QProgressDialog(t("Copying your archive..."), t("Cancel"), 0, 1000, self)
        self.copy_box.setWindowTitle(t("Change location"))
        self.copy_box.setWindowModality(Qt.ApplicationModal)
        self.copy_box.setAutoClose(False)
        self.copy_box.setAutoReset(False)
        self.copy_box.setMinimumDuration(0)
        # Held here, not only by the worker: if its end dropped the last reference, Qt would undo this connection
        # on that thread just as closing the box fires it, and the two would wait for each other for ever.
        self.copy_cancel = threading.Event()
        self.copy_box.canceled.connect(self.copy_cancel.set)
        self.ocr_stop.set()
        worker = threading.Thread(target=self.copy_in_background, args=(self.core, target, self.copy_cancel), daemon=True, name="moving")
        self.sorting.append(worker)
        worker.start()

    def copy_in_background(self, core, target, cancel):
        try:
            if self.ocr_thread:
                self.ocr_thread.join()
            result = {"target": target} if core.copy_to(target, self.signals.copying.emit, cancel) else {}
        except Exception as exc:
            result = {"problem": str(exc)}
        try:
            self.signals.copied.emit(result)
        except RuntimeError:
            pass  # The window has gone.

    def archive_copied(self, result):
        self.sorting.pop(0)
        self.copy_box.close()
        self.ocr_stop.clear()
        if "target" in result:
            old = self.data_root
            note = t("Your archive is now in {path}.", path=str(result["target"])) + "\n\n" + t("The old folder is still at {path}. You can delete it once everything looks right.", path=str(old))
            if self.config.get("mcp"):
                note += "\n\n" + t("Connected tools still use the old folder. Copy the setup for your tool again.")
            self.use_archive(result["target"], note)
        else:
            if "problem" in result:
                QMessageBox.warning(self, t("Could not move the archive"), as_plain(t(result["problem"])))
            self.start_reading()

    def use_archive(self, target, note):
        """Everything that reads the data folder turns to the new one, as a restart would."""
        try:
            core = ArchiveCore(target)
            remember_location(BASE, target)
        except (OSError, ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, t("Could not move the archive"), as_plain(t(str(exc))))
            self.start_reading()
            return
        self.ocr_stop.set()
        if self.ocr_thread:
            self.ocr_thread.join(3)
        self.ocr_stop.clear()
        self.core = core
        self.data_root = target
        self.config_file = target / "ui-state.json"
        self.config = self.load_config()
        self.selected_id = None
        self.place = "all"
        self.last_sorted = []
        if self.assistant:
            self.assistant.release_images()
            self.assistant.images_folder = self.assistant_folder()
            self.assistant.load_images()
        self.watch_sources()
        self.apply_language(self.config.get("language", language()))
        self.start_reading()
        QMessageBox.information(self, t("Change location"), as_plain(note))

    def add_drop_settings(self, layout):
        layout.addWidget(label(t("Files you drop on {name}", name=t("Roo")), "caption"))
        action = QComboBox()
        for value, title in (("copy", "Keep a copy, and leave the originals where they are"), ("move", "Move them into my library")):
            action.addItem(t(title), value)
        action.setCurrentIndex(action.findData(self.config.get("drop_action", "copy")))
        layout.addWidget(action)
        where = label("", "caption")
        where.setWordWrap(True)
        where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(where)
        change = button(t("Choose library folder"), None, "folder", "quiet")
        layout.addWidget(change, 0, Qt.AlignLeft)
        def show():
            moving = action.currentData() == "move"
            where.setText(t("They leave their folder for {path}, sorted by subject. You can put any of them back from its details.", path=str(self.library_folder())) if moving
                          else t("A copy is saved in the archive, so a file stays safe even if the original is deleted."))
            change.setVisible(moving)
        def picked():
            self.save_setting("drop_action", action.currentData())
            show()
        def choose():
            chosen = QFileDialog.getExistingDirectory(self, t("Choose library folder"), str(self.library_folder()))
            if chosen:
                self.save_setting("library_folder", chosen)
                show()
        action.currentIndexChanged.connect(picked)
        change.clicked.connect(choose)
        show()

    def add_background_settings(self, layout):
        layout.addWidget(label(t("Quick search shortcut"), "caption"))
        shortcut = QKeySequenceEdit(QKeySequence(self.config.get("hotkey", DEFAULT_HOTKEY)))
        shortcut.setMaximumSequenceLength(1)
        layout.addWidget(shortcut)
        problem = label("", "warning")
        problem.hide()
        layout.addWidget(problem)
        shortcut.editingFinished.connect(lambda: self.change_hotkey(shortcut, problem))
        if self.tray:
            keep = QCheckBox(t("Keep running in the tray after closing the window"))
            keep.setChecked(self.config.get("keep_running", True))
            keep.toggled.connect(lambda checked: self.save_setting("keep_running", checked))
            layout.addWidget(keep)
        roo = QCheckBox(t("Show {name} on the desktop", name=t("Roo")))
        roo.setChecked(self.config.get("assistant", True))
        roo.toggled.connect(self.show_assistant)
        layout.addWidget(roo)
        self.add_drop_settings(layout)
        island = QCheckBox(t("Show new files at the top of the screen"))
        island.setChecked(self.config.get("island", True))
        island.toggled.connect(lambda checked: self.save_setting("island", checked))
        layout.addWidget(island)
        if AUTOSTART_AVAILABLE and not self.demo:
            start = QCheckBox(t("Start when I sign in"))
            start.setChecked(autostart_enabled())
            start.toggled.connect(lambda checked: set_autostart(checked, autostart_command(BASE / "desktop_app.py")))
            layout.addWidget(start)
        layout.addSpacing(6)
        self.add_connected_tools(layout)

    def add_connected_tools(self, layout):
        layout.addWidget(label(t("Connected tools (MCP)"), "caption"))
        read = QCheckBox(t("Let connected tools read this archive"))
        read.setChecked(self.config.get("mcp", False))
        write = QCheckBox(t("Also let them add files to collections"))
        write.setChecked(self.config.get("mcp_write", False))
        write.setEnabled(read.isChecked())
        read.toggled.connect(lambda checked: (self.save_setting("mcp", checked), write.setEnabled(checked), write.setChecked(checked and write.isChecked())))
        write.toggled.connect(lambda checked: self.save_setting("mcp_write", checked))
        layout.addWidget(read)
        layout.addWidget(write)
        note = label(t("A connected tool can read the text of your files, and may send it to an online service."), "caption")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(button(t("Copy setup for your tool"), self.copy_mcp_setup, "copy", "quiet"), 0, Qt.AlignLeft)
        layout.addSpacing(6)

    def mcp_setup(self):
        server = {"command": str(Path(sys.executable).with_name("python.exe") if os.name == "nt" else sys.executable),
                  "args": [str(BASE / "mcp_server.py"), "--data", str(self.data_root)]}
        return json.dumps({"mcpServers": {"roostfile": server}}, ensure_ascii=False, indent=2)

    def copy_mcp_setup(self):
        QApplication.clipboard().setText(self.mcp_setup())
        self.show_status(t("Setup copied. Paste it into your tool's MCP settings."), 5)

    def save_setting(self, key, value):
        self.config[key] = value
        self.save_config()

    def change_hotkey(self, editor, problem):
        wanted = editor.keySequence().toString(QKeySequence.PortableText)
        current = self.config.get("hotkey", DEFAULT_HOTKEY)
        if not wanted or wanted == current:
            return
        accepted = self.hotkey.register(wanted)
        problem.setText("" if accepted else t("That shortcut cannot be used. It may belong to another program."))
        problem.setVisible(not accepted)
        if accepted:
            self.save_setting("hotkey", wanted)
            self.build_tray_menu()
        else:
            self.hotkey.register(current)
            editor.setKeySequence(QKeySequence(current))

    def apply_language(self, code):
        """Rebuild the interface in place; the search and view stay as they were."""
        self.save_tags()
        self.query_timer.stop()
        query, selected = self.search.text(), self.selected_id
        period, sort = self.period.currentIndex(), self.sort.currentIndex()
        set_language(code)
        show_qt_words(code)
        self.config["language"] = code
        self.save_config()
        if self.demo:
            prepare_demo(BASE, code)
            self.core = ArchiveCore(demo_data_dir(BASE, code))
            selected = None
        self.selected_id = None
        self.tags_file_id = None
        self.setWindowTitle(app_name())
        self.build()
        self.setup_shortcuts()
        if self.scanning and not self.scan_automatic:
            self.scan_button.setText(t("Stop update"))
        if self.tray:
            self.tray.setToolTip(app_name())
            self.build_tray_menu()
        self.search.blockSignals(True)
        self.search.setText(query)
        self.search.blockSignals(False)
        for combo, index in ((self.period, period), (self.sort, sort)):
            combo.setCurrentIndex(index)
        self.refresh()
        if selected:
            self.select_file(selected)

    def start_scan(self, checked=False, automatic=False, only=None):
        if self.sorting:
            if not automatic:
                self.show_status(t("Wait for {name} to finish sorting.", name=t("Roo")))
            return
        if self.scanning:
            if not automatic:
                if self.scan_automatic:
                    # A background check is running: show it instead of stopping it.
                    self.scan_automatic = False
                    self.scan_button.setText(t("Stop update"))
                    return
                self.cancel.set()
                self.show_status(t("Stopping safely. Finished records are kept."))
            return
        if not self.core.sources():
            if not automatic:
                self.add_source()
            return
        self.scanning = True
        self.scan_automatic = automatic
        self.scan_partial = only is not None
        self.scan_refreshed = 0.0
        self.cancel.clear()
        if not automatic:
            self.scan_button.setText(t("Stop update"))
        def work():
            try:
                result = self.core.scan_all(progress=self.signals.progress.emit, cancel=self.cancel, only=only)
                self.signals.finished.emit(result)
            except Exception as exc:
                if not self.closing:
                    self.signals.failed.emit(str(exc))
        self.scan_thread = threading.Thread(target=work, daemon=True)
        self.scan_thread.start()
        if not automatic and not self.rows:
            self.refresh_results()

    def scan_progress(self, progress):
        if self.scan_automatic:
            return
        self.show_status(t("Indexing {count} files  ·  {name}", count=progress.get("scanned", 0), name=progress.get("name", "")))
        moment = time.monotonic()
        if moment - self.scan_refreshed < SCAN_REFRESH_SECONDS:
            return
        self.scan_refreshed = moment
        self.refresh_stats()
        # A full page is left alone, so the list does not jump under the reader.
        if len(self.rows) < PAGE_SIZE and QApplication.focusWidget() is not self.tags_edit:
            self.refresh_results()
        elif self.search_args:
            self.result_count.setText(file_count(self.matches()))

    def scan_finished(self, result):
        automatic = self.scan_automatic
        self.scanning = False
        self.scan_automatic = False
        self.last_scan_end = time.monotonic()
        self.scan_button.setText(t("Update"))
        self.watch_sources()
        if self.hotkey:
            self.start_reading()
        # A full check finds what arrived hours ago, which is not news.
        if automatic and self.scan_partial:
            self.announce_arrivals(result.get("arrived", []))
        if automatic and not result.get("indexed") and not result.get("state_changes"):
            self.scan_new_sources()
            return
        self.refresh_stats()
        if QApplication.focusWidget() is not self.tags_edit:
            self.refresh_in_place() if automatic else self.refresh_results()
        message = t("Updated  ·  {indexed} new or changed, {skipped} skipped", indexed=result.get("indexed", 0), skipped=result.get("skipped", 0))
        if result.get("cancelled"):
            message = t("Update stopped. Finished records are kept.")
        if result.get("errors"):
            message += t(", {errors} could not be read fully", errors=result["errors"])
        self.show_status(message)
        self.refresh_tip()
        if self.hotkey and self.hotkey.active and not automatic and not self.config.get("hotkey_shown"):
            self.save_setting("hotkey_shown", True)
            self.show_status(t("Press {key} to search from anywhere.", key=key_label(self.config.get("hotkey", DEFAULT_HOTKEY))), 12)
        self.scan_new_sources()

    def scan_new_sources(self):
        """A folder added while something else was running has not been looked at yet."""
        if not self.closing and any(source["last_scan"] is None and source["status"] == "ready" for source in self.core.sources()):
            self.start_scan()

    def refresh_in_place(self):
        """For changes behind the reader's back: the list stays where they left it."""
        view = self.grid if self.view_mode == "grid" else self.list
        offset, loaded, current = view.verticalScrollBar().value(), len(self.rows), view.currentIndex().data(Qt.UserRole)
        self.refresh_results()
        while len(self.rows) < loaded and self.model.canFetchMore():
            self.model.fetchMore()
        row = next((index for index, item in enumerate(self.rows) if current and item["id"] == current["id"]), None)
        if row is not None:
            view.setCurrentIndex(self.model.index(row))
        view.doItemsLayout()
        view.verticalScrollBar().setValue(offset)

    def scan_failed(self, message):
        self.scanning = False
        self.scan_automatic = False
        self.scan_button.setText(t("Update"))
        self.show_status(t("Update failed: {message}", message=t(message)))

    def auto_scan(self):
        if not self.scanning and not self.source_dialog_open:
            self.start_scan(automatic=True)

    def changeEvent(self, event):
        super().changeEvent(event)
        # Not on the open itself: the deck should not shuffle under the pointer.
        if event.type() == QEvent.ActivationChange and self.isActiveWindow() and self.home_stale and not self.search.text():
            self.refresh()

    def update_covers(self):
        self.grid.viewport().update()
        self.deck.refresh_covers()

    def closeEvent(self, event):
        self.save_tags()
        if self.tray and self.config.get("keep_running", True) and not self.quitting:
            self.hide()
            event.ignore()
            if not self.config.get("tray_tip_shown"):
                self.save_setting("tray_tip_shown", True)
                self.tray.showMessage(app_name(), t("Still running here. Press {key} to search from anywhere.", key=key_label(self.config.get("hotkey", DEFAULT_HOTKEY))))
            return
        self.closing = True
        self.ocr_stop.set()
        if self.ocr_thread and self.ocr_thread.is_alive():
            self.ocr_thread.join(3)
        for worker in self.sorting:
            worker.join(3)
        self.cancel.set()
        self.watch.stop()
        self.query_timer.stop()
        self.status_timer.stop()
        self.covers.stop()
        if self.scan_thread and self.scan_thread.is_alive():
            self.scan_thread.join(3)
        event.accept()
        if self.tray:
            QApplication.quit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--render", help="Render this application's widgets to a PNG and exit")
    parser.add_argument("--view", choices=["fan", "grid", "list"], default="fan")
    parser.add_argument("--query", default="")
    parser.add_argument("--select-first", action="store_true")
    parser.add_argument("--line", action="store_true")
    parser.add_argument("--language", choices=list(LANGUAGES))
    parser.add_argument("--background", action="store_true", help="Start in the tray, as at sign-in")
    parser.add_argument("--width", type=int, default=1360)
    parser.add_argument("--height", type=int, default=850)
    args = parser.parse_args()
    if os.name == "nt":
        import ctypes
        # Otherwise the taskbar files this window under python.exe and uses its icon.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Roostfile.Archive")
    app = QApplication(sys.argv[:1])
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPixelSize(13)
    app.setFont(font)
    app.setStyleSheet(STYLE)
    instance = SingleInstance(str(BASE / (".demo-data" if args.demo else "data")))
    if not args.render and not instance.claim():
        return 0
    if not args.demo and not args.render and not find_archive():
        return 0
    if args.demo:
        (BASE / ".demo-data").mkdir(exist_ok=True)
        probe = args.language or json_language(BASE / ".demo-data" / "ui-state.json")
        prepare_demo(BASE, probe)
    window = ArchiveWindow(demo=args.demo, language_override=args.language)
    window.resize(args.width, args.height)
    if args.render:
        window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.set_view(args.view)
    if args.query:
        window.search.setText(args.query)
        window.refresh_results()
    if args.select_first and window.rows:
        window.select_file(window.rows[0]["id"])
    if args.line:
        window.toggle_deck_layout()
    if not args.render:
        window.start_background(app)
        instance.activated.connect(window.show_main)
    if not args.background or not window.tray:
        window.show()
    if args.render:
        window.deck.set_reduced_motion(True)
        app.processEvents()
        def render():
            window.grab().save(str(Path(args.render).resolve()), "PNG")
            app.quit()
        QTimer.singleShot(450, render)
    code = app.exec()
    ocr.release()
    return code


def find_archive():
    """A chosen folder may be on a drive that is not connected. An empty archive in its place would look like everything had gone."""
    set_language(json_language(BASE / "data" / "ui-state.json"))
    while True:
        folder = data_location(BASE)
        if folder == BASE / "data" or has_archive(folder):
            return True
        box = QMessageBox(QMessageBox.Warning, app_name(), as_plain(t("Could not find your archive at {path}. If it is on an external drive, connect the drive and try again.", path=str(folder))))
        retry = box.addButton(t("Try again"), QMessageBox.AcceptRole)
        default = box.addButton(t("Use the default folder"), QMessageBox.DestructiveRole)
        box.addButton(t("Quit"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() == default:
            forget_location(BASE)
        elif box.clickedButton() != retry:
            return False


def json_language(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("language", "en") if isinstance(data, dict) else "en"
    except (OSError, ValueError):
        return "en"


if __name__ == "__main__":
    sys.exit(main())
