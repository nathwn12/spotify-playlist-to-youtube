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

    for line in failures:
        print(line)
    print(f"{len(rows) - len(failed_ids)}/{len(rows)} fixtures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
