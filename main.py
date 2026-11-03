"""
Multi-Tool — Desktop Utility Belt (core shell)
==============================================

The core window is only a layout shell + router. Every tool lives in ./plugins/
and is discovered at runtime, Stremio-style.

Folder layout (next to main.py in dev, next to MultiTool.exe when built):

    plugins/            one .py file per tool, or a folder with __init__.py
    bin/                bundled engines: ffmpeg.exe, ffprobe.exe (+ their DLLs),
                        yt-dlp.exe, deno.exe, llama-cpu/ + llama-vulkan/ (llama-server.exe),
                        tesseract/tesseract.exe (+ tessdata/)
    assets/icon.ico     optional window icon
    data/               settings, logs, themes, per-plugin data (dev/portable mode)

PLUGIN CONTRACT (API v1)
------------------------
A plugin file must define `class Plugin`:

    import customtkinter as ctk

    class Plugin:
        meta_title = "Media Downloader"           # required (sidebar + header)
        meta_description = "Pull MP4/MP3 ..."     # shown under the page title
        meta_icon = "⬇"                           # optional, ONE character (stick to
                                                  # BMP symbols; Tk 8.6 can't draw most emoji)
        meta_order = 10                           # optional sidebar sort key
        meta_api_version = 1                      # optional, refuses to load on newer API

        def __init__(self, ctx):                  # ctx = PluginContext (see below)
            self.ctx = ctx                        # start background daemons here if the
                                                  # tool must run while its tab is hidden

        def get_frame(self, master):              # called lazily, first time the tab opens
            frame = ctk.CTkFrame(master, fg_color="transparent")
            ...
            return frame

        # optional hooks
        def on_show(self): ...
        def on_hide(self): ...
        def on_theme_changed(self, palette): ...
        def shutdown(self): ...                   # stop threads / hotkeys / file watchers

Files/folders starting with "_" are ignored by the loader but stay importable,
so shared helpers can live in plugins/_common.py and be imported normally.

PluginContext gives each plugin:
    ctx.plugin_id, ctx.app, ctx.paths, ctx.log, ctx.data_dir, ctx.dnd_available
    ctx.theme.tag(widget, role) / .apply(widget) / .palette / .font() / .subscribe(fn)
    ctx.bin_path("ffmpeg", required=True)      -> Path to bin/ffmpeg.exe
    ctx.subprocess_kwargs()                    -> hides console windows of child processes
    ctx.register_drop_target(widget, callback, accept=("files", "text"))
    ctx.call_in_ui(fn, *args)                  -> safe from worker threads
    ctx.run_in_thread(fn, *args, on_done=..., on_error=...)
    ctx.get_setting(key, default) / ctx.set_setting(key, value)
    ctx.copy_to_clipboard(text), ctx.set_status(text, level), ctx.open_path(path)
    ctx.register_file_type(".ext", "Label", handler)   -> Settings ▸ Install… accepts that file type

THEME PACKS (.mttheme)
----------------------
A theme is a single .mttheme file — plain JSON, or a zip containing theme.json plus an
optional banner image. Install by dropping it on Settings or via Settings ▸ Install theme….
    {
      "name": "My Theme", "author": "me", "version": "1.0", "description": "…",
      "extends": "Default Dark",                 # unspecified colours come from here
      "colors":  { "bg": "#000000", "accent": "#ffcc00", "radius": 0, "title_font": "Bahnschrift", … },
      "effects": { "uppercase_titles": true, "title_prefix": "// ", "glitch_titles": true, … },
      "banner":  "banner.png"                    # zip packs only
    }
Colour keys: see BUILTIN_THEMES. Effect keys: see EFFECT_DEFAULTS.

THEME ROLES (set with ctx.theme.tag(widget, role)):
    frames:  "transparent" | "surface" | "sidebar" | "card" | "entry" | "divider"
    buttons: "accent" (default) | "secondary" | "ghost" | "danger"
    labels:  "text" (default) | "muted" | "title" | "accent" | "accent2" |
             "danger" | "success" | "warning"
    any:     "skip"  -> theme engine never touches this widget or its children
                        (use for screen overlays / custom-drawn canvases)
Untagged widgets are themed automatically by type.
"""

from __future__ import annotations

import copy
import ctypes
import importlib
import importlib.util
import inspect
import io
import json
import logging
import logging.handlers
import multiprocessing
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import tkinter as tk

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, DND_TEXT, TkinterDnD

    _DnDBase = TkinterDnD.DnDWrapper
    _DND_IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # app still runs, drag-and-drop is just disabled
    TkinterDnD = None
    DND_FILES, DND_TEXT = "DND_Files", "DND_Text"
    _DND_IMPORT_ERROR = _exc

    class _DnDBase:  # type: ignore[no-redef]
        pass

try:
    from customtkinter.windows.widgets.core_widget_classes import CTkBaseClass as _CTkBase
except Exception:
    _CTkBase = None
_CTkCanvas = getattr(ctk, "CTkCanvas", None)


# ════════════════════════════════════════════════════════════════════════════
#  Constants
# ════════════════════════════════════════════════════════════════════════════

APP_NAME = "Multi-Tool"          # placeholder product name — change in one place
APP_ID = "MultiTool"             # filesystem / mutex safe id
APP_VERSION = "0.3.0"
PLUGIN_API_VERSION = 1
PLUGIN_NAMESPACE = "multitool_plugins"

IS_WINDOWS = sys.platform == "win32"
IS_FROZEN = bool(getattr(sys, "frozen", False))

SETTINGS_KEY = "__settings__"
EMPTY_KEY = "__empty__"
DEFAULT_THEME = "Default Dark"

# Engines the confirmed modules expect in /bin (name, who uses it)
KNOWN_ENGINES: list[tuple[str, str]] = [
    ("ffmpeg", "Media Downloader, Gameplay Clipper, Audio Extractor"),
    ("ffprobe", "Media Downloader, Audio Extractor"),
    ("yt-dlp", "Media Downloader"),
    ("deno", "Media Downloader (YouTube challenge solving)"),
    ("llama-vulkan/llama-server", "Desktop Companion offline AI (graphics card)"),
    ("llama-cpu/llama-server", "Desktop Companion offline AI (processor)"),
    ("tesseract", "Optics OCR"),
]

URL_RE = re.compile(r"https?://[^\s<>\"'{}|\\^`]+", re.IGNORECASE)


# ════════════════════════════════════════════════════════════════════════════
#  Themes
# ════════════════════════════════════════════════════════════════════════════

BUILTIN_THEMES: dict[str, dict[str, Any]] = {
    "Default Dark": {
        "bg": "#111116",
        "surface": "#111116",
        "sidebar": "#181824",
        "card": "#181824",
        "card_hover": "#20202e",
        "control": "#202030",
        "control_hover": "#2a2a3e",
        "border": "#262636",
        "border_strong": "#3a3a52",
        "text": "#e4e4ec",
        "text_muted": "#8a8aa0",
        "title": "#f5f5fa",
        "accent": "#4c6ef5",
        "accent_hover": "#3b5bdb",
        "accent_text": "#ffffff",
        "accent2": "#a5acc4",
        "selected": "#4c6ef5",
        "selected_hover": "#3b5bdb",
        "danger": "#e5484d",
        "danger_hover": "#c93c41",
        "success": "#3fb950",
        "warning": "#d29922",
        "entry_bg": "#0c0c11",
        "scrollbar": "#2a2a3c",
        "scrollbar_hover": "#3d3d56",
        "nav_active_bg": "#24243a",
        "nav_active_text": "#ffffff",
        "nav_active_border": "#24243a",
        "nav_active_border_width": 0,
        "drop_highlight": "#4c6ef5",
        "card_border_width": 1,
        "radius": 8,
        "font": "Segoe UI",
        "title_font": "Segoe UI Semibold",
        "mono_font": "Consolas",
    },
    "Cyberpunk Neon": {
        "bg": "#080810",
        "surface": "#080810",
        "sidebar": "#0b0b16",
        "card": "#0e0e1c",
        "card_hover": "#17172e",
        "control": "#12122a",
        "control_hover": "#1d1d40",
        "border": "#00ffcc",
        "border_strong": "#00ffcc",
        "text": "#d8fff6",
        "text_muted": "#7184a3",
        "title": "#00ffcc",
        "accent": "#00ffcc",
        "accent_hover": "#00d9ad",
        "accent_text": "#080810",
        "accent2": "#ff2bd6",
        "selected": "#ff2bd6",
        "selected_hover": "#d91fb5",
        "danger": "#ff3366",
        "danger_hover": "#e0204f",
        "success": "#39ff14",
        "warning": "#ffe600",
        "entry_bg": "#05050b",
        "scrollbar": "#3a1046",
        "scrollbar_hover": "#ff2bd6",
        "nav_active_bg": "#170c26",
        "nav_active_text": "#ff2bd6",
        "nav_active_border": "#ff2bd6",
        "nav_active_border_width": 1,
        "drop_highlight": "#ff2bd6",
        "card_border_width": 1,
        "radius": 2,
        "font": "Segoe UI",
        "title_font": "Consolas",
        "mono_font": "Consolas",
    },
}

FRAME_ROLE_COLORS = {
    "bg": "bg",
    "surface": "surface",
    "sidebar": "sidebar",
    "statusbar": "sidebar",
    "card": "card",
    "entry": "entry_bg",
    "divider": "border",
}

LABEL_ROLE_COLORS = {
    None: "text",
    "text": "text",
    "muted": "text_muted",
    "title": "title",
    "accent": "accent",
    "accent2": "accent2",
    "danger": "danger",
    "success": "success",
    "warning": "warning",
}


THEME_EXTENSIONS = (".mttheme", ".json")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
FONT_KEYS = {"font", "title_font", "mono_font"}

# Optional shell decorations a theme pack can switch on ("effects" in theme.json)
EFFECT_DEFAULTS: dict[str, Any] = {
    "uppercase_titles": False,   # page titles in CAPS
    "uppercase_nav": False,      # sidebar entries in CAPS
    "title_prefix": "",          # e.g. "// "
    "nav_active_prefix": "",     # e.g. "▶ " in front of the selected tool
    "status_prefix": "",         # e.g. "> " in the status bar
    "brand_tagline": "",         # replaces "Desktop Utility Belt" under the logo
    "glitch_titles": False,      # page title briefly scrambles every few seconds
    "glitch_colors": [],         # colours used while glitching (default: accent2 + danger)
    "divider_height": 1,         # thickness of the header / sidebar dividers
    "divider_color": "border",   # palette key for those dividers
    "sidebar_stripe": 0,         # width of a coloured stripe between sidebar and content
    "sidebar_stripe_color": "accent",
}


@dataclass
class ThemeInfo:
    name: str
    palette: dict[str, Any]
    effects: dict[str, Any]
    author: str = ""
    description: str = ""
    version: str = ""
    source: Path | None = None   # None = built into the app (cannot be removed)
    banner: str | None = None    # image inside a zipped .mttheme, shown instead of the app name

    @property
    def builtin(self) -> bool:
        return self.source is None


BUILTIN_THEME_INFO: dict[str, dict[str, Any]] = {
    "Default Dark": {"description": "Clean slate greys. The default — always available.", "effects": {}},
    "Cyberpunk Neon": {"description": "Near-black canvas, neon cyan lines, hot magenta accents.",
                       "effects": {"nav_active_prefix": "› ", "status_prefix": "> "}},
}


