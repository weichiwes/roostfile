"""The parts that work with the window closed. Needs Qt; some tests need Windows."""
import ctypes
import os
from pathlib import Path
import plistlib
import shutil
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_app as ui
import background
from background import FolderWatcher, GlobalHotkey, SingleInstance, key_label, parse_changes, parse_shortcut


class ShortcutTests(unittest.TestCase):
    def test_shortcuts_become_windows_keys(self):
        self.assertEqual(parse_shortcut("Ctrl+Alt+K"), (background.MOD_CONTROL | background.MOD_ALT, ord("K")))
        self.assertEqual(parse_shortcut("Ctrl+Shift+Space"), (background.MOD_CONTROL | background.MOD_SHIFT, 0x20))
        self.assertEqual(parse_shortcut("Alt+F2"), (background.MOD_ALT, 0x71))
        self.assertEqual(parse_shortcut("ctrl+alt+k")[1], ord("K"))

    def test_what_windows_cannot_register_is_refused(self):
        for text in ("K", "", "Ctrl+Alt", "Ctrl+Tab", "Super+K"):
            with self.subTest(text=text):
                self.assertIsNone(parse_shortcut(text))

    def test_shortcuts_read_the_way_the_interface_writes_keys(self):
        self.assertEqual(key_label("Ctrl+Alt+K"), "Ctrl Alt K")


