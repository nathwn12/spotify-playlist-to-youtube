#!/usr/bin/env python3
"""Offline checks for playlist cover inheritance (yt_set_playlist_cover).

No network: urllib.request.urlopen is replaced by a router that records every
request and answers from this script. The cookie is a made-up test value.
"""
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import backend  # noqa: E402

COVER_URL = "https://i.scdn.co/image/abc123"
UPLOAD_URL = "https://upload.example/u1"
YT_COOKIE = "SID=test-only; HSID=test-only; SAPISID=test-only-value"
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 64
PLID = "PLtestplaylist"


class Headers:
    """Case-insensitive .get, like http.client.HTTPMessage."""

    def __init__(self, d=None):
        self._d = {str(k).lower(): v for k, v in (d or {}).items()}

    def get(self, name, default=None):
        return self._d.get(str(name).lower(), default)

    def items(self):
        return list(self._d.items())


class Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.headers = Headers(headers)
        self._body = body

    def read(self, *a):
        return self._body[:a[0]] if a else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def hget(headers, name):
    """Request headers are stored capitalised by urllib; compare case-insensitively."""
    low = str(name).lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == low:
            return v
    return None


class Router:
    """Replaces urlopen. Routes by URL, records (url, headers, data, timeout)."""

    def __init__(self, download=None, handshake=None, upload=None, edit=None):
        self.calls = []
        self.routes = {"download": download, "handshake": handshake, "upload": upload, "edit": edit}

    def _route(self, url):
        if "playlist_image_upload/playlist_custom_thumbnail" in url:
            return "handshake"
        if "upload.example" in url:
            return "upload"
        if "browse/edit_playlist" in url:
            return "edit"
        return "download"

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", "")
        self.calls.append({"url": url, "headers": dict(getattr(req, "headers", {}) or {}),
                           "data": getattr(req, "data", None), "timeout": timeout})
        r = self.routes.get(self._route(url))
        if isinstance(r, BaseException):
            raise r
        return r

    def urls(self):
        return [c["url"] for c in self.calls]


def ok_router(**over):
    routes = {
        "download": Resp(200, JPEG),
        "handshake": Resp(200, b"", {"X-Goog-Upload-URL": UPLOAD_URL}),
        "upload": Resp(200, json.dumps({"encryptedBlobId": "blob123"}).encode()),
        "edit": Resp(200, json.dumps({"status": "STATUS_SUCCEEDED"}).encode()),
    }
    routes.update(over)
    return Router(**routes)


class CoverTests(unittest.TestCase):
    def run_cover(self, router, cover_url=COVER_URL):
        with mock.patch("urllib.request.urlopen", router):
            return backend.yt_set_playlist_cover(PLID, cover_url, YT_COOKIE)

    # 1
    def test_empty_cover_url_is_a_skip_with_no_request(self):
        router = ok_router()
        out = self.run_cover(router, "")
        self.assertFalse(out["ok"])
        self.assertEqual(out["why"], "no cover")
        self.assertEqual(router.calls, [])

    # 2
    def test_non_image_bytes_never_reach_the_handshake(self):
        r = ok_router(download=Resp(200, b"<html>not an image</html>"))
        out = self.run_cover(r)
        self.assertFalse(out["ok"])
        self.assertEqual(out["why"], "no image")
        self.assertEqual(len(r.calls), 1, r.urls())

    # 3
    def test_happy_path_uploads_the_bytes_and_sets_the_thumbnail(self):
        r = ok_router()
        out = self.run_cover(r)
        self.assertTrue(out["ok"], out)
        self.assertEqual([c["url"] for c in r.calls],
                         [COVER_URL,
                          "https://music.youtube.com/playlist_image_upload/playlist_custom_thumbnail?authuser=0",
                          UPLOAD_URL,
                          "https://music.youtube.com/youtubei/v1/browse/edit_playlist?key="
                          + backend.YT_KEY + "&prettyPrint=false"])
        hs = r.calls[1]
        self.assertEqual(hget(hs["headers"], "X-Goog-Upload-Command"), "start")
        self.assertEqual(hget(hs["headers"], "X-Goog-Upload-Protocol"), "resumable")
        self.assertEqual(hget(hs["headers"], "X-Goog-Upload-Header-Content-Length"), str(len(JPEG)))
        self.assertEqual(hget(hs["headers"], "X-Goog-AuthUser"), "0")
        self.assertIn("text/plain", hget(hs["headers"], "Content-Type"))
        self.assertEqual(hs["data"], b"")
        up = r.calls[2]
        self.assertEqual(hget(up["headers"], "X-Goog-Upload-Command"), "upload, finalize")
        self.assertEqual(hget(up["headers"], "X-Goog-Upload-Offset"), "0")
        self.assertEqual(hget(up["headers"], "Content-Type"), "image/jpeg")
        self.assertEqual(up["data"], JPEG)
        body = json.loads(r.calls[3]["data"].decode())
        self.assertEqual(body["playlistId"], PLID)
        act = body["actions"][0]
        self.assertEqual(act["action"], "ACTION_SET_CUSTOM_THUMBNAIL")
        thumb = act["addedCustomThumbnail"]
        self.assertEqual(thumb["imageKey"]["name"], "studio_square_thumbnail")
        self.assertEqual(thumb["imageKey"]["type"], "PLAYLIST_IMAGE_TYPE_CUSTOM_THUMBNAIL")
        self.assertEqual(thumb["playlistScottyEncryptedBlobId"], "blob123")

    # 4
    def test_handshake_without_an_upload_url_is_a_skip(self):
        r = ok_router(handshake=Resp(200, b""))
        out = self.run_cover(r)
        self.assertFalse(out["ok"])
        self.assertEqual(out["why"], "upload")
        self.assertEqual(len(r.calls), 2, r.urls())

    # 5
    def test_a_403_on_the_handshake_is_never_an_exception(self):
        err = urllib.error.HTTPError(
            "https://music.youtube.com/playlist_image_upload/playlist_custom_thumbnail",
            403, "Forbidden", Headers(), None)
        out = self.run_cover(ok_router(handshake=err))
        self.assertFalse(out["ok"])
        self.assertEqual(out["why"], "upload")

    # 6
    def test_a_failing_edit_is_a_skip(self):
        r = ok_router(edit=RuntimeError("boom"))
        out = self.run_cover(r)
        self.assertFalse(out["ok"])
        self.assertEqual(out["why"], "attach")
        self.assertEqual(len(r.calls), 4, r.urls())

    # 7
    def test_malformed_upload_json_is_a_skip(self):
        r = ok_router(upload=Resp(200, b"{not json"))
        out = self.run_cover(r)
        self.assertFalse(out["ok"])
        self.assertEqual(out["why"], "upload")
        self.assertEqual(len(r.calls), 3, r.urls())

    # 8
    def test_an_edit_status_failed_is_a_skip(self):
        r = ok_router(edit=Resp(200, json.dumps({"status": "STATUS_FAILED"}).encode()))
        out = self.run_cover(r)
        self.assertFalse(out["ok"])
        self.assertEqual(out["why"], "attach")


if __name__ == "__main__":
    unittest.main(verbosity=2)
