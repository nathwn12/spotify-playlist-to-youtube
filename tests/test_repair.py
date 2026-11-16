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
import contextlib
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


class Crash(BaseException):
    """A process that ends mid-run: BaseException on purpose, so no `except Exception`
    in the transfer turns a crash into a skipped copy."""


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
                 lose_reply=(), fail_read_after_loss=False, browse_broken=False,
                 lose_before_apply=(), add_script=(), accept_repeats=False,
                 create_keeps_repeats=False, relabel=None, fatal=0, crash_on_read=0):
        self.relabel = dict(relabel or {})  # ids the server stores under another id
        self.fatal = fatal            # every append answers this HTTP code, unapplied
        self.crash_on_read = crash_on_read  # this read burst dies with the process
        self.page = page
        self.playlist = list(extra)   # ids the playlist really holds, in order
        self.appends = []             # every id an ACTION_ADD_VIDEO asked for
        self.sends = []               # every add attempt, failures included
        self.creates = 0
        self.create_keeps_repeats = create_keeps_repeats  # a create that kept both copies
        self.throttle = throttle      # how many appends answer 429 before one lands
        self.throttled = 0
        self.paging_broken = paging_broken
        self.break_after = break_after  # let this many page reads through, then stall
        self.drop = set(drop)         # ids an append accepts but never stores
        self.blind_rows = blind_rows  # rows the parser cannot name (no videoId)
        self.empty_reads = empty_reads  # initial browses served as a stale empty shelf
        self.lose_reply = set(lose_reply)  # appends YTM applies, whose reply is lost
        self.lose_before_apply = set(lose_before_apply)  # the reply is lost, nothing applied
        self.add_script = list(add_script)  # per-append behaviour: failed/lost, consumed in order
        self.accept_repeats = accept_repeats  # a server that honours the skip for a held id
        self.fail_read_after_loss = fail_read_after_loss  # the read after one cannot complete
        self.browse_broken = browse_broken  # every read fails on the wire
        self.deletes = []             # every playlist id a delete asked for
        self.actions = []             # every ACTION_ADD_VIDEO entry an edit carried
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
        # A real create keeps one copy of each id: the playlist cannot hold two. The
        # `create_keeps_repeats` fake models the other possibility - the server kept both
        # copies the create carried - which a read can then prove by multiplicity.
        ids = list(body.get("videoIds") or [])
        for v in (ids if self.create_keeps_repeats else dict.fromkeys(ids)):
            stored = self.relabel.get(v, v)
            if self.create_keeps_repeats or stored not in self.playlist:
                self.playlist.append(stored)
        return Resp({"playlistId": PLID})

    def edit(self, body, url=""):
        v = ""
        for act in body.get("actions") or []:
            v = act.get("addedVideoId") or ""
        self.sends.append(v)          # every attempt, including the ones that failed
        if self.fatal:
            # A verdict on the CALL: the cookie is rejected / the playlist is gone.
            # Nothing was applied, and the same request will be refused again.
            raise urllib.error.HTTPError(url, self.fatal, "refused", {}, None)
        if self.throttled < self.throttle:
            self.throttled += 1
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        step = self.add_script.pop(0) if self.add_script else ""
        self.appends.append(v)
        if step == "lost" or v in self.lose_before_apply:
            # The reply never arrived and the request was never applied: the caller
            # cannot know whether the copy is on the playlist.
            self.lose_before_apply.discard(v)
            raise urllib.error.URLError("reply lost before it was applied")
        self.actions.append(dict((body.get("actions") or [{}])[0]))
        if step == "failed":
            return Resp({"status": "STATUS_FAILED"})
        # `relabel` is the shape a live run met: the append is accepted, one row is
        # added, and the row the read serves carries a videoId the plan never asked
        # for - so the playlist grows by every write and no read can name the copy.
        stored = self.relabel.get(v, v)
        if stored in self.playlist and not (self.accept_repeats
                                           and self.actions[-1].get("dedupeOption") == "DEDUPE_OPTION_SKIP"):
            return Resp(DUP_REPLY)
        if v not in self.drop:
            self.playlist.append(stored)
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
        if self.crash_on_read and self.reads == self.crash_on_read:
            # The process dies mid-run: not an exception a caller catches, exactly what
            # a closed window or a killed process leaves behind.
            raise Crash("process died between the add and its proof")
        if self.browse_broken:
            raise urllib.error.URLError("browse failed")
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

    def transfer(self, upstream, tracks, batch=None):
        self.upstream = upstream
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("urllib.request.urlopen", upstream))
            stack.enter_context(mock.patch.object(backend, "fetch_spotify_tracks", fake_fetch(tracks)))
            stack.enter_context(mock.patch.object(backend, "yt_search_batch", batch or fake_batch))
            rid = backend.run_begin("", "")
            backend._transfer_thread(SPOTIFY_URL, COOKIE, "", "PRIVATE", rid)
        return backend.run_snapshot()

    def created(self):
        try:
            with open(backend.CREATEDF, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    # --- 1a: a failure on the wire is never success, and never a retryable write ---
    def test_a_429_from_an_append_is_an_unknown_write_not_success(self):
        """The write endpoint is unofficial, so a 429 is not proof the request was
        refused unapplied. It is an unknown outcome like any other, and nothing may
        re-send it."""
        def throttled(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

        with mock.patch("urllib.request.urlopen", throttled):
            st = backend.yt_add_one(PLID, vid(1), COOKIE)
        self.assertTrue(str(st).startswith(backend.STATUS_UNSURE), st)
        self.assertNotIn("SUCCEEDED", str(st).upper())
        self.assertTrue(backend.is_unsure_status(st))
        self.assertFalse(hasattr(backend, "is_retry_status"),
                         "a predicate that authorises re-sending an append is back")

    # --- 1c: what "complete" may say ---------------------------------------
    def test_a_refused_copy_keeps_the_record_incomplete(self):
        """A duplicate is a source row the playlist does not hold, so the create's own
        write of `complete` may not count it. The record a later run starts from must
        say a copy is missing, not that every copy is there."""
        upstream = Upstream()                       # refuses a second copy of an id
        with mock.patch("urllib.request.urlopen", upstream):
            backend._create_new(COOKIE, "Repair Test", "", [vid(i) for i in
                                                           (1, 2, 3, 4, 3)], "PRIVATE", "SPOTREPAIR")
        rec = self.created()["SPOTREPAIR"]
        self.assertIs(rec["complete"], False,
                      "the create's record counts a refused copy as complete")

    def test_a_partial_read_keeps_the_record_incomplete(self):
        """`complete` needs a read that reached the end of the playlist. A page that
        happens to hold every copy is not the whole playlist."""
        class PlanFirst(Upstream):
            """The plan's rows are served first, with unrelated rows behind them."""

            def create(self, body):
                self.playlist[0:0] = [v for v in dict.fromkeys(body.get("videoIds") or [])]
                return Resp({"playlistId": PLID})

        upstream = PlanFirst(extra=["X%010d" % i for i in range(60)], paging_broken=True)
        with mock.patch("urllib.request.urlopen", upstream):
            backend._create_new(COOKIE, "Repair Test", "", [vid(i) for i in range(1, 51)],
                                "PRIVATE", "SPOTREPAIR")
        rec = self.created()["SPOTREPAIR"]
        self.assertIs(rec["complete"], False,
                      "a partial read of a playlist that held every copy read as complete")

    # --- 1b: a second copy inside the inline create is never re-sent --------
    def test_a_repeat_inside_the_inline_create_is_never_re_sent(self):
        """The create carried one videoId twice. The server may have kept one copy or
        both, and a read cannot say which - so a stale page showing one copy must not
        authorise the second: it is frozen for the run and named, never sent again.

        (A copy the run actually sends gets the server's own verdict; that path is
        pinned in test_counts, with the repeat past the inline batch.)"""
        tracks = tracks_for(4, repeat_of=3)      # 5 rows, rows 3 and 5 are one videoId
        upstream = Upstream()
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        rows = snap["rows"]
        states = [r.get("state") for r in rows]
        self.assertEqual(states.count("landed"), 4, states)
        self.assertEqual([r for r in rows if r.get("unverified")], [rows[4]],
                         "the unattributable copy is not filed as unknown")
        self.assertEqual(states.count("duplicate"), 0, states)
        self.assertEqual(states.count("refused"), 0, states)
        # The repeated videoId is on the playlist once, and the second copy was never
        # sent: no read could attribute it.
        self.assertEqual(upstream.playlist.count(vid(3)), 1)
        self.assertEqual(upstream.appends.count(vid(3)), 0,
                         "a copy the create carried twice was sent again")
        self.assertEqual(snap["counts"]["unverified"], 1, snap["counts"])
        self.assertIn("unverified", snap["summary"])
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(snap["residual"], [vid(3)])
        self.assertIs(self.created()["SPOTREPAIR"]["complete"], False)

    def test_a_stale_read_cannot_resend_an_inline_repeat(self):
        """The reported shape: plan `[A,A]`, the create stored one, every read is a page
        from before the create. The old floor counted distinct ids (1), so a stale one-A
        page passed it and the second copy was appended - a playlist with two A's for a
        plan that asked two, or three if the create had kept both. The copy is frozen."""
        a = vid(1)
        upstream = Upstream(accept_repeats=True)
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, [a, a], refused=[], unsure=[0, 1])
        self.assertEqual(out["state"], "gaveup", out)
        self.assertEqual(upstream.sends, [],
                         "a copy the create carried twice was re-sent")
        self.assertEqual(out["residual"], [a, a])

    # --- 2: a throttled append is an unknown write: named, never re-sent ----
    def test_a_throttled_append_is_named_unverified_and_never_re_sent(self):
        """A 429 is not proof the write was refused unapplied (the endpoint is
        unofficial), so the copy joins the unknown set: it is never sent again in this
        run, it is reported unverified, and the run says so. The copies after it that
        the server does accept still land - its verified writes are preserved."""
        tracks = tracks_for(60)                   # 50 inline at create, 10 appended
        upstream = Upstream(throttle=2)           # the first two appends answer 429
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(self.waits, [], "no append is waited out: a wait is not a proof")
        # The two unknown copies are named, and neither is on the playlist twice.
        self.assertEqual(snap["counts"]["unverified"], 2, snap["counts"])
        self.assertEqual(snap["counts"]["added"], 58, snap["counts"])
        self.assertEqual(snap["residual"], [vid(51), vid(52)])
        self.assertEqual(len(upstream.sends), len(set(upstream.sends)),
                         "a copy was sent twice")
        # Every copy the run proved is on the playlist, once, and the two unknown
        # ones are not claimed as landed.
        self.assertEqual(upstream.playlist,
                         [vid(i) for i in list(range(1, 51)) + list(range(53, 61))])
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["state"], "gaveup")
        self.assertIs(rec["complete"], False)
        # What the run knows is on disk: the unknown copies and the accepted floor.
        self.assertEqual(rec["unsure"], [50, 51])
        self.assertEqual(rec["expected_min"], 58)

    # --- 3: an incomplete read-back appends nothing -------------------------
    def test_an_incomplete_read_back_stands_by_and_appends_nothing(self):
        tracks = tracks_for(40)                   # all 40 land inline at create
        extras = ["X%010d" % i for i in range(70)]
        upstream = Upstream(extra=extras, paging_broken=True)   # 110 entries, tail unreadable
        snap = self.transfer(upstream, tracks)

        self.assertTrue(self.waits, "the run never stood by")
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
        # A run that never completed a read cannot call itself done: it spends the
        # budget it has and ends in a terminal state that names the shortfall - the
        # ten copies the read never reached, by name, not an empty residual.
        self.assertEqual(len(self.waits), backend.RUN_RETRY_MAX)
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(snap["residual"], [vid(i) for i in range(31, 41)])

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

    # --- 3b: an accepted copy no read can name is unknown, never re-sent ----
    def test_a_copy_the_read_never_shows_is_named_and_never_re_sent(self):
        """The emergency run's shape, offline.

        The server ACCEPTS the append and the playlist grows by one row, but the row
        the read serves carries a videoId the plan never asked for (live: 100 copies
        a cycle accepted, the read growing by exactly the accepted count each time,
        and no intended copy ever resolved). Every read is complete and its length
        matches the floor, so nothing here is a stale read.

        That copy is not absent - the playlist grew by its write - and nothing here
        can prove it stayed out, so it is unknown: named, frozen by id, and never
        re-sent. Re-sending it is what turned one live run into five cycles of the
        same copies: the playlist grew by 400 rows the app could not attribute.
        """
        tracks = tracks_for(60)
        hidden = vid(60)
        # Accepted, one row added, and the row is named something the plan never
        # asked for - the shape the live run met, five cycles running.
        upstream = Upstream(relabel={hidden: "Z0000000060"}, accept_repeats=True)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["state"], "gaveup", snap.get("reason"))
        self.assertIn(hidden, snap["residual"])
        self.assertEqual(upstream.sends.count(hidden), 1,
                         "a copy an already-accepted write covered was sent again: %r"
                         % upstream.sends)
        # One write per source copy, and not one more: the first pass accepted this
        # copy, so the first repair read is the evidence that freezes it - a repair
        # that cannot see that outcome sends it a second time.
        self.assertEqual(sorted(upstream.sends), sorted(set(upstream.sends)),
                         "a copy was written twice: %r" % upstream.sends)
        self.assertEqual(len(upstream.sends),
                         len(tracks) - backend.CREATE_INLINE_MAX,
                         "the run sent more appends than the plan has copies left: %r"
                         % upstream.sends)
        # One copy of every intended id: a second one is a row the run added and could
        # never attribute.
        written = [v for v in upstream.playlist if v.startswith("R")]
        self.assertEqual(sorted(written), sorted(set(written)),
                         "a copy was written twice: %r" % upstream.playlist)
        # Unknown, not missing: the write may have landed under a name this app
        # cannot read, and the row says a person must check it.
        row = [r for r in snap["rows"] if r.get("videoId") == hidden][0]
        self.assertTrue(row.get("unsure"), row)
        self.assertTrue(row.get("unverified"), row)
        self.assertIn("check the playlist", row.get("status", ""))
        rec = self.created()["SPOTREPAIR"]
        self.assertIs(rec["complete"], False)
        self.assertEqual(rec.get("unsure_ids"), [hidden], rec.get("unsure_ids"))
        self.assertEqual(rec.get("unsure_need"), {hidden: 1})

    def test_a_resume_after_an_unnameable_write_sends_nothing(self):
        """The page offers "press Ctrl+Enter to resume the same playlist". A resume
        may only be safe if the record names every copy it could not attribute: a
        copy whose write was accepted and never seen must not come back as a copy
        that was never sent, or the resume re-adds every one of them."""
        tracks = tracks_for(60)
        hidden = vid(60)
        upstream = Upstream(relabel={hidden: "Z0000000060"}, accept_repeats=True)
        self.transfer(upstream, tracks)
        sends = len(upstream.sends)

        self.transfer(upstream, tracks)     # Ctrl+Enter: resume, never a second create
        self.assertEqual(len(upstream.sends), sends,
                         "the resume re-sent copies whose writes it could not see")
        self.assertEqual(upstream.creates, 1)

    # --- 3d: a record from a version that cannot prove its writes fails closed --
    def _legacy_record(self, n=12, held=6):
        """The shape version 0.5.0 wrote (the owner's own record looked like this).

        complete:false, the floor it had proved, a stale half slice still queued, an
        EMPTY unknown set - and no record_version, because that version cleared its
        queue before the playlist read proved the writes. Every copy whose write it
        accepted and could not attribute is therefore invisible here: the record cannot
        tell "absent and never sent" from "on the playlist under a name a read does not
        serve". This fixture is deliberately written without the stamp."""
        plan = [vid(i) for i in range(1, n + 1)]
        queued = list(range(held, min(held + 3, n)))
        return {"yt_id": PLID, "url": "https://music.youtube.com/playlist?list=" + PLID,
                "title": "Repair Test", "expected": n, "complete": False,
                "state": "gaveup", "attempt": 0, "next_run_at": 0.0,
                "expected_min": held,
                "unsure": [], "unsure_ids": [], "unsure_need": {},
                "inflight": queued,
                "inflight_need": {plan[i]: 1 for i in queued},
                "video_ids": plan, "plan_slots": plan, "plan_guesses": {},
                "pending": plan[held:]}

    def test_a_legacy_record_refuses_to_resume_instead_of_re_appending(self):
        """The owner's real record: 0.5.0 wrote it, so it cannot say which of its copies
        are already on the playlist. A resume that read the copies it cannot see as
        copies it never sent would re-add every one of them - the exact failure this
        branch was opened for, one release later. Nothing may be sent: the run stops and
        the person decides, with the playlist link and what the record does prove."""
        tracks = tracks_for(12)
        rec = self._legacy_record()
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": rec}, f)
        upstream = Upstream()
        upstream.playlist = [vid(i) for i in range(1, 7)]    # what the old run proved
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.sends, [], "a legacy record was retried blanket-wise")
        self.assertEqual(upstream.creates, 0, "a second playlist was created")
        self.assertEqual(upstream.playlist, [vid(i) for i in range(1, 7)],
                         "the playlist was written to anyway")
        self.assertEqual(snap["state"], "failed", snap)
        self.assertEqual(snap.get("kind"), "user", snap)
        self.assertIn("older version", snap.get("error", ""), snap)
        err = str(snap.get("error", "")) + " " + str(snap.get("hint", ""))
        # /api/run truncates `error`, so the whole message is taken from the raise itself:
        # the record is the only copy of the log, the plan and the playlist link, and the
        # advice may not be to remove any of them.
        with mock.patch("urllib.request.urlopen", upstream):
            with self.assertRaises(RuntimeError) as ctx:
                backend.transfer_create_append(
                    COOKIE, "Repair Test", "", [vid(i) for i in range(1, 13)],
                    "PRIVATE", "SPOTREPAIR")
        full = str(ctx.exception)
        self.assertIn("older version", full)
        self.assertIn(PLID, full, "the message did not name the playlist")
        for text in (full, err, backend.classify(full).get("hint", "")):
            self.assertNotIn("Reset", text)
            self.assertNotIn("reset", text.lower())
            self.assertNotIn("delete", text.lower())
            self.assertNotIn("created.json", text.lower())
        self.assertTrue(os.path.exists(backend.CREATEDF), "the record was removed")
        with open(backend.LOGF, encoding="utf-8") as f:
            log = f.read()
        self.assertIn(PLID, log, "the message did not name the playlist")
        self.assertIn("6 of the 12 copies", log)
        # The record is not destroyed by the refusal: the playlist link and the plan
        # a later run needs are still there.
        after = self.created()["SPOTREPAIR"]
        self.assertEqual(after.get("yt_id"), PLID)
        self.assertEqual(after.get("video_ids"), rec["video_ids"])

    def test_a_legacy_record_that_says_complete_still_tops_up(self):
        """Refusing every old record would strand a finished transfer whose source simply
        gained a track. A record that says complete proved every copy from a complete
        read - that version never wrote complete over a copy it could not see - so a
        top-up is still safe and still adds only what is missing."""
        plan = [vid(i) for i in range(1, 9)]
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"yt_id": PLID,
                                      "url": "https://music.youtube.com/playlist?list=" + PLID,
                                      "title": "Repair Test", "expected": 8,
                                      "complete": True, "state": "done",
                                      "video_ids": plan, "plan_slots": plan,
                                      "plan_guesses": {}}}, f)
        upstream = Upstream()
        upstream.playlist = list(plan)
        snap = self.transfer(upstream, tracks_for(9))     # the source gained a track

        self.assertEqual(upstream.creates, 0, "a second playlist was created")
        self.assertEqual(upstream.appends, [vid(9)],
                         "the top-up sent more than the track that was missing")
        self.assertEqual(snap["state"], "done", snap)

    def test_an_inline_copy_a_complete_read_does_not_show_is_never_appended(self):
        """The create's reply named the playlist, so its inline batch was taken. An
        inline copy a COMPLETE read does not show is therefore a copy the read cannot
        name - not a copy the create failed to write. A read is padded with rows, so its
        length still matches while a specific id is missing, and appending that copy is
        one source row becoming two entries.

        Three tracks, the create stores one of them under another name: the twelve live
        copies the emergency run appended in this exact position became 460."""
        tracks = tracks_for(3)
        hidden = vid(1)
        upstream = Upstream(relabel={hidden: "Z0000000001"}, accept_repeats=True)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.sends, [],
                         "an inline copy the read did not show was appended: %r"
                         % upstream.sends)
        self.assertEqual(upstream.appends, [])
        self.assertIn(hidden, snap["residual"], snap)
        self.assertEqual(snap["counts"]["added"], 2, snap["counts"])
        self.assertEqual(snap["state"], "gaveup", snap)
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("unsure_ids"), [hidden], rec.get("unsure_ids"))

    def test_an_auth_refusal_reaches_the_run_as_its_own_status(self):
        """A 401 is a verdict on the call, not on a track. The run must carry that
        status into /api/run - `reason` is a fixed sentence with no status in it, so
        `error`/`kind`/`hint` are the only fields that can say what happened - and it
        must not pick a cause: an expired session and a permission answer are
        indistinguishable from here.

        The copies the run never sent are not "missing" either. Nothing asked the
        playlist for them, so they are not entries a read proved absent, and the one
        the server refused is refused - not a copy that vanished."""
        tracks = tracks_for(60)
        upstream = Upstream(fatal=401)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["state"], "gaveup")
        self.assertIn("401", str(snap.get("error", "")), snap)
        self.assertEqual(snap.get("kind"), "auth", snap)
        hint = str(snap.get("hint", ""))
        self.assertIn("may have expired", hint)
        self.assertIn("permission", hint)
        self.assertNotIn("cookie expired", hint)
        self.assertEqual(snap["counts"]["added"], 50, snap["counts"])
        self.assertEqual(snap["counts"]["refused"], 1, snap["counts"])
        # A dead cookie is not asked twice: the first answer ends the run, so the
        # repair pass never writes into the same refusal.
        self.assertEqual(upstream.sends, [vid(51)],
                         "the run kept writing after a fatal answer: %r" % upstream.sends)
        self.assertEqual(snap["missing"], 0,
                         "a copy the run never sent was counted as absent")
        not_sent = [r for r in snap["rows"] if r.get("not_sent")]
        self.assertEqual(len(not_sent), 9, not_sent)
        self.assertTrue(all("not sent" in r.get("status", "") for r in not_sent), not_sent)

    def test_a_crash_between_the_add_and_its_proof_keeps_the_copy_unknown(self):
        """The window a crash leaves open.

        An append is accepted, and the process dies before the read-back can prove it.
        The record must still say those copies were SENT: they are cleared from the
        write-ahead only once a read has settled them, so a crash here hands the next
        run a doubt. Clearing them at send time - which the sender used to do - left the
        resume reading a copy it could not see as a copy it never sent, and it wrote it
        again.

        The copy is one whose row the read cannot name, so the doubt is never answered
        and the resume may not re-send it."""
        tracks = tracks_for(60)
        hidden = vid(60)
        upstream = Upstream(relabel={hidden: "Z0000000060"}, accept_repeats=True,
                            crash_on_read=2)   # read 1 = the create's, 2 = the proof
        with self.assertRaises(Crash):
            self.transfer(upstream, tracks)

        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("inflight"), list(range(50, 60)),
                         "the accepted copies were not left queued: %r" % rec.get("inflight"))
        self.assertEqual(rec.get("inflight_need", {}).get(hidden), 1, rec.get("inflight_need"))
        before = len(upstream.sends)

        upstream.crash_on_read = 0
        self.transfer(upstream, tracks)         # the resume
        self.assertEqual(len(upstream.sends), before,
                         "the resume re-sent a copy whose write the crash left unknown: %r"
                         % upstream.sends[before:])
        self.assertEqual(upstream.creates, 1, "the resume created a second playlist")

    def test_a_freeze_that_cannot_be_persisted_stops_the_run(self):
        """A freeze that is not on disk is not a freeze: a resume would read the record,
        find nothing unknown, and send the copy again. The write that records it is a
        gate, like every other write-ahead in this file."""
        vids = [vid(i) for i in range(1, 4)]
        hidden = vids[2]
        upstream = Upstream(relabel={hidden: "Z0000000003"}, accept_repeats=True)
        real = backend._persist_fields
        seen = []

        def stubborn(spotify_id, **fields):
            if "unsure" in fields and "inflight" not in fields:   # the freeze write
                seen.append(dict(fields))
                return False
            return real(spotify_id, **fields)

        with mock.patch("urllib.request.urlopen", upstream), \
                mock.patch.object(backend, "_persist_fields", stubborn):
            with self.assertRaises(RuntimeError) as ctx:
                backend._converge(PLID, COOKIE, vids, spotify_id="SPOTFREEZE")

        self.assertIn("resume record could not be written", str(ctx.exception))
        self.assertTrue(seen, "the freeze was never attempted")
        # Nothing further goes out once the record refuses the doubt: the run stops on
        # the same write every other path fails closed on.
        self.assertEqual(len(upstream.sends), len(set(upstream.sends)),
                         "a copy was re-sent after the record refused it: %r"
                         % upstream.sends)

    def test_a_fatal_answer_during_a_top_up_stops_the_run(self):
        """The top-up is a third entry point, and it must answer like the other two.

        A 401 while topping up used to be returned without the status, so the caller went
        on to the repair pass and asked the same dead cookie for another write - one
        attempt per path instead of one attempt, full stop. The status now travels, the
        repair pass is not entered, and the page is told what the server said."""
        plan = [vid(i) for i in range(1, 9)]
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                                      "yt_id": PLID,
                                      "url": "https://music.youtube.com/playlist?list=" + PLID,
                                      "title": "Repair Test", "expected": 8,
                                      "complete": True, "state": "done",
                                      "video_ids": plan, "plan_slots": plan,
                                      "plan_guesses": {}}}, f)
        upstream = Upstream(fatal=401)
        upstream.playlist = list(plan)
        snap = self.transfer(upstream, tracks_for(9))       # the source gained a track

        self.assertEqual(upstream.sends, [vid(9)],
                         "the run wrote again after a fatal answer: %r" % upstream.sends)
        self.assertEqual(upstream.appends, [], "a refused append was recorded as landed")
        self.assertEqual(upstream.creates, 0)
        self.assertEqual(snap["state"], "gaveup", snap)
        self.assertIn("401", str(snap.get("error", "")), snap)
        self.assertEqual(snap.get("kind"), "auth", snap)
        self.assertIn("may have expired", snap.get("hint", ""))

    def test_freezing_a_song_removes_every_occurrence_from_the_repair(self):
        """Freezing is by videoId, because copies of one id cannot be told apart in a read.

        A plan of three copies of A, one accepted and no read showing it: A is frozen, and
        the two copies that were never sent must leave the repair with it. Pruning only the
        stalled position left them in the batch, so the next slice wrote two more copies of
        a song whose state nobody could account for."""
        a = vid(1)
        upstream = Upstream(relabel={a: "Z0000000001"}, accept_repeats=True)
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, [a, a, a],
                                    sent={0: "STATUS_SUCCEEDED"})

        self.assertEqual(upstream.sends, [],
                         "a frozen song's other copies were sent: %r" % upstream.sends)
        self.assertEqual(upstream.playlist, [], "the playlist was written to")
        self.assertEqual(out["state"], "gaveup", out)
        self.assertIn(a, out["residual"], out["residual"])

    def test_a_lost_create_reply_keeps_the_playlist_it_may_have_made(self):
        """The create may have made a playlist whose id we never saw - and that playlist is
        the only copy of the work this run carried.

        So the failure keeps it: it names the manual check, says the playlist is worth
        finishing by hand, and says no second one will be created. The old wording told the
        reader to delete it if it was there and start again, which loses the very thing the
        check is for. The record keeps the intent, so a later attach refuses too - with the
        same advice."""
        plan = [vid(1), vid(2), vid(3)]

        class LostReply(Upstream):
            def __call__(self, req, timeout=None):
                if "playlist/create" in (getattr(req, "full_url", "") or ""):
                    raise urllib.error.URLError("reply lost")
                return super().__call__(req, timeout)

        upstream = LostReply()
        upstream.playlist = plan          # the playlist the create really made
        with mock.patch("urllib.request.urlopen", upstream):
            with self.assertRaises(RuntimeError) as caught:
                backend.transfer_create_append(COOKIE, "Repair Test", "", plan,
                                               "PRIVATE", "SPOTREPAIR")
        first = str(caught.exception)
        self.assertIn("library", first.lower())
        self.assertIn("keep it", first.lower())
        self.assertNotIn("retry", first.lower())
        for text in (first, backend.classify(first).get("hint", "")):
            self.assertNotIn("delete", text.lower())
            self.assertNotIn("reset", text.lower())
            self.assertNotIn("start again", text.lower())
        self.assertEqual(upstream.creates, 0)
        rec = self.created()["SPOTREPAIR"]
        self.assertTrue(rec.get("create_pending"), "the create intent was not recorded")

        # The next attach refuses to create a second one, with the same advice and no
        # deletion: the playlist that may exist is still the only copy.
        with mock.patch("urllib.request.urlopen", Upstream()):
            with self.assertRaises(RuntimeError) as again:
                backend.transfer_create_append(COOKIE, "Repair Test", "", plan,
                                               "PRIVATE", "SPOTREPAIR")
        second = str(again.exception)
        self.assertIn("second one", second.lower())
        self.assertIn("keep it", second.lower())
        for text in (second, backend.classify(second).get("hint", "")):
            self.assertNotIn("delete", text.lower())
            self.assertNotIn("reset", text.lower())
            self.assertNotIn("start again", text.lower())

        # A reply that carries no id is the same situation and the same advice.
        class NoId(Upstream):
            def create(self, body):
                self.creates += 1
                return Resp({})
        noid = NoId()
        with mock.patch("urllib.request.urlopen", noid):
            with self.assertRaises(RuntimeError) as third:
                backend._create_new(COOKIE, "Repair Test", "", plan, "PRIVATE",
                                    "SPOTOTHER")
        text = str(third.exception)
        self.assertIn("keep it", text.lower())
        self.assertNotIn("delete", text.lower())
        self.assertNotIn("reset", text.lower())


        """A record that cannot be read is the likeliest place the playlist link still is.

        The old wording said to fix or delete it and Reset, which throws away the only
        copy of the link, the plan and the log of a run whose playlist may exist. The
        message - and the hint the page shows - now say to keep a safe copy and check the
        playlist, and nothing writes over the file."""
        bad = "{ this is not json"
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            f.write(bad)
        with self.assertRaises(backend.RecordUnreadable) as ctx:
            backend.load_created()
        msg = str(ctx.exception)
        self.assertTrue(os.path.exists(backend.CREATEDF), "the record was removed")
        with open(backend.CREATEDF, encoding="utf-8") as f:
            self.assertEqual(f.read(), bad, "the record was rewritten")
        for text in (msg, backend.classify(msg).get("hint", "")):
            self.assertIn("copy", text.lower())        # keep a safe copy first
            self.assertIn("playlist", text.lower())    # check the playlist
            self.assertNotIn("delete", text.lower())
            self.assertNotIn("reset", text.lower())
            self.assertNotIn("start again", text.lower())

        # The wrong shape gets the same treatment, and the run path adds nothing else.
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "record"], f)
        with self.assertRaises(backend.RecordUnreadable) as ctx2:
            backend.load_created()
        self.assertNotIn("delete", str(ctx2.exception).lower())

        upstream = Upstream()
        snap = self.transfer(upstream, tracks_for(6))
        self.assertEqual(upstream.sends, [], "a run wrote over an unreadable record")
        self.assertEqual(upstream.creates, 0)
        self.assertEqual(snap["state"], "failed", snap)
        self.assertNotIn("delete", str(snap.get("hint", "")).lower())
        self.assertNotIn("reset", str(snap.get("hint", "")).lower())
        with open(backend.CREATEDF, encoding="utf-8") as f:
            self.assertEqual(json.load(f), ["not", "a", "record"],
                             "the run overwrote the record it could not read")


        """A record that names copies whose outcome is unknown but not the plan they belong
        to cannot be resumed: rebuilding its rows from a fresh match would reconcile them
        against copies the playlist never held and append a second copy of every track.

        So it stops - and it stops without spending the evidence. The old wording sent the
        person to start again from scratch, which throws away the only copy of the log, the
        plan and the playlist link that finishing this by hand needs."""
        plan = [vid(i) for i in range(1, 7)]
        orphan = {"record_version": backend.RECORD_VERSION, "yt_id": PLID,
                  "url": "https://music.youtube.com/playlist?list=" + PLID,
                  "title": "Repair Test", "expected": 6, "complete": False,
                  "state": "gaveup", "attempt": 0,
                  "video_ids": [], "plan_slots": [],          # the plan is not on record
                  "unsure": [3], "unsure_need": {plan[3]: 1},
                  "inflight": [], "pending": []}
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": orphan}, f)
        upstream = Upstream()
        upstream.playlist = plan[:3]
        snap = self.transfer(upstream, tracks_for(6))

        self.assertEqual(upstream.sends, [], "a plan-less record was written to")
        self.assertEqual(upstream.creates, 0, "a second playlist was created")
        self.assertEqual(upstream.playlist, plan[:3], "the playlist was changed")
        self.assertEqual(snap["state"], "failed", snap)
        self.assertEqual(snap.get("kind"), "user", snap)
        self.assertIn(PLID, snap.get("error", ""), snap)
        for text in (snap.get("error", ""), snap.get("hint", "")):
            self.assertNotIn("Reset", text)
            self.assertNotIn("reset", text.lower())
            self.assertNotIn("scratch", text.lower())
            self.assertNotIn("delete", text.lower())
        # The record survives the refusal, minus nothing.
        after = self.created()["SPOTREPAIR"]
        self.assertEqual(after.get("yt_id"), PLID)
        self.assertEqual(after.get("unsure"), [3])
        # The other door to the same record - the durable unknown lookup - says the same
        # thing, and no more.
        with self.assertRaises(backend.RecordUnreadable) as ctx:
            backend._unknown_ids_in(orphan)
        msg = str(ctx.exception)
        self.assertNotIn("Reset", msg)
        self.assertNotIn("reset", msg.lower())
        self.assertNotIn("delete", msg.lower())
        self.assertIn("add what is missing by hand", msg)
        self.assertIn("add what is missing by hand", backend.classify(msg).get("hint", ""))


        """A version stamped by whichever write happened to touch the file would promote a
        record whose evidence is still from the old protocol: a top-up's plan write, a
        progress write, a standby - any of them would make an unsafe record look current
        and open the gate on evidence nobody verified.

        Only a record BORN here carries the version, and a legacy one is promoted only
        after a read proves its writes."""
        self._write_legacy(self._legacy_record())
        before = self.created()["SPOTREPAIR"]
        self.assertNotIn("record_version", before)

        backend._persist_fields("SPOTREPAIR", state="standby", attempt=2)
        backend._persist_progress("SPOTREPAIR", PLID,
                                  "https://example.invalid/p", "Repair Test", 12,
                                  state="standby", attempt=2, pending=[], plan=before["video_ids"])
        backend._adopt_plan("SPOTREPAIR", list(before["video_ids"]), None, None, 6, [50], [])

        rec = self.created()["SPOTREPAIR"]
        self.assertNotIn("record_version", rec,
                         "an unrelated save promoted a legacy record: %r" % rec)

    def test_a_legacy_record_that_says_complete_but_hides_a_missing_copy_fails_closed(self):
        """`complete: true` from an older version is not proof. A run interrupted while
        topping up left complete:true over writes whose outcomes that version had already
        cleared, so the flag cannot stand in for the read that proves the ambiguity gone.

        Eleven of twelve copies on the playlist, complete:true, no version: nothing may be
        sent, and nothing that would destroy the record may be suggested."""
        plan = [vid(i) for i in range(1, 13)]
        self._write_legacy({"yt_id": PLID,
                            "url": "https://music.youtube.com/playlist?list=" + PLID,
                            "title": "Repair Test", "expected": 12, "complete": True,
                            "state": "done", "video_ids": plan, "plan_slots": plan,
                            "plan_guesses": {}})
        upstream = Upstream()
        upstream.playlist = plan[:11]                 # one copy the old run cannot account
        snap = self.transfer(upstream, tracks_for(12))

        self.assertEqual(upstream.sends, [], "a legacy complete record was topped up")
        self.assertEqual(upstream.creates, 0)
        self.assertEqual(len(upstream.playlist), 11, "the playlist was written to")
        self.assertEqual(snap["state"], "failed", snap)
        self.assertEqual(snap.get("kind"), "user", snap)
        err = str(snap.get("error", "")) + " " + str(snap.get("hint", ""))
        self.assertIn("older version", err)
        self.assertNotIn("Reset", err)
        self.assertNotIn("reset", err.lower())
        self.assertNotIn("delete", err.lower())
        self.assertNotIn("record_version", self.created()["SPOTREPAIR"],
                         "a refused record was promoted")

    def test_a_legacy_record_is_promoted_only_after_a_read_proves_its_writes(self):
        """The verification is paid once: a record whose own plan a complete read finds
        whole is safe to write new ids against, so it carries the version from then on -
        and a later attach does not read the playlist twice to re-prove it."""
        plan = [vid(i) for i in range(1, 9)]
        self._write_legacy({"yt_id": PLID,
                            "url": "https://music.youtube.com/playlist?list=" + PLID,
                            "title": "Repair Test", "expected": 8, "complete": True,
                            "video_ids": plan, "plan_slots": plan, "plan_guesses": {}})
        upstream = Upstream()
        upstream.playlist = list(plan)
        snap = self.transfer(upstream, tracks_for(9))     # the source gained a track

        self.assertEqual(upstream.creates, 0)
        self.assertEqual(upstream.appends, [vid(9)])
        self.assertEqual(snap["state"], "done", snap)
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("record_version"), backend.RECORD_VERSION, rec)

    def test_a_create_crash_before_the_first_read_keeps_every_inline_copy_unknown(self):
        """The window between the create's reply and the first read.

        The create's reply named the playlist, so its inline batch was taken - and no read
        has looked at it yet. Every inline copy is therefore unproven, not just the
        repeats, and a process that dies here must leave all of them uncertain. Recording
        only the repeats let a resume read the copies it could not see as copies it never
        sent, and append them (the live run's twelve became 460)."""
        tracks = tracks_for(60)
        hidden = vid(1)                              # inline, stored under another name
        upstream = Upstream(relabel={hidden: "Z0000000001"}, accept_repeats=True,
                            crash_on_read=1)         # read 1 is the create's own seed
        with self.assertRaises(Crash):
            self.transfer(upstream, tracks)

        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("unsure"), list(range(50)), rec.get("unsure"))
        self.assertEqual(rec.get("unsure_ids"), [vid(i) for i in range(1, 51)],
                         rec.get("unsure_ids"))
        self.assertEqual(rec.get("record_version"), backend.RECORD_VERSION,
                         "a playlist created here is not a legacy record")

        upstream.crash_on_read = 0
        self.transfer(upstream, tracks)              # the resume
        # Only the copies the create never carried are appended; no inline copy is.
        self.assertEqual(upstream.sends, [vid(i) for i in range(51, 61)],
                         "an inline copy was re-sent: %r" % upstream.sends)
        self.assertEqual(upstream.creates, 1)

    def _write_legacy(self, rec):
        """Write a record the way an older version left it: no version stamp."""
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": rec}, f)

    # --- 4: a playlist the server refuses every write for stays bounded -----
    def test_a_wire_that_refuses_every_append_is_bounded_and_names_each_copy(self):
        """Every append answers 429. Each one is an unknown write, so none is re-sent,
        and the run may not write forever: the passes are bounded, the copies it did
        not prove are named, and no copy is ever written twice."""
        tracks = tracks_for(60)
        upstream = Upstream(throttle=99)          # every append answers 429
        started = time.monotonic()
        snap = self.transfer(upstream, tracks)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0, "the bounded loop took %.1fs" % elapsed)
        self.assertEqual(self.waits, [], "an append was waited out")
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(snap["counts"]["added"], 50, snap["counts"])
        self.assertEqual(snap["counts"]["unverified"], 10, snap["counts"])
        self.assertEqual(snap["counts"]["refused"], 0, snap["counts"])
        self.assertEqual(snap["residual"], [vid(i) for i in range(51, 61)])
        # Bounded: one send attempt per copy, never a second, and never more passes
        # than the convergence bound allows.
        self.assertEqual(len(upstream.sends), 10, upstream.sends)
        self.assertEqual(len(upstream.sends), len(set(upstream.sends)))
        self.assertEqual(upstream.playlist, [vid(i) for i in range(1, 51)])
        # No second playlist: the run repaired the one it had.
        self.assertEqual(upstream.creates, 1)
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["state"], "gaveup")
        self.assertIs(rec["complete"], False)
        self.assertEqual(rec["unsure"], list(range(50, 60)))
        self.assertEqual(rec["expected_min"], 50)

    # --- 4b: a resume continues the same run and never creates a playlist ---
    def test_a_persisted_run_resumes_in_place_with_its_own_attempt_count(self):
        tracks = tracks_for(60)
        upstream = Upstream()                        # holds everything it is given
        upstream.playlist = [vid(i) for i in range(1, 56)]      # five copies still absent
        due = time.time() + 600
        # The interrupted run: attempt 3, a retry time ten minutes out, 1:1 off, and
        # the plan it was sent - the last five in an order a fresh match would not
        # produce, so a rematch cannot pass for a resume.
        plan = [vid(i) for i in range(1, 56)] + [vid(i) for i in range(60, 55, -1)]
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION, "yt_id": PLID, "url": "https://example.invalid/p",
                                      "title": "Repair Test", "expected": 60,
                                      "complete": False, "state": "standby",
                                      "attempt": 3, "next_run_at": due,
                                      "video_ids": plan,
                                      "pending": []}}, f)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.creates, 0, "a resume must never create a playlist")
        self.assertEqual(snap["state"], "done")
        self.assertEqual(snap["attempt"], 3, "the resumed run kept its own attempt count")
        self.assertEqual(snap["landed"], 60)
        # The persisted retry time is honoured: a restart is not a shorter backoff,
        # so nothing is sent before the time the interrupted run chose.
        self.assertTrue(self.waits, "the resume ignored the persisted retry time")
        stood = self.waits[0]
        self.assertEqual(stood["snap"]["state"], "standby")
        self.assertEqual(stood["snap"]["attempt"], 3)
        self.assertGreaterEqual(stood["delay"], 590, "the resume waited less than the record asked")
        self.assertEqual(stood["appends"], [], "the resume appended before its retry time")
        # ...and it replays the plan it was interrupted with.
        self.assertEqual(upstream.appends, [vid(i) for i in range(60, 55, -1)])
        self.assertTrue(upstream.actions, "no append was sent")
        # A transfer is 1:1 with the source, always: the resume asks the server to
        # keep the copy too, whether or not an older record carried a toggle. The
        # toggle is gone from the settings, the API and the run state.
        for act in upstream.actions:
            self.assertEqual(act.get("dedupeOption"), "DEDUPE_OPTION_SKIP", act)

    # --- 4c: a resume replays the persisted plan, never a fresh match -------
    def test_a_resume_uses_the_plan_the_interrupted_run_persisted(self):
        """An interrupted run's record is the only plan its resume may use.

        The interrupted create landed eight copies, then every read failed. When
        the run is restarted the source searches to DIFFERENT videoIds, so a resume
        that matched again would reconcile the rows against ids the playlist never
        held and append a second copy of every track."""
        tracks = tracks_for(8)
        self.transfer(Upstream(browse_broken=True), tracks)   # the create, then no reads
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("yt_id"), PLID)
        self.assertIs(rec.get("complete"), False, rec)
        plan = [vid(i) for i in range(1, 9)]
        self.assertEqual(rec.get("video_ids"), plan,
                         "the interrupted record did not carry the plan it was sent")

        # The restart: the playlist still holds what the create carried, and the
        # upstream search now answers with ids the playlist has never seen.
        second = Upstream()
        second.playlist = list(plan)
        searches = []

        def shifted(chunk, workers=8):
            searches.append([(t or {}).get("title") for t in chunk])
            return [{"ok": True, "query": (t or {}).get("title", ""),
                     "videoId": "X%010d" % (k + 1)} for k, t in enumerate(chunk)]

        snap = self.transfer(second, tracks, batch=shifted)

        self.assertEqual(searches, [], "a resume matched the source again")
        self.assertEqual(second.appends, [], "a resume appended off a fresh match")
        self.assertEqual(second.playlist, plan, "the resume changed the playlist")
        self.assertEqual([r.get("videoId") for r in snap["rows"]], plan,
                         "the rows were reconciled against the new match")
        self.assertEqual(snap["landed"], 8)
        self.assertEqual(snap["phase"], "done", snap.get("error"))

    def test_a_record_without_a_plan_reports_the_interrupted_run(self):
        """A record that predates the persisted plan cannot say which copies the
        playlist holds: resuming it would guess, and guessing appends copies that
        are already there. So the run is reported as interrupted, not resumed."""
        tracks = tracks_for(5)
        upstream = Upstream()
        upstream.playlist = [vid(i) for i in range(1, 6)]
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION, "yt_id": PLID, "url": "https://example.invalid/p",
                                      "title": "Repair Test", "expected": 5,
                                      "complete": False, "state": "standby",
                                      "attempt": 1, "next_run_at": 0, "pending": []}}, f)
        searches = []

        def shifted(chunk, workers=8):
            searches.append([(t or {}).get("title") for t in chunk])
            return [{"ok": True, "query": (t or {}).get("title", ""),
                     "videoId": "X%010d" % (k + 1)} for k, t in enumerate(chunk)]

        snap = self.transfer(upstream, tracks, batch=shifted)

        self.assertEqual(snap["phase"], "failed", snap)
        self.assertIn("interrupted", str(snap.get("error", "")).lower(), snap.get("error"))
        self.assertEqual(searches, [], "a record without a plan was rematched")
        self.assertEqual(upstream.appends, [], "a run without a plan appended off a guess")
        self.assertEqual(upstream.playlist, [vid(i) for i in range(1, 6)])

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
        upstream = Upstream(empty_reads=6)      # the stale window, then the truth
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.deletes, [], "a stale empty read deleted the playlist")
        self.assertEqual(len(upstream.playlist), len(tracks))
        self.assertNotIn("husk deleted", snap.get("error", ""))
        self.assertEqual(snap["phase"], "failed", snap)

    def test_a_wholly_failed_read_is_a_failed_run_with_unverified_rows(self):
        """Every read fails after the create. Nothing is proved either way about
        the copies the create carried, so those rows are unverified - never
        skipped, which claims they were never attempted - and the state is failed."""
        tracks = tracks_for(30)
        upstream = Upstream(browse_broken=True)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["phase"], "failed", snap.get("error"))
        self.assertEqual(snap["state"], "failed", "a failed phase must not claim a running state")
        self.assertEqual(snap["skipped"], 0)
        unproven = [r for r in snap["rows"] if r.get("unverified")]
        self.assertEqual(len(unproven), 30, snap["rows"][:2])
        self.assertTrue(all(r.get("state") == "missing" for r in unproven),
                        "an unproven row must not be filed as a refusal")
        self.assertEqual(snap["counts"]["added"], 0)
        self.assertEqual(snap["counts"]["unverified"], 30)
        self.assertEqual(snap["counts"]["refused"], 0)
        self.assertEqual(upstream.creates, 1)

    # --- 6: an unknown outcome is read before it is resent ------------------
    def test_an_ambiguous_add_is_not_resent_without_a_complete_read(self):
        """The add was applied and its reply was lost. A resend may only follow a
        COMPLETE read; when the read cannot prove the playlist, nothing is sent."""
        upstream = Upstream(lose_reply={vid(1)}, paging_broken=True)
        upstream.playlist = ["X%010d" % i for i in range(150)]   # more than one page
        with mock.patch("urllib.request.urlopen", upstream):
            st = backend.yt_add_one(PLID, vid(1), COOKIE)

        self.assertTrue(backend.is_unsure_status(st), st)
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

    def test_an_add_whose_reply_is_lost_is_not_resent_off_one_complete_read(self):
        """The write landed and its reply was lost. The read straight after it is
        token-complete and shows the copy - and a resend sent on that read is not a
        repair: every add carries the skip, so the server keeps the second copy and
        the playlist ends up holding one source row twice. A complete read is not
        proof the copy stayed out, so the add reports the ambiguity and sends
        nothing more; the repair pass re-reads after its own backoff and decides."""
        held, sends = [], []

        def fake(req, timeout=None):
            url = getattr(req, "full_url", "") or ""
            try:
                body = json.loads((getattr(req, "data", b"") or b"{}").decode("utf-8"))
            except Exception:
                body = {}
            if "edit_playlist" in url:
                v = (body.get("actions") or [{}])[0].get("addedVideoId") or ""
                sends.append(v)
                held.append(v)      # the skip is honoured: a second copy lands
                raise urllib.error.URLError("timed out")
            rows = [{"musicResponsiveListItemRenderer": {"playlistItemData": {"videoId": v}}}
                    for v in held]
            return Resp({"contents": {"musicPlaylistShelfRenderer": {"contents": rows}}})

        with mock.patch("urllib.request.urlopen", fake):
            st = backend.yt_add_one(PLID, vid(1), COOKIE)

        self.assertTrue(backend.is_unsure_status(st), st)
        self.assertNotIn("SUCCEEDED", str(st).upper())
        self.assertEqual(sends, [vid(1)], "an add whose outcome is unknown was re-sent")
        self.assertEqual(held, [vid(1)], "one source row asked the server for two copies")

    def test_a_lost_reply_in_the_repair_pass_is_not_resent_either(self):
        """The repair sender has the same rule, and it goes further: a write whose
        outcome is unknown is never sent again by this run, whatever a read says.
        Here the fake applied it, so the read proves the copy and the run is done -
        with one send and no standby, because there is no retry to wait for."""
        upstream = Upstream(lose_reply={vid(6)}, fail_read_after_loss=True)
        self.upstream = upstream
        upstream.playlist = [vid(i) for i in range(1, 6)]
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, [vid(i) for i in range(1, 7)])

        self.assertEqual(upstream.appends.count(vid(6)), 1,
                         "the repair re-sent an add whose outcome was unknown")
        self.assertEqual(out["state"], "done", out)
        self.assertEqual(self.waits, [],
                         "the run waited out a retry it must never make")

    def test_a_stale_read_hiding_an_unknown_write_never_authorises_a_resend(self):
        """The write landed, its reply was lost, and every read after it is a stale
        snapshot taken before the write. A complete read that does not show the copy
        used to be the whole proof for sending it again - and every add carries the
        skip, so the playlist ended up holding one source row twice. Absence in a
        possibly-stale read proves nothing, so the copy is frozen for the run and
        reported unknown instead."""
        held, appends = [vid(1)], []
        snapshot = {"reads": 0}

        def fake(req, timeout=None):
            url = getattr(req, "full_url", "") or ""
            body = json.loads((getattr(req, "data", b"") or b"{}").decode("utf-8"))
            if "edit_playlist" in url:
                v = (body.get("actions") or [{}])[0].get("addedVideoId") or ""
                appends.append(v)
                held.append(v)              # the write lands...
                raise urllib.error.URLError("reply lost")   # ...and the reply does not
            snapshot["reads"] += 1
            rows = [{"musicResponsiveListItemRenderer": {"playlistItemData": {"videoId": v}}}
                    for v in held[:1]]       # a stale page: only what the first read saw
            return Resp({"contents": {"musicPlaylistShelfRenderer": {"contents": rows}}})

        with mock.patch("urllib.request.urlopen", fake):
            out = backend._converge(PLID, COOKIE, [vid(1), vid(2)])

        self.assertEqual(appends, [vid(2)],
                         "an unknown write was sent again off a stale read")
        self.assertEqual(out["state"], "gaveup", out)
        self.assertIn("outcome is unknown", out["reason"])
        self.assertEqual(out["residual"], [vid(2)])
        self.assertTrue(backend.is_unsure_status(out["statuses"].get(1, "")),
                        out["statuses"])

    def test_an_unknown_write_that_did_not_land_is_unverified_not_resent(self):
        """The reply was lost and the write never landed. Absence proves nothing
        either way, so the copy is not re-sent: the run ends gaveup naming it, the
        row is unverified rather than refused or missing, and the row says how to
        recover it. Its verified neighbours still land - nothing else is held back."""
        tracks = tracks_for(60)
        upstream = Upstream(lose_before_apply={vid(55)})
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.appends.count(vid(55)), 1,
                         "an unknown write was re-sent automatically")
        self.assertEqual(snap["counts"]["added"], 59, snap["counts"])
        self.assertEqual(snap["counts"]["unverified"], 1, snap["counts"])
        self.assertEqual(snap["counts"]["refused"], 0, snap["counts"])
        self.assertEqual(snap["state"], "gaveup")
        self.assertIn("outcome is unknown", snap["reason"])
        self.assertEqual(snap["residual"], [vid(55)])
        row = snap["rows"][54]
        self.assertEqual(row["state"], "missing", row)
        self.assertTrue(row.get("unverified"), row)
        self.assertIn("outcome unknown", row["status"])
        self.assertIn("not re-sent", row["status"])
        # The read reached the end, so nothing here may blame a short read.
        self.assertEqual(snap["tail_unverified"], 0, snap)
        self.assertIn("1 unverified", snap["summary"])
        self.assertNotIn("read stopped", snap["summary"])
        self.assertIs(self.created()["SPOTREPAIR"]["complete"], False)

    def test_an_unknown_write_reads_back_as_unverified_not_missing(self):
        """`_reconcile` is the one place a status becomes an outcome: an unknown
        write absent from a read is unverified and named for recovery, never a
        refusal and never a proven absence. A 429 is one of those: for this endpoint
        it does not prove the request was refused unapplied."""
        vids = [vid(1), vid(2), vid(3)]
        ledger = backend._reconcile(
            vids, [vid(1)],
            {1: backend.STATUS_UNSURE + " URLError",
             2: backend.STATUS_UNSURE + " HTTPError 429"})
        self.assertEqual(ledger[0]["outcome"], "landed")
        for i in (1, 2):
            self.assertEqual(ledger[i]["outcome"], "missing", ledger[i])
            self.assertTrue(ledger[i].get("unverified"), ledger[i])
            self.assertTrue(ledger[i].get("unsure"), ledger[i])
            self.assertIn("outcome unknown", ledger[i]["status"])
            self.assertIn("not re-sent", ledger[i]["status"])
        # A refusal the server actually stated stays a refusal: only an unreadable
        # outcome is unknown.
        refused = backend._reconcile(vids, [vid(1)], {1: "STATUS_FAILED"})
        self.assertEqual(refused[1]["outcome"], "rejected")
        self.assertFalse(refused[1].get("unverified"), refused[1])

    def test_a_429_is_an_unknown_write_and_is_never_re_sent(self):
        """A 429 on this endpoint is not proof the write was refused unapplied, so it
        takes the same path as a lost reply: one send, unverified, no retry."""
        self.assertTrue(backend.is_unsure_status(backend.STATUS_UNSURE + " HTTPError 429"))
        tracks = tracks_for(60)
        upstream = Upstream(throttle=2)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["state"], "gaveup", snap)
        self.assertEqual(snap["counts"]["added"], 58, snap["counts"])
        self.assertEqual(snap["counts"]["unverified"], 2, snap["counts"])
        self.assertEqual(len(upstream.sends), len(set(upstream.sends)))

    def test_a_refused_copy_does_not_block_another_occurrence_of_the_same_song(self):
        """A verdict belongs to the copy the server answered about. The plan asks for
        three copies of one videoId, the playlist holds one, and the server refused the
        second copy: the third is still free to be sent, and sending it is what makes
        the playlist hold what the plan asks. Blocking by videoId denies that write."""
        a = vid(1)
        upstream = Upstream(accept_repeats=True)
        upstream.playlist = [a]
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, [a, a, a], refused=[1])

        self.assertTrue(upstream.sends, "the refused copy blocked a later occurrence")
        self.assertEqual(set(upstream.sends), {a})
        self.assertEqual(upstream.playlist, [a, a, a])
        self.assertEqual(out["state"], "done", out)
        self.assertEqual(out["statuses"], {2: "STATUS_SUCCEEDED"}, out["statuses"])

    def test_a_copy_a_read_attributes_to_a_refused_slot_is_not_reported_absent(self):
        """Two copies of one videoId are indistinguishable in a read, so the copy a
        repair sends can be the entry an earlier, REFUSED copy's slot would claim. A
        slice proof that asks "did plan slot i land" then reports a copy that DID land
        as absent ("accepted and is not on the playlist read") and stops a slice that
        was fine.

        What a read can prove is multiplicity: the plan asks for two copies of A, the
        first was refused, the playlist holds one, and the repair's copy makes two."""
        a, b = vid(1), vid(2)
        upstream = Upstream(accept_repeats=True)
        upstream.playlist = [b]          # A's first copy was refused; only B is held
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, [a, b, a], refused=[0])

        with open(backend.LOGF, encoding="utf-8") as f:
            log = f.read()
        self.assertNotIn("not on the playlist read", log,
                         "a slice that landed was reported unproven")
        self.assertEqual(out["state"], "done", out)
        self.assertEqual(sorted(upstream.playlist), sorted([a, a, b]))

    def test_progress_is_measured_against_the_previous_read_not_the_union(self):
        """Present ids {A} -> {B} -> {A}: the third read shows a playlist that
        changed, so there was progress. Judged against the union of every read of
        the run it looks like none, and the run gives up on a live playlist."""
        a, b, c = vid(1), vid(2), vid(3)
        scripted = [[a], [b], [a], [a, b, c]]
        calls = []

        def reader(plid, cookie):
            calls.append(plid)
            entries = scripted[min(len(calls) - 1, len(scripted) - 1)]
            return {"entries": list(entries), "complete": True, "why": ""}

        upstream = Upstream()
        self.upstream = upstream
        with mock.patch("urllib.request.urlopen", upstream), \
                mock.patch.object(backend, "yt_playlist_read", reader):
            out = backend._converge(PLID, COOKIE, [a, b, c])

        self.assertEqual(out["state"], "done", out)
        self.assertEqual(self.waits, [], "a playlist that changed was read as no progress")
        self.assertEqual(len(calls), 4, "the run stopped before it could see the whole list")

    def test_the_repair_pass_publishes_its_own_progress(self):
        """Convergence runs its own sender, so it takes the same hooks the first pass
        does: the phase it is in, the copies it sends, and the rows its own complete
        read proves. Without them a repairing run is invisible to the page and reads
        as a stall."""
        a, b, c = vid(1), vid(2), vid(3)
        upstream = Upstream()
        upstream.playlist = [a, c]
        seen = []

        def on_verified(ledger):
            seen.append(("verified", [e["i"] for e in ledger if e["outcome"] == "landed"]))

        hooks = {"on_phase": seen.append,
                 "on_append_start": lambda pos: seen.append(("start", pos)),
                 "on_append_one": lambda pos, st: seen.append(("one", pos, bool(st))),
                 "on_verified": on_verified}
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, [a, b, c], hooks=hooks)

        self.assertEqual(out["state"], "done", out)
        self.assertIn("append", seen, seen)
        self.assertIn("verify", seen, seen)
        self.assertIn(("start", 1), seen, seen)   # position 1 is the absent copy
        self.assertTrue(any(isinstance(e, tuple) and e[0] == "one" and e[1] == 1
                            for e in seen), seen)
        self.assertTrue(any(isinstance(e, tuple) and e[0] == "verified" and 2 in e[1]
                            for e in seen), seen)

    def test_a_seed_read_that_stopped_short_publishes_no_landed_row(self):
        """The create's own read reaches page one and loses the continuation. Those
        copies really are on the playlist, and the read still proved nothing about the
        playlist: mid-run, `added` moves only on a read that reached the end, and this
        read's presence is accounted for once, by the final ledger, in the open."""
        tracks = tracks_for(60)
        extras = ["X%010d" % i for i in range(70)]
        upstream = Upstream(extra=extras, paging_broken=True)   # 130 entries, tail unreadable
        snap = self.transfer(upstream, tracks)

        self.assertTrue(self.waits, "the run never stood by")
        for stood in self.waits:
            self.assertEqual(stood["snap"]["counts"]["added"], 0,
                             "a read that stopped short published verified progress")
            self.assertFalse([r for r in stood["snap"]["rows"] if r.get("state") == "landed"],
                             "a partial read published landed rows")
        # The final ledger is where those pages count, once: the 30 copies the read
        # served are landed, the 30 it never reached are unverified.
        self.assertEqual(snap["counts"]["added"], 30, snap["counts"])
        self.assertEqual(snap["counts"]["unverified"], 30, snap["counts"])

    def test_a_resume_seed_that_stopped_short_publishes_no_landed_row(self):
        """The resume's first read is a seed with the same rule: a read that stopped
        before the end of the playlist proves no copy landed mid-run."""
        tracks = tracks_for(60)
        plan = [vid(i) for i in range(1, 61)]
        upstream = Upstream(extra=["X%010d" % i for i in range(70)] + plan,
                            paging_broken=True)
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION, "yt_id": PLID, "url": "https://example.invalid/p",
                                      "title": "Repair Test", "expected": 60,
                                      "complete": False, "state": "verifying",
                                      "attempt": 0, "next_run_at": 0,
                                      "video_ids": plan, "pending": []}}, f)
        snap = self.transfer(upstream, tracks)

        self.assertTrue(self.waits, "the run never stood by")
        for stood in self.waits:
            self.assertEqual(stood["snap"]["counts"]["added"], 0,
                             "a resume's partial read published verified progress")
        self.assertEqual(snap["counts"]["unverified"], 30, snap["counts"])

    def test_a_note_that_cannot_be_saved_stops_the_run(self):
        """The unknown set is only as durable as its last save.

        When the note that records an unknown does not land, the record on disk has no
        doubt - and the next save merges its own (empty) unknown set over it, so the
        doubt is written away by the very write that was supposed to be harmless. The
        run may therefore not go on past a note it could not make: it stops after the
        slice it sent, and the queue keeps that slice for the next run."""
        tracks = tracks_for(250)                  # 50 inline, 200 appended: two slices
        upstream = Upstream()
        seen = []
        real_save = backend.save_created
        state = {"armed": False, "failed": False}

        def save(d):
            rec = (d or {}).get("SPOTREPAIR") or {}
            if rec.get("inflight") and not state["armed"]:
                state["armed"] = True             # the first slice's write-ahead landed
                return real_save(d)
            if state["armed"] and not state["failed"] and not rec.get("inflight"):
                state["failed"] = True            # its note is lost: the queue stays
                return False
            return real_save(d)

        def watch(req, timeout=None):
            rec = (backend.load_created() or {}).get("SPOTREPAIR") or {}
            if "edit_playlist" in (getattr(req, "full_url", "") or ""):
                seen.append(list(rec.get("inflight") or []))
            return upstream(req, timeout)

        with mock.patch.object(backend, "save_created", save), \
                mock.patch("urllib.request.urlopen", watch):
            rid = backend.run_begin("", "")
            with mock.patch.object(backend, "fetch_spotify_tracks",
                                   fake_fetch(tracks)), \
                    mock.patch.object(backend, "yt_search_batch", fake_batch):
                snap = backend._transfer_thread(SPOTIFY_URL, COOKIE, "", "PRIVATE", rid) or \
                    backend.run_snapshot()

        self.assertEqual(snap["state"], "failed", snap)
        self.assertIn("could not be written", snap.get("error", ""))
        # One slice went out and not one add more: the second slice is never written.
        self.assertEqual(len(upstream.appends), 100, upstream.appends[100:])
        self.assertEqual(len(seen), 100, "the run kept writing past a lost note")
        # The slice the run could not classify is still on disk, so the next run
        # inherits a doubt instead of a queue of unsent-looking copies.
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("inflight"), list(range(50, 150)), rec.get("inflight"))

    def test_a_new_playlist_does_not_inherit_an_old_records_evidence(self):
        """A create starts a new evidence set. A record whose playlist is gone can still
        carry a floor and an unknown from the run that made it; inheriting them would
        make the new playlist read as short from its very first page."""
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"expected_min": 50, "unsure": [3, 4],
                                      "inflight": [5], "complete": True}}, f)
        upstream = Upstream()
        with mock.patch("urllib.request.urlopen", upstream):
            backend._create_new(COOKIE, "Repair Test", "", [vid(i) for i in range(1, 11)],
                                "PRIVATE", "SPOTREPAIR")

        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("unsure"), [], "the new playlist inherited stale unknowns")
        self.assertEqual(rec.get("inflight"), [])
        self.assertEqual(rec.get("expected_min"), 10,
                         "the floor is the new playlist's inline count, not the old record's")
        # ...and this create really did land every copy, so its own record may say so.
        self.assertIs(rec.get("complete"), True)

    def test_a_top_up_persists_the_plan_its_positions_index(self):
        """The top-up writes against the CURRENT match, which can be a different plan from
        the record's. A position means nothing without its plan, so both are persisted
        before the first send: a restart must not freeze the wrong copies."""
        old = [vid(i) for i in range(10, 16)]          # the record's older plan
        new = [vid(i) for i in range(20, 26)]          # this run's match
        upstream = Upstream()
        upstream.playlist = list(new[:5])              # the new plan, one copy absent
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                "yt_id": PLID, "url": "https://example.invalid/p",
                "title": "Repair Test", "expected": 6, "complete": True,
                "video_ids": old, "plan_slots": old}}, f)

        def mismatched(chunk, workers=8):
            return [{"ok": True, "query": (t or {}).get("title", ""),
                     "videoId": new[int(re.match(r"Track (\d+)$", t["title"]).group(1)) - 1]}
                    for t in chunk]

        with mock.patch("urllib.request.urlopen", upstream), \
                mock.patch.object(backend, "fetch_spotify_tracks",
                                  fake_fetch(tracks_for(6))), \
                mock.patch.object(backend, "yt_search_batch", mismatched):
            rid = backend.run_begin("", "")
            backend._transfer_thread(SPOTIFY_URL, COOKIE, "", "PRIVATE", rid)

        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("video_ids"), new,
                         "the record still describes the older plan its positions no longer mean")
        self.assertEqual(rec.get("plan_slots"), new)

    def test_the_append_floor_starts_at_the_reading_read(self):
        """A stored floor can be older and lower than the read that authorises a pass.
        The pass must start from what that read actually showed, or a later short read
        passes as complete."""
        a, b, c = vid(1), vid(2), vid(3)
        scripted = [[a, b, c], [a]]        # the authorising read, then a stale page
        calls = []

        def reader(plid, cookie):
            calls.append(plid)
            entries = scripted[min(len(calls) - 1, len(scripted) - 1)]
            return {"entries": list(entries), "complete": True, "why": ""}

        upstream = Upstream()
        upstream.playlist = [a, b, c]
        with mock.patch("urllib.request.urlopen", upstream), \
                mock.patch.object(backend, "yt_playlist_read", reader):
            # floor=0 is the stale stored value; the read proves 3.
            attempts, entries, complete, expected_min = backend._append_verify(
                PLID, [a, b, c], [], COOKIE, None, [a, b, c], True, floor=0)
        self.assertEqual(expected_min, 3,
                         "the floor ignored the entries the authorising read returned")

    def test_a_corrupt_record_fails_closed_instead_of_creating_a_playlist(self):
        """An unreadable created.json is not an empty one: a run that treated it as empty
        would create a second playlist and then overwrite the record it could not read."""
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            f.write("{not json")
        upstream = Upstream()
        with self.assertRaises(backend.RecordUnreadable):
            backend.transfer_create_append(COOKIE, "Repair Test", "",
                                           [vid(1), vid(2)], "PRIVATE", "SPOTREPAIR")
        self.assertEqual(upstream.creates, 0, "a playlist was created over a bad record")

    def test_a_missing_record_is_still_a_first_run(self):
        """The control: no file at all is a first run, and it creates."""
        upstream = Upstream()
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend.transfer_create_append(COOKIE, "Repair Test", "",
                                                 [vid(1), vid(2)], "PRIVATE", "SPOTREPAIR")
        self.assertEqual(out["playlistId"], PLID)
        self.assertEqual(upstream.creates, 1)

    def test_a_lost_create_reply_never_advises_a_retry(self):
        """The create may have made a playlist whose id we never saw. Retrying would make
        a second one, so the failure names the manual check and the record keeps the
        intent: a later run refuses to create over it."""
        class LostReply(Upstream):
            def __call__(self, req, timeout=None):
                if "playlist/create" in (getattr(req, "full_url", "") or ""):
                    raise urllib.error.URLError("reply lost")
                return super().__call__(req, timeout)

        upstream = LostReply()
        with mock.patch("urllib.request.urlopen", upstream):
            with self.assertRaises(RuntimeError) as caught:
                backend.transfer_create_append(COOKIE, "Repair Test", "",
                                               [vid(1), vid(2)], "PRIVATE", "SPOTREPAIR")
        msg = str(caught.exception).lower()
        self.assertIn("library", msg, "the failure does not send the user to check YTM")
        self.assertNotIn("retry", msg, "the failure advises a retry that would duplicate")
        self.assertEqual(upstream.creates, 0)
        rec = self.created()["SPOTREPAIR"]
        self.assertTrue(rec.get("create_pending"), "the create intent was not recorded")
        # ...and a second attempt refuses instead of creating another playlist.
        with mock.patch("urllib.request.urlopen", Upstream()):
            with self.assertRaises(RuntimeError) as again:
                backend.transfer_create_append(COOKIE, "Repair Test", "",
                                               [vid(1), vid(2)], "PRIVATE", "SPOTREPAIR")
        self.assertIn("second one", str(again.exception).lower())

    def test_a_top_up_adds_a_track_the_source_gained(self):
        """A record that was complete for five tracks is topped up against a six-track
        plan (the source gained a song). The read must reach what the LAST run proved,
        not what this run intends: demanding six entries refuses the new track by
        definition, because the playlist cannot hold it yet.

        Pinned on the synchronous top-up (`/api/create-playlist`), which has no convergence
        pass behind it: there, a refused read is a track the user never gets."""
        old = [vid(i) for i in range(1, 6)]
        new = old + [vid(6)]
        upstream = Upstream()
        upstream.playlist = list(old)              # holds exactly what the last run proved
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                "yt_id": PLID, "url": "https://example.invalid/p",
                "title": "Repair Test", "expected": 5, "complete": True,
                "video_ids": old, "plan_slots": old, "expected_min": 5}}, f)

        with mock.patch("urllib.request.urlopen", upstream):
            out = backend.transfer_create_append(COOKIE, "Repair Test", "", new,
                                                 "PRIVATE", "SPOTREPAIR")

        self.assertEqual(upstream.sends, [vid(6)],
                         "the new track was refused because the read could not hold it yet")
        self.assertEqual(out["added"], 6, out)
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("video_ids"), new, "the record did not adopt the new plan")
        self.assertIs(rec.get("complete"), True)

    def test_a_saved_unknown_position_is_translated_through_its_own_plan(self):
        """A top-up runs against a fresh match, so a saved position can name a different
        song in the new plan. The unknown copy keeps its song: the song it belongs to is
        frozen, and the song that happens to sit at that position now is not."""
        a, b, c = vid(1), vid(2), vid(3)
        rec = {"video_ids": [a, b, c], "unsure": [0], "expected_min": 2}
        floor, frozen, ids, need = backend._persisted_unknown(rec, [b, a, c])
        self.assertEqual(floor, 2)
        self.assertEqual(frozen, {1},
                         "the saved position was applied to the new plan as-is")
        self.assertEqual(ids, {a}, "the unknown copy lost its durable id")

    def test_an_unknown_id_survives_a_plan_that_drops_it(self):
        """A record knows A's write was unknown. The next match no longer contains A, so
        the adopted plan has no position for it - but the unknown must survive as an ID,
        or a later run that matches A again would send it. Reintroduced here: it is
        frozen and never sent."""
        a, b, c = vid(1), vid(2), vid(3)
        rec = {"video_ids": [a, b, c], "unsure": [0], "expected_min": 2}
        floor, frozen, ids, need = backend._persisted_unknown(rec, [b, c])
        self.assertEqual(frozen, set(), "an id with no position cannot be frozen by name")
        self.assertEqual(ids, {a}, "the unknown was forgotten when the plan dropped it")
        # ...and a later plan that names A again inherits the freeze.
        floor2, frozen2, ids2, _ = backend._persisted_unknown(
            {"video_ids": [a, b, c], "unsure_ids": sorted(ids)}, [c, b, a])
        self.assertEqual(frozen2, {2}, "the reintroduced copy was not frozen")
        self.assertEqual(ids2, {a})

    def test_one_visible_copy_does_not_resolve_an_unknown_second_copy(self):
        """The record says [A,A] and one copy's write is unknown. A complete read showing
        one A answers nothing: the doubt exists precisely because the second copy may be
        the one that landed. Resolving on a smaller plan would let a later run add A
        again, making three."""
        a = vid(1)
        need = {a: 2}
        self.assertEqual(backend._unresolved_ids({a}, [a], need=need), {a},
                         "one visible copy resolved an unknown second copy")
        # The original evidence - two copies seen - does resolve it.
        self.assertEqual(backend._unresolved_ids({a}, [a, a], need=need), set())
        # A doubt with no recorded multiplicity is never cleared: the evidence that
        # could answer it was not kept.
        self.assertEqual(backend._unresolved_ids({a}, [a, a, a]), {a},
                         "an unprovable doubt was cleared by a read that cannot prove it")

    def test_an_unknown_id_is_not_forgotten_across_plans_and_is_never_resent(self):
        """The durable rule, end to end: A's write was unknown, the next match excludes A
        (so the adopted plan has no slot for it), and the match after that brings A back.
        If the record forgot A when the plan dropped it, that last run would send A again
        - a second copy of a song whose first copy nobody ever saw land."""
        a, b, c = vid(1), vid(2), vid(3)
        upstream = Upstream()
        upstream.playlist = [b, c]
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                "yt_id": PLID, "url": "https://example.invalid/p",
                "title": "Repair Test", "expected": 3, "complete": True,
                "video_ids": [a, b, c], "plan_slots": [a, b, c],
                "unsure": [0], "unsure_ids": [a], "expected_min": 2}}, f)

        with mock.patch("urllib.request.urlopen", upstream):
            # Match 1: A is gone from the source.
            backend.transfer_create_append(COOKIE, "Repair Test", "", [b, c],
                                           "PRIVATE", "SPOTREPAIR")
            rec = self.created()["SPOTREPAIR"]
            self.assertEqual(rec.get("video_ids"), [b, c])
            self.assertEqual(rec.get("unsure_ids"), [a],
                             "the unknown was dropped when the plan stopped naming it")
            self.assertEqual(upstream.sends, [])

            # Match 2: A is back.
            backend.transfer_create_append(COOKIE, "Repair Test", "", [a, b, c],
                                           "PRIVATE", "SPOTREPAIR")

        self.assertEqual(upstream.sends, [],
                         "a song whose earlier write was unknown was sent again")
        self.assertEqual(upstream.playlist.count(a), 0)
        rec = self.created()["SPOTREPAIR"]
        self.assertIn(a, rec.get("unsure_ids") or [], "the unknown was resolved for free")

    def test_a_rematch_cannot_resend_the_song_whose_write_was_unknown(self):
        """End to end: the record knows A's write was unknown, the fresh match puts A at a
        different position, and A is missing from the read. The top-up must not send A -
        and must not freeze B, which is on the playlist."""
        a, b, c = vid(1), vid(2), vid(3)
        upstream = Upstream()
        upstream.playlist = [b, c]                  # A absent from the read
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                "yt_id": PLID, "url": "https://example.invalid/p",
                "title": "Repair Test", "expected": 3, "complete": True,
                "video_ids": [a, b, c], "plan_slots": [a, b, c],
                "unsure": [0], "expected_min": 2}}, f)

        # The fresh match reorders: A is now the second copy of the plan.
        def rematched(chunk, workers=8):
            order = [b, a, c]
            return [{"ok": True, "query": (t or {}).get("title", ""),
                     "videoId": order[int(re.match(r"Track (\d+)$", t["title"]).group(1)) - 1]}
                    for t in chunk]

        with mock.patch("urllib.request.urlopen", upstream), \
                mock.patch.object(backend, "fetch_spotify_tracks",
                                  fake_fetch(tracks_for(3))), \
                mock.patch.object(backend, "yt_search_batch", rematched):
            rid = backend.run_begin("", "")
            backend._transfer_thread(SPOTIFY_URL, COOKIE, "", "PRIVATE", rid)
        snap = backend.run_snapshot()

        self.assertEqual(upstream.sends, [],
                         "a song whose write was unknown was sent again after a rematch")
        self.assertEqual(snap["state"], "gaveup", snap)
        self.assertIn("outcome is unknown", snap["reason"])
        self.assertEqual(snap["counts"]["unverified"], 1, snap["counts"])
        rec = self.created()["SPOTREPAIR"]
        # The unknown is now associated with A's position in the adopted plan.
        self.assertEqual(rec.get("video_ids"), [b, a, c])
        self.assertEqual(rec.get("unsure"), [1],
                         "the unknown stayed in the old plan's coordinates")

    def test_a_sync_top_up_never_persists_complete_over_an_unknown_copy(self):
        """The synchronous top-up has no convergence pass behind it, so its own record
        write is the only one: an unknown copy may not be reported as a whole playlist."""
        plan = [vid(i) for i in range(1, 6)]
        upstream = Upstream()
        upstream.playlist = list(plan[:4])         # one copy absent, one write unknown
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                "yt_id": PLID, "url": "https://example.invalid/p",
                "title": "Repair Test", "expected": 5, "complete": True,
                "video_ids": plan, "plan_slots": plan,
                "unsure": [4], "expected_min": 4}}, f)

        with mock.patch("urllib.request.urlopen", upstream):
            out = backend.transfer_create_append(COOKIE, "Repair Test", "", plan,
                                                 "PRIVATE", "SPOTREPAIR")

        self.assertEqual(upstream.sends, [], "the unknown copy was re-sent")
        self.assertIs(self.created()["SPOTREPAIR"].get("complete"), False,
                      "a run with an unknown copy persisted complete")
        self.assertEqual(out.get("missing"), 1, out)

    def test_a_partial_read_never_lets_a_top_up_declare_complete(self):
        """The top-up has no convergence pass behind it, so its own write is the last word
        on the record. A read that stopped before the end of the playlist proves nothing
        about its tail, even when every copy it did show is there."""
        plan = [vid(i) for i in range(1, 4)]
        upstream = Upstream()
        upstream.playlist = list(plan)
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                "yt_id": PLID, "url": "https://example.invalid/p",
                "title": "Repair Test", "expected": 3, "complete": True,
                "video_ids": plan, "plan_slots": plan, "expected_min": 3}}, f)

        with mock.patch("urllib.request.urlopen", upstream), \
                mock.patch.object(backend, "_read_back_retry",
                                  lambda *a, **k: (list(plan), False)):
            backend.transfer_create_append(COOKIE, "Repair Test", "", plan,
                                           "PRIVATE", "SPOTREPAIR")

        self.assertEqual(upstream.sends, [])
        self.assertIs(self.created()["SPOTREPAIR"].get("complete"), False,
                      "a partial read was recorded as a whole playlist")

    def test_two_visible_copies_do_prove_a_repeat_landed(self):
        """Duplicates are allowed, so an inline repeat is not automatically unverified. A
        read cannot say WHICH copy landed, but it can say HOW MANY: when it holds two, the
        plan's two copies are satisfied and nothing is left unknown."""
        tracks = tracks_for(4, repeat_of=3)      # 5 rows, rows 3 and 5 are one videoId
        upstream = Upstream(create_keeps_repeats=True)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["state"], "done", snap)
        self.assertEqual(snap["counts"]["added"], 5, snap["counts"])
        self.assertEqual(snap["counts"]["unverified"], 0, snap["counts"])
        self.assertEqual(upstream.playlist.count(vid(3)), 2)
        self.assertEqual(upstream.sends, [], "a copy the read proved was sent again")
        self.assertIs(self.created()["SPOTREPAIR"]["complete"], True)

    def test_the_write_ahead_persists_the_multiplicity_it_queues(self):
        """A slice is written ahead before its first add. If the process dies there, the
        next run inherits those positions as unknowns - and an unknown with no recorded
        multiplicity can never be resolved, so a complete read showing both copies would
        still leave the song frozen forever. The count goes on disk in the same write."""
        a = vid(1)
        self.assertTrue(backend._write_ahead("SPOTREPAIR", [0, 1], 0, plan=[a, a]))
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("inflight"), [0, 1])
        self.assertEqual((rec.get("inflight_need") or {}).get(a), 2,
                         "the write-ahead queued copies without their multiplicity")
        floor, frozen, ids, need = backend._persisted_unknown(rec, [a, a])
        self.assertEqual(ids, {a})
        self.assertEqual(backend._unresolved_ids(ids, [a, a], need=need), set(),
                         "a read holding every copy could not clear the unknown")
        self.assertEqual(frozen, {0, 1})

    def test_a_crashed_slices_copy_clears_when_a_read_shows_every_copy(self):
        """End to end on the resume path: the write-ahead left one copy of a repeated song
        queued, the playlist really holds both, and the complete read says so. The copy is
        proven, not unverified - the run must not leave a doubt it can answer."""
        a = vid(1)
        upstream = Upstream(accept_repeats=True)
        upstream.playlist = [a, a]
        backend._write_ahead("SPOTREPAIR", [0, 1], 0, plan=[a, a])
        backend._persist_fields("SPOTREPAIR", yt_id=PLID,
                                url="https://example.invalid/p", title="Repair Test",
                                expected=2)
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend.transfer_create_append(COOKIE, "Repair Test", "", [a, a],
                                                 "PRIVATE", "SPOTREPAIR")

        self.assertEqual(upstream.sends, [], "a copy the read proved was sent again")
        self.assertEqual(out.get("unsure"), [], out.get("unsure"))
        self.assertEqual(out.get("added"), 2, out)
        rec = self.created()["SPOTREPAIR"]
        self.assertIs(rec.get("complete"), True, "a proven run left the record incomplete")
        self.assertEqual(rec.get("unsure_ids") or [], [])

    def test_a_done_run_never_leaves_its_record_calling_a_copy_unknown(self):
        """The contradiction: `_converge` returned done while its own record said a copy
        was unknown, because the read had accounted for that copy and the frozen set was
        never re-examined. A complete read that holds both copies answers the doubt, so
        the record and the verdict agree - and the run is done on evidence, not on a
        count that ignores the doubt."""
        a = vid(1)
        upstream = Upstream(accept_repeats=True)
        upstream.playlist = [a, a]
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, [a, a], spotify_id="SPOTREPAIR",
                                    title="Repair Test",
                                    url="https://example.invalid/p", unsure=[1])

        self.assertEqual(out["state"], "done", out)
        rec = self.created()["SPOTREPAIR"]
        self.assertIs(rec.get("complete"), True,
                      "the run said done while its record said a copy was unknown")
        self.assertEqual(rec.get("unsure_ids") or [], [])

    def test_a_done_run_refuses_when_the_doubt_cannot_be_answered(self):
        """The other half of the same rule: when the read does not hold enough copies to
        answer the doubt, the run may not say done. It ends unresolved and names it."""
        a = vid(1)
        upstream = Upstream(accept_repeats=True)
        upstream.playlist = [a]                  # one copy seen, two in doubt
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, [a, a], spotify_id="SPOTREPAIR",
                                    title="Repair Test",
                                    url="https://example.invalid/p", unsure=[1])

        self.assertEqual(out["state"], "gaveup", out)
        self.assertIn(a, out["residual"])
        self.assertIs(self.created()["SPOTREPAIR"].get("complete"), False)

    # --- 6d: the record is the safety net, so it fails closed ---------------
    def test_a_write_ahead_that_cannot_be_saved_sends_nothing(self):
        """A run whose record cannot be written does not know what a restart will be
        told about its writes, so it may not write at all: the append never leaves, and
        the run says why."""
        tracks = tracks_for(55)
        upstream = Upstream()
        real_save = backend.save_created

        def refuse_write_ahead(d):
            rec = (d or {}).get("SPOTREPAIR") or {}
            if rec.get("inflight"):
                return False            # the write-ahead of a slice cannot be saved
            return real_save(d)

        with mock.patch.object(backend, "save_created", refuse_write_ahead):
            snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.sends, [],
                         "the run appended without a durable resume record")
        self.assertEqual(snap["phase"], "failed", snap)
        self.assertIn("resume record", snap["error"])
        self.assertEqual(snap["counts"]["unverified"], 5, snap["counts"])

    def test_a_top_up_honours_an_unknown_write_from_the_record(self):
        """The complete-record branch used to read, then append, trusting `complete`.
        A run interrupted while topping up leaves that flag set, so the unknown copies
        the record carries must be honoured here exactly as a resume honours them."""
        plan = [vid(i) for i in range(1, 7)]
        upstream = Upstream()
        upstream.playlist = list(plan[:5])        # copy 6 absent
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                "yt_id": PLID, "url": "https://example.invalid/p",
                "title": "Repair Test", "expected": 6, "complete": True,
                "video_ids": plan, "plan_slots": plan,
                "inflight": [5], "expected_min": 5}}, f)
        snap = self.transfer(upstream, tracks_for(6))

        self.assertEqual(upstream.sends, [],
                         "a top-up re-sent a copy whose write was unknown")
        self.assertEqual(snap["state"], "gaveup", snap)
        self.assertIn("outcome is unknown", snap["reason"])
        self.assertEqual(snap["counts"]["unverified"], 1, snap["counts"])
        self.assertIs(self.created()["SPOTREPAIR"]["complete"], False)

    def test_a_new_slice_never_erases_an_earlier_write_ahead(self):
        """A slice's write-ahead merges with the queue instead of replacing it, and a
        slice only removes the positions it classified. A note that fails to save
        therefore leaves the queue for the next slice to carry, not to overwrite."""
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION, "yt_id": PLID, "complete": False}}, f)

        self.assertTrue(backend._write_ahead("SPOTREPAIR", [3], 1))
        self.assertEqual(self.created()["SPOTREPAIR"]["inflight"], [3])
        # The note of that slice is lost (the save fails), then the next slice queues.
        with mock.patch.object(backend, "save_created", lambda d: False):
            backend._note_outcomes("SPOTREPAIR", [3], [3], 1)
        backend._write_ahead("SPOTREPAIR", [7], 2)

        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["inflight"], [3, 7],
                         "a new write-ahead erased the queue the failed note left")
        # Only positions that were classified leave the queue.
        backend._note_outcomes("SPOTREPAIR", [7], [7], 3)
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["inflight"], [3])
        self.assertEqual(rec["unsure"], [7])
        self.assertEqual(rec["expected_min"], 3)

    def test_the_created_floor_is_on_disk_before_the_first_read_back(self):
        """The create's inline batch is a write the playlist already accepted: the floor
        it establishes (one entry per distinct inline id) must be on disk before the
        read-back, or a crash in that window lets a restart read a short playlist as a
        plan and append the rest of it."""
        upstream = Upstream(browse_broken=True)   # the create works, every read fails
        snap = self.transfer(upstream, tracks_for(60))

        self.assertEqual(snap["phase"], "failed", snap.get("error"))
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("expected_min"), 50,
                         "the inline floor was not recorded at create time")
        self.assertIs(rec.get("complete"), False)

    def test_an_unknown_write_freezes_every_copy_of_that_song(self):
        """A playlist read cannot tell two copies of one videoId apart, so an unknown
        write on one occurrence makes the others unattributable too: none of them is
        sent, and each is named. Sending the other copy could be the second row of a
        song the playlist already holds twice."""
        a = vid(1)
        upstream = Upstream(accept_repeats=True)
        upstream.playlist = []                    # both copies absent from the read
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, [a, a], unsure=[1])

        self.assertEqual(upstream.sends, [],
                         "another copy of a song with an unknown write was sent")
        self.assertEqual(out["state"], "gaveup", out)
        self.assertEqual(out["residual"], [a, a])

    def test_a_later_write_never_lowers_the_persisted_floor(self):
        """The record's floor only rises. A repair pass that computed its own, older
        floor - or a progress write with a stale one - must not put it back over a
        floor another writer already proved: a restart reading a low floor treats a
        short playlist as complete."""
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION, "yt_id": PLID, "complete": False,
                                      "expected_min": 50, "unsure": [3]}}, f)

        backend._persist_progress("SPOTREPAIR", PLID, "https://example.invalid/p",
                                  "Repair Test", 6, state="repairing", attempt=1,
                                  expected_min=2, unsure=[])
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["expected_min"], 50, "a stale floor was written back")
        self.assertEqual(rec["unsure"], [3], "a persisted unknown was dropped")

    # --- 6c: what the run knows is on disk, so a restart cannot guess ---------
    def test_every_slice_is_written_ahead_before_it_is_sent(self):
        """A close mid-slice must not turn copies the run already sent into copies it
        never sent. Each slice's positions go on disk before its first add, and are
        replaced by its outcomes after it."""
        tracks = tracks_for(55)
        upstream = Upstream()
        seen = []
        real_rec = backend.load_created

        def watch():
            rec = (real_rec() or {}).get("SPOTREPAIR") or {}
            seen.append((list(rec.get("inflight") or []), int(rec.get("expected_min") or 0)))
            return real_rec()

        with mock.patch.object(backend, "load_created", watch):
            snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        # The five copies beyond the inline create are written ahead as one slice.
        self.assertIn(([50, 51, 52, 53, 54], 50), seen, seen)
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("inflight"), [], "the write-ahead list was never cleared")
        self.assertEqual(rec.get("expected_min"), 55)
        self.assertEqual(rec.get("unsure"), [])

    def test_a_resume_does_not_resend_a_copy_whose_write_was_unknown(self):
        """The record says which copies were written with an unknown outcome. A resume
        is automatic, so it may not send them again: it freezes them and says so. Both
        the outcome list and the write-ahead list of a slice cut short count."""
        for field in ("unsure", "inflight"):
            with self.subTest(field=field):
                upstream = Upstream()
                upstream.playlist = [vid(i) for i in range(1, 6)]   # copy 6 is absent
                with open(backend.CREATEDF, "w", encoding="utf-8") as f:
                    json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                        "yt_id": PLID, "url": "https://example.invalid/p",
                        "title": "Repair Test", "expected": 6, "complete": False,
                        "state": "verifying", "attempt": 0, "next_run_at": 0,
                        "video_ids": [vid(i) for i in range(1, 7)],
                        "plan_slots": [vid(i) for i in range(1, 7)],
                        "pending": [], field: [5]}}, f)
                snap = self.transfer(upstream, tracks_for(6))

                self.assertEqual(upstream.appends, [],
                                 "a resume re-sent a copy whose write was %s" % field)
                self.assertEqual(snap["state"], "gaveup", snap)
                self.assertEqual(snap["counts"]["unverified"], 1, snap["counts"])
                self.assertEqual(snap["residual"], [vid(6)])
                self.assertIn("outcome is unknown", snap["reason"])
                self.assertIs(self.created()["SPOTREPAIR"]["complete"], False)

    def test_a_resume_restores_the_accepted_floor(self):
        """The floor a run proved is on disk. A resume that forgot it would take a
        short read for a whole playlist and append a copy that is already there."""
        upstream = Upstream()
        upstream.playlist = [vid(i) for i in range(1, 6)]      # holds 5, floor says 6
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                "yt_id": PLID, "url": "https://example.invalid/p",
                "title": "Repair Test", "expected": 6, "complete": False,
                "state": "verifying", "attempt": 0, "next_run_at": 0,
                "video_ids": [vid(i) for i in range(1, 7)],
                "plan_slots": [vid(i) for i in range(1, 7)],
                "pending": [], "expected_min": 6}}, f)
        snap = self.transfer(upstream, tracks_for(6))

        self.assertEqual(upstream.appends, [],
                         "a resume appended off a read below the floor it inherited")
        self.assertEqual(snap["state"], "gaveup", snap)
        self.assertTrue(self.waits, "the run did not stand by on the short read")
        self.assertEqual(self.waits[0]["snap"]["reason"], "read incomplete")

    def test_a_resume_without_a_floor_still_repairs_what_is_missing(self):
        """The control for the two above: a clean record's resume does send the absent
        copy, so the freezes are what stop it, not the resume itself."""
        upstream = Upstream()
        upstream.playlist = [vid(i) for i in range(1, 6)]
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                "yt_id": PLID, "url": "https://example.invalid/p",
                "title": "Repair Test", "expected": 6, "complete": False,
                "state": "verifying", "attempt": 0, "next_run_at": 0,
                "video_ids": [vid(i) for i in range(1, 7)],
                "plan_slots": [vid(i) for i in range(1, 7)], "pending": []}}, f)
        snap = self.transfer(upstream, tracks_for(6))

        self.assertEqual(upstream.appends, [vid(6)])
        self.assertEqual(snap["state"], "done", snap)
        self.assertEqual(snap["counts"]["added"], 6, snap["counts"])

    # --- 7: a short complete read is short, not complete --------------------
    def test_a_token_complete_read_below_the_minimum_is_not_complete(self):
        """Three entries served with no continuation is a token-complete read; when
        five are expected it proves the playlist is short, not that it is whole."""
        upstream = Upstream()
        upstream.playlist = ["A%010d" % i for i in range(3)]
        with mock.patch("urllib.request.urlopen", upstream):
            entries, complete = backend._read_back_retry(PLID, COOKIE, tries=2, minimum=5)
        self.assertEqual(entries, ["A%010d" % i for i in range(3)])
        self.assertFalse(complete, "a read below the expected minimum called itself complete")
        # No minimum (the window straight after a create) and the same read is the
        # honest answer, so the floor is never invented.
        upstream.reads = 0
        with mock.patch("urllib.request.urlopen", upstream):
            entries, complete = backend._read_back_retry(PLID, COOKIE, tries=2, minimum=0)
        self.assertTrue(complete, "an unknown minimum was treated as a short read")

    def test_a_repair_read_shorter_than_the_last_one_is_incomplete(self):
        """A playlist does not shrink during a run: a complete read that lost an
        entry is a stale page, so the repair waits instead of trusting it."""
        scripted = [[vid(1), vid(2), vid(3), vid(4)], [vid(1), vid(2), vid(3)]]
        calls = []

        def reader(plid, cookie):
            calls.append(plid)
            entries = scripted[min(len(calls) - 1, len(scripted) - 1)]
            return {"entries": list(entries), "complete": True, "why": ""}

        upstream = Upstream()
        self.upstream = upstream
        with mock.patch("urllib.request.urlopen", upstream), \
                mock.patch.object(backend, "yt_playlist_read", reader):
            backend._converge(PLID, COOKIE, [vid(i) for i in range(1, 6)])

        self.assertTrue(self.waits, "a read that shrank was trusted")
        self.assertEqual(self.waits[0]["snap"]["reason"], "read incomplete")
        self.assertEqual(self.waits[0]["appends"], [vid(5)], "one send for the absent copy")
        self.assertGreaterEqual(len(scripted), 2)

    def test_a_sent_row_is_pending_until_a_completed_read_confirms_it(self):
        """An accepted add is not an add. Until a complete read shows the copy, the
        row is pending, and the live breakdown counts exactly what the rows say."""
        tracks = tracks_for(60)
        upstream = Upstream(drop={vid(60)})     # accepted, never stored
        snap = self.transfer(upstream, tracks)

        self.assertTrue(self.waits, "the run never stood by")
        stood = self.waits[0]["snap"]
        rows = stood["rows"]
        landed = [r for r in rows if r["state"] == "landed"]
        pending = [r for r in rows if r["state"] in ("pending", "appending")]
        self.assertEqual(rows[-1]["state"], "pending",
                         "an accepted add was marked landed before any read proved it")
        self.assertTrue(pending, "no row was waiting on a read-back")
        # The live breakdown is recounted from the same rows, never accumulated.
        self.assertEqual(stood["counts"]["added"], len(landed), stood["counts"])
        self.assertEqual(stood["counts"]["pending"], len(pending), stood["counts"])
        self.assertEqual(stood["counts"]["unverified"], 0, stood["counts"])
        self.assertEqual(stood["landed"], len(landed))
        # And the copy no read ever proved never lands.
        self.assertNotEqual(snap["rows"][-1]["state"], "landed")
        self.assertEqual(snap["landed"], 59)

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