def _merge_effects(raw: Any, warnings: list[str]) -> dict[str, Any]:
    effects = dict(EFFECT_DEFAULTS)
    if not isinstance(raw, dict):
        return effects
    for key, value in raw.items():
        default = EFFECT_DEFAULTS.get(key)
        if key not in EFFECT_DEFAULTS:
            warnings.append(f"unknown effect {key!r}")
        elif isinstance(default, bool):
            effects[key] = bool(value)
        elif isinstance(default, int) and isinstance(value, (int, float)) and not isinstance(value, bool):
            effects[key] = max(0, min(int(value), 12))
        elif isinstance(default, list) and isinstance(value, list):
            effects[key] = [v for v in value if isinstance(v, str) and HEX_COLOR_RE.match(v)]
        elif isinstance(default, str) and isinstance(value, str):
            effects[key] = value[:40]
        else:
            warnings.append(f"invalid effect {key!r}")
    return effects


def builtin_theme_infos() -> dict[str, ThemeInfo]:
    out = {}
    for name, palette in BUILTIN_THEMES.items():
        meta = BUILTIN_THEME_INFO.get(name, {})
        out[name] = ThemeInfo(name=name, palette=dict(palette), effects=_merge_effects(meta.get("effects"), []),
                              author="Built in", description=meta.get("description", ""))
    return out


def read_theme_pack(path: Path) -> tuple[ThemeInfo, list[str]]:
    """A theme pack is a .mttheme file: plain JSON, or a zip holding theme.json (+ banner image).
    Plain .json files in the themes folder are read the same way."""
    warnings: list[str] = []
    banner_member = None
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            member = next((n for n in names if n.rsplit("/", 1)[-1].lower() == "theme.json"), None)
            if member is None:
                raise ValueError("the pack has no theme.json inside")
            raw = json.loads(zf.read(member).decode("utf-8-sig"))
            prefix = member[: -len("theme.json")]
            banner = raw.get("banner") if isinstance(raw, dict) else None
            if isinstance(banner, str) and (prefix + banner) in names:
                banner_member = prefix + banner
            elif banner:
                warnings.append(f"banner {banner!r} not found in pack")
    else:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("theme must be a JSON object")
    name = str(raw.get("name") or path.stem).strip()[:60] or path.stem
    base = BUILTIN_THEMES.get(str(raw.get("extends", DEFAULT_THEME)), BUILTIN_THEMES[DEFAULT_THEME])
    palette = dict(base)
    colors = raw.get("colors")
    if not isinstance(colors, dict):  # older flat format: colour keys at top level
        colors = {k: v for k, v in raw.items() if k in palette}
    for key, value in colors.items():
        if key not in palette:
            warnings.append(f"unknown colour key {key!r}")
        elif key in FONT_KEYS and isinstance(value, str) and value.strip():
            palette[key] = value.strip()
        elif isinstance(palette[key], int) and isinstance(value, (int, float)) and not isinstance(value, bool):
            palette[key] = max(0, min(int(value), 24))
        elif isinstance(palette[key], str) and isinstance(value, str) and HEX_COLOR_RE.match(value):
            palette[key] = value
        else:
            warnings.append(f"invalid value for {key!r}")
    info = ThemeInfo(name=name, palette=palette, effects=_merge_effects(raw.get("effects"), warnings),
                     author=str(raw.get("author", ""))[:60], description=str(raw.get("description", ""))[:200],
                     version=str(raw.get("version", ""))[:20], source=path, banner=banner_member)
    return info, warnings


def load_theme_packs(themes_dir: Path, log: logging.Logger) -> dict[str, ThemeInfo]:
    out: dict[str, ThemeInfo] = {}
    for file in sorted(themes_dir.iterdir(), key=lambda f: f.name.lower()) if themes_dir.is_dir() else []:
        if file.suffix.lower() not in THEME_EXTENSIONS or not file.is_file():
            continue
        try:
            info, warnings = read_theme_pack(file)
            if info.name in BUILTIN_THEMES or info.name in out:
                info.name = f"{info.name} ({file.stem})"
            for w in warnings:
                log.warning("Theme %s: %s", file.name, w)
            out[info.name] = info
            log.info("Loaded theme pack %r from %s", info.name, file.name)
        except Exception as exc:
            log.warning("Skipping theme file %s: %s", file, exc)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Paths, environment, logging (portable-build aware)
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class AppPaths:
    app_dir: Path          # folder holding main.py / MultiTool.exe
    resource_dir: Path     # read-only bundled resources (PyInstaller _MEIPASS)
    plugins_dir: Path
    bin_dir: Path
    data_dir: Path         # writable user data
    logs_dir: Path
    themes_dir: Path
    plugin_data_dir: Path
    settings_file: Path
    portable: bool


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_test_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_paths() -> AppPaths:
    if IS_FROZEN:
        app_dir = Path(sys.executable).resolve().parent
        resource_dir = Path(getattr(sys, "_MEIPASS", app_dir))
    else:
        app_dir = Path(__file__).resolve().parent
        resource_dir = app_dir

    # Dev runs and builds shipped with a `portable.flag` file keep data beside the app.
    # Installed builds use %APPDATA%\MultiTool (Program Files is read-only).
    portable = (not IS_FROZEN) or (app_dir / "portable.flag").exists()
    data_dir = app_dir / "data"
    if not portable or not _is_writable_dir(data_dir):
        portable = False
        if IS_WINDOWS:
            base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            data_dir = Path(base) / APP_ID
        else:
            data_dir = Path.home() / f".{APP_ID.lower()}"

    logs_dir = data_dir / "logs"
    themes_dir = data_dir / "themes"
    plugin_data_dir = data_dir / "plugin_data"
    for d in (data_dir, logs_dir, themes_dir, plugin_data_dir):
        d.mkdir(parents=True, exist_ok=True)

    plugins_dir = app_dir / "plugins"
    bin_dir = app_dir / "bin"
    for d in (plugins_dir, bin_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # read-only install location; the installer ships these folders

    return AppPaths(
        app_dir=app_dir,
        resource_dir=resource_dir,
        plugins_dir=plugins_dir,
        bin_dir=bin_dir,
        data_dir=data_dir,
        logs_dir=logs_dir,
        themes_dir=themes_dir,
        plugin_data_dir=plugin_data_dir,
        settings_file=data_dir / "settings.json",
        portable=portable,
    )


def ensure_stdio(paths: AppPaths) -> None:
    """PyInstaller --windowed sets sys.stdout/stderr to None; libraries like tqdm
    (used by Whisper) and yt-dlp crash when they write to it. Point them at a file."""
    if sys.stdout is None or sys.stderr is None:
        stream = open(paths.logs_dir / "console.log", "a", encoding="utf-8", errors="replace", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream


def setup_logging(paths: AppPaths) -> logging.Logger:
    log = logging.getLogger(APP_ID)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        paths.logs_dir / "multitool.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    log.addHandler(file_handler)

    if not IS_FROZEN:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        console.setLevel(logging.INFO)
        log.addHandler(console)

    def excepthook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        name = args.thread.name if args.thread else "?"
        log.error("Unhandled exception in thread %s", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    return log


def configure_environment(paths: AppPaths, log: logging.Logger) -> None:
    """Make bundled engines win over anything on the user's machine — for THIS process
    and its children only. The user's Windows PATH is never touched."""
    extra = [d for d in (paths.bin_dir, paths.bin_dir / "tesseract") if d.is_dir()]
    if extra:
        os.environ["PATH"] = os.pathsep.join([str(d) for d in extra] + [os.environ.get("PATH", "")])
    tessdata = paths.bin_dir / "tesseract" / "tessdata"
    if tessdata.is_dir():
        os.environ["TESSDATA_PREFIX"] = str(tessdata)

    # plugins/ on sys.path lets plugins share helpers (plugins/_common.py -> `import _common`)
    if str(paths.plugins_dir) not in sys.path:
        sys.path.insert(0, str(paths.plugins_dir))
    if IS_FROZEN:
        sys.dont_write_bytecode = True  # don't litter the install folder with __pycache__
    log.debug("PATH prefix: %s", [str(d) for d in extra])


def find_binary(bin_dir: Path, name: str, required: bool = False) -> Path | None:
    """bin/<name>.exe or bin/<name>/<name>.exe (tesseract ships as a folder)."""
    stem = name[:-4] if name.lower().endswith(".exe") else name
    filename = stem + (".exe" if IS_WINDOWS else "")
    for candidate in (bin_dir / filename, bin_dir / stem / filename):
        if candidate.is_file():
            return candidate
    if required:
        raise FileNotFoundError(f"Bundled engine '{filename}' is missing. Expected it at: {bin_dir / filename}")
    return None


def hidden_subprocess_kwargs() -> dict[str, Any]:
    """Pass to subprocess.Popen/run so ffmpeg/yt-dlp don't flash console windows."""
    if not IS_WINDOWS:
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}


def open_in_os(path: Path | str) -> None:
    target = str(path)
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


# ── Windows integration ─────────────────────────────────────────────────────

_INSTANCE_MUTEX = None


def acquire_single_instance() -> bool:
    """Global hotkeys (F8, Ctrl+Alt+S) would collide if two copies ran."""
    global _INSTANCE_MUTEX
    if not IS_WINDOWS:
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        _INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, f"Local\\{APP_ID}_SingleInstance")
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def focus_existing_window() -> None:
    if not IS_WINDOWS:
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, APP_NAME)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def set_app_user_model_id() -> None:
    """Own taskbar identity/icon instead of being grouped under python.exe."""
    if IS_WINDOWS:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_ID}.Desktop")
        except Exception:
            pass


def show_fatal_error(message: str) -> None:
    if IS_WINDOWS:
        try:
            ctypes.windll.user32.MessageBoxW(None, message, f"{APP_NAME} failed to start", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


# ════════════════════════════════════════════════════════════════════════════
#  Settings store (atomic JSON)
# ════════════════════════════════════════════════════════════════════════════

class SettingsStore:
    def __init__(self, path: Path, log: logging.Logger):
        self.path = path
        self.log = log
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, dict)}
            raise ValueError("root is not an object")
        except Exception as exc:
            backup = self.path.with_name(self.path.stem + ".corrupt.json")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            self.log.warning("settings.json was unreadable (%s); moved to %s", exc, backup.name)
            return {}

    def get(self, section: str, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(section, {}).get(key, default))

    def set(self, section: str, key: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault(section, {})[key] = value
            self._save_locked()

    def _save_locked(self) -> None:
        tmp_name = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp_name, self.path)
        except Exception:
            self.log.exception("Failed to save settings")
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass


# ════════════════════════════════════════════════════════════════════════════
#  Theme engine
# ════════════════════════════════════════════════════════════════════════════

def _safe_configure(widget: Any, **kwargs: Any) -> None:
    """Configure what the widget supports; silently skip options it doesn't."""
    if not kwargs:
        return
    try:
        widget.configure(**kwargs)
    except Exception:
        for key, value in kwargs.items():
            try:
                widget.configure(**{key: value})
            except Exception:
                pass


def _is_ctk(widget: Any) -> bool:
    if _CTkBase is not None and isinstance(widget, _CTkBase):
        return True
    return isinstance(widget, (ctk.CTk, ctk.CTkToplevel, ctk.CTkScrollableFrame)) or hasattr(
        widget, "_apply_appearance_mode"
    )


def _container_of(widget: Any) -> Any:
    """CTkScrollableFrame lives inside a hidden host frame; that host is what gets gridded."""
    return getattr(widget, "_parent_frame", widget)


def _resolve_color(color: Any) -> Any:
    if isinstance(color, (list, tuple)) and color:
        return color[-1]  # (light, dark) -> app is always dark
    return color


