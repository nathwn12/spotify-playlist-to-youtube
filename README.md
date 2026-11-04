# sp2yt

Keyless Spotify → YouTube Music playlist transfer. A small local app for personal use: point it at a public Spotify playlist, give it your YouTube cookie, one click, done.

No developer API keys, no Spotify app, no Google Cloud project, no paid transfer service. It rides the same free endpoints the official web players use.

## How it works

- **Spotify side (read):** keyless web-player token (TOTP from the player bundle’s own secret, memory-only, never stored) + api-partner `fetchPlaylist` query paged 100 at a time past the embed’s 100-track cap; the embed stays the metadata fallback (name/cover). If fetched < Spotify’s declared total the run fails before touching YouTube. Public links need no login.
- **YouTube Music side (search):** queries the InnerTube `WEB_REMIX` search endpoint (the same one music.youtube.com uses) with the public web client key. No quota, no billing.
- **YouTube Music side (write):** creates the playlist and adds the matched videos using *your* pasted cookie (`SAPISIDHASH` is derived locally).

## Quick start

- **DEV (localhost server):** double-click `sp2yt-start.cmd` (or the `sp2yt-dev` desktop shortcut). Runs the Python source at `http://127.0.0.1:8787`. Iterate here, no build.
- **PROD (portable app):** run `build.cmd`, then launch `sp2yt.exe` (or copy it to Desktop). Self-contained WebView2 window, no browser tab. Needs the WebView2 runtime (ships with Edge).

Manual dev:

```bat
python3 backend.py
```

then open `http://127.0.0.1:8787`. Requires Python 3.10+. No dependencies — standard library only.

## Usage

1. Export your `music.youtube.com` cookie to a `.txt` file (cookie-export extension or DevTools → Network → copy `Cookie`). Spotify needs no login — public links only.
2. Click the single upload button and select the file — it latches automatically. Typing/pasting works too. The app watches Desktop + Downloads (plus one optional custom folder) for regenerated exports and re-latches by content.
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
| `POST` | `/api/transfer` | `{url, yt_cookie, title, privacy}` → `{run_id}` immediately; worker runs full spotify fetch (100/page to declared total) → search (10s/8 workers, transport retries, ambiguous ties skipped with reason) → create → append → verify, rows stream via `/api/run` |
| `GET` | `/api/run?since=N` | `{run_id, seq, phase, total/matched/sent/landed/refused/skipped/missing, url, created, rows(changed), age}` polled every 700ms; `since=0` returns full rows |
| `GET`/`POST` | `/api/settings` | `{privacy}` persisted server-side in `settings.json`; POST merges, unknown keys ignored, privacy clamped to `PRIVATE\|PUBLIC\|UNLISTED` |

Errors carry a `kind` (`auth`, `throttle`, `transient`, `parse`, `user`, `unknown`) plus a `hint`. Transient/throttled batches auto-retry once at reduced workers; fatal errors stop the run with a diagnosis line.

`python3 watchdog.py` inspects the audit log and names the fix for the latest failure (`--watch` loops every 15 s).

## Privacy

- Everything runs on `127.0.0.1`. The only outbound traffic is the app → Spotify / YouTube Music directly.
- The YouTube cookie is latched in a DPAPI-encrypted local vault (`%APPDATA%\sp2yt`) so it survives restarts. Logs record flags and counts only, never values. `forget` clears vault + watch folder.
- Runtime state (`server.log`, vault, watch path, playlist record, `settings.json`) is machine-local and git-ignored. No secrets are committed.
- Settings persist server-side only: `privacy` in `%APPDATA%\sp2yt\settings.json` via `/api/settings` (survives WebView2 profile resets; shared by DEV and PROD). Deliberately never persisted: Spotify URL, playlist title, cookie (cookie lives DPAPI-encrypted in the vault).

## Limits & notes

- Public Spotify playlists only.
- YouTube Music caps playlists at 5,000 tracks.
- Auto-match is typically ~90%; misses are listed in the log for manual add.
- Embed scraping and InnerTube are unofficial surfaces — if either changes format, transfers fail with a `parse` diagnosis until the parser is updated.

## Layout

```text
sp2yt/
├── app.py            # WebView2 desktop shell (PROD entry)
├── backend.py        # stdlib-only server (ThreadingHTTPServer)
├── index.html        # single-page UI
├── watchdog.py       # log monitor / failure diagnosis
├── sp2yt-start.cmd   # DEV launcher (localhost server)
├── build.cmd         # PROD build: single portable sp2yt.exe + smoke test
└── server.log        # local audit log (git-ignored, in %APPDATA%\sp2yt)
```

## Icons

Live mark: `assets/icon.ico` (built from `assets/icon-src.png`). Candidate shelf lives beside it — swap the winner in, rebuild the `.exe`, then run `refresh-shortcut.cmd`: it rebuilds the Desktop shortcut and refreshes the Windows icon cache automatically. No manual cache clearing.

## Build

Two lanes, same repo:

- **DEV** — double-click `sp2yt-start.cmd` (or the `sp2yt-dev` desktop shortcut). Runs the Python source directly at `http://127.0.0.1:8787`, no build. Iterate here.
- **PROD** — run `build.cmd`. Backs up the current exe, kills running instances, compiles a single portable `sp2yt.exe` (WebView2 window, no browser tab), then smoke-tests it (health). Green? The backup is deleted and prod is live. Red? The previous exe is restored automatically.

Every build must go green before it ships — no green, no prod.

## License

MIT — see `LICENSE`.
