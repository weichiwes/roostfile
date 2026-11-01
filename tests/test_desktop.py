"""Native Qt integration tests, restricted to the synthetic demo archive."""
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_app as ui
from assistant import BUBBLE_WIDTH
import character as ui_character
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, QRectF, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QEnterEvent, QImage, QKeyEvent, QMouseEvent, QPainter, QTextDocument
from PySide6.QtTest import QTest
from demo import prepare_demo
from i18n import set_language, t
from visual_widgets import _file_key, decode_cover


def pillow():
    try:
        from PIL import Image
    except ImportError:
        return None
    return Image


class StayingQuickSearch(ui.QuickSearch):
    """Open however the test machine's windows are stacked; one test covers closing."""

    def changeEvent(self, event):
        ui.QFrame.changeEvent(self, event)


class CoverLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = ui.QApplication.instance() or ui.QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.loader = ui.CoverLoader()
        self.addCleanup(self.loader.stop)

    def wait_for_cover(self, row):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.app.processEvents()
            cover = self.loader.cover(row, 200, 260)
            if cover is not None:
                return cover
            time.sleep(.01)
        self.fail("The cover never arrived")

    def photo(self, name, color):
        path = Path(self.folder.name) / name
        image = ui.QPixmap(800, 600)
        image.fill(ui.QColor(color))
        image.save(str(path))
        return {"id": 1, "name": name, "ext": path.suffix, "path": str(path), "size": path.stat().st_size, "mtime": path.stat().st_mtime}

    def test_a_photo_is_read_in_the_background_and_then_cached(self):
        row = self.photo("first.png", "#3060A0")
        self.assertIsNone(self.loader.cover(row, 200, 260))
        cover = self.wait_for_cover(row)
        self.assertEqual(cover.toImage().pixelColor(100, 130).name(), "#3060a0")
        self.assertIs(self.loader.cover(row, 200, 260), cover)

    def test_an_unreadable_photo_falls_back_to_a_document_cover(self):
        row = self.photo("broken.png", "#3060A0")
        Path(row["path"]).write_bytes(b"not an image")
        self.assertFalse(self.wait_for_cover(row).isNull())

    def test_other_files_get_their_cover_at_once(self):
        path = Path(self.folder.name) / "note.txt"
        path.write_text("text", encoding="utf-8")
        self.assertIsNotNone(self.loader.cover({"id": 2, "name": "note.txt", "ext": ".txt", "path": str(path), "text": "text"}, 200, 260))

    def test_requests_too_old_to_be_on_screen_are_dropped(self):
        self.loader._workers.shutdown(wait=True)
        self.loader._workers = SimpleNamespace(submit=lambda job: None, shutdown=lambda **kwargs: None)
        for number in range(self.loader.MAX_WAITING + 10):
            self.loader.cover(self.photo(f"{number}.png", "#102030"), 200, 260)
        self.assertEqual(len(self.loader._waiting), self.loader.MAX_WAITING)


