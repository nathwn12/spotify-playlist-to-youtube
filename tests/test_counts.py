#!/usr/bin/env python3
"""The seven counts the page reads, and the 1:1 toggle that moves them.

`RUN["counts"]` is the single structured source of the arithmetic:

    source      tracks read from Spotify, duplicates included
    unique      distinct videoIds in the intended plan
    added       verified present after a complete read-back
    skipped     never attempted, no confident match
    duplicate   second copies the server refused because the id was already there
    unverified  present in no completed read
    refused     the server refused for a reason other than duplication

The owner's real run is the reference point: 508 rows in the source, 480 distinct
videoIds, 28 second copies refused. Those one-to-one == off numbers are pinned here
exactly, together with the 1:1 toggle that changes them: with it on every add carries
`"dedupeOption": "DEDUPE_OPTION_SKIP"`, so the server accepts a second copy and the
same 28 copies verify as `added` instead of `duplicate`.

No network: the InnerTube playlist calls go to one fake that holds the playlist the
way a real one does - paged, and refusing an id it already holds unless the action
asked it to skip that check. Spotify fetch/matching are replaced.

Run: python -X utf8 tests/test_counts.py   (exit 0 only when every check passes)
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import backend  # noqa: E402

PLID = "PLcountsTest0001"
SPOTIFY_URL = "https://open.spotify.com/playlist/PLcountsTest0001"
COOKIE = "SID=test-only; HSID=test-only; SAPISID=test-only-value"
PAGE = 100          # InnerTube musicPlaylistShelfRenderer page size
SOURCE = 508        # the owner's real run: rows read from Spotify
REPEATS = 28        # of those, second copies of an id already in the plan
SKIP = "DEDUPE_OPTION_SKIP"

# The documented refusal of a second copy: HTTP 200, STATUS_FAILED, add-toast.
DUP_REPLY = {"status": "STATUS_FAILED", "actions": [{"addToToastAction": {"item": {
    "notificationTextRenderer": {"successResponseRenderer": {"text": {
        "runs": [{"text": "This song is already in the playlist"}]}}}}}}]}


def vid(i: int) -> str:
    """11-char InnerTube videoId for source track i (1-based)."""
    return "C%010d" % i


def tracks(unique: int, repeats: int = 0):
    """`unique` distinct rows, then `repeats` rows repeating the first ones - a
    genuine repeat in the source: same track, same videoId, asked for twice."""
    titles = ["Track %03d" % i for i in range(1, unique + 1)]
    titles += titles[:repeats]
    return [{"uri": "spotify:track:%022d" % i, "title": t, "artist": "Artist %03d" % i,
             "duration_ms": 180000 + i}
            for i, t in enumerate(titles, 1)]


def fake_fetch(rows, name="Counts Test"):
    def fetch(url, cookie=""):
        return {"playlist_id": "SPOTCOUNTS", "playlist_name": name, "cover_url": "",
                "tracks": list(rows), "declared_total": len(rows)}
    return fetch


def fake_batch(chunk, workers=8):
    """One confident match per row, joined on the title."""
    out = []
    for t in chunk:
        m = re.match(r"Track (\d+)$", str((t or {}).get("title", "")))
        out.append({"ok": True, "query": (t or {}).get("title", ""),
                    "videoId": vid(int(m.group(1))) if m else None})
    return out


class Resp:
    def __init__(self, body=b""):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def read(self, *a):
        return self._body[:a[0]] if a else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Upstream:
    """The YTM playlist endpoints: create, edit_playlist, browse.

    It holds the playlist the way the real one does: bounded pages behind a
    continuation token, and an append of an id it already holds is refused with
    the duplicate toast - unless the action entry carried `dedupeOption`
    DEDUPE_OPTION_SKIP, which is exactly what the 1:1 toggle turns on. Every
    ACTION_ADD_VIDEO action entry it was sent is kept for inspection."""

    def __init__(self, page=PAGE):
        self.page = page
        self.playlist = []
        self.actions = []     # every ACTION_ADD_VIDEO entry an edit call carried
        self.creates = 0
        self.plain_refusals = False  # answer STATUS_FAILED with no duplicate toast

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", "") or str(req)
        try:
            body = json.loads((getattr(req, "data", b"") or b"{}").decode("utf-8"))
        except Exception:
            body = {}
        if "edit_playlist" in url:
            return self.edit(body)
        if "youtubei/v1/playlist/create" in url:
            return self.create(body)
        if "youtubei/v1/browse" in url:
            return self.browse(body)
        raise AssertionError("no fake for " + url)

    def create(self, body):
        self.creates += 1
        for v in dict.fromkeys(body.get("videoIds") or []):
            if v not in self.playlist:
                self.playlist.append(v)
        return Resp({"playlistId": PLID})

    def edit(self, body):
        act = (body.get("actions") or [{}])[0]
        self.actions.append(act)
        v = act.get("addedVideoId") or ""
        if v in self.playlist and act.get("dedupeOption") != SKIP:
            return Resp({"status": "STATUS_FAILED"} if self.plain_refusals else DUP_REPLY)
        self.playlist.append(v)
        return Resp({"status": "STATUS_SUCCEEDED"})

    def browse(self, body):
        held = list(self.playlist)
        token = body.get("continuation")
        start = int(re.sub(r"\D", "", token) or 0) if token else 0
        page = held[start:start + self.page]
        shelf = {"contents": [{"musicResponsiveListItemRenderer":
                               {"playlistItemData": {"videoId": v}}} for v in page]}
        if start + len(page) < len(held):
            shelf["continuations"] = [{"nextContinuationData":
                                       {"continuation": "page:%d" % (start + len(page))}}]
        return Resp({"contents": {"musicPlaylistShelfRenderer": shelf}})


class Counts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp2yt-counts-")
        self._saved = {k: getattr(backend, k) for k in
                       ("LOGF", "CREATEDF", "SETTINGF", "_SEARCH_CACHE")}
        backend.LOGF = os.path.join(self.tmp, "server.log")
        backend.CREATEDF = os.path.join(self.tmp, "created.json")
        backend.SETTINGF = os.path.join(self.tmp, "settings.json")
        backend._SEARCH_CACHE = {}
        self._sleep = mock.patch("time.sleep", lambda *a, **k: None)
        self._sleep.start()
        self._wait = mock.patch.object(backend, "_standby_wait", lambda s: None, create=True)
        self._wait.start()

    def tearDown(self):
        self._wait.stop()
        self._sleep.stop()
        for k, v in self._saved.items():
            setattr(backend, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transfer(self, upstream, rows, one_to_one):
        with mock.patch("urllib.request.urlopen", upstream), \
                mock.patch.object(backend, "fetch_spotify_tracks", fake_fetch(rows)), \
                mock.patch.object(backend, "yt_search_batch", fake_batch), \
                mock.patch.object(backend, "one_to_one_setting", lambda: one_to_one):
            rid = backend.run_begin("", "")
            backend._transfer_thread(SPOTIFY_URL, COOKIE, "", "PRIVATE", rid)
        return backend.run_snapshot()

    # --- (a) the seven counts, from the owner's real run ---------------------
    def test_a_508_row_run_with_28_repeats_reports_the_seven_counts(self):
        upstream = Upstream()
        snap = self.transfer(upstream, tracks(480, REPEATS), one_to_one=False)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        self.assertEqual(snap["counts"], {
            "source": SOURCE, "unique": 480, "added": 480, "skipped": 0,
            "duplicate": REPEATS, "unverified": 0, "refused": 0})
        # Every source row is accounted for exactly once, and by the real numbers:
        # 508 rows read, 480 distinct ids intended, 28 copies the server refused.
        counts = snap["counts"]
        self.assertEqual(counts["source"], len(tracks(480, REPEATS)),
                         "source must be the rows the Spotify read returned")
        self.assertEqual(counts["unique"], 480, "unique must be the distinct videoIds")
        self.assertEqual(sum(counts[k] for k in
                             ("added", "skipped", "duplicate", "unverified", "refused")),
                         counts["source"])
        self.assertEqual(len(upstream.playlist), 480)
        # 50 ids rode the create inline; the other 458 copies were sent one at a time.
        self.assertEqual(len(upstream.actions), 458)

    def test_one_to_one_on_turns_the_same_28_copies_into_added(self):
        upstream = Upstream()
        snap = self.transfer(upstream, tracks(480, REPEATS), one_to_one=True)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        counts = snap["counts"]
        self.assertEqual(counts["duplicate"], 0, counts)
        self.assertEqual(counts["added"], SOURCE, counts)
        self.assertEqual(counts["unique"], 480, counts)
        # The denominator is what the source read, so 1:1 on reads 508 of 508 —
        # never 508 landed against a 480 denominator.
        self.assertTrue(snap["summary"].startswith(
            "landed 508 of 508 \u00b7 480 unique \u00b7 0 skipped \u00b7 0 duplicate \u00b7 "
            "0 unverified \u00b7 0 refused"), snap["summary"])
        self.assertEqual(len(upstream.playlist), SOURCE)
        self.assertEqual(sum(counts[k] for k in
                             ("added", "skipped", "duplicate", "unverified", "refused")),
                         counts["source"])

    # --- (b) 1:1 on: every add carries the skip, a second copy lands --------
    def test_one_to_one_on_sends_the_dedupe_skip_on_every_add_and_the_copy_lands(self):
        # 61 rows, 60 distinct: the 61st repeats videoId 1, which the create put
        # on the playlist inline - so that copy is only accepted because the
        # action asked the server to skip its own duplicate check.
        upstream = Upstream()
        snap = self.transfer(upstream, tracks(60, 1), one_to_one=True)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        self.assertEqual(len(upstream.actions), 11, "10 new ids + the repeated one")
        for act in upstream.actions:
            self.assertEqual(act.get("action"), "ACTION_ADD_VIDEO", act)
            self.assertEqual(act.get("dedupeOption"), SKIP, act)
        self.assertEqual(upstream.playlist.count(vid(1)), 2, "the second copy did not land")
        self.assertEqual(snap["counts"]["added"], 61)
        self.assertEqual(snap["counts"]["duplicate"], 0)
        self.assertEqual(snap["counts"]["unique"], 60)
        self.assertEqual(snap["counts"]["source"], 61)
        self.assertEqual(snap["state"], "done")

    def test_the_repair_pass_sends_with_the_same_flag(self):
        """The standby/repair pass is its own sender: it carries the 1:1 flag too."""
        upstream = Upstream()
        upstream.playlist = [vid(i) for i in range(1, 6)]   # 6 intended, 5 held
        with mock.patch("urllib.request.urlopen", upstream), \
                mock.patch.object(backend, "one_to_one_setting", lambda: True):
            out = backend._converge(PLID, COOKIE, [vid(i) for i in range(1, 7)],
                                    one_to_one=True)
        self.assertEqual(out["state"], "done", out)
        self.assertEqual([a.get("addedVideoId") for a in upstream.actions], [vid(6)])
        self.assertEqual(upstream.actions[-1].get("dedupeOption"), SKIP)
        self.assertEqual(len(upstream.playlist), 6)

    # --- (c) 1:1 off: no field, and the refusal classifies as duplicate -----
    def test_one_to_one_off_omits_the_field_and_the_refusal_is_a_duplicate(self):
        upstream = Upstream()
        snap = self.transfer(upstream, tracks(3, 1), one_to_one=False)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        self.assertTrue(upstream.actions, "no append was sent")
        for act in upstream.actions:
            self.assertNotIn("dedupeOption", act, act)
        # HTTP 200 + STATUS_FAILED + the add-toast is a duplicate, never a refusal.
        self.assertEqual(snap["counts"]["duplicate"], 1, snap["counts"])
        self.assertEqual(snap["counts"]["refused"], 0, snap["counts"])
        self.assertEqual(snap["counts"]["added"], 3, snap["counts"])
        self.assertEqual([r["state"] for r in snap["rows"]].count("duplicate"), 1)
        self.assertEqual([r["state"] for r in snap["rows"]].count("refused"), 0)
        self.assertIn("1 duplicate", snap["summary"])
        self.assertNotIn("1 refused", snap["summary"])

    # --- (c2) a duplicate needs its proof: multiplicity only corroborates -----
    def test_multiplicity_alone_never_makes_a_duplicate(self):
        """Two copies intended, one held. Nothing said about the second: absent.
        A plain STATUS_FAILED: refused. Only the documented refusal is duplicate."""
        ids = ["AAAAAAAAAAA", "AAAAAAAAAAA"]
        ledger = backend._reconcile(ids, ["AAAAAAAAAAA"], {1: ""}, True)
        self.assertEqual(ledger[1]["outcome"], "missing", ledger[1])
        ledger = backend._reconcile(ids, ["AAAAAAAAAAA"], {1: "STATUS_FAILED"}, True)
        self.assertEqual(ledger[1]["outcome"], "rejected", ledger[1])
        ledger = backend._reconcile(ids, ["AAAAAAAAAAA"], {1: backend.STATUS_DUPLICATE}, True)
        self.assertEqual(ledger[1]["outcome"], "duplicate", ledger[1])

    def test_a_plain_refusal_on_a_repeated_id_stays_refused(self):
        """The server said FAILED with no duplicate toast: the second copy was
        refused, not rejected as a duplicate, and the first pass's status is not
        thrown away when the run realigns the playlist."""
        upstream = Upstream()
        upstream.plain_refusals = True
        snap = self.transfer(upstream, tracks(3, 1), one_to_one=False)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        counts = snap["counts"]
        self.assertEqual(counts["refused"], 1, counts)
        self.assertEqual(counts["duplicate"], 0, counts)
        self.assertEqual(counts["added"], 3, counts)
        self.assertEqual([r["state"] for r in snap["rows"]].count("refused"), 1)
        self.assertIn("1 refused", snap["summary"])
        self.assertNotIn("1 duplicate", snap["summary"])

    # --- (e2) a write that fails is an error, not an acknowledgement --------
    def _serve(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), backend.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]

        def post(p, obj):
            req = urllib.request.Request(base + p, data=json.dumps(obj).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                return json.loads(urllib.request.urlopen(req, timeout=10).read()), 200
            except urllib.error.HTTPError as e:
                try:
                    return json.loads(e.read()), e.code
                finally:
                    e.close()

        return srv, post

    def test_a_settings_write_that_fails_is_an_error_not_an_acknowledgement(self):
        """If settings.json cannot be written the toggle did not change, so the
        endpoint must say so: a page that claims a value which will not survive a
        restart is the defect this pins."""
        backend.save_settings({"privacy": "UNLISTED"})
        broken = os.path.join(self.tmp, "settings-is-a-directory")
        os.makedirs(broken, exist_ok=True)
        backend.SETTINGF = broken
        before = backend.one_to_one_setting()
        srv, post = self._serve()
        try:
            body, code = post("/api/one-to-one", {"on": not before})
            self.assertEqual(code, 500, body)
            self.assertIs(body.get("ok"), False, body)
            self.assertEqual(body.get("oneToOne"), before,
                             "the reply acknowledged a value that was never saved")
            self.assertEqual(backend.one_to_one_setting(), before, "the toggle moved anyway")

            body, code = post("/api/settings", {"privacy": "PUBLIC"})
            self.assertEqual(code, 500, body)
            self.assertIs(body.get("ok"), False, body)
            self.assertNotEqual(body.get("privacy"), "PUBLIC",
                                "the reply acknowledged a privacy value that was never saved")
        finally:
            srv.shutdown()
            srv.server_close()
            backend.run_reset()

    # --- (d) the done line: one line, the shared shape ----------------------
    def test_the_done_line_is_one_line_in_the_shared_shape(self):
        upstream = Upstream()
        snap = self.transfer(upstream, tracks(480, REPEATS), one_to_one=False)

        line = snap["summary"]
        self.assertEqual(line.count("\n"), 0, line)
        self.assertTrue(line.startswith(
            "landed 480 of 508 \u00b7 480 unique \u00b7 0 skipped \u00b7 28 duplicate \u00b7 "
            "0 unverified \u00b7 0 refused"), line)
        # The gate and the page both lean on these two literals.
        self.assertIn("landed ", line)
        self.assertIn(" of ", line)
        # The proof the counts were reconciled against is still named on the line.
        self.assertIn("holds 480", line)

    def test_the_shape_holds_for_skips_guesses_and_a_topped_up_run(self):
        shape = ("landed 3 of 5 \u00b7 4 unique \u00b7 1 skipped \u00b7 0 duplicate "
                 "\u00b7 0 unverified \u00b7 0 refused")
        line = backend._done_summary(
            {"source": 5, "unique": 4, "added": 3, "skipped": 1, "duplicate": 0,
             "unverified": 0, "refused": 0}, 3)
        self.assertTrue(line.startswith(shape), line)
        self.assertEqual(line.count("\n"), 0, line)
        # A labelled guess is still named, never silent.
        self.assertIn("best guess 1", backend._done_summary(
            {"source": 3, "unique": 3, "added": 3, "skipped": 0, "duplicate": 0,
             "unverified": 0, "refused": 0}, 3, guessed=1))
        # A topped-up run says so, and keeps the shape.
        self.assertTrue(backend._done_summary(
            {"source": 5, "unique": 4, "added": 3, "skipped": 1, "duplicate": 0,
             "unverified": 0, "refused": 0}, 3, topped_up=2, sent=4).startswith(shape))
        self.assertIn("topped up 2 of 4", backend._done_summary(
            {"source": 5, "unique": 4, "added": 3, "skipped": 1, "duplicate": 0,
             "unverified": 0, "refused": 0}, 3, topped_up=2, sent=4))

    # --- (e) /api/run serves counts as-is, plus oneToOne --------------------
    def test_api_run_serves_counts_and_the_toggle_round_trips(self):
        counts = {"source": SOURCE, "unique": 480, "added": 480, "skipped": 0,
                  "duplicate": REPEATS, "unverified": 0, "refused": 0}
        backend.save_settings({"privacy": "UNLISTED"})
        backend.run_begin("", "")
        backend.run_set(phase="done", state="done", counts=dict(counts))
        srv = ThreadingHTTPServer(("127.0.0.1", 0), backend.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % srv.server_address[1]

            def get(p):
                return json.loads(urllib.request.urlopen(base + p, timeout=10).read())

            def post(p, obj):
                req = urllib.request.Request(base + p, data=json.dumps(obj).encode(),
                                             headers={"Content-Type": "application/json"})
                try:
                    return json.loads(urllib.request.urlopen(req, timeout=10).read()), 200
                except urllib.error.HTTPError as e:
                    try:
                        return json.loads(e.read()), e.code
                    finally:
                        e.close()

            run = get("/api/run?since=0")
            self.assertEqual(run["counts"], counts, "counts must be served as-is")
            self.assertIs(run["oneToOne"], True, "1:1 is on unless it was turned off")
            self.assertEqual(run["state"], "done")

            ok, code = post("/api/one-to-one", {"on": False})
            self.assertEqual(code, 200)
            self.assertEqual(ok["oneToOne"], False)
            self.assertIs(get("/api/run?since=0")["oneToOne"], False)
            with open(backend.SETTINGF, encoding="utf-8") as f:
                self.assertIs(json.load(f).get("one_to_one"), False, "toggle not persisted")
            kept = backend.load_settings()
            self.assertEqual(kept.get("privacy"), "UNLISTED", "the toggle erased the privacy latch")

            bad, code = post("/api/one-to-one", {"on": "yes"})
            self.assertEqual(code, 400, bad)
            self.assertIs(get("/api/run?since=0")["oneToOne"], False, "a bad body changed it")
        finally:
            srv.shutdown()
            srv.server_close()
            backend.run_reset()


if __name__ == "__main__":
    unittest.main(verbosity=2)
