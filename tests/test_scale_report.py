#!/usr/bin/env python3
"""Scale regression: a 500-track transfer must report what the playlist holds.

The transfer derives every reported outcome from the playlist read-back
(yt_playlist_entries). A real ~500-item playlist is served by InnerTube in
pages of ~100 behind a continuation token. When the read-back consumes one
page and never follows the continuation, 400 tracks that really landed are
scored absent and reported as refused/missing.

No network: urlopen is replaced by one upstream that answers exactly the
shapes backend.py asks for, and holds the playlist as a real one would -
paged, with a continuation, and refusing an id it already holds.

Run: python -X utf8 tests/test_scale_report.py   (exit 0 only when every check passes)
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import fixture_lib  # noqa: E402

import backend  # noqa: E402

TRACKS = 500
PAGE = 100  # InnerTube musicPlaylistShelfRenderer page size
PLID = "PLscaleReport0001"
SPOTIFY_URL = "https://open.spotify.com/playlist/" + PLID
YT_COOKIE = "SID=test-only; HSID=test-only; SAPISID=test-only-value"
BUNDLE_JS = "https://open.spotifycdn.com/cdn/build/web-player/web-player.test01.js"
# The report may not print a refusal it cannot prove; these are the words that
# mark a track as present-but-unproven instead.
UNVERIFIED_WORDS = ("unverified", "inconclusive", "not verified", "unconfirmed")


def vid(i: int) -> str:
    """11-char InnerTube videoId for source track i (1-based)."""
    return "T%010d" % i


def source_tracks(n=TRACKS):
    """The Spotify rows a 500-track playlist resolves to: plain titles, no versions."""
    return [{"uri": "spotify:track:%022d" % i, "title": "Test Track %03d" % i,
             "artist": "Test Artist %03d" % i, "duration_ms": 180000 + i}
            for i in range(1, n + 1)]


def search_sections(i: int):
    """One confident match, in the recorded-fixture shape (fixture_lib.envelope wraps it)."""
    return [{"musicShelfRenderer": {"contents": [{"musicResponsiveListItemRenderer": {
        "playlistItemData": {"videoId": vid(i)},
        "flexColumns": [
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                {"text": "Test Track %03d" % i}]}}},
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                {"text": "Song"}, {"text": " \u2022 "}, {"text": "Test Artist %03d" % i}]}}},
        ],
        "fixedColumns": [{"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [
            {"text": "3:00"}]}}}],
    }}]}}]


class MissingResponse(BaseException):
    """BaseException on purpose: a request the fake does not know must not be
    swallowed into a skip - that would make a harness gap look like a match
    failure."""


class Resp:
    def __init__(self, body=b"", status=200):
        self._body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.status = status

    def read(self, *a):
        return self._body[:a[0]] if a else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def json_resp(obj):
    return Resp(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


class Upstream:
    """The fake platform: Spotify embed + web-player token, InnerTube search,
    playlist create/append/browse. Holds the playlist the way YTM does."""

    def __init__(self, tracks, paging_broken=False):
        self.tracks = tracks
        self.paging_broken = paging_broken
        self.playlist = []          # what the playlist really holds, in order
        self.appends = []           # every id an ACTION_ADD_VIDEO asked for
        self.refused_adds = []      # ids YTM already held (a real edit_playlist refuses those)
        self.calls = []
        self.continuations = []     # continuation tokens the code asked for
        self.searches = {}
        for i in range(1, len(tracks) + 1):
            src = tracks[i - 1]
            sections = search_sections(i)
            for q in backend._query_variants(src["title"], src["artist"]):
                self.searches[q] = sections

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", "") or str(req)
        data = getattr(req, "data", None)
        self.calls.append(url)
        try:
            body = json.loads(data.decode("utf-8")) if data else {}
        except Exception:
            body = {}
        if "edit_playlist" in url:
            return self.edit(body)
        if "youtubei/v1/playlist/create" in url:
            return self.create(body)
        if "youtubei/v1/browse" in url:
            return self.browse(body)
        if "youtubei/v1/search" in url:
            return self.search(body)
        if "/embed/playlist/" in url:
            return self.embed()
        if "api-partner.spotify.com/pathfinder" in url:
            return self.pathfinder(url)
        if url == BUNDLE_JS:
            return Resp(self.bundle_js())
        if "open.spotify.com/api/server-time" in url:
            return json_resp({"serverTime": int(time.time())})
        if "open.spotify.com/api/token" in url:
            return json_resp({"accessToken": "test-anon-token",
                              "accessTokenExpirationTimestampMs": int((time.time() + 3600) * 1000)})
        if url.rstrip("/") == "https://open.spotify.com":
            return Resp(self.home_html())
        raise MissingResponse("no fake for " + url)

    # --- Spotify -----------------------------------------------------------
    def home_html(self):
        return ('<html><script src="' + BUNDLE_JS + '"></script></html>')

    def bundle_js(self):
        qhash = "0" * 64
        return ('var a={secret:"test-secret",version:5};'
                'var b=["fetchPlaylist","query","' + qhash + '"];')

    def embed(self):
        entity = {"title": "Scale Test 500", "coverArt": {"sources": []}}
        page = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
        return Resp('<html><script id="__NEXT_DATA__" type="application/json">'
                    + json.dumps(page) + "</script></html>")

    def pathfinder(self, url):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        variables = json.loads((qs.get("variables") or ["{}"])[0] or "{}")
        off = int(variables.get("offset", 0) or 0)
        limit = int(variables.get("limit", PAGE) or PAGE)
        items = []
        for src in self.tracks[off:off + limit]:
            items.append({"itemV2": {"data": {
                "__typename": "Track",
                "uri": src["uri"],
                "name": src["title"],
                "artists": {"items": [{"profile": {"name": src["artist"]}}]},
                "trackDuration": {"totalMilliseconds": src["duration_ms"]},
            }}})
        return json_resp({"data": {"playlistV2": {
            "__typename": "Playlist", "name": "Scale Test 500",
            "content": {"totalCount": len(self.tracks), "items": items}}}})

    # --- InnerTube ---------------------------------------------------------
    def search(self, body):
        q = str(body.get("query", ""))
        if q not in self.searches:
            raise MissingResponse("no fake search for " + q)
        return json_resp(fixture_lib.envelope(self.searches[q]))

    def create(self, body):
        self.playlist.extend(body.get("videoIds") or [])
        return json_resp({"playlistId": PLID})

    def edit(self, body):
        for act in body.get("actions") or []:
            v = act.get("addedVideoId") or ""
            if not v:
                continue
            self.appends.append(v)
            if v in self.playlist:
                # A real edit_playlist refuses an id the list already holds.
                self.refused_adds.append(v)
                return json_resp({"status": "STATUS_FAILED"})
            self.playlist.append(v)
        return json_resp({"status": "STATUS_SUCCEEDED"})

    def browse(self, body):
        """One playlist page, with a continuation when more entries follow.
        Asking for a page the fake cannot deliver raises a transport error."""
        held = list(self.playlist)
        token = body.get("continuation")
        if token:
            self.continuations.append(token)
            if self.paging_broken:
                raise urllib.error.URLError("continuation stalled")
            start = int(re.sub(r"\D", "", token) or 0)
        else:
            start = 0
        page = held[start:start + PAGE]
        shelf = {"contents": [{"musicResponsiveListItemRenderer": {"playlistItemData": {"videoId": v}}}
                              for v in page]}
        if start + len(page) < len(held):
            shelf["continuations"] = [{"nextContinuationData": {"continuation": "page:%d" % (start + len(page))}}]
        return json_resp({"contents": {"musicPlaylistShelfRenderer": shelf}})


class ScaleReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp2yt-scale-")
        self._saved = {k: getattr(backend, k) for k in ("LOGF", "CREATEDF", "_SEARCH_CACHE", "_SP_TOK", "_SP_BUNDLE")}
        backend.LOGF = os.path.join(self.tmp, "server.log")
        backend.CREATEDF = os.path.join(self.tmp, "created.json")
        backend._SEARCH_CACHE = {}
        backend._SP_TOK = {"token": "", "exp_ms": 0}
        backend._SP_BUNDLE = {"js": "", "qhash": "", "at": 0.0}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(backend, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transfer(self, upstream):
        """A complete run through the backend's own transfer path."""
        with mock.patch("urllib.request.urlopen", upstream):
            rid = backend.run_begin("", "")
            backend._transfer_thread(SPOTIFY_URL, YT_COOKIE, "", "PRIVATE", rid)
        return backend.run_snapshot()

    # 1
    def test_a_500_track_transfer_reports_every_track_it_really_holds(self):
        upstream = Upstream(source_tracks())
        snap = self.transfer(upstream)
        self.assertEqual(snap["phase"], "done", snap.get("error"))
        # The fake accepted every add, so the playlist really holds all 500.
        self.assertEqual(len(upstream.playlist), TRACKS)
        self.assertEqual(snap["total"], TRACKS)
        self.assertEqual(snap["landed"], TRACKS)
        self.assertEqual(snap["refused"], 0)
        self.assertEqual(snap["missing"], 0)
        self.assertEqual(snap["playlist_count"], TRACKS)
        self.assertIn("holds %d" % TRACKS, snap["summary"])

    # 2
    def test_the_served_ledger_agrees_with_the_summary(self):
        upstream = Upstream(source_tracks())
        snap = self.transfer(upstream)
        self.assertEqual(snap["phase"], "done", snap.get("error"))
        rows = snap["rows"]
        self.assertEqual(len(rows), TRACKS)
        states = {}
        for row in rows:
            states[row.get("state")] = states.get(row.get("state"), 0) + 1
        # Every present track is a landed row; nothing present is scored absent.
        self.assertEqual(states.get("landed", 0), TRACKS, states)
        self.assertEqual(states.get("refused", 0), 0, states)
        self.assertEqual(states.get("missing", 0), 0, states)
        # The session counters are recounted from those rows, so they must agree.
        self.assertEqual(snap["landed"], states.get("landed", 0))
        self.assertEqual(snap["refused"], states.get("refused", 0))
        self.assertEqual(snap["missing"], states.get("missing", 0))
        wrong = [r for r in rows if r.get("state") in ("refused", "missing")][:5]
        self.assertEqual(wrong, [])

    # 3
    def test_an_incomplete_read_back_never_reports_tracks_as_absent(self):
        upstream = Upstream(source_tracks(), paging_broken=True)
        snap = self.transfer(upstream)
        self.assertEqual(snap["phase"], "done", snap.get("error"))
        self.assertEqual(len(upstream.playlist), TRACKS)
        # Exactly the first page was proven; the rest is present but unproven.
        self.assertEqual(snap["landed"], PAGE)
        self.assertEqual(snap["refused"], 0)
        self.assertEqual(snap["missing"], 0)
        rest = [r for r in snap["rows"] if r.get("state") != "landed"]
        self.assertEqual(len(rest), TRACKS - PAGE)
        for row in rest:
            blob = (str(row.get("state", "")) + " " + str(row.get("status", ""))).lower()
            self.assertTrue(any(w in blob for w in UNVERIFIED_WORDS),
                            "row %r claims a state it cannot prove" % (row,))
        summary = snap["summary"]
        self.assertTrue(any(w in summary.lower() for w in UNVERIFIED_WORDS), summary)
        self.assertIn(str(TRACKS - PAGE), summary)
        # A count it cannot support must not be printed at all.
        self.assertNotIn("refused %d" % (TRACKS - PAGE), summary)
        self.assertNotIn("missing %d" % (TRACKS - PAGE), summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
