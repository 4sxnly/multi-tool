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
| `build/multitool.spec` | PyInstaller: bundles Python + whole stdlib + `BUNDLED_PACKAGES` |
| `build/fetch_engines.ps1` | Downloads + SHA-256-verifies yt-dlp, Deno, ffmpeg into `bin/` at build time |
| `build/installer.iss` | Inno Setup: per-user install, no admin, tool checkboxes |
| `.github/workflows/build.yml` | Runs all of the above on a GitHub Windows machine |

## Adding plugins to an installed copy

Settings → **Add plugin…** (or drop a `.py` / `.zip` onto the Settings page). Plugins can import
the standard library plus anything in `BUNDLED_PACKAGES`.
