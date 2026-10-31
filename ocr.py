"""Reads the words in photos and scanned pages with the recogniser built into the system.

That is Windows.Media.Ocr, or the Vision framework on macOS. It runs on this
computer, needs no model download and sends nothing anywhere. Without one,
``available`` is False and files stay searchable by name, date and tags.
"""
from __future__ import annotations

import re
import sys
import threading
import time

from PySide6.QtCore import QBuffer, QSize, Qt
from PySide6.QtGui import QImage

from pages import image_reader, page_count, render_page

MAX_PDF_PAGES = 20
PDF_PAGE_WIDTH = 1654  # A4 at 200 dpi.
MIN_LONG_SIDE = 240  # Anything smaller is an icon rather than a document.
SHARPEST_LONG_SIDE = 1800  # Small text reads better enlarged.
MAX_LONG_SIDE = 4000
MAX_PIXELS = 120_000_000
_CJK = r"　-〿㐀-鿿＀-￯"
# The recogniser separates every Chinese character, and sometimes digits, with spaces.
_LOOSE = re.compile(rf"(?<=[{_CJK}0-9]) (?=[{_CJK}])|(?<=[{_CJK}]) (?=[0-9])|(?<=[0-9]) (?=[0-9])")
_FAILURES = (OSError, RuntimeError, ValueError, AttributeError)  # AttributeError: released while exiting.
_engine = None
_engine_checked = False
_engine_lock = threading.Lock()


def tidy(line: str) -> str:
    return _LOOSE.sub("", line)


def _load_engine():
    global _engine, _engine_checked
    with _engine_lock:
        if not _engine_checked:
            _engine_checked = True
            try:
                if sys.platform == "darwin":
                    import Vision
                    _engine = Vision
                else:
                    from winrt.windows.globalization import Language
                    from winrt.windows.media.ocr import OcrEngine
                    _engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN")) or OcrEngine.try_create_from_user_profile_languages()
            except (ImportError, OSError):
                _engine = None
        return _engine


def available() -> bool:
    return _load_engine() is not None


def release():
    """Call before exit. Freed after Qt has shut COM down, the Windows engine crashes the process."""
    global _engine
    with _engine_lock:
        _engine = None


def _recognise_windows(image: QImage) -> str:
    from winrt.windows.foundation import AsyncStatus
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter
    image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    writer = DataWriter()
    writer.write_bytes(bytes(image.constBits()))
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                    image.width(), image.height(), BitmapAlphaMode.PREMULTIPLIED)

    # Polled, because an asyncio loop needs a socket pair and offline.py refuses all sockets.
    reading = _load_engine().recognize_async(bitmap)
    while reading.status == AsyncStatus.STARTED:
        time.sleep(.002)
    return "\n".join(tidy(line.text) for line in reading.get_results().lines)


def _recognise_mac(image: QImage) -> str:
    """Apple's Vision framework, which also runs on this computer."""
    from Foundation import NSData
    vision = _load_engine()
    png = QBuffer()
    png.open(QBuffer.ReadWrite)
    image.save(png, "PNG")
    data = bytes(png.data())
    request = vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLanguages_(["zh-Hans", "en-US"])
    request.setRecognitionLevel_(0)  # Accurate.
    handler = vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithBytes_length_(data, len(data)), {})
    done, _ = handler.performRequests_error_([request], None)
    if not done:
        return ""
    lines = (result.topCandidates_(1) for result in request.results() or ())
    return "\n".join(tidy(str(found[0].string())) for found in lines if found)


def _recognise(image: QImage) -> str:
    return _recognise_mac(image) if sys.platform == "darwin" else _recognise_windows(image)


def read_image(path) -> str:
    reader = image_reader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    long_side = max(size.width(), size.height())
    if long_side < MIN_LONG_SIDE or size.width() * size.height() > MAX_PIXELS:
        return ""
    target = min(max(long_side, min(long_side * 2, SHARPEST_LONG_SIDE)), MAX_LONG_SIDE)
    reader.setScaledSize(size.scaled(QSize(target, target), Qt.KeepAspectRatio))
    image = reader.read()
    return "" if image.isNull() else _recognise(image)


def read_pdf(path) -> str:
    parts, trouble = [], None
    for index in range(min(page_count(path), MAX_PDF_PAGES)):
        image = render_page(path, index, PDF_PAGE_WIDTH)
        if not image.isNull():
            try:
                parts.append(_recognise(image))
            except _FAILURES as exc:
                trouble = exc  # The other pages are still worth having.
    if trouble and not any(parts):
        raise trouble
    return "\n".join(part for part in parts if part)


def read_file(path) -> str | None:
    """The words in a photo or scan, which may be none. None means reading failed, so try again another time."""
    try:
        return read_pdf(path) if str(path).lower().endswith(".pdf") else read_image(path)
    except _FAILURES:
        return None
