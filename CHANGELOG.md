# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-11-17

Large playlists now transfer in 100-track slices, and the words the app uses for progress cost something. The changes below are behaviour changes, not wording changes.

### Changed

- The write goes out in slices of 100 source copies (`TRANSFER_SLICE`), and every slice is proved by a complete read-back before the next one is sent. A 2,500-track transfer is ~2,450 adds and ~25 read-backs, against one blind queue and two reads before.
- Progress is verified progress: `counts.added` only counts a copy a completed read proved present, and it grows slice by slice. `counts.pending` is a copy that was sent and is not proved either way, and `/api/run`'s `attempted` counts it - a run mid-slice used to report its seed count and nothing else until the end. A queued row is in neither.
- The write is 1:1 with the source, always: every add carries `DEDUPE_OPTION_SKIP`, so the same song on an album and on its single stays two entries. The 508-row reference run (480 distinct videoIds, 28 second copies) now reports `landed 508 of 508` instead of `duplicate 28`.

### Fixed

- A slice is no longer followed on a read that does not show it. The read floor rises by the copies the server accepted, and every accepted copy of the slice must be present in that read; a token-complete but stale snapshot now stops the run instead of authorising the next slice - and the repair pass starts from the same floor, so it can no longer re-send the whole plan against a stale page.
- Convergence counts copies, not ids. Plan `[A,B,A,C]` against a playlist holding `[A,B,C]` is one copy short: that copy is re-sent (in plan order) instead of the run persisting `complete: true` over a missing row.
- A repair that lands after a later copy is reported: `order_ok` is false and the done line says the playlist order differs from the source, because `edit_playlist` can only append.
- A copy the server refused (duplicate toast or plain refusal) is not re-sent by the same run; it ends as a named residual, and `complete` stays false.
- Mid-run `added` only moves on a read that reached the end of the playlist; a partial read no longer publishes verified progress.
- A repair is sent through the same sender as the first pass: at most 100 copies, each batch proved by its own read-back before the next goes out. It used to go out as one batch of every absent copy, with a single read after it.
- The convergence pass reports one status per copy, keyed by the copy's position in the plan, and its floor rises by the copies it actually sent. Two copies of one videoId used to collapse to one status and one promised entry, so a stale read one entry short could authorise a third copy for a plan that asks for two.
- An append whose reply is lost is `STATUS_UNSURE`, a class of its own, and is never re-sent automatically - not from inside `yt_add_one`, not by the repair pass, and not after a restart. No read proves such a copy stayed out (a token-complete reply can be a snapshot taken before the write), and every add carries the skip, so a resend is a duplicate source row. The copy is frozen for the run, reported `unverified` with a manual recovery, and the run ends `gaveup` naming it. Against a stale read the old path sent the same copy 14 times.
- A 429 takes the same path. The write endpoint is unofficial, so a rate-limit reply is not proof the request was refused unapplied; the copy is unknown, not retried, and named. The append backoff is gone with it - waiting does not make a read fresh - and the retry budget now governs reads only.
- An unknown write survives a restart. Each slice's positions are written ahead to the resume record before its first add (`inflight`, merged with any queue a lost save left behind), and replaced by its outcomes and the accepted floor (`unsure`, `expected_min`) after it. A resume freezes every position either list names - and every other copy of those songs, which a read cannot tell apart - and reads with the floor as its minimum, so a read that is short of what the run proved is incomplete rather than an authorisation to append a copy that may already be there. A slice cut short by a close leaves at most that slice's copies unknown, and they are named rather than re-sent.
- Persistence fails closed. `save_created` writes a temp file and `os.replace`s it, so a failed save cannot truncate the record it replaces and cannot pass for success; `_persist_fields` returns whether the bytes landed, the floor can only rise and the unknown set only grow, and a run whose write-ahead (or whose create record) cannot be saved sends nothing and says so. The complete-record top-up branch honours the same unknown set and floor, and marks the record incomplete before its first send.
- A repeat inside the inline create is unattributable until a read answers it, and never re-sent on a page that cannot. The create carried one videoId twice, so a stale page showing one copy could authorise the second - and land a third if the create had kept both. Unknowns are now durable by videoId (`unsure_ids`) and carry the multiplicity they were recorded against (`unsure_need`); a complete read that holds that many copies proves the repeat landed (duplicates are allowed), and only the doubt a read does not account for stays `unverified` with the manual recovery.
- An unknown outlives the plan it was recorded in. Adopting a fresh plan no longer drops an unknown id the new plan does not contain, so a later run that matches that song again still knows the copy is unproven and never resends it. A doubt with no recorded multiplicity is never cleared, and a record whose unknown positions have no plan to read them against is refused rather than guessed.
- The write-ahead persists the multiplicity of every copy it queues (`inflight_need`) in the same save as the positions. Without it, a slice that died before its outcomes were known left a doubt no later read could answer: the song stayed frozen even when a complete read showed every copy.
- A doubt is cleared only by a complete read that holds as many copies of the id as the doubt was recorded against. `_converge` re-examines its frozen copies on every complete read, so it can no longer return done while its own record says a copy is unknown; a doubt the read cannot answer ends the run unresolved, naming it. The resume, top-up and converge paths all write `complete:true` only when no unknown remains.
- `_append_verify` starts its floor at the authorising read's own entry count as well as the stored floor, so an older, lower floor cannot let a later short read pass as complete.
- The top-up persists the plan its positions index (ids, slots) and re-expresses the unknown copies in that plan's positions in one write (`_adopt_plan`) before the first send, so a top-up against a fresh match never queues positions that point into an older record's plan. Its read minimum is the last run's proven floor, not this run's distinct count: a plan the source has grown could never be held yet, so demanding it refused every newly added track. Its "nothing to send" branch writes `complete:true` only from a read that reached the end of the playlist and left nothing unknown: a partial or below-floor read proves nothing about the tail.
- A corrupt or unreadable `created.json` fails closed: `load_created` raises `RecordUnreadable` (a missing file is still a first run), so a run cannot treat an unreadable record as empty, create a second playlist over it, and overwrite it. `save_created` writes a temp file and replaces it, so a failed save cannot truncate the record either.
- A create whose reply is lost leaves the create's intent on disk (`create_pending`) before the request goes out, and the failure no longer advises a retry: it names the manual check (look for a playlist with this title, delete it if it is there, Reset, start again), and a later run refuses to create over a pending record.
- An unknown write freezes the whole song, not one row: two copies of one videoId are indistinguishable in a read, so no further copy of that id is sent by the run.
- `complete` no longer counts a refused duplicate or ignores the read: it is written only when the final read reached the end of the playlist and every intended copy was read back. A duplicate pair used to persist `complete: true` over a playlist one copy short.
- A refusal blocks its own copy, not its videoId: refusal and unknown sets are keyed by position in the plan, so a refused occurrence cannot deny a later occurrence that was never sent.
- The repair pass now takes the live-progress hooks: its phase, the copies it sends and the rows its own read proves reach `/api/run` while it works, instead of the page showing the first pass's last state.
- A failed run that matched nothing recounts its counters from its rows, so `source` is the number of tracks read and `skipped` the number that did not match, instead of four zeros.

