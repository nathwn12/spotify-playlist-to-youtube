#!/usr/bin/env python3

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import backend  # noqa: E402

SECRET = "SyntheticGuardValue01"
SYNTH_YT = "\n".join(
    [
        "# Netscape HTTP Cookie File",
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\t" + SECRET,
        ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\ttz=UTC",
    ]
)
SYNTH_SP = "\n".join(
    [
        "# Netscape HTTP Cookie File",
        ".spotify.com\tTRUE\t/\tTRUE\t0\tsp_dc\t" + SECRET,
        ".spotify.com\tTRUE\t/\tTRUE\t0\tsp_key\t" + SECRET,
    ]
)


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp2yt-guard-")
        self.watch = os.path.join(self.tmp, "watch")
        os.makedirs(self.watch)
        self._saved = {
            k: getattr(backend, k)
            for k in ("VAULTF", "WATCHF", "COOKIEF", "LOGF", "CREATEDF", "SETTINGF", "cookie_dirs")
        }
        backend.VAULTF = os.path.join(self.tmp, "cookie_vault.json")
        backend.WATCHF = os.path.join(self.tmp, "cookie_watch.json")
        backend.COOKIEF = os.path.join(self.tmp, "cookie_state.json")
        backend.CREATEDF = os.path.join(self.tmp, "created.json")
        backend.SETTINGF = os.path.join(self.tmp, "settings.json")
        backend.LOGF = os.path.join(self.tmp, "server.log")
        backend.cookie_dirs = lambda: [self.watch]
        backend._BLOCKED.clear()
        self.srv = backend.ThreadingHTTPServer(("127.0.0.1", 0), backend.H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for k, v in self._saved.items():
            setattr(backend, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, method, path, body=None, host=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if host is not None:
            headers["Host"] = host
        if origin is not None:
            headers["Origin"] = origin
        try:
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            raw = r.read().decode()
            return r.status, raw, {k.lower(): v for k, v in r.getheaders()}
        finally:
            conn.close()

    def vault_text(self):
        try:
            with open(backend.VAULTF, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def write_created(self, rec):
        with open(backend.CREATEDF, "w", encoding="utf-8") as f:
            json.dump(rec, f)

    def read_created(self):
        try:
            with open(backend.CREATEDF, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def test_forged_host_is_refused_on_reads_and_writes(self):
        forged = "attacker.example:%d" % self.port
        for method, path, body in (
            ("GET", "/api/cookies", None),
            ("GET", "/api/run", None),
            ("GET", "/api/vault", None),
            ("GET", "/health", None),
            ("POST", "/api/latch", {"yt_cookie": SYNTH_YT}),
            ("POST", "/api/reset", {}),
        ):
            status, raw, _ = self.call(method, path, body, host=forged)
            self.assertEqual(status, 403, "%s %s answered %d" % (method, path, status))
            self.assertFalse(json.loads(raw)["ok"], "%s %s looked successful" % (method, path))
        self.assertEqual(self.vault_text(), "", "a refused write still reached the vault")

    def test_rebinding_name_that_resolves_to_loopback_is_refused(self):
        for host in (
            "127.0.0.1.nip.io:%d" % self.port,
            "localhost.attacker.example:%d" % self.port,
            "0.0.0.0:%d" % self.port,
            "attacker.example",
        ):
            status, _, _ = self.call("GET", "/api/cookies", host=host)
            self.assertEqual(status, 403, host + " was accepted")

    def test_cross_origin_write_is_refused(self):
        for origin in (
            "https://attacker.example",
            "http://127.0.0.1.evil.example",
            "null",
            "http://127.0.0.1",  # port 80 is not this server
            "http://localhost:9999",
        ):  # some other local server
            status, raw, _ = self.call("POST", "/api/latch", {"yt_cookie": SYNTH_YT}, origin=origin)
            self.assertEqual(status, 403, origin + " was accepted")
        self.assertEqual(self.vault_text(), "", "a refused write still reached the vault")

    def test_same_origin_write_is_accepted(self):
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port):
            origin_host = host.split(":")[0]
            status, raw, _ = self.call(
                "POST",
                "/api/latch",
                {"yt_cookie": SYNTH_YT},
                host=host,
                origin="http://%s:%d" % (origin_host, self.port),
            )
            self.assertEqual(status, 200, raw)
            self.assertTrue(json.loads(raw)["ok"])

    def test_no_origin_caller_is_allowed(self):
        status, raw, _ = self.call("POST", "/api/latch", {"yt_cookie": SYNTH_YT})
        self.assertEqual(status, 200, raw)
        self.assertTrue(json.loads(raw)["ok"])

    def test_health_and_page_stay_readable_for_the_shell_and_the_build(self):
        status, raw, _ = self.call("GET", "/health")
        self.assertEqual(status, 200, raw)
        self.assertTrue(json.loads(raw)["ok"])
        status, raw, headers = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])

    def test_vault_route_returns_flags_not_values(self):
        self.assertTrue(self.call("POST", "/api/latch", {"yt_cookie": SYNTH_YT, "spotify_cookie": SYNTH_SP})[0] == 200)
        blob = json.loads(self.vault_text())
        status, raw, _ = self.call("POST", "/api/vault", {})
        self.assertEqual(status, 200, raw)
        body = json.loads(raw)
        self.assertTrue(body["ok"])
        self.assertTrue(body["has_yt"])
        self.assertTrue(body["has_sp"])
        self.assertGreater(body["yt_len"], 0)
        self.assertGreater(body["sp_len"], 0)
        for leak in (SECRET, "cookie", blob["yt"], blob["sp"]):
            self.assertNotIn(leak, raw, "the vault route published a value")
        self.assertNotIn("yt_cookie", body)
        self.assertNotIn("sp_cookie", body)

    def test_no_cors_grant_is_ever_answered(self):
        for method in ("OPTIONS", "HEAD"):
            _, _, headers = self.call(method, "/api/latch")
            self.assertNotIn("access-control-allow-origin", headers)
            self.assertNotIn("access-control-allow-methods", headers)

    def test_reset_reports_a_record_it_could_not_rewrite(self):
        self.write_created({"sid-1": {"yt_id": "PLone", "complete": False}})
        os.makedirs(backend.CREATEDF + ".tmp")  # the atomic write cannot land
        status, raw, _ = self.call("POST", "/api/reset", {"spotify_id": "sid-1"})
        self.assertEqual(status, 500, raw)
        body = json.loads(raw)
        self.assertFalse(body["ok"], "a failed reset was reported as success")
        self.assertIs(body.get("reset_partial"), True,
                      "the page keys on reset_partial; this 500 did not carry it")
        self.assertIn("sid-1", self.read_created(), "the record was lost by its own failure")
        self.assert_says_record_survived(body["error"])

    def test_reset_reports_a_record_it_could_not_remove(self):
        os.makedirs(backend.CREATEDF)
        status, raw, _ = self.call("POST", "/api/reset", {})
        self.assertEqual(status, 500, raw)
        body = json.loads(raw)
        self.assertFalse(body["ok"], "a failed reset was reported as success")
        self.assertIs(body.get("reset_partial"), True,
                      "the page keys on reset_partial; this 500 did not carry it")
        self.assert_says_record_survived(body["error"])

    def assert_says_record_survived(self, error):
        self.assertIn("could not be cleared", error)
        self.assertIn("may still be used", error)
        self.assertNotIn("resum", error.lower(), "a reset failure promised a resume")

    def test_reset_still_clears_a_record_it_can(self):
        self.write_created(
            {"sid-1": {"yt_id": "PLone", "complete": False}, "sid-2": {"yt_id": "PLtwo", "complete": False}}
        )
        status, raw, _ = self.call("POST", "/api/reset", {"spotify_id": "sid-1"})
        self.assertEqual(status, 200, raw)
        self.assertNotIn("reset_partial", json.loads(raw),
                         "a completed reset carried a partial-reset verdict")
        self.assertEqual(list(self.read_created()), ["sid-2"])
        status, raw, _ = self.call("POST", "/api/reset", {})
        self.assertEqual(status, 200, raw)
        self.assertNotIn("reset_partial", json.loads(raw),
                         "a completed reset carried a partial-reset verdict")
        self.assertIsNone(self.read_created())

    def test_repeated_refusals_do_not_grow_the_audit_log(self):
        for _ in range(20):
            self.call("GET", "/api/cookies", host="attacker.example")
            self.call("POST", "/api/latch", {"yt_cookie": SYNTH_YT}, origin="https://attacker.example")
        try:
            with open(backend.LOGF, encoding="utf-8") as f:
                blocked = [ln for ln in f.read().splitlines() if " block " in ln]
        except FileNotFoundError:
            blocked = []
        self.assertEqual(len(blocked), 2, blocked)

    def test_a_refused_reset_carries_no_partial_verdict(self):
        self.write_created({"sid-1": {"yt_id": "PLone", "complete": False}})
        saved = backend.RUN["phase"]
        backend.RUN["phase"] = "append"
        try:
            status, raw, _ = self.call("POST", "/api/reset", {"spotify_id": "sid-1"})
        finally:
            backend.RUN["phase"] = saved
        self.assertEqual(status, 409, raw)
        body = json.loads(raw)
        self.assertFalse(body["ok"])
        self.assertNotIn("reset_partial", body, "a refusal claimed a half-done reset")
        self.assertIn("sid-1", self.read_created(), "a refused reset removed the record")


if __name__ == "__main__":
    unittest.main(verbosity=2)
