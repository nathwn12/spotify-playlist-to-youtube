# sp2yt — agent notes

## What it is

Keyless Spotify to YouTube Music playlist transfer for Windows. The server binds to 127.0.0.1:8787.

## Stack

- Python 3.10+, standard library only — no requirements file, no venv. Do not add dependencies (`backend.py` imports stdlib plus an optional `Crypto`, which is tolerated missing).
- Three files do everything: `backend.py` (server + all transfer logic), `app.py` (PROD WebView2 shell), `index.html` (single-page UI). `watchdog.py` diagnoses failures from the audit log.

## Run / build

- **DEV (iterate here):** `python3 backend.py` (or `sp2yt-start.cmd`), open `http://127.0.0.1:8787`. No build step.
- **PROD:** run `build.cmd` from the repo root (needs `pyinstaller` + `pywebview`). It backs up the exe, kills running instances, compiles one portable `sp2yt.exe`, smoke-tests `/health`, deletes the backup on green / restores it on red. Never invoke `pyinstaller` directly — the backup/restore + smoke logic is the build.
- The gate is `acceptance.cmd`, which must print `36/36 fixtures` and then pass the language tests, the cover tests, the page-script parse check, the cookie tests, the connect-card checks and the release-asset checks — every one green — before anything ships.
- Port is fixed at `8787`, bound to `127.0.0.1` only.

## Architecture quirks (unofficial surfaces)

- Spotify read = scrape of the public embed page `__NEXT_DATA__` for metadata, plus the keyless token path for titles past the 100-track cap (`fetch_spotify_tracks`). No login. If Spotify changes format, failure surfaces as `parse` — fix the parser, not the retry.
- YTM = InnerTube `WEB_REMIX` + the public web client key in `backend.py`. `SAPISIDHASH` is derived per call server-side (`yt_headers`) — it goes stale in minutes, so every call re-derives; don't cache headers.
- `POST /api/search-batch`: max **25 items/batch**, workers clamped **1–10** (default 8). Item failure is a skip (`videoId: None`), never a batch failure — don't "fix" this to raise.
- Playlist write (`transfer_create_append`): create carries ≤50 ids inline (`CREATE_INLINE_MAX`), then one-at-a-time appends for missing copies, then a read-back **ledger** reconciled against the playlist entries. Repeats are significant — N copies in ⇒ N entries out (`edit_playlist` refuses ids already on the list, which is why the two-shape flow exists). Never dedupe `video_ids`.
- Dupe-aware create (`yt_create_checked`): same `spotify_id` + live YTM copy ⇒ returns the existing playlist, creates nothing. Record lives in `created.json`.
- Script-aware matching (`_fold_text` → kana romanization → rank-1 guess): Japanese-script and romaji spellings of one title align, and a script-crossing row with no confident match takes YouTube Music's rank-1 answer as a guess — always labelled (`best_guess`, ledger status, done-line count), never silent. Latin-only rows still run the legacy scorer alone.
- Cover inheritance (`yt_set_playlist_cover`): a **newly created** playlist takes the Spotify cover through the 3-call resumable upload (handshake → upload → attach). Best-effort by construction — no cover, non-image bytes, a refused handshake or a failed attach is a silent skip, one `cover` audit line, and never a failed run. Never runs on a resume/top-up, so it cannot overwrite a hand-set cover.

## Cookie / state gotchas

- `normalize_cookie` accepts a typed `a=b; c=d` header **or** Netscape file content — preserve both paths.
- The vault is DPAPI-encrypted per Windows user; disk watchers (Desktop + Downloads plus one optional extra folder) re-latch by content markers. Logs record lengths/flags only — **never log or print cookie values**.
- Runtime state lives in the per-user application data folder (`sp2yt`), falling back to the repo dir if unwritable. All git-ignored — never commit `*.log`, vault/watch/created json, `sp2yt.exe`, `build/`, `dist/`, or `settings.json`.
- Diagnose via `python3 watchdog.py` (or `--watch` loop); errors carry `kind` (`auth`/`throttle`/`transient`/`parse`/`source`/`user`/`write`/`unknown`) + `hint` via `classify()`.

## Fixtures

- `tests/fixtures/playlist.json` holds 36 recorded rows — each with its real source row and the real YouTube Music search responses, trimmed to only the keys the parser reads. The six Japanese rows are hand-authored, not recorded: each carries its own `responses` map so rows sharing a source title can still replay different cards. The gate replays them offline through the real matcher in `backend.py`.
- Re-record when the matcher changes: `python3 tests\record_fixtures.py <public playlist url>` (at most 40 real searches, 300 ms apart; everything after that runs offline). `merge_rows` carries the hand-authored rows over, so only recorded rows are replaced. Review the printed `ok`/`skip` lines before committing the new fixture.

## Icons

- Live mark is `assets/icon.ico` built from `assets/icon-src.png`. To change it: replace both, rebuild via `build.cmd`, then run `refresh-shortcut.cmd` (rebuilds the Desktop shortcut + refreshes the Windows icon cache).
