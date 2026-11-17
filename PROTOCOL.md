# sp2yt — the protocol in detail

`README.md` is the entry point for using the app. This file is what the app actually
sends, what it refuses, and what it cannot prove. `AGENTS.md` holds the invariants any
change must preserve.

Both surfaces are unofficial. A shape change upstream stops the run with a diagnosis
instead of writing a wrong playlist.

## 1. Spotify read — keyless

- **Metadata:** the public embed page `open.spotify.com/embed/playlist/<id>` is scraped for
  `__NEXT_DATA__` — playlist name and cover only. Best effort: a miss is logged and the run
  continues to the full fetch.
- **Full track list:** `api-partner.spotify.com/pathfinder/v1/query`, operation
  `fetchPlaylist`, authenticated with the web player's **anonymous token**. The TOTP that
  mints it is derived from the player bundle's own secret and kept in memory — never
  logged, never stored. `api.spotify.com` answers an anonymous token with HTTP 429
  (verified live), so it is not the full-fetch route.
- **Paging:** 100 tracks per request, sequential, about 0.2 s apart. Episodes count as rows
  the same way tracks do.
- **Refusals before YouTube:** the first page must carry a declared `totalCount`; a missing
  total is refused rather than treated as zero, and a read that ends below the declared
  total raises before YouTube Music is touched at all.

No Spotify login, no client id, no secret.

## 2. YouTube Music search — InnerTube `WEB_REMIX`

- InnerTube `WEB_REMIX` with the public web client key. No quota, no billing.
- `SAPISIDHASH` is derived per call from the cookie and the current timestamp. It goes
  stale in minutes, so every call re-derives it; headers are never cached.
- `POST /api/search-batch` takes at most **25** items and clamps `workers` to **1–10**
  (default 8). One item failing is a skip for that row, never a failed batch.

## 3. Matching

- A candidate is judged on title, artist and duration together. `score` is a similarity
  index, never a probability.
- Scripts cross: a Japanese-script title and its romaji spelling of the same song align
  either way. A row whose spellings cross scripts and still has no identified match is
  **skipped with its reason** — rank 1 is the search engine's opinion and is not used as a
  fallback.
- A candidate carried by an exact title alone, with no artist agreement and no duration
  reading, is refused as `title only` and the next query variant gets its turn. Artist
  agreement alone, or a duration that is not plainly wrong, still matches.
- An album-bearing query variant is tried **last**, and only when the source row carries an
  album. An edition word in that album (deluxe, single, EP, remaster) breaks a near-tie
  among candidates already within 0.05 of the leader — a widening, never a gate.
- Soft second search: a row whose search never answered (timeout, connection, 429, 5xx) is
  asked once more with its own first query, under the same evidence bar, capped at **8**
  rows per run. A row that answered about the song is never re-searched.

## 4. Write

`transfer_create_append`, in order:

1. **The intent is written before the request.** `created.json` records `create_pending`
   plus the plan and title, so a lost reply leaves evidence instead of a maybe-playlist.
2. **Create** carries at most **50** video ids inline (`CREATE_INLINE_MAX`).
3. **Remainder in slices of 100** (`TRANSFER_SLICE`). A slice is sent, then proved by a
   complete read-back of the playlist before the next one goes out. The read floor rises by
   the copies the server accepted, and every accepted copy of the slice must be present.
4. **Presence is counted by id, never by plan slot.** Copies of one `videoId` are
   indistinguishable in a read, so a copy that landed can be attributed to an earlier
   occurrence of the same id.
5. **A read that stops before the end, or that hides a slice, sends nothing further.**
6. **The final ledger is reconciled against the last completed read.** `complete` is
   written only when that read reached the end of the playlist *and* every intended copy
   was read back.

A duplicate-create check runs first: the same Spotify id with a live YouTube Music copy
returns the existing playlist and creates nothing.

## 5. Repeats are significant

N copies in, N entries out. There is no setting that merges them, and no dedupe of
`video_ids` — a transfer is one-to-one with the source, always. Every add carries
`"dedupeOption": "DEDUPE_OPTION_SKIP"` for that reason. `duplicate` exists only for a
server that refuses anyway, and only from its own duplicate toast.

## 6. When a write is uncertain

This is the part that protects your playlist. A failure whose application is unproven —
a timeout, a closed connection, a 5xx, **and a 429** — is `STATUS_UNSURE`, never a failure
that "did not happen":

