#!/usr/bin/env python3
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import fixture_lib  # noqa: E402

import backend  # noqa: E402

PLID = "PLsliceTest0001"
SPOTIFY_URL = "https://open.spotify.com/playlist/" + PLID
COOKIE = "SID=test-only; HSID=test-only; SAPISID=test-only-value"
PAGE = 100        # InnerTube musicPlaylistShelfRenderer page size
SLICE = backend.TRANSFER_SLICE if hasattr(backend, "TRANSFER_SLICE") else 100
SKIP = "DEDUPE_OPTION_SKIP"


def vid(n: int) -> str:
    return "S%010d" % n


def song_no(title: str):
    m = re.search(r"(\d+)$", str(title or ""))
    return int(m.group(1)) if m else None


def source_tracks(n=2500, repeat_of=50):
    rows = []
    for i in range(1, n + 1):
        if i <= n // 2 + repeat_of and i > n // 2:
            song = i - n // 2
            album = "Single %03d" % song
        else:
            song = i
            album = "Album A"
        rows.append({"uri": "spotify:track:%022d" % i, "title": "Song %04d" % song,
                     "artist": "Test Artist %04d" % song, "duration_ms": 180000 + song,
                     "album": album})
    return rows


def search_sections(song: int, album="Album A"):
    return [{"musicShelfRenderer": {"contents": [{"musicResponsiveListItemRenderer": {
        "playlistItemData": {"videoId": vid(song)},
        "flexColumns": [
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                {"text": "Song %04d" % song}]}}},
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                {"text": "Song"}, {"text": " \u2022 "},
                {"text": "Test Artist %04d" % song}, {"text": " \u2022 "},
                {"text": album}]}}},
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

    def __init__(self, tracks, broken_from=0, frozen=False):
        self.tracks = tracks
        self.broken_from = broken_from
        self.frozen = frozen        # a read served from the playlist as of the create
        self.snapshot = None
        self.playlist = []          # what the playlist really holds, in order
        self.appends = []           # every id an ACTION_ADD_VIDEO asked for
        self.actions = []           # every ACTION_ADD_VIDEO entry an edit carried
        self.creates = 0
        self.reads = 0              # read bursts (first-page browses)
        self.pages = 0
        self.searches = {}
        self.watcher = None
        self.seq = []               # ("add", videoId) / ("read", entries) in call order
        for src in tracks:
            song = song_no(src["title"])
            for q in backend._query_variants(src["title"], src["artist"]):
                self.searches[q] = search_sections(song)

    def note(self, stage):
        if self.watcher is None:
            return
        snap = backend.run_snapshot()
        c = dict(snap.get("counts") or {})
        rows = snap.get("rows") or []
        self.watcher({
            "stage": stage,
            "added": int(c.get("added", 0) or 0),
            "pending": c.get("pending"),
            "keys": sorted(c),
            "rows_landed": sum(1 for r in rows if r.get("state") == "landed"),
            "rows_pending": sum(1 for r in rows
                                if r.get("state") in ("appending", "pending")),
            "playlist": len(self.playlist),
            "appends": len(self.appends),
        })

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", "") or str(req)
        data = getattr(req, "data", None)
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
        raise MissingResponse("no fake for " + url)

    def search(self, body):
        q = str(body.get("query", ""))
        if q not in self.searches:
            raise MissingResponse("no fake search for " + q)
        return json_resp(fixture_lib.envelope(self.searches[q]))

    def create(self, body):
        self.creates += 1
        for v in dict.fromkeys(body.get("videoIds") or []):
            if v not in self.playlist:
                self.playlist.append(v)
        self.note("create")
        return json_resp({"playlistId": PLID})

    def edit(self, body):
        act = (body.get("actions") or [{}])[0]
        self.actions.append(act)
        v = act.get("addedVideoId") or ""
        self.seq.append(("add", v))
        if self.frozen and self.snapshot is None:
            self.snapshot = list(self.playlist)
        if v and v in self.playlist and act.get("dedupeOption") != SKIP:
            return json_resp({"status": "STATUS_FAILED"})
        if v:
            self.playlist.append(v)
            self.appends.append(v)
        if len(self.appends) % 10 == 0:
            self.note("append")
        return json_resp({"status": "STATUS_SUCCEEDED"})

    def browse(self, body):
        token = body.get("continuation")
        if token:
            self.pages += 1
            if self.broken_from and self.reads >= self.broken_from:
                raise urllib.error.URLError("continuation stalled")
            start = int(re.sub(r"\D", "", token) or 0)
        else:
            self.reads += 1
            start = 0
        held = list(self.snapshot if self.snapshot is not None else self.playlist)
        if not token:
            self.seq.append(("read", len(held)))
        page = held[start:start + PAGE]
        shelf = {"contents": [{"musicResponsiveListItemRenderer": {"playlistItemData": {"videoId": v}}}
                              for v in page]}
        if start + len(page) < len(held):
            shelf["continuations"] = [{"nextContinuationData": {
                "continuation": "page:%d" % (start + len(page))}}]
        if not token:
            self.note("read")
        return json_resp({"contents": {"musicPlaylistShelfRenderer": shelf}})