class ThemeManager:
    """Walks the live widget tree and restyles it from the active palette.
    Runs on every theme switch and on every newly mounted plugin page."""

    def __init__(self, root: tk.Misc, settings: SettingsStore, themes_dir: Path, log: logging.Logger):
        self._root = root
        self._settings = settings
        self.themes_dir = themes_dir
        self.log = log
        self.infos: dict[str, ThemeInfo] = {}
        self.themes: dict[str, dict[str, Any]] = {}
        self.name = DEFAULT_THEME
        self._listeners: list[tuple[str, Callable[[dict[str, Any]], None]]] = []
        self._fonts: dict[tuple, ctk.CTkFont] = {}
        self._banners: dict[str, Any] = {}
        self.reload()
        saved = settings.get("core", "theme", DEFAULT_THEME)
        self.name = saved if saved in self.themes else DEFAULT_THEME

    # ── theme packs ───────────────────────────────────────────────────────
    def reload(self) -> None:
        """Re-scan the themes folder (built-in themes always come first)."""
        self.infos = builtin_theme_infos()
        self.infos.update(load_theme_packs(self.themes_dir, self.log))
        self.themes = {name: info.palette for name, info in self.infos.items()}
        self._banners.clear()
        if self.name not in self.themes:
            self.name = DEFAULT_THEME

    def info(self, name: str | None = None) -> ThemeInfo:
        return self.infos[name or self.name]

    def install(self, src: Path) -> str:
        """Validate and copy a .mttheme into the themes folder. Returns the installed theme's name."""
        info, _warnings = read_theme_pack(src)  # raises if the pack is broken
        slug = re.sub(r"[^\w\-]+", "-", info.name).strip("-").lower() or "theme"
        dest = self.themes_dir / f"{slug}{src.suffix.lower()}"
        for existing in self.infos.values():  # replacing an older copy of the same theme
            if existing.source and existing.name == info.name and existing.source != dest:
                existing.source.unlink(missing_ok=True)
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        self.reload()
        installed = next((n for n, i in self.infos.items() if i.source and i.source.resolve() == dest.resolve()),
                         info.name)
        self.log.info("Installed theme %r -> %s", installed, dest.name)
        return installed

    def remove(self, name: str) -> None:
        info = self.infos.get(name)
        if info is None or info.builtin:
            raise ValueError("built-in themes can't be removed")
        info.source.unlink(missing_ok=True)
        was_active = name == self.name
        self.reload()
        if was_active:
            self.set_theme(DEFAULT_THEME)

    @property
    def effects(self) -> dict[str, Any]:
        return self.infos[self.name].effects

    def banner_image(self, name: str | None = None) -> Any:
        """PIL image of a theme's banner, or None."""
        name = name or self.name
        if name in self._banners:
            return self._banners[name]
        image = None
        info = self.infos.get(name)
        if info and info.banner and info.source:
            try:
                from PIL import Image
                with zipfile.ZipFile(info.source) as zf:
                    image = Image.open(io.BytesIO(zf.read(info.banner)))
                    image.load()
            except Exception as exc:
                self.log.warning("Theme %s: banner unreadable: %s", name, exc)
        self._banners[name] = image
        return image

    # ── public API ────────────────────────────────────────────────────────
    @property
    def palette(self) -> dict[str, Any]:
        return self.themes[self.name]

    def names(self) -> list[str]:
        return list(self.themes)

    def set_theme(self, name: str) -> None:
        if name not in self.themes:
            self.log.warning("Unknown theme %r", name)
            return
        self.name = name
        self._settings.set("core", "theme", name)
        started = time.perf_counter()
        self.apply()
        self.log.info("Theme -> %s (%.0f ms)", name, (time.perf_counter() - started) * 1000)
        palette = dict(self.palette)
        for owner, fn in list(self._listeners):
            try:
                fn(palette)
            except Exception:
                self.log.exception("Theme listener from %s failed", owner)

    def subscribe(self, fn: Callable[[dict[str, Any]], None], owner: str = "core") -> Callable[[], None]:
        entry = (owner, fn)
        self._listeners.append(entry)

        def unsubscribe() -> None:
            if entry in self._listeners:
                self._listeners.remove(entry)
        return unsubscribe

    def unsubscribe_owner(self, owner: str) -> None:
        self._listeners = [(o, f) for (o, f) in self._listeners if o != owner]

    @staticmethod
    def tag(widget: Any, role: str | None, **options: Any) -> Any:
        """Assign a theme role (and extras like font_size=18, keep_shape=True). Returns widget."""
        widget._mt_role = role
        for key, value in options.items():
            setattr(widget, f"_mt_{key}", value)
        return widget

    def font(self, size: int = 13, weight: str = "normal", family: str | None = None,
             mono: bool = False) -> ctk.CTkFont:
        p = self.palette
        fam = family or (p["mono_font"] if mono else p["font"])
        key = (fam, size, weight)
        if key not in self._fonts:
            self._fonts[key] = ctk.CTkFont(family=fam, size=size, weight=weight)
        return self._fonts[key]

    def apply(self, widget: Any = None) -> None:
        target = widget if widget is not None else self._root
        if target is self._root:
            parent_bg = self.palette["bg"]
        else:
            parent_bg = self.effective_bg(_container_of(target).master)
        try:
            self._walk(target, parent_bg, 0)
        except Exception:
            self.log.exception("Theme apply failed")

    def effective_bg(self, widget: Any) -> str:
        w = widget
        while w is not None:
            try:
                if isinstance(w, (ctk.CTk, ctk.CTkToplevel)):
                    return _resolve_color(w.cget("fg_color"))
                if _is_ctk(w):
                    fg = w.cget("fg_color")
                    if fg and fg != "transparent":
                        return _resolve_color(fg)
                else:
                    bg = w.cget("bg")
                    if bg:
                        return bg
            except Exception:
                pass
            w = getattr(w, "master", None)
        return self.palette["bg"]

    # ── tree walk ─────────────────────────────────────────────────────────
    def _walk(self, w: Any, parent_bg: str, depth: int) -> None:
        if depth > 250:
            return
        try:
            if not w.winfo_exists():
                return
        except Exception:
            return
        role = getattr(w, "_mt_role", None)
        if role == "skip":
            return
        if isinstance(w, ctk.CTkFrame) and not isinstance(w, ctk.CTkSegmentedButton):
            scroll = self._scroll_child(w)
            if scroll is not None:  # hidden host of a CTkScrollableFrame -> style the real widget
                self._walk(scroll, parent_bg, depth + 1)
                return
        own_bg = self._style(w, role, parent_bg)
        for child in self._children(w):
            self._walk(child, own_bg, depth + 1)

    @staticmethod
    def _scroll_child(frame: Any) -> Any:
        try:
            for child in frame.winfo_children():
                if isinstance(child, tk.Canvas):
                    for grandchild in child.winfo_children():
                        if isinstance(grandchild, ctk.CTkScrollableFrame):
                            return grandchild
        except Exception:
            pass
        return None

    @staticmethod
    def _children(w: Any) -> list[Any]:
        if isinstance(w, ctk.CTkTabview):
            return list(getattr(w, "_tab_dict", {}).values())
        if isinstance(w, ctk.CTkSegmentedButton):
            return []
        is_container = isinstance(w, (ctk.CTk, ctk.CTkToplevel, ctk.CTkFrame, ctk.CTkScrollableFrame)) or (
            isinstance(w, (tk.Frame, tk.Toplevel, tk.Tk)) and not _is_ctk(w)
        )
        if not is_container:
            return []  # never descend into a CTk widget's private internals
        try:
            kids = w.winfo_children()
        except Exception:
            return []
        if _CTkCanvas is not None:
            kids = [k for k in kids if not isinstance(k, _CTkCanvas)]
        return kids

    def _style(self, w: Any, role: str | None, parent_bg: str) -> str:
        p = self.palette
        shape = {} if getattr(w, "_mt_keep_shape", False) else {"corner_radius": p["radius"]}

        if isinstance(w, (ctk.CTk, ctk.CTkToplevel)):
            color = p[FRAME_ROLE_COLORS.get(role or "bg", "bg")]
            _safe_configure(w, fg_color=color)
            return color

        if isinstance(w, ctk.CTkScrollableFrame):
            color = p[FRAME_ROLE_COLORS[role]] if role in FRAME_ROLE_COLORS else parent_bg
            _safe_configure(w, fg_color=color, bg_color=parent_bg,
                            scrollbar_button_color=p["scrollbar"],
                            scrollbar_button_hover_color=p["scrollbar_hover"])
            if role == "card":
                _safe_configure(w, border_color=p["border"], border_width=p["card_border_width"])
            return color

        if not _is_ctk(w):
            return self._style_tk(w, role, parent_bg)

        _safe_configure(w, bg_color=parent_bg)

        if isinstance(w, ctk.CTkSegmentedButton):
            _safe_configure(w, fg_color=p["control"], selected_color=p["selected"],
                            selected_hover_color=p["selected_hover"], unselected_color=p["control"],
                            unselected_hover_color=p["control_hover"], text_color=p["text"],
                            text_color_disabled=p["text_muted"])
            return parent_bg

        if isinstance(w, ctk.CTkFrame):
            try:
                current = w.cget("fg_color")
            except Exception:
                current = None
            if role in FRAME_ROLE_COLORS:
                color = p[FRAME_ROLE_COLORS[role]]
                kwargs: dict[str, Any] = {"fg_color": color}
                if role == "card":
                    kwargs.update(border_color=p["border"], border_width=p["card_border_width"], **shape)
                _safe_configure(w, **kwargs)
                return color
            if role == "transparent" or current == "transparent":
                if current != "transparent":
                    _safe_configure(w, fg_color="transparent")
                return parent_bg
            color = p["card"]  # untagged opaque frame -> card colours, keeps its own geometry
            _safe_configure(w, fg_color=color, border_color=p["border"])
            return color

        if isinstance(w, ctk.CTkTabview):
            _safe_configure(w, fg_color=p["card"], border_color=p["border"],
                            segmented_button_fg_color=p["control"],
                            segmented_button_selected_color=p["selected"],
                            segmented_button_selected_hover_color=p["selected_hover"],
                            segmented_button_unselected_color=p["control"],
                            segmented_button_unselected_hover_color=p["control_hover"],
                            text_color=p["text"])
            return p["card"]

        if isinstance(w, ctk.CTkButton):
            self._style_button(w, role, shape)
        elif isinstance(w, ctk.CTkLabel):
            _safe_configure(w, text_color=p[LABEL_ROLE_COLORS.get(role, "text")])
            if role == "title":
                size = int(getattr(w, "_mt_font_size", 18))
                _safe_configure(w, font=self.font(size, "bold", family=p["title_font"]))
        elif isinstance(w, ctk.CTkEntry):
            _safe_configure(w, fg_color=p["entry_bg"], border_color=p["border"], text_color=p["text"],
                            placeholder_text_color=p["text_muted"], **shape)
        elif isinstance(w, ctk.CTkTextbox):
            _safe_configure(w, fg_color=p["entry_bg"], border_color=p["border"], text_color=p["text"],
                            scrollbar_button_color=p["scrollbar"],
                            scrollbar_button_hover_color=p["scrollbar_hover"], **shape)
        elif isinstance(w, ctk.CTkOptionMenu):
            _safe_configure(w, fg_color=p["control"], button_color=p["control_hover"],
                            button_hover_color=p["selected"], text_color=p["text"],
                            dropdown_fg_color=p["card"], dropdown_hover_color=p["control_hover"],
                            dropdown_text_color=p["text"], **shape)
        elif isinstance(w, ctk.CTkComboBox):
            _safe_configure(w, fg_color=p["entry_bg"], border_color=p["border"],
                            button_color=p["control_hover"], button_hover_color=p["selected"],
                            text_color=p["text"], dropdown_fg_color=p["card"],
                            dropdown_hover_color=p["control_hover"], dropdown_text_color=p["text"], **shape)
        elif isinstance(w, ctk.CTkSwitch):
            _safe_configure(w, fg_color=p["control_hover"], progress_color=p["accent"],
                            button_color=p["text"], button_hover_color=p["title"], text_color=p["text"])
        elif isinstance(w, ctk.CTkCheckBox):
            _safe_configure(w, fg_color=p["accent"], hover_color=p["accent_hover"],
                            border_color=p["border_strong"], checkmark_color=p["accent_text"],
                            text_color=p["text"])
        elif isinstance(w, ctk.CTkRadioButton):
            _safe_configure(w, fg_color=p["accent"], hover_color=p["accent_hover"],
                            border_color=p["border_strong"], text_color=p["text"])
        elif isinstance(w, ctk.CTkProgressBar):
            _safe_configure(w, fg_color=p["control"], progress_color=p["accent"], border_color=p["border"])
        elif isinstance(w, ctk.CTkSlider):
            _safe_configure(w, fg_color=p["control"], progress_color=p["accent"],
                            button_color=p["accent"], button_hover_color=p["accent_hover"])
        elif isinstance(w, ctk.CTkScrollbar):
            _safe_configure(w, fg_color="transparent", button_color=p["scrollbar"],
                            button_hover_color=p["scrollbar_hover"])
        return parent_bg

    def _style_button(self, w: Any, role: str | None, shape: dict[str, Any]) -> None:
        p = self.palette
        common = {"text_color_disabled": p["text_muted"], **shape}
        if role == "nav":
            if getattr(w, "_mt_active", False):
                _safe_configure(w, fg_color=p["nav_active_bg"], hover_color=p["nav_active_bg"],
                                text_color=p["nav_active_text"], border_color=p["nav_active_border"],
                                border_width=p["nav_active_border_width"], **common)
            else:
                text = p["danger"] if getattr(w, "_mt_error", False) else p["text_muted"]
                _safe_configure(w, fg_color="transparent", hover_color=p["card_hover"],
                                text_color=text, border_width=0, **common)
        elif role == "secondary":
            _safe_configure(w, fg_color="transparent", hover_color=p["card_hover"], text_color=p["text"],
                            border_color=p["border_strong"], border_width=1, **common)
        elif role == "ghost":
            _safe_configure(w, fg_color="transparent", hover_color=p["card_hover"],
                            text_color=p["text_muted"], border_width=0, **common)
        elif role == "danger":
            _safe_configure(w, fg_color=p["danger"], hover_color=p["danger_hover"], text_color="#ffffff",
                            border_width=0, **common)
        else:
            _safe_configure(w, fg_color=p["accent"], hover_color=p["accent_hover"],
                            text_color=p["accent_text"], border_width=0, **common)

    def _style_tk(self, w: Any, role: str | None, parent_bg: str) -> str:
        """Plain tkinter widgets a plugin might use (Listbox for long lists, Canvas, Text)."""
        p = self.palette
        try:
            if isinstance(w, tk.Toplevel) or isinstance(w, tk.Canvas):
                if role is None:
                    return parent_bg  # overlays / custom canvases: hands off unless tagged
                color = parent_bg if role == "transparent" else p[FRAME_ROLE_COLORS.get(role, "surface")]
                w.configure(bg=color, highlightthickness=0)
                return color
            if isinstance(w, tk.Frame):
                color = p[FRAME_ROLE_COLORS[role]] if role in FRAME_ROLE_COLORS else parent_bg
                w.configure(bg=color)
                return color
            if isinstance(w, tk.Listbox):
                w.configure(bg=p["entry_bg"], fg=p["text"], selectbackground=p["selected"],
                            selectforeground="#ffffff", highlightthickness=0, borderwidth=0)
            elif isinstance(w, tk.Text):
                w.configure(bg=p["entry_bg"], fg=p["text"], insertbackground=p["text"],
                            selectbackground=p["selected"], highlightthickness=0, borderwidth=0)
            elif isinstance(w, tk.Label):
                w.configure(bg=parent_bg, fg=p[LABEL_ROLE_COLORS.get(role, "text")])
        except Exception:
            pass
        return parent_bg