### Removed

- The 1:1 setting: `one_to_one`, `/api/one-to-one`, `oneToOne` in `/api/run`, and the tray of text that carried it. Nothing decides whether an occurrence is kept any more; it is kept.
- The rank-1 fallback guess. A row that crosses scripts and has no identified candidate is skipped with its reason (`not identified` in the ledger), because rank 1 is the search engine's opinion and the app cannot tell you it is right. The `best_guess` path is gone from the matcher; a record written by an older version still replays its label.

### Added

- The Spotify read carries `album` per row when the player query supplies `albumOfTrack.name`. The album joins the search cache key (folded name plus edition signature, so `Night Drive` and `Night Drive (Deluxe Edition)` cannot share one lookup), an album-bearing query is tried last, and an edition word in the source album breaks a near-tie among candidates already within 0.05 of the leader. No hard edition gate: a row the album query cannot identify skips as before.
- A title is not a match on its own. A candidate whose title agrees and whose artist does not agree at all, on a reply that carried no duration, is refused by name (`title only`) and the next query variant is tried, instead of being accepted at a score of 0.70. An exact title from a reply with no artist and no duration reading was enough to put an unrelated curator upload in a playlist. Artist agreement alone, or a duration that is not plainly wrong, still matches.
- Skipped rows whose search never answered (timeout, closed connection, 429, 5xx) are searched once more - the row's own first query, the same evidence bar, at most `SECOND_SEARCH_MAX` (8) rows per run, one request each, stopping at the first row that still fails on the wire or is refused outright. A row resolved this way says `found on a second search`. A skip that answered about the song is never re-searched, and a row still unresolved keeps its skip.
- Tests: `tests/test_slices.py` (a 2,500-row transfer, live progress, a broken continuation), `tests/test_editions.py` (album carry-through, cache key, edition tie-break, compound `Slowed + Reverb` labels), and the counts/API contract in `tests/test_counts.py`.

