#!/usr/bin/env python3
"""Offline gate: a fatal append verdict survives a read-back that used the same auth.

A 401/403 on an append is a verdict on the CALL. The read-back that follows uses the
same cookie, so on a dead session it fails too - and three entry points used to raise
"the rest could not be verified", which is true of a dead cookie, a dead connection and
a short read alike. The status the server actually gave was dropped on the way out, and
`classify` then guessed at the cause from the sentence.

What this file claims:
  * the status, kind and hint reach the run (`_write_verdict`, from the status alone),
  * nothing is sent again after the verdict, and no repair pass is entered,
  * the record's unknown write-ahead is untouched - the run adds no write of its own,
  * a fatal copy is filed `refused` even when the read never reached the end.

Isolation: APPDATA, USERPROFILE and HOME are pointed at a temp directory BEFORE
`backend` is imported, so STATE, the vault, created.json, settings and the log all land
there and `cookie_dirs()` cannot see the real Desktop or Downloads. The import refuses
to run if `backend` was already loaded against the real profile, so a shared test runner
cannot silently write owner state.
"""

import atexit
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.error
import warnings
from unittest import mock

warnings.filterwarnings("ignore", category=ResourceWarning)  # HTTPError holds a file object

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def _inside(path, root):
    """True when `path` really is under `root` (case-insensitive, like the host)."""
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root))
    return path == root or path.startswith(root + os.sep)


def _bootstrap():
    """(backend, scratch_root, owned_temp) under a state root this file can prove.

    Two ways to run, and the isolation claim is the same in both:
      * inside the acceptance gate, `tests/state_guard.py` has already imported
        backend with APPDATA/USERPROFILE/HOME pointed at SP2YT_SCRATCH and refuses to
        start the gate unless every state path is inside it. That import is reused -
        re-pointing the environment now would leave the loaded module writing to the
        guard's root anyway - and the scratch root becomes the root this file checks.
      * standalone, no backend is loaded yet, so this file makes its own temp profile,
        imports backend against it, and deletes it at exit.

    Anything else (backend imported with no scratch root, or a root that does not
    contain its STATE) stops the run before a single write."""
    scratch = os.environ.get("SP2YT_SCRATCH", "")
    if "backend" in sys.modules:
        mod = sys.modules["backend"]
        if not scratch or not _inside(mod.STATE, scratch):
            raise SystemExit(
                "refusing to run: backend was already imported and its STATE (%r) is not "
                "inside SP2YT_SCRATCH (%r). Run this file standalone or through "
                "tests/state_guard.py." % (mod.STATE, scratch)
            )
        return mod, scratch, None
    owned = tempfile.mkdtemp(prefix="sp2yt-fatal-")
    atexit.register(shutil.rmtree, owned, ignore_errors=True)
    os.environ["APPDATA"] = owned
    os.environ["USERPROFILE"] = owned
    os.environ["HOME"] = owned
    import backend as mod  # noqa: F401  (import after the environment is set)

    if not _inside(mod.STATE, owned):
        raise SystemExit(
            "refusing to run: backend resolved STATE to %r, outside the temp profile %r" % (mod.STATE, owned)
        )
    return mod, owned, owned


backend, ROOT, OWNED = _bootstrap()

PLID = "PLfatalMask0001"
COOKIE = "SID=test-only; SAPISID=test-only-value"
PAGE = 100


def vid(i):
    return "F%010d" % i


def tracks_for(n):
    return [
        {
            "uri": "spotify:track:%022d" % i,
            "title": "Track %03d" % i,
            "artist": "Artist %03d" % i,
            "duration_ms": 180000 + i,
        }
        for i in range(1, n + 1)
    ]


def fake_fetch(tracks):
    def fetch(url, cookie=""):
        return {
            "playlist_id": "SPOTFATAL",
            "playlist_name": "Fatal Mask",
            "cover_url": "",
            "tracks": list(tracks),
            "declared_total": len(tracks),
        }

    return fetch


