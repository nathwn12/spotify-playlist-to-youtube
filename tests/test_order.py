#!/usr/bin/env python3
import json
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import backend  # noqa: E402

PLID = "PLorderTest0001"
COOKIE = "SID=test-only; HSID=test-only; SAPISID=test-only-value"
SKIP = "DEDUPE_OPTION_SKIP"
PAGE = 100


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

    def __init__(self, held, refuse_extra=False, script=()):
        self.playlist = list(held)
        self.appends = []
        self.actions = []
        self.refuse_extra = refuse_extra
        self.script = [list(s) for s in script]   # complete replies, one per first-page read
        self.reads = 0

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", "") or str(req)
        try:
            body = json.loads((getattr(req, "data", b"") or b"{}").decode("utf-8"))
        except Exception:
            body = {}
        if "edit_playlist" in url:
            return self.edit(body)
        if "youtubei/v1/browse" in url:
            return self.browse(body)
        raise AssertionError("no fake for " + url)

    def edit(self, body):
        act = (body.get("actions") or [{}])[0]
        self.actions.append(act)
        v = act.get("addedVideoId") or ""
        if v in self.playlist:
            if self.refuse_extra or act.get("dedupeOption") != SKIP:
                return Resp({"status": "STATUS_FAILED"})
        self.playlist.append(v)
        self.appends.append(v)
        return Resp({"status": "STATUS_SUCCEEDED"})

    def browse(self, body):
        token = body.get("continuation")
        start = int(re.sub(r"\D", "", token) or 0) if token else 0
        if not token:
            self.reads += 1
        held = list(self.playlist)
        if not token and self.script:
            held = list(self.script.pop(0))
        page = held[start:start + PAGE]
        shelf = {"contents": [{"musicResponsiveListItemRenderer":
                               {"playlistItemData": {"videoId": v}}} for v in page]}
        if start + len(page) < len(held):
            shelf["continuations"] = [{"nextContinuationData":
                                       {"continuation": "page:%d" % (start + len(page))}}]
        return Resp({"contents": {"musicPlaylistShelfRenderer": shelf}})


A, B, C, D = "AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC", "DDDDDDDDDDD"


class Order(unittest.TestCase):
    def setUp(self):
        self.waits = []
        self._seams = [mock.patch.object(backend, "_pause", lambda s: None),
                       mock.patch.object(backend, "_standby_wait", self._wait),
                       mock.patch("time.sleep", lambda *a, **k: None)]
        for p in self._seams:
            p.start()
        self._saved = {k: getattr(backend, k) for k in ("CREATEDF", "LOGF")}
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="sp2yt-order-")
        backend.CREATEDF = os.path.join(self.tmp, "created.json")
        backend.LOGF = os.path.join(self.tmp, "server.log")

    def _wait(self, seconds):
        self.waits.append(seconds)

    def tearDown(self):
        for p in self._seams:
            p.stop()
        for k, v in self._saved.items():
            setattr(backend, k, v)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def converge(self, upstream, plan, spotify_id=""):
        with mock.patch("urllib.request.urlopen", upstream):
            return backend._converge(PLID, COOKIE, plan, spotify_id=spotify_id)

    def test_order_preserved_is_a_subsequence_test(self):
        self.assertTrue(backend._order_preserved([A, B, A, C], [A, B, A, C]))
        self.assertTrue(backend._order_preserved([A, B, A, C], ["X", A, B, A, C]))
        self.assertTrue(backend._order_preserved([], []))
        self.assertFalse(backend._order_preserved([A, B, A, C], [A, C, B, A]))
        self.assertFalse(backend._order_preserved([A, B, A, C], [A, B, C]))
        self.assertFalse(backend._order_preserved([A, B, A, C], [A, A, B, C]),
                         "the two A copies may not swap places")

    def test_a_missing_repeat_copies_the_copy_the_plan_asked_for(self):
        plan = [A, B, A, C]
        upstream = Upstream([A, B, C])          # the second A was lost
        out = self.converge(upstream, plan)
        self.assertEqual(out["state"], "done", out)
        self.assertEqual(upstream.appends, [A], "the missing second copy was not re-sent")
        self.assertEqual(upstream.playlist, [A, B, C, A])
        self.assertTrue(out["complete"], out)

    def test_the_repair_set_is_the_missing_copies_in_plan_order(self):
        plan = [A, B, A, C]
        upstream = Upstream([A, C])             # B and the second A are absent
        out = self.converge(upstream, plan)
        self.assertEqual(out["state"], "done", out)
        self.assertEqual(upstream.appends, [B, A], "the repair did not follow plan order")
        self.assertEqual(upstream.playlist, [A, C, B, A])

    def test_a_repair_of_a_repeated_copy_is_proved_by_the_copies_it_sent(self):
        upstream = Upstream([B], script=[[B], [B, A]])   # read 2 is a stale page
        out = self.converge(upstream, [A, A, B])
        self.assertEqual(out["state"], "done", out)
        self.assertEqual(upstream.appends, [A, A],
                         "a copy was re-sent off a stale read")
        self.assertEqual(upstream.playlist, [B, A, A], upstream.playlist)
        self.assertEqual(sorted(out["statuses"]), [0, 1],
                         "the pass reported %r for two copies" % (out["statuses"],))

    def test_a_repeat_the_server_will_not_accept_is_not_a_finished_run(self):
        plan = [A, B, A, C]
        upstream = Upstream([A, B, C], refuse_extra=True)
        out = self.converge(upstream, plan, spotify_id="SPORDER")
        self.assertEqual(out["state"], "gaveup", out)
        self.assertIn(A, out["residual"], out)
        rec = backend.load_created().get("SPORDER") or {}
        self.assertIs(rec.get("complete"), False,
                      "a short playlist persisted complete: %r" % (rec,))

    def test_a_repaired_copy_at_the_end_is_reported_as_a_different_order(self):
        plan = [A, B, A, C]
        upstream = Upstream([A, C])
        out = self.converge(upstream, plan)
        self.assertEqual(upstream.playlist, [A, C, B, A])
        self.assertFalse(backend._order_preserved(plan, out["entries"] or []))
        line = backend._done_summary(
            {"source": 4, "unique": 3, "added": 4, "skipped": 0, "duplicate": 0,
             "unverified": 0, "refused": 0}, 4, order_differs=True)
        self.assertIn("order", line.lower(), line)
        self.assertIn("differs", line.lower(), line)

    def test_an_untouched_playlist_keeps_its_order_and_says_nothing(self):
        plan = [A, B, A, C, D]
        upstream = Upstream([A, B, A, C, D])
        out = self.converge(upstream, plan)
        self.assertEqual(out["state"], "done", out)
        self.assertEqual(upstream.appends, [])
        self.assertTrue(backend._order_preserved(plan, out["entries"] or []))
        line = backend._done_summary(
            {"source": 5, "unique": 4, "added": 5, "skipped": 0, "duplicate": 0,
             "unverified": 0, "refused": 0}, 5)
        self.assertNotIn("order", line.lower(), line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
