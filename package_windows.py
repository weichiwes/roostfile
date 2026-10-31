"""Builds a portable Windows copy: nothing to install, unzip and double-click start.cmd.

Run on Windows. ``--python`` is an unpacked python.org "embeddable package", which
becomes runtime\\, the first Python launch.ps1 looks for. ``--vendor`` is a folder
made with ``pip install --target``. The archive folder is created beside the app
the first time it runs, so moving the folder moves the library with it.
"""
import argparse
from pathlib import Path
import shutil
import zipfile

BASE = Path(__file__).resolve().parent
APP_FILES = (
    "desktop_app.py", "visual_widgets.py", "search_ux.py", "core.py", "background.py", "pages.py", "ocr.py", "assistant.py", "mcp_server.py",
    "i18n.py", "theme.py", "demo.py", "offline.py", "launch.ps1", "start.cmd", "demo.cmd", "README.md", "README.zh-CN.md",
    "LICENSE", "THIRD_PARTY_NOTICES.md",
)
SKIPPED = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyi", "*.h", "*.lib", "examples", "include", "typesystems", "glue", "doc")


def build(python: Path, vendor: Path, out: Path, name: str) -> Path:
    if not (python / "pythonw.exe").is_file():
        raise SystemExit(f"{python} is not an unpacked embeddable Python: pythonw.exe is missing.")
    if not (vendor / "PySide6").is_dir():
        raise SystemExit(f"{vendor} has no PySide6. Run pip install --target first.")
    folder = out / name
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    for file in APP_FILES:
        shutil.copy2(BASE / file, folder / file)
    shutil.copytree(python, folder / "runtime")
    shutil.copytree(vendor, folder / ".vendor", ignore=SKIPPED)
    # pip's launchers are not used here, and each one records the path of the Python that made it.
    shutil.rmtree(folder / ".vendor" / "bin", ignore_errors=True)
    archive = out / f"{name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(out))
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--vendor", type=Path, default=BASE / ".vendor")
    parser.add_argument("--out", type=Path, default=BASE / "dist")
    parser.add_argument("--name", default="Roostfile-windows-x64")
    args = parser.parse_args()
    archive = build(args.python, args.vendor, args.out, args.name)
    print(f"{archive}  {archive.stat().st_size / 1048576:.0f} MB")


if __name__ == "__main__":
    main()