def fake_batch(chunk, workers=8):
    out = []
    for t in chunk:
        m = re.match(r"Track (\d+)$", str((t or {}).get("title", "")))
        out.append({"ok": True, "query": (t or {}).get("title", ""), "videoId": vid(int(m.group(1))) if m else None})
    return out


class Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def read(self, *a):
        return self._body[: a[0]] if a else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def unauthorized(url):
    return urllib.error.HTTPError(url, 401, "Unauthorized", None, None)


class DeadAfterFirstAppend:
    """The create answers, reads serve what the playlist holds, the first append is
    refused with 401 and every later read fails with the same dead auth."""

    def __init__(self, held=None, empty_reads=0):
        self.held = list(held or [])
        self.empty_reads = empty_reads
        self.sends = []
        self.creates = 0
        self.reads = 0
        self.dead = False

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", "") or str(req)
        if "playlist/create" in url:
            self.creates += 1
            # The created playlist is NOT added to what reads serve: this fake models
            # a read that does not show the inline batch (a padded snapshot), which is
            # what leaves the plan unproved while the cookie is still alive.
            return Resp({"playlistId": PLID})
        if "edit_playlist" in url:
            v = json.loads(req.data.decode())["actions"][0]["addedVideoId"]
            self.sends.append(v)
            self.dead = True
            raise unauthorized(url)
        if "youtubei/v1/browse" in url:
            if self.empty_reads:
                # A token-complete reply that claims an empty playlist: InnerTube serves
                # this right after a create for a playlist that does hold rows.
                self.empty_reads -= 1
                return Resp({"contents": {"musicPlaylistShelfRenderer": {"contents": []}}})
            if self.dead:
                raise unauthorized(url)
            self.reads += 1
            body = json.loads(req.data.decode()) if req.data else {}
            start = int(re.sub(r"\D", "", body.get("continuation") or "0") or 0)
            page = self.held[start : start + PAGE]
            shelf = {
                "contents": [{"musicResponsiveListItemRenderer": {"playlistItemData": {"videoId": v}}} for v in page]
            }
            if start + len(page) < len(self.held):
                shelf["continuations"] = [{"nextContinuationData": {"continuation": "page:%d" % (start + len(page))}}]
            return Resp({"contents": {"musicPlaylistShelfRenderer": shelf}})
        raise AssertionError("no fake for " + url)