def fake_fetch(rows, name="Slice Test"):
    def fetch(url, cookie=""):
        return {"playlist_id": "SPOTSLICE", "playlist_name": name, "cover_url": "",
                "tracks": list(rows), "declared_total": len(rows)}
    return fetch


class Slices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp2yt-slices-")
        self._saved = {k: getattr(backend, k) for k in
                       ("LOGF", "CREATEDF", "SETTINGF", "_SEARCH_CACHE")}
        backend.LOGF = os.path.join(self.tmp, "server.log")
        backend.CREATEDF = os.path.join(self.tmp, "created.json")
        backend.SETTINGF = os.path.join(self.tmp, "settings.json")
        backend._SEARCH_CACHE = {}
        self.snaps = []
        self.waits = []
        self._seams = [mock.patch.object(backend, "_pause", lambda s: None),
                       mock.patch.object(backend, "_standby_wait", self._wait)]
        for p in self._seams:
            p.start()

    def _wait(self, seconds):
        self.waits.append(seconds)

    def tearDown(self):
        for p in self._seams:
            p.stop()
        for k, v in self._saved.items():
            setattr(backend, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transfer(self, upstream, rows):
        upstream.watcher = self.snaps.append
        with mock.patch("urllib.request.urlopen", upstream), \
                mock.patch.object(backend, "fetch_spotify_tracks", fake_fetch(rows)):
            rid = backend.run_begin("", "")
            backend._transfer_thread(SPOTIFY_URL, COOKIE, "", "PRIVATE", rid)
        return backend.run_snapshot()

    def test_2500_rows_keep_every_occurrence_in_source_order(self):
        rows = source_tracks(2500, 50)
        upstream = Upstream(rows)
        snap = self.transfer(upstream, rows)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        expected = [vid(song_no(r["title"])) for r in rows]
        self.assertEqual(upstream.creates, 1, "the plan must create once")
        self.assertEqual(upstream.playlist, expected,
                         "the playlist is not the source plan, in order")
        self.assertEqual(len(upstream.playlist), 2500)
        counts = snap["counts"]
        self.assertEqual(counts["source"], 2500, counts)
        self.assertEqual(counts["unique"], 2450, counts)
        self.assertEqual(counts["added"], 2500, counts)
        self.assertEqual(counts["duplicate"], 0, counts)
        self.assertEqual(counts["refused"], 0, counts)
        self.assertEqual(counts["unverified"], 0, counts)
        self.assertEqual(counts["pending"], 0, counts)
        self.assertEqual(snap["state"], "done")
        self.assertEqual(snap["summary"].count("\n"), 0)
        self.assertIn("holds 2500", snap["summary"])

    def test_the_write_is_sent_in_100_song_slices_with_a_read_back_after_each(self):
        rows = source_tracks(2500, 50)
        upstream = Upstream(rows)
        snap = self.transfer(upstream, rows)
        self.assertEqual(snap["phase"], "done", snap.get("error"))

        to_send = 2500 - backend.CREATE_INLINE_MAX
        self.assertEqual(len(upstream.appends), to_send,
                         "an append per copy, no re-sends and no extra call")
        slices = -(-to_send // 100)
        self.assertLessEqual(upstream.reads, 1 + slices + 2,
                             "the run made %d read bursts for %d slices"
                             % (upstream.reads, slices))
        self.assertGreaterEqual(upstream.reads, 1 + slices,
                                "a slice was sent without its own read-back")
        for act in upstream.actions:
            self.assertEqual(act.get("dedupeOption"), SKIP, act)

    def test_live_progress_is_verified_and_a_sent_copy_is_pending(self):
        rows = source_tracks(2500, 50)
        upstream = Upstream(rows)
        snap = self.transfer(upstream, rows)
        self.assertEqual(snap["phase"], "done", snap.get("error"))
        self.assertTrue(self.snaps, "no progress was observed")

        added = [s["added"] for s in self.snaps]
        self.assertEqual(added, sorted(added), "verified progress went backwards")
        self.assertGreaterEqual(len(set(added)), 20,
                                "progress moved in %d steps over 2,500 rows" % len(set(added)))
        for s in self.snaps:
            self.assertIn("pending", s["keys"], s)
            self.assertEqual(s["added"], s["rows_landed"],
                             "counts.added disagrees with the landed rows")
            self.assertLessEqual(s["added"], s["playlist"],
                                 "a copy was counted added before it was on the playlist")
            self.assertEqual(s["pending"], s["rows_pending"],
                             "counts.pending disagrees with the in-flight rows")
        self.assertLess(min(added), 500, "no early progress was ever served")
        self.assertGreater(max(added), 2000, "no late progress was ever served")
        pending_snaps = [s for s in self.snaps if s["pending"]]
        self.assertTrue(pending_snaps, "no in-flight copy was ever reported pending")

    def test_a_broken_continuation_stops_the_sends_and_claims_nothing(self):
        rows = source_tracks(300, 0)
        upstream = Upstream(rows, broken_from=2)   # read 1 = the create's
        snap = self.transfer(upstream, rows)

        self.assertEqual(len(upstream.appends), 100,
                         "a copy was sent after a read that could not reach the end")
        self.assertEqual(len(upstream.playlist), 150)
        self.assertEqual(snap["landed"], 100, snap["counts"])
        self.assertEqual(snap["refused"], 0, snap["counts"])
        self.assertEqual(snap["missing"], 0, snap["counts"])
        self.assertEqual(snap["unverified"], 200, snap["counts"])
        rest = [r for r in snap["rows"] if r.get("state") != "landed"]
        self.assertEqual(len(rest), 200)
        for row in rest:
            blob = (str(row.get("state", "")) + " " + str(row.get("status", ""))).lower()
            self.assertTrue(any(w in blob for w in ("unverified", "not verified", "unconfirmed")),
                            "row %r claims a state it cannot prove" % (row,))
        self.assertIn("unverified", snap["summary"].lower())
        self.assertNotIn("200 missing", snap["summary"])
        self.assertNotIn("missing 200", snap["summary"])
        self.assertEqual(sorted(upstream.appends), sorted(set(upstream.appends)),
                         "a copy was appended twice off an incomplete read")
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(len(self.waits), backend.RUN_RETRY_MAX)
        self.assertEqual(self.waits, sorted(self.waits))
        self.assertEqual(self.waits, list(dict.fromkeys(self.waits)),
                         "the backoff did not grow between attempts")


    def test_a_stale_read_that_hides_the_slice_stops_the_run(self):
        rows = source_tracks(300, 0)
        upstream = Upstream(rows, frozen=True)
        snap = self.transfer(upstream, rows)

        self.assertEqual(len(upstream.appends), 100,
                         "a slice was sent on a read that never showed the last one")
        self.assertEqual(len(upstream.playlist), 150)
        self.assertEqual(snap["landed"], 50, snap["counts"])
        self.assertEqual(snap["unverified"], 250, snap["counts"])
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(len(self.waits), backend.RUN_RETRY_MAX)

    def test_a_partial_read_never_publishes_verified_progress(self):
        rows = source_tracks(300, 0)
        upstream = Upstream(rows, broken_from=2)
        snap = self.transfer(upstream, rows)
        seed = backend.CREATE_INLINE_MAX
        for s in self.snaps:
            self.assertLessEqual(s["added"], seed,
                                 "a partial read published verified progress: %r" % (s,))
        self.assertEqual(snap["landed"], 100, snap["counts"])


    def test_the_repair_is_sent_in_slices_proved_by_their_own_read(self):
        n = 250
        vids = [vid(i) for i in range(1, n + 1)]
        upstream = Upstream([])          # the playlist holds none of them
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, vids)

        self.assertEqual(out["state"], "done", out)
        self.assertEqual(upstream.playlist, vids,
                         "the repair did not send the plan once, in plan order")
        runs, run = [], 0
        for kind, _ in upstream.seq:
            if kind == "add":
                run += 1
            else:
                runs.append(run)
                run = 0
        self.assertEqual(sum(runs), n)
        self.assertLessEqual(max(runs), backend.TRANSFER_SLICE,
                             "a repair batch of %d went out with no read between them"
                             % max(runs))
        self.assertEqual(upstream.reads, 1 + -(-n // backend.TRANSFER_SLICE),
                         "the repair made %d read bursts for %d slices"
                         % (upstream.reads, -(-n // backend.TRANSFER_SLICE)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