- An unknown copy is **never re-sent** by any automatic path: not inside the add, not by
  the repair pass, not after a restart. The run ends `gaveup` naming it, and the doubt is
  written to disk.
- Uncertainty is keyed by **videoId**, so it taints every occurrence of that song in the
  run. Copies of one id cannot be told apart in a read, so freezing one freezes them all.
- A doubt clears only on evidence that can answer it: a **complete** read holding at least
  as many copies of the id as the doubt was recorded against. A doubt with no recorded
  multiplicity is never cleared.
- A copy the run never sent is named **not sent** — unverified, never counted as missing.
- A 400/401/403/404 is a verdict on the call, not an unknown: the run stops there and the
  error names the status, with a hint naming an expired session or a permission answer,
  because nothing here can tell the two apart.
- **A record that cannot be read is never overwritten.** Nothing is deleted or rewritten;
  take a safe copy first and check the playlist that record may still name.

The practical consequence: after an interrupted or unattributed write, **check the playlist
by hand**. Do not Reset, do not delete the playlist, do not delete `created.json` — that
record is the only copy of the log, the plan and the playlist link.

## 7. Order

`edit_playlist` can only append. A copy repaired after a later one landed therefore lands
at the end: the playlist holds every intended copy but not in the source's order. The app
says so out loud (`order_ok` false, named on the done line).

Convergence counts copies, not ids: a repeated `videoId` the playlist holds once is one
copy short, and re-sending it is the repair, in plan order. The repair goes out through the
same sender as the first pass, each batch proved by its own read-back.

## 8. Cover

A **newly created** playlist takes the Spotify cover through a 3-call resumable upload
(handshake, upload, attach). Best-effort by construction: no cover, non-image bytes, a
refused handshake or a failed attach is a silent skip with one `cover` audit line, and
never a failed run. It never runs on a resume or top-up, so it cannot overwrite a
hand-set cover.

## 9. Cost and measurements

Recorded on this machine; treat them as scale indicators, not promises.

- A 2,500-track transfer is about **2,450 adds** and about **25 full read-backs** — one
  playlist read per 100 tracks is the price of verified progress.
- The playlist read is capped at **200 pages** (about 100 entries each, so well past 5,000).
- The Spotify read is sequential: 100 tracks per request, about 0.2 s apart. Measured
  **1,101 tracks in 10.3–10.9 s**; 2,500 tracks extrapolates to roughly 20–25 s before
  YouTube Music is touched at all.
- Auto-match ran at about **90 %** on the playlist this was built against, and every miss is
  listed with its reason.

## 10. Local HTTP surface

Loopback only, `127.0.0.1:8787`, same origin, no CDN and no third party.

| Route | What it does |
|---|---|
| `GET /health` | Liveness for the build smoke test |
| `GET /api/status` | Live status and the audit trail the watchdog reads |
| `GET`/`POST /api/settings` | Only `privacy` is persisted; unknown keys are ignored |
| `GET /api/run` | Poll the current run's ledger and counts |
| `POST /api/transfer` | Start a transfer |
| `POST /api/retry` | End a standby wait and verify immediately |
| `POST /api/check` | Check a supplied Spotify or YouTube Music cookie, falling back to the saved one |
| `POST /api/spotify` | Read a public playlist: name, cover, tracks and the declared total |
| `POST /api/search`, `/api/search-batch` | Search, and batched search (≤25 items, 1–10 workers) |
| `POST /api/create-playlist` | Create a playlist with its inline ids, or return the existing copy |
| `GET /api/cookies`, `/api/vault` | Cookie state — flags and lengths, never values |
| `POST /api/cookies/check`, `/use`, `/remove`, `/latch` | Verify, choose, remove, latch a cookie |
| `POST /api/watch` | Read or set the one extra watch folder |
| `POST /api/reset` | Clear the run and its created-playlist link (refused while a run is live) |
| `POST /api/forget` | Clear the vault and the watch path |

## 11. State on disk

`%APPDATA%\sp2yt`, falling back to the repo directory when that is unwritable:

- `created.json` — the create record: plan, playlist link, unknowns, read floor.
- `settings.json` — privacy, and nothing else.
- `server.log` — the audit trail. Lengths and flags only; cookie values are never written.
- the cookie vault (DPAPI-encrypted per Windows user) and the watch path.

All of it is git-ignored. `python3 watchdog.py` asks the running server for its audit trail
and prints the fix for the latest failure (`--watch` repeats every 15 s).