class DesktopTests(unittest.TestCase):
    language = "zh"

    @classmethod
    def setUpClass(cls):
        cls.app = ui.QApplication.instance() or ui.QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(ui.STYLE)
        prepare_demo(ui.BASE, cls.language)

    def setUp(self):
        scratch = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(scratch.cleanup)
        self.scratch = scratch.name
        self.window = ui.ArchiveWindow(demo=True, language_override=self.language)
        self.window.setAttribute(ui.Qt.WA_DontShowOnScreen, True)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        set_language("en")

    def test_search_retrieval_filters_and_preview(self):
        self.window.search.setText("认知负荷")
        self.window.refresh_results()
        self.assertEqual(len(self.window.rows), 1)
        self.assertEqual(self.window.view_mode, "list")
        row = self.window.rows[0]
        self.assertEqual(row["match_source"], "body")
        self.window.select_file(row["id"])
        self.assertIn("认知负荷", self.window.preview_text.toPlainText())
        self.assertTrue(self.window.details.isVisible())
        self.assertTrue(self.window.preview_text.extraSelections())
        self.window.select_place("type:Images")
        self.assertEqual(self.window.rows, [])
        self.window.reset_filters()
        self.assertGreaterEqual(len(self.window.rows), 9)

    def test_entry_point_finds_its_modules_under_an_embedded_python(self):
        # -I leaves the script's folder off sys.path, as runtime\\python.exe does.
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, "-I", str(ui.BASE / "desktop_app.py"), "--help"],
                                    cwd=folder, capture_output=True, text=True, timeout=60)
        self.assertNotIn("No module named 'offline'", result.stderr)

    def use_library(self, files):
        """Points the window at an empty archive holding only these simulated files."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        source = Path(folder.name) / "source"
        source.mkdir()
        for name, text in files.items():
            (source / name).write_text(text, encoding="utf-8")
        self.window.core = ui.ArchiveCore(Path(folder.name) / "data")
        self.window.core.add_source(source)
        return source

    def assistant(self):
        assistant = ui.Assistant(Path(self.scratch) / "assistant")
        assistant.setAttribute(ui.Qt.WA_DontShowOnScreen, True)
        self.addCleanup(assistant.close)
        self.window.assistant = assistant
        self.addCleanup(setattr, self.window, "assistant", None)
        self.window.assistant_folder = lambda: Path(self.scratch) / "assistant"
        self.window.assistant_watch = QTimer()
        assistant.dropped.connect(self.window.organise_drop)
        assistant.show()
        return assistant

    def picture(self, name, color="#CC3344", size=64):
        image = QImage(size, size, QImage.Format_ARGB32)
        image.fill(QColor(color))
        path = Path(self.scratch) / name
        self.assertTrue(image.save(str(path)))
        return path

    def wait_until(self, done, seconds=10):
        limit = time.monotonic() + seconds
        while not done() and time.monotonic() < limit:
            self.app.processEvents()
            time.sleep(.01)
        self.assertTrue(done())

    def drop_on(self, assistant, *paths):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        spot = QPointF(assistant.width() / 2, assistant.height() / 2)
        offered = ui.Qt.CopyAction | ui.Qt.MoveAction | ui.Qt.LinkAction
        enter = QDragEnterEvent(spot.toPoint(), offered, mime, ui.Qt.LeftButton, ui.Qt.NoModifier)
        assistant.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        self.assertEqual(enter.dropAction(), ui.Qt.CopyAction)
        self.assertEqual(assistant.mood, "excited")
        move = QDragMoveEvent(spot.toPoint(), offered, mime, ui.Qt.LeftButton, ui.Qt.NoModifier)
        assistant.dragMoveEvent(move)
        self.assertEqual(move.dropAction(), ui.Qt.CopyAction)
        drop = QDropEvent(spot, offered, mime, ui.Qt.LeftButton, ui.Qt.NoModifier)
        drop.setDropAction(ui.Qt.MoveAction)
        assistant.dropEvent(drop)
        self.assertTrue(drop.isAccepted())
        self.assertEqual(drop.dropAction(), ui.Qt.CopyAction)

    def test_a_drag_that_can_only_move_is_refused(self):
        # Agreeing to a move tells File Explorer to remove the original.
        assistant = self.assistant()
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(Path(self.scratch) / "a.txt"))])
        spot = QPointF(10, 10)
        enter = QDragEnterEvent(spot.toPoint(), ui.Qt.MoveAction, mime, ui.Qt.LeftButton, ui.Qt.NoModifier)
        assistant.dragEnterEvent(enter)
        self.assertFalse(enter.isAccepted())
        self.assertEqual(assistant.mood, "idle")
        dropped = MagicMock()
        assistant.dropped.connect(dropped)
        drop = QDropEvent(spot, ui.Qt.MoveAction, mime, ui.Qt.LeftButton, ui.Qt.NoModifier)
        assistant.dropEvent(drop)
        self.assertFalse(drop.isAccepted())
        dropped.assert_not_called()

    def test_roo_is_drawn_in_every_mood_at_every_size(self):
        assistant = self.assistant()
        for size in ui.SIZES:
            assistant.set_size(size)
            for mood in ("idle", "excited", "working", "happy"):
                assistant.set_mood(mood)
                assistant.frame += 7
                image = assistant.grab().toImage()
                with self.subTest(size=size, mood=mood):
                    middle = image.pixelColor(image.width() // 2, int(image.height() * .75))
                    self.assertGreater(middle.alpha(), 200)
                    self.assertNotEqual(middle.getRgb()[:3], (0, 0, 0))

    def test_her_eyes_follow_and_she_blinks(self):
        rig = ui_character.Rig()
        pose = None
        for _ in range(60):
            pose = rig.step(1 / 30, "idle", (.9, 0))
        self.assertGreater(pose.eye_x, .7)
        self.assertGreater(pose.angle_x, .5)
        rig.next_blink = 0
        shut = min(rig.step(1 / 30).eye_open for _ in range(8))
        self.assertLess(shut, .3)
        self.assertGreater(max(rig.step(1 / 30).eye_open for _ in range(8)), .9)

    def test_the_rig_cannot_blow_up_at_a_long_frame(self):
        rig = ui_character.Rig()
        for look in ((1, 1), (-1, -1)) * 20:
            pose = rig.step(.2, "excited", look, True)
            self.assertTrue(all(abs(getattr(pose, field.name)) < 1e4 for field in ui_character.fields(pose)))

    def test_her_irises_stay_inside_her_eyes(self):
        puppet = ui_character.Puppet()
        puppet.prepare(192)
        self.assertFalse(puppet.white_outline.isEmpty())
        looked = []
        for look in (-1, 1):
            image = QImage(192, 230, QImage.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            painter.translate(96, 226)
            puppet.draw(painter, 192, ui_character.Pose(eye_x=look))
            painter.end()
            looked.append(bytes(image.constBits()))
        self.assertNotEqual(*looked)

    def test_every_layer_is_a_real_picture(self):
        puppet = ui_character.Puppet()
        self.assertGreaterEqual(len(puppet.layers), 28)
        for name, (image, rect) in puppet.layers.items():
            self.assertFalse(image.isNull(), name)
            self.assertTrue(image.hasAlphaChannel(), name)
        self.assertLess(sum(path.stat().st_size for path in ui_character.ASSETS.iterdir()), 2 * 1024 * 1024)

    def test_she_stays_icon_sized(self):
        self.assertEqual(ui.SIZES, {"small": 72, "medium": 96, "large": 128})
        assistant = self.assistant()
        assistant.set_size("medium")
        self.assertLessEqual(assistant.height(), 125)

    def drawn(self, puppet, pose, size=192):
        image = QImage(size, round(size * 1.2), QImage.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        painter.translate(size / 2, image.height() - 4)
        puppet.draw(painter, size, pose)
        painter.end()
        return image

    def test_turning_her_head_leaves_its_outline_where_it_was(self):
        puppet = ui_character.Puppet()
        still = self.drawn(puppet, ui_character.Pose())
        for turned in (-1.0, 1.0):
            moved = self.drawn(puppet, ui_character.Pose(angle_x=turned))
            for y in range(4, 60):  # The top of her hair, where layers used to come apart.
                for x in range(40, 150, 6):
                    self.assertLess(abs(still.pixelColor(x, y).alpha() - moved.pixelColor(x, y).alpha()), 200, (x, y))

    def test_everything_on_her_head_moves_with_the_same_turn(self):
        puppet = ui_character.Puppet()
        for moved in puppet.moved(QPointF(puppet.head.x() + 1000, puppet.head.y()), 12, 6):
            self.assertAlmostEqual(moved, 0)
        middle = puppet.moved(puppet.head, 12, 0)[0]
        self.assertAlmostEqual(middle, ui_character.TURN)

    def test_a_turn_is_kept_rather_than_warped_again(self):
        puppet = ui_character.Puppet()
        puppet.prepare(96)
        puppet.warped("key down", 3, 0)
        kept = puppet.warped("key down", 3, 0)
        self.assertIs(kept, puppet.warped("key down", 3, 0))
        self.assertEqual(len(puppet.warps), 1)

    def test_kept_layers_stay_within_their_limit(self):
        puppet = ui_character.Puppet()
        puppet.prepare(96)
        for turn in range(ui_character.CACHED + 20):
            puppet.remember(puppet.warps, turn, None)
        self.assertLessEqual(len(puppet.warps), ui_character.CACHED)

    def test_every_action_is_a_whole_painting(self):
        puppet = ui_character.Puppet()
        for mood, key in (("idle", "key down"), ("working", "key hold hug"), ("greeting", "key wave top"), ("stretching", "key stretch top"),
                          ("napping", "key nap"), ("dangling", "key lifted")):
            with patch.object(puppet, "put_warped", wraps=puppet.put_warped) as put:
                self.drawn(puppet, ui_character.replace(ui_character._MOODS[mood], clock=0))
            self.assertEqual(put.call_args_list[0].args[1], key, mood)

    def test_an_action_passes_through_its_paintings_on_the_way_up(self):
        puppet, rig = ui_character.Puppet(), ui_character.Rig()
        keys = []
        for _ in range(60):
            key = puppet.key(rig.step(1 / 30, "working"))
            if not keys or keys[-1] != key:
                keys.append(key)
        self.assertEqual(keys, ["key down", "key hold low", "key hold hug"])

    def test_held_up_mid_action_she_lets_go_of_it(self):
        puppet = ui_character.Puppet()
        self.assertEqual(puppet.key(ui_character.Pose(lifted=1, hold=1, book=1)), "key lifted")

    def test_at_the_sip_the_cup_covers_her_mouth(self):
        puppet = ui_character.Puppet()
        pose = ui_character.Pose(cup=1, clock=math.pi / 2 / .95)
        self.assertEqual(puppet.key(pose), "key tea sip")
        with patch.object(puppet, "mouth_") as mouth:
            self.drawn(puppet, pose)
        mouth.assert_not_called()

    def test_every_painting_the_rig_can_ask_for_exists(self):
        puppet = ui_character.Puppet()
        keys = {name for name in puppet.layers if name.startswith("key ")}
        asked = set()
        for field in ui_character.ARMS + ("lifted", "nap"):
            for amount in (.1, .3, .5, .8, 1):
                for clock in (0, .4, .8, 1.2, 2, 2.6, 3.4):
                    pose = ui_character.replace(ui_character.Pose(clock=clock), **{field: amount})
                    asked.add(puppet.key(pose))
                    asked.add(puppet.key(ui_character.replace(pose, swing=10.0)))
        self.assertEqual(asked, keys)

    def test_the_whole_window_catches_a_drop(self):
        # Fully clear pixels let a drop fall through to the desktop.
        image = self.assistant().grab().toImage()
        self.assertGreater(image.pixelColor(1, 1).alpha(), 0)

    def svg_pointing_at(self, target, name):
        path = Path(self.scratch) / name
        path.write_text(f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="64" height="64">
            <image width="64" height="64" xlink:href="{target}"/></svg>''', encoding="utf-8")
        return path

    def test_an_svg_is_never_decoded_whatever_it_is_called(self):
        # It can name a \\\\server\\share path, and decoding it would connect there.
        other = self.picture("elsewhere.png")
        for name in ("a.svg", "disguised.png", "disguised.jpg", "no-extension"):
            path = self.svg_pointing_at(other, name)
            with self.subTest(name=name):
                self.assertTrue(decode_cover(path, 64, 64).isNull())
                self.assertIsNone(ui.preview_pixmap({"path": str(path), "ext": path.suffix}, 64, 64))
                self.assertEqual(ui.ocr.read_image(path), "")
                self.assertFalse(ui.install_image(path, Path(self.scratch) / "look"))

    def test_real_pictures_still_decode(self):
        path = self.picture("real.png")
        self.assertFalse(decode_cover(path, 32, 32).isNull())
        self.assertIsNotNone(ui.preview_pixmap({"path": str(path), "ext": ".png"}, 32, 32))

    def test_markup_in_a_name_stays_text(self):
        hostile = '<img src="\\\\192.0.2.1\\share\\x.png">'
        made = ui.label(hostile)
        self.assertEqual(made.textFormat(), ui.Qt.PlainText)
        self.assertEqual(self.assistant().bubble.text.textFormat(), ui.Qt.PlainText)
        tip = ui.as_plain(hostile)
        self.assertNotIn("<img", tip)
        document = QTextDocument()
        document.setHtml(tip)
        self.assertEqual(document.toPlainText(), hostile)

    def test_a_tooltip_on_a_result_cannot_carry_markup(self):
        model = ui.FileModel(lambda: None)
        self.addCleanup(model.deleteLater)
        model.replace([{"id": 1, "name": '<img src="x.png">.txt', "ext": ".txt", "kind": "Plain text"}], 1)
        self.assertNotIn("<img", model.data(model.index(0), ui.Qt.ToolTipRole))

    def quick(self):
        quick = ui.QuickSearch(self.window)
        quick.setAttribute(ui.Qt.WA_DontShowOnScreen, True)
        self.addCleanup(quick.deleteLater)
        quick.toggle()
        return quick

    def press(self, quick, key, modifiers=ui.Qt.NoModifier):
        quick.eventFilter(quick.search, QKeyEvent(QEvent.KeyPress, key, modifiers))

    def test_switching_language_on_the_first_run_screen_keeps_it_working(self):
        self.use_library({})
        self.window.core.remove_source(self.window.core.sources()[0]["id"])
        with patch.object(ui, "suggested_folders", return_value=[("Documents", "D:/a")]):
            self.window.refresh()
            self.assertEqual(len(self.window.starter_boxes), 1)
            with patch.object(self.window, "demo", False):
                self.window.apply_language("en")
            self.app.processEvents()
            self.app.sendPostedEvents(None, QEvent.DeferredDelete)
            self.window.refresh()
            self.assertEqual(self.window.starter_boxes[0].property("path"), "D:/a")
            self.assertTrue(self.window.starter_boxes[0].isChecked())

    def test_copying_with_no_results_leaves_the_clipboard_alone(self):
        source = self.use_library({"salary.txt": "工资"})
        self.window.core.scan_all()
        self.window.refresh()
        self.window.select_file(self.window.rows[0]["id"])
        self.app.clipboard().setText("mine")
        quick = self.quick()
        quick.search.setText("nothingmatchesthis")
        quick.refresh()
        self.press(quick, ui.Qt.Key_C, ui.Qt.ControlModifier)
        self.press(quick, ui.Qt.Key_C, ui.Qt.ControlModifier | ui.Qt.ShiftModifier)
        self.assertEqual(self.app.clipboard().text(), "mine")
        self.assertTrue(quick.isVisible())
        quick.dismiss()

    def test_a_key_right_after_typing_acts_on_what_was_typed(self):
        self.use_library({"apple-plan.txt": "甲", "zebra-note.txt": "乙"})
        self.window.core.scan_all()
        self.window.refresh()
        quick = self.quick()
        quick.search.setText("zebra")
        self.assertTrue(quick.timer.isActive())
        with patch.object(self.window, "open_file") as opened:
            self.press(quick, ui.Qt.Key_Return)
        name = self.window.core.get_file(opened.call_args[0][0])["name"]
        self.assertEqual(name, "zebra-note.txt")

    def test_a_folder_added_during_a_background_check_is_still_scanned(self):
        source = self.use_library({"a.txt": "甲"})
        self.window.scanning = True
        self.window.scan_automatic = True
        self.window.scan_partial = False
        with patch.object(self.window, "start_scan") as scan:
            self.window.scan_finished({"indexed": 0, "skipped": 1})
        scan.assert_called_once()

    def test_a_folder_added_during_sorting_is_still_scanned(self):
        self.use_library({"a.txt": "甲"})
        assistant = self.assistant()
        self.window.sorting = [MagicMock()]
        with patch.object(self.window, "start_scan") as scan, patch.object(self.window, "start_reading"):
            self.window.finish_sorting({"groups": {"合同": 1}, "files": 1})
        scan.assert_called_once()

    def test_a_source_that_is_unavailable_does_not_scan_for_ever(self):
        self.use_library({"a.txt": "甲"})
        with self.window.core._connect() as db:
            db.execute("UPDATE sources SET status='unavailable'")
        with patch.object(self.window, "start_scan") as scan:
            self.window.scan_new_sources()
        scan.assert_not_called()

    def test_a_deleted_file_shows_as_missing_without_a_refresh(self):
        source = self.use_library({"a.txt": "甲"})
        self.window.core.scan_all()
        self.window.refresh()
        (source / "a.txt").unlink()
        result = self.window.core.scan_all()
        self.assertEqual(result["state_changes"], 1)
        self.window.scanning = self.window.scan_automatic = True
        self.window.scan_finished(result)
        self.assertTrue(self.window.rows[0]["missing"])

    def test_search_during_a_dialog_brings_the_dialog_forward(self):
        quick = ui.QuickSearch(self.window)
        self.addCleanup(quick.deleteLater)
        dialog = MagicMock()
        with patch.object(ui.QApplication, "activeModalWidget", return_value=dialog):
            quick.toggle()
        dialog.activateWindow.assert_called_once()
        self.assertFalse(quick.isVisible())

    def test_a_background_refresh_leaves_the_reader_where_they_were(self):
        self.use_library({f"文件{number:03}.txt": "内容" for number in range(ui.PAGE_SIZE + 40)})
        self.window.core.scan_all()
        self.window.refresh()
        self.window.set_view("list")
        self.window.resize(1200, 700)
        self.app.processEvents()
        view = self.window.list
        while len(self.window.rows) < ui.PAGE_SIZE + 40 and self.window.model.canFetchMore():
            self.window.model.fetchMore()
        view.setCurrentIndex(self.window.model.index(ui.PAGE_SIZE + 20))
        view.doItemsLayout()
        view.verticalScrollBar().setValue(view.verticalScrollBar().maximum() - 40)
        chosen, offset = self.window.selected_id, view.verticalScrollBar().value()
        self.window.scanning = self.window.scan_automatic = True
        self.window.scan_finished({"indexed": 1, "skipped": 0})
        self.assertEqual(view.currentIndex().row(), ui.PAGE_SIZE + 20)
        self.assertEqual(view.verticalScrollBar().value(), offset)
        self.assertEqual(self.window.selected_id, chosen)

    def test_switching_language_during_a_scan_keeps_the_stop_button(self):
        self.window.scanning = True
        self.window.scan_automatic = False
        self.window.apply_language("en")
        self.assertEqual(self.window.scan_button.text(), "Stop update")
        self.window.scanning = False

    def test_qt_words_follow_the_language(self):
        ui.show_qt_words("zh")
        self.assertEqual(QCoreApplication.translate("QPlatformTheme", "Cancel"), "取消")
        ui.show_qt_words("en")
        self.assertEqual(QCoreApplication.translate("QPlatformTheme", "Cancel"), "Cancel")

    def test_favoriting_shows_at_once(self):
        self.use_library({"a.txt": "甲", "b.txt": "乙"})
        self.window.core.scan_all()
        self.window.refresh()
        self.window.select_file(self.window.rows[0]["id"])
        self.window.toggle_favorite()
        self.assertTrue(next(row for row in self.window.rows if row["id"] == self.window.selected_id)["favorite"])

    def test_an_export_that_fails_says_so(self):
        self.use_library({"a.txt": "甲"})
        collection = self.window.core.create_collection("集")
        with patch.object(ui.QFileDialog, "getSaveFileName", return_value=(self.scratch, "")), patch.object(ui.QMessageBox, "warning") as warned:
            self.window.export_collection(collection, "集")
        warned.assert_called_once()

    def test_dialogs_do_not_pile_up(self):
        before = len(self.window.findChildren(ui.QDialog))
        with patch.object(ui.QDialog, "exec", return_value=0):
            for _ in range(3):
                self.window.show_settings()
                self.window.show_sources()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertEqual(len(self.window.findChildren(ui.QDialog)), before)

    def test_an_ampersand_in_a_name_is_not_a_shortcut(self):
        made = ui.NavButton("Q&A", "folder", lambda: None)
        self.addCleanup(made.deleteLater)
        self.assertEqual(made.title, "Q&A")
        self.assertTrue(made.shortcut().isEmpty())
        self.assertEqual(made.text(), "Q&&A")

    def test_a_drag_passing_over_a_working_roo_leaves_it_working(self):
        assistant = self.assistant()
        assistant.set_mood("working")
        assistant.dragLeaveEvent(None)
        self.assertEqual(assistant.mood, "working")

    def test_she_waves_when_she_arrives_and_introduces_herself_once(self):
        assistant = self.assistant()
        assistant.hide()
        self.window.config.pop("greeted", None)
        with patch.object(ui, "user_is_busy", return_value=False):
            self.window.place_assistant()
        self.assertEqual(assistant.mood, "greeting")
        self.assertIn(t("Roo"), assistant.bubble.text.text())
        self.assertTrue(self.window.config["greeted"])
        assistant.set_mood("idle")
        assistant.bubble.hide()
        assistant.hide()
        with patch.object(ui, "user_is_busy", return_value=False):
            self.window.place_assistant()
        self.assertEqual(assistant.mood, "idle")
        self.assertFalse(assistant.bubble.isVisible())

    def test_the_wave_changes_how_she_looks(self):
        assistant = self.assistant()
        assistant.reduced_motion = True
        assistant.rig.reduced_motion = True
        assistant.set_mood("happy")
        calm = bytes(assistant.grab().toImage().constBits())
        assistant.set_mood("greeting")
        self.assertNotEqual(bytes(assistant.grab().toImage().constBits()), calm)

    def press_and_drag(self, assistant, start=(100, 100), end=(170, 130)):
        assistant.mousePressEvent(SimpleNamespace(button=lambda: ui.Qt.LeftButton, globalPosition=lambda: QPointF(*start)))
        assistant.mouseMoveEvent(SimpleNamespace(buttons=lambda: ui.Qt.LeftButton, globalPosition=lambda: QPointF(*end)))

    def test_she_can_be_picked_up_by_her_collar_and_put_down(self):
        assistant = self.assistant()
        moved = MagicMock()
        assistant.moved.connect(moved)
        size = assistant.size()
        self.press_and_drag(assistant)
        self.assertTrue(assistant.lifting)
        self.assertEqual(assistant.mood, "dangling")
        self.assertGreater(assistant.width(), size.width())
        self.assertEqual(assistant.pos() + assistant.grip, QPoint(170, 130))
        feet = assistant.pos() + assistant.margin
        assistant.mouseReleaseEvent(SimpleNamespace(button=lambda: ui.Qt.LeftButton))
        self.assertFalse(assistant.lifting)
        self.assertEqual(assistant.size(), size)
        self.assertEqual(assistant.pos(), feet)
        self.assertEqual(assistant.mood, "happy")
        moved.assert_called_once()

    def test_she_is_put_down_if_the_mouse_release_goes_astray(self):
        assistant = self.assistant()
        size = assistant.size()
        self.press_and_drag(assistant)
        with patch.object(ui.QApplication, "mouseButtons", return_value=ui.Qt.NoButton):
            assistant.tick()
        self.assertFalse(assistant.lifting)
        self.assertEqual(assistant.size(), size)

    def test_being_picked_up_does_not_interrupt_her_sorting(self):
        assistant = self.assistant()
        assistant.set_mood("working")
        self.press_and_drag(assistant)
        assistant.set_mood("happy", 3)
        assistant.mouseReleaseEvent(SimpleNamespace(button=lambda: ui.Qt.LeftButton))
        self.assertEqual(assistant.mood, "happy")

    def test_a_pinned_roo_stays_put(self):
        assistant = self.assistant()
        assistant.pinned = True
        where = assistant.pos()
        self.press_and_drag(assistant)
        self.assertFalse(assistant.lifting)
        self.assertEqual(assistant.pos(), where)

    def test_a_chosen_picture_just_follows_the_pointer(self):
        assistant = self.assistant()
        self.window.set_assistant_image(self.picture("me.png"))
        self.press_and_drag(assistant)
        self.assertFalse(assistant.lifting)
        self.assertEqual(assistant.margin, QPoint(0, 0))

    def test_she_swings_behind_the_hand_that_holds_her(self):
        rig = ui.character.Rig()
        for _ in range(12):
            rig.step(1 / 30, "dangling", pull=1200)
        self.assertLess(rig.pose.swing, -4)
        for _ in range(120):
            rig.step(1 / 30, "dangling", pull=-9000)
            self.assertLessEqual(abs(rig.pose.swing), ui.character.MAX_SWING)

    def test_every_action_looks_different(self):
        assistant = self.assistant()
        assistant.plays = False
        assistant.reduced_motion = True
        looks = {}
        for mood in ("idle", "greeting", "stretching", "reading", "homework", "playing", "tea", "napping", "working", "happy"):
            assistant.mood = mood
            assistant.rig.step(0, mood)
            looks[mood] = bytes(assistant.grab().toImage().constBits())
        self.assertEqual(len(set(looks.values())), len(looks))

    def bored(self, assistant):
        assistant.next_play = 0
        assistant.mood = "idle"

    def test_left_alone_she_finds_something_to_do_then_looks_up_when_touched(self):
        assistant = self.assistant()
        self.bored(assistant)
        assistant.maybe_play()
        self.assertIn(assistant.mood, ui.PASTIMES)
        assistant.enterEvent(None)
        self.assertEqual(assistant.mood, "idle")
        self.assertGreater(assistant.next_play, assistant.frame)

    def test_she_does_not_repeat_herself(self):
        assistant = self.assistant()
        for _ in range(20):
            before = assistant.last_pastime
            self.bored(assistant)
            assistant.maybe_play()
            self.assertNotEqual(assistant.last_pastime, before)

    def test_after_a_long_while_she_dozes_and_wakes_with_a_stretch(self):
        assistant = self.assistant()
        self.bored(assistant)
        assistant.frame = int(ui.NAP_AFTER * 1000 / ui.FRAME_MS) + 10
        assistant.maybe_play()
        self.assertEqual(assistant.mood, "napping")
        assistant.touched()
        self.assertEqual(assistant.mood, "stretching")

    def test_she_keeps_still_when_asked(self):
        assistant = self.assistant()
        for setup in (lambda: setattr(assistant, "plays", False), lambda: (setattr(assistant, "plays", True), setattr(assistant, "reduced_motion", True))):
            setup()
            self.bored(assistant)
            assistant.maybe_play()
            self.assertEqual(assistant.mood, "idle")

    def test_a_chosen_picture_has_no_pastimes(self):
        assistant = self.assistant()
        self.window.set_assistant_image(self.picture("me.png"))
        self.bored(assistant)
        assistant.maybe_play()
        self.assertEqual(assistant.mood, "idle")

    def test_the_menu_saves_her_settings(self):
        assistant = self.assistant()
        with patch.object(self.window, "save_setting") as saved:
            self.window.toggle_assistant_play()
            self.window.toggle_assistant_pinned()
        self.assertFalse(assistant.plays)
        self.assertTrue(assistant.pinned)
        self.assertEqual(saved.call_args_list[0][0], ("assistant_play", False))
        self.assertEqual(saved.call_args_list[1][0], ("assistant_pinned", True))

    def test_a_hidden_roo_says_nothing(self):
        assistant = self.assistant()
        assistant.hide()
        assistant.say("hello")
        self.assertFalse(assistant.bubble.isVisible())

    def test_a_short_message_stays_on_one_line_and_a_long_one_wraps(self):
        bubble = self.assistant().bubble
        anchor = QRectF(300, 300, 96, 115)
        bubble.say("已把 1 份文件整理到“合同”", anchor)
        line = bubble.text.fontMetrics().lineSpacing()
        self.assertLess(bubble.text.height(), line * 2)
        bubble.say("已把 12 份文件整理到“合同”、“发票”、“保险”，原文件没有移动。" * 2, anchor)
        self.assertGreaterEqual(bubble.text.height(), line * 2)
        self.assertLessEqual(bubble.text.width(), BUBBLE_WIDTH)
        bubble.hide()

    def test_the_moods_look_different(self):
        assistant = self.assistant()
        assistant.reduced_motion = True
        looks = set()
        for mood in ("idle", "excited", "happy"):
            assistant.set_mood(mood)
            looks.add(bytes(assistant.grab().toImage().constBits()))
        self.assertEqual(len(looks), 3)

    def test_it_never_takes_the_keyboard(self):
        assistant = self.assistant()
        self.assertTrue(assistant.windowFlags() & ui.Qt.WindowDoesNotAcceptFocus)
        self.assertTrue(assistant.windowFlags() & ui.Qt.WindowStaysOnTopHint)
        self.assertTrue(assistant.testAttribute(ui.Qt.WA_ShowWithoutActivating))
        self.assertTrue(assistant.bubble.windowFlags() & ui.Qt.WindowDoesNotAcceptFocus)

    def test_reduced_motion_keeps_it_still(self):
        assistant = self.assistant()
        assistant.reduced_motion = True
        assistant.set_mood("excited")
        assistant.frame = 13
        self.assertEqual(assistant.pose(), (0.0, 1.0, 0.0))
        looking = [assistant.rig.step(1 / 30, "excited", (1, 1), True) for _ in range(5)][-1]
        self.assertEqual((looking.eye_x, looking.breath, looking.swing), (0, 0, 0))
        self.assertEqual(assistant.gaze(), (0.0, 0.0))
        assistant.reduced_motion = False
        self.assertNotEqual(assistant.pose(), (0.0, 1.0, 0.0))

    def test_a_chosen_picture_replaces_roo_and_stays_after_the_original_goes(self):
        assistant = self.assistant()
        source = self.picture("me.png")
        self.window.set_assistant_image(source)
        self.assertTrue(assistant.custom)
        source.unlink()
        again = ui.Assistant(Path(self.scratch) / "assistant")
        self.addCleanup(again.close)
        self.assertTrue(again.custom)
        centre = assistant.grab().toImage().pixelColor(assistant.width() // 2, assistant.height() // 2)
        self.assertEqual(centre.getRgb()[:3], (0xCC, 0x33, 0x44))

    def test_every_mood_of_a_chosen_picture_moves_too(self):
        assistant = self.assistant()
        self.window.set_assistant_image(self.picture("me.png"))
        assistant.set_mood("working")
        assistant.frame = 5
        self.assertNotEqual(assistant.pose(), (0.0, 1.0, 0.0))
        self.assertIsNotNone(assistant.picture())

    def test_each_mood_can_have_its_own_picture(self):
        assistant = self.assistant()
        folder = Path(self.scratch) / "set"
        folder.mkdir()
        for name, color in (("idle", "#112233"), ("Happy", "#445566"), ("other", "#778899")):
            image = QImage(32, 32, QImage.Format_ARGB32)
            image.fill(QColor(color))
            image.save(str(folder / f"{name}.png"))
        self.window.set_assistant_images(folder)
        self.assertEqual(set(assistant.pictures), {"idle", "happy"})
        assistant.mood = "happy"
        self.assertEqual(assistant.picture().toImage().pixelColor(1, 1).name(), "#445566")
        assistant.mood = "working"
        self.assertEqual(assistant.picture().toImage().pixelColor(1, 1).name(), "#112233")

    def test_an_animated_picture_plays(self):
        assistant = self.assistant()
        path = Path(self.scratch) / "moving.gif"
        Image = pillow()
        if Image is None:
            self.skipTest("Writing a GIF needs Pillow.")
        Image.new("RGB", (32, 32), "red").save(path, save_all=True, append_images=[Image.new("RGB", (32, 32), "blue")], duration=50, loop=0)
        self.window.set_assistant_image(path)
        self.assertIn("idle", assistant.movies)
        self.wait_until(lambda: not assistant.picture().isNull())
        # A playing GIF must not stop the picture being changed on Windows.
        self.window.set_assistant_image(self.picture("still.png"))
        self.assertEqual(assistant.movies, {})
        self.assertEqual(sorted(path.name for path in (Path(self.scratch) / "assistant").iterdir()), ["idle.png"])

    def test_a_picture_that_cannot_be_used_changes_nothing(self):
        assistant = self.assistant()
        self.window.set_assistant_image(self.picture("good.png"))
        broken = Path(self.scratch) / "broken.png"
        broken.write_text("not a picture", encoding="utf-8")
        with patch.object(assistant, "say") as said:
            self.window.set_assistant_image(broken)
        self.assertIn("20 MB", said.call_args[0][0])
        self.assertTrue(assistant.custom)
        hollow = Path(self.scratch) / "hollow.gif"
        hollow.write_bytes(b"GIF89a")
        self.assertFalse(ui.install_image(hollow, Path(self.scratch) / "other"))
        with patch.object(ui.Path, "stat", return_value=SimpleNamespace(st_size=30 * 1024 * 1024, st_mode=0o100644)):
            self.assertFalse(ui.install_image(self.picture("huge.png"), Path(self.scratch) / "other"))

    def test_the_picture_in_use_can_be_chosen_again(self):
        assistant = self.assistant()
        self.window.set_assistant_image(self.picture("me.png"))
        self.window.set_assistant_image(Path(self.scratch) / "assistant" / "idle.png")
        self.assertTrue(assistant.custom)

    def test_going_back_to_roo_removes_the_copy(self):
        assistant = self.assistant()
        self.window.set_assistant_image(self.picture("me.png"))
        self.window.reset_assistant()
        self.assertFalse(assistant.custom)
        self.assertFalse((Path(self.scratch) / "assistant").exists())

    def test_the_menu_offers_the_way_back_only_for_a_chosen_picture(self):
        assistant = self.assistant()
        back = t("Back to {name}", name=t("Roo"))
        self.assertNotIn(back, [action.text() for action in self.window.assistant_menu().actions()])
        self.window.set_assistant_image(self.picture("me.png"))
        self.assertIn(back, [action.text() for action in self.window.assistant_menu().actions()])

    def test_a_click_searches_and_a_drag_moves(self):
        assistant = self.assistant()
        clicked, moved = MagicMock(), MagicMock()
        assistant.clicked.connect(clicked)
        assistant.moved.connect(moved)
        QTest.mouseClick(assistant, ui.Qt.LeftButton, pos=QPoint(10, 10))
        clicked.assert_called_once()
        assistant.mousePressEvent(SimpleNamespace(button=lambda: ui.Qt.LeftButton, globalPosition=lambda: QPointF(100, 100)))
        assistant.mouseMoveEvent(SimpleNamespace(buttons=lambda: ui.Qt.LeftButton, globalPosition=lambda: QPointF(160, 150)))
        assistant.mouseReleaseEvent(SimpleNamespace(button=lambda: ui.Qt.LeftButton))
        moved.assert_called_once()
        clicked.assert_called_once()

    def test_it_stays_away_during_games_and_presentations(self):
        assistant = self.assistant()
        with patch.object(ui, "user_is_busy", return_value=False):
            self.window.place_assistant()
        self.assertTrue(assistant.isVisible())
        with patch.object(ui, "user_is_busy", return_value=True):
            self.window.place_assistant()
        self.assertFalse(assistant.isVisible())

    def test_it_can_be_turned_off(self):
        assistant = self.assistant()
        with patch.object(ui, "user_is_busy", return_value=False):
            self.window.show_assistant(False)
            self.assertFalse(assistant.isVisible())
            self.window.show_assistant(True)
            self.assertTrue(assistant.isVisible())

    def test_it_comes_back_where_it_was_left_unless_that_screen_has_gone(self):
        assistant = self.assistant()
        area = self.app.primaryScreen().availableGeometry()
        self.window.config["assistant_at"] = [area.left() + 40, area.top() + 50]
        assistant.hide()
        with patch.object(ui, "user_is_busy", return_value=False):
            self.window.place_assistant()
            self.assertEqual((assistant.x(), assistant.y()), (area.left() + 40, area.top() + 50))
            assistant.hide()
            self.window.config["assistant_at"] = [area.right() + 5000, 10]
            self.window.place_assistant()
        self.assertTrue(area.contains(assistant.pos()))

    def test_a_dropped_file_is_read_and_filed_by_subject(self):
        source = self.use_library({})
        (source / "合同.txt").write_text("甲方与乙方", encoding="utf-8")
        (source / "杂记.txt").write_text("租赁期限两年，租赁押金一月", encoding="utf-8")
        (source / "没拖.txt").write_text("合同", encoding="utf-8")
        self.window.core.remove_source(self.window.core.sources()[0]["id"])
        assistant = self.assistant()
        self.drop_on(assistant, source / "合同.txt", source / "杂记.txt")
        self.assertEqual(assistant.mood, "working")
        self.wait_until(lambda: not self.window.sorting)
        collections = {row["name"]: row["count"] for row in self.window.core.collections()}
        self.assertEqual(collections, {"合同": 1, "租房": 1})
        self.assertEqual(self.window.core.search_page("没拖")["total"], 0)
        self.assertEqual(assistant.mood, "happy")
        self.assertIn("原文件没有移动", assistant.bubble.text.text())
        self.assertIn("“合同”", assistant.bubble.text.text())
        self.assertEqual((source / "合同.txt").read_text(encoding="utf-8"), "甲方与乙方")

    def test_a_file_with_no_clear_subject_is_filed_by_type(self):
        source = self.use_library({})
        (source / "x.txt").write_text("没什么特别的", encoding="utf-8")
        self.window.core.remove_source(self.window.core.sources()[0]["id"])
        self.drop_on(self.assistant(), source / "x.txt")
        self.wait_until(lambda: not self.window.sorting)
        self.assertEqual([row["name"] for row in self.window.core.collections()], [t(ui.file_group(".txt"))])

    def test_filing_into_a_collection_that_already_exists(self):
        source = self.use_library({"合同一.txt": "甲", "合同二.txt": "乙"})
        existing = self.window.core.create_collection("合同")
        assistant = self.assistant()
        self.drop_on(assistant, source)
        self.wait_until(lambda: not self.window.sorting)
        self.assertEqual([(row["id"], row["count"]) for row in self.window.core.collections()], [(existing, 2)])

    def test_files_that_cannot_be_taken_are_reported_and_roo_is_not_left_working(self):
        self.use_library({})
        assistant = self.assistant()
        inside = self.window.core.data_dir / "内部.txt"
        inside.write_text("不该收", encoding="utf-8")
        self.drop_on(assistant, inside)
        self.wait_until(lambda: not self.window.sorting)
        self.assertEqual(assistant.mood, "idle")
        self.assertTrue(assistant.bubble.text.text())
        self.assertEqual(self.window.core.collections(), [])

    def test_a_failure_while_sorting_never_leaves_scans_blocked(self):
        source = self.use_library({"a.txt": "甲"})
        assistant = self.assistant()
        with patch.object(self.window.core, "files_at", side_effect=sqlite3.OperationalError("disk I/O error")):
            self.drop_on(assistant, source / "a.txt")
            self.wait_until(lambda: not self.window.sorting)
        self.assertEqual(assistant.mood, "idle")

    def test_scans_wait_while_files_are_being_sorted(self):
        self.use_library({"a.txt": "甲"})
        self.window.sorting = [MagicMock()]
        self.window.start_scan(automatic=True)
        self.assertFalse(self.window.scanning)
        self.window.changed_paths = {"x": {"a.txt"}}
        self.window.scan_changes()
        self.assertTrue(self.window.change_timer.isActive())
        self.window.change_timer.stop()
        self.assertEqual(self.window.changed_paths, {"x": {"a.txt"}})
        self.window.changed_paths = {}

    def test_a_drop_during_a_scan_waits_its_turn(self):
        source = self.use_library({"合同.txt": "甲"})
        self.window.core._scan_lock.acquire()
        self.addCleanup(lambda: self.window.core._scan_lock.locked() and self.window.core._scan_lock.release())
        assistant = self.assistant()
        self.drop_on(assistant, source / "合同.txt")
        time.sleep(.5)
        self.app.processEvents()
        self.assertTrue(self.window.sorting)
        self.window.core._scan_lock.release()
        self.wait_until(lambda: not self.window.sorting)
        self.assertEqual([row["name"] for row in self.window.core.collections()], ["合同"])

    def test_words_in_a_dropped_scan_decide_where_it_goes(self):
        source = self.use_library({})
        self.picture("scan.png").rename(source / "scan.png")
        self.window.core.remove_source(self.window.core.sources()[0]["id"])
        with patch.object(ui.ocr, "available", return_value=True), patch.object(ui.ocr, "read_file", return_value="增值税发票 发票号码"), \
                patch.object(self.window, "start_reading"):
            self.drop_on(self.assistant(), source / "scan.png")
            self.wait_until(lambda: not self.window.sorting)
        self.assertEqual([row["name"] for row in self.window.core.collections()], ["发票"])

    def linkable(self, *names):
        source = self.use_library({name: f"text of {name}" for name in names})
        self.window.core.scan_all()
        self.window.refresh()
        rows = {row["name"]: row["id"] for row in self.window.core.search("", limit=50)}
        return source, [rows[name] for name in names]

    def forget_moving(self):
        """Saved settings outlive the window, and the next test must not start out moving files."""
        self.window.config.pop("drop_action", None)
        self.window.config.pop("library_folder", None)
        self.window.save_config()

    def moving_to_library(self):
        self.addCleanup(self.forget_moving)
        self.window.config["drop_action"] = "move"
        self.window.config["library_folder"] = str(Path(self.scratch) / "library")

    def test_dropped_files_keep_a_copy_unless_moving_was_chosen(self):
        source = self.use_library({"kept.txt": "a quote for the roof"})
        loose = Path(self.scratch) / "desk" / "loose.txt"
        loose.parent.mkdir()
        loose.write_text("增值税发票", encoding="utf-8")
        with patch.object(self.window, "start_reading"):
            self.drop_on(self.assistant(), loose)
            self.wait_until(lambda: not self.window.sorting)
        self.assertTrue(loose.is_file())
        self.assertTrue(Path(self.window.core.files_at([loose])[0]["archive_path"]).is_file())

    def test_moving_sorts_the_file_into_a_subject_folder_and_it_can_go_back(self):
        self.use_library({"kept.txt": "words"})
        self.moving_to_library()
        loose = Path(self.scratch) / "desk" / "loose.txt"
        loose.parent.mkdir()
        loose.write_text("增值税发票 发票号码", encoding="utf-8")
        with patch.object(self.window, "start_reading"):
            self.drop_on(self.assistant(), loose)
            self.wait_until(lambda: not self.window.sorting)
        self.assertFalse(loose.exists())
        moved = Path(self.scratch) / "library" / "发票" / "loose.txt"
        self.assertTrue(moved.is_file())
        row = self.window.core.files_at([moved])[0]
        self.window.select_file(row["id"])
        self.assertFalse(self.window.put_back_button.isHidden())
        self.window.put_back()
        self.assertTrue(loose.is_file())
        self.assertFalse(moved.exists())
        self.assertTrue(self.window.put_back_button.isHidden())

    def test_a_dropped_folder_keeps_its_shape_when_moved(self):
        self.use_library({"kept.txt": "words"})
        self.moving_to_library()
        folder = Path(self.scratch) / "desk" / "project"
        (folder / "inner").mkdir(parents=True)
        (folder / "inner" / "plan.txt").write_text("plan", encoding="utf-8")
        with patch.object(self.window, "start_reading"):
            self.drop_on(self.assistant(), folder)
            self.wait_until(lambda: not self.window.sorting)
        self.assertTrue((Path(self.scratch) / "library" / "project" / "inner" / "plan.txt").is_file())
        self.assertFalse(folder.exists())

    def test_the_setting_chooses_copy_or_move_and_says_what_will_happen(self):
        self.addCleanup(self.forget_moving)
        holder = ui.QWidget()
        self.addCleanup(holder.deleteLater)
        self.window.add_drop_settings(ui.QVBoxLayout(holder))
        choice = holder.findChild(ui.QComboBox)
        self.assertEqual(choice.currentData(), "copy")
        choice.setCurrentIndex(choice.findData("move"))
        self.assertEqual(self.window.config["drop_action"], "move")
        words = " ".join(item.text() for item in holder.findChildren(ui.QLabel))
        self.assertIn(str(self.window.library_folder()), words)
        self.assertNotIn("<", words)  # Labels here show plain text, so markup would appear as it is.

    def test_the_map_shows_linked_files_and_a_drag_ties_them(self):
        _, (one, two, three) = self.linkable("one.txt", "two.txt", "three.txt")
        self.window.select_place("links")
        self.assertIs(self.window.pages.currentWidget(), self.window.links_page)
        self.assertIs(self.window.map_pages.currentIndex(), 1)
        self.window.tie([one, two])
        self.assertEqual(self.window.map_pages.currentIndex(), 0)
        self.assertEqual(set(self.window.map.nodes), {one, two})
        self.window.map.tied.emit(two, three) if three in self.window.map.nodes else self.window.tie([two, three])
        self.assertEqual(len(self.window.core.bundle(one)), 3)
        self.window.select_file(three)
        self.assertEqual(self.window.map.focused, three)
        self.assertEqual(self.window.map.members, {one, two, three})

    def test_typing_a_search_leaves_the_map_for_the_results(self):
        self.linkable("one.txt", "two.txt")
        self.window.select_place("links")
        self.window.search.setText("one")
        self.window.refresh_results()
        self.assertEqual(self.window.place, "all")
        self.assertFalse(self.window.filter_row.isHidden())

    def test_details_list_linked_files_and_offer_the_whole_set(self):
        _, (one, two, three) = self.linkable("one.txt", "two.txt", "three.txt")
        self.window.tie([one, two])
        self.window.tie([two, three])
        self.window.select_file(one)
        self.assertTrue(self.window.linked.count() >= 3)
        self.assertTrue(self.window.copy_rows(self.window.core.bundle(one)))
        self.assertEqual(len(self.app.clipboard().mimeData().urls()), 3)

    def test_several_files_can_be_picked_and_linked_at_once(self):
        _, ids = self.linkable("one.txt", "two.txt", "three.txt")
        self.window.set_view("list")
        self.window.list.selectAll()
        self.assertEqual(len(self.window.selected_rows()), 3)
        rows = self.window.selected_rows()
        self.assertEqual(self.window.many_menu(rows).actions()[0].text(), t("Link these {files}", files=ui.file_count(3)))
        self.window.link_selection()
        self.assertEqual(len(self.window.core.bundle(ids[0])), 3)

    def test_the_picker_keeps_ticks_across_searches_and_leaves_out_what_is_linked(self):
        _, (one, two, three, four) = self.linkable("one.txt", "two.txt", "three.txt", "four.txt")
        self.window.tie([one, two])
        picker = ui.LinkPicker(self.window, one)
        self.addCleanup(picker.deleteLater)
        self.assertFalse(any("<" in item.text() for item in picker.findChildren(ui.QLabel)))
        names = lambda: [picker.results.item(index).data(ui.Qt.UserRole)[1] for index in range(picker.results.count())]
        self.assertNotIn("two.txt", names())
        picker.box.setText("three")
        picker.clicked(picker.results.item(0))
        picker.box.setText("four")
        picker.tick_first()
        picker.box.setText("")
        self.assertEqual(picker.ids(), [one, three, four])
        self.assertTrue(picker.confirm.isEnabled())

    def board(self, *names):
        _, ids = self.linkable(*names)
        self.window.tie(ids)
        self.window.select_place("links")
        board = self.window.map
        board.resize(1200, 800)
        board.lay_out()
        board.fit()
        return board, ids

    def spot_of(self, board, file_id):
        return board.nodes[file_id].rect.center() * board.zoom + board.pan

    def mouse(self, board, kind, spot, button=ui.Qt.LeftButton):
        handler = {QEvent.MouseButtonPress: board.mousePressEvent, QEvent.MouseMove: board.mouseMoveEvent, QEvent.MouseButtonRelease: board.mouseReleaseEvent}[kind]
        handler(QMouseEvent(kind, spot, spot, button, button if kind != QEvent.MouseButtonRelease else ui.Qt.NoButton, ui.Qt.NoModifier))

    def test_every_card_on_the_board_has_its_own_place(self):
        board, ids = self.board("one.txt", "two.txt", "three.txt", "four.txt")
        rects = [board.nodes[file_id].rect for file_id in ids]
        for index, rect in enumerate(rects):
            self.assertFalse(any(rect.intersects(other) for other in rects[index + 1:]))
            self.assertTrue(board.frames[0].rect.contains(rect))

    def test_a_click_shows_the_set_a_card_belongs_to(self):
        board, ids = self.board("one.txt", "two.txt", "three.txt")
        spot = self.spot_of(board, ids[1])
        self.mouse(board, QEvent.MouseButtonPress, spot)
        self.mouse(board, QEvent.MouseButtonRelease, spot)
        self.assertEqual(self.window.selected_id, ids[1])
        self.assertEqual(board.focused, ids[1])
        self.assertEqual(board.members, set(ids))

    def test_dropping_a_card_on_another_links_them_and_it_goes_back_to_its_place(self):
        _, (one, two, three, four) = self.linkable("one.txt", "two.txt", "three.txt", "four.txt")
        self.window.tie([one, two])
        self.window.tie([three, four])
        self.window.select_place("links")
        board = self.window.map
        board.resize(1200, 800)
        board.lay_out()
        board.fit()
        before = board.nodes[one].rect
        self.mouse(board, QEvent.MouseButtonPress, self.spot_of(board, one))
        self.mouse(board, QEvent.MouseMove, self.spot_of(board, three))
        self.assertEqual(board.target, three)
        self.mouse(board, QEvent.MouseButtonRelease, self.spot_of(board, three))
        self.assertEqual(len(self.window.core.bundle(one)), 4)
        self.assertEqual(len(self.window.map.frames), 1)
        self.assertIsNone(board.dragging)

    def test_a_card_dropped_nowhere_changes_nothing(self):
        board, ids = self.board("one.txt", "two.txt")
        self.mouse(board, QEvent.MouseButtonPress, self.spot_of(board, ids[0]))
        self.mouse(board, QEvent.MouseMove, QPointF(5, 5))
        self.mouse(board, QEvent.MouseButtonRelease, QPointF(5, 5))
        self.assertEqual(self.window.core.link_graph()["edges"], [(min(ids), max(ids))])

    def test_a_suggestion_links_with_one_click(self):
        source = self.use_library({"plan2026.docx": "a", "plan2026.pdf": "b", "other.txt": "c"})
        self.window.core.scan_all()
        rows = {row["name"]: row["id"] for row in self.window.core.search("", limit=9)}
        self.window.refresh()
        self.window.tie([rows["plan2026.docx"], rows["other.txt"]])
        self.window.select_place("links")
        board = self.window.map
        board.resize(1200, 800)
        board.lay_out()
        board.fit()
        self.window.select_file(rows["plan2026.docx"])
        self.assertEqual([offer["name"] for offer in board.offers], ["plan2026.pdf"])
        self.assertEqual(board.offers[0]["why_text"], t("Same name"))
        spot = board.offer_rects()[0][1].center()
        self.mouse(board, QEvent.MouseButtonPress, spot)
        self.mouse(board, QEvent.MouseButtonRelease, spot)
        self.assertEqual(len(self.window.core.bundle(rows["plan2026.pdf"])), 3)

    def test_a_board_at_rest_needs_no_timer(self):
        board, _ = self.board("one.txt", "two.txt")
        board.focus(None)
        self.assertFalse(board.timer.isActive())

    def test_a_map_with_no_ties_needs_no_timer(self):
        _, (one, two) = self.linkable("one.txt", "two.txt")
        self.window.tie([one, two])
        self.window.select_place("links")
        self.window.map.focus(None)
        self.assertFalse(self.window.map.timer.isActive())

    def test_turning_off_connected_tools_also_turns_off_changes(self):
        holder = ui.QWidget()
        self.addCleanup(holder.deleteLater)
        layout = ui.QVBoxLayout(holder)
        self.window.add_connected_tools(layout)
        read, write = holder.findChildren(ui.QCheckBox)
        self.assertFalse(self.window.config.get("mcp", False))
        self.assertFalse(read.isChecked())
        self.assertFalse(write.isEnabled())
        read.setChecked(True)
        write.setChecked(True)
        self.assertTrue(self.window.config["mcp"] and self.window.config["mcp_write"])
        read.setChecked(False)
        self.assertFalse(self.window.config["mcp"])
        self.assertFalse(self.window.config["mcp_write"])
        self.assertFalse(write.isEnabled())

    def test_the_copied_setup_starts_this_archive(self):
        server = json.loads(self.window.mcp_setup())["mcpServers"]["roostfile"]
        self.assertTrue(server["args"][0].endswith("mcp_server.py"))
        self.assertTrue(Path(server["args"][0]).is_file())
        self.assertEqual(server["args"][1:], ["--data", str(self.window.data_root)])

    def test_a_handed_over_file_shows_in_sources_for_what_it_is(self):
        source = self.use_library({"a.txt": "甲"})
        self.window.core.remove_source(self.window.core.sources()[0]["id"])
        self.window.core.adopt([source / "a.txt"])
        shown = {}
        def inspect(dialog):
            shown["text"] = [widget.text() for widget in dialog.findChildren(ui.QLabel)]
        with patch.object(ui.QDialog, "exec", inspect):
            self.window.show_sources()
        self.assertIn(t("Only {files} from this folder", files=ui.file_count(1)), shown["text"])

    def test_a_handed_over_folder_is_not_watched_for_everything_else(self):
        source = self.use_library({"a.txt": "甲"})
        self.window.core.remove_source(self.window.core.sources()[0]["id"])
        self.window.core.adopt([source / "a.txt"])
        self.window.folders = MagicMock()
        self.addCleanup(setattr, self.window, "folders", None)
        self.window.watch_sources()
        self.assertEqual(list(self.window.folders.watch.call_args[0][0]), [])

    def test_a_first_scan_shows_reading_and_then_files_as_they_arrive(self):
        self.use_library({"预算.txt": "第四季度"})
        self.window.scanning = True
        self.window.refresh_results()
        self.assertEqual(self.window.pages.currentWidget(), self.window.empty)
        self.assertEqual(self.window.empty_title.text(), t("Reading your files"))
        self.assertFalse(self.window.empty_action.isVisible())
        self.window.core.scan_all()
        self.window.scan_progress({"scanned": 1, "name": "预算.txt"})
        self.window.scanning = False
        self.assertEqual([row["name"] for row in self.window.rows], ["预算.txt"])
        self.assertNotEqual(self.window.pages.currentWidget(), self.window.empty)

    def test_a_full_page_is_left_alone_during_a_scan(self):
        self.window.search_args = dict(self.window.search_args)
        self.window.rows = [{"id": number} for number in range(ui.PAGE_SIZE)]
        with patch.object(self.window, "refresh_results") as refresh:
            self.window.scan_progress({"scanned": 500, "name": "x"})
            refresh.assert_not_called()

    def test_files_are_dragged_as_copies_and_never_moved(self):
        row = self.window.rows[0]
        model = self.window.model
        index = model.index(0)
        self.assertEqual(model.supportedDragActions(), ui.Qt.CopyAction)
        self.assertTrue(model.flags(index) & ui.Qt.ItemIsDragEnabled)
        self.assertEqual(model.mimeData([index]).urls()[0].toLocalFile(), str(Path(row["path"])).replace("\\", "/"))
        for view in (self.window.grid, self.window.list):
            self.assertTrue(view.dragEnabled())
            self.assertEqual(view.defaultDropAction(), ui.Qt.CopyAction)

    def test_ctrl_c_puts_the_file_on_the_clipboard(self):
        self.window.set_view("list")
        self.window.select_file(self.window.rows[0]["id"])
        # A file left on the offscreen clipboard crashes PySide at exit on Linux, in any program.
        self.addCleanup(self.app.clipboard().clear)
        QTest.keyClick(self.window.list, ui.Qt.Key_C, ui.Qt.ControlModifier)
        urls = self.app.clipboard().mimeData().urls()
        self.assertEqual(urls[0].toLocalFile(), str(Path(self.window.rows[0]["path"])).replace("\\", "/"))

    def test_a_cloud_placeholder_is_not_offered_to_other_apps(self):
        row = dict(self.window.rows[0], extraction_status="cloud_placeholder", archive_path="")
        self.assertIsNone(ui.file_mime([row]))

    def test_a_search_with_no_match_falls_back_to_a_related_word_and_says_so(self):
        self.use_library({"服务合同.txt": "甲方 乙方"})
        self.window.core.scan_all()
        for word in ("合约", "contract"):
            self.window.search.setText(word)
            self.window.refresh_results()
            self.assertEqual([row["name"] for row in self.window.rows], ["服务合同.txt"])
            self.assertEqual(self.window.replaced_query, "合同")
            self.assertTrue(self.window.chip_row.isVisible())

    def test_an_exact_match_is_never_replaced(self):
        self.use_library({"服务合同.txt": "甲方", "保密协议.txt": "乙方"})
        self.window.core.scan_all()
        self.window.search.setText("协议")
        self.window.refresh_results()
        self.assertIsNone(self.window.replaced_query)
        self.assertEqual([row["name"] for row in self.window.rows], ["保密协议.txt"])

    def test_nothing_found_is_specific(self):
        self.use_library({"预算.txt": "第四季度", "会议.txt": "周一"})
        self.window.core.scan_all()
        self.window.search.setText("海边")
        with patch.object(ui.ocr, "available", return_value=False):
            self.window.refresh_results()
        self.assertEqual(self.window.empty_title.text(), t('Nothing found for "{query}"', query="海边"))
        self.assertEqual(self.window.empty_hint.text(), t("Photos and scanned pages are found by name or date, not by what is in them."))
        self.assertEqual(self.window.empty_action.text(), t("Clear search"))
        self.window.search.setText("预算 海边")
        self.window.refresh_results()
        self.assertTrue(self.window.suggestion_row.isVisible())
        self.assertEqual(self.window.suggestions.count(), 3)

    def test_clearing_filters_keeps_the_search(self):
        self.use_library({"预算.txt": "第四季度"})
        self.window.core.scan_all()
        self.window.refresh_stats()
        self.window.place = "favorites"
        self.window.search.setText("预算")
        self.window.refresh_results()
        self.assertEqual(self.window.empty_hint.text(), t("Without these filters: {files}", files=ui.file_count(1)))
        self.window.empty_clicked()
        self.assertEqual(self.window.search.text(), "预算")
        self.assertEqual(len(self.window.rows), 1)

    def test_stored_messages_are_shown_in_the_interface_language(self):
        self.use_library({"scan.bin": "x"})
        self.window.core.scan_all()
        self.window.search.setText("scan")
        self.window.refresh_results()
        self.assertEqual(self.window.rows[0]["snippet"], t(self.window.rows[0]["summary"]))
        self.assertNotEqual(self.window.rows[0]["snippet"], self.window.rows[0]["summary"])

    def test_a_new_search_closes_a_preview_it_no_longer_shows(self):
        self.window.select_file(self.window.rows[0]["id"])
        other = self.window.rows[0]["id"]
        with patch.object(self.window, "load_page", return_value={"rows": [dict(self.window.rows[1])], "total": 500, "tokens": []}):
            self.window.selected_id = other
            self.window.search.setText("something else")
            self.window.refresh_results()
        self.assertFalse(self.window.details.isVisible())

    def test_closing_during_a_scan_stays_quiet(self):
        self.window.closing = True
        self.window.signals.failed.disconnect()
        hook = patch.object(threading, "excepthook")
        with hook as called, patch.object(self.window.core, "scan_all", side_effect=RuntimeError("Signal source has been deleted")):
            self.window.start_scan()
            self.window.scan_thread.join(5)
        called.assert_not_called()
        self.window.scanning = False

    def open_quick(self):
        quick = StayingQuickSearch(self.window)
        quick.setAttribute(ui.Qt.WA_DontShowOnScreen, True)
        self.addCleanup(quick.deleteLater)
        quick.toggle()
        self.app.processEvents()
        return quick

    def type_quick(self, quick, text):
        quick.search.setText(text)
        quick.refresh()

    def test_quick_search_opens_on_recent_files_and_toggles_away(self):
        quick = self.open_quick()
        self.assertTrue(quick.isVisible())
        self.assertEqual(quick.model.rows[0]["id"], self.window.core.search_page(limit=1)["rows"][0]["id"])
        self.assertLessEqual(quick.list.height(), quick.ROWS * quick.ROW_HEIGHT)
        quick.toggle()
        self.assertFalse(quick.isVisible())

    def test_quick_search_opens_the_first_match_with_enter(self):
        quick = self.open_quick()
        self.type_quick(quick, "认知负荷")
        with patch.object(self.window, "open_file") as opened:
            QTest.keyClick(quick.search, ui.Qt.Key_Return)
        opened.assert_called_once_with(quick.model.rows[0]["id"])
        self.assertFalse(quick.isVisible())

    def test_quick_search_arrows_move_the_choice_and_typing_stays_in_the_box(self):
        quick = self.open_quick()
        QTest.keyClick(quick.search, ui.Qt.Key_Down)
        self.assertEqual(quick.list.currentIndex().row(), 1)
        QTest.keyClick(quick.search, ui.Qt.Key_Up)
        QTest.keyClick(quick.search, ui.Qt.Key_Up)
        self.assertEqual(quick.list.currentIndex().row(), 0)
        self.assertEqual(quick.list.focusPolicy(), ui.Qt.NoFocus)

    def test_quick_search_copies_the_file_and_gets_out_of_the_way(self):
        self.addCleanup(self.app.clipboard().clear)
        quick = self.open_quick()
        expected = quick.model.rows[0]["path"]
        QTest.keyClick(quick.search, ui.Qt.Key_C, ui.Qt.ControlModifier)
        self.assertFalse(quick.isVisible())
        self.assertEqual(self.app.clipboard().mimeData().urls()[0].toLocalFile(), str(Path(expected)).replace("\\", "/"))

    def test_ctrl_c_on_selected_text_still_copies_the_text(self):
        quick = self.open_quick()
        self.type_quick(quick, "光线")
        quick.search.selectAll()
        with patch.object(self.window, "copy_file") as copied:
            QTest.keyClick(quick.search, ui.Qt.Key_C, ui.Qt.ControlModifier)
        copied.assert_not_called()
        self.assertTrue(quick.isVisible())

    def test_quick_search_shows_in_folder_with_ctrl_enter(self):
        quick = self.open_quick()
        with patch.object(self.window, "reveal_file") as revealed:
            QTest.keyClick(quick.search, ui.Qt.Key_Return, ui.Qt.ControlModifier)
        revealed.assert_called_once_with(quick.model.rows[0]["id"])

    def test_quick_search_closes_with_escape(self):
        quick = self.open_quick()
        QTest.keyClick(quick.search, ui.Qt.Key_Escape)
        self.assertFalse(quick.isVisible())

    def test_quick_search_closes_when_another_window_takes_over(self):
        quick = ui.QuickSearch(self.window)
        quick.setAttribute(ui.Qt.WA_DontShowOnScreen, True)
        self.addCleanup(quick.deleteLater)
        quick.show()
        with patch.object(quick, "isActiveWindow", return_value=False):
            quick.changeEvent(ui.QEvent(ui.QEvent.ActivationChange))
        self.assertFalse(quick.isVisible())

    def test_leaving_quick_search_returns_to_the_window_in_use(self):
        quick = self.open_quick()
        quick.previous_window = "the window in use"
        with patch.object(ui, "give_keyboard_to") as given:
            quick.dismiss()
        given.assert_called_once_with("the window in use")

    def test_quick_search_tries_related_words_and_says_so(self):
        self.use_library({"服务合同.txt": "甲方"})
        self.window.core.scan_all()
        quick = self.open_quick()
        self.type_quick(quick, "合约")
        self.assertEqual([row["name"] for row in quick.model.rows], ["服务合同.txt"])
        self.assertIn("合同", quick.hint.text())

    def test_quick_search_with_no_match_hands_over_to_the_main_window(self):
        quick = self.open_quick()
        self.type_quick(quick, "完全不存在的词")
        self.assertFalse(quick.list.isVisible())
        with patch.object(self.window, "show_main") as main:
            QTest.keyClick(quick.search, ui.Qt.Key_Return)
        main.assert_called_once_with("完全不存在的词")

    def test_quick_search_drags_copies_too(self):
        quick = self.open_quick()
        self.assertTrue(quick.list.dragEnabled())
        self.assertEqual(quick.list.defaultDropAction(), ui.Qt.CopyAction)

    def test_closing_keeps_running_in_the_tray_and_says_so_once(self):
        self.window.tray = MagicMock()
        self.addCleanup(setattr, self.window, "tray", None)
        self.window.config["tray_tip_shown"] = False
        for shown in (1, 1):
            self.window.show()
            event = QCloseEvent()
            self.window.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertFalse(self.window.isVisible())
            self.assertEqual(self.window.tray.showMessage.call_count, shown)
        self.assertFalse(self.window.closing)

    def test_quit_and_signing_out_really_close(self):
        self.window.tray = MagicMock()
        self.addCleanup(setattr, self.window, "tray", None)
        self.window.session_ending(None)
        event = QCloseEvent()
        with patch.object(ui.QApplication, "quit"):
            self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertTrue(self.window.closing)

    def test_turning_off_the_tray_closes_normally(self):
        self.window.tray = MagicMock()
        self.addCleanup(setattr, self.window, "tray", None)
        self.window.config["keep_running"] = False
        self.addCleanup(self.window.config.pop, "keep_running")
        event = QCloseEvent()
        with patch.object(ui.QApplication, "quit") as quit_app:
            self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        quit_app.assert_called_once()

    def test_changed_paths_are_gathered_and_looked_at_together(self):
        self.window.folder_changed("D:/资料", ["a.txt"])
        self.window.folder_changed("D:/资料", ["b.txt"])
        self.app.processEvents()
        self.assertTrue(self.window.change_timer.isActive())
        with patch.object(self.window, "start_scan") as scan:
            self.window.scan_changes()
        scan.assert_called_once_with(automatic=True, only={"D:/资料": ["a.txt", "b.txt"]})
        self.assertEqual(self.window.changed_paths, {})
        self.window.change_timer.stop()

    def test_a_running_scan_makes_changes_wait_their_turn(self):
        self.window.note_change("D:/资料", ["a.txt"])
        self.window.scanning = True
        with patch.object(self.window, "start_scan") as scan:
            self.window.scan_changes()
        self.window.scanning = False
        scan.assert_not_called()
        self.assertTrue(self.window.change_timer.isActive())
        self.window.change_timer.stop()
        self.window.changed_paths = {}

    def test_too_many_changes_become_a_full_look_at_most_once_a_minute(self):
        for paths in (None, [str(number) for number in range(ui.MAX_CHANGED_PATHS + 1)]):
            self.window.changed_paths = {}
            self.window.note_change("D:/资料", paths)
            with patch.object(self.window, "auto_scan") as scan:
                self.window.last_scan_end = time.monotonic()
                self.window.scan_changes()
                scan.assert_not_called()
                self.assertGreater(self.window.change_timer.remainingTime(), ui.CHANGE_SETTLE_MS)
                self.window.last_scan_end = time.monotonic() - ui.MIN_SCAN_GAP_SECONDS - 1
                self.window.scan_changes()
                scan.assert_called_once()
        self.window.change_timer.stop()

    def test_a_flood_of_notices_stays_a_full_look(self):
        self.window.note_change("D:/资料", None)
        self.window.note_change("D:/资料", ["a.txt"])
        self.assertIsNone(self.window.changed_paths["D:/资料"])
        self.window.changed_paths = {}
        self.window.change_timer.stop()

    def test_folders_can_be_followed_down_and_back(self):
        source = self.use_library({})
        (source / "工作" / "合同").mkdir(parents=True)
        (source / "工作" / "合同" / "甲.txt").write_text("一", encoding="utf-8")
        (source / "工作" / "方案.txt").write_text("二", encoding="utf-8")
        self.window.core.scan_all()
        self.window.refresh_stats()
        top = next(place for place in self.window.nav_buttons if place.startswith("folder:"))
        self.window.select_place(top)
        titles = [self.window.folder_nav.itemAt(i).widget().text() for i in range(self.window.folder_nav.count())]
        self.assertEqual(titles, [t("Up one level"), "工作", "合同"])
        self.assertEqual(len(self.window.rows), 2)
        down = self.window.folder_nav.itemAt(2).widget()
        down.click()
        self.assertEqual([row["name"] for row in self.window.rows], ["甲.txt"])
        self.assertEqual(self.window.section_title.text(), "合同")
        self.window.folder_nav.itemAt(0).widget().click()
        self.assertEqual(self.window.place, top)
        self.window.folder_nav.itemAt(0).widget().click()
        self.assertEqual(self.window.place, "all")

    def test_a_right_click_offers_what_can_be_done_with_the_file(self):
        row = next(item for item in self.window.rows if not self.window.core.get_file(item["id"])["text"])
        menu = self.window.file_menu(row["id"])
        self.assertEqual(self.window.selected_id, row["id"])
        self.assertEqual([action.text() for action in menu.actions() if action.text()],
                         [t("Open"), t("Copy file"), t("Show in folder"), t("Copy path"), t("Favorite") if not row.get("favorite") else t("Favorited"), t("Tags"), t("Add to collection"), t("Link a file")])
        with patch.object(self.window, "open_file") as opened:
            menu.actions()[0].trigger()
        opened.assert_called_once_with(row["id"])

    def test_a_sheet_is_previewed_as_a_table_with_matches_marked(self):
        self.use_library({"预算.csv": "项目,金额\n物业费,1200\n水电费,300\n"})
        self.window.core.scan_all()
        self.window.search.setText("水电费")
        self.window.refresh_results()
        self.window.select_file(self.window.rows[0]["id"])
        table = self.window.preview_table
        self.assertTrue(table.isVisible())
        self.assertFalse(self.window.preview_text.isVisible())
        self.assertEqual((table.horizontalHeaderItem(1).text(), table.item(1, 1).text()), ("金额", "300"))
        self.assertEqual(table.item(1, 0).foreground().color().name().upper(), ui.ACCENT.upper())

    def test_a_document_goes_back_to_text_after_a_sheet(self):
        self.use_library({"预算.csv": "项目,金额\n物业费,1200\n", "备忘.txt": "内容"})
        self.window.core.scan_all()
        self.window.refresh_results()
        rows = {row["name"]: row["id"] for row in self.window.rows}
        self.window.select_file(rows["预算.csv"])
        self.window.select_file(rows["备忘.txt"])
        self.assertFalse(self.window.preview_table.isVisible())
        self.assertTrue(self.window.preview_text.isVisible())

    def test_a_shortcut_another_program_owns_is_refused_and_the_old_one_stays(self):
        self.window.hotkey = MagicMock()
        self.addCleanup(setattr, self.window, "hotkey", None)
        self.window.hotkey.register.side_effect = lambda key: key == ui.DEFAULT_HOTKEY
        editor = ui.QKeySequenceEdit(ui.QKeySequence("Ctrl+Alt+J"))
        problem = ui.QLabel()
        self.window.change_hotkey(editor, problem)
        self.assertEqual(editor.keySequence().toString(ui.QKeySequence.PortableText), ui.DEFAULT_HOTKEY)
        self.assertFalse(problem.isHidden())
        self.assertEqual(self.window.hotkey.register.call_args_list[-1].args, (ui.DEFAULT_HOTKEY,))

    def test_saved_versions_are_marked_and_listed_beside_the_preview(self):
        source = self.use_library({"报价.docx": "旧", "报价-最新.docx": "新", "报价_v2.docx": "中"})
        for name, moment in (("报价.docx", 1000), ("报价_v2.docx", 2000), ("报价-最新.docx", 3000)):
            os.utime(source / name, (moment, moment))
        self.window.core.scan_all()
        self.window.search.setText("报价")
        self.window.refresh_results()
        tags = {row["name"]: ui.version_tag(row) for row in self.window.rows}
        self.assertEqual(tags["报价-最新.docx"], t("Latest of {count}", count=3))
        self.assertEqual(tags["报价.docx"], t("Older version"))
        old = next(row for row in self.window.rows if row["name"] == "报价.docx")
        self.window.select_file(old["id"])
        self.assertTrue(self.window.version_panel.isVisible())
        choices = [self.window.versions.itemAt(i).widget() for i in range(1, self.window.versions.count())]
        self.assertEqual(len(choices), 2)
        self.assertIn(t("Latest"), choices[0].text())
        choices[0].click()
        self.assertEqual(self.window.detail_title.text(), "报价-最新.docx")

    def test_a_file_on_its_own_shows_no_versions(self):
        self.window.select_file(self.window.rows[0]["id"])
        self.assertFalse(self.window.version_panel.isVisible())
        self.assertEqual(ui.version_tag(self.window.rows[0]), "")

    def test_the_reader_stores_words_and_reports_what_is_left(self):
        self.use_library({"扫描.png": "x", "截图.png": "y"})
        self.window.core.scan_all()
        reported = []
        self.window.signals.reading.connect(reported.append)
        with patch.object(ui.ocr, "available", return_value=True), patch.object(ui.ocr, "read_file", return_value="星河科技"):
            self.window.start_reading()
            self.window.ocr_thread.join(5)
        self.app.processEvents()
        self.assertEqual(self.window.core.search_page("星河科技")["total"], 2)
        self.assertEqual(reported[-1], 0)

    def test_the_reader_stops_when_asked(self):
        self.use_library({"扫描.png": "x"})
        self.window.core.scan_all()
        self.window.ocr_stop.set()
        self.addCleanup(self.window.ocr_stop.clear)
        with patch.object(ui.ocr, "read_file") as read:
            self.window.read_waiting_files(self.window.core)
        read.assert_not_called()

    def test_no_recogniser_means_no_reader_and_the_older_hint(self):
        self.use_library({"预算.txt": "第四季度"})
        self.window.core.scan_all()
        with patch.object(ui.ocr, "available", return_value=False):
            self.window.start_reading()
            self.assertIsNone(self.window.ocr_thread)
            self.window.search.setText("海边")
            self.window.refresh_results()
        self.assertEqual(self.window.empty_hint.text(), t("Photos and scanned pages are found by name or date, not by what is in them."))

    def test_with_a_recogniser_the_hint_says_what_can_be_found(self):
        self.use_library({"预算.txt": "第四季度"})
        self.window.core.scan_all()
        with patch.object(ui.ocr, "available", return_value=True):
            self.window.search.setText("海边")
            self.window.refresh_results()
        self.assertEqual(self.window.empty_hint.text(), t("Photos are found by the words in them, their name or date, not by what they show."))

    def test_the_sidebar_lists_the_types_and_folders_that_exist(self):
        source = self.use_library({"预算.txt": "一", "照片.png": "二", "数据.bin": "三"})
        (source / "工作").mkdir()
        (source / "工作" / "合同.txt").write_text("四", encoding="utf-8")
        self.window.core.scan_all()
        self.window.refresh_stats()
        types = [self.window.type_nav.itemAt(i).widget().place for i in range(self.window.type_nav.count())]
        self.assertEqual(types, ["type:Documents", "type:Images", "type:Other"])
        self.assertEqual(self.window.nav_buttons["type:Documents"].count.text(), "2")
        folders = [self.window.folder_nav.itemAt(i).widget() for i in range(self.window.folder_nav.count())]
        self.assertEqual([button.text() for button in folders], ["工作"])

    def test_a_type_or_folder_narrows_the_results(self):
        source = self.use_library({"预算.txt": "一", "照片.png": "二", "数据.bin": "三"})
        (source / "工作").mkdir()
        (source / "工作" / "合同.txt").write_text("四", encoding="utf-8")
        self.window.core.scan_all()
        self.window.refresh_stats()
        self.window.select_place("type:Images")
        self.assertEqual([row["name"] for row in self.window.rows], ["照片.png"])
        self.assertEqual(self.window.section_title.text(), t("Images"))
        self.window.select_place("type:Other")
        self.assertEqual([row["name"] for row in self.window.rows], ["数据.bin"])
        folder = next(place for place in self.window.nav_buttons if place.startswith("folder:"))
        self.window.select_place(folder)
        self.assertEqual([row["name"] for row in self.window.rows], ["合同.txt"])
        self.assertEqual(self.window.section_title.text(), "工作")
        self.window.search.setText("预算")
        self.window.refresh_results()
        self.assertEqual(self.window.rows, [])
        self.assertEqual(self.window.empty_hint.text(), t("Without these filters: {files}", files=ui.file_count(1)))

    def test_a_folder_that_is_gone_returns_to_all_files(self):
        self.window.place = "folder:999:没有了"
        self.window.refresh()
        self.assertEqual(self.window.place, "all")

    def test_cards_show_the_file_type_rather_than_a_guess(self):
        row = self.window.rows[0]
        self.assertNotIn("category", row)
        self.assertEqual(ui.kind_text({"kind": "File", "ext": ".zip"}), t("{ext} file", ext="ZIP"))

    def test_tags_are_edited_on_their_own(self):
        row = self.window.rows[0]
        self.window.select_file(row["id"])

        def save():
            dialog = self.app.activeModalWidget()
            dialog.findChild(ui.QLineEdit).setText("归档线索")
            dialog.findChild(ui.QDialogButtonBox).button(ui.QDialogButtonBox.Save).click()
        ui.QTimer.singleShot(0, save)
        self.window.edit_metadata()
        self.assertEqual(self.window.core.get_file(row["id"])["tags"], ["归档线索"])
        self.window.core.set_tags(row["id"], row.get("tags", []))

    def test_the_first_row_sits_in_the_middle_of_the_deck(self):
        self.assertEqual(ui.center_out([0, 1, 2, 3, 4, 5, 6, 7, 8]), [7, 5, 3, 1, 0, 2, 4, 6, 8])
        self.assertEqual(ui.center_out([0, 1, 2])[1], 0)

    def test_home_puts_what_was_used_in_the_middle_with_new_files_around_it(self):
        self.use_library({f"资料{number}.txt": "内容" for number in range(12)})
        self.window.core.scan_all()
        self.window.refresh_stats()
        self.window.refresh_results()
        self.assertEqual(self.window.section_title.text(), t("Recent files"))
        used = self.window.rows[-1]
        self.window.mark_used(used["id"])
        self.window.refresh()
        tiles = [tile.file_id for tile in self.window.deck._tiles]
        self.assertEqual(len(tiles), 9)
        self.assertEqual(tiles[len(tiles) // 2], used["id"])
        self.assertEqual(self.window.deck._selected_id, used["id"])
        self.assertEqual(self.window.section_title.text(), t("Pick up where you left off"))
        self.assertEqual(self.window.deck_focus_label.text(), used["name"])
        self.assertIn(t("Used {date}", date=ui.date_text(time.time())), self.window.deck_focus_meta.text())

    def test_opening_copying_and_revealing_count_as_use(self):
        self.addCleanup(self.app.clipboard().clear)
        row = self.window.rows[0]
        self.window.core.mark_used(row["id"])
        with patch.object(self.window, "mark_used") as used, patch.object(ui, "open_with_default"), patch.object(ui, "show_in_folder"):
            self.window.open_file(row["id"])
            self.window.reveal_file(row["id"])
            self.window.copy_file(row["id"])
        self.assertEqual(used.call_count, 3)

    def test_the_deck_waits_until_the_window_is_back_to_rearrange(self):
        self.window.mark_used(self.window.rows[0]["id"])
        self.assertTrue(self.window.home_stale)
        with patch.object(self.window, "isActiveWindow", return_value=True), patch.object(self.window, "refresh") as refresh:
            self.window.changeEvent(ui.QEvent(ui.QEvent.ActivationChange))
        refresh.assert_called_once()

    def test_a_new_library_offers_the_usual_folders_and_reads_nothing_until_asked(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        documents, downloads = Path(folder.name) / "文档", Path(folder.name) / "下载"
        documents.mkdir()
        downloads.mkdir()
        (documents / "预算.txt").write_text("第四季度", encoding="utf-8")
        (downloads / "报价.txt").write_text("十二万", encoding="utf-8")
        self.window.core = ui.ArchiveCore(Path(folder.name) / "data")
        with patch.object(ui, "suggested_folders", return_value=[("Documents", str(documents)), ("Downloads", str(downloads))]):
            self.window.refresh()
            self.assertEqual(len(self.window.starter_boxes), 2)
            self.assertTrue(self.window.starter.isVisible())
            self.assertEqual(self.window.empty_action.text(), t("Start with these folders"))
            self.assertEqual(self.window.core.sources(), [])
            self.window.starter_boxes[1].setChecked(False)
            with patch.object(self.window, "start_scan") as scan:
                self.window.empty_clicked()
            scan.assert_called_once()
            self.assertEqual([row["path"] for row in self.window.core.sources()], [str(documents)])

    def test_with_nothing_ticked_the_user_chooses_a_folder(self):
        with patch.object(ui, "suggested_folders", return_value=[("Documents", "C:/没有这个")]):
            self.window.core = ui.ArchiveCore(Path(tempfile.mkdtemp(dir=self.scratch)) / "data")
            self.window.refresh()
            self.window.starter_boxes[0].setChecked(False)
            with patch.object(self.window, "add_source") as add:
                self.window.empty_clicked()
            add.assert_called_once()

    def test_a_folder_that_overlaps_is_skipped_and_the_rest_still_start(self):
        folder = Path(tempfile.mkdtemp(dir=self.scratch))
        outer, inner = folder / "外层", folder / "外层" / "内层"
        inner.mkdir(parents=True)
        self.window.core = ui.ArchiveCore(folder / "data")
        with patch.object(ui, "suggested_folders", return_value=[("Documents", str(outer)), ("Desktop", str(inner))]):
            self.window.refresh()
            with patch.object(self.window, "start_scan") as scan:
                self.window.empty_clicked()
        scan.assert_called_once()
        self.assertEqual(len(self.window.core.sources()), 1)

    def test_a_photo_word_is_offered_once_as_something_to_try(self):
        self.use_library({"截图.png": "x"})
        self.window.core.scan_all()
        row = self.window.core.search()[0]
        self.window.core.save_ocr(row["id"], "购买方 星河科技")
        self.addCleanup(self.window.config.pop, "words_tip_done", None)
        self.window.config.pop("words_tip_done", None)
        with patch.object(ui.ocr, "available", return_value=True):
            self.window.refresh_tip()
            self.assertTrue(self.window.tip.isVisible())
            self.assertIn("星河科技", self.window.tip_text.text())
            self.window.accept_tip()
            self.assertEqual(self.window.search.text(), "星河科技")
            self.assertFalse(self.window.tip.isVisible())
            self.window.refresh_tip()
            self.assertFalse(self.window.tip.isVisible())

    def test_starting_with_windows_is_asked_after_a_file_was_used_and_only_once(self):
        self.window.hotkey = MagicMock(active=True)
        self.addCleanup(setattr, self.window, "hotkey", None)
        self.window.demo = False
        self.addCleanup(setattr, self.window, "demo", True)
        self.addCleanup(self.window.config.pop, "autostart_asked", None)
        self.window.config["words_tip_done"] = True
        self.addCleanup(self.window.config.pop, "words_tip_done", None)
        self.window.config.pop("autostart_asked", None)
        with patch.object(ui, "AUTOSTART_AVAILABLE", True), patch.object(ui, "autostart_enabled", return_value=False), \
                patch.object(ui, "set_autostart") as turn_on:
            self.window.used_total = 0
            self.window.refresh_tip()
            self.assertFalse(self.window.tip.isVisible())
            self.window.used_total = 1
            self.window.refresh_tip()
            self.assertTrue(self.window.tip.isVisible())
            self.window.accept_tip()
            turn_on.assert_called_once()
            self.assertTrue(turn_on.call_args.args[0])
            self.assertFalse(self.window.tip.isVisible())

    def test_saying_not_now_to_starting_with_windows_changes_nothing(self):
        self.window.tip_id = "autostart"
        self.addCleanup(self.window.config.pop, "autostart_asked", None)
        with patch.object(ui, "set_autostart") as turn_on:
            self.window.dismiss_tip()
        turn_on.assert_not_called()
        self.assertTrue(self.window.config["autostart_asked"])

    def test_a_collection_can_be_made_filled_searched_and_emptied(self):
        self.use_library({"报税表.txt": "二〇二五 报税", "发票一.txt": "发票", "发票二.txt": "发票", "随笔.txt": "无关"})
        self.window.core.scan_all()
        self.window.refresh()
        with patch.object(self.window, "ask_name", return_value="报税"):
            collection = self.window.new_collection()
        place = f"collection:{collection}"
        self.assertEqual(self.window.place, place)
        self.assertEqual(self.window.nav_buttons[place].text(), "报税")
        self.assertEqual(self.window.empty_title.text(), t("This collection is empty"))
        self.assertFalse(self.window.empty_action.isVisible())
        self.window.select_place("all")
        self.window.search.setText("发票")
        self.window.refresh_results()
        self.assertTrue(self.window.collect_all.isVisible())
        menu = ui.QMenu()
        self.window.collection_choices(menu, [row["id"] for row in self.window.rows])
        menu.actions()[0].trigger()
        self.window.search.setText("")
        self.window.select_place(place)
        self.assertEqual(sorted(row["name"] for row in self.window.rows), ["发票一.txt", "发票二.txt"])
        self.assertEqual(self.window.nav_buttons[place].count.text(), "2")
        self.assertFalse(self.window.collect_all.isVisible())
        self.assertEqual(self.window.section_title.text(), "报税")
        self.window.leave_collection(collection, self.window.rows[0]["id"])
        self.assertEqual(len(self.window.rows), 1)

    def test_the_file_menu_adds_to_a_collection_and_shows_membership(self):
        self.use_library({"a.txt": "内容"})
        self.window.core.scan_all()
        self.window.refresh()
        row = self.window.rows[0]
        collection = self.window.core.create_collection("项目")
        self.window.refresh_stats()
        menu = self.window.file_menu(row["id"])
        choice = menu.findChildren(ui.QMenu)[0].actions()[0]
        self.assertFalse(choice.isChecked())
        choice.trigger()
        self.assertEqual(self.window.core.collections_of(row["id"]), {collection})
        menu = self.window.file_menu(row["id"])
        self.assertTrue(menu.findChildren(ui.QMenu)[0].actions()[0].isChecked())

    def test_a_deleted_collection_leaves_the_files_and_returns_to_all(self):
        self.use_library({"a.txt": "内容"})
        self.window.core.scan_all()
        self.window.refresh()
        collection = self.window.core.create_collection("临时")
        self.window.core.collect(collection, [self.window.rows[0]["id"]])
        self.window.refresh()
        self.window.select_place(f"collection:{collection}")
        with patch.object(ui.QMessageBox, "question", return_value=ui.QMessageBox.Yes):
            self.window.delete_collection(collection, "临时")
        self.assertEqual(self.window.place, "all")
        self.assertEqual(self.window.core.stats()["total"], 1)

    def test_every_colour_in_the_stylesheet_is_filled_in(self):
        self.assertNotIn("@", ui.STYLE)

    def test_a_collection_can_be_renamed(self):
        self.use_library({"a.txt": "内容"})
        collection = self.window.core.create_collection("旧名")
        self.window.refresh()
        with patch.object(self.window, "ask_name", return_value="新名"):
            self.window.rename_collection(collection, "旧名")
        self.assertEqual(self.window.nav_buttons[f"collection:{collection}"].text(), "新名")

    def island(self):
        island = ui.Island(self.window)
        island.setAttribute(ui.Qt.WA_DontShowOnScreen, True)
        self.addCleanup(island.deleteLater)
        self.window.island = island
        self.addCleanup(setattr, self.window, "island", None)
        return island

    def arrivals(self, *file_ids, busy=False, active=False):
        with patch.object(ui, "user_is_busy", return_value=busy), patch.object(self.window, "isActiveWindow", return_value=active):
            self.window.announce_arrivals(list(file_ids))

    def test_a_new_file_appears_at_the_top_of_the_screen_with_what_to_do_next(self):
        island = self.island()
        row = self.window.rows[0]
        self.arrivals(row["id"])
        self.assertTrue(island.isVisible())
        self.assertEqual(island.file_id, row["id"])
        self.assertEqual(island.detail.text(), t("New file"))
        self.assertFalse(island.copy_button.isHidden())
        self.assertEqual(island.y(), (self.app.primaryScreen().availableGeometry().top() + 10))

    def test_several_new_files_are_counted(self):
        island = self.island()
        self.arrivals(*[row["id"] for row in self.window.rows[:3]])
        self.assertEqual(island.detail.text(), t("{count} new files", count=3))

    def test_nothing_appears_over_a_game_or_presentation(self):
        island = self.island()
        self.arrivals(self.window.rows[0]["id"], busy=True)
        self.assertFalse(island.isVisible())

    def test_nothing_appears_for_someone_already_in_the_window(self):
        island = self.island()
        self.arrivals(self.window.rows[0]["id"], active=True)
        self.assertFalse(island.isVisible())

    def test_a_temporary_file_is_not_news(self):
        island = self.island()
        with patch.object(self.window.core, "get_file", return_value={"id": 1, "name": "download.tmp", "relative_path": "download.tmp"}):
            self.arrivals(1)
        self.assertFalse(island.isVisible())

    def test_the_arrival_bar_can_be_turned_off(self):
        island = self.island()
        self.window.config["island"] = False
        self.addCleanup(self.window.config.pop, "island", None)
        self.arrivals(self.window.rows[0]["id"])
        self.assertFalse(island.isVisible())

    def test_the_arrival_bar_never_takes_the_keyboard(self):
        island = self.island()
        self.assertTrue(island.windowFlags() & ui.Qt.WindowDoesNotAcceptFocus)
        self.assertTrue(island.testAttribute(ui.Qt.WA_ShowWithoutActivating))
        self.assertEqual(island.copy_button.focusPolicy(), ui.Qt.NoFocus)

    def test_a_pointer_on_the_bar_keeps_it_open(self):
        island = self.island()
        island.present("文件", "新文件", seconds=6)
        island.enterEvent(QEnterEvent(QPointF(), QPointF(), QPointF()))
        self.assertFalse(island.timer.isActive())
        island.leaveEvent(ui.QEvent(ui.QEvent.Leave))
        self.assertTrue(island.timer.isActive())

    def test_copying_from_the_bar_copies_the_file(self):
        self.addCleanup(self.app.clipboard().clear)
        island = self.island()
        row = self.window.rows[0]
        self.arrivals(row["id"])
        island.copy_button.click()
        self.assertFalse(island.isVisible())
        self.assertTrue(self.app.clipboard().mimeData().urls())

    def test_an_automatic_scan_announces_only_what_arrived(self):
        with patch.object(self.window, "announce_arrivals") as announced:
            self.window.scan_automatic = True
            self.window.scan_partial = True
            self.window.scanning = True
            self.window.scan_finished({"indexed": 1, "skipped": 0, "arrived": [7]})
            announced.assert_called_once_with([7])
            announced.reset_mock()
            self.window.scan_automatic = True
            self.window.scan_partial = False
            self.window.scanning = True
            self.window.scan_finished({"indexed": 1, "skipped": 0, "arrived": [8]})
            announced.assert_not_called()
            self.window.scan_automatic = False
            self.window.scanning = True
            self.window.scan_finished({"indexed": 5, "skipped": 0, "arrived": [1, 2, 3, 4, 5]})
            announced.assert_not_called()

    def test_quick_search_confirms_a_copy_on_the_bar(self):
        self.addCleanup(self.app.clipboard().clear)
        island = self.island()
        quick = self.open_quick()
        with patch.object(ui, "user_is_busy", return_value=False):
            QTest.keyClick(quick.search, ui.Qt.Key_C, ui.Qt.ControlModifier)
        self.assertTrue(island.isVisible())
        self.assertEqual(island.detail.text(), t("Copied. Ready to paste."))
        self.assertTrue(island.copy_button.isHidden())

    def test_the_words_of_a_file_can_be_copied_on_their_own(self):
        self.addCleanup(self.app.clipboard().clear)
        self.use_library({"摘录.txt": "认知负荷理论的一段话"})
        self.window.core.scan_all()
        self.window.refresh()
        row = self.window.rows[0]
        menu = self.window.file_menu(row["id"])
        next(action for action in menu.actions() if action.text() == t("Copy text")).trigger()
        self.assertEqual(self.app.clipboard().text(), "认知负荷理论的一段话")

    def test_quick_search_copies_words_with_ctrl_shift_c(self):
        self.addCleanup(self.app.clipboard().clear)
        self.use_library({"摘录.txt": "一段可以引用的话"})
        self.window.core.scan_all()
        quick = self.open_quick()
        QTest.keyClick(quick.search, ui.Qt.Key_C, ui.Qt.ControlModifier | ui.Qt.ShiftModifier)
        self.assertEqual(self.app.clipboard().text(), "一段可以引用的话")
        self.assertFalse(quick.isVisible())

    def test_a_file_without_words_has_no_copy_text(self):
        self.use_library({"数据.bin": "x"})
        self.window.core.scan_all()
        self.window.refresh()
        menu = self.window.file_menu(self.window.rows[0]["id"])
        self.assertNotIn(t("Copy text"), [action.text() for action in menu.actions()])

    def test_photos_are_offered_on_first_run(self):
        folder = Path(self.scratch)
        with patch.object(ui.QStandardPaths, "writableLocation", return_value=str(folder)):
            self.assertEqual(ui.suggested_folders(), [("Documents", str(folder))])

    def test_the_copied_message_clears_itself(self):
        self.window.show_status("copied", 5)
        self.assertTrue(self.window.status_timer.isActive())
        self.window.show_status("working")
        self.assertFalse(self.window.status_timer.isActive())

    def test_a_cover_is_redrawn_for_the_interface_language(self):
        row = self.window.rows[0]
        chinese = _file_key(row, 1, 1)
        set_language("en")
        self.assertNotEqual(chinese, _file_key(row, 1, 1))
        set_language(self.language)

    def test_app_icon_exists(self):
        self.assertFalse(ui.app_icon().isNull())

    def test_empty_results_offer_one_way_to_clear_filters(self):
        self.window.search.setText("no such file anywhere")
        self.window.refresh_results()
        self.assertEqual(self.window.pages.currentWidget(), self.window.empty)
        self.assertFalse(self.window.clear_filters.isVisible())
        self.assertTrue(self.window.empty_action.isVisible())

    def test_runtime_blocks_network_socket(self):
        with self.assertRaises(PermissionError):
            socket.socket()

    def test_clearing_a_search_returns_to_the_deck(self):
        self.window.search.setText("认知负荷")
        self.assertEqual(self.window.view_mode, "list")
        self.window.search.clear()
        self.assertEqual(self.window.view_mode, "fan")

    def test_deck_layout_and_external_selection_do_not_loop(self):
        emissions = []
        self.window.deck.selected.connect(emissions.append)
        self.window.select_file(self.window.rows[0]["id"])
        self.assertEqual(emissions, [])
        self.window.toggle_deck_layout()
        self.assertEqual(self.window.deck_mode.text(), t("Fan out"))
        self.window.set_view("grid")
        self.assertEqual(self.window.pages.currentWidget(), self.window.grid)
        self.window.set_view("fan")
        self.assertEqual(self.window.pages.currentIndex(), 0)

    def test_explicit_conditions_and_persistent_tags(self):
        self.window.search.setText("最近7天 图片")
        self.window.refresh_results()
        self.assertTrue(self.window.rows)
        self.assertTrue(all(row["ext"] == ".png" for row in self.window.rows))
        first = self.window.rows[0]
        before = list(first.get("tags", []))
        self.window.select_file(first["id"])
        self.assertTrue(self.window.preview_image.isVisible())
        self.window.tags_edit.setText("Qt交互回归标签")
        self.window.save_tags()
        self.assertEqual(self.window.core.get_file(first["id"])["tags"], ["Qt交互回归标签"])
        self.window.core.set_tags(first["id"], before)
        self.window.tags_edit.setText(", ".join(before))

    def test_image_preview_keeps_its_proportions(self):
        self.window.search.setText("最近7天 图片")
        self.window.refresh_results()
        self.window.select_file(self.window.rows[0]["id"])
        pixmap = self.window.preview_image.pixmap()
        self.assertAlmostEqual(pixmap.width() / pixmap.height(), 900 / 560, places=1)

    def test_preview_path_rejects_cloud_placeholders(self):
        row = dict(self.window.rows[0], extraction_status="cloud_placeholder", archive_path="")
        with patch.object(ui, "_assert_local_path") as validator:
            self.assertIsNone(ui.readable_path(row))
            validator.assert_not_called()

    def test_metadata_dialog_cancel_and_save(self):
        row = self.window.rows[0]
        before = list(row.get("tags", []))
        self.window.select_file(row["id"])
        def edit_then_cancel():
            dialog = self.app.activeModalWidget()
            dialog.findChild(ui.QLineEdit).setText("不应保存的标签")
            dialog.reject()
        ui.QTimer.singleShot(0, edit_then_cancel)
        self.window.edit_metadata()
        self.assertEqual(self.window.core.get_file(row["id"])["tags"], before)
        def edit_then_save():
            dialog = self.app.activeModalWidget()
            dialog.findChild(ui.QLineEdit).setText("元数据弹窗回归")
            dialog.accept()
        ui.QTimer.singleShot(0, edit_then_save)
        self.window.edit_metadata()
        self.assertEqual(self.window.core.get_file(row["id"])["tags"], ["元数据弹窗回归"])
        self.window.core.set_tags(row["id"], before)
        self.window.tags_edit.setText(", ".join(before))

    def test_narrow_layout_keeps_search_and_controls_visible(self):
        self.window.resize(1100, 700)
        self.app.processEvents()
        self.assertGreater(self.window.search.width(), 300)
        self.assertTrue(self.window.view_buttons["list"].isVisible())
        self.assertGreaterEqual(self.window.deck.height(), 200)
        deck_bottom = self.window.deck.mapTo(self.window, ui.QPoint(0, self.window.deck.height())).y()
        controls_top = self.window.deck_mode.mapTo(self.window, ui.QPoint(0, 0)).y()
        self.assertGreaterEqual(controls_top, deck_bottom)

    def test_space_can_be_typed_in_search(self):
        from PySide6.QtTest import QTest
        self.window.search.setFocus()
        self.window.search.setText("PDF")
        self.window.search.setCursorPosition(3)
        QTest.keyClick(self.window.search, ui.Qt.Key_Space)
        self.assertEqual(self.window.search.text(), "PDF ")

    def test_down_arrow_moves_from_search_into_results(self):
        self.window.set_view("list")
        self.window.search.setFocus()
        event = ui.QKeyEvent(ui.QEvent.KeyPress, ui.Qt.Key_Down, ui.Qt.NoModifier)
        self.assertTrue(self.window.eventFilter(self.window.search, event))
        self.assertEqual(self.window.list.currentIndex().row(), 0)

    def test_grid_fills_width_with_whole_columns(self):
        self.window.set_view("grid")
        self.window.resize(1360, 850)
        self.app.processEvents()
        self.window.grid.fit_cells()
        width = self.window.grid.viewport().width()
        cell = self.window.grid.gridSize().width()
        self.assertGreaterEqual(cell, ui.MIN_CELL_WIDTH)
        self.assertLess(width - (width // cell) * cell, cell / 2)

    def test_grid_cells_do_not_depend_on_the_scroll_bar(self):
        grid = self.window.grid
        self.window.set_view("grid")
        self.window.resize(1360, 850)
        sizes = []
        for policy in (ui.Qt.ScrollBarAlwaysOff, ui.Qt.ScrollBarAlwaysOn):
            grid.setVerticalScrollBarPolicy(policy)
            self.app.processEvents()
            grid.fit_cells()
            sizes.append(grid.gridSize())
        grid.setVerticalScrollBarPolicy(ui.Qt.ScrollBarAsNeeded)
        self.assertEqual(sizes[0], sizes[1])

    def test_language_switch_rebuilds_in_place_and_is_remembered(self):
        self.window.set_view("list")
        self.window.apply_language("en")
        self.addCleanup(self.window.apply_language, "zh")
        self.assertEqual(self.window.scan_button.text(), "Update")
        self.assertEqual(self.window.windowTitle(), "Roostfile")
        self.assertEqual(self.window.nav_buttons["type:Documents"].text(), "Documents")
        self.assertEqual(self.window.pages.currentWidget(), self.window.list)
        self.assertTrue(self.window.rows)
        self.assertTrue(all(ord(ch) < 0x2E80 for row in self.window.rows for ch in row["name"]))
        self.assertEqual(self.window.load_config()["language"], "en")


class EnglishDefaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = ui.QApplication.instance() or ui.QApplication([])
        prepare_demo(ui.BASE, "en")

    def test_english_is_the_default_interface(self):
        (ui.BASE / ".demo-data" / "ui-state.json").write_text("{}", encoding="utf-8")
        window = ui.ArchiveWindow(demo=True)
        window.setAttribute(ui.Qt.WA_DontShowOnScreen, True)
        window.show()
        self.assertEqual(window.search.placeholderText()[:6], "Search")
        self.assertEqual(window.nav_buttons["type:Documents"].text(), "Documents")
        window.search.setText("cognitive load")
        window.refresh_results()
        self.assertEqual(len(window.rows), 1)
        window.search.setText("last 7 days images")
        window.refresh_results()
        self.assertTrue(all(row["ext"] == ".png" for row in window.rows))
        window.close()


class PagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = ui.QApplication.instance() or ui.QApplication([])
        prepare_demo(ui.BASE, "en")

    def test_model_loads_the_next_page_on_request(self):
        pages = [[{"id": i} for i in range(3, 6)], [{"id": 6}]]
        model = ui.FileModel(lambda offset: pages.pop(0))
        model.replace([{"id": i} for i in range(3)], 7)
        self.assertTrue(model.canFetchMore())
        model.fetchMore()
        self.assertEqual(model.rowCount(), 6)
        model.fetchMore()
        self.assertEqual(model.rowCount(), 7)
        self.assertFalse(model.canFetchMore())

    def test_launching_files_are_never_run_directly(self):
        for ext in (".py", ".ps1", ".exe", ".lnk", ".vbs", ".jar"):
            self.assertIn(ext, ui.LAUNCHING_EXTS)
        self.assertNotIn(".pdf", ui.LAUNCHING_EXTS)


if __name__ == "__main__":
    unittest.main()
