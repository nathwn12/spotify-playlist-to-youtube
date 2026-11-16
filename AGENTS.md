# sp2yt — agent notes

## What it is

Keyless Spotify to YouTube Music playlist transfer for Windows. The server binds to 127.0.0.1:8787.

## Stack

- Python 3.12+, standard library only — no requirements file, no venv. Do not add dependencies (`backend.py` imports only the standard library).
- Three files do everything: `backend.py` (server + all transfer logic), `app.py` (PROD WebView2 shell), `index.html` (single-page UI). `watchdog.py` diagnoses failures from the audit log.

## Run / build

- **DEV (iterate here):** `python3 backend.py` (or `sp2yt-start.cmd`), open `http://127.0.0.1:8787`. No build step.
- **PROD:** run `build.cmd` from the repo root (needs `pyinstaller` + `pywebview`). It backs up the exe, kills running instances, compiles one portable `sp2yt.exe`, smoke-tests `/health`, deletes the backup on green / restores it on red. Never invoke `pyinstaller` directly — the backup/restore + smoke logic is the build.
- The gate is `acceptance.cmd`, which must print `36/36 fixtures` and then pass the language tests, the counts/API contract, the edition tests, the order/convergence tests, the cover tests, the page-script parse check, the UI checks, the cookie tests, the connect-card checks and the release-asset checks — every one green — before anything ships. The heavier offline suites are explicit: `tests/test_slices.py` (a 2,500-row transfer, ~20 s), `tests/test_repair.py` and `tests/test_scale_report.py`.
- **Lint / format:** `python3 -m ruff check .` and `python3 -m ruff format .`; the config is `pyproject.toml` (`target-version = "py312"`, rules `E,F,W,I,UP,B`). Outstanding findings are real debt, not a gate yet: fix them rule by rule, never with a mass reflow or `--unsafe-fixes`.
- Port is fixed at `8787`, bound to `127.0.0.1` only.

## Architecture quirks (unofficial surfaces)

- Spotify read = scrape of the public embed page `__NEXT_DATA__` for metadata, plus the keyless token path for titles past the 100-track cap (`fetch_spotify_tracks`). No login. If Spotify changes format, failure surfaces as `parse` — fix the parser, not the retry.
- YTM = InnerTube `WEB_REMIX` + the public web client key in `backend.py`. `SAPISIDHASH` is derived per call server-side (`yt_headers`) — it goes stale in minutes, so every call re-derives; don't cache headers.
- `POST /api/search-batch`: max **25 items/batch**, workers clamped **1–10** (default 8). Item failure is a skip (`videoId: None`), never a batch failure — don't "fix" this to raise.
- Playlist write (`transfer_create_append`): create carries ≤50 ids inline (`CREATE_INLINE_MAX`), then the outstanding copies go out in slices of 100 (`TRANSFER_SLICE`). A slice is followed by another only when its own read proves it: the read floor rises by the copies the server accepted, and every accepted copy of the slice must be present (an outside add padding the total is not evidence). A read that stops before the end, or that hides a slice, sends nothing further. The final **ledger** is reconciled against the last completed read. Repeats are significant - N copies in ⇒ N entries out, and every add carries `"dedupeOption": "DEDUPE_OPTION_SKIP"` because a transfer is 1:1 with the source, always (there is no toggle; never dedupe `video_ids`). `duplicate` survives only for a server that refuses anyway, and only from its own duplicate toast.
- Convergence (`_converge`) counts copies, not ids: a repeated videoId the playlist holds once is one copy short and re-sending it is the repair, in plan order. A copy the server refused (duplicate toast or plain refusal) is not re-sent in the same run - that verdict is final for it, and it ends as a named residual rather than a `complete` record. `edit_playlist` can only append, so a repaired copy can land after a later one; `_order_preserved` reports that and `order_ok`/the done line say so, because a playlist holding every copy in a different order is not a faithful transfer.
- Live progress is the playlist's own word: `counts.added` is a copy a read proved present, `counts.pending` is one that was sent and not proved yet, and `/api/run`'s `attempted` counts both plus the terminal outcomes. A queued row is in none of them. Mid-run, `added` only moves on a read that reached the end of the playlist; a partial read's presence is accounted for once, by the final ledger, in the open. Recount from rows only (`_counts_from_rows` / `_run_counts_locked`).
- Dupe-aware create (`yt_create_checked`): same `spotify_id` + live YTM copy ⇒ returns the existing playlist, creates nothing. Record lives in `created.json`.
- Script-aware matching (`_fold_text` → kana romanization → identified match): Japanese-script and romaji spellings of one title align, and a script-crossing row with no identified candidate is **skipped** with its reason — the rank-1 fallback is refused, never guessed, because rank 1 is the search engine's opinion. A `score` is a similarity index, never a probability. Latin-only rows still run the legacy scorer alone. An album-bearing query variant is tried **last**, only when the source row carries `album`, and an edition word in that album (deluxe/single/EP/remaster) breaks a near-tie among candidates already within 0.05 of the leader: a widening, never a gate.
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
