# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Nothing yet.

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
