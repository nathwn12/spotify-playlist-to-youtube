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
import time
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

    def __init__(self, page=PAGE, extra=(), throttle=0, paging_broken=False,
                 break_after=0, drop=(), blind_rows=False, empty_reads=0,
                 lose_reply=(), fail_read_after_loss=False):
        self.page = page
        self.playlist = list(extra)   # ids the playlist really holds, in order
        self.appends = []             # every id an ACTION_ADD_VIDEO asked for
        self.creates = 0
        self.throttle = throttle      # how many appends answer 429 before one lands
        self.throttled = 0
        self.paging_broken = paging_broken
        self.break_after = break_after  # let this many page reads through, then stall
        self.drop = set(drop)         # ids an append accepts but never stores
        self.blind_rows = blind_rows  # rows the parser cannot name (no videoId)
        self.empty_reads = empty_reads  # initial browses served as a stale empty shelf
        self.lose_reply = set(lose_reply)  # appends YTM applies, whose reply is lost
        self.fail_read_after_loss = fail_read_after_loss  # the read after one cannot complete
        self.deletes = []             # every playlist id a delete asked for
        self.reads = 0
        self.continuations = []

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", "") or str(req)
        try:
            body = json.loads((getattr(req, "data", b"") or b"{}").decode("utf-8"))
        except Exception:
            body = {}
        if "playlist/delete" in url:
            self.deletes.append(body.get("playlistId", ""))
            return Resp({})
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
        if v not in self.drop:
            self.playlist.append(v)
        if v in self.lose_reply:
            # Applied, and the reply was lost on the way back: the caller cannot
            # know whether the copy landed.
            self.lose_reply.discard(v)
            if self.fail_read_after_loss:
                self._fail_next_read = True
            raise urllib.error.URLError("reply lost")
        return Resp({"status": "STATUS_SUCCEEDED"})

    def browse(self, body):
        self.reads += 1
        if getattr(self, "_fail_next_read", False):
            self._fail_next_read = False
            raise urllib.error.URLError("browse failed after a lost reply")
        held = list(self.playlist)
        token = body.get("continuation")
        if not token and self.empty_reads:
            # A stale, token-complete reply that claims an empty playlist: InnerTube
            # serves this straight after a create for a playlist that does hold rows.
            self.empty_reads -= 1
            return Resp({"contents": {"musicPlaylistShelfRenderer": {"contents": []}}})
        if token:
            self.continuations.append(token)
            if self.paging_broken or (self.break_after and self.reads > self.break_after):
                raise urllib.error.URLError("continuation stalled")
            start = int(re.sub(r"\D", "", token) or 0)
        else:
            start = 0
        page = held[start:start + self.page]
        shelf = {"contents": [{"musicResponsiveListItemRenderer":
                               ({} if self.blind_rows else {"playlistItemData": {"videoId": v}})}
                              for v in page]}
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
        self.upstream = None
        self._patches = [mock.patch("time.sleep", lambda *a, **k: None),
                         mock.patch.object(backend, "_standby_wait", self._wait,
                                           create=True)]
        for p in self._patches:
            p.start()

    def _wait(self, seconds):
        """What the transfer thread does while standing by: nothing but wait. The
        snapshot taken here is exactly what the page reads from /api/run."""
        self.waits.append({"delay": seconds, "snap": backend.run_snapshot(),
                           "appends": list(self.upstream.appends) if self.upstream else []})

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for k, v in self._saved.items():
            setattr(backend, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transfer(self, upstream, tracks):
        self.upstream = upstream
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
        self.assertIn("1 duplicate", snap["summary"])
        self.assertNotIn("1 refused", snap["summary"])
        self.assertEqual(snap["duplicate"], 1)
        self.assertEqual(snap["refused"], 0)
        # A duplicate is unaddable, not unfinished: the record must not ask for
        # another resume of a playlist that is exactly as complete as it can be.
        self.assertIs(self.created()["SPOTREPAIR"]["complete"], True)

    # --- 2: a 429 stands by, appends nothing, then realigns -----------------
    def test_a_throttled_append_stands_by_and_resumes_with_exactly_the_missing_copies(self):
        tracks = tracks_for(60)                   # 50 inline at create, 10 appended
        upstream = Upstream(throttle=2)           # both attempts on one append answer 429
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        self.assertEqual(snap["state"], "done")
        # The run stood by once, with a clock and a reason, and appended nothing
        # while it could not see whether the throttled copy had landed.
        self.assertTrue(self.waits, "the run never stood by")
        stood = self.waits[0]
        self.assertEqual(stood["snap"]["state"], "standby")
        self.assertEqual(stood["snap"]["attempt"], 1)
        self.assertGreater(stood["snap"]["next_attempt_at"], 0)
        self.assertEqual(stood["snap"]["reason"], "append failed on the wire")
        self.assertEqual(stood["appends"], [])
        # Resumed: exactly the verified-absent copies, once each.
        self.assertEqual(upstream.appends, [vid(i) for i in range(51, 61)])
        self.assertEqual(len(upstream.playlist), 60)
        self.assertEqual(len(upstream.playlist), len(set(upstream.playlist)))
        self.assertEqual(snap["landed"], 60)
        self.assertEqual(snap["playlist_count"], 60)
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["state"], "done")
        self.assertIs(rec["complete"], True)

    # --- 3: an incomplete read-back appends nothing -------------------------
    def test_an_incomplete_read_back_stands_by_and_appends_nothing(self):
        tracks = tracks_for(40)                   # all 40 land inline at create
        extras = ["X%010d" % i for i in range(70)]
        upstream = Upstream(extra=extras, paging_broken=True)   # 110 entries, tail unreadable
        snap = self.transfer(upstream, tracks)

        self.assertEqual(len(self.waits), 1, "one wait, for a repair that does not exist")
        stood = self.waits[0]
        self.assertEqual(stood["snap"]["state"], "standby")
        self.assertEqual(stood["snap"]["reason"], "read incomplete")
        self.assertEqual(stood["snap"]["attempt"], 1)
        self.assertGreater(stood["snap"]["next_attempt_at"], 0)
        # Appending off a read that never reached the end is what double-adds.
        self.assertEqual(stood["appends"], [])
        self.assertEqual(upstream.appends, [])
        # The read never completed, so the run reports what it proved and nothing
        # more: 30 of the 40 are in the page it read, ten are unverified.
        self.assertEqual(snap["unverified"], 10)
        self.assertEqual(snap["refused"], 0)
        self.assertEqual(snap["missing"], 0)
        self.assertIn("10 unverified", snap["summary"])
        self.assertEqual(len(upstream.playlist), 110)

    # --- 3b: a pending repair is never attempted off an incomplete read ------
    def test_an_incomplete_read_back_holds_back_a_pending_repair(self):
        tracks = tracks_for(60)
        extras = ["X%010d" % i for i in range(70)]      # 130 entries: two pages
        # The playlist accepts vid(60) and never stores it, and paging stalls once
        # the create pass is done - so a repair is pending exactly when the read
        # stops being complete.
        upstream = Upstream(extra=extras, break_after=6, drop={vid(60)})
        snap = self.transfer(upstream, tracks)

        self.assertTrue(self.waits, "the run never stood by")
        stood = self.waits[0]
        self.assertEqual(stood["snap"]["state"], "standby")
        self.assertEqual(stood["snap"]["reason"], "read incomplete")
        held_at_standby = len(stood["appends"])
        self.assertEqual(len(upstream.appends), held_at_standby,
                         "an append was sent while the read was incomplete")
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(snap["residual"], [vid(60)])
        self.assertEqual(len(self.waits), backend.RUN_RETRY_MAX)
        self.assertEqual(upstream.creates, 1)

    # --- 3c: an append accepted but never reflected is not an endless loop --
    def test_a_dropped_append_terminates_instead_of_spinning(self):
        """60 intended; the server accepts the append of #60 and never stores it.

        The read-back is complete every pass, so nothing here is transport-
        retryable: the verified-present set is identical pass after pass, which
        is progress zero. The loop must stop on that proof - one standby naming
        the stall, a second no-progress pass gives up carrying the residual -
        never spin, and never claim the dropped copy landed.

        Implemented as `gaveup` after the second no-progress pass: a repair the
        server accepted and dropped will be dropped again, so waiting out the
        retry budget would only spend six minutes on the same reply. The first
        no-progress pass still goes through the existing standby machinery, so a
        genuinely slow read-back gets its wait.
        """
        tracks = tracks_for(60)
        upstream = Upstream(drop={vid(60)})      # accepted, never stored
        started = time.monotonic()
        snap = self.transfer(upstream, tracks)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0, "the bounded loop took %.1fs" % elapsed)
        self.assertIn(snap["state"], ("standby", "gaveup"), snap)
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(snap["residual"], [vid(60)])
        self.assertNotEqual(snap["rows"][-1]["state"], "landed",
                            "a copy no complete read-back proved must never land")
        # Never spin: one send by the create pass, at most one by the repair pass.
        self.assertLessEqual(upstream.appends.count(vid(60)), 2, upstream.appends)

    # --- 4: the budget is bounded and the give-up names the residue ---------
    def test_an_exhausted_budget_gives_up_with_the_residual_list(self):
        tracks = tracks_for(60)
        upstream = Upstream(throttle=99)          # every append answers 429
        snap = self.transfer(upstream, tracks)

        self.assertEqual(len(self.waits), backend.RUN_RETRY_MAX)
        self.assertEqual([w["snap"]["attempt"] for w in self.waits], [1, 2, 3, 4, 5, 6])
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(snap["residual"], [vid(i) for i in range(51, 61)])
        # Quiet and truthful: it names what is still absent and claims no more.
        self.assertEqual(snap["refused"], 0)
        self.assertNotIn("refused 10", snap["summary"])
        self.assertIn("10", snap["summary"])
        # The wait lengthens: 30s, 1m, 2m, 5m, 10m, 20m, +/-20% jitter, capped.
        delays = [w["delay"] for w in self.waits]
        self.assertLess(delays[0], delays[-1])
        for seconds, base in zip(delays, (30, 60, 120, 300, 600, 1200), strict=True):
            self.assertGreaterEqual(seconds, base * 0.8 - 0.001)
            self.assertLessEqual(seconds, min(1200, base * 1.2) + 0.001)
        # No second playlist: the run repaired the one it had.
        self.assertEqual(upstream.creates, 1)
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["state"], "gaveup")
        self.assertEqual(rec["attempt"], backend.RUN_RETRY_MAX)
        self.assertEqual(rec["pending"], [vid(i) for i in range(51, 61)])
        self.assertIs(rec["complete"], False)

    # --- 4b: a resume continues the same run and never creates a playlist ---
    def test_a_persisted_run_resumes_in_place_with_its_own_attempt_count(self):
        tracks = tracks_for(60)
        upstream = Upstream()                        # holds everything it is given
        upstream.playlist = [vid(i) for i in range(1, 61)]
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"yt_id": PLID, "url": "https://example.invalid/p",
                                      "title": "Repair Test", "expected": 60,
                                      "complete": False, "state": "standby",
                                      "attempt": 3, "pending": []}}, f)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.creates, 0, "a resume must never create a playlist")
        self.assertEqual(snap["state"], "done")
        self.assertEqual(snap["attempt"], 3, "the resumed run kept its own attempt count")
        self.assertEqual(snap["landed"], 60)

    # --- 5: an empty parse is not an empty playlist -------------------------
    def test_a_read_that_parsed_no_ids_never_deletes_the_playlist(self):
        """Rows the parser cannot name, then a continuation that stalls: the read
        parses zero ids and proves nothing, so it must not delete the playlist."""
        tracks = tracks_for(40)
        extras = ["X%010d" % i for i in range(70)]
        upstream = Upstream(extra=extras, blind_rows=True, paging_broken=True)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.deletes, [], "an unread playlist was deleted")
        self.assertNotIn("husk deleted", snap.get("error", ""))
        self.assertIn(snap["phase"], ("failed", "done"), snap)

    def test_a_single_empty_complete_read_is_not_proof_the_playlist_is_empty(self):
        """The browse served after a create can be token-complete and hold nothing
        for a playlist that does hold rows: one such read must never delete it."""
        tracks = tracks_for(40)
        upstream = Upstream(empty_reads=10)     # ten stale empty replies, then the truth
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.deletes, [], "a stale empty read deleted the playlist")
        self.assertEqual(len(upstream.playlist), len(tracks))
        self.assertNotIn("husk deleted", snap.get("error", ""))

    # --- 6: an unknown outcome is read before it is resent ------------------
    def test_an_ambiguous_add_is_not_resent_without_a_complete_read(self):
        """The add was applied and its reply was lost. A resend may only follow a
        COMPLETE read; when the read cannot prove the playlist, nothing is sent."""
        upstream = Upstream(lose_reply={vid(1)}, paging_broken=True)
        upstream.playlist = ["X%010d" % i for i in range(150)]   # more than one page
        with mock.patch("urllib.request.urlopen", upstream):
            st = backend.yt_add_one(PLID, vid(1), COOKIE, one_to_one=True)

        self.assertTrue(backend.is_transport_status(st), st)
        self.assertEqual(upstream.appends.count(vid(1)), 1,
                         "an ambiguous add was resent without a complete read")

    def test_a_lost_reply_on_an_append_is_never_sent_twice(self):
        """The appended copy's outcome was unknown: the run reads before it sends
        again, and the copy ends up on the playlist exactly once."""
        tracks = tracks_for(60)
        upstream = Upstream(lose_reply={vid(55)}, fail_read_after_loss=True)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.appends.count(vid(55)), 1,
                         "an ambiguous add was resent without a complete read")
        self.assertEqual(upstream.playlist.count(vid(55)), 1)
        self.assertEqual(snap["phase"], "done", snap.get("error"))
        self.assertEqual(snap["landed"], 60)

    def test_a_lost_reply_in_the_repair_pass_is_not_resent_either(self):
        """The repair sender has the same rule: read first, resend only off proof."""
        upstream = Upstream(lose_reply={vid(6)}, fail_read_after_loss=True)
        self.upstream = upstream
        upstream.playlist = [vid(i) for i in range(1, 6)]
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, [vid(i) for i in range(1, 7)])

        self.assertEqual(upstream.appends.count(vid(6)), 1,
                         "the repair resent an add whose outcome was unknown")
        self.assertTrue(self.waits, "the repair never stood by")
        self.assertEqual(self.waits[0]["appends"].count(vid(6)), 1,
                         "an append was sent while the outcome was unknown")
        self.assertEqual(out["state"], "done", out)

    # --- 5: Retry now only has a wait to cut short --------------------------
    def test_retry_now_only_cuts_short_a_standby(self):
        backend.run_set(state="done", next_attempt_at=0.0)
        self.assertFalse(backend.run_retry_now())
        backend.run_set(state="standby", next_attempt_at=time.time() + 600)
        self.assertTrue(backend.run_retry_now())
        self.assertLess(backend.RUN["next_attempt_at"], time.time() + 600)
        self.assertEqual(backend.RUN["reason"], "retry now")
        backend.RUN_WAKE.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
