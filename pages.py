"""Draws real PDF pages with PDFium, for covers, previews and reading scans.

PDFium is not thread-safe, so every call goes through one lock. A page that
cannot be drawn gives a null image, and without the library everything still
works with text covers.
"""
from __future__ import annotations

import threading

from PySide6.QtGui import QImage, QImageReader

_LOCK = threading.Lock()


def image_reader(path) -> QImageReader:
    """A reader that never decodes SVG, however the file is named.

    An SVG can point at other files, and Qt opens them. On Windows that includes
    \\\\server\\share paths, so merely drawing a cover would reach out to a stranger's machine.
    """
    reader = QImageReader(str(path))
    # Asking the format only looks at the first bytes. An empty reader then reads as nothing.
    return QImageReader() if bytes(reader.format()) in (b"svg", b"svgz") else reader


def _open(path):
    try:
        import pypdfium2
    except ImportError:
        return None
    return pypdfium2.PdfDocument(str(path))


def page_count(path) -> int:
    with _LOCK:
        try:
            document = _open(path)
            if document is None:
                return 0
            try:
                return len(document)
            finally:
                document.close()
        except Exception:
            return 0  # Encrypted or damaged.


def render_page(path, index: int, width: int) -> QImage:
    with _LOCK:
        try:
            document = _open(path)
            if document is None:
                return QImage()
            try:
                page = document[index]
                bitmap = page.render(scale=width / page.get_width(), force_bitmap_format=_BGRA())
                data = bytes(bitmap.buffer)
                # Copied so the image outlives the bitmap and the document.
                return QImage(data, bitmap.width, bitmap.height, bitmap.stride, QImage.Format_ARGB32).copy()
            finally:
                document.close()
        except Exception:
            return QImage()


def _BGRA():
    from pypdfium2 import raw
    return raw.FPDFBitmap_BGRA
