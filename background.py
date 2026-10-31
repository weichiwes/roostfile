"""Everything that differs by operating system: a shortcut that works anywhere, one
running copy, starting at sign-in, change notices from added folders, and handing
files to the system.

Windows is the tested platform. The macOS paths follow Apple's documented APIs, and
any piece that is missing reports itself unavailable, so the app still runs. Nothing
here reads a file's contents, and the single-instance check is a local pipe, not a socket.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import importlib.util
import os
from pathlib import Path
import plistlib
import shlex
import struct
import subprocess
import sys
import threading

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from core import is_noise

DEFAULT_HOTKEY = "Ctrl+Alt+K"
AUTOSTART_NAME = "Roostfile"
MAC = sys.platform == "darwin"
AUTOSTART_AVAILABLE = os.name == "nt" or MAC
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.roostfile.app.plist"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MODIFIERS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "meta": MOD_WIN, "win": MOD_WIN}
_KEYS = {
    "space": 0x20,
    **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
    **{chr(code): code for code in range(ord("0"), ord("9") + 1)},
    **{f"F{number}": 0x6F + number for number in range(1, 25)},
}

FILE_LIST_DIRECTORY = 0x1
SHARE_EVERYTHING = 0x7
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE = wintypes.HANDLE(-1).value
# Names, sizes and saved changes. Attributes and access times stay out, so a
# scan reading a file never reports itself.
NOTIFY_FILTER = 0x1 | 0x2 | 0x8 | 0x10
WAIT_FOR_EVER = 0xFFFFFFFF
BUFFER_BYTES = 64 * 1024


# macOS key codes follow keyboard position, not letters.
_MAC_KEYS = dict(zip("ASDFHGZXCV", range(10))) | dict(zip("BQWERYT", range(11, 18))) | dict(zip("OUIP", (31, 32, 34, 35))) \
    | dict(zip("LJK", (37, 38, 40))) | dict(zip("NM", (45, 46))) | dict(zip("123465", (18, 19, 20, 21, 22, 23))) \
    | dict(zip("9785", (25, 26, 28, 23))) | {"0": 29, "SPACE": 49} \
    | dict(zip((f"F{n}" for n in range(1, 13)), (122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111)))
# Qt writes the Command key as Ctrl, so the same setting means the usual key on each system.
_MAC_MODIFIERS = {"ctrl": 0x100, "shift": 0x200, "alt": 0x800, "meta": 0x1000}


def parse_mac_shortcut(text: str):
    """(modifiers, key code) for macOS, or None."""
    parts = [part.strip() for part in str(text).split("+") if part.strip()]
    if len(parts) < 2:
        return None
    modifiers = 0
    for part in parts[:-1]:
        flag = _MAC_MODIFIERS.get(part.casefold())
        if flag is None:
            return None
        modifiers |= flag
    key = _MAC_KEYS.get(parts[-1].upper())
    return (modifiers, key) if key is not None else None


def parse_shortcut(text: str):
    """(modifiers, virtual key) for ``Ctrl+Alt+K``, or None if Windows could not register it."""
    parts = [part.strip() for part in str(text).split("+") if part.strip()]
    if len(parts) < 2:
        return None
    modifiers = 0
    for part in parts[:-1]:
        flag = _MODIFIERS.get(part.casefold())
        if flag is None:
            return None
        modifiers |= flag
    key = parts[-1]
    key = _KEYS.get(key.upper()) or _KEYS.get(key.casefold())
    return (modifiers, key) if key else None


def key_label(text: str) -> str:
    return native_keys(str(text).replace("+", " "))


def native_keys(text: str) -> str:
    """Ctrl C as people see it on this computer."""
    return text.replace("Ctrl ", "⌘ ").replace("Alt ", "⌥ ") if MAC else text


def open_with_default(path):
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.run(["open" if MAC else "xdg-open", str(path)], check=True)


def show_in_folder(path):
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
    elif MAC:
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


def user_is_busy() -> bool:
    """True during a full-screen game, film or presentation, when nothing should appear."""
    if os.name != "nt":
        return False
    state = ctypes.c_int()
    # 5 is QUNS_ACCEPTS_NOTIFICATIONS. Anything else means someone is occupied.
    return ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value != 5


class _HotKeyId(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventType(ctypes.Structure):
    _fields_ = [("event_class", ctypes.c_uint32), ("kind", ctypes.c_uint32)]


class GlobalHotkey(QAbstractNativeEventFilter):
    """Install with QApplication.installNativeEventFilter, then call register."""

    ID = 0x5144

    def __init__(self, pressed):
        super().__init__()
        self.pressed = pressed
        self.active = False
        self._mac_ref = None
        self._mac_handler = None

    def register(self, text: str) -> bool:
        self.unregister()
        if MAC:
            return self._register_mac(text)
        parsed = parse_shortcut(text)
        if os.name != "nt" or not parsed:
            return False
        self.active = bool(ctypes.windll.user32.RegisterHotKey(None, self.ID, parsed[0] | MOD_NOREPEAT, parsed[1]))
        return self.active

    def _register_mac(self, text):
        parsed = parse_mac_shortcut(text)
        if not parsed:
            return False
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        target = carbon.GetApplicationEventTarget()
        if self._mac_handler is None:
            handler = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
            self._mac_handler = handler(lambda *_: self.pressed() or 0)
            pressed_event = _EventType(0x6B657962, 5)  # 'keyb', kEventHotKeyPressed
            carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, handler, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            carbon.InstallEventHandler(target, self._mac_handler, 1, ctypes.byref(pressed_event), None, None)
        reference = ctypes.c_void_p()
        carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, _HotKeyId, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        status = carbon.RegisterEventHotKey(parsed[1], parsed[0], _HotKeyId(self.ID, 1), target, 0, ctypes.byref(reference))
        self.active = status == 0
        self._mac_ref = reference if self.active else None
        return self.active

    def unregister(self):
        if self.active and MAC:
            ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon").UnregisterEventHotKey(self._mac_ref)
        elif self.active:
            ctypes.windll.user32.UnregisterHotKey(None, self.ID)
        self.active = False

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            event = wintypes.MSG.from_address(int(message))
            if event.message == WM_HOTKEY and event.wParam == self.ID:
                self.pressed()
                return True, 0
        return False, 0


class SingleInstance(QObject):
    """A second launch wakes the running copy instead of opening the archive twice."""

    activated = Signal()

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.name = "roostfile-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        self.server = None

    def claim(self) -> bool:
        probe = QLocalSocket()
        probe.connectToServer(self.name)
        if probe.waitForConnected(400):
            probe.disconnectFromServer()
            return False
        QLocalServer.removeServer(self.name)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._woken)
        return self.server.listen(self.name)

    def _woken(self):
        connection = self.server.nextPendingConnection()
        if connection:
            connection.close()
            connection.deleteLater()
        self.activated.emit()


def autostart_command(script) -> str:
    python = Path(sys.executable)
    windowed = python.with_name("pythonw.exe")
    return f'"{windowed if windowed.exists() else python}" "{script}" --background'


def autostart_enabled(key=RUN_KEY) -> bool:
    if MAC:
        return LAUNCH_AGENT.exists()
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as run:
            winreg.QueryValueEx(run, AUTOSTART_NAME)
        return True
    except OSError:
        return False


def launch_agent(command: str) -> bytes:
    """What macOS reads to start the app at sign-in."""
    return plistlib.dumps({"Label": "com.roostfile.app", "ProgramArguments": shlex.split(command), "RunAtLoad": True})


def set_autostart(enabled: bool, command: str, key=RUN_KEY):
    if MAC:
        if enabled:
            LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
            LAUNCH_AGENT.write_bytes(launch_agent(command))
        else:
            LAUNCH_AGENT.unlink(missing_ok=True)
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as run:
        if enabled:
            winreg.SetValueEx(run, AUTOSTART_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(run, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


def foreground_window():
    """The window that has the keyboard now, to hand it back later. None if unknown."""
    if MAC:
        try:
            from AppKit import NSWorkspace
            return NSWorkspace.sharedWorkspace().frontmostApplication()
        except ImportError:
            return None
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    return user32.GetForegroundWindow()


def give_keyboard_to(window):
    if window and MAC:
        window.activateWithOptions_(0)
    elif window:
        user32 = ctypes.windll.user32
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow(window)


def parse_changes(data: bytes) -> list[str]:
    """The relative paths in a FILE_NOTIFY_INFORMATION buffer."""
    names, offset = [], 0
    while offset + 12 <= len(data):
        following, _, length = struct.unpack_from("<III", data, offset)
        names.append(data[offset + 12:offset + 12 + length].decode("utf-16-le", "replace"))
        if not following:
            break
        offset += following
    return names


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped), ctypes.c_void_p]
    kernel.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped)]
    kernel.WaitForMultipleObjects.restype = wintypes.DWORD
    kernel.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]
    for name in ("SetEvent", "ResetEvent", "CloseHandle"):
        getattr(kernel, name).argtypes = [wintypes.HANDLE]
    return kernel


class FolderWatcher:
    """Calls ``changed(folder, paths)`` when something worth a scan happens under a folder.

    Windows tells us, so an idle library costs no disk work at all. One thread
    per added folder waits on the whole tree. A folder that goes away ends its
    thread, and the next ``watch`` call starts it again if it has returned.
    """

    available = os.name == "nt" or (MAC and importlib.util.find_spec("watchdog") is not None)

    def __init__(self, changed):
        self.changed = changed
        self._watches = {}
        self._observer = None

    def watch(self, folders):
        wanted = {str(folder) for folder in folders}
        if MAC:
            self._watch_mac(wanted)
            return
        for folder, (thread, stop) in list(self._watches.items()):
            if folder not in wanted or not thread.is_alive():
                self._end(folder, stop)
        if not self.available:
            return
        kernel = _kernel32()
        for folder in wanted - set(self._watches):
            stop = kernel.CreateEventW(None, True, False, None)
            thread = threading.Thread(target=self._wait, args=(kernel, folder, stop), daemon=True, name="folder-watch")
            self._watches[folder] = (thread, stop)
            thread.start()

    def _watch_mac(self, wanted):
        if not self.available:
            return
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
        if self._observer is None:
            self._observer = Observer()
            self._observer.start()
        for folder in set(self._watches) - wanted:
            self._observer.unschedule(self._watches.pop(folder))
        watcher = self

        class Notices(FileSystemEventHandler):
            def __init__(self, folder):
                self.folder = folder

            def on_any_event(self, event):
                if event.event_type in ("opened", "closed_no_write"):
                    return
                paths = [os.path.relpath(path, self.folder) for path in (event.src_path, getattr(event, "dest_path", "")) if path]
                wanted_paths = [path for path in paths if not is_noise(path)]
                if wanted_paths:
                    watcher.changed(self.folder, wanted_paths)

        for folder in wanted - set(self._watches):
            self._watches[folder] = self._observer.schedule(Notices(folder), folder, recursive=True)

    def _end(self, folder, stop):
        kernel = _kernel32()
        thread, _ = self._watches.pop(folder)
        kernel.SetEvent(stop)
        thread.join(2)
        kernel.CloseHandle(stop)

    def stop(self):
        if self._observer is not None:
            self._observer.stop()
            self._observer = None
            self._watches = {}
            return
        for folder, (_, stop) in list(self._watches.items()):
            self._end(folder, stop)

    def _wait(self, kernel, folder, stop):
        handle = kernel.CreateFileW(folder, FILE_LIST_DIRECTORY, SHARE_EVERYTHING, None, OPEN_EXISTING,
                                    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED, None)
        if handle == INVALID_HANDLE:
            return
        done = kernel.CreateEventW(None, True, False, None)
        overlapped = _Overlapped(hEvent=done)
        buffer = ctypes.create_string_buffer(BUFFER_BYTES)
        returned = wintypes.DWORD()
        waiting_on = (wintypes.HANDLE * 2)(done, stop)
        try:
            while True:
                kernel.ResetEvent(done)
                if not kernel.ReadDirectoryChangesW(handle, buffer, BUFFER_BYTES, True, NOTIFY_FILTER, None, ctypes.byref(overlapped), None):
                    return
                if kernel.WaitForMultipleObjects(2, waiting_on, False, WAIT_FOR_EVER) != 0:
                    kernel.CancelIoEx(handle, ctypes.byref(overlapped))
                    kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(returned), True)
                    return
                if not kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(returned), False):
                    return
                wanted = [name for name in parse_changes(buffer.raw[:returned.value]) if not is_noise(name)]
                if not returned.value:
                    self.changed(folder, None)  # Too much at once for the buffer: look everywhere.
                elif wanted:
                    self.changed(folder, wanted)
        finally:
            kernel.CloseHandle(done)
            kernel.CloseHandle(handle)
