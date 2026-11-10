#!/usr/bin/env python3
"""Offline checks for cookie detection, shape validation, adoption and the state API.

No network: nothing here calls check_yt_cookie or check_spotify_cookie.
Every state path is redirected into a temporary directory, so the real
%APPDATA%\\sp2yt, the Desktop and Downloads are never read or written.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import backend  # noqa: E402

DESKTOP_NAME = "music.youtube.com_cookies.txt"

HEADER = "SID=abc123def456; HSID=def456ghi789; SSID=ghi789jkl012; APISID=jkl012mno345; SAPISID=AbCdEfGhIjKlMnOp"
SP_HEADER = "sp_dc=SpOtIfYdC1234567890; sp_key=SpOtIfYk1234567890"

NETSCAPE = "\n".join([
    "# Netscape HTTP Cookie File",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tAbCdEfGhIjKlMnOp",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\ttz=UTC",
])

OLD_NETSCAPE = "\n".join([
    "# Netscape HTTP Cookie File",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tOldOldOldOldOld1",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\ttz=UTC",
])

NEW_NETSCAPE = "\n".join([
    "# Netscape HTTP Cookie File",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tNewNewNewNewNew2",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\ttz=UTC",
])

HTTPONLY = "\n".join([
    "# Netscape HTTP Cookie File",
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tHtTpOnLyOnLy1234",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\ttz=UTC",
])

EXPIRED = "\n".join([
    "# Netscape HTTP Cookie File",
    ".youtube.com\tTRUE\t/\tTRUE\t1600000000\tSAPISID\tExpExpExpExpExp12",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\ttz=UTC",
])

JUNK = "\n".join([
    "# Netscape HTTP Cookie File",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\t<paste the real value here>",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\ttz=UTC",
])

HTML = "<!doctype html><html><body><p>SAPISID=AbCdEfGhIjKlMnOp</p></body></html>"
JSON_BLOB = '{"SAPISID": "AbCdEfGhIjKlMnOp", "PREF": "tz=UTC"}'
NOTES = "my cookie notes\nfoo=bar\n"


def api(path, body=None):
    """One call against a throwaway loopback instance of the real handler.
    Returns (status, parsed body); an error status is a result, not an exception."""
    srv = backend.ThreadingHTTPServer(("127.0.0.1", 0), backend.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d%s" % (srv.server_address[1], path)
        req = urllib.request.Request(
            url, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read().decode())
    finally:
        srv.shutdown()
        srv.server_close()


class CookieTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp2yt-cookie-")
        self.watch = os.path.join(self.tmp, "watch")
        os.makedirs(self.watch)
        self._saved = {k: getattr(backend, k)
                       for k in ("VAULTF", "WATCHF", "COOKIEF", "LOGF", "cookie_dirs")}
        backend.VAULTF = os.path.join(self.tmp, "vault.json")
        backend.WATCHF = os.path.join(self.tmp, "watch.json")
        backend.COOKIEF = os.path.join(self.tmp, "cookie_state.json")
        backend.LOGF = os.path.join(self.tmp, "server.log")
        backend.cookie_dirs = lambda: [self.watch]

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(backend, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text, mtime=None):
        p = os.path.join(self.watch, name)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    # 1
    def test_desktop_export_filename_is_a_yt_cookie(self):
        self.assertEqual(backend.cookie_site(NETSCAPE, DESKTOP_NAME), "yt")
        shp = backend.cookie_shape(NETSCAPE, "yt")
        self.assertTrue(shp["ok"], shp)
        self.assertEqual(shp["state"], "valid")
        self.assertEqual(shp["expires_at"], 0)

    # 2
    def test_header_paste_is_valid_and_never_expired(self):
        self.assertEqual(backend.cookie_site(HEADER), "yt")
        shp = backend.cookie_shape(HEADER, "yt")
        self.assertEqual(shp["state"], "valid")
        self.assertEqual(shp["expires_at"], 0)

    # 3
    def test_text_file_without_marker_is_junk_even_with_the_right_name(self):
        self.assertEqual(backend.cookie_site(NOTES), "")
        shp = backend.cookie_shape(NOTES, "yt")
        self.assertEqual(shp["state"], "junk")
        self.assertEqual(backend.cookie_shape(NOTES, "yt")["state"], "junk")

    def test_a_filename_alone_never_makes_junk_valid(self):
        self.assertEqual(backend.cookie_site(NOTES, DESKTOP_NAME), "yt")
        self.assertEqual(backend.cookie_shape(NOTES, "yt")["state"], "junk")

    # 4
    def test_html_and_json_exports_are_junk(self):
        self.assertEqual(backend.cookie_shape(HTML, "yt")["state"], "junk")
        self.assertEqual(backend.cookie_shape(JSON_BLOB, "yt")["state"], "junk")

    # 5
    def test_expired_netscape_auth_cookie_is_expired_and_not_adopted(self):
        shp = backend.cookie_shape(EXPIRED, "yt")
        self.assertEqual(shp["state"], "expired")
        self.assertEqual(shp["expires_at"], 1600000000)
        self.write(DESKTOP_NAME, EXPIRED)
        v = backend.vault_load()
        self.assertEqual(v["yt_cookie"], "")
        self.assertEqual(v["state"]["yt"], "none")

    # 6
    def test_httponly_netscape_line_survives_normalize(self):
        norm = backend.normalize_cookie(HTTPONLY)
        self.assertIn("SAPISID=HtTpOnLyOnLy1234", norm)
        self.assertEqual(backend.cookie_site(HTTPONLY), "yt")
        self.assertEqual(backend.cookie_shape(HTTPONLY, "yt")["state"], "valid")

    # 7
    def test_only_a_newer_file_is_adopted(self):
        old = self.write(DESKTOP_NAME, OLD_NETSCAPE, mtime=time.time() - 3600)
        v = backend.vault_load()
        self.assertEqual(v["yt_cookie"], backend.normalize_cookie(OLD_NETSCAPE))
        self.assertEqual(v["state"]["yt"], "unverified")
        self.assertEqual(v["meta"]["yt"]["source"], "extra")
        self.assertEqual(v["refreshed"], ["yt:" + DESKTOP_NAME])

        self.assertTrue(backend.vault_save("yt", HEADER, source="manual")["ok"])
        v = backend.vault_load()
        self.assertEqual(v["yt_cookie"], backend.normalize_cookie(HEADER),
                         "an older disk file overwrote a newer manual latch")

        self.write("yt-new.txt", NEW_NETSCAPE, mtime=time.time() + 120)
        v = backend.vault_load()
        self.assertEqual(v["yt_cookie"], backend.normalize_cookie(NEW_NETSCAPE))
        self.assertIn("yt:yt-new.txt", v["refreshed"])
        self.assertGreater(os.path.getmtime(old), 0)

    # 9
    def test_rejected_files_are_ignored_not_best(self):
        self.write(DESKTOP_NAME, JUNK, mtime=time.time())
        self.write("yt-old.txt", EXPIRED, mtime=time.time() - 60)
        scan = backend.scan_disk_cookies()
        self.assertNotIn("yt", scan["best"])
        states = sorted(i["state"] for i in scan["ignored"])
        self.assertEqual(states, ["expired", "junk"])
        names = sorted(i["name"] for i in scan["ignored"])
        self.assertEqual(names, [DESKTOP_NAME, "yt-old.txt"])
        self.assertEqual(scan["dirs"], [self.watch])

    def test_vault_save_writes_nothing_for_junk_or_empty(self):
        r = backend.vault_save("yt", NOTES)
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "junk")
        self.assertFalse(os.path.exists(backend.VAULTF))
        r = backend.vault_save("yt", "")
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "empty")
        self.assertEqual(backend.vault_load()["yt_cookie"], "")

    def test_check_rejects_only_the_value_that_failed(self):
        """A definitive verdict drops the stored cookie; a different posted value never drops it.
        check_yt_cookie is stubbed: this test must never touch the network."""
        real = backend.check_yt_cookie
        try:
            self.assertTrue(backend.vault_save("yt", HEADER)["ok"])
            backend.check_yt_cookie = lambda c: {"ok": False, "msg": "expired - export a fresh cookie"}
            r = backend.cookie_check("yt", backend.normalize_cookie(HEADER))
            self.assertEqual(r["state"], "expired")
            self.assertEqual(backend.vault_load()["yt_cookie"], "")

            backend.check_yt_cookie = lambda c: {"ok": False, "msg": "could not reach music.youtube.com"}
            self.assertTrue(backend.vault_save("yt", HEADER)["ok"])
            r = backend.cookie_check("yt", backend.normalize_cookie(HEADER))
            self.assertEqual(r["state"], "network")
            self.assertIn("your file is saved", r["msg"])
            self.assertEqual(backend.vault_load()["yt_cookie"], backend.normalize_cookie(HEADER))

            backend.check_yt_cookie = lambda c: {"ok": False, "msg": "expired - export a fresh cookie"}
            r = backend.cookie_check("yt", OLD_NETSCAPE)
            self.assertEqual(r["state"], "expired")
            self.assertEqual(backend.vault_load()["yt_cookie"], backend.normalize_cookie(HEADER),
                             "a posted value removed the cookie that was stored")
        finally:
            backend.check_yt_cookie = real

    # 10
    def test_a_connected_verdict_never_survives_a_different_cookie(self):
        """An adopted value is unverified until it is itself checked.
        check_yt_cookie is stubbed: this test must never touch the network."""
        real = backend.check_yt_cookie
        try:
            self.assertTrue(backend.vault_save("yt", OLD_NETSCAPE)["ok"])
            backend.check_yt_cookie = lambda c: {"ok": True, "msg": ""}
            self.assertEqual(backend.cookie_check("yt", backend.normalize_cookie(OLD_NETSCAPE))["state"], "connected")
            self.assertEqual(backend.vault_load()["state"]["yt"], "connected")

            self.write("yt-new.txt", NEW_NETSCAPE, mtime=time.time() + 120)
            v = backend.vault_load()
            self.assertEqual(v["yt_cookie"], backend.normalize_cookie(NEW_NETSCAPE))
            self.assertEqual(v["state"]["yt"], "unverified",
                             "a connected verdict was carried onto a different cookie")
            self.assertEqual(backend.cookies_state()["yt"]["state"], "unverified")

            # the same value keeps the verdict it already earned
            self.assertEqual(backend.cookie_check("yt", backend.normalize_cookie(NEW_NETSCAPE))["state"], "connected")
            self.assertEqual(backend.vault_load()["state"]["yt"], "connected")
        finally:
            backend.check_yt_cookie = real

    # 11
    def test_remove_stays_removed_until_an_explicit_use(self):
        self.write(DESKTOP_NAME, NETSCAPE, mtime=time.time() - 60)
        self.assertEqual(backend.vault_load()["yt_cookie"], backend.normalize_cookie(NETSCAPE))
        backend.vault_remove("yt")
        v = backend.vault_load()
        self.assertEqual(v["yt_cookie"], "", "a removed cookie came back from the disk file")
        self.assertEqual(v["state"]["yt"], "none")
        body = backend.cookies_state()
        self.assertFalse(body["yt"]["has"])
        self.assertEqual(body["yt"]["state"], "none")
        self.assertEqual(body["yt"]["msg"], "Not connected yet")
        self.assertIsNotNone(body["detected"]["yt"], "the file is still there - the user must be able to pick it")

        code, used = api("/api/cookies/use", {"site": "yt"})
        self.assertEqual(code, 200, used)
        self.assertEqual(used["file"], DESKTOP_NAME)
        v = backend.vault_load()
        self.assertEqual(v["yt_cookie"], backend.normalize_cookie(NETSCAPE))
        self.assertEqual(v["state"]["yt"], "unverified")

    # 12
    def test_an_expired_paste_is_rejected_and_the_working_cookie_survives(self):
        self.assertTrue(backend.vault_save("yt", HEADER)["ok"])
        before = backend.vault_load()["yt_cookie"]
        r = backend.vault_save("yt", EXPIRED)
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "expired")
        self.assertTrue(r["msg"])
        self.assertEqual(backend.vault_load()["yt_cookie"], before,
                         "an expired paste replaced the stored working cookie")

        code, body = api("/api/latch", {"yt_cookie": EXPIRED})
        self.assertEqual(code, 400, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["state"], "expired")
        self.assertEqual(backend.vault_load()["yt_cookie"], before)

    # 13
    def test_check_cannot_resurrect_a_removed_cookie(self):
        self.write(DESKTOP_NAME, NETSCAPE, mtime=time.time() - 60)
        self.assertTrue(backend.vault_load()["yt_cookie"])
        self.assertEqual(api("/api/cookies/remove", {"site": "yt"})[0], 200)

        real, calls = backend.check_yt_cookie, []
        backend.check_yt_cookie = lambda c: calls.append(c) or {"ok": True, "msg": ""}
        try:
            code, r = api("/api/cookies/check", {"site": "yt"})
        finally:
            backend.check_yt_cookie = real
        self.assertEqual(code, 200, r)
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "none")
        self.assertEqual(r["msg"], "Not connected yet")
        self.assertEqual(calls, [], "a removed file was checked and could have been latched again")
        self.assertEqual(backend.vault_load()["yt_cookie"], "")
        body = api("/api/cookies")[1]
        self.assertFalse(body["yt"]["has"])
        self.assertEqual(body["yt"]["state"], "none")

    # 14
    def test_latch_reports_the_spotify_verdict_and_never_calls_junk_saved(self):
        backend.HISTORY.clear()
        code, body = api("/api/latch", {"spotify_cookie": NOTES, "yt_cookie": HEADER})
        self.assertEqual(code, 400, body)
        self.assertFalse(body["ok"])
        self.assertFalse(body["sp"]["ok"])
        self.assertEqual(body["sp"]["state"], "junk")
        self.assertTrue(body["sp"]["msg"])
        self.assertFalse([l for l in backend.HISTORY if "cookies latched" in l],
                         "a junk Spotify paste was logged as latched")
        self.assertEqual(backend.vault_load()["yt_cookie"], backend.normalize_cookie(HEADER),
                         "the yt path is unchanged: a valid yt cookie is still saved")

        code, body = api("/api/latch", {"spotify_cookie": SP_HEADER})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertTrue(body["sp"]["ok"])
        self.assertEqual(body["sp"]["state"], "unverified")
        self.assertEqual(backend.vault_load()["sp_cookie"], backend.normalize_cookie(SP_HEADER))

    # 15
    def test_remove_is_idempotent_and_keeps_its_block(self):
        self.write(DESKTOP_NAME, NETSCAPE, mtime=time.time() - 60)
        self.assertTrue(backend.vault_load()["yt_cookie"])
        self.assertEqual(api("/api/cookies/remove", {"site": "yt"})[0], 200)
        first = dict(backend.cookie_states()["yt"])
        self.assertEqual(first["state"], "removed")
        self.assertEqual(first["hash"], backend.cookie_hash(NETSCAPE))
        self.assertEqual(api("/api/cookies/remove", {"site": "yt"})[0], 200)
        self.assertEqual(backend.cookie_states()["yt"]["hash"], first["hash"],
                         "a second remove forgot which value was removed")
        body = api("/api/cookies")[1]
        self.assertEqual(body["yt"]["state"], "none")
        self.assertFalse(body["yt"]["has"])
        self.assertEqual(backend.vault_load()["yt_cookie"], "")

    # 16
    def test_a_verdict_is_only_shown_for_the_value_it_describes(self):
        real = backend.check_yt_cookie
        try:
            self.assertTrue(backend.vault_save("yt", OLD_NETSCAPE)["ok"])
            self.assertEqual(backend.vault_load()["state"]["yt"], "unverified")
            backend.check_yt_cookie = lambda c: {"ok": True, "msg": ""}
            probe = backend.cookie_check("yt", backend.normalize_cookie(NEW_NETSCAPE))
            self.assertEqual(probe["state"], "connected", "the probe's verdict must still come back to its caller")
        finally:
            backend.check_yt_cookie = real
        v = backend.vault_load()
        self.assertEqual(v["yt_cookie"], backend.normalize_cookie(OLD_NETSCAPE), "a probe replaced the stored value")
        self.assertEqual(v["state"]["yt"], "unverified", "a probe's verdict was shown for the stored value")
        body = api("/api/cookies")[1]
        self.assertTrue(body["yt"]["has"])
        self.assertEqual(body["yt"]["state"], "unverified")
        self.assertEqual((backend.cookie_states().get("yt") or {}).get("state"), "unverified",
                         "a probe overwrote the stored verdict")

        # a record that describes some other value is never shown for the stored one
        backend._cookie_state_set("yt", "connected", "ok", "manual", NEW_NETSCAPE)
        self.assertEqual(backend.vault_load()["state"]["yt"], "unverified")
        body = api("/api/cookies")[1]
        self.assertEqual(body["yt"]["state"], "unverified")
        self.assertEqual(body["yt"]["checked_at"], 0, "a foreign record's checked_at was reported")

    # 17
    def test_an_empty_yt_key_is_rejected_and_writes_nothing(self):
        code, body = api("/api/latch", {"yt_cookie": "", "spotify_cookie": SP_HEADER})
        self.assertEqual(code, 400, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["state"], "empty")
        self.assertEqual(body["error"], "missing - upload the cookie file")
        self.assertEqual(backend.cookie_states(), {}, "an empty yt still wrote a verdict")
        self.assertEqual(backend.vault_load()["sp_cookie"], "", "an empty yt still reached the sp path")

        code, body = api("/api/latch", {"spotify_cookie": SP_HEADER})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(backend.vault_load()["sp_cookie"], backend.normalize_cookie(SP_HEADER))

    # 18
    def test_an_empty_probe_writes_nothing_and_the_removed_record_survives(self):
        self.write(DESKTOP_NAME, NETSCAPE, mtime=time.time() - 60)
        self.assertTrue(backend.vault_load()["yt_cookie"])
        self.assertEqual(api("/api/cookies/remove", {"site": "yt"})[0], 200)
        blocked = dict(backend.cookie_states()["yt"])
        self.assertEqual(blocked["state"], "removed")
        self.assertEqual(blocked["hash"], backend.cookie_hash(NETSCAPE))

        code, r = api("/api/check", {"yt_cookie": ""})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["ytm"]["state"], "none")
        self.assertFalse(r["ytm"]["ok"])

        os.remove(os.path.join(self.watch, DESKTOP_NAME))   # no candidate left to fall back to
        code, r = api("/api/cookies/check", {"site": "yt"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["state"], "none")
        self.assertEqual(r["msg"], "Not connected yet")

        self.assertEqual(backend.cookie_states()["yt"], blocked, "an empty probe rewrote the removed record")
        self.assertNotEqual(backend.cookie_states()["yt"]["hash"], backend.cookie_hash(""),
                            "the removed hash was replaced by the hash of nothing")
        self.assertEqual(backend.vault_load()["yt_cookie"], "")

        self.write(DESKTOP_NAME, NETSCAPE, mtime=time.time() - 60)
        self.assertEqual(backend.vault_load()["yt_cookie"], "", "an empty probe let the removed file back in")

    # 19
    def test_a_verdict_never_lands_on_a_record_it_did_not_see(self):
        self.assertTrue(backend.vault_save("yt", HEADER)["ok"])
        started, release = threading.Event(), threading.Event()
        real = backend.check_yt_cookie

        def slow(c):
            started.set()
            release.wait(10)
            return {"ok": True, "msg": ""}

        backend.check_yt_cookie = slow
        out = {}
        try:
            t = threading.Thread(target=lambda: out.update(backend.cookie_check("yt", backend.normalize_cookie(HEADER))))
            t.start()
            self.assertTrue(started.wait(10), "the check never reached the network stub")
            backend.vault_remove("yt")     # the record changes while the check is in flight
            release.set()
            t.join(10)
        finally:
            backend.check_yt_cookie = real
        self.assertEqual(out.get("state"), "connected", "the verdict must still come back to its caller")
        record = backend.cookie_states()["yt"]
        self.assertEqual(record["state"], "removed", "an in-flight verdict overwrote the removal")
        self.assertEqual(record["hash"], backend.cookie_hash(HEADER), "the removed value lost its identity")
        self.assertEqual(backend.vault_load()["yt_cookie"], "")

        self.write("headers.txt", HEADER, mtime=time.time() - 60)
        self.assertEqual(backend.vault_load()["yt_cookie"], "",
                         "an in-flight verdict let the removed value back in on the next scan")

    # 20
    def test_a_named_file_can_be_chosen_even_when_it_is_not_the_newest(self):
        self.write("yt-old.txt", OLD_NETSCAPE, mtime=time.time() - 600)
        self.write("yt-new.txt", NEW_NETSCAPE, mtime=time.time() - 60)
        scan = backend.scan_disk_cookies()
        self.assertEqual(scan["best"]["yt"]["name"], "yt-new.txt")
        self.assertEqual([c["name"] for c in scan["all"]["yt"]], ["yt-new.txt", "yt-old.txt"])

        code, r = api("/api/cookies/use", {"site": "yt", "file": "yt-old.txt"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["file"], "yt-old.txt")
        self.assertEqual(backend._decode(backend._vault_read(), "yt"), backend.normalize_cookie(OLD_NETSCAPE),
                         "the named file was not the one latched")
        body = api("/api/cookies")[1]
        self.assertNotEqual(backend.vault_load()["yt_cookie"], backend.normalize_cookie(NEW_NETSCAPE),
                            "a later read swapped the chosen value for the newest file")
        self.assertEqual(body["yt"]["state"], "unverified")
        self.assertEqual(body["detected"]["yt"]["name"], "yt-new.txt", "detected must still name the newest")

    # 8
    def test_api_cookies_never_carries_a_value(self):
        self.assertTrue(backend.vault_save("yt", HEADER)["ok"])
        srv = backend.ThreadingHTTPServer(("127.0.0.1", 0), backend.H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            url = "http://127.0.0.1:%d/api/cookies" % srv.server_address[1]
            with urllib.request.urlopen(url, timeout=10) as r:
                body = r.read().decode()
        finally:
            srv.shutdown()
            srv.server_close()
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertTrue(data["yt"]["has"])
        self.assertEqual(data["yt"]["state"], "unverified")
        self.assertEqual(data["yt"]["source"], "manual")
        self.assertEqual(data["sp"]["state"], "none")
        self.assertIsNone(data["detected"]["yt"])
        for secret in ("AbCdEfGhIjKlMnOp", "SAPISID", "yt_cookie", "sp_cookie"):
            self.assertNotIn(secret, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