class FatalMask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp2yt-fatal-case-")
        self._saved = {k: getattr(backend, k) for k in ("_SEARCH_CACHE", "LOGF", "CREATEDF")}
        backend._SEARCH_CACHE = {}
        # A case's own files, inside a directory this case owns: the record and the log
        # never land in the guard's node either, so a case cannot read another's leftovers.
        backend.LOGF = os.path.join(self.tmp, "server.log")
        backend.CREATEDF = os.path.join(self.tmp, "created.json")
        self._patches = [
            mock.patch.object(backend, "_pause", lambda s: None),
            mock.patch("time.sleep", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for k, v in self._saved.items():
            setattr(backend, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self, sid):
        try:
            with open(backend.CREATEDF, encoding="utf-8") as f:
                return json.load(f).get(sid) or {}
        except FileNotFoundError:
            return {}

    def transfer(self, upstream, tracks, batch=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("urllib.request.urlopen", upstream))
            stack.enter_context(mock.patch.object(backend, "fetch_spotify_tracks", fake_fetch(tracks)))
            stack.enter_context(mock.patch.object(backend, "yt_search_batch", batch or fake_batch))
            rid = backend.run_begin("", "")
            backend._transfer_thread("https://open.spotify.com/playlist/PLfatal", COOKIE, "", "PRIVATE", rid)
        return backend.run_snapshot()

    # 1
    def test_isolated_state_is_the_scratch_root(self):
        """The guard this file runs under: no state path it writes is in the owner profile.

        Standalone the root is this file's temp profile; inside the gate it is the run's
        SP2YT_SCRATCH, which `state_guard.py` proved before the gate started."""
        for name in ("STATE", "VAULTF", "COOKIEF", "WATCHF", "SETTINGF"):
            value = getattr(backend, name)
            self.assertTrue(_inside(value, ROOT), "%s is outside %r: %r" % (name, ROOT, value))
        self.assertTrue(
            _inside(backend.home_dir(), ROOT),
            "home_dir() reached outside the isolated profile: %r" % (backend.home_dir(),),
        )
        for d in backend.cookie_dirs():
            self.assertTrue(_inside(d, ROOT), "cookie_dirs() reached outside the isolated home: %r" % (d,))

    # 2
    def test_a_create_whose_read_back_dies_with_the_cookie_reports_the_401(self):
        """51 copies: the create carries 50 inline, the seed read is complete and padded
        with ids the plan does not name (a documented read), so copy 51 is the only
        append - and it is refused while the cookie is already dead."""
        tracks = tracks_for(51)
        pad = ["P%010d" % i for i in range(50)]
        upstream = DeadAfterFirstAppend(held=pad)
        snap = self.transfer(upstream, tracks, batch=fake_batch)

        self.assertIn("401", str(snap.get("error", "")), snap)
        self.assertEqual(snap.get("kind"), "auth", snap)
        hint = str(snap.get("hint", ""))
        self.assertIn("may have expired", hint)
        self.assertIn("permission", hint)
        self.assertIn("Nothing here can tell those apart", hint)
        self.assertNotIn("start again", hint)
        self.assertNotIn("if it repeats", hint)
        # One attempt, no repair pass: a dead cookie is not asked twice.
        self.assertEqual(upstream.sends, [vid(51)], upstream.sends)
        # The verdict is a refusal even though the read never reached the end.
        rec = self.record("SPOTFATAL")
        self.assertEqual(rec.get("complete"), False, rec)
        self.assertEqual(
            sorted(rec.get("unsure_ids") or []),
            sorted(vid(i) for i in range(1, 51)),
            "the inline copies the failed read could not name were dropped from the record",
        )

    # 3a
    def test_the_rows_say_refused_unknown_and_nothing_else(self):
        """The row beside a 401 must not say "unverified".

        One run, three shapes: the copy the server refused, the copies the create carried
        that no read could name, and copies nothing was ever asked about. The first is
        `refused` with its own status; the others stay unverified - the conservative claim
        that fits both a write nobody can account for and a write never attempted - and
        none of them may be called refused or landed."""
        tracks = tracks_for(51)
        upstream = DeadAfterFirstAppend(held=["P%010d" % i for i in range(50)])
        snap = self.transfer(upstream, tracks, batch=fake_batch)

        rows = {r.get("videoId"): r for r in snap.get("rows") or []}
        refused_row = rows[vid(51)]
        self.assertEqual(refused_row.get("state"), "refused", refused_row)
        self.assertIn("401", refused_row.get("status", ""), refused_row)
        self.assertFalse(refused_row.get("unverified"), refused_row)
        self.assertFalse(refused_row.get("not_sent"), refused_row)
        for i in range(1, 51):
            row = rows[vid(i)]
            self.assertNotIn(row.get("state"), ("refused", "landed"), row)
            self.assertTrue(row.get("unverified"), row)
        counts = snap.get("counts") or {}
        self.assertEqual(counts.get("refused"), 1, counts)
        self.assertEqual(counts.get("unverified"), 50, counts)
        self.assertEqual(counts.get("added"), 0, counts)
        self.assertEqual(counts.get("pending"), 0, counts)
        self.assertEqual(snap.get("missing", 0), 0, snap.get("missing"))
        # The counts are the rows, not a parallel tally: every source row is accounted for.
        self.assertEqual(
            sum(int(counts.get(k) or 0) for k in ("added", "skipped", "duplicate", "unverified", "refused", "pending")),
            counts.get("source"),
            counts,
        )

    # 3b
    def test_only_the_source_row_whose_copy_was_refused_is_refused(self):
        """The settlement is per POSITION, and positions are not source rows.

        The source repeats one song twice beyond the inline batch and also holds a row that
        matched nothing. The plan is [1..50, 60, 60]: positions 50 and 51 are the SAME
        videoId, and the unmatched row has no plan position at all, so every source index
        after it is one ahead of the plan index it feeds. Only position 50 was sent, and it
        came back 401: that source row must be refused, the second copy of the same id must
        stay unverified, the unmatched row must stay skipped, and the never-sent copies must
        stay unverified. Settling by plan index would refuse the wrong row here - the id
        appears twice, and the skipped row shifts the mapping."""
        tracks = tracks_for(50) + [
            {
                "uri": "spotify:track:unmatched",
                "title": "Unmatched source row",
                "artist": "Nobody",
                "duration_ms": 100000,
            },
            {"uri": "spotify:track:060a", "title": "Track 060", "artist": "Artist 060", "duration_ms": 180060},
            {
                "uri": "spotify:track:060b",
                "title": "Track 060",  # the same song again
                "artist": "Artist 060",
                "duration_ms": 180060,
            },
        ]
        self.assertEqual(len(tracks), 53)
        upstream = DeadAfterFirstAppend(held=["P%010d" % i for i in range(50)])
        snap = self.transfer(upstream, tracks, batch=fake_batch)

        # One append went out - the first copy of the repeated id - and it was the 401.
        self.assertEqual(upstream.sends, [vid(60)], upstream.sends)
        self.assertEqual(snap.get("kind"), "auth", snap)
        rows = snap.get("rows") or []
        refused = [r for r in rows if r.get("state") == "refused"]
        self.assertEqual(len(refused), 1, [(r.get("i"), r.get("state"), r.get("videoId")) for r in rows])
        # The refused row is the FIRST of the two copies (source row 52, row index 51),
        # not the second, and not the unmatched row (index 50) its plan index would
        # otherwise point at.
        self.assertEqual(refused[0].get("i"), 52, refused[0])
        self.assertEqual(rows[51], refused[0])
        self.assertEqual(refused[0].get("videoId"), vid(60), refused[0])
        self.assertIn("401", refused[0].get("status", ""), refused[0])
        # The second copy of the same id was never sent: never refused, never landed.
        second = [r for r in rows if r.get("videoId") == vid(60) and r.get("i") != 52]
        self.assertEqual(len(second), 1, second)
        self.assertEqual(second[0].get("i"), 53, second[0])
        self.assertNotIn(second[0].get("state"), ("refused", "landed"), second[0])
        self.assertTrue(second[0].get("unverified"), second[0])
        # The row that matched nothing keeps its own state - it has no plan position.
        self.assertEqual(rows[50].get("state"), "skipped", rows[50])
        # The rest is still only ever unverified.
        for row in rows[:50]:
            self.assertNotIn(row.get("state"), ("refused", "landed"), row)
        counts = snap.get("counts") or {}
        self.assertEqual(counts.get("refused"), 1, counts)
        self.assertEqual(counts.get("skipped"), 1, counts)
        self.assertEqual(counts.get("unverified"), 51, counts)
        self.assertEqual(counts.get("added"), 0, counts)
        self.assertEqual(
            sum(int(counts.get(k) or 0) for k in ("added", "skipped", "duplicate", "unverified", "refused", "pending")),
            counts.get("source"),
            counts,
        )

    # 3
    def test_a_resume_whose_read_back_dies_with_the_cookie_reports_the_401(self):
        """A record whose plan is absent from a complete read: every copy is outstanding,
        the first append is refused, and the read-back fails with the same 401."""
        plan = [vid(1), vid(2), vid(3)]
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "SPOTFATAL": {
                        "record_version": backend.RECORD_VERSION,
                        "yt_id": PLID,
                        "url": "https://music.youtube.com/playlist?list=" + PLID,
                        "title": "Fatal Mask",
                        "expected": 3,
                        "expected_min": 0,
                        "complete": False,
                        "state": "incomplete",
                        "video_ids": plan,
                        "plan_slots": plan,
                        "plan_guesses": {},
                    }
                },
                f,
            )
        upstream = DeadAfterFirstAppend(held=["O%010d" % i for i in range(3)])
        snap = self.transfer(upstream, tracks_for(3))

        self.assertIn("401", str(snap.get("error", "")), snap)
        self.assertEqual(snap.get("kind"), "auth", snap)
        self.assertEqual(upstream.sends, [vid(1)], upstream.sends)
        self.assertEqual(upstream.creates, 0, "the resume created a second playlist")
        rec = self.record("SPOTFATAL")
        self.assertEqual(rec.get("inflight"), [1, 2], "the copies sent but not proved were cleared by the failed read")
        self.assertEqual(rec.get("complete"), False, rec)

    # 4
    def test_a_top_up_keeps_its_401_too(self):
        """The third entry point already carried the status through its result; the
        regression pins it, because the other two now carry it the same way."""
        plan = [vid(1), vid(2)]
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "SPOTFATAL": {
                        "record_version": backend.RECORD_VERSION,
                        "yt_id": PLID,
                        "url": "https://music.youtube.com/playlist?list=" + PLID,
                        "title": "Fatal Mask",
                        "expected": 2,
                        "complete": True,
                        "state": "done",
                        "video_ids": plan,
                        "plan_slots": plan,
                        "plan_guesses": {},
                    }
                },
                f,
            )
        upstream = DeadAfterFirstAppend(held=[vid(1)])
        snap = self.transfer(upstream, tracks_for(3))  # the source gained a track

        self.assertEqual(upstream.creates, 0)
        self.assertEqual(upstream.sends, [vid(2)], upstream.sends)
        self.assertIn("401", str(snap.get("error", "")), snap)
        self.assertEqual(snap.get("kind"), "auth", snap)
        self.assertEqual(snap.get("state"), "gaveup", snap)

    # 5
    def test_a_fatal_copy_is_refused_even_when_the_read_stopped_short(self):
        """A verdict does not depend on how much of the playlist was read: the server
        answered and the copy was not added. Filing it as "unverified" made the run say
        "0 refused" beside a 401."""
        ledger = backend._reconcile(["a", "b"], ["a"], {1: "HTTPError 401"}, False)
        self.assertEqual(ledger[1]["outcome"], "rejected", ledger[1])
        self.assertIn("401", ledger[1]["status"])

    # 6
    def test_raise_outcome_carries_the_status_and_nothing_else_changes(self):
        with self.assertRaises(backend.FatalAppend) as got:
            backend._raise_outcome("the playlist is at X", {0: "HTTPError 403"})
        self.assertEqual(got.exception.status, "HTTPError 403")
        self.assertIn("403", str(got.exception))
        # No fatal verdict: the plain reason, no invented status.
        with self.assertRaises(RuntimeError) as plain:
            backend._raise_outcome("the playlist is at X", {0: "STATUS_UNSURE timeout"})
        self.assertNotIsInstance(plain.exception, backend.FatalAppend)

    # 7
    def test_the_advice_never_promises_a_record_or_asks_for_a_reset(self):
        """A record may be gone, blocked or non-resumable, so recovery is conditional; and
        no verdict may tell a person to reset - the record is the only account of what was
        already sent."""
        for status in ("HTTPError 401", "HTTPError 403"):
            note = backend._write_verdict(status)
            hint = note["hint"]
            self.assertEqual(note["kind"], "auth", note)
            self.assertIn("If a safe recovery record exists", hint)
            self.assertIn("follow its own guidance", hint)
            self.assertIn("otherwise preserve the playlist", hint)
            self.assertNotIn("resume this run from its record", hint)
            self.assertNotIn("start again", hint)
        for status in ("HTTPError 404", "HTTPError 400"):
            hint = backend._write_verdict(status)["hint"]
            self.assertNotIn("Reset this run", hint, status)
            self.assertNotIn("reset", hint.lower().replace("do not reset", ""), status)
            self.assertIn("keep", hint.lower(), status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
