<p align="center">
  <img src="assets/icon-src.png" width="120" height="120" alt="sp2yt icon" />
</p>
<h1 align="center">sp2yt</h1>
<p align="center"><strong>Copy a public Spotify playlist into YouTube Music without an API key.</strong></p>

<p align="center">
  <img src="assets/app.png" width="520" alt="The sp2yt transfer deck with YouTube Music connected: the Connect card reads Connected and Your account is ready, above an empty Spotify link field, the privacy selector and an empty track ledger with every outcome count at zero." />
</p>

## Install

Download `sp2yt.exe` from the [latest release](https://github.com/nathwn12/spotify-playlist-to-youtube/releases/latest) and run it. One portable executable — no installer, no Python, no console window.

### Offline or pinned

Run the source instead of the exe: `scripts\sp2yt-start.cmd`, or `python3 backend.py`, then open http://127.0.0.1:8787. Python 3.12+ with the standard library only — nothing to install.

Pin an exact version:

```bat
git clone https://github.com/nathwn12/spotify-playlist-to-youtube
cd spotify-playlist-to-youtube
git checkout v0.5.2
```

## Use it

Two steps: the first runs once, the second runs every transfer.

1. Connect YouTube Music. The card finds `music.youtube.com_cookies.txt` on your Desktop or in Downloads and offers it; choose the file, or paste its header instead. If it finds nothing, choose the file by hand — an export from a cookie extension works the same way. Once it is connected, `Replace` and `Remove` sit on the card: `Remove` clears the saved cookie, and it stays removed until you choose a file again. ([First-run screen](assets/app-setup.png))
2. Paste a public Spotify link, choose the privacy (Private is the default), then press Start transfer (or `Ctrl+Enter`). The counter moves against Spotify's declared track count, and the new playlist appears as a chip in the top bar, linked to its `music.youtube.com/playlist?list=` page.

Each track gets a ledger row carrying its outcome and its reason, and the landed count is read back from YouTube Music rather than assumed. Repeats stay separate: an add asks YouTube Music to keep a second copy, and two source rows are never merged into one. Running the same playlist again resumes instead of adding everything twice.

`More options` holds two controls:

- Watch folder — one extra folder to watch.
- Copy rows — a paste-ready list of the tracks that were not added.

### Keys

- `Ctrl+K` — command palette.
- `Ctrl+Enter` — start a transfer, or re-attach to a detached one.
- `Esc` — detach, which leaves the transfer running on the server; reloading the page re-attaches.
- `Alt+1…6` — outcome filter.
- `/` — focus the link field.
- `j` / `k` — scroll the ledger.
- `Ctrl+Shift+N` — new transfer.

## Configure it

| Setting | Where | Default | Notes |
|---|---|---|---|
| Privacy | Side rail | Private (`PRIVATE`/`UNLISTED`/`PUBLIC`) | Persisted server-side in the app's per-user data folder as `sp2yt/settings.json` |
| YouTube Music cookie | Connect card | Unset | Auto-detected from Desktop or Downloads, or chosen by hand; held DPAPI-encrypted so it survives a restart; `Remove` on the card clears it |
| Watch folder | `More options` | Desktop and Downloads | One extra folder; regenerated exports re-latch by content |
| Port | Fixed | `8787` | Bound to 127.0.0.1 only |

The cookie is never logged — audit lines record lengths and flags only.

## Requirements

- Windows 10 or 11, x64.
- The Microsoft Edge WebView2 runtime, which ships with Edge.
- Public Spotify playlists only — no Spotify login.
- YouTube Music caps a playlist at 5,000 tracks.
- The YouTube Music cookie expires; the connect card's own check reports it and asks for a fresh export.
- Python 3.12+ for the source route, standard library only.
- The exe is not code-signed, so the first launch trips SmartScreen ("Windows protected your PC"): choose More info, then Run anyway.

## Development

Four files do the work — `backend.py` (server and transfer logic), `index.html` (the page), `app.py` (the WebView2 shell), `watchdog.py` (diagnosis from the audit trail). Two documents hold the rules: `AGENTS.md` for the invariants a change must preserve, `docs/PROTOCOL.md` for the protocol, the recovery rules, the local API and the measured costs.

- Dev lane: `scripts\sp2yt-start.cmd` serves the source at http://127.0.0.1:8787.
- Build lane: `scripts\build.cmd` runs the gate, backs up the exe, compiles one portable exe, smoke-tests `/health`, keeps green and restores on red. It needs `pyinstaller` and `pywebview`.
- Gate: `scripts\acceptance.cmd` replays the recorded fixtures plus every offline check, and ends `acceptance: all gates green`.
- Lint: `python3 -m ruff check .`

Layout: the root is the app package and its standard controls — `backend.py`, `app.py`, `watchdog.py`, `index.html`, `pyproject.toml`, `AGENTS.md`, `README.md`, `CHANGELOG.md`, `LICENSE`. Helper scripts live in `scripts\`, documents in `docs\`, checks in `tests\`, images in `assets\`; `sp2yt.exe` and the build outputs at the root are ignored artifacts, not sources. Each script resolves the root from its own location, so it runs from any directory.

## Status

**Unverified** — as-is, use at your own risk.

## Links

- [Issues](https://github.com/nathwn12/spotify-playlist-to-youtube/issues) — report a break with the command you ran and what you saw.
- [Releases](https://github.com/nathwn12/spotify-playlist-to-youtube/releases) — the portable `sp2yt.exe`.
- [Protocol](docs/PROTOCOL.md) — what the app sends, what it refuses, and what it cannot prove.
- [Change invariants](AGENTS.md) — what a code change must preserve.
- [MIT licence](LICENSE)
