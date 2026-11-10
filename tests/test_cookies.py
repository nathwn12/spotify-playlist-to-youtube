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
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import backend  # noqa: E402

DESKTOP_NAME = "music.youtube.com_cookies.txt"

HEADER = "SID=abc123def456; HSID=def456ghi789; SSID=ghi789jkl012; APISID=jkl012mno345; SAPISID=AbCdEfGhIjKlMnOp"

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