class MacTests(unittest.TestCase):
    """The parts of macOS support that are plain logic. Apple's own APIs need a Mac."""

    def test_the_default_shortcut_means_command_option_k(self):
        self.assertEqual(background.parse_mac_shortcut("Ctrl+Alt+K"), (0x100 | 0x800, 40))

    def test_keys_map_to_their_position_on_the_keyboard(self):
        self.assertEqual(background.parse_mac_shortcut("Ctrl+Shift+Space"), (0x100 | 0x200, 49))
        self.assertEqual(background.parse_mac_shortcut("Ctrl+A"), (0x100, 0))
        self.assertEqual(background.parse_mac_shortcut("Meta+F1"), (0x1000, 122))

    def test_every_letter_and_digit_can_be_used(self):
        for key in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
            self.assertIsNotNone(background.parse_mac_shortcut(f"Ctrl+{key}"), key)

    def test_no_two_keys_share_a_code(self):
        codes = {key: background.parse_mac_shortcut(f"Ctrl+{key}")[1] for key in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
        self.assertEqual(len(set(codes.values())), len(codes))

    def test_what_macos_cannot_register_is_refused(self):
        for text in ("K", "Ctrl+Tab", "Super+K"):
            self.assertIsNone(background.parse_mac_shortcut(text))

    def test_keys_are_written_the_mac_way(self):
        with patch.object(background, "MAC", True):
            self.assertEqual(background.key_label("Ctrl+Alt+K"), "⌘ ⌥ K")
            self.assertEqual(background.native_keys("Ctrl C to copy"), "⌘ C to copy")

    def test_windows_keeps_its_own_labels(self):
        with patch.object(background, "MAC", False):
            self.assertEqual(background.key_label("Ctrl+Alt+K"), "Ctrl Alt K")

    def test_sign_in_uses_a_launch_agent_that_can_be_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            agent = Path(folder) / "LaunchAgents" / "com.roostfile.app.plist"
            with patch.object(background, "MAC", True), patch.object(background, "LAUNCH_AGENT", agent):
                self.assertFalse(background.autostart_enabled())
                background.set_autostart(True, '"/usr/bin/python3" "/Apps/Roostfile/desktop_app.py" --background')
                self.assertTrue(background.autostart_enabled())
                saved = plistlib.loads(agent.read_bytes())
                self.assertEqual(saved["ProgramArguments"], ["/usr/bin/python3", "/Apps/Roostfile/desktop_app.py", "--background"])
                self.assertTrue(saved["RunAtLoad"])
                background.set_autostart(False, "")
                self.assertFalse(background.autostart_enabled())


class ChangeListTests(unittest.TestCase):
    @staticmethod
    def entry(name, following=0):
        encoded = name.encode("utf-16-le")
        return struct.pack("<III", following, 1, len(encoded)) + encoded

    def test_every_changed_path_is_read(self):
        first = self.entry("工作\\合同.docx")
        first = self.entry("工作\\合同.docx", len(first))
        self.assertEqual(parse_changes(first + self.entry("新建.txt")), ["工作\\合同.docx", "新建.txt"])

    def test_an_empty_buffer_has_no_names(self):
        self.assertEqual(parse_changes(b""), [])


class SingleInstanceTests(unittest.TestCase):
    def test_a_second_copy_wakes_the_first_and_steps_aside(self):
        app = ui.QApplication.instance() or ui.QApplication([])
        key = f"test-{os.getpid()}-{time.time()}"
        first, second = SingleInstance(key), SingleInstance(key)
        woken = []
        first.activated.connect(lambda: woken.append(True))
        self.assertTrue(first.claim())
        self.assertFalse(second.claim())
        deadline = time.monotonic() + 3
        while not woken and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        self.assertTrue(woken)
        first.server.close()


@unittest.skipUnless(os.name == "nt", "Windows only")
class WindowsHotkeyTests(unittest.TestCase):
    def test_a_registered_shortcut_reaches_the_app(self):
        app = ui.QApplication.instance() or ui.QApplication([])
        pressed = []
        hotkey = GlobalHotkey(lambda: pressed.append(True))
        app.installNativeEventFilter(hotkey)
        self.addCleanup(app.removeNativeEventFilter, hotkey)
        self.addCleanup(hotkey.unregister)
        session = ctypes.c_ulong()
        ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
        if not session.value:
            self.skipTest("Needs a signed-in desktop")
        self.assertTrue(hotkey.register("Ctrl+Alt+Shift+F23"))
        thread = ctypes.windll.kernel32.GetCurrentThreadId()
        ctypes.windll.user32.PostThreadMessageW(thread, background.WM_HOTKEY, GlobalHotkey.ID, 0)
        deadline = time.monotonic() + 2
        while not pressed and time.monotonic() < deadline:
            app.processEvents()
        self.assertTrue(pressed)
        hotkey.unregister()
        self.assertFalse(hotkey.active)


@unittest.skipUnless(os.name == "nt", "Windows only")
class WindowsAutostartTests(unittest.TestCase):
    KEY = r"Software\RoostfileTest\Run"

    def tearDown(self):
        import winreg
        for key in (self.KEY, r"Software\RoostfileTest"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass

    def test_it_can_be_turned_on_and_off(self):
        self.assertFalse(background.autostart_enabled(self.KEY))
        background.set_autostart(True, r'"C:\app\pythonw.exe" "C:\app\desktop_app.py" --background', self.KEY)
        self.assertTrue(background.autostart_enabled(self.KEY))
        background.set_autostart(False, "", self.KEY)
        self.assertFalse(background.autostart_enabled(self.KEY))

    def test_the_command_starts_in_the_tray(self):
        self.assertTrue(background.autostart_command(r"C:\app\desktop_app.py").endswith('"C:\\app\\desktop_app.py" --background'))


@unittest.skipUnless(os.name == "nt", "Windows only")
class WindowsFolderWatchTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="roostfile-watch-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.calls = []
        self.watcher = FolderWatcher(lambda folder, paths: self.calls.append((folder, paths)))
        self.addCleanup(self.watcher.stop)
        self.watcher.watch([self.folder])
        time.sleep(.2)

    def noticed(self, seconds=2):
        deadline = time.monotonic() + seconds
        while not self.calls and time.monotonic() < deadline:
            time.sleep(.01)
        return bool(self.calls)

    def test_a_new_file_deep_in_the_folder_is_noticed(self):
        deep = self.folder / "工作" / "合同"
        deep.mkdir(parents=True)
        self.assertTrue(self.noticed())
        self.calls.clear()
        (deep / "新文件.txt").write_text("内容", encoding="utf-8")
        self.assertTrue(self.noticed())
        folder, paths = self.calls[0]
        self.assertEqual(folder, str(self.folder))
        self.assertIn(os.path.join("工作", "合同", "新文件.txt"), paths)

    def test_system_and_temporary_files_do_not_wake_a_scan(self):
        (self.folder / "desktop.ini").write_text("x", encoding="utf-8")
        (self.folder / "~$draft.docx").write_text("x", encoding="utf-8")
        (self.folder / "download.tmp").write_text("x", encoding="utf-8")
        self.assertFalse(self.noticed(.7))

    def test_reading_a_file_does_not_look_like_a_change(self):
        path = self.folder / "读取.txt"
        path.write_text("内容", encoding="utf-8")
        self.assertTrue(self.noticed())
        time.sleep(.3)
        self.calls.clear()
        path.read_text(encoding="utf-8")
        self.assertFalse(self.noticed(.7))

    def test_watching_never_stops_a_folder_being_renamed_or_removed(self):
        renamed = self.folder.with_name(self.folder.name + "-renamed")
        self.folder.rename(renamed)
        self.addCleanup(shutil.rmtree, renamed, True)
        renamed.rename(self.folder)

    def test_a_folder_that_went_away_is_watched_again_when_it_returns(self):
        self.watcher.stop()
        shutil.rmtree(self.folder)
        self.watcher.watch([self.folder])
        time.sleep(.2)
        self.folder.mkdir()
        self.watcher.watch([self.folder])
        time.sleep(.2)
        (self.folder / "回来了.txt").write_text("内容", encoding="utf-8")
        self.assertTrue(self.noticed())


if __name__ == "__main__":
    unittest.main()
