#!/usr/bin/env python3
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

DUP_REPLY = {"status": "STATUS_FAILED", "actions": [{"addToToastAction": {"item": {
    "notificationTextRenderer": {"successResponseRenderer": {"text": {
        "runs": [{"text": "This song is already in the playlist"}]}}}}}}]}


def vid(i: int) -> str:
    return "R%010d" % i


def tracks_for(n: int, repeat_of=None):
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
            raise urllib.error.HTTPError(url, self.fatal, "refused", {}, None)
        if self.throttled < self.throttle:
            self.throttled += 1
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        step = self.add_script.pop(0) if self.add_script else ""
        self.appends.append(v)
        if step == "lost" or v in self.lose_before_apply:
            self.lose_before_apply.discard(v)
            raise urllib.error.URLError("reply lost before it was applied")
        self.actions.append(dict((body.get("actions") or [{}])[0]))
        if step == "failed":
            return Resp({"status": "STATUS_FAILED"})
        stored = self.relabel.get(v, v)
        if stored in self.playlist and not (self.accept_repeats
                                           and self.actions[-1].get("dedupeOption") == "DEDUPE_OPTION_SKIP"):
            return Resp(DUP_REPLY)
        if v not in self.drop:
            self.playlist.append(stored)
        if v in self.lose_reply:
            self.lose_reply.discard(v)
            if self.fail_read_after_loss:
                self._fail_next_read = True
            raise urllib.error.URLError("reply lost")
        return Resp({"status": "STATUS_SUCCEEDED"})

    def browse(self, body):
        self.reads += 1
        if self.crash_on_read and self.reads == self.crash_on_read:
            raise Crash("process died between the add and its proof")
        if self.browse_broken:
            raise urllib.error.URLError("browse failed")
        if getattr(self, "_fail_next_read", False):
            self._fail_next_read = False
            raise urllib.error.URLError("browse failed after a lost reply")
        held = list(self.playlist)
        token = body.get("continuation")
        if not token and self.empty_reads:
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

    def test_a_429_from_an_append_is_an_unknown_write_not_success(self):
        def throttled(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

        with mock.patch("urllib.request.urlopen", throttled):
            st = backend.yt_add_one(PLID, vid(1), COOKIE)
        self.assertTrue(str(st).startswith(backend.STATUS_UNSURE), st)
        self.assertNotIn("SUCCEEDED", str(st).upper())
        self.assertTrue(backend.is_unsure_status(st))
        self.assertFalse(hasattr(backend, "is_retry_status"),
                         "a predicate that authorises re-sending an append is back")

    def test_a_refused_copy_keeps_the_record_incomplete(self):
        upstream = Upstream()                       # refuses a second copy of an id
        with mock.patch("urllib.request.urlopen", upstream):
            backend._create_new(COOKIE, "Repair Test", "", [vid(i) for i in
                                                           (1, 2, 3, 4, 3)], "PRIVATE", "SPOTREPAIR")
        rec = self.created()["SPOTREPAIR"]
        self.assertIs(rec["complete"], False,
                      "the create's record counts a refused copy as complete")

    def test_a_partial_read_keeps_the_record_incomplete(self):
        class PlanFirst(Upstream):

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

    def test_a_repeat_inside_the_inline_create_is_never_re_sent(self):
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
        self.assertEqual(upstream.playlist.count(vid(3)), 1)
        self.assertEqual(upstream.appends.count(vid(3)), 0,
                         "a copy the create carried twice was sent again")
        self.assertEqual(snap["counts"]["unverified"], 1, snap["counts"])
        self.assertIn("unverified", snap["summary"])
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(snap["residual"], [vid(3)])
        self.assertIs(self.created()["SPOTREPAIR"]["complete"], False)

    def test_a_stale_read_cannot_resend_an_inline_repeat(self):
        a = vid(1)
        upstream = Upstream(accept_repeats=True)
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend._converge(PLID, COOKIE, [a, a], refused=[], unsure=[0, 1])
        self.assertEqual(out["state"], "gaveup", out)
        self.assertEqual(upstream.sends, [],
                         "a copy the create carried twice was re-sent")
        self.assertEqual(out["residual"], [a, a])

    def test_a_throttled_append_is_named_unverified_and_never_re_sent(self):
        tracks = tracks_for(60)                   # 50 inline at create, 10 appended
        upstream = Upstream(throttle=2)           # the first two appends answer 429
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["phase"], "done", snap.get("error"))
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(self.waits, [], "no append is waited out: a wait is not a proof")
        self.assertEqual(snap["counts"]["unverified"], 2, snap["counts"])
        self.assertEqual(snap["counts"]["added"], 58, snap["counts"])
        self.assertEqual(snap["residual"], [vid(51), vid(52)])
        self.assertEqual(len(upstream.sends), len(set(upstream.sends)),
                         "a copy was sent twice")
        self.assertEqual(upstream.playlist,
                         [vid(i) for i in list(range(1, 51)) + list(range(53, 61))])
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["state"], "gaveup")
        self.assertIs(rec["complete"], False)
        self.assertEqual(rec["unsure"], [50, 51])
        self.assertEqual(rec["expected_min"], 58)

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
        self.assertEqual(stood["appends"], [])
        self.assertEqual(upstream.appends, [])
        self.assertEqual(snap["unverified"], 10)
        self.assertEqual(snap["refused"], 0)
        self.assertEqual(snap["missing"], 0)
        self.assertIn("10 unverified", snap["summary"])
        self.assertEqual(len(upstream.playlist), 110)
        self.assertEqual(len(self.waits), backend.RUN_RETRY_MAX)
        self.assertEqual(snap["state"], "gaveup")
        self.assertEqual(snap["residual"], [vid(i) for i in range(31, 41)])

    def test_an_incomplete_read_back_holds_back_a_pending_repair(self):
        tracks = tracks_for(60)
        extras = ["X%010d" % i for i in range(70)]      # 130 entries: two pages
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

    def test_a_dropped_append_terminates_instead_of_spinning(self):
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
        self.assertLessEqual(upstream.appends.count(vid(60)), 2, upstream.appends)

    def test_a_copy_the_read_never_shows_is_named_and_never_re_sent(self):
        tracks = tracks_for(60)
        hidden = vid(60)
        upstream = Upstream(relabel={hidden: "Z0000000060"}, accept_repeats=True)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["state"], "gaveup", snap.get("reason"))
        self.assertIn(hidden, snap["residual"])
        self.assertEqual(upstream.sends.count(hidden), 1,
                         "a copy an already-accepted write covered was sent again: %r"
                         % upstream.sends)
        self.assertEqual(sorted(upstream.sends), sorted(set(upstream.sends)),
                         "a copy was written twice: %r" % upstream.sends)
        self.assertEqual(len(upstream.sends),
                         len(tracks) - backend.CREATE_INLINE_MAX,
                         "the run sent more appends than the plan has copies left: %r"
                         % upstream.sends)
        written = [v for v in upstream.playlist if v.startswith("R")]
        self.assertEqual(sorted(written), sorted(set(written)),
                         "a copy was written twice: %r" % upstream.playlist)
        row = [r for r in snap["rows"] if r.get("videoId") == hidden][0]
        self.assertTrue(row.get("unsure"), row)
        self.assertTrue(row.get("unverified"), row)
        self.assertIn("check the playlist", row.get("status", ""))
        rec = self.created()["SPOTREPAIR"]
        self.assertIs(rec["complete"], False)
        self.assertEqual(rec.get("unsure_ids"), [hidden], rec.get("unsure_ids"))
        self.assertEqual(rec.get("unsure_need"), {hidden: 1})

    def test_a_resume_after_an_unnameable_write_sends_nothing(self):
        tracks = tracks_for(60)
        hidden = vid(60)
        upstream = Upstream(relabel={hidden: "Z0000000060"}, accept_repeats=True)
        self.transfer(upstream, tracks)
        sends = len(upstream.sends)

        self.transfer(upstream, tracks)     # Ctrl+Enter: resume, never a second create
        self.assertEqual(len(upstream.sends), sends,
                         "the resume re-sent copies whose writes it could not see")
        self.assertEqual(upstream.creates, 1)

    def _legacy_record(self, n=12, held=6):
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
        after = self.created()["SPOTREPAIR"]
        self.assertEqual(after.get("yt_id"), PLID)
        self.assertEqual(after.get("video_ids"), rec["video_ids"])

    def test_a_legacy_record_that_says_complete_still_tops_up(self):
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
        self.assertEqual(upstream.sends, [vid(51)],
                         "the run kept writing after a fatal answer: %r" % upstream.sends)
        self.assertEqual(snap["missing"], 0,
                         "a copy the run never sent was counted as absent")
        not_sent = [r for r in snap["rows"] if r.get("not_sent")]
        self.assertEqual(len(not_sent), 9, not_sent)
        self.assertTrue(all("not sent" in r.get("status", "") for r in not_sent), not_sent)

    def test_a_crash_between_the_add_and_its_proof_keeps_the_copy_unknown(self):
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
        self.assertEqual(len(upstream.sends), len(set(upstream.sends)),
                         "a copy was re-sent after the record refused it: %r"
                         % upstream.sends)

    def test_a_fatal_answer_during_a_top_up_stops_the_run(self):
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
        after = self.created()["SPOTREPAIR"]
        self.assertEqual(after.get("yt_id"), PLID)
        self.assertEqual(after.get("unsure"), [3])
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
        self.assertEqual(upstream.sends, [vid(i) for i in range(51, 61)],
                         "an inline copy was re-sent: %r" % upstream.sends)
        self.assertEqual(upstream.creates, 1)

    def _write_legacy(self, rec):
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": rec}, f)

    def test_a_wire_that_refuses_every_append_is_bounded_and_names_each_copy(self):
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
        self.assertEqual(len(upstream.sends), 10, upstream.sends)
        self.assertEqual(len(upstream.sends), len(set(upstream.sends)))
        self.assertEqual(upstream.playlist, [vid(i) for i in range(1, 51)])
        self.assertEqual(upstream.creates, 1)
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["state"], "gaveup")
        self.assertIs(rec["complete"], False)
        self.assertEqual(rec["unsure"], list(range(50, 60)))
        self.assertEqual(rec["expected_min"], 50)

    def test_a_persisted_run_resumes_in_place_with_its_own_attempt_count(self):
        tracks = tracks_for(60)
        upstream = Upstream()                        # holds everything it is given
        upstream.playlist = [vid(i) for i in range(1, 56)]      # five copies still absent
        due = time.time() + 600
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
        self.assertTrue(self.waits, "the resume ignored the persisted retry time")
        stood = self.waits[0]
        self.assertEqual(stood["snap"]["state"], "standby")
        self.assertEqual(stood["snap"]["attempt"], 3)
        self.assertGreaterEqual(stood["delay"], 590, "the resume waited less than the record asked")
        self.assertEqual(stood["appends"], [], "the resume appended before its retry time")
        self.assertEqual(upstream.appends, [vid(i) for i in range(60, 55, -1)])
        self.assertTrue(upstream.actions, "no append was sent")
        for act in upstream.actions:
            self.assertEqual(act.get("dedupeOption"), "DEDUPE_OPTION_SKIP", act)

    def test_a_resume_uses_the_plan_the_interrupted_run_persisted(self):
        tracks = tracks_for(8)
        self.transfer(Upstream(browse_broken=True), tracks)   # the create, then no reads
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("yt_id"), PLID)
        self.assertIs(rec.get("complete"), False, rec)
        plan = [vid(i) for i in range(1, 9)]
        self.assertEqual(rec.get("video_ids"), plan,
                         "the interrupted record did not carry the plan it was sent")

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

    def test_a_read_that_parsed_no_ids_never_deletes_the_playlist(self):
        tracks = tracks_for(40)
        extras = ["X%010d" % i for i in range(70)]
        upstream = Upstream(extra=extras, blind_rows=True, paging_broken=True)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.deletes, [], "an unread playlist was deleted")
        self.assertNotIn("husk deleted", snap.get("error", ""))
        self.assertIn(snap["phase"], ("failed", "done"), snap)

    def test_a_single_empty_complete_read_is_not_proof_the_playlist_is_empty(self):
        tracks = tracks_for(40)
        upstream = Upstream(empty_reads=6)      # the stale window, then the truth
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.deletes, [], "a stale empty read deleted the playlist")
        self.assertEqual(len(upstream.playlist), len(tracks))
        self.assertNotIn("husk deleted", snap.get("error", ""))
        self.assertEqual(snap["phase"], "failed", snap)

    def test_a_wholly_failed_read_is_a_failed_run_with_unverified_rows(self):
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

    def test_an_ambiguous_add_is_not_resent_without_a_complete_read(self):
        upstream = Upstream(lose_reply={vid(1)}, paging_broken=True)
        upstream.playlist = ["X%010d" % i for i in range(150)]   # more than one page
        with mock.patch("urllib.request.urlopen", upstream):
            st = backend.yt_add_one(PLID, vid(1), COOKIE)

        self.assertTrue(backend.is_unsure_status(st), st)
        self.assertEqual(upstream.appends.count(vid(1)), 1,
                         "an ambiguous add was resent without a complete read")

    def test_a_lost_reply_on_an_append_is_never_sent_twice(self):
        tracks = tracks_for(60)
        upstream = Upstream(lose_reply={vid(55)}, fail_read_after_loss=True)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(upstream.appends.count(vid(55)), 1,
                         "an ambiguous add was resent without a complete read")
        self.assertEqual(upstream.playlist.count(vid(55)), 1)
        self.assertEqual(snap["phase"], "done", snap.get("error"))
        self.assertEqual(snap["landed"], 60)

    def test_an_add_whose_reply_is_lost_is_not_resent_off_one_complete_read(self):
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
        self.assertEqual(snap["tail_unverified"], 0, snap)
        self.assertIn("1 unverified", snap["summary"])
        self.assertNotIn("read stopped", snap["summary"])
        self.assertIs(self.created()["SPOTREPAIR"]["complete"], False)

    def test_an_unknown_write_reads_back_as_unverified_not_missing(self):
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
        refused = backend._reconcile(vids, [vid(1)], {1: "STATUS_FAILED"})
        self.assertEqual(refused[1]["outcome"], "rejected")
        self.assertFalse(refused[1].get("unverified"), refused[1])

    def test_a_429_is_an_unknown_write_and_is_never_re_sent(self):
        self.assertTrue(backend.is_unsure_status(backend.STATUS_UNSURE + " HTTPError 429"))
        tracks = tracks_for(60)
        upstream = Upstream(throttle=2)
        snap = self.transfer(upstream, tracks)

        self.assertEqual(snap["state"], "gaveup", snap)
        self.assertEqual(snap["counts"]["added"], 58, snap["counts"])
        self.assertEqual(snap["counts"]["unverified"], 2, snap["counts"])
        self.assertEqual(len(upstream.sends), len(set(upstream.sends)))

    def test_a_refused_copy_does_not_block_another_occurrence_of_the_same_song(self):
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
        self.assertEqual(snap["counts"]["added"], 30, snap["counts"])
        self.assertEqual(snap["counts"]["unverified"], 30, snap["counts"])

    def test_a_resume_seed_that_stopped_short_publishes_no_landed_row(self):
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
        self.assertEqual(len(upstream.appends), 100, upstream.appends[100:])
        self.assertEqual(len(seen), 100, "the run kept writing past a lost note")
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("inflight"), list(range(50, 150)), rec.get("inflight"))

    def test_a_new_playlist_does_not_inherit_an_old_records_evidence(self):
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
        self.assertIs(rec.get("complete"), True)

    def test_a_top_up_persists_the_plan_its_positions_index(self):
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
            attempts, entries, complete, expected_min = backend._append_verify(
                PLID, [a, b, c], [], COOKIE, None, [a, b, c], True, floor=0)
        self.assertEqual(expected_min, 3,
                         "the floor ignored the entries the authorising read returned")

    def test_a_corrupt_record_fails_closed_instead_of_creating_a_playlist(self):
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            f.write("{not json")
        upstream = Upstream()
        with self.assertRaises(backend.RecordUnreadable):
            backend.transfer_create_append(COOKIE, "Repair Test", "",
                                           [vid(1), vid(2)], "PRIVATE", "SPOTREPAIR")
        self.assertEqual(upstream.creates, 0, "a playlist was created over a bad record")

    def test_a_missing_record_is_still_a_first_run(self):
        upstream = Upstream()
        with mock.patch("urllib.request.urlopen", upstream):
            out = backend.transfer_create_append(COOKIE, "Repair Test", "",
                                                 [vid(1), vid(2)], "PRIVATE", "SPOTREPAIR")
        self.assertEqual(out["playlistId"], PLID)
        self.assertEqual(upstream.creates, 1)

    def test_a_lost_create_reply_never_advises_a_retry(self):
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
        with mock.patch("urllib.request.urlopen", Upstream()):
            with self.assertRaises(RuntimeError) as again:
                backend.transfer_create_append(COOKIE, "Repair Test", "",
                                               [vid(1), vid(2)], "PRIVATE", "SPOTREPAIR")
        self.assertIn("second one", str(again.exception).lower())

    def test_a_top_up_adds_a_track_the_source_gained(self):
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
        a, b, c = vid(1), vid(2), vid(3)
        rec = {"video_ids": [a, b, c], "unsure": [0], "expected_min": 2}
        floor, frozen, ids, need = backend._persisted_unknown(rec, [b, a, c])
        self.assertEqual(floor, 2)
        self.assertEqual(frozen, {1},
                         "the saved position was applied to the new plan as-is")
        self.assertEqual(ids, {a}, "the unknown copy lost its durable id")

    def test_an_unknown_id_survives_a_plan_that_drops_it(self):
        a, b, c = vid(1), vid(2), vid(3)
        rec = {"video_ids": [a, b, c], "unsure": [0], "expected_min": 2}
        floor, frozen, ids, need = backend._persisted_unknown(rec, [b, c])
        self.assertEqual(frozen, set(), "an id with no position cannot be frozen by name")
        self.assertEqual(ids, {a}, "the unknown was forgotten when the plan dropped it")
        floor2, frozen2, ids2, _ = backend._persisted_unknown(
            {"video_ids": [a, b, c], "unsure_ids": sorted(ids)}, [c, b, a])
        self.assertEqual(frozen2, {2}, "the reintroduced copy was not frozen")
        self.assertEqual(ids2, {a})

    def test_one_visible_copy_does_not_resolve_an_unknown_second_copy(self):
        a = vid(1)
        need = {a: 2}
        self.assertEqual(backend._unresolved_ids({a}, [a], need=need), {a},
                         "one visible copy resolved an unknown second copy")
        self.assertEqual(backend._unresolved_ids({a}, [a, a], need=need), set())
        self.assertEqual(backend._unresolved_ids({a}, [a, a, a]), {a},
                         "an unprovable doubt was cleared by a read that cannot prove it")

    def test_an_unknown_id_is_not_forgotten_across_plans_and_is_never_resent(self):
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
            backend.transfer_create_append(COOKIE, "Repair Test", "", [b, c],
                                           "PRIVATE", "SPOTREPAIR")
            rec = self.created()["SPOTREPAIR"]
            self.assertEqual(rec.get("video_ids"), [b, c])
            self.assertEqual(rec.get("unsure_ids"), [a],
                             "the unknown was dropped when the plan stopped naming it")
            self.assertEqual(upstream.sends, [])

            backend.transfer_create_append(COOKIE, "Repair Test", "", [a, b, c],
                                           "PRIVATE", "SPOTREPAIR")

        self.assertEqual(upstream.sends, [],
                         "a song whose earlier write was unknown was sent again")
        self.assertEqual(upstream.playlist.count(a), 0)
        rec = self.created()["SPOTREPAIR"]
        self.assertIn(a, rec.get("unsure_ids") or [], "the unknown was resolved for free")

    def test_a_rematch_cannot_resend_the_song_whose_write_was_unknown(self):
        a, b, c = vid(1), vid(2), vid(3)
        upstream = Upstream()
        upstream.playlist = [b, c]                  # A absent from the read
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION,
                "yt_id": PLID, "url": "https://example.invalid/p",
                "title": "Repair Test", "expected": 3, "complete": True,
                "video_ids": [a, b, c], "plan_slots": [a, b, c],
                "unsure": [0], "expected_min": 2}}, f)

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
        self.assertEqual(rec.get("video_ids"), [b, a, c])
        self.assertEqual(rec.get("unsure"), [1],
                         "the unknown stayed in the old plan's coordinates")

    def test_a_sync_top_up_never_persists_complete_over_an_unknown_copy(self):
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

    def test_a_write_ahead_that_cannot_be_saved_sends_nothing(self):
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
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION, "yt_id": PLID, "complete": False}}, f)

        self.assertTrue(backend._write_ahead("SPOTREPAIR", [3], 1))
        self.assertEqual(self.created()["SPOTREPAIR"]["inflight"], [3])
        with mock.patch.object(backend, "save_created", lambda d: False):
            backend._note_outcomes("SPOTREPAIR", [3], [3], 1)
        backend._write_ahead("SPOTREPAIR", [7], 2)

        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["inflight"], [3, 7],
                         "a new write-ahead erased the queue the failed note left")
        backend._note_outcomes("SPOTREPAIR", [7], [7], 3)
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["inflight"], [3])
        self.assertEqual(rec["unsure"], [7])
        self.assertEqual(rec["expected_min"], 3)

    def test_the_created_floor_is_on_disk_before_the_first_read_back(self):
        upstream = Upstream(browse_broken=True)   # the create works, every read fails
        snap = self.transfer(upstream, tracks_for(60))

        self.assertEqual(snap["phase"], "failed", snap.get("error"))
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("expected_min"), 50,
                         "the inline floor was not recorded at create time")
        self.assertIs(rec.get("complete"), False)

    def test_an_unknown_write_freezes_every_copy_of_that_song(self):
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
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump({"SPOTREPAIR": {"record_version": backend.RECORD_VERSION, "yt_id": PLID, "complete": False,
                                      "expected_min": 50, "unsure": [3]}}, f)

        backend._persist_progress("SPOTREPAIR", PLID, "https://example.invalid/p",
                                  "Repair Test", 6, state="repairing", attempt=1,
                                  expected_min=2, unsure=[])
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec["expected_min"], 50, "a stale floor was written back")
        self.assertEqual(rec["unsure"], [3], "a persisted unknown was dropped")

    def test_every_slice_is_written_ahead_before_it_is_sent(self):
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
        self.assertIn(([50, 51, 52, 53, 54], 50), seen, seen)
        rec = self.created()["SPOTREPAIR"]
        self.assertEqual(rec.get("inflight"), [], "the write-ahead list was never cleared")
        self.assertEqual(rec.get("expected_min"), 55)
        self.assertEqual(rec.get("unsure"), [])

    def test_a_resume_does_not_resend_a_copy_whose_write_was_unknown(self):
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

    def test_a_token_complete_read_below_the_minimum_is_not_complete(self):
        upstream = Upstream()
        upstream.playlist = ["A%010d" % i for i in range(3)]
        with mock.patch("urllib.request.urlopen", upstream):
            entries, complete = backend._read_back_retry(PLID, COOKIE, tries=2, minimum=5)
        self.assertEqual(entries, ["A%010d" % i for i in range(3)])
        self.assertFalse(complete, "a read below the expected minimum called itself complete")
        upstream.reads = 0
        with mock.patch("urllib.request.urlopen", upstream):
            entries, complete = backend._read_back_retry(PLID, COOKIE, tries=2, minimum=0)
        self.assertTrue(complete, "an unknown minimum was treated as a short read")

    def test_a_repair_read_shorter_than_the_last_one_is_incomplete(self):
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
        self.assertEqual(stood["counts"]["added"], len(landed), stood["counts"])
        self.assertEqual(stood["counts"]["pending"], len(pending), stood["counts"])
        self.assertEqual(stood["counts"]["unverified"], 0, stood["counts"])
        self.assertEqual(stood["landed"], len(landed))
        self.assertNotEqual(snap["rows"][-1]["state"], "landed")
        self.assertEqual(snap["landed"], 59)

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