class ThemeHandle:
    """Per-plugin view of the theme engine (subscriptions are cleaned up on unload)."""

    def __init__(self, manager: ThemeManager, owner: str):
        self._m = manager
        self._owner = owner

    @property
    def name(self) -> str:
        return self._m.name

    @property
    def palette(self) -> dict[str, Any]:
        return dict(self._m.palette)

    def tag(self, widget: Any, role: str | None, **options: Any) -> Any:
        return self._m.tag(widget, role, **options)

    def apply(self, widget: Any) -> None:
        self._m.apply(widget)

    def font(self, size: int = 13, weight: str = "normal", family: str | None = None,
             mono: bool = False) -> ctk.CTkFont:
        return self._m.font(size, weight, family, mono)

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        return self._m.subscribe(fn, owner=self._owner)


# ════════════════════════════════════════════════════════════════════════════
#  Drag and drop
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class DropPayload:
    files: list[Path] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    text: str = ""
    raw: str = ""

    @property
    def kind(self) -> str:
        if self.files:
            return "files"
        if self.urls:
            return "urls"
        return "text"

    def summary(self) -> str:
        parts = []
        if self.files:
            parts.append(f"{len(self.files)} file(s)")
        if self.urls:
            parts.append(f"{len(self.urls)} link(s)")
        if not parts and self.text:
            parts.append(f"{len(self.text)} chars of text")
        return ", ".join(parts) or "nothing"


