# Multi-Tool — Desktop Utility Belt

A plugin-based Windows toolbox. The installer contains everything: its own Python runtime,
all libraries, and the engines (yt-dlp, ffmpeg, Deno). Users install nothing else.

## Build the installer (no software needed on your PC)

1. Push this folder to a GitHub repository.
2. Open the repo's **Actions** tab → **Build Windows installer** → **Run workflow**.
3. When it finishes (~10 min), download **MultiTool-Setup-x.y.z** from the run's *Artifacts*.

Pushing a tag such as `v0.2.0` also publishes a GitHub Release with the installer and the
standalone plugin files attached.

## Layout

| Path | What |
|---|---|
| `main.py` | Core shell: window, theme engine, plugin loader, drag-and-drop |
| `plugins/` | One file (or folder) per tool. Shipped as optional installer components |
| `extras/` | Optional theme and character packs offered by the installer |
| `build/multitool.spec` | PyInstaller: bundles Python + whole stdlib + `BUNDLED_PACKAGES` |
| `build/fetch_engines.ps1` | Downloads + SHA-256-verifies yt-dlp, Deno, ffmpeg into `bin/` at build time |
| `build/installer.iss` | Inno Setup: per-user install, no admin, tool checkboxes |
| `.github/workflows/build.yml` | Runs all of the above on a GitHub Windows machine |

## Offline AI (Desktop Companion)

The installer bundles llama.cpp's `llama-server` (Vulkan build for graphics cards + CPU build as fallback)
in `bin/llama-vulkan` and `bin/llama-cpu`. The model itself (Qwen3.5 0.8B / 2B / 4B, Apache 2.0) is downloaded
once from inside the app — Companion ▸ AI & Voice ▸ Download — into `%LOCALAPPDATA%\MultiTool\models`,
resumable and SHA-256 checked. After that everything runs offline on 127.0.0.1; no account or key.

## Installing add-ons into an installed copy

Settings → **＋ Install…**, or drop the file anywhere on the Settings page:

| File | What it is |
|---|---|
| `.py` / `.zip` | Plugin (a tool in the sidebar). Can import the stdlib + anything in `BUNDLED_PACKAGES` |
| `.mttheme` | Theme pack: JSON, or a zip with `theme.json` + banner image. Format documented at the top of `main.py` |
| `.mtchar` | Desktop Companion character: zip with `character.json` (+ PNG layers). Companion ▸ Export template… writes a documented starter |

`extras/` holds the optional packs the installer offers (Overdrive 2099 theme, Vex character).
