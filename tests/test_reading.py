"""Real PDF pages and the words in photos and scans. Needs Qt; reading needs Windows."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_app as ui
from demo import prepare_demo
import ocr
from pages import page_count, render_page

try:
    import pypdfium2
except ImportError:
    pypdfium2 = None


def simple_pdf(pages=1):
    """A small real PDF, with one line of text on each page."""
    kids = " ".join(f"{3 + 2 * page} 0 R" for page in range(pages))
    objects = [b"<</Type/Catalog/Pages 2 0 R>>", f"<</Type/Pages/Kids[{kids}]/Count {pages}>>".encode()]
    font = 3 + 2 * pages
    for page in range(pages):
        stream = f"BT /F1 24 Tf 40 700 Td (Page {page}) Tj ET".encode()
        objects.append(f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents {4 + 2 * page} 0 R/Resources<</Font<</F1 {font} 0 R>>>>>>".encode())
        objects.append(b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    table = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objects) + 1, table))
    return out.getvalue()


class TidyTests(unittest.TestCase):
    def test_chinese_loses_the_spaces_the_recogniser_adds(self):
        self.assertEqual(ocr.tidy("增 值 税 电 子 普 通 发 票"), "增值税电子普通发票")
        self.assertEqual(ocr.tidy("开 票 日 期 2026 年 03 月 1 2 日"), "开票日期2026年03月12日")

    def test_english_words_stay_apart(self):
        self.assertEqual(ocr.tidy("wifi password here"), "wifi password here")
        self.assertEqual(ocr.tidy("wifi 密 码"), "wifi 密码")


@unittest.skipUnless(pypdfium2, "Needs pypdfium2")
class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = ui.QApplication.instance() or ui.QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "报告 文档.pdf"
        self.path.write_bytes(simple_pdf(3))

    def test_a_page_is_drawn_at_the_width_asked_for(self):
        self.assertEqual(page_count(self.path), 3)
        image = render_page(self.path, 1, 300)
        self.assertEqual(image.width(), 300)
        self.assertAlmostEqual(image.height(), 300 * 792 / 612, delta=1)
        colors = {image.pixelColor(x, y).name() for x in range(0, 300, 3) for y in range(0, 388, 3)}
        self.assertIn("#ffffff", colors)
        self.assertGreater(len(colors), 1)

    def test_a_damaged_pdf_is_a_null_image_rather_than_an_error(self):
        self.path.write_bytes(b"%PDF-1.4 not really")
        self.assertTrue(render_page(self.path, 0, 200).isNull())
        self.assertEqual(page_count(self.path), 0)

    def test_a_pdf_cover_is_its_real_first_page(self):
        loader = ui.CoverLoader()
        self.addCleanup(loader.stop)
        row = {"id": 1, "name": self.path.name, "ext": ".pdf", "path": str(self.path), "size": 1, "mtime": 1, "text": ""}
        self.assertIsNone(loader.cover(row, 200, 260))
        cover = None
        while cover is None:
            self.app.processEvents()
            cover = loader.cover(row, 200, 260)
        self.assertEqual(cover.toImage().pixelColor(100, 200).name(), "#ffffff")


@unittest.skipUnless(pypdfium2, "Needs pypdfium2")
class PagePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = ui.QApplication.instance() or ui.QApplication([])
        cls.app.setStyleSheet(ui.STYLE)
        prepare_demo(ui.BASE, "en")

    def setUp(self):
        # A cover may still be drawing from the file as the test ends.
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.folder.cleanup)
        self.window = ui.ArchiveWindow(demo=True, language_override="en")
        self.window.setAttribute(ui.Qt.WA_DontShowOnScreen, True)
        self.window.show()
        self.addCleanup(self.window.close)
        source = Path(self.folder.name) / "source"
        source.mkdir()
        (source / "report.pdf").write_bytes(simple_pdf(ui.PREVIEW_PAGES + 2))
        self.window.core = ui.ArchiveCore(Path(self.folder.name) / "data")
        self.window.core.add_source(source)
        self.window.core.scan_all()
        self.window.refresh_results()
        self.row = self.window.core.get_file(self.window.rows[0]["id"])

    def test_the_preview_panel_shows_the_real_page(self):
        self.window.select_file(self.row["id"])
        self.assertTrue(self.window.preview_image.isVisible())
        self.assertFalse(self.window.preview_image.pixmap().isNull())

    def test_the_full_preview_scrolls_through_pages_and_says_when_it_stops(self):
        scroll = self.window.preview_pages(self.row)
        column = scroll.widget().layout()
        self.assertEqual(column.count(), ui.PREVIEW_PAGES + 1)
        self.assertEqual(column.itemAt(column.count() - 1).widget().text(), f"Showing the first {ui.PREVIEW_PAGES} of {ui.PREVIEW_PAGES + 2} pages")

    def test_other_files_have_no_pages(self):
        self.assertIsNone(self.window.preview_pages(dict(self.row, ext=".txt", kind="Plain text")))


@unittest.skipUnless(os.name == "nt" and ocr.available(), "Needs a Windows recogniser")
class WindowsReadingTests(unittest.TestCase):
    """desktop_app has already turned on the socket block, as it is in real use."""

    @classmethod
    def setUpClass(cls):
        cls.app = ui.QApplication.instance() or ui.QApplication([])

    def picture(self, text, path, size=(1200, 600), pixels=44):
        image = ui.QPixmap(*size)
        image.fill(ui.QColor("#FFFFFF"))
        painter = ui.QPainter(image)
        painter.setPen(ui.QColor("#000000"))
        font = ui.QFont("Microsoft YaHei")
        font.setPixelSize(pixels)
        painter.setFont(font)
        painter.drawText(image.rect().adjusted(40, 40, -40, -40), ui.Qt.AlignLeft | ui.Qt.TextWordWrap, text)
        painter.end()
        image.save(str(path))

    def test_words_in_a_picture_are_read_without_added_spaces(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "发票.png"
            self.picture("增值税电子普通发票 上海星河科技有限公司", path)
            text = ocr.read_file(path)
        self.assertIn("星河科技", text)

    def run_app(self, ending):
        code = ("import sys; sys.path.insert(0, %r); import desktop_app as ui; app = ui.QApplication(['x']); ui.ocr.available(); %s"
                % (str(ui.BASE), ending))
        return subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60).returncode

    def test_quitting_after_the_recogniser_was_used_does_not_crash(self):
        self.assertEqual(self.run_app("ui.ocr.release()"), 0)

    def test_a_tiny_icon_is_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "icon.png"
            self.picture("图", path, (64, 64), 20)
            self.assertEqual(ocr.read_file(path), "")

    def test_a_broken_file_reads_as_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.jpg"
            path.write_bytes(b"not an image")
            self.assertEqual(ocr.read_file(path), "")


if __name__ == "__main__":
    unittest.main()
