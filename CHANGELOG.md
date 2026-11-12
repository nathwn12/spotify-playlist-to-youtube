# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
