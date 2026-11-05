#!/usr/bin/env python3
"""Offline acceptance gate: replay the owner's recorded data through backend.py.

Each fixture row is the real Spotify source row and the real InnerTube search
responses seen for it. Nothing here reaches the network: urlopen is replaced
with a lookup keyed by the search query.

Exit 0 only when every row resolves exactly as recorded and the four versions
of one title resolve to four distinct videoIds with zero repeats.
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import backend  # noqa: E402
import fixture_lib  # noqa: E402


class MissingResponse(BaseException):
    """BaseException on purpose: yt_match must not swallow a fixture gap."""


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._body


def test_clear():
    """Clear empties the served state; a fresh start after clear serves nothing.

    Drives the real HTTP routes (POST /api/reset, GET /api/run) against an
    in-process server with temp STATE files, so the proof covers the handler,
    not just the helpers. Settings survive (privacy latch is not transfer state).
    """
    import tempfile
    import threading
    from http.server import ThreadingHTTPServer

    tmp = tempfile.mkdtemp(prefix="sp2yt-clear-")
    real_created, real_setting = backend.CREATEDF, backend.SETTINGF
    backend.CREATEDF = os.path.join(tmp, "created.json")
    backend.SETTINGF = os.path.join(tmp, "settings.json")
    try:
        backend.run_begin("seedsp", "Seed Title")
        with backend._RUN_LOCK:
            backend.RUN["total"] = 2
            backend.RUN["declared"] = 2
            backend.RUN["landed"] = 1
            backend.RUN["skipped"] = 1
            backend.RUN["url"] = "https://music.youtube.com/playlist?list=PLSEED"
            backend.RUN["created"] = True
            backend.RUN["title"] = "Seed Title"
            backend.RUN["phase"] = "done"
            backend.RUN["rows"] = [
                {"i": 1, "title": "A", "artist": "B", "videoId": "dQw4w9WgXcQ",
                 "state": "landed", "status": "", "row_seq": 1},
                {"i": 2, "title": "C", "artist": "D", "videoId": "",
                 "state": "skipped", "status": "no match", "row_seq": 2},
            ]
        backend._persist_incomplete("seedsp", "PLSEED",
                                    "https://music.youtube.com/playlist?list=PLSEED",
                                    "Seed Title", 2)
        backend.save_settings({"privacy": "UNLISTED"})
        assert os.path.exists(backend.CREATEDF), "seed created.json missing"
        assert backend._latest_incomplete() is not None, "seed interrupted record missing"

        srv = ThreadingHTTPServer(("127.0.0.1", 0), backend.H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % port

            def get(p):
                return json.loads(urllib.request.urlopen(base + p, timeout=10).read())
            pre = get("/api/run?since=0")
            assert pre["ok"] and len(pre["rows"]) == 2 and pre["created"] is True, \
                "seeded run not served"
            req = urllib.request.Request(base + "/api/reset", data=b"{}",
                                         headers={"Content-Type": "application/json"})
            cleared = json.loads(urllib.request.urlopen(req, timeout=10).read())
            assert cleared.get("ok"), "reset not ok: %r" % (cleared,)
            post = get("/api/run?since=0")
            assert post["ok"], "run not ok after clear"
            assert post["run_id"] == "" and post["rows"] == [], "served rows survived clear"
            assert post["total"] == 0 and post["declared"] == 0, "served counts survived clear"
            assert post["created"] is False and not post["url"], "served link survived clear"
            assert "interrupted" not in post, "interrupted ghost survived clear"
            assert not os.path.exists(backend.CREATEDF), "created.json survived clear"
            fresh = get("/api/run?since=0")
            assert fresh["rows"] == [] and fresh["run_id"] == "" \
                and "interrupted" not in fresh, "fresh start after clear not empty"
            assert backend.load_settings().get("privacy") == "UNLISTED", "settings not kept"
        finally:
            srv.shutdown()
            srv.server_close()
        print("clear: served state empty after reset, fresh start serves nothing, settings kept")
        return None
    except AssertionError as e:
        return "FAIL clear: %s" % (e,)
    except Exception as e:
        return "FAIL clear: %s: %s" % (type(e).__name__, e)
    finally:
        backend.CREATEDF, backend.SETTINGF = real_created, real_setting
        backend.run_reset()


def test_cover_wiring():
    """cover_url rides the run payload; the page has a real <img> with fallback.

    Seeds RUN with a cover, serves GET /api/run over HTTP, asserts the key is
    present, then resets and asserts it is gone. Static part: index.html must
    hold an <img id="pcover"> (not a gradient span), a setCover() that falls
    back to the gradient, and a Clear path that empties the URL field.
    """
    import tempfile
    import threading
    from http.server import ThreadingHTTPServer

    tmp = tempfile.mkdtemp(prefix="sp2yt-cover-")
    real_created, real_setting = backend.CREATEDF, backend.SETTINGF
    backend.CREATEDF = os.path.join(tmp, "created.json")
    backend.SETTINGF = os.path.join(tmp, "settings.json")
    try:
        backend.run_begin("seedcover", "Seed Cover")
        with backend._RUN_LOCK:
            backend.RUN["phase"] = "done"
            backend.RUN["cover_url"] = "https://i.scdn.co/image/seedcover"
            backend.RUN["title"] = "Seed Cover"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), backend.H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % port

            def get(p):
                return json.loads(urllib.request.urlopen(base + p, timeout=10).read())
            pre = get("/api/run?since=0")
            assert pre["ok"] and pre.get("cover_url") == "https://i.scdn.co/image/seedcover", \
                "cover_url missing from /api/run: %r" % (pre.get("cover_url"),)
            req = urllib.request.Request(base + "/api/reset", data=b"{}",
                                         headers={"Content-Type": "application/json"})
            cleared = json.loads(urllib.request.urlopen(req, timeout=10).read())
            assert cleared.get("ok"), "reset not ok: %r" % (cleared,)
            post = get("/api/run?since=0")
            assert post.get("cover_url", "") == "", "cover_url survived clear"
        finally:
            srv.shutdown()
            srv.server_close()
        page = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        assert '<img id="pcover"' in page, "no <img id=pcover> in index.html"
        assert "setCover(r.cover_url" in page, "autoTitle drops cover_url"
        assert "d.cover_url!==undefined" in page, "mergeRun drops cover_url"
        assert 'id="srcx"' in page, "no inline URL-clear button"
        assert 'clearLocal(true)' not in page and 'clearLocal(false)' not in page, \
            "keepUrl switch still present — Clear must empty the field"
        print("cover: served over /api/run, cleared on reset, img+fallback wired")
        return None
    except AssertionError as e:
        return "FAIL cover: %s" % (e,)
    except Exception as e:
        return "FAIL cover: %s: %s" % (type(e).__name__, e)
    finally:
        backend.CREATEDF, backend.SETTINGF = real_created, real_setting
        backend.run_reset()


def test_scoped_clear():
    """Clear drops only the box's playlist key; other playlists keep theirs.

    Seeds created.json with two incomplete records, resets with one
    spotify_id, and asserts the other survives. An empty payload still
    clears the whole file (the test_clear path).
    """
    import tempfile
    import threading
    from http.server import ThreadingHTTPServer

    tmp = tempfile.mkdtemp(prefix="sp2yt-scope-")
    real_created, real_setting = backend.CREATEDF, backend.SETTINGF
    backend.CREATEDF = os.path.join(tmp, "created.json")
    backend.SETTINGF = os.path.join(tmp, "settings.json")
    try:
        backend._persist_incomplete("AAAA", "PLAAAA",
                                    "https://music.youtube.com/playlist?list=PLAAAA",
                                    "Playlist A", 10)
        backend._persist_incomplete("BBBB", "PLBBBB",
                                    "https://music.youtube.com/playlist?list=PLBBBB",
                                    "Playlist B", 20)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), backend.H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % port

            def post_reset(payload):
                req = urllib.request.Request(base + "/api/reset",
                                             data=json.dumps(payload).encode(),
                                             headers={"Content-Type": "application/json"})
                return json.loads(urllib.request.urlopen(req, timeout=10).read())

            def get_run():
                return json.loads(urllib.request.urlopen(base + "/api/run?since=0",
                                                         timeout=10).read())
            r = post_reset({"spotify_id": "BBBB"})
            assert r.get("ok"), "scoped reset not ok: %r" % (r,)
            kept = backend.load_created()
            assert "AAAA" in kept, "unrelated playlist A lost on scoped clear"
            assert "BBBB" not in kept, "playlist B survived its own clear"
            assert get_run().get("interrupted", {}).get("spotify_id") == "AAAA", \
                "interrupted restore should name the surviving playlist"
            r = post_reset({})
            assert r.get("ok"), "unscoped reset not ok: %r" % (r,)
            assert backend.load_created() == {}, "unscoped reset kept keys"
            assert "interrupted" not in get_run(), "ghost survived unscoped clear"
        finally:
            srv.shutdown()
            srv.server_close()
        print("scope: box playlist cleared, unrelated playlist kept, empty payload clears all")
        return None
    except AssertionError as e:
        return "FAIL scope: %s" % (e,)
    except Exception as e:
        return "FAIL scope: %s: %s" % (type(e).__name__, e)
    finally:
        backend.CREATEDF, backend.SETTINGF = real_created, real_setting
        backend.run_reset()


def test_reconcile_present_but_refused():
    """Owner run r1: 25 refused rows whose videos were on the playlist.
    Reconcile flips present-but-refused to landed; the done line then reads
    landed == holds instead of 48 beside 73."""
    try:
        vids = ["VID%08d" % i for i in range(5)]
        entries = list(vids)  # read-back holds every requested copy
        attempts = {0: "STATUS_FAILED", 1: "STATUS_FAILED",
                    2: "STATUS_SUCCEEDED", 3: "STATUS_SUCCEEDED", 4: "STATUS_SUCCEEDED"}
        ledger = backend._reconcile(vids, entries, attempts)
        landed = sum(1 for e in ledger if e["outcome"] == "landed")
        refused = sum(1 for e in ledger if e["outcome"] == "rejected")
        if landed != len(entries) or refused:
            return "FAIL reconcile-present: landed=%d refused=%d, want 5/0" % (landed, refused)
        s = backend._done_summary(landed, refused, 0, 0, 5, len(entries), "none")
        if "landed 5 of 5" not in s or "verified against the playlist (holds 5)" not in s:
            return "FAIL reconcile-present: done line %r" % (s,)
        print("reconcile-present: refused-but-present flips to landed, done line verified (holds 5)")
        return None
    except Exception as e:
        return "FAIL reconcile-present: %s: %s" % (type(e).__name__, e)


def test_reconcile_absent_stays_refused():
    """Genuinely absent at read-back stays refused (FAILED) or missing
    (SUCCEEDED-but-absent); the done line prints the same holds count the
    ledger consumed, with the split named."""
    try:
        vids = ["VID%08d" % i for i in range(5)]
        entries = vids[:3]  # last two copies never made the read-back
        attempts = {3: "STATUS_FAILED", 4: "STATUS_SUCCEEDED"}
        ledger = backend._reconcile(vids, entries, attempts)
        by = {e["videoId"]: e["outcome"] for e in ledger}
        if by[vids[3]] != "rejected" or by[vids[4]] != "missing":
            return "FAIL reconcile-absent: %r" % (by,)
        if vids[3] in entries:
            return "FAIL reconcile-absent: refused row present at read-back, must be proven absent"
        landed = sum(1 for e in ledger if e["outcome"] == "landed")
        if landed != len(entries):
            return "FAIL reconcile-absent: landed=%d, read-back holds %d" % (landed, len(entries))
        s = backend._done_summary(landed, 1, 0, 1, 5, len(entries), "none")
        if "holds 3" not in s or "verified against the playlist" not in s:
            return "FAIL reconcile-absent: done line %r" % (s,)
        print("reconcile-absent: absent stays refused/missing, done line holds 3")
        return None
    except Exception as e:
        return "FAIL reconcile-absent: %s: %s" % (type(e).__name__, e)


def test_timeout_retry_already_exists():
    """Append times out, retry comes back already-exists -> recorded added."""
    try:
        real = urllib.request.urlopen
        calls = []

        class FakeDup:
            def read(self):
                return b'"error": "video already in playlist"'

        class FakeOK:
            def read(self):
                return b'{"status": "STATUS_SUCCEEDED"}'

        def fake_first_timeout(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.URLError("timed out")
            return FakeDup()

        real_sleep = __import__("time").sleep
        __import__("time").sleep = lambda s: None
        try:
            urllib.request.urlopen = fake_first_timeout
            st = backend.yt_add_one("PLX", "VID00000001", "SAPISID=x")
            if "SUCCEEDED" not in (st or "").upper() or len(calls) != 2:
                return "FAIL retry: timeout+already-exists -> %r (calls=%d)" % (st, len(calls))

            calls.clear()

            def fake_twice_timeout(req, timeout=None):
                calls.append(1)
                raise urllib.error.URLError("timed out")

            urllib.request.urlopen = fake_twice_timeout
            st = backend.yt_add_one("PLX", "VID00000001", "SAPISID=x")
            if "SUCCEEDED" in (st or "").upper():
                return "FAIL retry: double timeout must not read as added (%r)" % (st,)

            calls.clear()
            urllib.request.urlopen = lambda req, timeout=None: (calls.append(1), FakeOK())[1]
            st = backend.yt_add_one("PLX", "VID00000001", "SAPISID=x")
            if "SUCCEEDED" not in (st or "").upper() or len(calls) != 1:
                return "FAIL retry: clean SUCCEEDED retried (%r, calls=%d)" % (st, len(calls))
        finally:
            urllib.request.urlopen = real
            __import__("time").sleep = real_sleep
        print("retry: timeout+already-exists lands, double timeout does not, clean success unretried")
        return None
    except Exception as e:
        return "FAIL retry: %s: %s" % (type(e).__name__, e)


def test_done_line_never_splits():
    """The done line is incapable of printing N landed beside a different
    holds count without naming the split in the same sentence."""
    try:
        bad = []
        for landed, n in [(48, 73), (73, 48), (0, 5), (5, 0)]:
            s = backend._done_summary(landed, 0, 0, 0, 75 if landed == 48 else 5, n, "none")
            if f"holds {n}" not in s:
                bad.append("no holds in %r" % (s,))
            if landed != n and "verified against the playlist" in s:
                bad.append("false verified in %r" % (s,))
            if landed != n and ("short" not in s and "not attributed" not in s
                                and "do not reconcile" not in s):
                bad.append("unexplained split in %r" % (s,))
        if bad:
            return "FAIL done-line: %s" % ("; ".join(bad),)
        print("done-line: every landed/holds split names itself")
        return None
    except Exception as e:
        return "FAIL done-line: %s: %s" % (type(e).__name__, e)


def test_running_motion():
    """Running state visibly moves without faking progress.

    Pill carries a moving dot + the same landedNow()/declared count the chips
    use; rule sweeps only while the total is unknown and fills capped at the
    known count; only the appending row pulses; one 1s footer tick cleared
    when live ends; all motion is CSS keyframes silenced under
    prefers-reduced-motion; no animation class survives a terminal phase.
    """
    try:
        page = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        need = [
            ('pill dot element', '<span class="pdot"'),
            ('pill text element', 'class="ptxt"'),
            ('pill pulse keyframes', '@keyframes pulse'),
            ('pill run selector', '#statepill.run .pdot'),
            ('pill count from landedNow', 'landedNow()'),
            ('pill label helper', 'pillLabel'),
            ('indeterminate sweep keyframes', '@keyframes slidesweep'),
            ('indeterminate pseudo-element', '.rule.live::after'),
            ('determinate cap', 'Math.min(100,100*a/'),
            ('unknown total guards sweep', '!known'),
            ('in-flight row class', 'inflight'),
            ('in-flight only appending', 'r.state==="appending"'),
            ('in-flight pulse keyframes', '@keyframes rowpulse'),
            ('single 1s tick', 'setInterval(()=>{paintFacts();},1000)'),
            ('tick guard', 'if(S.tick)return'),
            ('tick cleared', 'clearInterval(S.tick)'),
            ('tick stops off-live', 'if(live)tickStart();else tickStop()'),
            ('reduced-motion block', 'prefers-reduced-motion: reduce'),
        ]
        for label, s in need:
            if s not in page:
                return "FAIL motion: missing %s (%r)" % (label, s)
        seg = page.split("prefers-reduced-motion", 1)[1][:1500]
        for sel in ("rule.live", "pdot", "inflight"):
            if sel not in seg or "animation:none" not in seg:
                return "FAIL motion: reduced-motion does not silence %s" % (sel,)
        if "pill.textContent=human(p)" in page:
            return "FAIL motion: pill still overwrites the dot (textContent wipe)"
        if "@keyframes sweep" in page:
            return "FAIL motion: dead --fill sweep still present"
        print("motion: pill dot+count, rule sweep-then-fill capped, appending row pulses, 1s tick, reduced-motion static")
        return None
    except Exception as e:
        return "FAIL motion: %s: %s" % (type(e).__name__, e)


def main():
    try:
        data = fixture_lib.load()
    except Exception as e:
        print(f"FAIL fixtures: cannot read {fixture_lib.FIXTURE} ({type(e).__name__}: {e})")
        print("0/0 fixtures")
        return 1

    rows = data.get("rows") or []
    responses = data.get("responses") or {}
    if not rows:
        print("FAIL fixtures: no rows recorded")
        print("0/0 fixtures")
        return 1

    def fake_urlopen(req, timeout=None):
        try:
            query = json.loads((req.data or b"{}").decode("utf-8", "ignore")).get("query", "")
        except Exception:
            query = ""
        if query not in responses:
            raise MissingResponse(f"no recorded response for search: {query!r}")
        return FakeResponse(fixture_lib.envelope(responses[query]))

    real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    failures, failed_ids, replayed = [], set(), {}
    try:
        for row in rows:
            rid = row.get("id", "?")
            src = row.get("source") or {}
            title = src.get("title", "")
            artist = src.get("artist", "")
            ms = int(src.get("duration_ms") or 0)
            expect = row.get("expect") or {}
            want_sig = expect.get("signature", "")

            missing = [q for q in row.get("queries") or [] if q not in responses]
            if missing:
                failures.append(f"FAIL {rid}: fixture gap, no response recorded for {missing[0]!r}")
                failed_ids.add(rid)
                continue
            try:
                backend._SEARCH_CACHE.clear()
                result = backend.yt_match(title, artist, ms)
            except MissingResponse as e:
                failures.append(f"FAIL {rid}: {e}")
                failed_ids.add(rid)
                continue
            except Exception as e:
                failures.append(f"FAIL {rid}: matcher raised {type(e).__name__}: {e}")
                failed_ids.add(rid)
                continue

            result = result or {}
            got_sig = backend._sig_label(backend._version_sig(title))
            if got_sig != want_sig:
                failures.append(f"FAIL {rid}: source signature is {got_sig}, fixture recorded {want_sig}")
                failed_ids.add(rid)
                continue

            vid = result.get("videoId") or ""
            replayed[rid] = result
            if expect.get("videoId"):
                if not vid:
                    failures.append(f"FAIL {rid}: expected videoId {expect['videoId']}, skipped: "
                                    f"{str(result.get('skipped', ''))[:160]}")
                    failed_ids.add(rid)
                    continue
                chosen_sig = backend._sig_label(backend._version_sig(result.get("title", "")))
                if chosen_sig != want_sig:
                    failures.append(f"FAIL {rid}: chosen version {chosen_sig}, want {want_sig} "
                                    f"('{result.get('title', '')}')")
                    failed_ids.add(rid)
                    continue
                if vid != expect["videoId"]:
                    failures.append(f"FAIL {rid}: expected {expect['videoId']}, got {vid} "
                                    f"('{result.get('title', '')}')")
                    failed_ids.add(rid)
                    continue
                seen = []
                for q in row.get("queries") or []:
                    env = fixture_lib.envelope(responses[q])
                    seen += [c.get("videoId") for c in backend._candidates_from_search_json(env)]
                if expect["videoId"] not in seen:
                    failures.append(f"FAIL {rid}: recorded responses do not contain {expect['videoId']}")
                    failed_ids.add(rid)
                    continue
            elif expect.get("skip"):
                if vid:
                    failures.append(f"FAIL {rid}: expected skip ({expect['skip']}), got {vid} "
                                    f"('{result.get('title', '')}')")
                    failed_ids.add(rid)
                    continue
                got_class = fixture_lib.skip_class(result.get("skipped", ""))
                if got_class != expect["skip"]:
                    failures.append(f"FAIL {rid}: skip class {got_class}, want {expect['skip']} "
                                    f"({str(result.get('skipped', ''))[:140]})")
                    failed_ids.add(rid)
                    continue
            else:
                failures.append(f"FAIL {rid}: fixture has neither videoId nor skip")
                failed_ids.add(rid)
                continue

        # Four versions of one title must be four videos, not four copies of one.
        group = [r for r in rows if (r.get("source") or {}).get("title", "").startswith(
            "MONTAGEM RETINAL PLATINA - ")]
        if len(group) != 4:
            failures.append(f"FAIL versions: expected 4 MONTAGEM RETINAL PLATINA rows, found {len(group)}")
        else:
            ids = [(replayed.get(r["id"]) or {}).get("videoId") or "" for r in group]
            sigs = [backend._sig_label(backend._version_sig(r["source"]["title"])) for r in group]
            repeats = backend._repeat_count(ids, sigs)
            if not all(ids):
                failures.append(f"FAIL versions: {sum(1 for v in ids if v)}/4 versions resolved")
            elif len(set(ids)) != 4:
                failures.append(f"FAIL versions: 4 versions collapsed to {len(set(ids))} ids: {ids}")
            elif repeats:
                failures.append(f"FAIL versions: {repeats} repeat(s): {ids}")
            else:
                print("versions: 4/4 distinct, 0 repeats")
    finally:
        urllib.request.urlopen = real_urlopen

    clear_err = test_clear()
    if clear_err:
        failures.append(clear_err)

    cover_err = test_cover_wiring()
    if cover_err:
        failures.append(cover_err)

    scope_err = test_scoped_clear()
    if scope_err:
        failures.append(scope_err)

    motion_err = test_running_motion()
    if motion_err:
        failures.append(motion_err)

    for fn in (test_reconcile_present_but_refused,
               test_reconcile_absent_stays_refused,
               test_timeout_retry_already_exists,
               test_done_line_never_splits):
        try:
            err = fn()
        except Exception as e:
            err = "FAIL %s: %s: %s" % (fn.__name__, type(e).__name__, e)
        if err:
            failures.append(err)

    for line in failures:
        print(line)
    print(f"{len(rows) - len(failed_ids)}/{len(rows)} fixtures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