### Known limits

- The Spotify read is still sequential: 100 tracks per request, ~0.2 s apart. Measured 1,101 tracks in 10.3–10.9 s; 2,500 tracks extrapolates to ~20–25 s before YouTube Music is touched. Not addressed here.
- One playlist read per 100 tracks is the cost of verified progress: ~25 full read-backs for 2,500 tracks, bounded by the 200-page read cap.
- A copy the server silently drops on a repeated videoId cannot be repaired by the convergence pass (the id is already present), so it is reported `unverified`/`missing` rather than re-sent blind.
- The second search does not widen the query. A row skipped because no candidate carried artist or duration evidence stays skipped: a different query shape might find it, but that is unproven without a real recording, so it is not shipped as a guess. Manual nearest-match selection is not offered either - a picker that cannot prove which copy is right is the same error with a person's name on it.
- A lost reply is not retried by any automatic path. The run freezes that copy, reports it `unverified` with a manual recovery, and ends `gaveup` naming it; a fresh run (or the user, in YouTube Music) is the recovery. The cost is real: a dropped reply, and now a 429 as well, leaves that copy for a person to resolve instead of possibly landing it twice, so a throttled playlist ends with its unproven copies named rather than retried.
- The write-ahead is per slice (up to 100 copies), not per copy: a close in the middle of a slice leaves that slice's copies unknown - named and never re-sent - which is conservative in the direction of no duplicates, at the cost of not re-sending copies the server never received.
- An unknown write freezes the whole song for the run, so a plan that repeats one videoId can end a copy short even where the server would have taken the other occurrence. A playlist read cannot attribute copies of one id, so the run names the shortfall instead of guessing.
- A record that cannot be written stops the transfer. That is deliberate, but it means an unwritable app-data folder now ends a run that would previously have carried on without a resume record.
- A corrupt `created.json` also stops every transfer until it is fixed or deleted: the app will not create a playlist over a record it cannot read, and the escape hatch is deleting the file (Reset the whole run).
- A create whose reply was lost cannot be resolved by the app at all: the playlist may exist with an id nobody saw. The run stops and asks the user to check their library; it never retries, because a retry is how a second playlist gets made.
- A repeat inside the first 50 tracks of a plan is `unverified` only when the read cannot account for it. A complete read that holds both copies clears it (duplicates are allowed, and a count proves multiplicity without attributing copies); a read that shows one copy while the doubt was recorded against two keeps it unknown, and that doubt is never cleared by a plan that has since shrunk to one copy.
- A top-up re-expresses its unknown copies in the plan it adopts, so a song the fresh match no longer contains has no position to carry its unknown. The id is kept (`unsure_ids`), so a later plan that matches the song again still refuses to resend it; it is not named per row in the run that dropped it.
- An unknown recorded by an older version without a multiplicity (`unsure_need`, `inflight_need`) is never cleared by a read, however many copies it shows: the evidence that could answer it was not kept. The copy stays `unverified` for a manual check.
- A resolved doubt needs a complete read that counts enough copies; a playlist that shrank outside the app, or a server that drops a repeated copy, leaves the doubt standing and the run ends naming it. Deleting and re-running is the recovery.

## [0.4.0] - 2026-11-13

