# sp2yt — agent notes

Keyless Spotify → YouTube Music transfer. Local-only app, personal use.

## Stack

- Python 3.10+, **stdlib only** — no requirements, no venv, no tests, no lint, no CI. Do not add dependencies (`backend.py` imports stdlib + optional `Crypto`, which is tolerated missing).
- Three files do everything: `backend.py` (server + all transfer logic), `app.py` (PROD WebView2 shell), `index.html` (single-page UI). `watchdog.py` diagnoses failures from the audit log.

## Run / build (two lanes, same repo)

- **DEV (iterate here):** `python3 backend.py`, open `http://127.0.0.1:8787`. Or double-click `sp2yt-start.cmd`. No build step.
- **PROD:** run `build.cmd` from repo root (needs `pyinstaller` + `pywebview`). It backs up the exe, kills running instances, compiles one portable `sp2yt.exe`, smoke-tests `/health`, deletes backup on green / restores on red. Never invoke `pyinstaller` directly — the backup/restore + smoke logic is the build.
- Port is fixed at `8787`, bound to `127.0.0.1` only.

## Architecture quirks (unofficial surfaces)

- Spotify read = scrape of public embed page `__NEXT_DATA__` (`fetch_spotify_tracks`). No login. If Spotify changes HTML, failure surfaces as `parse` — fix the parser, not the retry.
- YTM = InnerTube `WEB_REMIX` + hardcoded `YT_KEY`/`CLIENT` in `backend.py`. `SAPISIDHASH` is derived per-call server-side (`yt_headers`) — it goes stale in minutes, so every call re-derives; don't cache headers.
- `POST /api/search-batch`: max **25 items/batch**, workers clamped **1–10** (default 8). Item failure is a skip (`videoId: None`), never a batch failure — don't "fix" this to raise.
- Playlist write (`yt_create_playlist`): create carries ≤50 ids inline (`CREATE_INLINE_MAX`), then one-at-a-time appends for missing copies, then a read-back **ledger** vs playlist entries. Repeats are significant — N copies in ⇒ N entries out (`edit_playlist` refuses ids already on the list, which is why the two-shape flow exists). Never dedupe `video_ids`.
- Dupe-aware create (`yt_create_checked`): same `spotify_id` + live YTM copy ⇒ returns existing, creates nothing. Record lives in `created.json`.

## Cookie / state gotchas

- `normalize_cookie` accepts a typed `a=b; c=d` header **or** Netscape file content — preserve both paths.
- Vault (`cookie_vault.json`) is DPAPI-encrypted per Windows user; disk watchers (Desktop + Downloads + one optional extra) re-latch by content markers. Logs (`blog`) record lengths/flags only — **never log or print cookie values**; `watchdog.py` upholds this.
- Runtime state lives in `%APPDATA%\sp2yt` (`server.log`, vault, watch path, `created.json`), falls back to repo dir if unwritable. All git-ignored — never commit `*.log`, vault/watch/created json, `sp2yt.exe`, `build/`, `dist/`.
- Diagnose via `python3 watchdog.py` (or `--watch` loop); errors carry `kind` (`auth`/`throttle`/`transient`/`parse`/`user`/`write`/`unknown`) + `hint` via `classify()`.

## Icons

- Live mark is `assets/icon.ico`. Shelf candidates sit beside it — to swap: replace `icon.ico`, rebuild via `build.cmd`, then run `refresh-shortcut.cmd` (rebuilds Desktop shortcut + icon cache).