def _read_url_shortcut(path: Path) -> str | None:
    """Browsers sometimes hand over a .url Internet Shortcut instead of plain text."""
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().lower().startswith("url="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_drop_data(widget: Any, data: str) -> DropPayload:
    data = data or ""
    try:
        items = list(widget.tk.splitlist(data)) if data else []
    except Exception:
        items = []

    def _exists(item: str) -> bool:
        try:
            return bool(item) and Path(item).exists()
        except (OSError, ValueError):
            return False

    if items and len(items) <= 20000 and all(_exists(i) for i in items):
        files: list[Path] = []
        urls: list[str] = []
        for item in items:
            p = Path(item)
            if p.suffix.lower() == ".url":
                url = _read_url_shortcut(p)
                if url:
                    urls.append(url)
                    continue
            files.append(p)
        return DropPayload(files=files, urls=_dedupe(urls), text="", raw=data)

    text = data.strip()
    urls = [u.rstrip(").,;]'\"") for u in URL_RE.findall(text)]
    return DropPayload(files=[], urls=_dedupe(urls), text=text, raw=data)


def _iter_widget_tree(widget: Any) -> Iterator[Any]:
    yield widget
    try:
        children = widget.winfo_children()
    except Exception:
        return
    for child in children:
        yield from _iter_widget_tree(child)


def _is_descendant(widget: Any, ancestor: Any) -> bool:
    w = getattr(widget, "master", None)
    while w is not None:
        if w is ancestor:
            return True
        w = getattr(w, "master", None)
    return False


# ════════════════════════════════════════════════════════════════════════════
#  Plugin system
# ════════════════════════════════════════════════════════════════════════════

class PluginError(Exception):
    """Contract violation; shown to the user without a traceback."""


@dataclass
class PluginRecord:
    key: str
    path: Path
    is_package: bool
    module_name: str
    title: str
    description: str = ""
    icon: str = "◆"
    order: int = 100
    enabled: bool = True
    module: Any = None
    instance: Any = None
    ctx: Any = None
    error: str | None = None

    @property
    def display_path(self) -> str:
        return f"{self.path.parent.name}/" if self.is_package else self.path.name

    @property
    def status(self) -> str:
        if not self.enabled:
            return "Disabled"
        return "Error" if self.error else "Loaded"


def _prettify(key: str) -> str:
    return re.sub(r"[_\-]+", " ", key).strip().title() or key


def discover_plugins(plugins_dir: Path) -> list[tuple[str, Path, bool]]:
    found: list[tuple[str, Path, bool]] = []
    if not plugins_dir.is_dir():
        return found
    for entry in sorted(plugins_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(("_", ".")):
            continue
        if entry.is_file() and entry.suffix.lower() == ".py":
            found.append((entry.stem, entry, False))
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            found.append((entry.name, entry / "__init__.py", True))
    return found


def import_plugin_module(rec: PluginRecord) -> types.ModuleType:
    if PLUGIN_NAMESPACE not in sys.modules:
        namespace = types.ModuleType(PLUGIN_NAMESPACE)
        namespace.__path__ = []  # type: ignore[attr-defined]
        sys.modules[PLUGIN_NAMESPACE] = namespace
    spec = importlib.util.spec_from_file_location(
        rec.module_name,
        rec.path,
        submodule_search_locations=[str(rec.path.parent)] if rec.is_package else None,
    )
    if spec is None or spec.loader is None:
        raise PluginError(f"Python could not create an import spec for {rec.path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[rec.module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(rec.module_name, None)
        raise
    return module


def instantiate_plugin(cls: type, ctx: "PluginContext") -> Any:
    try:
        params = [
            p for p in inspect.signature(cls).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
        ]
    except (TypeError, ValueError):
        params = []
    if params:
        return cls(ctx)
    instance = cls()
    instance.ctx = ctx
    return instance


class PluginContext:
    """Everything a plugin may touch. Keeps plugins decoupled from shell internals."""

    def __init__(self, app: "App", plugin_id: str, plugin_path: Path):
        self._app = app
        self.plugin_id = plugin_id
        self.plugin_path = plugin_path
        self.log = app.log.getChild(f"plugin.{plugin_id}")
        self.theme = ThemeHandle(app.theme, plugin_id)

    @property
    def app(self) -> "App":
        return self._app

    @property
    def paths(self) -> AppPaths:
        return self._app.paths

    @property
    def dnd_available(self) -> bool:
        return self._app.dnd_available

    @property
    def data_dir(self) -> Path:
        d = self._app.paths.plugin_data_dir / self.plugin_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self._app.settings.get(f"plugin:{self.plugin_id}", key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self._app.settings.set(f"plugin:{self.plugin_id}", key, value)

    def bin_path(self, name: str, required: bool = False) -> Path | None:
        return find_binary(self._app.paths.bin_dir, name, required)

    @staticmethod
    def subprocess_kwargs() -> dict[str, Any]:
        return hidden_subprocess_kwargs()

    def register_drop_target(self, widget: Any, callback: Callable[[DropPayload], None],
                             accept: tuple[str, ...] = ("files", "text")) -> bool:
        return self._app.register_drop_target(widget, callback, accept, owner=self.plugin_id)

    def call_in_ui(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self._app.call_in_ui(fn, *args, **kwargs)

    def run_in_thread(self, fn: Callable[..., Any], *args: Any,
                      on_done: Callable[[Any], None] | None = None,
                      on_error: Callable[[BaseException], None] | None = None,
                      **kwargs: Any) -> threading.Thread:
        return self._app.run_in_thread(fn, *args, on_done=on_done, on_error=on_error,
                                       name=f"{self.plugin_id}-worker", **kwargs)

    def copy_to_clipboard(self, text: str) -> None:
        self._app.copy_to_clipboard(text)

    def register_file_type(self, extension: str, label: str, handler: Callable[[Path], Any]) -> None:
        """Let Settings ▸ Install… (and drops on the Settings page) accept a file type for this plugin,
        e.g. ctx.register_file_type(".mtchar", "Companion characters", self.import_character)."""
        self._app.register_file_type(extension, label, handler, owner=self.plugin_id)

    def set_status(self, text: str, level: str = "info") -> None:
        self._app.call_in_ui(self._app.set_status, text, level)

    def open_path(self, path: Path | str) -> None:
        self._app.open_path(path)


# ════════════════════════════════════════════════════════════════════════════
#  Main window
# ════════════════════════════════════════════════════════════════════════════

class App(ctk.CTk, _DnDBase):
    def __init__(self, paths: AppPaths, log: logging.Logger):
        super().__init__()
        self.paths = paths
        self.log = log
        self.dnd_available = False
        self.dnd_version: str | None = None
        self.dnd_error: str | None = None
        self._init_dnd()

        self.settings = SettingsStore(paths.settings_file, log)
        self.theme = ThemeManager(self, self.settings, paths.themes_dir, log)
        self.theme.subscribe(self._notify_plugins_theme, owner="core")
        self.theme.subscribe(lambda _p: self._apply_shell_effects(), owner="core")
        self._file_types: dict[str, tuple[str, str, Callable[[Path], Any]]] = {}
        self._raw_title = ""
        self._default_tagline = "Desktop Utility Belt"
        self._glitch_after: str | None = None
        self._banner_image: Any = None

        self.records: list[PluginRecord] = []
        self._pages: dict[str, Any] = {}
        self._nav_buttons: dict[str, Any] = {}
        self._active_key: str | None = None
        self._ui_queue: "queue.Queue[tuple[Callable[..., Any], tuple, dict]]" = queue.Queue()
        self._status_after: str | None = None
        self._closing = False

        self.title(APP_NAME)
        self.minsize(960, 600)
        self._restore_geometry()
        self._set_icon()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_shell()
        self._load_plugins()
        self._rebuild_nav()
        self.theme.apply()
        self._apply_shell_effects()
        self.select(self._initial_page())

        self.bind("<Control-R>", lambda _e: self.reload_plugins())  # Ctrl+Shift+R
        self.after(20, self._pump_ui_queue)
        loaded = sum(1 for r in self.records if r.instance is not None)
        failed = sum(1 for r in self.records if r.error)
        self.set_status(f"{loaded} plugin(s) loaded" + (f", {failed} failed" if failed else ""),
                        "warning" if failed else "info")

    # ── setup ─────────────────────────────────────────────────────────────
    def _init_dnd(self) -> None:
        if TkinterDnD is None:
            self.dnd_error = f"tkinterdnd2 could not be imported: {_DND_IMPORT_ERROR}"
            self.log.warning(self.dnd_error)
            return
        try:
            self.dnd_version = str(TkinterDnD._require(self))
            self.dnd_available = True
            self.log.info("tkdnd %s loaded", self.dnd_version)
        except Exception as exc:
            # Typical cause in a frozen build: tkdnd binaries not collected by PyInstaller
            self.dnd_error = f"tkdnd library failed to load: {exc}"
            self.log.warning(self.dnd_error)

    def _set_icon(self) -> None:
        for base in (self.paths.resource_dir, self.paths.app_dir):
            ico = base / "assets" / "icon.ico"
            if ico.is_file() and IS_WINDOWS:
                try:
                    self.iconbitmap(str(ico))
                    return
                except Exception:
                    self.log.debug("iconbitmap failed for %s", ico)

    def _screen_bounds(self) -> tuple[int, int, int, int]:
        if IS_WINDOWS:
            try:
                gsm = ctypes.windll.user32.GetSystemMetrics
                return gsm(76), gsm(77), gsm(78), gsm(79)  # virtual desktop (all monitors)
            except Exception:
                pass
        return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

    def _restore_geometry(self) -> None:
        geometry = self.settings.get("core", "geometry", "") or ""
        match = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geometry)
        if match:
            w, h, x, y = (int(g) for g in match.groups())
            w, h = max(w, 960), max(h, 600)
            vx, vy, vw, vh = self._screen_bounds()
            if vx - 50 <= x <= vx + vw - 150 and vy - 10 <= y <= vy + vh - 100:
                self.geometry(f"{w}x{h}+{x}+{y}")
            else:
                self.geometry(f"{w}x{h}")
        else:
            self.geometry("1200x760")
        if self.settings.get("core", "maximized", False) and IS_WINDOWS:
            self.after(60, lambda: self.state("zoomed"))

    def _build_shell(self) -> None:
        tag = self.theme.tag
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._dividers: list[Any] = []
        self._theme_menus: list[Any] = []

        # ── sidebar ──
        self._sidebar = tag(ctk.CTkFrame(self, width=236, corner_radius=0), "sidebar", keep_shape=True)
        self._sidebar.grid(row=0, column=0, sticky="nsw")
        self._sidebar.grid_propagate(False)
        self._sidebar.grid_columnconfigure(0, weight=1)
        self._sidebar.grid_rowconfigure(2, weight=1)

        self._brand_title = tag(ctk.CTkLabel(self._sidebar, text=APP_NAME, anchor="w"), "title", font_size=21)
        self._brand_title.grid(row=0, column=0, sticky="ew", padx=18, pady=(20, 0))
        self._brand_banner = tag(ctk.CTkLabel(self._sidebar, text=""), "transparent")
        self._brand_sub = tag(ctk.CTkLabel(self._sidebar, text="Desktop Utility Belt", anchor="w"), "muted")
        self._brand_sub.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 12))

        self._nav_frame = tag(ctk.CTkScrollableFrame(self._sidebar, corner_radius=0), "transparent")
        self._nav_frame.grid(row=2, column=0, sticky="nsew", padx=(8, 4))
        self._nav_frame.grid_columnconfigure(0, weight=1)

        bottom = tag(ctk.CTkFrame(self._sidebar, fg_color="transparent"), "transparent")
        bottom.grid(row=3, column=0, sticky="ew", padx=12, pady=(6, 14))
        bottom.grid_columnconfigure(0, weight=1)
        divider = tag(ctk.CTkFrame(bottom, height=1, corner_radius=0, border_width=0), "skip")
        divider.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._dividers.append(divider)
        tag(ctk.CTkLabel(bottom, text="THEME", anchor="w", height=16,
                         font=self.theme.font(11, "bold")), "muted").grid(row=1, column=0, sticky="ew", padx=4)
        self._theme_var = ctk.StringVar(value=self.theme.name)
        menu = ctk.CTkOptionMenu(bottom, values=self.theme.names(), variable=self._theme_var,
                                 command=self._on_theme_selected, dynamic_resizing=False)
        menu.grid(row=2, column=0, sticky="ew", pady=(2, 8))
        self._theme_menus.append(menu)

        # optional coloured stripe between sidebar and content (theme effect)
        self._stripe = tag(ctk.CTkFrame(self, width=0, corner_radius=0, border_width=0), "skip")
        self._make_nav_button(bottom, SETTINGS_KEY, "⚙", "Settings").grid(row=3, column=0, sticky="ew")

        # ── main area ──
        main = tag(ctk.CTkFrame(self, corner_radius=0), "surface", keep_shape=True)
        main.grid(row=0, column=2, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        header = tag(ctk.CTkFrame(main, fg_color="transparent"), "transparent")
        header.grid(row=0, column=0, sticky="ew", padx=26, pady=(20, 10))
        header.grid_columnconfigure(0, weight=1)
        self._page_title = tag(ctk.CTkLabel(header, text="", anchor="w"), "title", font_size=24)
        self._page_title.grid(row=0, column=0, sticky="ew")
        self._page_desc = tag(ctk.CTkLabel(header, text="", anchor="w", justify="left"), "muted")
        self._page_desc.grid(row=1, column=0, sticky="ew")
        header.bind("<Configure>", lambda e: self._page_desc.configure(wraplength=max(240, e.width - 20)))

        divider = tag(ctk.CTkFrame(main, height=1, corner_radius=0, border_width=0), "skip")
        divider.grid(row=1, column=0, sticky="ew", padx=26)
        self._dividers.append(divider)

        self.viewport = tag(ctk.CTkFrame(main, fg_color="transparent", corner_radius=0), "transparent")
        self.viewport.grid(row=2, column=0, sticky="nsew", padx=26, pady=16)
        self.viewport.grid_columnconfigure(0, weight=1)
        self.viewport.grid_rowconfigure(0, weight=1)

        status = tag(ctk.CTkFrame(main, height=30, corner_radius=0), "statusbar", keep_shape=True)
        status.grid(row=3, column=0, sticky="ew")
        status.grid_columnconfigure(0, weight=1)
        self._status_label = tag(ctk.CTkLabel(status, text="Ready", anchor="w", height=26), "muted")
        self._status_label.grid(row=0, column=0, sticky="ew", padx=14)
        dnd_text = "Drag & drop ready" if self.dnd_available else "Drag & drop unavailable"
        tag(ctk.CTkLabel(status, text=dnd_text, anchor="e", height=26),
            "muted" if self.dnd_available else "warning").grid(row=0, column=1, sticky="e", padx=14)

    def _make_nav_button(self, parent: Any, key: str, icon: str, title: str) -> Any:
        btn = ctk.CTkButton(parent, text=f"{icon}   {title}", anchor="w", height=36,
                            command=lambda k=key: self.select(k))
        self.theme.tag(btn, "nav", active=False, error=False, text=f"{icon}   {title}")
        self._nav_buttons[key] = btn
        self._render_nav_text(btn)
        return btn

    # ── theme effects (shell decorations a theme pack can enable) ─────────
    def _render_nav_text(self, btn: Any) -> None:
        e = self.theme.effects
        text = getattr(btn, "_mt_text", "") or ""
        if getattr(btn, "_mt_active", False) and e["nav_active_prefix"]:
            text = e["nav_active_prefix"] + text
        if e["uppercase_nav"]:
            text = text.upper()
        try:
            btn.configure(text=text, font=self.theme.font(13, family=self.theme.palette["font"]))
        except Exception:
            pass

    def _rendered_title(self) -> str:
        e = self.theme.effects
        text = self._raw_title
        if text and e["uppercase_titles"]:
            text = text.upper()
        if text and e["title_prefix"]:
            text = e["title_prefix"] + text
        return text

    def _apply_shell_effects(self) -> None:
        e, p = self.theme.effects, self.theme.palette
        # brand: banner image from the theme pack, or the app name
        image = self.theme.banner_image()
        if image is not None:
            w, h = image.size
            scale = min(204 / max(w, 1), 72 / max(h, 1), 1.0)
            self._banner_image = ctk.CTkImage(light_image=image, dark_image=image,
                                              size=(max(1, int(w * scale)), max(1, int(h * scale))))
            self._brand_banner.configure(image=self._banner_image)
            self._brand_title.grid_remove()
            self._brand_banner.grid(row=0, column=0, sticky="w", padx=14, pady=(16, 2))
        else:
            self._banner_image = None
            self._brand_banner.grid_remove()
            self._brand_title.grid()
        self._brand_sub.configure(text=e["brand_tagline"] or self._default_tagline)
        # dividers and stripe
        divider_color = p.get(e["divider_color"], p["border"])
        for divider in self._dividers:
            _safe_configure(divider, height=max(1, e["divider_height"]), fg_color=divider_color,
                            bg_color=divider_color)
        if e["sidebar_stripe"] > 0:
            color = p.get(e["sidebar_stripe_color"], p["accent"])
            _safe_configure(self._stripe, width=e["sidebar_stripe"], fg_color=color, bg_color=color)
            self._stripe.grid(row=0, column=1, sticky="ns")
        else:
            self._stripe.grid_remove()
        # titles, nav, glitch
        self._page_title.configure(text=self._rendered_title(), text_color=p["title"])
        for btn in self._nav_buttons.values():
            self._render_nav_text(btn)
        self._schedule_glitch()

    GLITCH_CHARS = "▓▒░█#@%&/\\<>[]=+*01"

    def _schedule_glitch(self) -> None:
        if self._glitch_after is not None:
            try:
                self.after_cancel(self._glitch_after)
            except Exception:
                pass
            self._glitch_after = None
        if self.theme.effects["glitch_titles"] and not self._closing:
            self._glitch_after = self.after(random.randint(3500, 9000), self._glitch_step, 0)

    def _glitch_step(self, frame: int) -> None:
        self._glitch_after = None
        e, p = self.theme.effects, self.theme.palette
        if self._closing or not e["glitch_titles"]:
            return
        base = self._rendered_title()
        colors = e["glitch_colors"] or [p["accent2"], p["danger"]]
        if frame < 4 and base.strip():
            chars = list(base)
            for _ in range(max(1, len(chars) // 5)):
                i = random.randrange(len(chars))
                if not chars[i].isspace():
                    chars[i] = random.choice(self.GLITCH_CHARS)
            self._page_title.configure(text="".join(chars), text_color=colors[frame % len(colors)])
            self._glitch_after = self.after(70, self._glitch_step, frame + 1)
        else:
            self._page_title.configure(text=base, text_color=p["title"])
            self._schedule_glitch()

    # ── plugin lifecycle ──────────────────────────────────────────────────
    def _load_plugins(self) -> None:
        disabled = set(self.settings.get("core", "disabled_plugins", []) or [])
        self.records = []
        used: set[str] = set()
        started = time.perf_counter()
        for raw_key, path, is_package in discover_plugins(self.paths.plugins_dir):
            base = re.sub(r"\W", "_", raw_key).lower()
            key, n = base, 2
            while key in used or key in (SETTINGS_KEY, EMPTY_KEY):
                key, n = f"{base}_{n}", n + 1
            used.add(key)
            rec = PluginRecord(key=key, path=path, is_package=is_package,
                               module_name=f"{PLUGIN_NAMESPACE}.{key}", title=_prettify(raw_key))
            if key in disabled:
                rec.enabled = False
            else:
                self._import_and_init(rec)
            self.records.append(rec)
        self.log.info("Plugin scan: %d found in %.0f ms (%s)", len(self.records),
                      (time.perf_counter() - started) * 1000, self.paths.plugins_dir)

    def _import_and_init(self, rec: PluginRecord) -> None:
        try:
            module = import_plugin_module(rec)
            rec.module = module
            cls = getattr(module, "Plugin", None)
            if cls is None or not inspect.isclass(cls):
                raise PluginError(f"{rec.display_path} must define a class named `Plugin`.")
            rec.title = str(getattr(cls, "meta_title", None) or rec.title)
            needed = int(getattr(cls, "meta_api_version", 1))
            if needed > PLUGIN_API_VERSION:
                raise PluginError(f"Needs plugin API v{needed}; this build provides v{PLUGIN_API_VERSION}.")
            if not callable(getattr(cls, "get_frame", None)):
                raise PluginError("The Plugin class must implement get_frame(self, master).")
            rec.ctx = PluginContext(self, rec.key, rec.path)
            instance = instantiate_plugin(cls, rec.ctx)
            rec.instance = instance
            rec.title = str(getattr(instance, "meta_title", None) or rec.title)
            rec.description = str(getattr(instance, "meta_description", "") or "")
            icon = str(getattr(instance, "meta_icon", "") or "").strip()
            rec.icon = icon[:2] if icon else "◆"
            try:
                rec.order = int(getattr(instance, "meta_order", 100))
            except (TypeError, ValueError):
                rec.order = 100
            self.log.info("Loaded plugin %s (%s)", rec.key, rec.title)
        except PluginError as exc:
            rec.error = str(exc)
            self.log.error("Plugin %s rejected: %s", rec.key, exc)
        except KeyboardInterrupt:
            raise
        except BaseException:  # includes SystemExit from a badly written plugin
            rec.error = traceback.format_exc()
            self.log.error("Plugin %s failed to load:\n%s", rec.key, rec.error)

    def _safe_call(self, rec: PluginRecord, method: str, *args: Any) -> bool:
        fn = getattr(rec.instance, method, None)
        if not callable(fn):
            return True
        started = time.perf_counter()
        try:
            fn(*args)
            return True
        except Exception:
            self.log.exception("%s.%s() raised", rec.key, method)
            return False
        finally:
            elapsed = time.perf_counter() - started
            if elapsed > 1.0:
                self.log.warning("%s.%s() took %.1fs (keep hooks fast; use threads)", rec.key, method, elapsed)

    def _notify_plugins_theme(self, palette: dict[str, Any]) -> None:
        for rec in self.records:
            if rec.instance is not None:
                self._safe_call(rec, "on_theme_changed", dict(palette))

    def _unload_plugins(self) -> None:
        for rec in self.records:
            if rec.instance is not None:
                if rec.key == self._active_key:
                    self._safe_call(rec, "on_hide")
                self._safe_call(rec, "shutdown")
            self.theme.unsubscribe_owner(rec.key)
            for ext in [e for e, (owner, _l, _h) in self._file_types.items() if owner == rec.key]:
                del self._file_types[ext]
        for frame in self._pages.values():
            try:
                _container_of(frame).destroy()
            except Exception:
                pass
        self._pages.clear()
        self._active_key = None
        for name in [n for n in sys.modules if n.startswith(PLUGIN_NAMESPACE + ".")]:
            sys.modules.pop(name, None)
        importlib.invalidate_caches()
        self.records = []

    def reload_plugins(self) -> None:
        previous = self._active_key
        self.log.info("Reloading plugins")
        self._unload_plugins()
        self._load_plugins()
        self._rebuild_nav()
        keys = self._visible_keys()
        self.select(previous if previous in keys or previous == SETTINGS_KEY else self._initial_page())
        loaded = sum(1 for r in self.records if r.instance is not None)
        failed = sum(1 for r in self.records if r.error)
        self.set_status(f"Reloaded: {loaded} loaded" + (f", {failed} failed" if failed else ""),
                        "warning" if failed else "success")

    # ── navigation ────────────────────────────────────────────────────────
    def _visible_records(self) -> list[PluginRecord]:
        visible = [r for r in self.records if r.enabled and (r.instance is not None or r.error)]
        return sorted(visible, key=lambda r: (r.order, r.title.lower()))

    def _visible_keys(self) -> list[str]:
        return [r.key for r in self._visible_records()]

    def _record(self, key: str) -> PluginRecord | None:
        return next((r for r in self.records if r.key == key), None)

    def _initial_page(self) -> str:
        keys = self._visible_keys()
        last = self.settings.get("core", "last_page")
        if last in keys or last == SETTINGS_KEY:
            return last
        return keys[0] if keys else EMPTY_KEY

    def _rebuild_nav(self) -> None:
        for child in self._nav_frame.winfo_children():
            child.destroy()
        self._nav_buttons = {k: b for k, b in self._nav_buttons.items() if k == SETTINGS_KEY}
        visible = self._visible_records()
        for row, rec in enumerate(visible):
            btn = self._make_nav_button(self._nav_frame, rec.key, "⚠" if rec.error else rec.icon, rec.title)
            btn._mt_error = bool(rec.error)
            btn.grid(row=row, column=0, sticky="ew", pady=1)
        if not visible:
            self.theme.tag(ctk.CTkLabel(self._nav_frame, text="No plugins loaded", anchor="w"), "muted").grid(
                row=0, column=0, sticky="ew", padx=10, pady=6)
        count = sum(1 for r in visible if not r.error)
        self._default_tagline = f"Desktop Utility Belt · {count} tool{'s' if count != 1 else ''}"
        self.theme.apply(self._sidebar)
        self._apply_shell_effects()

    def select(self, key: str) -> None:
        if key == self._active_key and key in self._pages:
            return
        old_key = self._active_key
        if old_key is not None:
            old_frame = self._pages.get(old_key)
            if old_frame is not None:
                _container_of(old_frame).grid_remove()
            old_rec = self._record(old_key)
            if old_rec is not None and old_rec.instance is not None:
                self._safe_call(old_rec, "on_hide")

        frame = self._pages.get(key) or self._build_page(key)
        _container_of(frame).grid(row=0, column=0, sticky="nsew", in_=self.viewport)
        self._active_key = key

        rec = self._record(key)
        if key == SETTINGS_KEY:
            title, desc = "Settings", "Appearance, plugins, bundled engines and storage locations."
        elif key == EMPTY_KEY:
            title, desc = "No plugins found", "Drop plugin .py files into the plugins folder, then reload."
        elif rec is not None:
            title = rec.title
            desc = "This plugin failed to load." if rec.error else rec.description
        else:
            title, desc = "", ""
        self._raw_title = title
        self._page_title.configure(text=self._rendered_title())
        self._page_desc.configure(text=desc)

        for k, btn in self._nav_buttons.items():
            active = k == key
            if getattr(btn, "_mt_active", None) != active:
                btn._mt_active = active
                self._render_nav_text(btn)
                self.theme.apply(btn)

        if key not in (EMPTY_KEY,):
            self.settings.set("core", "last_page", key)
        if rec is not None and rec.instance is not None and not rec.error:
            self._safe_call(rec, "on_show")

    def _build_page(self, key: str) -> Any:
        if key == SETTINGS_KEY:
            frame = self._build_settings_page()
        elif key == EMPTY_KEY:
            frame = self._build_empty_page()
        else:
            rec = self._record(key)
            if rec is None:
                frame = self._build_empty_page()
            elif rec.error or rec.instance is None:
                frame = self._build_error_page(rec)
            else:
                try:
                    frame = rec.instance.get_frame(self.viewport)
                    if frame is None or not isinstance(_container_of(frame), tk.Misc):
                        raise PluginError("get_frame() must return a tkinter/CustomTkinter widget.")
                except Exception as exc:
                    rec.error = str(exc) if isinstance(exc, PluginError) else traceback.format_exc()
                    self.log.error("Plugin %s get_frame failed:\n%s", key, rec.error)
                    btn = self._nav_buttons.get(key)
                    if btn is not None:
                        btn._mt_text = f"⚠   {rec.title}"
                        btn._mt_error = True
                        self._render_nav_text(btn)
                    frame = self._build_error_page(rec)
        if not hasattr(frame, "_mt_role"):
            frame._mt_role = "transparent"  # page roots blend into the viewport by default
        self._pages[key] = frame
        self.theme.apply(frame)
        return frame

    # ── built-in pages ────────────────────────────────────────────────────
    def _card(self, parent: Any, row: int, title: str, subtitle: str | None = None) -> Any:
        tag = self.theme.tag
        card = tag(ctk.CTkFrame(parent), "card")
        card.grid(row=row, column=0, sticky="ew", pady=(0, 14), padx=(0, 8))
        card.grid_columnconfigure(0, weight=1)
        tag(ctk.CTkLabel(card, text=title, anchor="w"), "title", font_size=15).grid(
            row=0, column=0, sticky="ew", padx=18, pady=(14, 2 if subtitle else 10))
        if subtitle:
            tag(ctk.CTkLabel(card, text=subtitle, anchor="w", justify="left", wraplength=700), "muted").grid(
                row=1, column=0, sticky="ew", padx=18, pady=(0, 10))
        body = tag(ctk.CTkFrame(card, fg_color="transparent"), "transparent")
        body.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 16))
        body.grid_columnconfigure(1, weight=1)
        return body

    def _build_settings_page(self) -> Any:
        tag = self.theme.tag
        page = tag(ctk.CTkScrollableFrame(self.viewport, corner_radius=0), "transparent")
        page.grid_columnconfigure(0, weight=1)

        # Appearance — theme gallery
        body = self._card(page, 0, "Themes",
                          "Themes are drop-in packs (.mttheme files). Drop one anywhere on this page or use "
                          "Install theme… — it's applied straight away. Default Dark is built in and always "
                          "here to go back to.")
        bar = tag(ctk.CTkFrame(body, fg_color="transparent"), "transparent")
        bar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ctk.CTkButton(bar, text="＋  Install theme…", width=150,
                      command=lambda: self.ask_install("theme")).pack(side="left")
        tag(ctk.CTkButton(bar, text="Back to default", width=140,
                          command=lambda: self._on_theme_selected(DEFAULT_THEME)), "secondary").pack(
            side="left", padx=8)
        tag(ctk.CTkButton(bar, text="Open themes folder", width=160,
                          command=lambda: self.open_path(self.paths.themes_dir)), "ghost").pack(side="left")
        for i, name in enumerate(self.theme.names(), start=1):
            self._theme_row(body, i, self.theme.info(name))

        # Plugins
        body = self._card(page, 1, "Plugins",
                          f"Scanned from {self.paths.plugins_dir}. Toggle a plugin off to skip loading it "
                          "(useful for heavy tools); changes apply on reload (Ctrl+Shift+R).")
        bar = tag(ctk.CTkFrame(body, fg_color="transparent"), "transparent")
        bar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ctk.CTkButton(bar, text="＋  Install…", width=130, command=self.ask_install).pack(side="left")
        tag(ctk.CTkButton(bar, text="Reload plugins", width=140, command=self.reload_plugins),
            "secondary").pack(side="left", padx=8)
        tag(ctk.CTkButton(bar, text="Open plugins folder", width=160,
                          command=lambda: self.open_path(self.paths.plugins_dir)), "secondary").pack(side="left")
        tag(ctk.CTkLabel(bar, text="or drop plugins, themes or characters anywhere on this page"), "muted").pack(
            side="left", padx=12)
        if not self.records:
            tag(ctk.CTkLabel(body, text="No plugin files found.", anchor="w"), "muted").grid(
                row=1, column=0, columnspan=3, sticky="w")
        for i, rec in enumerate(sorted(self.records, key=lambda r: (r.order, r.title.lower())), start=1):
            var = ctk.BooleanVar(value=rec.enabled)
            ctk.CTkSwitch(body, text="", width=46, variable=var,
                          command=lambda k=rec.key, v=var: self._toggle_plugin(k, v.get())).grid(
                row=i, column=0, sticky="w", pady=3)
            tag(ctk.CTkLabel(body, text=f"{rec.title}    ·    {rec.display_path}", anchor="w"), "text").grid(
                row=i, column=1, sticky="ew", padx=(4, 12))
            role = {"Loaded": "success", "Error": "danger", "Disabled": "muted"}[rec.status]
            tag(ctk.CTkLabel(body, text=rec.status, anchor="e"), role).grid(row=i, column=2, sticky="e")

        # Bundled engines
        body = self._card(page, 2, "Bundled engines",
                          f"Resolved only from {self.paths.bin_dir} — nothing is read from the system PATH.")
        row = 0
        for name, used_by in KNOWN_ENGINES:
            found = find_binary(self.paths.bin_dir, name)
            tag(ctk.CTkLabel(body, text="✓" if found else "✗", width=24),
                "success" if found else "danger").grid(row=row, column=0, sticky="w")
            tag(ctk.CTkLabel(body, text=f"{name}  —  {used_by}", anchor="w"), "text").grid(
                row=row, column=1, sticky="ew")
            tag(ctk.CTkLabel(body, text=str(found) if found else "missing", anchor="e"), "muted").grid(
                row=row, column=2, sticky="e")
            row += 1
        dnd_text = (f"tkdnd {self.dnd_version} loaded" if self.dnd_available
                    else (self.dnd_error or "unavailable"))
        tag(ctk.CTkLabel(body, text="✓" if self.dnd_available else "✗", width=24),
            "success" if self.dnd_available else "danger").grid(row=row, column=0, sticky="w")
        tag(ctk.CTkLabel(body, text=f"Drag & drop  —  {dnd_text}", anchor="w", justify="left",
                         wraplength=600), "text").grid(row=row, column=1, columnspan=2, sticky="ew")
        tag(ctk.CTkButton(body, text="Open bin folder", width=150,
                          command=lambda: self.open_path(self.paths.bin_dir)), "secondary").grid(
            row=row + 1, column=0, columnspan=3, sticky="w", pady=(10, 0))

        # Storage
        body = self._card(page, 3, "Storage",
                          f"Mode: {'portable (data beside the app)' if self.paths.portable else 'installed (data in AppData)'}"
                          f"  ·  {APP_NAME} {APP_VERSION}  ·  Python {sys.version.split()[0]}"
                          f"  ·  Plugin API v{PLUGIN_API_VERSION}")
        locations = [
            ("App folder", self.paths.app_dir),
            ("Plugins", self.paths.plugins_dir),
            ("Bundled engines", self.paths.bin_dir),
            ("User data", self.paths.data_dir),
            ("Logs", self.paths.logs_dir),
        ]
        for r, (label, path) in enumerate(locations):
            tag(ctk.CTkLabel(body, text=label, anchor="w", width=130), "text").grid(row=r, column=0, sticky="w")
            tag(ctk.CTkLabel(body, text=str(path), anchor="w"), "muted").grid(row=r, column=1, sticky="ew")
            tag(ctk.CTkButton(body, text="Open", width=70, height=26,
                              command=lambda p=path: self.open_path(p)), "ghost").grid(row=r, column=2, sticky="e")
        self.register_drop_target(page, self._on_plugin_drop, accept=("files",))
        return page

    def _theme_row(self, parent: Any, row: int, info: ThemeInfo) -> None:
        tag = self.theme.tag
        active = info.name == self.theme.name
        swatches = tag(ctk.CTkFrame(parent, fg_color="transparent"), "transparent")
        swatches.grid(row=row, column=0, sticky="w", pady=5, padx=(0, 14))
        for key in ("bg", "card", "accent", "accent2", "selected", "text"):
            tag(ctk.CTkFrame(swatches, width=20, height=26, corner_radius=0, fg_color=info.palette[key],
                             border_width=1, border_color=info.palette["border"]), "skip").pack(side="left")
        text_box = tag(ctk.CTkFrame(parent, fg_color="transparent"), "transparent")
        text_box.grid(row=row, column=1, sticky="ew")
        tag(ctk.CTkLabel(text_box, text=info.name + (f"   v{info.version}" if info.version else ""), anchor="w",
                         font=self.theme.font(13, "bold")), "text").pack(fill="x")
        byline = " · ".join(x for x in (info.author or "", info.description) if x)
        tag(ctk.CTkLabel(text_box, text=byline, anchor="w", justify="left", wraplength=520), "muted").pack(fill="x")
        actions = tag(ctk.CTkFrame(parent, fg_color="transparent"), "transparent")
        actions.grid(row=row, column=2, sticky="e")
        if active:
            tag(ctk.CTkLabel(actions, text="●  Active", width=90), "success").pack(side="left", padx=4)
        else:
            tag(ctk.CTkButton(actions, text="Apply", width=90,
                              command=lambda n=info.name: self._on_theme_selected(n)), "secondary").pack(
                side="left", padx=4)
        if not info.builtin:
            tag(ctk.CTkButton(actions, text="Remove", width=80,
                              command=lambda n=info.name: self._remove_theme(n)), "ghost").pack(side="left")

    def _refresh_settings_page(self) -> None:
        """Rebuild Settings (after a theme/plugin change) — deferred so a button can trigger it safely."""
        def rebuild() -> None:
            frame = self._pages.pop(SETTINGS_KEY, None)
            if frame is None:
                return
            was_active = self._active_key == SETTINGS_KEY
            try:
                _container_of(frame).destroy()
            except Exception:
                pass
            if was_active:
                self._active_key = None
                self.select(SETTINGS_KEY)
        self.after(10, rebuild)

    def _refresh_theme_menus(self) -> None:
        for menu in self._theme_menus:
            try:
                menu.configure(values=self.theme.names())
            except Exception:
                pass
        self._theme_var.set(self.theme.name)

    def _remove_theme(self, name: str) -> None:
        try:
            self.theme.remove(name)
        except Exception as exc:
            self.set_status(f"Couldn't remove theme: {exc}", "error")
            return
        self._refresh_theme_menus()
        self._refresh_settings_page()
        self.set_status(f"Removed theme {name}", "success")

    def _build_error_page(self, rec: PluginRecord) -> Any:
        tag = self.theme.tag
        page = ctk.CTkFrame(self.viewport, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)
        tag(ctk.CTkLabel(page, text=f"⚠  {rec.title} failed to load", anchor="w",
                         font=self.theme.font(16, "bold")), "danger").grid(row=0, column=0, sticky="ew")
        tag(ctk.CTkLabel(page, text=str(rec.path), anchor="w"), "muted").grid(row=1, column=0, sticky="ew")
        box = ctk.CTkTextbox(page, wrap="none", font=self.theme.font(12, mono=True))
        box.grid(row=2, column=0, sticky="nsew", pady=12)
        box.insert("1.0", rec.error or "Unknown error")
        box.configure(state="disabled")
        bar = tag(ctk.CTkFrame(page, fg_color="transparent"), "transparent")
        bar.grid(row=3, column=0, sticky="w")
        tag(ctk.CTkButton(bar, text="Copy error", width=120,
                          command=lambda: self.copy_to_clipboard(rec.error or "")), "secondary").pack(side="left")
        ctk.CTkButton(bar, text="Reload plugins", width=140, command=self.reload_plugins).pack(side="left", padx=8)
        tag(ctk.CTkButton(bar, text="Open plugins folder", width=160,
                          command=lambda: self.open_path(self.paths.plugins_dir)), "secondary").pack(side="left")
        return page

    def _build_empty_page(self) -> Any:
        tag = self.theme.tag
        page = ctk.CTkFrame(self.viewport, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        body = self._card(page, 0, "Your utility belt is empty",
                          "Each tool is a Python file in the plugins folder that defines `class Plugin` "
                          "with meta_title, meta_description and get_frame(master). Files starting with "
                          "an underscore are ignored.")
        tag(ctk.CTkLabel(body, text=str(self.paths.plugins_dir), anchor="w"), "accent").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ctk.CTkButton(body, text="＋  Install…", width=150, command=self.ask_install).grid(
            row=1, column=0, sticky="w")
        tag(ctk.CTkButton(body, text="Open plugins folder", width=170,
                          command=lambda: self.open_path(self.paths.plugins_dir)), "secondary").grid(
            row=1, column=1, sticky="w", padx=8)
        self.register_drop_target(page, self._on_plugin_drop, accept=("files",))
        return page

    # ── services used by PluginContext ────────────────────────────────────
    def call_in_ui(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if threading.current_thread() is threading.main_thread():
            fn(*args, **kwargs)
        else:
            self._ui_queue.put((fn, args, kwargs))

    def _pump_ui_queue(self) -> None:
        for _ in range(300):
            try:
                fn, args, kwargs = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args, **kwargs)
            except Exception:
                self.log.exception("UI callback %r failed", getattr(fn, "__qualname__", fn))
        if not self._closing:
            self.after(20, self._pump_ui_queue)

    def run_in_thread(self, fn: Callable[..., Any], *args: Any,
                      on_done: Callable[[Any], None] | None = None,
                      on_error: Callable[[BaseException], None] | None = None,
                      name: str = "worker", **kwargs: Any) -> threading.Thread:
        def runner() -> None:
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                self.log.exception("Background task %s failed", name)
                if on_error is not None:
                    self._ui_queue.put((on_error, (exc,), {}))
                else:
                    self._ui_queue.put((self.set_status, (f"Task failed: {exc}", "error"), {}))
                return
            if on_done is not None:
                self._ui_queue.put((on_done, (result,), {}))

        thread = threading.Thread(target=runner, name=name, daemon=True)
        thread.start()
        return thread

    def copy_to_clipboard(self, text: str) -> None:
        def _copy() -> None:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
        self.call_in_ui(_copy)

    def open_path(self, path: Path | str) -> None:
        try:
            target = Path(path)
            if not target.exists() and not target.suffix:
                target.mkdir(parents=True, exist_ok=True)
            open_in_os(target)
        except Exception as exc:
            self.set_status(f"Could not open {path}: {exc}", "error")

    def set_status(self, text: str, level: str = "info") -> None:
        role = {"info": "muted", "success": "success", "warning": "warning", "error": "danger"}.get(level, "muted")
        text = " ".join(str(text).split())
        text = self.theme.effects["status_prefix"] + text
        self._status_label.configure(text=text[:300] + ("…" if len(text) > 300 else ""))
        self.theme.tag(self._status_label, role)
        self.theme.apply(self._status_label)
        if self._status_after is not None:
            try:
                self.after_cancel(self._status_after)
            except Exception:
                pass
            self._status_after = None
        if level in ("info", "success"):
            self._status_after = self.after(8000, lambda: self.set_status("Ready", "_idle"))
        if level == "error":
            self.log.error("Status: %s", text)

    def register_drop_target(self, widget: Any, callback: Callable[[DropPayload], None],
                             accept: tuple[str, ...] = ("files", "text"), owner: str = "core") -> bool:
        """Registers `widget` and every child window under it (CTk widgets are covered by
        internal canvases, so registering only the outer frame would miss most drops)."""
        if not self.dnd_available:
            return False
        dnd_types = ([DND_FILES] if "files" in accept else []) + ([DND_TEXT] if "text" in accept else [])
        if not dnd_types:
            return False
        target = widget

        def on_drop(event: Any) -> Any:
            self._drop_feedback(target, False)
            action = getattr(event, "action", "copy")
            try:
                payload = parse_drop_data(target, getattr(event, "data", ""))
            except Exception:
                self.log.exception("Could not parse drop data")
                return action
            if not (payload.files or payload.urls or payload.text):
                return action
            try:
                callback(payload)
            except Exception as exc:
                self.log.exception("Drop handler of %s failed", owner)
                self.set_status(f"Drop failed: {exc}", "error")
            return action

        def on_enter(event: Any) -> Any:
            self._drop_feedback(target, True)
            return getattr(event, "action", "copy")

        def on_leave(event: Any) -> Any:
            self._drop_feedback(target, False)
            return getattr(event, "action", "copy")

        bound = 0
        for w in _iter_widget_tree(_container_of(widget)):
            current = getattr(w, "_mt_drop_target", None)
            if current is not None and current is not target and _is_descendant(current, target):
                continue  # a nested drop target owns this child window
            try:
                self._ensure_dnd_methods(w)
                w.drop_target_register(*dnd_types)
                w.dnd_bind("<<Drop>>", on_drop)
                w.dnd_bind("<<DropEnter>>", on_enter)
                w.dnd_bind("<<DropLeave>>", on_leave)
                w._mt_drop_target = target
                bound += 1
            except Exception as exc:
                self.log.debug("DnD register failed on %s: %s", w, exc)
        return bound > 0

    @staticmethod
    def _ensure_dnd_methods(widget: Any) -> None:
        if hasattr(widget, "drop_target_register") or TkinterDnD is None:
            return
        for name, value in vars(TkinterDnD.DnDWrapper).items():
            if name.startswith("__"):
                continue
            setattr(widget, name, types.MethodType(value, widget) if inspect.isfunction(value) else value)

    def _drop_feedback(self, target: Any, active: bool) -> None:
        try:
            if active:
                if getattr(target, "_mt_drop_saved", None) is None:
                    target._mt_drop_saved = (target.cget("border_color"), target.cget("border_width"))
                target.configure(border_color=self.theme.palette["drop_highlight"], border_width=2)
            else:
                saved = getattr(target, "_mt_drop_saved", None)
                if saved is not None:
                    target.configure(border_color=saved[0], border_width=saved[1])
                    target._mt_drop_saved = None
        except Exception:
            pass

    # ── plugin installation ───────────────────────────────────────────────
    def register_file_type(self, extension: str, label: str, handler: Callable[[Path], Any],
                           owner: str = "core") -> None:
        ext = extension.lower() if extension.startswith(".") else "." + extension.lower()
        self._file_types[ext] = (owner, label, handler)

    def ask_install(self, kind: str = "any") -> None:
        from tkinter import filedialog
        types = [("Themes", "*.mttheme")] if kind == "theme" else [
            ("Everything Multi-Tool can install",
             " ".join(["*.py", "*.zip", "*.mttheme"] + [f"*{e}" for e in self._file_types])),
            ("Plugins", "*.py *.zip"), ("Themes", "*.mttheme"),
        ] + [(label, f"*{ext}") for ext, (_o, label, _h) in self._file_types.items()]
        chosen = filedialog.askopenfilenames(parent=self, title="Install into Multi-Tool",
                                             filetypes=types + [("All files", "*.*")])
        if chosen:
            self.install_files([Path(c) for c in chosen])

    def ask_install_plugin(self) -> None:
        self.ask_install()

    def _on_plugin_drop(self, payload: DropPayload) -> None:
        if payload.files:
            self.install_files(payload.files)
        else:
            self.set_status("Drop plugin, theme or character files here.", "warning")

    def install_files(self, files: list[Path]) -> None:
        """One entry point for everything installable: plugins (.py/.zip), themes (.mttheme)
        and any file type a plugin registered (e.g. companion characters)."""
        plugin_files: list[Path] = []
        problems: list[str] = []
        themes: list[str] = []
        handled: list[str] = []
        for src in files:
            suffix = src.suffix.lower()
            try:
                if suffix == ".mttheme":
                    themes.append(self.theme.install(src))
                elif suffix in (".py", ".zip"):
                    plugin_files.append(src)
                elif suffix in self._file_types:
                    _owner, label, handler = self._file_types[suffix]
                    result = handler(src)
                    handled.append(str(result or src.name))
                else:
                    problems.append(f"{src.name} (not a plugin, theme or known file type)")
            except Exception as exc:
                self.log.exception("Install of %s failed", src)
                problems.append(f"{src.name} ({exc})")
        if themes:
            self._refresh_theme_menus()
            self._on_theme_selected(themes[-1])
            self.set_status(f"Installed theme: {', '.join(themes)}", "success")
        if handled:
            self.set_status(f"Installed: {', '.join(handled)}", "success")
        if plugin_files:
            self.install_plugins(plugin_files, problems)
        elif problems:
            self.set_status("Not installed: " + "; ".join(problems), "error" if not (themes or handled) else "warning")

    def install_plugins(self, files: list[Path], problems: list[str] | None = None) -> None:
        """Copy .py plugins (or .zip packages containing one plugin folder) into plugins/, then reload."""
        installed: list[str] = []
        problems = problems if problems is not None else []
        for src in files:
            try:
                suffix = src.suffix.lower()
                if suffix == ".py" and src.is_file():
                    dest = self.paths.plugins_dir / src.name
                    if src.resolve() != dest.resolve():
                        shutil.copy2(src, dest)
                    installed.append(re.sub(r"\W", "_", src.stem).lower())
                elif suffix == ".zip" and zipfile.is_zipfile(src):
                    installed.extend(self._install_zip(src))
                else:
                    problems.append(f"{src.name} (not a .py or .zip plugin)")
            except PermissionError:
                problems.append(f"{src.name} (plugins folder is read-only)")
            except Exception as exc:
                problems.append(f"{src.name} ({exc})")
        if installed:
            disabled = set(self.settings.get("core", "disabled_plugins", []) or [])
            self.settings.set("core", "disabled_plugins", sorted(disabled - set(installed)))
            self.settings.set("core", "last_page", installed[0])
            self.log.info("Installed plugins: %s", installed)
            # reload after this event returns: the page that received the drop gets rebuilt
            self.after(50, self._after_install, installed, problems)
        elif problems:
            self.set_status("Not installed: " + "; ".join(problems), "error")

    def _after_install(self, installed: list[str], problems: list[str]) -> None:
        self.reload_plugins()
        if installed[0] in self._visible_keys():
            self.select(installed[0])
        msg = f"Installed {len(installed)} plugin(s): {', '.join(installed)}"
        if problems:
            msg += "  ·  skipped: " + "; ".join(problems)
        self.set_status(msg, "warning" if problems else "success")

    def _install_zip(self, src: Path) -> list[str]:
        with zipfile.ZipFile(src) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) != 1 or not any(n.count("/") == 1 and n.endswith("/__init__.py") for n in names):
                raise PluginError("zip must contain exactly one plugin folder with an __init__.py")
            top = tops.pop()
            target_root = self.paths.plugins_dir.resolve()
            dest_dir = target_root / top
            for name in names:
                out = (target_root / name).resolve()
                if target_root not in out.parents:
                    raise PluginError("zip contains unsafe paths")
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            zf.extractall(target_root)
        return [re.sub(r"\W", "_", top).lower()]

    # ── settings actions ──────────────────────────────────────────────────
    def _on_theme_selected(self, name: str) -> None:
        self._theme_var.set(name)
        self.theme.set_theme(name)
        self._refresh_settings_page()

    def _toggle_plugin(self, key: str, enabled: bool) -> None:
        disabled = set(self.settings.get("core", "disabled_plugins", []) or [])
        (disabled.discard if enabled else disabled.add)(key)
        self.settings.set("core", "disabled_plugins", sorted(disabled))
        self.set_status("Plugin list changed — reload plugins (Ctrl+Shift+R) to apply.", "warning")

    # ── Tk plumbing ───────────────────────────────────────────────────────
    def report_callback_exception(self, exc: type, val: BaseException, tb: Any) -> None:
        self.log.error("Tk callback exception", exc_info=(exc, val, tb))
        try:
            self.set_status(f"Error: {val}", "error")
        except Exception:
            pass

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            state = self.state()
            self.settings.set("core", "maximized", state == "zoomed")
            if state == "normal":
                self.settings.set("core", "geometry", self.geometry())
        except Exception:
            self.log.exception("Could not save window geometry")
        self._schedule_glitch()  # cancels the pending glitch timer (closing flag is set)
        self._unload_plugins()
        self.log.info("Shutting down")
        self.destroy()


# ════════════════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════════════════

def _set_default_font() -> None:
    """CTk's default family (Roboto) isn't installed on most Windows PCs."""
    if not IS_WINDOWS:
        return
    try:
        ctk.ThemeManager.theme["CTkFont"]["family"] = "Segoe UI"
    except Exception:
        pass


def main() -> int:
    paths = resolve_paths()
    ensure_stdio(paths)
    log = setup_logging(paths)
    log.info("Starting %s %s (frozen=%s, portable=%s) from %s", APP_NAME, APP_VERSION, IS_FROZEN,
             paths.portable, paths.app_dir)

    if not acquire_single_instance():
        log.info("Another instance is already running; focusing it.")
        focus_existing_window()
        return 0

    set_app_user_model_id()
    configure_environment(paths, log)
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    _set_default_font()

    try:
        app = App(paths, log)
    except Exception:
        log.critical("Startup failed", exc_info=True)
        show_fatal_error(f"{APP_NAME} could not start.\n\n{traceback.format_exc(limit=3)}\n\n"
                         f"Full log: {paths.logs_dir / 'multitool.log'}")
        return 1
    app.mainloop()
    log.info("Exited cleanly")
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required for multiprocessing inside a PyInstaller exe
    sys.exit(main())
