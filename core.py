"""Local file catalogue. Only explicitly selected folders are read.

No socket, HTTP, telemetry, remote model, or automatic file deletion is used.
The optional ``copy`` mode stores content-addressed copies inside data_dir/vault.
A file is moved only when someone has chosen that, and it can be put back.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
from contextlib import contextmanager
import errno
import html
import io
from itertools import islice
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib



MAX_EXTRACT_BYTES = 24 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_XML_TOTAL = 48 * 1024 * 1024
MAX_BUNDLE = 500
MAX_GRAPH_LINKS = 2000
NEARBY_SECONDS = 3600

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".rst", ".tex",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".scss", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".java", ".rs", ".go", ".rb", ".php", ".vue",
    ".svelte", ".swift", ".kt", ".sql", ".sh", ".ps1", ".bat", ".xml",
    ".html", ".htm", ".rtf", ".srt", ".vtt", ".ipynb", ".r", ".m",
}
CODE_EXTS = TEXT_EXTS - {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".rst", ".tex",
    ".html", ".htm", ".rtf", ".srt", ".vtt",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".avif", ".svg", ".ico"}
DESIGN_EXTS = {".psd", ".ai", ".sketch", ".fig", ".blend", ".fbx", ".obj", ".glb", ".gltf", ".stl", ".eps", ".indd"}
MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".aiff", ".aif"}
IGNORED_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", "$recycle.bin", "system volume information"}
IGNORED_FILES = {"desktop.ini", "thumbs.db", "ehthumbs.db", "ehthumbs_vista.db", ".ds_store"}
MAX_WINDOWS_DIR = 248
NO_PDF_PARSER = "PDF parser not installed. Indexed file details only."
NO_TEXT_LAYER = "No text layer in this PDF. Scanned pages are not read."
TOO_LARGE = "File is large. Indexed file details only."
TABLE_EXTS = (".csv", ".tsv", ".xlsx")
MAX_PREVIEW_ROWS = 200
MAX_PREVIEW_COLUMNS = 30
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
OCR_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
# Saved over and over under nearly the same name. Photos and code are not.
VERSIONED_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".txt", ".md", ".odt", ".ods", ".odp", ".rtf", ".pages", ".numbers", ".key"}
_UNNAMED = ("新建", "未命名", "untitled", "document", "new ", "文档", "工作簿")
_VERSION_TAIL = re.compile(
    r"(?:\s*[(（]\d+[)）]"
    r"|[\s_\-.]*(?:副本|最新版?|最终版?|终稿|定稿|初稿|修改版?|修订版?)"
    r"|[\s_\-.]+(?:v\d+(?:\.\d+)*|copy|final|latest|draft|old|new|改|新版|旧版|备份))$", re.IGNORECASE)
CLOUD_PLACEHOLDER = "Cloud placeholder: the content is not on this computer yet, so it was not read or copied."
TOO_DEEP = "Some folders are nested too deeply for Windows to read, so they were left out."
MAX_TOKEN_GRAMS = 12
COVER_TEXT_CHARS = 1600
# Antivirus software checks each file the first time it is opened. On Windows that
# wait dominates a first scan, and it passes in parallel: 6.3x faster with 6 threads.
SCAN_WORKERS = 4
SCAN_BATCH = 32
SCAN_FLUSH_ROWS = 100
SCAN_FLUSH_SECONDS = 1.0
PROGRESS_INTERVAL = 0.1
# Everything except the extracted text: list views never carry document bodies.
FILE_COLUMNS = (
    "id", "source_id", "path", "path_key", "relative_path", "name",
    "kind", "ext", "size", "mtime", "mtime_ns", "tags", "summary",
    "favorite", "missing", "archive_path", "content_hash", "extraction_status",
    "extraction_error", "indexed_at", "version_key", "last_used",
)
_LIST_SELECT = ",".join("f." + column for column in FILE_COLUMNS)
_GRAM_RUN = re.compile(r"[^\W_]+")


class ScanCancelled(Exception):
    pass


def _normal(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _assert_local_path(path: Path | str, check_links=False):
    """Reject remote volumes before any resolve/stat that could contact them."""
    raw = os.fspath(path)
    if raw.startswith(("\\\\", "//")) or "://" in raw:
        raise ValueError("Network paths are not supported. Choose a folder on this computer.")
    absolute = Path(os.path.abspath(os.path.expanduser(raw)))
    if os.name == "nt":
        import ctypes
        drive, _ = os.path.splitdrive(str(absolute))
        if drive and ctypes.windll.kernel32.GetDriveTypeW(str(drive + "\\")) == 4:
            raise ValueError("Mapped network drives are not supported. Choose a folder on this computer.")
    elif sys.platform == "darwin" and _on_network_volume(str(absolute)):
        raise ValueError("Mapped network drives are not supported. Choose a folder on this computer.")
    if check_links:
        # Walk ancestors from the local volume; never descend through a junction.
        candidates = [*reversed(absolute.parents), absolute]
        for candidate in candidates:
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if candidate.is_symlink() or _redirects(info):
                raise ValueError("Choose a real local folder. Symbolic links and reparse points are not supported.")
    return absolute


def network_mounts(listing):
    """Mount points of network volumes, from the output of the macOS mount command."""
    return [match[1] for match in re.finditer(r" on (/.+?) \((?:smbfs|nfs|afpfs|webdav|cifs)\b", listing)]


_mounts_checked = [0.0, []]


def _on_network_volume(path):
    # Checked for every file shown, so the list of mounts is only refreshed each minute.
    if time.monotonic() - _mounts_checked[0] > 60 or not _mounts_checked[0]:
        try:
            _mounts_checked[1:] = [network_mounts(subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=5).stdout)]
        except (OSError, subprocess.SubprocessError):
            _mounts_checked[1:] = [[]]
        _mounts_checked[0] = time.monotonic()
    return any(path == mount or path.startswith(mount + "/") for mount in _mounts_checked[1])


def is_noise(relative_path):
    """A change no scan would act on, so it should not wake one."""
    parts = [part.casefold() for part in re.split(r"[\\/]", str(relative_path)) if part]
    if not parts or any(part in IGNORED_DIRS for part in parts):
        return True
    return parts[-1] in IGNORED_FILES or parts[-1].startswith("~$") or parts[-1].endswith((".tmp", ".crdownload", ".part", ".icloud"))


def version_key(relative_path, name):
    """The same document saved as 合同终稿(1), 合同终稿-最新 and so on, in one folder."""
    stem, ext = os.path.splitext(name)
    if ext.lower() not in VERSIONED_EXTS:
        return ""
    family = _normal(stem)
    shorter = _VERSION_TAIL.sub("", family)
    while shorter != family:
        family, shorter = shorter, _VERSION_TAIL.sub("", shorter)
    family = family.strip(" _-.")
    if not family or family.startswith(_UNNAMED):
        return ""
    folder = re.split(r"[\\/](?=[^\\/]*$)", str(relative_path))
    return _normal(folder[0] if len(folder) > 1 else "") + "|" + family + ext.lower()


def _family(source_id, relative_path, name):
    """Versions live in one folder of one source, however alike two sources are."""
    key = version_key(relative_path, name)
    return f"{source_id}:{key}" if key else ""


def _redirects(info):
    """Junctions and symlinks lead elsewhere. The reparse tag on a cloud folder does not."""
    return bool(getattr(info, "st_reparse_tag", 0) & 0x20000000)


def _path_too_long(error):
    """Windows cannot reach a deep folder unless long paths are turned on."""
    return getattr(error, "winerror", None) in {3, 206} and len(os.fspath(error.filename or "")) >= MAX_WINDOWS_DIR


def _cloud_placeholder(stat):
    attrs = getattr(stat, "st_file_attributes", 0)
    # SF_DATALESS is how macOS marks a file still in iCloud.
    return bool(attrs & (0x1000 | 0x40000 | 0x400000)) or bool(getattr(stat, "st_flags", 0) & 0x40000000)


def _same_bytes(one, other):
    """Compared whole, since a move deletes the original on the strength of this."""
    with open(one, "rb") as first, open(other, "rb") as second:
        while True:
            left, right = first.read(1024 * 1024), second.read(1024 * 1024)
            if left != right:
                return False
            if not left:
                return True


def _free_name(path):
    """A name nothing else has: report.pdf, then report (2).pdf."""
    candidate, number = path, 2
    while candidate.exists():
        candidate, number = path.with_name(f"{path.stem} ({number}){path.suffix}"), number + 1
    return candidate


def _carry_file(origin, target):
    """Moves one file. Across disks it is copied and compared before the original goes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError("Something else is already at that location.")
    try:
        os.rename(origin, target)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    temporary = target.with_name(target.name + ".part")
    try:
        shutil.copyfile(origin, temporary)
        shutil.copystat(origin, temporary)
        if not _same_bytes(origin, temporary):
            raise OSError("The copy did not match, so the original was left in place.")
        os.rename(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        os.unlink(origin)
    except OSError:
        target.unlink(missing_ok=True)  # Two copies would be worse than a move that did not happen.
        raise


def _within(path: Path | str, root: Path | str) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except ValueError:
        return False


def _search_grams(normalized: str) -> str:
    """Distinct character bigrams, plus each run's last character.

    Chinese has no word boundaries and two-character queries are the common
    case, so neither word tokens nor trigrams can answer them. A bigram matches
    any two-character term exactly; a longer term is the AND of its bigrams and
    is confirmed against the full text; one character is a prefix query.
    """
    grams = set()
    for run in _GRAM_RUN.findall(normalized):
        grams.update(run[i:i + 2] for i in range(len(run) - 1))
        grams.add(run[-1])
    return " ".join(grams)


def _match_expression(tokens):
    """Return (FTS5 expression or None, tokens that still need a text check)."""
    clauses, unconfirmed = [], []
    for token in tokens:
        runs = _GRAM_RUN.findall(token)
        if not runs:
            unconfirmed.append(token)
            continue
        if runs != [token] or len(token) > 2:
            unconfirmed.append(token)
        grams = []
        for run in runs:
            grams.extend([f'"{run}"*'] if len(run) == 1 else (f'"{run[i:i + 2]}"' for i in range(len(run) - 1)))
        # A pasted sentence stays within FTS5's expression limits; the text check is exact.
        clauses.extend(grams[:MAX_TOKEN_GRAMS])
    return (" AND ".join(dict.fromkeys(clauses)) or None), unconfirmed


def query_terms(query) -> list[str]:
    return [_normal(t.strip('"\'')) for t in re.split(r"\s+", str(query).strip()) if t.strip('"\'')][:20]


def _pdf_parser_available() -> bool:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def _cancelled(cancel) -> bool:
    if cancel is None:
        return False
    return bool(cancel.is_set() if hasattr(cancel, "is_set") else cancel())


def _friendly_kind(ext: str) -> str:
    if ext == ".pdf":
        return "PDF document"
    if ext in {".doc", ".docx", ".odt"}:
        return "Text document"
    if ext in {".xls", ".xlsx", ".ods", ".csv", ".tsv"}:
        return "Spreadsheet"
    if ext in {".ppt", ".pptx", ".odp", ".key"}:
        return "Presentation"
    if ext in IMAGE_EXTS:
        return "Image"
    if ext in DESIGN_EXTS:
        return "Design file"
    if ext in MEDIA_EXTS:
        return "Audio" if ext in {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".aiff", ".aif"} else "Video"
    if ext in CODE_EXTS:
        return "Code file"
    if ext in TEXT_EXTS:
        return "Plain text"
    if ext in {".zip", ".7z", ".rar", ".tar", ".gz"}:
        return "Archive"
    return "File"


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignore_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignore_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignore_depth = max(0, self.ignore_depth - 1)

    def handle_data(self, data):
        if not self.ignore_depth:
            self.parts.append(data)


_SURROGATES = re.compile("[\ud800-\udfff]")  # PDF text maps can produce these, and they cannot be stored.


def _clean_text(value: str) -> str:
    value = _SURROGATES.sub("", value.replace("\x00", " "))
    return re.sub(r"[ \t\r\f\v]+", " ", value)[:MAX_TEXT_CHARS].strip()


def _decode_text(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if b"\x00" in raw[:2048]:
        # UTF-16 without BOM is common in Windows text exports.
        if raw[:2048].count(b"\x00") > len(raw[:2048]) // 4:
            return raw.decode("utf-16-le", errors="replace")
        raise ValueError("Not recognisable text")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def _natural(name):
    """slide2 before slide10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _sheet_text(root):
    # A cell of type "s" stores a number pointing at a shared string, not a value.
    skipped = {id(value) for cell in root.iter() if _local(cell.tag) == "c" and cell.get("t") == "s" for value in cell}
    return " ".join(node.text or "" for node in root.iter() if _local(node.tag) in {"t", "v"} and id(node) not in skipped)


def _paragraphs(root):
    """Keeps a document's line breaks, so a preview does not read as one long block."""
    lines = ("".join(node.itertext()).strip() for node in root.iter() if _local(node.tag) in {"p", "h"})
    return "\n".join(line for line in lines if line) or " ".join(root.itertext())


def _office_text(path: Path, ext: str) -> str:
    output = []
    chars = total_bytes = 0
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if ext == ".docx":
            selected = [n for n in names if n == "word/document.xml" or re.fullmatch(r"word/(header|footer)\d+\.xml", n)]
        elif ext == ".pptx":
            selected = [n for n in names if re.fullmatch(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml", n)]
        elif ext == ".xlsx":
            # Strings first, then numbers/inline strings; formatting and formulas are not executed.
            selected = [n for n in names if n == "xl/sharedStrings.xml" or re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
        elif ext in {".odt", ".ods", ".odp"}:
            selected = ["content.xml"] if "content.xml" in names else []
        elif ext == ".epub":
            selected = [n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))][:150]
        else:
            return ""
        for name in sorted(selected, key=_natural):
            info = archive.getinfo(name)
            if info.file_size > MAX_XML_BYTES or total_bytes + info.file_size > MAX_XML_TOTAL:
                continue
            raw = archive.read(name)
            total_bytes += len(raw)
            if ext == ".epub":
                parser = _HTMLText()
                parser.feed(_decode_text(raw))
                text = " ".join(parser.parts)
            else:
                root = ET.fromstring(raw)
                if ext == ".xlsx":
                    text = _sheet_text(root)
                else:
                    text = _paragraphs(root)
            output.append(text)
            chars += len(text)
            if chars >= MAX_TEXT_CHARS:
                break
    return _clean_text("\n".join(output))


def _excel_rows(path):
    """The first sheet as rows. Formulas are never run; their saved values are read."""
    with zipfile.ZipFile(path) as book:
        names = book.namelist()
        sheets = sorted((name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)), key=_natural)
        if not sheets or book.getinfo(sheets[0]).file_size > MAX_XML_BYTES:
            return []
        shared = []
        if "xl/sharedStrings.xml" in names and book.getinfo("xl/sharedStrings.xml").file_size <= MAX_XML_BYTES:
            shared = ["".join(node.text or "" for node in item.iter() if _local(node.tag) == "t") for item in ET.fromstring(book.read("xl/sharedStrings.xml"))]
        rows = []
        with book.open(sheets[0]) as sheet:
            for _, node in ET.iterparse(sheet):
                if _local(node.tag) != "row":
                    continue
                cells = {}
                for position, cell in enumerate(node):
                    letters = re.match(r"[A-Z]+", cell.get("r", ""))
                    column = position
                    if letters:
                        column = 0
                        for letter in letters.group():
                            column = column * 26 + ord(letter) - 64
                        column -= 1
                    value = "".join(part.text or "" for part in cell.iter() if _local(part.tag) in {"v", "t"})
                    if cell.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    if column < MAX_PREVIEW_COLUMNS:
                        cells[column] = value
                rows.append([cells.get(column, "") for column in range(max(cells, default=-1) + 1)])
                node.clear()
                if len(rows) >= MAX_PREVIEW_ROWS:
                    break
        return rows


def read_table(path, ext):
    """The first rows of a CSV or Excel sheet as laid out. Empty if it cannot be read that way."""
    try:
        if ext == ".xlsx":
            return _excel_rows(path)
        with open(path, "rb") as stream:
            text = _decode_text(stream.read(MAX_PREVIEW_BYTES))
        first = text.split("\n", 1)[0]
        delimiter = "\t" if ext == ".tsv" else ";" if first.count(";") > first.count(",") else ","
        return [row[:MAX_PREVIEW_COLUMNS] for row in islice(csv.reader(io.StringIO(text), delimiter=delimiter), MAX_PREVIEW_ROWS)]
    except (OSError, ValueError, KeyError, RuntimeError, csv.Error, zipfile.BadZipFile, zlib.error, ET.ParseError):
        return []


def _leads_outside(root, name):
    """A change notice can name a link, or a path through one. Neither may be followed."""
    path = root
    for part in Path(name).parts:
        path = path / part
        if part == os.pardir:
            return True
        try:
            if os.path.islink(path) or _redirects(os.lstat(path)):
                return True
        except OSError:
            return False  # Gone. Nothing there to follow.
    return False


def _change_targets(root, names):
    """Where to look after these paths changed: (folders, trees)."""
    folders, trees = set(), set()
    for name in names:
        if _leads_outside(root, name):
            continue
        path = root / name
        if path.is_dir():
            trees.add(name)  # Moved in whole, its contents raise no notices of their own.
        else:
            folders.add(os.path.dirname(name))
            if not path.exists():
                trees.add(name)  # It may have been a folder.
    return folders, trees


def _in_targets(relative_path, targets):
    folders, trees = targets
    return "" in trees or os.path.dirname(relative_path) in folders or any(relative_path == tree or relative_path.startswith(tree + os.sep) for tree in trees)


def _walk(root, targets, onerror):
    """Every folder under root, or only the ones that changed."""
    if targets is None:
        yield from os.walk(root, followlinks=False, onerror=onerror)
        return
    folders, trees = targets
    for folder in folders:
        if not (root / folder).is_dir():
            continue  # Removed along with the files that were reported.
        found = next(os.walk(root / folder, followlinks=False, onerror=onerror), None)
        if found:
            yield found[0], [], found[2]
    for tree in trees:
        if (root / tree).is_dir():
            yield from os.walk(root / tree, followlinks=False, onerror=onerror)


def extract_text(path: Path, size: int) -> tuple[str, str, str]:
    """Return text, status, readable error. Never execute document content."""
    ext = path.suffix.lower()
    if size > MAX_EXTRACT_BYTES:
        return "", "too_large", TOO_LARGE
    try:
        if ext in TEXT_EXTS or (not ext and size < 1_000_000):
            with path.open("rb") as stream:
                text = _decode_text(stream.read(MAX_EXTRACT_BYTES + 1))
            if ext in {".html", ".htm"}:
                parser = _HTMLText()
                parser.feed(text)
                text = " ".join(parser.parts)
            elif ext == ".rtf":
                text = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]", " ", text)
            return _clean_text(text), "ok", ""
        if ext in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub"}:
            text = _office_text(path, ext)
            return text, "ok" if text else "metadata", "" if text else "No searchable text found."
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return "", "metadata", NO_PDF_PARSER
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted:
                try:
                    unlocked = reader.decrypt("")
                except Exception:
                    unlocked = False
                if not unlocked:
                    return "", "encrypted", "Encrypted PDF. Indexed file details only."
            parts, chars = [], 0
            for page in reader.pages[:100]:
                part = page.extract_text() or ""
                parts.append(part)
                chars += len(part)
                if chars >= MAX_TEXT_CHARS:
                    break
            text = _clean_text("\n".join(parts))
            return text, "ok" if text else "metadata", "" if text else NO_TEXT_LAYER
        return "", "metadata", "Indexed name, path and file details only."
    except (PermissionError, OSError) as exc:
        return "", "unreadable", str(exc)[:240]
    except Exception as exc:
        return "", "error", "Could not read the text of this file."


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY,
        path TEXT NOT NULL,
        path_key TEXT NOT NULL UNIQUE,
        mode TEXT NOT NULL DEFAULT 'index',
        added_at REAL NOT NULL,
        last_scan REAL,
        status TEXT NOT NULL DEFAULT 'ready',
        last_error TEXT NOT NULL DEFAULT '',
        picks TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        path_key TEXT NOT NULL UNIQUE,
        relative_path TEXT NOT NULL,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        ext TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        mtime_ns INTEGER NOT NULL,
        tags TEXT NOT NULL DEFAULT '[]',
        summary TEXT NOT NULL DEFAULT '',
        favorite INTEGER NOT NULL DEFAULT 0,
        missing INTEGER NOT NULL DEFAULT 0,
        archive_path TEXT NOT NULL DEFAULT '',
        content_hash TEXT NOT NULL DEFAULT '',
        extraction_status TEXT NOT NULL DEFAULT 'metadata',
        extraction_error TEXT NOT NULL DEFAULT '',
        name_norm TEXT NOT NULL DEFAULT '',
        path_norm TEXT NOT NULL DEFAULT '',
        tags_norm TEXT NOT NULL DEFAULT '',
        indexed_at REAL NOT NULL,
        version_key TEXT NOT NULL DEFAULT '',
        ocr INTEGER NOT NULL DEFAULT 0,
        last_used REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS file_bodies (
        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        text TEXT NOT NULL DEFAULT '',
        search_text TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS collections (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS collection_files (
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        added_at REAL NOT NULL,
        PRIMARY KEY (collection_id, file_id)
    );
    CREATE TABLE IF NOT EXISTS file_links (
        low INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        high INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        added_at REAL NOT NULL,
        PRIMARY KEY (low, high),
        CHECK (low < high)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS file_links_high ON file_links(high);
    CREATE TABLE IF NOT EXISTS moved_files (
        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        original TEXT NOT NULL,
        moved_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS collection_files_file ON collection_files(file_id);
    CREATE INDEX IF NOT EXISTS files_source ON files(source_id);
    CREATE INDEX IF NOT EXISTS files_used ON files(last_used DESC) WHERE last_used>0;
    CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime DESC);
    CREATE INDEX IF NOT EXISTS files_name ON files(name COLLATE NOCASE);
    CREATE INDEX IF NOT EXISTS files_size ON files(size DESC);
    CREATE INDEX IF NOT EXISTS files_ext ON files(ext, mtime DESC);
    CREATE INDEX IF NOT EXISTS files_version ON files(version_key) WHERE version_key<>'';
    CREATE INDEX IF NOT EXISTS files_ocr ON files(mtime DESC) WHERE ocr=0;
"""


class ArchiveCore:
    def __init__(self, data_dir):
        self.data_dir = _assert_local_path(data_dir, check_links=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "archive.sqlite3"
        self.vault_dir = self.data_dir / "vault"
        self._write_lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._fts = False
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS file_search USING fts5(grams, tokenize='unicode61 remove_diacritics 0', detail=none)")
                self._fts = True
            except sqlite3.OperationalError:
                pass  # A build without FTS5 still searches, by reading the text.
            self._follow_move(db)

    def _follow_move(self, db):
        """The archive folder can move, and the saved copies inside it go too."""
        row = db.execute("SELECT archive_path FROM files WHERE archive_path<>'' LIMIT 1").fetchone()
        mark = os.sep + self.vault_dir.name + os.sep
        old = row["archive_path"].rpartition(mark)[0] if row else ""
        if old and old != str(self.data_dir):
            db.execute("UPDATE files SET archive_path=?||substr(archive_path,?) WHERE substr(archive_path,1,?)=?",
                       (str(self.data_dir), len(old) + 1, len(old), old))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=20000")
        # With WAL this cannot corrupt the database; a rescan recovers any lost tail.
        db.execute("PRAGMA synchronous=NORMAL")
        # Deleted text is overwritten, not just unlinked. Some SQLite builds default to this; Python's on Windows does not.
        db.execute("PRAGMA secure_delete=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _file_dict(row):
        if row is None:
            return None
        result = dict(row)
        try:
            result["tags"] = json.loads(result.get("tags", "[]"))
        except (ValueError, TypeError):
            result["tags"] = []
        result["favorite"] = bool(result["favorite"])
        result["missing"] = bool(result["missing"])
        return result

    def _write_search_index(self, db, file_id, search_text):
        if self._fts:
            db.execute("DELETE FROM file_search WHERE rowid=?", (file_id,))
            db.execute("INSERT INTO file_search(rowid,grams) VALUES(?,?)", (file_id, _search_grams(search_text)))

    def add_source(self, path, mode="index"):
        if mode not in {"index", "copy"}:
            raise ValueError("Source mode must be index or copy")
        folder = _assert_local_path(path, check_links=True)
        if not folder.is_dir():
            raise ValueError("Choose a folder that exists and can be read.")
        if _within(folder, self.data_dir):
            raise ValueError("The archive data folder cannot be a source.")
        key = _path_key(folder)
        with self._write_lock, self._connect() as db:
            rows = db.execute("SELECT * FROM sources").fetchall()
            for row in rows:
                # Single files handed over do not claim their whole folder.
                if row["path_key"] != key and not row["picks"] and (_within(folder, row["path"]) or _within(row["path"], folder)):
                    raise ValueError("This folder overlaps a source you already added. Choose folders that do not contain each other.")
            same = next((row for row in rows if row["path_key"] == key), None)
            if same:
                db.execute("UPDATE sources SET mode=?,picks='' WHERE id=?", (mode, same["id"]))
                source_id = same["id"]
            else:
                source_id = db.execute(
                    "INSERT INTO sources(path,path_key,mode,added_at) VALUES(?,?,?,?)",
                    (str(folder), key, mode, time.time()),
                ).lastrowid
            for row in rows:
                if row["picks"] and row["path_key"] != key and _within(row["path"], folder):
                    self._absorb_picks(db, row, source_id, folder)
            return dict(db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())

    def _absorb_picks(self, db, picked, source_id, folder):
        """Files handed over earlier now belong to the folder around them, and stay in their collections."""
        for row in db.execute("SELECT f.id,f.path,f.name,f.tags,COALESCE(b.text,'') AS text FROM files f LEFT JOIN file_bodies b ON b.file_id=f.id WHERE f.source_id=?", (picked["id"],)).fetchall():
            relative = os.path.relpath(row["path"], folder)
            db.execute("UPDATE files SET source_id=?,relative_path=?,path_norm=?,version_key=? WHERE id=?",
                       (source_id, relative, _normal(relative), _family(source_id, relative, row["name"]), row["id"]))
            search_text = self._search_value(row["name"], relative, json.loads(row["tags"]), row["text"])
            db.execute("UPDATE file_bodies SET search_text=? WHERE file_id=?", (search_text, row["id"]))
            self._write_search_index(db, row["id"], search_text)
        db.execute("DELETE FROM sources WHERE id=?", (picked["id"],))

    def adopt(self, paths, mode="index"):
        """Take in what someone handed over, and nothing else from around it.

        A folder becomes a source. A single file is remembered on its own, so
        the rest of its folder is never read. ``copy`` also keeps a saved copy.
        Returns the ``only`` for scan_all.
        """
        only, problem = {}, None
        for raw in paths:
            try:
                path = _assert_local_path(raw, check_links=True)
                covering = next((row for row in self.sources() if not row["picks"] and _within(path, row["path"])), None)
                if covering:
                    inside = os.path.relpath(path, covering["path"])
                    if any(part.casefold() in IGNORED_DIRS for part in Path(inside).parts):
                        raise ValueError("This is in a folder that is never indexed, such as node_modules.")
                    only.setdefault(covering["path"], []).append("" if inside == os.curdir else inside)
                elif path.is_dir():
                    only.setdefault(self.add_source(path, mode)["path"], []).append("")
                elif path.is_file():
                    only.setdefault(self._pick_file(path, mode), []).append(path.name)
            except (ValueError, OSError) as exc:
                problem = exc  # One bad item does not lose the rest of the armful.
        if problem and not only:
            raise problem
        return only

    def _pick_file(self, path, mode="index"):
        if _within(path, self.data_dir):
            raise ValueError("The archive data folder cannot be a source.")
        folder, key = path.parent, _path_key(path.parent)
        with self._write_lock, self._connect() as db:
            row = db.execute("SELECT * FROM sources WHERE path_key=?", (key,)).fetchone()
            if row is None:
                db.execute("INSERT INTO sources(path,path_key,mode,added_at,picks) VALUES(?,?,?,?,?)",
                           (str(folder), key, mode, time.time(), json.dumps([path.name], ensure_ascii=False)))
                return str(folder)
            picks = set(json.loads(row["picks"])) | {path.name}
            # Keeping copies is the safer promise, so a later pick never quietly drops it.
            db.execute("UPDATE sources SET picks=?,mode=? WHERE id=?",
                       (json.dumps(sorted(picks), ensure_ascii=False), "copy" if mode == "copy" else row["mode"], row["id"]))
            return row["path"]

    def files_at(self, paths):
        """Everything in the archive at these paths, or inside these folders."""
        found = []
        with self._connect() as db:
            for path in paths:
                try:
                    key = _path_key(_assert_local_path(path))
                except ValueError:
                    continue
                found += [dict(row) for row in db.execute(
                    f"{self._ROW_WITH_BODY} WHERE f.path_key=? OR substr(f.path_key,1,?)=?", (key, len(key) + 1, key + os.sep))]
        return found

    def sources(self):
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY id")]

    def set_source_mode(self, source_id, mode):
        """Change future scan behavior without deleting existing archive copies."""
        if mode not in {"index", "copy"}:
            raise ValueError("Source mode must be index or copy")
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError("Wait for the scan to finish before changing the source mode.")
        try:
            with self._write_lock, self._connect() as db:
                row = db.execute("SELECT * FROM sources WHERE id=?", (int(source_id),)).fetchone()
                if row is None:
                    raise ValueError("This source no longer exists.")
                db.execute("UPDATE sources SET mode=? WHERE id=?", (mode, int(source_id)))
                result = dict(row)
                result["mode"] = mode
                return result
        finally:
            self._scan_lock.release()

    def remove_source(self, source_id):
        """Remove only catalogue records. Source files and archive copies stay intact."""
        if self._scan_lock.locked():
            raise RuntimeError("Wait for the scan to finish before removing a source.")
        with self._write_lock, self._connect() as db:
            if self._fts:
                db.execute("DELETE FROM file_search WHERE rowid IN (SELECT id FROM files WHERE source_id=?)", (int(source_id),))
            db.execute("DELETE FROM sources WHERE id=?", (int(source_id),))
            if self._fts:
                # A removed row only leaves a tombstone in the index until it is merged away.
                db.execute("INSERT INTO file_search(file_search) VALUES('optimize')")
        with self._connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()  # The log still holds the old pages.

    def _copy_to_vault(self, path: Path, cancel=None):
        temporary_dir = self.vault_dir / ".tmp"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        temporary = temporary_dir / (uuid.uuid4().hex + ".part")
        before = path.stat()
        digest = hashlib.sha256()
        try:
            with path.open("rb") as original, temporary.open("xb") as target:
                while True:
                    if _cancelled(cancel):
                        raise ScanCancelled()
                    block = original.read(1024 * 1024)
                    if not block:
                        break
                    target.write(block)
                    digest.update(block)
                target.flush()
                os.fsync(target.fileno())
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise OSError("The file changed while copying. It will be retried on the next scan.")
            content_hash = digest.hexdigest()
            ext = path.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,12}", ext):
                ext = ""
            final_dir = self.vault_dir / content_hash[:2]
            final_dir.mkdir(parents=True, exist_ok=True)
            destination = final_dir / (content_hash + ext)
            damaged_path = None
            if destination.exists():
                verified = hashlib.sha256()
                if destination.stat().st_size == before.st_size:
                    with destination.open("rb") as stored:
                        while True:
                            if _cancelled(cancel):
                                raise ScanCancelled()
                            chunk = stored.read(1024 * 1024)
                            if not chunk:
                                break
                            verified.update(chunk)
                if verified.hexdigest() == content_hash:
                    temporary.unlink()
                    return str(destination), content_hash
                # Preserve a damaged or externally edited vault object exactly as it is.
                # A fresh verified copy gets a new path; catalogue references are repaired.
                damaged_path = str(destination)
                with self._connect() as db:
                    previous = db.execute(
                        "SELECT DISTINCT archive_path FROM files WHERE content_hash=? AND archive_path<>''",
                        (content_hash,),
                    ).fetchall()
                for row in previous:
                    candidate = Path(row["archive_path"])
                    if not _within(candidate, self.vault_dir) or candidate == destination or not candidate.is_file():
                        continue
                    candidate_hash = hashlib.sha256()
                    if candidate.stat().st_size != before.st_size:
                        continue
                    with candidate.open("rb") as stored:
                        while True:
                            if _cancelled(cancel):
                                raise ScanCancelled()
                            chunk = stored.read(1024 * 1024)
                            if not chunk:
                                break
                            candidate_hash.update(chunk)
                    if candidate_hash.hexdigest() == content_hash:
                        destination = candidate
                        temporary.unlink()
                        break
                else:
                    destination = final_dir / (content_hash + ".verified-" + uuid.uuid4().hex[:12] + ext)
            if temporary.exists():
                os.replace(temporary, destination)
                try:
                    shutil.copystat(path, destination)
                except OSError:
                    pass
            if damaged_path:
                with self._write_lock, self._connect() as db:
                    db.execute(
                        "UPDATE files SET archive_path=? WHERE archive_path=? AND content_hash=?",
                        (str(destination), damaged_path, content_hash),
                    )
            return str(destination), content_hash
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _search_value(name, relative_path, tags, text):
        return _normal("\n".join((name, relative_path, " ".join(tags), text)))

    def _flush_scan(self, pending, created=None):
        """Write extracted rows in one transaction and return their ids.

        Users may edit labels while extraction runs in the background, so those
        fields are re-read here, inside the final write transaction.
        """
        written = []
        if not pending:
            return written
        with self._write_lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in pending:
                db.execute("SAVEPOINT row")
                try:
                    current = db.execute("SELECT tags FROM files WHERE path_key=?", (item["key"],)).fetchone()
                    tags = json.loads(current["tags"]) if current else []
                    search_text = self._search_value(item["name"], item["relative_path"], tags, item["text"])
                    db.execute("""
                        INSERT INTO files(source_id,path,path_key,relative_path,name,kind,ext,size,mtime,mtime_ns,tags,summary,archive_path,content_hash,extraction_status,extraction_error,name_norm,path_norm,tags_norm,indexed_at,version_key,ocr)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(path_key) DO UPDATE SET
                            relative_path=excluded.relative_path,name=excluded.name,
                            kind=excluded.kind,ext=excluded.ext,
                            size=excluded.size,mtime=excluded.mtime,mtime_ns=excluded.mtime_ns,
                            summary=excluded.summary,archive_path=excluded.archive_path,
                            content_hash=excluded.content_hash,extraction_status=excluded.extraction_status,
                            extraction_error=excluded.extraction_error,name_norm=excluded.name_norm,
                            path_norm=excluded.path_norm,indexed_at=excluded.indexed_at,
                            version_key=excluded.version_key,ocr=excluded.ocr,missing=0
                    """, (item["source_id"], item["path"], item["key"], item["relative_path"], item["name"],
                          _friendly_kind(item["ext"]), item["ext"], item["size"], item["mtime"], item["mtime_ns"],
                          json.dumps(tags, ensure_ascii=False), item["summary"], item["archive_path"], item["content_hash"],
                          item["status"], item["error"], _normal(item["name"]), _normal(item["relative_path"]),
                          _normal(" ".join(tags)), item["now"], _family(item["source_id"], item["relative_path"], item["name"]), item["ocr"]))
                    file_id = db.execute("SELECT id FROM files WHERE path_key=?", (item["key"],)).fetchone()[0]
                    db.execute("INSERT INTO file_bodies(file_id,text,search_text) VALUES(?,?,?) ON CONFLICT(file_id) DO UPDATE SET text=excluded.text,search_text=excluded.search_text",
                               (file_id, item["text"], search_text))
                    self._write_search_index(db, file_id, search_text)
                    written.append(file_id)
                    if created is not None and current is None:
                        created.append(file_id)
                    db.execute("RELEASE row")
                except (OverflowError, UnicodeError):
                    # One file that cannot be stored, such as a name no text encoding can spell, must not stop the rest.
                    db.execute("ROLLBACK TO row")
                    db.execute("RELEASE row")
        pending.clear()
        return written

    def _carry_moved(self, created, vanished=()):
        """A renamed or moved file looks like one gone and one new.

        Size and saved time together tie them, so tags, favorites and recent use
        follow the file. If two files match equally, neither is guessed at. The
        two halves can arrive in either order, in different scans.
        """
        with self._write_lock, self._connect() as db:
            gone = {}
            for row in db.execute("SELECT id,size,mtime_ns,tags,tags_norm,favorite,last_used,archive_path,content_hash FROM files WHERE missing=1 AND size>0"):
                gone.setdefault((row["size"], row["mtime_ns"]), []).append(row)
            if not gone:
                return set()
            candidates = list(created)
            for file_id in vanished:
                # Only labels are worth this, since a twin that was always there could look the same.
                old = db.execute("SELECT size,mtime_ns,indexed_at FROM files WHERE id=? AND (tags<>'[]' OR favorite=1 OR last_used>0"
                                 " OR id IN (SELECT file_id FROM collection_files))", (file_id,)).fetchone()
                if old:
                    candidates += [row["id"] for row in db.execute(
                        "SELECT id FROM files WHERE missing=0 AND size=? AND mtime_ns=? AND indexed_at>? AND tags='[]' AND favorite=0 AND last_used=0"
                        " AND id NOT IN (SELECT file_id FROM collection_files)", (old["size"], old["mtime_ns"], old["indexed_at"]))]
            candidates = list(dict.fromkeys(candidates))
            arrived = {}
            for start in range(0, len(candidates), 500):
                batch = candidates[start:start + 500]
                for row in db.execute(f"SELECT id,name,relative_path,size,mtime_ns,archive_path FROM files WHERE id IN ({','.join('?' * len(batch))})", batch):
                    arrived.setdefault((row["size"], row["mtime_ns"]), []).append(row)
            carried = set()
            for key, rows in arrived.items():
                if len(rows) != 1 or len(gone.get(key, ())) != 1:
                    continue
                new, old = rows[0], gone[key][0]
                archive = new["archive_path"] or old["archive_path"]
                db.execute("UPDATE files SET tags=?,tags_norm=?,favorite=?,last_used=?,archive_path=?,content_hash=? WHERE id=?",
                           (old["tags"], old["tags_norm"], old["favorite"], old["last_used"], archive, old["content_hash"], new["id"]))
                body = db.execute("SELECT text FROM file_bodies WHERE file_id=?", (new["id"],)).fetchone()
                search_text = self._search_value(new["name"], new["relative_path"], json.loads(old["tags"]), body["text"] if body else "")
                db.execute("UPDATE file_bodies SET search_text=? WHERE file_id=?", (search_text, new["id"]))
                self._write_search_index(db, new["id"], search_text)
                db.execute("UPDATE OR IGNORE collection_files SET file_id=? WHERE file_id=?", (new["id"], old["id"]))
                db.execute("UPDATE OR IGNORE moved_files SET file_id=? WHERE file_id=?", (new["id"], old["id"]))
                for link in db.execute("SELECT low,high FROM file_links WHERE low=? OR high=?", (old["id"], old["id"])).fetchall():
                    other = link["high"] if link["low"] == old["id"] else link["low"]
                    if other != new["id"]:
                        db.execute("INSERT OR IGNORE INTO file_links(low,high,added_at) VALUES(?,?,?)", (min(other, new["id"]), max(other, new["id"]), time.time()))
                if self._fts:
                    db.execute("DELETE FROM file_search WHERE rowid=?", (old["id"],))
                db.execute("DELETE FROM files WHERE id=?", (old["id"],))
                carried.add(new["id"])
            return carried

    def _finish_scan(self, source_id, seen, complete, looked_at=None):
        """Returns the files that have just gone missing, and how many came back."""
        vanished = []
        with self._write_lock, self._connect() as db:
            db.execute("CREATE TEMP TABLE seen_files(id INTEGER PRIMARY KEY)")
            db.executemany("INSERT OR IGNORE INTO seen_files(id) VALUES(?)", ((file_id,) for file_id in seen))
            back = db.execute("UPDATE files SET missing=0 WHERE source_id=? AND missing=1 AND id IN (SELECT id FROM seen_files)", (source_id,)).rowcount
            if complete and looked_at is None:
                vanished = [row[0] for row in db.execute("SELECT id FROM files WHERE source_id=? AND missing=0 AND id NOT IN (SELECT id FROM seen_files)", (source_id,))]
            elif complete:
                unseen = {row[0] for row in db.execute("SELECT id FROM files WHERE source_id=? AND missing=0 AND id NOT IN (SELECT id FROM seen_files)", (source_id,))}
                vanished = [file_id for file_id in looked_at if file_id in unseen]
            db.executemany("UPDATE files SET missing=1 WHERE id=?", ((file_id,) for file_id in vanished))
        return vanished, back

    @staticmethod
    def _examine(path, existing, mode, pdf_available, cancel):
        """Everything that waits on the disk, so a virus scanner can check several files at once."""
        if _cancelled(cancel):
            return None
        try:
            stat = path.stat()
            if not path.is_file():
                return None
            placeholder = _cloud_placeholder(stat)
            # Freeing up space keeps the size and date, so what was read before still holds.
            changed = not existing or existing["size"] != stat.st_size or existing["mtime_ns"] != stat.st_mtime_ns or (
                existing["extraction_status"] == "cloud_placeholder" and not placeholder)
            copy_needed = mode == "copy" and not placeholder and (changed or not existing["archive_path"] or not Path(existing["archive_path"]).is_file())
            # Neither of these changes on disk: a PDF indexed before the parser
            # was installed, and a file another program held open during a scan.
            retry = bool(existing) and not changed and not placeholder and (
                existing["extraction_status"] == "unreadable"
                or (pdf_available and existing["extraction_error"] == NO_PDF_PARSER))
            text, status, error = None, "", ""
            if placeholder and changed:
                text, status, error = "", "cloud_placeholder", CLOUD_PLACEHOLDER
            elif changed or retry:
                text, status, error = extract_text(path, stat.st_size)
            return {"stat": stat, "changed": changed, "copy_needed": copy_needed, "retry": retry, "text": text, "status": status, "error": error}
        except Exception as exc:
            return exc

    def scan_all(self, progress=None, cancel=None, only=None):
        """Bring the archive up to date. ``only`` limits it to {source path: changed paths}."""
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError("A scan is already running.")
        result = {"phase": "starting", "name": "", "scanned": 0, "indexed": 0, "skipped": 0, "errors": 0, "moved": 0, "arrived": [], "source_id": None, "cancelled": False, "state_changes": 0}
        last_emit = [0.0]
        created, vanished = [], []
        pool = ThreadPoolExecutor(SCAN_WORKERS, thread_name_prefix="scan")

        def emit(phase=None, name=None):
            if phase is not None:
                result["phase"] = phase
            if name is not None:
                result["name"] = name
            # One Qt signal per file would flood the UI thread on a large library.
            moment = time.monotonic()
            if progress and (phase is not None or moment - last_emit[0] >= PROGRESS_INTERVAL):
                last_emit[0] = moment
                progress(dict(result))

        try:
            pdf_available = _pdf_parser_available()
            for source in self.sources():
                if _cancelled(cancel):
                    result["cancelled"] = True
                    break
                if only is not None and source["path"] not in only:
                    continue
                source_id = source["id"]
                result["source_id"] = source_id
                try:
                    root = _assert_local_path(source["path"], check_links=True)
                except (ValueError, OSError) as exc:
                    with self._write_lock, self._connect() as db:
                        db.execute("UPDATE sources SET status='unavailable',last_error=? WHERE id=?", (str(exc)[:240], source_id))
                    result["errors"] += 1
                    result["state_changes"] += source["status"] != "unavailable"
                    emit("unavailable", Path(source["path"]).name)
                    continue
                if not root.is_dir():
                    with self._write_lock, self._connect() as db:
                        db.execute("UPDATE sources SET status='unavailable',last_error=? WHERE id=?", ("Source unavailable. Existing records were kept.", source_id))
                    result["errors"] += 1
                    result["state_changes"] += source["status"] != "unavailable"
                    emit("unavailable", root.name)
                    continue
                walk_errors = []
                targets = None if only is None else _change_targets(root, only[source["path"]])
                picks = set(json.loads(source["picks"])) if source["picks"] else None
                if picks is not None:
                    targets = ({""}, set())  # A file of the same name further down was not handed over.
                    picks = {os.path.normcase(name) for name in picks}
                with self._write_lock, self._connect() as db:
                    db.execute("UPDATE sources SET status='scanning',last_error='' WHERE id=?", (source_id,))
                    # Only what the unchanged check needs; bodies are read on demand.
                    known = {row["path_key"]: row for row in db.execute(
                        "SELECT id,path_key,relative_path,size,mtime_ns,archive_path,content_hash,extraction_status,extraction_error,ocr FROM files WHERE source_id=?", (source_id,))}
                seen, pending = set(), []
                last_flush = time.monotonic()
                emit("scanning", root.name)
                for directory, dirnames, filenames in _walk(root, targets, walk_errors.append):
                    if _cancelled(cancel):
                        result["cancelled"] = True
                        break
                    current = Path(directory)
                    allowed = []
                    for name in dirnames:
                        child = current / name
                        is_junction = getattr(os.path, "isjunction", lambda p: False)
                        if name.casefold() in IGNORED_DIRS or child.is_symlink() or is_junction(str(child)) or _within(child, self.data_dir):
                            continue
                        allowed.append(name)
                    dirnames[:] = allowed
                    entries = []
                    for name in filenames:
                        path = current / name
                        if name.casefold() in IGNORED_FILES or name.endswith(".icloud") or path.is_symlink() or _within(path, self.data_dir) or name.startswith("~$"):
                            continue
                        if picks is not None and os.path.normcase(name) not in picks:
                            continue
                        entries.append((name, path, known.get(_path_key(path))))
                    for start in range(0, len(entries), SCAN_BATCH):
                        batch = entries[start:start + SCAN_BATCH]
                        examined = pool.map(lambda entry: self._examine(entry[1], entry[2], source["mode"], pdf_available, cancel), batch)
                        for (name, path, existing), found in zip(batch, examined):
                            if _cancelled(cancel):
                                result["cancelled"] = True
                                break
                            result["scanned"] += 1
                            result["name"] = name
                            try:
                                if existing:
                                    seen.add(existing["id"])
                                if found is None:
                                    continue
                                if isinstance(found, Exception):
                                    raise found
                                stat, changed, copy_needed = found["stat"], found["changed"], found["copy_needed"]
                                if not changed and not copy_needed and not found["retry"]:
                                    result["skipped"] += 1
                                    emit()
                                    continue
                                if found["text"] is not None:
                                    text, extraction_status, extraction_error = found["text"], found["status"], found["error"]
                                else:
                                    with self._connect() as db:
                                        stored = db.execute("SELECT text FROM file_bodies WHERE file_id=?", (existing["id"],)).fetchone()
                                    text, extraction_status, extraction_error = stored["text"] if stored else "", existing["extraction_status"], existing["extraction_error"]
                                if extraction_status in {"error", "unreadable"}:
                                    result["errors"] += 1
                                if _cancelled(cancel):
                                    result["cancelled"] = True
                                    break
                                archive_path = existing["archive_path"] if existing else ""
                                content_hash = existing["content_hash"] if existing else ""
                                if changed and source["mode"] == "index":
                                    archive_path, content_hash = "", ""
                                if copy_needed:
                                    try:
                                        archive_path, content_hash = self._copy_to_vault(path, cancel)
                                    except ScanCancelled:
                                        result["cancelled"] = True
                                        break
                                    except OSError as exc:
                                        result["errors"] += 1
                                        # Keep the row retryable: a changed failed copy cannot inherit
                                        # the previous version's archive path as if it were current.
                                        archive_path, content_hash = "", ""
                                        extraction_error = (extraction_error + " Archive copy failed: " + str(exc)).strip()[:500]
                                relative_path = str(path.relative_to(root))
                                summary = re.sub(r"\s+", " ", text).strip()[:170]
                                if not summary:
                                    summary = extraction_error
                                pending.append({
                                    "key": _path_key(path), "source_id": source_id, "path": str(path), "relative_path": relative_path,
                                    "name": name, "ext": path.suffix.lower(), "size": stat.st_size, "mtime": stat.st_mtime,
                                    "mtime_ns": stat.st_mtime_ns, "text": text, "summary": summary,
                                    "archive_path": archive_path, "content_hash": content_hash,
                                    "status": extraction_status, "error": extraction_error, "now": time.time(),
                                    # Words already read from an unchanged file stay read.
                                    "ocr": existing["ocr"] if existing and not changed and not found["retry"] else 0,
                                })
                                result["indexed"] += 1
                                if len(pending) >= SCAN_FLUSH_ROWS or time.monotonic() - last_flush >= SCAN_FLUSH_SECONDS:
                                    seen.update(self._flush_scan(pending, created))
                                    last_flush = time.monotonic()
                            except Exception as exc:
                                result["errors"] += 1
                                # File-level failures do not abort the rest of a folder.
                            emit()
                        if result["cancelled"]:
                            break
                    if result["cancelled"]:
                        break
                seen.update(self._flush_scan(pending, created))
                if walk_errors:
                    result["errors"] += len(walk_errors)
                # Nothing in a folder Windows cannot reach was ever indexed, so it
                # gives no reason to hold back marking other files as missing.
                unread = [error for error in walk_errors if not _path_too_long(error)]
                complete = not result["cancelled"] and not unread
                looked_at = None if targets is None else [row["id"] for row in known.values() if _in_targets(row["relative_path"], targets)]
                gone, back = self._finish_scan(source_id, seen, complete, looked_at)
                vanished += gone
                result["state_changes"] += len(gone) + back
                with self._write_lock, self._connect() as db:
                    if result["cancelled"]:
                        db.execute("UPDATE sources SET status='cancelled',last_error=? WHERE id=?", ("Scan paused. It will continue next time.", source_id))
                    elif unread:
                        db.execute("UPDATE sources SET status='partial',last_scan=?,last_error=? WHERE id=?", (time.time(), "Some folders could not be read. Existing records were not marked missing.", source_id))
                    else:
                        db.execute("UPDATE sources SET status='ready',last_scan=?,last_error=? WHERE id=?", (time.time(), TOO_DEEP if walk_errors else "", source_id))
                if result["cancelled"]:
                    break
            if created or vanished:
                carried = self._carry_moved(created, vanished)
                result["moved"] = len(carried)
                result["arrived"] = [file_id for file_id in created if file_id not in carried]
            emit("cancelled" if result["cancelled"] else "complete", "")
            return dict(result)
        finally:
            pool.shutdown()
            self._scan_lock.release()

    _ROW_WITH_BODY = "SELECT f.*,COALESCE(b.text,'') AS text,COALESCE(b.search_text,'') AS search_text FROM files f LEFT JOIN file_bodies b ON b.file_id=f.id"
    _ORDERS = {
        "name": "f.name COLLATE NOCASE ASC", "size": "f.size DESC", "oldest": "f.mtime ASC", "recent": "f.mtime DESC", "used": "f.last_used DESC",
    }

    def _where(self, tokens, view, source_id, folder, collection, exts, after, before):
        """Filters run in SQL, so a date or type condition is not limited by a candidate cap."""
        clauses, params = [], []
        if view == "favorites":
            clauses.append("f.favorite=1")
        elif view == "used":
            clauses.append("f.last_used>0")
        if collection:
            clauses.append("f.id IN (SELECT file_id FROM collection_files WHERE collection_id=?)")
            params.append(collection)
        if folder:
            clauses.append("substr(f.relative_path,1,?)=?")
            params.extend((len(folder) + 1, folder + os.sep))
        if source_id is not None:
            clauses.append("f.source_id=?")
            params.append(int(source_id))
        if exts is not None:
            marks = ",".join("?" * len(exts))
            clauses.append(f"f.ext IN ({marks})")
            params.extend(exts)
        if after is not None:
            clauses.append("f.mtime>=?")
            params.append(after)
        if before is not None:
            clauses.append("f.mtime<?")
            params.append(before)
        body_needed = False
        if tokens:
            expression, unconfirmed = _match_expression(tokens) if self._fts else (None, tokens)
            if expression:
                clauses.append("f.id IN (SELECT rowid FROM file_search WHERE file_search MATCH ?)")
                params.append(expression)
            for token in unconfirmed:
                body_needed = True
                clauses.append("instr(b.search_text,?)>0")
                params.append(token)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        join = " JOIN file_bodies b ON b.file_id=f.id" if body_needed else ""
        return join + where, params

    def _rank_order(self, tokens):
        """Filename, then tags, then body, then location; matching filter_and_rank's tiers."""
        def hits(column):
            return "+".join(f"(instr({column},?)>0)" for _ in tokens)

        elsewhere = "+".join("(instr(f.name_norm,?)=0 AND instr(f.tags_norm,?)=0 AND instr(f.path_norm,?)=0)" for _ in tokens)
        order = [
            f"({hits('f.name_norm')})={len(tokens)} DESC", f"({hits('f.name_norm')}) DESC",
            f"({hits('f.tags_norm')}) DESC", f"({elsewhere}) DESC", f"({hits('f.path_norm')}) DESC",
        ]
        params = [*tokens, *tokens, *tokens]
        params.extend(token for token in tokens for _ in range(3))
        params.extend(tokens)
        return ",".join(order) + ",f.mtime DESC,f.id DESC", params

    def search_page(self, query="", view="all", sort="recent", exts=None, after=None, before=None,
                    offset=0, limit=120, source_id=None, folder=None, collection=None, relevance=True):
        """One page of results plus the true total. Bodies stay out of the result."""
        tokens = query_terms(query)
        condition, params = self._where(tokens, view, source_id, folder, collection, exts, after, before)
        with self._connect() as db:
            total = db.execute("SELECT COUNT(*) FROM files f" + condition, params).fetchone()[0]
            if tokens and relevance:
                order, order_params = self._rank_order(tokens)
            else:
                order, order_params = self._ORDERS.get(sort, "f.mtime DESC") + ",f.id DESC", []
            # Two steps, so sorting every match never reads a document body.
            ids = [row[0] for row in db.execute(
                f"SELECT f.id FROM files f{condition} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, *order_params, max(1, int(limit)), max(0, int(offset))),
            )]
            marks = ",".join("?" * len(ids))
            found = {row["id"]: row for row in db.execute(
                f"SELECT {_LIST_SELECT},substr(b.text,1,?) AS text FROM files f"
                f" LEFT JOIN file_bodies b ON b.file_id=f.id WHERE f.id IN ({marks})", (COVER_TEXT_CHARS, *ids))}
        rows = []
        for file_id in ids:
            if file_id not in found:
                continue  # Removed by a scan between the two queries.
            item = self._file_dict(found[file_id])
            item["text"] = item["text"] or ""
            rows.append(item)
        return {"total": total, "rows": rows, "offset": max(0, int(offset)), "tokens": tokens}

    def search(self, query="", view="all", sort="recent", limit=500, source_id=None):
        """Full rows, for callers that want bodies. The interface uses search_page."""
        page = self.search_page(query, view, sort, limit=min(int(limit), 10_000), source_id=source_id, relevance=False)
        return [self.get_file(row["id"]) for row in page["rows"]]

    def bodies(self, file_ids):
        """Full text for one result page, so snippets can come from the real match."""
        found = {}
        ids = [int(file_id) for file_id in file_ids]
        with self._connect() as db:
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                marks = ",".join("?" * len(chunk))
                for row in db.execute(f"SELECT file_id,text FROM file_bodies WHERE file_id IN ({marks})", chunk):
                    found[row["file_id"]] = row["text"]
        return found

    def get_file(self, file_id):
        with self._connect() as db:
            return self._file_dict(db.execute(self._ROW_WITH_BODY + " WHERE f.id=?", (int(file_id),)).fetchone())

    def stats(self):
        with self._connect() as db:
            totals = db.execute("SELECT COUNT(*) AS total,COALESCE(SUM(size),0) AS size,COALESCE(SUM(favorite),0) AS favorites,COALESCE(SUM(missing),0) AS missing,COALESCE(SUM(last_used>0),0) AS used FROM files").fetchone()
            sources = db.execute("SELECT COUNT(*) AS n,MAX(last_scan) AS last_scan FROM sources").fetchone()
            return {**dict(totals), "sources": sources["n"], "last_scan": sources["last_scan"]}

    def ext_counts(self):
        with self._connect() as db:
            return {row["ext"]: row["n"] for row in db.execute("SELECT ext,COUNT(*) AS n FROM files GROUP BY ext")}

    def folders(self, limit=30):
        """Where files really are: each added folder, then the largest folders inside them."""
        with self._connect() as db:
            sources = [dict(row) for row in db.execute(
                "SELECT s.id AS source_id,s.path,COUNT(f.id) AS count FROM sources s LEFT JOIN files f ON f.source_id=s.id GROUP BY s.id ORDER BY count DESC")]
            inside = [dict(row) for row in db.execute(
                "SELECT source_id,substr(relative_path,1,instr(relative_path,?)-1) AS folder,COUNT(*) AS count FROM files"
                " WHERE instr(relative_path,?)>0 GROUP BY source_id,folder ORDER BY count DESC LIMIT ?", (os.sep, os.sep, limit))]
        # A single added folder is already All files, so only its contents say anything.
        listed = [{"source_id": row["source_id"], "folder": "", "name": Path(row["path"]).name or row["path"], "count": row["count"]} for row in sources] if len(sources) > 1 else []
        listed += [{**row, "name": row["folder"]} for row in inside]
        return listed[:limit]

    def subfolders(self, source_id, inside, limit=30):
        """The folders directly inside one folder, largest first."""
        prefix = inside + os.sep if inside else ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT substr(rest,1,instr(rest,?)-1) AS name,COUNT(*) AS count FROM"
                " (SELECT substr(relative_path,?) AS rest FROM files WHERE source_id=? AND substr(relative_path,1,?)=?)"
                " WHERE instr(rest,?)>0 GROUP BY name ORDER BY count DESC LIMIT ?",
                (os.sep, len(prefix) + 1, source_id, len(prefix), prefix, os.sep, limit)).fetchall()
        return [{"source_id": source_id, "folder": prefix + row["name"], "name": row["name"], "count": row["count"]} for row in rows]

    def collections(self):
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT c.id,c.name,COUNT(m.file_id) AS count FROM collections c"
                " LEFT JOIN collection_files m ON m.collection_id=c.id GROUP BY c.id ORDER BY c.created_at,c.id")]

    def _collection_name(self, name):
        name = str(name).strip()[:60]
        if not name:
            raise ValueError("Give the collection a name.")
        return name

    def create_collection(self, name):
        try:
            with self._write_lock, self._connect() as db:
                return db.execute("INSERT INTO collections(name,created_at) VALUES(?,?)", (self._collection_name(name), time.time())).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("You already have a collection with this name.") from None

    def rename_collection(self, collection_id, name):
        try:
            with self._write_lock, self._connect() as db:
                db.execute("UPDATE collections SET name=? WHERE id=?", (self._collection_name(name), collection_id))
        except sqlite3.IntegrityError:
            raise ValueError("You already have a collection with this name.") from None

    def delete_collection(self, collection_id):
        """Removes the list only. The files stay where they are."""
        with self._write_lock, self._connect() as db:
            db.execute("DELETE FROM collections WHERE id=?", (collection_id,))

    def collect(self, collection_id, file_ids):
        moment = time.time()
        with self._write_lock, self._connect() as db:
            db.executemany("INSERT OR IGNORE INTO collection_files(collection_id,file_id,added_at) VALUES(?,?,?)",
                           ((collection_id, file_id, moment) for file_id in file_ids))

    def uncollect(self, collection_id, file_id):
        with self._write_lock, self._connect() as db:
            db.execute("DELETE FROM collection_files WHERE collection_id=? AND file_id=?", (collection_id, file_id))

    def collections_of(self, file_id):
        with self._connect() as db:
            return {row[0] for row in db.execute("SELECT collection_id FROM collection_files WHERE file_id=?", (file_id,))}

    def link(self, file_ids):
        """Ties files together, spoked around the first, which keeps the picture readable. Returns how many ties are new."""
        ids = list(dict.fromkeys(int(file_id) for file_id in file_ids))
        moment = time.time()
        with self._write_lock, self._connect() as db:
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO file_links(low,high,added_at) VALUES(?,?,?)",
                           ((min(ids[0], other), max(ids[0], other), moment) for other in ids[1:]))
            return db.total_changes - before

    def unlink(self, one, other):
        with self._write_lock, self._connect() as db:
            db.execute("DELETE FROM file_links WHERE low=? AND high=?", (min(one, other), max(one, other)))

    def unlink_all(self, file_id):
        with self._write_lock, self._connect() as db:
            db.execute("DELETE FROM file_links WHERE low=? OR high=?", (file_id, file_id))

    def links_of(self, file_id):
        """The files tied directly to this one."""
        with self._connect() as db:
            return [self._file_dict(row) for row in db.execute(
                "SELECT f.* FROM file_links l JOIN files f ON f.id=CASE WHEN l.low=? THEN l.high ELSE l.low END"
                " WHERE l.low=? OR l.high=? ORDER BY f.name COLLATE NOCASE", (file_id, file_id, file_id))]

    def bundle(self, file_id):
        """Everything reachable from this file through ties, itself included."""
        with self._connect() as db:
            return [self._file_dict(row) for row in db.execute(
                "WITH RECURSIVE reached(id) AS (SELECT ? UNION SELECT CASE WHEN l.low=r.id THEN l.high ELSE l.low END"
                " FROM reached r JOIN file_links l ON l.low=r.id OR l.high=r.id)"
                f" SELECT f.* FROM reached r JOIN files f ON f.id=r.id ORDER BY f.name COLLATE NOCASE LIMIT {MAX_BUNDLE}", (file_id,))]

    def link_graph(self):
        """Every tie and the files on either end, for drawing them."""
        with self._connect() as db:
            edges = [(row["low"], row["high"]) for row in db.execute(f"SELECT low,high FROM file_links ORDER BY added_at DESC LIMIT {MAX_GRAPH_LINKS}")]
            ids = sorted({file_id for edge in edges for file_id in edge})
            nodes = []
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                nodes += [self._file_dict(row) for row in db.execute(f"{self._ROW_WITH_BODY} WHERE f.id IN ({','.join('?' * len(batch))})", batch)]
            return {"nodes": nodes, "edges": edges}

    def linked_counts(self, file_ids):
        """How many ties each of these files has, leaving out those with none."""
        ids, counts = [int(file_id) for file_id in file_ids], {}
        with self._connect() as db:
            for start in range(0, len(ids), 400):
                marks = ",".join("?" * len(ids[start:start + 400]))
                batch = ids[start:start + 400] * 2
                for row in db.execute(f"SELECT low,high FROM file_links WHERE low IN ({marks}) OR high IN ({marks})", batch):
                    for file_id in (row["low"], row["high"]):
                        counts[file_id] = counts.get(file_id, 0) + 1
        return {file_id: count for file_id, count in counts.items() if file_id in set(ids)}

    def suggest_links(self, file_id, limit=6):
        """Files likely to belong with this one, each with why. Nothing is tied until someone says so."""
        row = self.get_file(file_id)
        if not row:
            return []
        taken = {item["id"] for item in self.bundle(file_id)}
        stem = _normal(Path(row["name"]).stem)
        same_name, nearby, folder = [], [], os.path.dirname(row["relative_path"])
        with self._connect() as db:
            if len(stem) >= 3:
                pattern = stem.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%"
                same_name = [(item, "Same name") for item in db.execute(
                    "SELECT * FROM files WHERE name_norm LIKE ? ESCAPE '\\' AND missing=0 AND id<>? LIMIT 12", (pattern, file_id))]
            nearby = [(item, "Saved around the same time") for item in db.execute(
                "SELECT * FROM files WHERE source_id=? AND missing=0 AND id<>? AND ABS(mtime-?)<? ORDER BY ABS(mtime-?) LIMIT 12",
                (row["source_id"], file_id, row["mtime"], NEARBY_SECONDS, row["mtime"])) if os.path.dirname(item["relative_path"]) == folder]
        mentions = []
        if len(stem) >= 4:
            matches = self.search(stem, limit=12)
            words = self.bodies([item["id"] for item in matches])
            mentions = [(item, "Mentions this file") for item in matches if stem in _normal(words.get(item["id"], ""))]
        found = same_name + mentions + nearby
        offers = {}
        for item, why in found:
            item = dict(item)
            if item["id"] not in taken and item["id"] not in offers and (not row["version_key"] or item.get("version_key") != row["version_key"]):
                offers[item["id"]] = {"id": item["id"], "name": item["name"], "ext": item["ext"], "kind": item["kind"], "why": why}
        return list(offers.values())[:limit]

    def moved_from(self, file_id):
        """Where a file was before it was moved into the library, or an empty string."""
        with self._connect() as db:
            row = db.execute("SELECT original FROM moved_files WHERE file_id=?", (file_id,)).fetchone()
            return row["original"] if row else ""

    def _repoint(self, db, row, target, source):
        """The catalogue follows a file to its new home, so tags, ties and read text all stay."""
        relative = os.path.relpath(target, source["path"])
        db.execute("UPDATE files SET source_id=?,path=?,path_key=?,relative_path=?,path_norm=?,version_key=?,missing=0 WHERE id=?",
                   (source["id"], str(target), _path_key(target), relative, _normal(relative), _family(source["id"], relative, row["name"]), row["id"]))
        body = db.execute("SELECT text FROM file_bodies WHERE file_id=?", (row["id"],)).fetchone()
        search_text = self._search_value(row["name"], relative, row["tags"], body["text"] if body else "")
        db.execute("UPDATE file_bodies SET search_text=? WHERE file_id=?", (search_text, row["id"]))
        self._write_search_index(db, row["id"], search_text)
        left = db.execute("SELECT * FROM sources WHERE id=?", (row["source_id"],)).fetchone()
        if left is None or left["id"] == source["id"]:
            return
        if left["picks"]:
            db.execute("UPDATE sources SET picks=? WHERE id=?", (json.dumps(sorted(set(json.loads(left["picks"])) - {row["name"]}), ensure_ascii=False), left["id"]))
        if not db.execute("SELECT 1 FROM files WHERE source_id=? LIMIT 1", (left["id"],)).fetchone() and (left["picks"] or not Path(left["path"]).exists()):
            db.execute("DELETE FROM sources WHERE id=?", (left["id"],))  # Handed over file by file, and all of them have gone.

    def _home_for(self, folder):
        """The source a moved file belongs to, made if there is none."""
        covering = next((row for row in self.sources() if not row["picks"] and _within(folder, row["path"])), None)
        return covering or self.add_source(folder)

    def move_files(self, moves, library, emptied=()):
        """Moves files into the library. ``moves`` pairs a file id with its path inside the library.

        The original goes only after the new file is safely in place. Nothing is
        replaced: a name that is taken gets a number. One failure leaves the
        rest of the armful unaffected. ``emptied`` lists folders that are left
        behind if nothing remains in them. Returns the ids moved and what went wrong.
        """
        library = _assert_local_path(library, check_links=True)
        if _within(library, self.data_dir):
            raise ValueError("The library cannot be inside the archive data folder.")
        library.mkdir(parents=True, exist_ok=True)
        moved, problems = [], []
        with self._scan_lock:
            home = self._home_for(library)
            for file_id, inside in moves:
                row = self.get_file(file_id)
                if not row or _within(row["path"], library):
                    continue
                try:
                    origin = _assert_local_path(row["path"], check_links=True)
                    if row["missing"] or not origin.is_file():
                        raise OSError("The file is not at its original location.")
                    target = _free_name(library / inside)
                    _carry_file(origin, target)
                    try:
                        with self._write_lock, self._connect() as db:
                            db.execute("INSERT OR IGNORE INTO moved_files(file_id,original,moved_at) VALUES(?,?,?)", (file_id, str(origin), time.time()))
                            self._repoint(db, row, target, home)
                    except Exception:
                        _carry_file(target, origin)  # A file the catalogue cannot find is as good as lost.
                        raise
                    moved.append(file_id)
                except Exception as exc:  # The rest of the armful still goes.
                    problems.append((row["name"], str(exc)[:200]))
            for folder in emptied:
                self._tidy_away(Path(folder))
        return {"moved": moved, "problems": problems}

    def _tidy_away(self, folder):
        """Removes folders a move has emptied. rmdir refuses one with anything left in it, so nothing is lost."""
        for inside, _, _ in os.walk(folder, topdown=False):
            try:
                os.rmdir(inside)
            except OSError:
                pass
        if not folder.exists():
            with self._write_lock, self._connect() as db:
                db.execute("DELETE FROM sources WHERE path_key=? AND NOT EXISTS (SELECT 1 FROM files WHERE source_id=sources.id)", (_path_key(folder),))

    def move_back(self, file_id):
        """Returns a moved file to where it came from. Nothing already there is replaced."""
        original = self.moved_from(file_id)
        row = self.get_file(file_id)
        if not original or not row:
            raise ValueError("This file was not moved here.")
        origin, target = Path(row["path"]), _assert_local_path(original)
        if target.exists():
            raise ValueError("Something else is already at the original location.")
        with self._scan_lock:
            _carry_file(origin, target)
            try:
                covering = next((item for item in self.sources() if not item["picks"] and _within(target, item["path"])), None)
                held_by = None if covering else self._pick_file(target)
                source = covering or next(item for item in self.sources() if item["path"] == held_by)
                with self._write_lock, self._connect() as db:
                    self._repoint(db, row, target, source)
                    db.execute("DELETE FROM moved_files WHERE file_id=?", (file_id,))
            except Exception:
                _carry_file(target, origin)
                raise

    def export_collection(self, collection_id, path):
        """A plain CSV list, so a collection is never trapped in this archive."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT f.name,f.path,f.tags,f.mtime FROM files f JOIN collection_files m ON m.file_id=f.id"
                " WHERE m.collection_id=? ORDER BY f.name COLLATE NOCASE", (collection_id,)).fetchall()
        with open(path, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("name", "path", "tags", "modified"))
            for row in rows:
                writer.writerow((row["name"], row["path"], ", ".join(json.loads(row["tags"])), time.strftime("%Y-%m-%d %H:%M", time.localtime(row["mtime"]))))
        return len(rows)

    def ocr_texts(self, limit=30):
        """Words recently read from photos and scans."""
        with self._connect() as db:
            return [row[0] for row in db.execute(
                "SELECT b.text FROM files f JOIN file_bodies b ON b.file_id=f.id WHERE f.extraction_status='ocr' ORDER BY f.mtime DESC LIMIT ?", (limit,))]

    def mark_used(self, file_id):
        with self._write_lock, self._connect() as db:
            db.execute("UPDATE files SET last_used=? WHERE id=?", (time.time(), file_id))

    def count_since(self, moment):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM files WHERE mtime>=?", (moment,)).fetchone()[0]

    def _update_labels(self, file_id, tags=None, favorite=None):
        with self._write_lock, self._connect() as db:
            row = db.execute(self._ROW_WITH_BODY + " WHERE f.id=?", (int(file_id),)).fetchone()
            if not row:
                raise ValueError("This archive record no longer exists.")
            current_tags = json.loads(row["tags"])
            if tags is not None:
                if isinstance(tags, str):
                    tags = re.split(r"[,，;；\n]", tags)
                current_tags = list(dict.fromkeys(str(tag).strip()[:50] for tag in tags if str(tag).strip()))[:30]
            search_text = self._search_value(row["name"], row["relative_path"], current_tags, row["text"])
            db.execute("UPDATE files SET tags=?,tags_norm=?,favorite=? WHERE id=?", (
                json.dumps(current_tags, ensure_ascii=False), _normal(" ".join(current_tags)),
                int(bool(favorite)) if favorite is not None else row["favorite"], int(file_id),
            ))
            if search_text != row["search_text"]:
                db.execute("UPDATE file_bodies SET search_text=? WHERE file_id=?", (search_text, int(file_id)))
                self._write_search_index(db, int(file_id), search_text)
            return self._file_dict(db.execute(self._ROW_WITH_BODY + " WHERE f.id=?", (int(file_id),)).fetchone())

    _OCR_WAITING = (
        "FROM files WHERE ocr=0 AND missing=0 AND extraction_status<>'cloud_placeholder'"
        f" AND (ext IN ({','.join('?' * len(OCR_IMAGE_EXTS))}) OR (ext='.pdf' AND extraction_error IN (?,?)))"
    )

    def ocr_waiting(self, limit=20, among=None):
        """Photos and scans whose words have not been read yet, newest first."""
        narrow = "" if among is None else f" AND id IN ({','.join(str(int(file_id)) for file_id in among)})"
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                f"SELECT id,path,archive_path,ext,extraction_status {self._OCR_WAITING}{narrow} ORDER BY mtime DESC LIMIT ?",
                (*OCR_IMAGE_EXTS, NO_TEXT_LAYER, TOO_LARGE, limit))]

    def ocr_remaining(self):
        with self._connect() as db:
            return db.execute(f"SELECT COUNT(*) {self._OCR_WAITING}", (*OCR_IMAGE_EXTS, NO_TEXT_LAYER, TOO_LARGE)).fetchone()[0]

    def save_ocr(self, file_id, text):
        """Stores what was read, so the words search like any other text in the file."""
        with self._write_lock, self._connect() as db:
            row = db.execute("SELECT name,relative_path,tags FROM files WHERE id=?", (file_id,)).fetchone()
            if row is None:
                return
            db.execute("UPDATE files SET ocr=1 WHERE id=?", (file_id,))
            if text:
                search_text = self._search_value(row["name"], row["relative_path"], json.loads(row["tags"]), text)
                db.execute("UPDATE files SET extraction_status='ocr',extraction_error='',summary=? WHERE id=?", (re.sub(r"\s+", " ", text).strip()[:170], file_id))
                db.execute("INSERT INTO file_bodies(file_id,text,search_text) VALUES(?,?,?) ON CONFLICT(file_id) DO UPDATE SET text=excluded.text,search_text=excluded.search_text",
                           (file_id, text, search_text))
                self._write_search_index(db, file_id, search_text)

    def version_counts(self, keys):
        """For each family: how many files, and which one was saved last."""
        keys = list({key for key in keys if key})
        if not keys:
            return {}
        with self._connect() as db:
            return {row["version_key"]: (row["total"], row["id"]) for row in db.execute(
                f"SELECT version_key,COUNT(*) AS total,id,MAX(mtime) FROM files WHERE missing=0 AND version_key IN ({','.join('?' * len(keys))}) GROUP BY version_key", keys)}

    def versions(self, key):
        """A family, newest first."""
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT id,name,mtime FROM files WHERE missing=0 AND version_key=? ORDER BY mtime DESC,id DESC", (key,))] if key else []

    def set_tags(self, file_id, tags):
        return self._update_labels(file_id, tags=tags)

    def set_favorite(self, file_id, favorite):
        return self._update_labels(file_id, favorite=favorite)

    def close(self):
        """Connections are per operation; kept for desktop application's lifecycle."""
        return None
