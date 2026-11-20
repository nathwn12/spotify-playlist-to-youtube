<p align="center">
  <img src="assets/icon-src.png" width="120" height="120" alt="sp2yt icon" />
</p>
<h1 align="center">sp2yt</h1>
<p align="center"><strong>Copy a public Spotify playlist into YouTube Music without an API key.</strong></p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-windows%20x64-blue" alt="platform">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT">
</p>

## Why

sp2yt moves a public Spotify playlist into YouTube Music with no API key and no sign-in. It ships as one portable executable built from standard-library Python. Each track gets a ledger row with its outcome and reason, and the landed count is read back from YouTube Music rather than assumed. The YouTube Music cookie is held DPAPI-encrypted and is never logged. Running the same playlist again resumes instead of adding everything twice.

## Install

Download `sp2yt.exe` from the [latest release](https://github.com/nathwn12/spotify-playlist-to-youtube/releases/latest) and run it. One portable executable: no installer, no Python and no console window.

To run the source instead, use `scripts\sp2yt-start.cmd` or `python3 backend.py`, then open http://127.0.0.1:8787. Python 3.12+ with the standard library only.

Pin an exact version:

```bat
git clone https://github.com/nathwn12/spotify-playlist-to-youtube
cd spotify-playlist-to-youtube
git checkout v0.5.2
```

## Use

Two steps: the first runs once, the second runs every transfer.

1. Connect YouTube Music. The card finds `music.youtube.com_cookies.txt` on your Desktop or in Downloads and offers it; choose the file, or paste its header instead. If it finds nothing, choose the file by hand. Once connected, `Replace` and `Remove` sit on the card; `Remove` clears the saved cookie until you choose a file again. ([First-run screen](assets/app-setup.png))
2. Paste a public Spotify link, choose the privacy (Private is the default), then press Start transfer (or `Ctrl+Enter`). The counter moves against Spotify's declared track count, and the new playlist appears as a chip in the top bar, linked to its `music.youtube.com/playlist?list=` page.

`More options` holds two controls: a watch folder, and a paste-ready list of the tracks that were not added. Repeats stay separate: an add asks YouTube Music to keep a second copy, and two source rows are never merged into one.

Keys:

- `Ctrl+K` — command palette.
- `Ctrl+Enter` — start a transfer, or re-attach to a detached one.
- `Esc` — detach; the transfer keeps running on the server, and reloading the page re-attaches.
- `Alt+1…6` — outcome filter.
- `/` — focus the link field.
- `j` / `k` — scroll the ledger.
- `Ctrl+Shift+N` — new transfer.

## Configure

| Setting | Where | Default | Notes |
| --- | --- | --- | --- |
| Privacy | Side rail | Private (`PRIVATE`/`UNLISTED`/`PUBLIC`) | Persisted server-side as `sp2yt/settings.json` in the app's per-user data folder |
| YouTube Music cookie | Connect card | Unset | Auto-detected from Desktop or Downloads, or chosen by hand; DPAPI-encrypted so it survives a restart; `Remove` clears it |
| Watch folder | `More options` | Desktop and Downloads | One extra folder; regenerated exports re-latch by content |
| Port | Fixed | `8787` | Bound to 127.0.0.1 only |

The cookie is never logged; audit lines record lengths and flags only.

## Requirements

- Windows 10 or 11, x64, and the Microsoft Edge WebView2 runtime, which ships with Edge.
- Public Spotify playlists only; no Spotify login. YouTube Music caps a playlist at 5,000 tracks.
- The YouTube Music cookie expires; the connect card checks it and asks for a fresh export.
- Python 3.12+ for the source route, standard library only.
- The exe is not code-signed, so the first launch trips SmartScreen. Choose More info, then Run anyway.

## Links

- [Issues](https://github.com/nathwn12/spotify-playlist-to-youtube/issues) — report a break with the command you ran and what you saw.
- [Releases](https://github.com/nathwn12/spotify-playlist-to-youtube/releases) — the portable `sp2yt.exe`.
- [Protocol](docs/PROTOCOL.md) — what the app sends, what it refuses and what it cannot prove.
- [Change invariants](AGENTS.md) — what a code change must preserve.
- [MIT](LICENSE)

<p align="center">Built by nathwn12</p>
