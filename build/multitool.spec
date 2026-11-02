# PyInstaller spec — builds dist/MultiTool/ (one-folder app with its own embedded Python).
#
#   pyinstaller --noconfirm --clean build/multitool.spec
#
# Plugins are plain .py files loaded at runtime, so PyInstaller can't see what they import.
# To make any plugin work without the user installing anything, this build ships:
#   * the ENTIRE Python standard library
#   * every third-party package listed in BUNDLED_PACKAGES (keep in sync with requirements.txt)

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH is injected by PyInstaller)

# Third-party packages plugins may import. Add new ones here + in requirements.txt.
BUNDLED_PACKAGES = [
    "customtkinter",
    "tkinterdnd2",   # includes the native tkdnd library for drag-and-drop
    "PIL",           # Pillow: thumbnails, screenshots, textures
    "darkdetect",    # customtkinter dependency
]

# ── whole standard library ──────────────────────────────────────────────────
SKIP_STDLIB = {
    "test", "tests", "idlelib", "lib2to3", "turtledemo", "ensurepip", "venv", "pydoc_data",
    "tkinter.test", "unittest.test", "distutils", "this", "antigravity", "__phello__", "_pyrepl",
    # POSIX-only modules that don't exist on Windows
    "curses", "_curses", "_curses_panel", "readline", "grp", "pwd", "termios", "tty", "pty", "fcntl",
    "resource", "posix", "syslog", "crypt", "spwd", "nis", "ossaudiodev", "_posixsubprocess",
    "_posixshmem", "_scproxy", "_gdbm", "_dbm", "dbm.gnu", "dbm.ndbm", "multiprocessing.popen_fork",
    "multiprocessing.popen_forkserver", "multiprocessing.popen_spawn_posix", "_tkinter_test",
}


def _keep(name: str) -> bool:
    parts = name.split(".")
    if any(p in ("test", "tests", "idle_test") for p in parts[1:]):
        return False
    return not any(name == s or name.startswith(s + ".") for s in SKIP_STDLIB)


hiddenimports = []
for top in sorted(sys.stdlib_module_names):
    if not _keep(top):
        continue
    try:
        hiddenimports += [m for m in collect_submodules(top, filter=_keep) if _keep(m)] or [top]
    except Exception:
        hiddenimports.append(top)

datas, binaries = [], []
for pkg in BUNDLED_PACKAGES:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

icon = ROOT / "assets" / "icon.ico"
if icon.is_file():
    datas.append((str(icon), "assets"))

a = Analysis(  # noqa: F821
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    excludes=["tkinter.test", "unittest.test", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MultiTool",
    console=False,                # windowed app, no console
    icon=str(icon) if icon.is_file() else None,
    upx=False,                    # UPX triggers antivirus false positives
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MultiTool",
)
