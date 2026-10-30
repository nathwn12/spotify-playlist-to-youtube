# sp2yt

Keyless Spotify → YouTube Music playlist transfer. A small local web app for personal use: point it at a public Spotify playlist, give it your own cookies, one click, done.

No developer API keys, no Spotify app, no Google Cloud project, no paid transfer service. It rides the same free endpoints the official web players use.

## How it works

- **Spotify side (read):** fetches the public playlist embed page and parses the embedded track list (`title / artist / duration`). No auth needed for public playlists; you can optionally supply your Spotify cookies for login-walled cases.
- **YouTube Music side (search):** queries the InnerTube `WEB_REMIX` search endpoint (the same one music.youtube.com uses) with the public web client key. No quota, no billing.
- **YouTube Music side (write):** creates the playlist and adds the matched videos using *your* pasted cookies (`SAPISIDHASH` is derived locally). Nothing is stored anywhere — cookies live in your browser tab and go straight to `music.youtube.com`.

## Quick start (Windows)

Double-click `sp2yt-start.cmd` (or the `sp2yt` desktop shortcut). It starts the server if needed and opens the page at `http://127.0.0.1:8787`.

Manual:

```bat
python3 backend.py
```

then open `http://127.0.0.1:8787`. Requires Python 3.10+. No dependencies — standard library only.

## Usage

1. Export your `music.youtube.com` cookie to a `.txt` file (cookie-export extension or DevTools → Network → copy `Cookie`). Spotify needs no login — public links only.
2. Click the single upload button, select both files — they're auto-sorted by filename and content markers. Typing/pasting works too.
3. Paste the Spotify playlist URL, set title + privacy, click **1-CLICK TRANSFER**.
4. Watch the spinner + progress bar. Ends with the new `music.youtube.com/playlist?list=…` link.

## API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Liveness |
| `GET` | `/api/status` | Recent server events (audit trail) |
| `POST` | `/api/spotify` | `{url, spotify_cookie}` → track list |
| `POST` | `/api/search` | `{query}` → top YTM match |
| `POST` | `/api/search-batch` | `{queries[], workers}` → matches in order (max 25/batch, 8 threads) |
| `POST` | `/api/create-playlist` | `{yt_cookie, title, description, videoIds[], privacy}` → playlist URL |

Errors carry a `kind` (`auth`, `throttle`, `transient`, `parse`, `user`, `unknown`) plus a `hint`. Transient/throttled batches auto-retry once at reduced workers; fatal errors stop the run with a diagnosis line.

`python3 watchdog.py` inspects the audit log and names the fix for the latest failure (`--watch` loops every 15 s).

## Privacy

- Everything runs on `127.0.0.1`. The only outbound traffic is your browser → Spotify / YouTube Music directly.
- Cookies are never written to disk by the server and never logged (logs record pair counts and lengths only).
- `server.log` is local-only and git-ignored.

## Limits & notes

- Public Spotify playlists only.
- YouTube Music caps playlists at 5,000 tracks.
- Auto-match is typically ~90%; misses are listed in the log for manual add.
- Embed scraping and InnerTube are unofficial surfaces — if either changes format, transfers fail with a `parse` diagnosis until the parser is updated.

## Layout

```text
sp2yt/
├── backend.py        # stdlib-only server (ThreadingHTTPServer)
├── index.html        # single-page UI
├── watchdog.py       # log monitor / failure diagnosis
├── sp2yt-start.cmd   # Windows double-click launcher
└── server.log        # local audit log (git-ignored)
```

## Icons

Live mark: `assets/icon.ico` (built from `assets/icon-src.png`). Candidate shelf lives beside it — swap the winner in, rebuild the `.exe`, then run `refresh-shortcut.cmd`: it rebuilds the Desktop shortcut and refreshes the Windows icon cache automatically. No manual cache clearing.

## Build

Two lanes, same repo:

- **DEV** — double-click `sp2yt-start.cmd` (or the `sp2yt-dev` desktop shortcut). Runs the Python source directly, no build. Iterate here.
- **PROD** — run `build.cmd`. Backs up the current exe, kills running instances, compiles a single portable `sp2yt.exe`, then smoke-tests it (health + UI markers). Green? The backup is deleted and prod is live. Red? The previous exe is restored automatically.

Every build must go green before it ships — no green, no prod.

## License

MIT — see `LICENSE`.

MIT — see `LICENSE`.

MIT — see `LICENSE`.
