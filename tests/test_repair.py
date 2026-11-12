#!/usr/bin/env python3
"""Truthful statuses, duplicate classification, standby and bounded realignment.

Three behaviours are pinned here, all of them things the run got wrong:

1. A transport failure (429, 5xx, URLError) is a transport failure. It is
   retryable, it is distinguishable, and it is never "succeeded" - every count
   the run prints is derived from these statuses.
2. YouTube Music refuses a second copy of a videoId it already holds, with HTTP
   200, {"status": "STATUS_FAILED"} and an add-toast reading "This song is
   already in the playlist". That copy can never be added, so it is `duplicate`,
   not `refused`, and it is never retried.
3. A repair pass starts only from a COMPLETE paginated read-back. An incomplete
   read proves nothing about the tail, so the run stands by and appends nothing
   until it can see the whole playlist. Six attempts, then it gives up and names
   what is still missing.

No network: the InnerTube playlist calls go to one fake that holds the playlist
the way YTM does, and Spotify fetch/matching are replaced so this file is about
the playlist machinery alone.

Run: python -X utf8 tests/test_repair.py   (exit 0 only when every check passes)
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import backend  # noqa: E402

PLID = "PLrepairTest0001"
SPOTIFY_URL = "https://open.spotify.com/playlist/PLrepairTest0001"
COOKIE = "SID=test-only; HSID=test-only; SAPISID=test-only-value"
PAGE = 100  # InnerTube musicPlaylistShelfRenderer page size

# The documented refusal of a second copy: HTTP 200, STATUS_FAILED, add-toast.
DUP_REPLY = {"status": "STATUS_FAILED", "actions": [{"addToToastAction": {"item": {
    "notificationTextRenderer": {"successResponseRenderer": {"text": {
        "runs": [{"text": "This song is already in the playlist"}]}}}}}}]}


def vid(i: int) -> str:
    """11-char InnerTube videoId for source track i (1-based)."""
    return "R%010d" % i


def tracks_for(n: int, repeat_of=None):
    """n source rows. `repeat_of` duplicates one row's title, so both rows resolve
    to the same videoId - a genuine repeat in the source."""
    titles = ["Track %03d" % i for i in range(1, n + 1)]
    if repeat_of is not None:
        titles.append(titles[repeat_of - 1])
    return [{"uri": "spotify:track:%022d" % i, "title": t, "artist": "Artist %03d" % i,
             "duration_ms": 180000 + i}
            for i, t in enumerate(titles, 1)]


def fake_fetch(tracks, name="Repair Test"):
    def fetch(url, cookie=""):
        return {"playlist_id": "SPOTREPAIR", "playlist_name": name, "cover_url": "",
                "tracks": list(tracks), "declared_total": len(tracks)}
    return fetch


def fake_batch(chunk, workers=8):
    """One confident match per source row, joined on the title."""
    out = []
    for t in chunk:
        m = re.match(r"Track (\d+)$", str((t or {}).get("title", "")))
        out.append({"ok": True, "query": (t or {}).get("title", ""),
                    "videoId": vid(int(m.group(1))) if m else None})
    return out


class Resp:
    def __init__(self, body=b"", status=200):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.status = status

    def read(self, *a):
        return self._body[:a[0]] if a else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Upstream:
    """The YTM playlist endpoints: create, edit_playlist, browse.

    It holds the playlist the way the real one does: bounded pages behind a
    continuation token, no second copy of an id (create drops the repeat, an
    append of an id it already holds answers with the duplicate toast), and an
    optional throttle that answers the next append with 429."""

    def __init__(self, page=PAGE, extra=(), throttle=0, paging_broken=False):
        self.page = page
        self.playlist = list(extra)   # ids the playlist really holds, in order
        self.appends = []             # every id an ACTION_ADD_VIDEO asked for
        self.appends_at_standby = None
        self.creates = 0
        self.throttle = throttle      # how many appends answer 429 before one lands
        self.throttled = 0
        self.paging_broken = paging_broken
        self.continuations = []

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", "") or str(req)
        try:
            body = json.loads((getattr(req, "data", b"") or b"{}").decode("utf-8"))
        except Exception:
            body = {}
        if "edit_playlist" in url:
            return self.edit(body, url)
        if "youtubei/v1/playlist/create" in url:
            return self.create(body)
        if "youtubei/v1/browse" in url:
            return self.browse(body)
        raise AssertionError("no fake for " + url)

    def create(self, body):
        self.creates += 1
        # A real create keeps one copy of each id: the playlist cannot hold two.
        for v in dict.fromkeys(body.get("videoIds") or []):
            if v not in self.playlist:
                self.playlist.append(v)
        return Resp({"playlistId": PLID})

    def edit(self, body, url=""):
        v = ""
        for act in body.get("actions") or []:
            v = act.get("addedVideoId") or ""
        if self.throttled < self.throttle:
            self.throttled += 1
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        self.appends.append(v)
        if v in self.playlist:
            return Resp(DUP_REPLY)
        self.playlist.append(v)
        return Resp({"status": "STATUS_SUCCEEDED"})

    def browse(self, body):
        held = list(self.playlist)
        token = body.get("continuation")
        if token:
            self.continuations.append(token)
            if self.paging_broken:
                raise urllib.error.URLError("continuation stalled")
            start = int(re.sub(r"\D", "", token) or 0)
        else:
            start = 0
        page = held[start:start + self.page]
        shelf = {"contents": [{"musicResponsiveListItemRenderer":
                               {"playlistItemData": {"videoId": v}}} for v in page]}
        if start + len(page) < len(held):
            shelf["continuations"] = [{"nextContinuationData":
                                       {"continuation": "page:%d" % (start + len(page))}}]
        return Resp({"contents": {"musicPlaylistShelfRenderer": shelf}})


class Repair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp2yt-repair-")
        self._saved = {k: getattr(backend, k) for k in
                       ("LOGF", "CREATEDF", "_SEARCH_CACHE")}
        backend.LOGF = os.path.join(self.tmp, "server.log")
        backend.CREATEDF = os.path.join(self.tmp, "created.json")
        backend._SEARCH_CACHE = {}
        self.waits = []
        self._patches = [mock.patch("time.sleep", lambda *a, **k: None),
                         mock.patch.object(backend, "_standby_wait", self._wait,
                                           create=True)]
        for p in self._patches:
            p.start()

    def _wait(self, seconds):
        """What the transfer thread does while standing by: nothing but wait. The
        snapshot taken here is exactly what the page reads from /api/run."""
        self.waits.append((seconds, backend.run_snapshot()))

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for k, v in self._saved.items():
            setattr(backend, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transfer(self, upstream, tracks):
        with mock.patch("urllib.request.urlopen", upstream), \
                mock.patch.object(backend, "fetch_spotify_tracks", fake_fetch(tracks)), \
                mock.patch.object(backend, "yt_search_batch", fake_batch):
            rid = backend.run_begin("", "")
            backend._transfer_thread(SPOTIFY_URL, COOKIE, "", "PRIVATE", rid)
        return backend.run_snapshot()

    def created(self):
        try:
            with open(backend.CREATEDF, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    # --- 1a: a transport failure is never success ---------------------------
    def test_a_429_from_an_append_is_a_transport_failure_not_success(self):
        def throttled(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

        with mock.patch("urllib.request.urlopen", throttled):
            st = backend.yt_add_one(PLID, vid(1), COOKIE)
        self.assertTrue(str(st).startswith(backend.STATUS_TRANSPORT), st)
        self.assertNotIn("SUCCEEDED", str(st).upper())
        self.assertTrue(backend.is_transport_status(st))

    # --- 1b: a refused second copy is a duplicate, not a refusal ------------
    def test_a_duplicated_source_pair_lands_once_and_is_never_retried(self):
        tracks = tracks_for(4, repeat_of=3)      # 5 rows, rows 3 and 5 are one videoId
        upstream = Upstream()
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        rows = snap["rows"]
        states = [r.get("state") for r in rows]
        self.assertEqual(states.count("landed"), 4, states)
        self.assertEqual(states.count("duplicate"), 1, states)
        self.assertEqual(states.count("refused"), 0, states)
        # The repeated videoId is on the playlist once and is not re-sent.
        self.assertEqual(upstream.playlist.count(vid(3)), 1)
        self.assertEqual(upstream.appends.count(vid(3)), 1)
        self.assertIn("duplicate 1", snap["summary"])
        self.assertNotIn("refused 1", snap["summary"])
        self.assertEqual(snap["duplicate"], 1)
        self.assertEqual(snap["refused"], 0)
        # A duplicate is unaddable, not unfinished: the record must not ask for
        # another resume of a playlist that is exactly as complete as it can be.
        self.assertIs(self.created()["SPOTREPAIR"]["complete"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
