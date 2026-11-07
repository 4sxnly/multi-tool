# Multi-Tool — Desktop Utility Belt

A plugin-based Windows toolbox. The installer contains everything: its own Python runtime,
all libraries, and the engines (yt-dlp, ffmpeg, Deno, llama.cpp, the voice engine). Users install nothing else.

## How updates work (no new installer for most changes)

| You change… | What happens |
|---|---|
| `main.py`, anything in `plugins/`, `extras/themes/*.mttheme`, `extras/characters/*.mtchar` | Just commit it. The installed app finds it in **Settings ▸ Updates** (it also checks every 6 hours) and downloads only the changed files. A `main.py` change restarts the app. |
| `requirements.txt`, `launcher.py`, `build/**`, `assets/**`, `.github/workflows/**` | GitHub builds a new installer automatically and publishes it as a Release. The app's **Settings ▸ Updates** offers **Download & install**, which runs it silently and reopens Multi-Tool. |

The exe runs `launcher.py`, which loads `core/main.py` next to the exe (the updatable copy). If that file is
ever broken, it falls back to the copy frozen into the exe, so a bad update can't stop the app from starting.

## Build the installer by hand

Repo ▸ **Actions** ▸ **Build Windows installer** ▸ **Run workflow**. When it finishes, the installer is on the
repo's **Releases** page (and in the run's *Artifacts*).

## Layout

| Path | What |
|---|---|
| `main.py` | Core shell: window, theme engine (effects + sounds), plugin loader, pop-up notifications, updater |
| `launcher.py` | Tiny frozen entry point that loads `core/main.py` |
| `plugins/` | One file (or folder) per tool |
| `extras/` | Theme packs and character packs |
| `build/multitool.spec` | PyInstaller: bundles Python + whole stdlib + `BUNDLED_PACKAGES` |
| `build/fetch_engines.ps1` | Downloads + verifies yt-dlp, Deno, ffmpeg, llama.cpp into `bin/` at build time |
| `build/installer.iss` | Inno Setup: per-user install, no admin, tool checkboxes |
| `.github/workflows/build.yml` | Builds, smoke-tests and publishes the installer |

## Plugins

| Plugin | What it does |
|---|---|
| Media Downloader | YouTube (and 1,000+ sites) to MP4/MP3, queue, playlists, pop-up when done |
| Desktop Companion (Juno) | Desktop character with an offline AI. Off until you switch her on. Neural voices + speed, hands-free voice chat with "mute me", up to 4 images per message (paste, drop, screenshot) |
| Discord Heads-Up | Themed pop-ups for Discord messages (avatar, name, server, text, your notes). Reads Windows' notification list; no Discord login |
| Drop Zone (Demo) | Reference plugin for writing your own |

## Offline AI and voice (Desktop Companion)

Bundled engines: llama.cpp `llama-server` (Vulkan + CPU) and sherpa-onnx. Models are downloaded once from inside the
app into `%LOCALAPPDATA%\MultiTool\models` and then run offline:

* Brain — Qwen3.5 0.8B / 2B / 4B (Apache 2.0), Companion ▸ AI & Voice ▸ Download
* Neural voices — Kokoro (about 130 MB), 28 English voices
* Voice chat — Moonshine speech recognition + Silero voice detection (about 110 MB)

## Themes

Built in: Default Dark, Cyberpunk Neon. Packs: Overdrive 2099, Phosphor CRT, Amber Lab 86.
A `.mttheme` is JSON or a zip with `theme.json` (+ banner PNG, + `sounds/*.wav`). `effects` switch on shell features:
`card_echo` (stacked 3D panel), `corner_brackets`, `title_shadow`, `title_decrypt`, `glitch_titles`, `kicker`,
`hex_ticker`, `scan_sweep`, `boot_sequence` + `boot_lines`, `status_clock`, `status_telemetry`, `blink_cursor`,
`body_font`, `synth_sounds` (`neon`, `hazard`, `phosphor`, `amber`, `soft` — built-in sounds, no files needed).
Full list: `EFFECT_DEFAULTS` in `main.py`.

## Installing add-ons by hand

Settings → **＋ Install…**, or drop the file anywhere on the Settings page:

| File | What it is |
|---|---|
| `.py` / `.zip` | Plugin |
| `.mttheme` | Theme pack |
| `.mtchar` | Companion character (Companion ▸ Export template… writes a documented starter) |