This release is about the app telling the truth about what it did. Earlier versions were quick to say Done: a run that gave up halfway could still read as finished, a row whose copy nobody had checked looked the same as one that had landed, and the playlist read could stop at the first page of a long list and call the rest absent. So the claims now cost something. The playlist is read to its end, the counters are counted from the rows instead of accumulated, and Done means a completed read-back proves the playlist holds what the app says it holds.

That shows up in the deck as seven counts that never disagree with each other — source, unique, added, skipped, duplicate, unverified, refused — with a 1:1 control for whether a song listed twice should land twice, and a Copy button for the list of tracks that did not make it. Unverified is its own word, because "sent but never proven" is neither a success nor a refusal.

A run that meets a throttle or a dead read now stands by with a clock and a reason, realigns against the playlist once it can see the whole of it, and gives up bounded and out loud rather than quietly reshaping the playlist. Restarting in the middle of one resumes the playlist it was already building from the plan it wrote down, and a plan it cannot vouch for is reported as interrupted instead of guessed at.

### Known limits

- Mid-run resume across an app restart is best-effort: the plan is persisted when the playlist is created, never re-derived, so a record that cannot name its own plan is reported as interrupted and the transfer has to be started fresh.
- A settings file that cannot be written surfaces as an error rather than a silent success — a setting is only in force once the bytes are on disk.
- The playlist read is capped at 200 pages and reports `incomplete` above that instead of claiming the tail is absent.
- The WebView render is not covered by the automated tests: the page script is parsed and its logic is checked, but no test proves the window draws.

## [0.3.1] - 2026-11-11

### Fixed

- The cover tile can no longer show a broken-image glyph after a failed cover load: the error handler repaints a transparent placeholder pixel and the gradient instead of removing `src`, which WebView2 keeps painting.
- The app page is served `no-store` (with `Pragma: no-cache`), so a rebuilt exe cannot render a cached older page.

## [0.3.0] - 2026-11-11

### Added

- Script-aware matching: a Japanese-script title and its romaji spelling — in either direction — now align, and a script-crossing row with no confident match is added as a labelled best guess (`best guess` in the ledger and the done line) instead of leaving a hole in the playlist.
- The new playlist inherits the source Spotify cover when YouTube Music accepts it. The upload is best-effort and silent: a failed cover is a skip that never affects the transfer, and a resume never touches an existing cover.

### Changed

- The acceptance gate replays 36 fixture rows and now runs the language tests and the cover tests as well.

## [0.2.1] - 2026-11-11

### Fixed

- The link row reads as one row: the playlist input, the cover tile and the Public/Unlisted/Private control now share a single height and top edge instead of three staggered ones.
- The PRIVACY label can no longer be stranded from its control when the row wraps in a narrow window (it could sit 61.5px away).
- The cover placeholder no longer breaks mid-phrase and leaves a single orphaned word on its second line.
- Key hints in the footer no longer clip by 2px.

## [0.2.0] - 2026-11-11

### Added

- One-step Connect card for the YouTube Music cookie: Choose file, Paste, Replace, Remove.
- Desktop and Downloads are scanned for a cookie export, including `music.youtube.com_cookies.txt`, and a file the user chooses stays chosen.
- A value-free cookie state API, so the page can say Connected, Saved, Expired or Try again without ever holding a cookie value.

### Changed

- The advanced drawer is gone. The watch folder and Copy rows live in `More options`.
- The start button gates on a usable cookie instead of letting the run fail at the first write.

### Fixed

- An expired or junk export no longer replaces a working cookie.
- A new cookie is checked before it is trusted.
- Remove stays removed until the user chooses a file.

## [0.1.0] - 2026-11-05

### Added

- Initial public release: keyless public Spotify playlist read, InnerTube `WEB_REMIX` matching, playlist create and append with a pasted YouTube Music cookie, a read-back ledger that reconciles every landed row, repeat-preserving writes, resume on re-run, a DPAPI-encrypted cookie vault, and a WebView2 desktop shell built as one portable `sp2yt.exe`.
- Offline acceptance gate replaying 30 recorded rows through the real matcher.
