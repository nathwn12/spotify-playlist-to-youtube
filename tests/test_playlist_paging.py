#!/usr/bin/env python3
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import backend  # noqa: E402

PLID = "PLpagingTest001"
COOKIE = "SID=test-only; HSID=test-only; SAPISID=test-only-value"

PAGES = [["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"],
         ["DDDDDDDDDDD", "AAAAAAAAAAA"],
         ["EEEEEEEEEEE", "FFFFFFFFFFF"]]
TOKENS = ["tok-page-2", "tok-page-3"]
EXPECTED = [v for page in PAGES for v in page]


class Resp:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")

    def read(self, *a):
        return self._body[:a[0]] if a else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def rows(ids):
    return [{"musicResponsiveListItemRenderer": {"playlistItemData": {"videoId": v}}} for v in ids]


def live_token(token):
    return {"continuationItemRenderer": {"continuationEndpoint": {
        "continuationCommand": {"token": token,
                                "request": "CONTINUATION_REQUEST_TYPE_BROWSE"}}}}


class Paged:

    def __init__(self, shape):
        self.shape = shape
        self.tokens = []
        self.pages = []

    def __call__(self, req, timeout=None):
        body = json.loads(getattr(req, "data", b"") or b"{}")
        token = body.get("continuation") or ""
        idx = 0
        if token:
            if token not in TOKENS:
                raise AssertionError("unexpected continuation %r" % token)
            idx = TOKENS.index(token) + 1
            self.tokens.append(token)
        self.pages.append(idx)
        nxt = TOKENS[idx] if idx < len(TOKENS) else ""
        return Resp(json.dumps(self.reply(PAGES[idx], nxt)))

    def reply(self, page, nxt):
        contents = rows(page)
        if self.shape == "live":
            if nxt:
                contents = contents + [live_token(nxt)]
            inner = {"contents": contents}
            if self.pages[-1] == 0:
                return {"contents": {"musicPlaylistShelfRenderer": inner}}
            return {"continuationContents": {"musicPlaylistShelfContinuation": inner}}
        if self.shape == "live-append":
            if nxt:
                contents = contents + [live_token(nxt)]
            if self.pages[-1] == 0:
                return {"contents": {"musicPlaylistShelfRenderer": {"contents": contents}}}
            return {"onResponseReceivedActions": [
                {"appendContinuationItemsAction": {"continuationItems": contents,
                                                   "targetId": "VL" + PLID}}]}
        shelf = {"contents": contents}
        if nxt:
            shelf["continuations"] = [{"nextContinuationData": {"continuation": nxt}}]
        return {"contents": {"musicPlaylistShelfRenderer": shelf}}


class Stuck:

    def __init__(self):
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        page = PAGES[0] if self.calls == 1 else PAGES[1]
        return Resp(json.dumps({"contents": {"musicPlaylistShelfRenderer": {
            "contents": rows(page),
            "continuations": [{"nextContinuationData": {"continuation": "tok-same"}}]}}}))


class Shelfless:

    def __call__(self, req, timeout=None):
        return Resp(json.dumps({"alerts": [{"text": "that playlist does not exist"}]}))


def shelf(contents, continuations=None):
    inner = {"contents": contents}
    if continuations is not None:
        inner["continuations"] = continuations
    return {"contents": {"musicPlaylistShelfRenderer": inner}}


class OneReply:

    def __init__(self, body):
        self.body = body
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        return Resp(json.dumps(self.body))


class Paging(unittest.TestCase):
    def read(self, shape):
        upstream = Paged(shape)
        with mock.patch("urllib.request.urlopen", upstream):
            entries = backend.yt_playlist_entries(PLID, COOKIE)
        return upstream, entries

    def read_reply(self, body):
        upstream = OneReply(body)
        with mock.patch("urllib.request.urlopen", upstream):
            read = backend.yt_playlist_read(PLID, COOKIE)
        return upstream, read

    def test_an_unrecognised_row_renderer_is_unread_not_the_end_of_the_list(self):
        _, read = self.read_reply(shelf([
            rows(["AAAAAAAAAAA"])[0],
            {"musicResponsiveListItemRendererV2": {"videoId": "BBBBBBBBBBB"}}]))
        self.assertFalse(read["complete"], read)
        self.assertIn("renderer", read["why"])

    def test_a_row_without_a_parsable_video_id_is_unread_not_skipped(self):
        _, read = self.read_reply(shelf([
            rows(["AAAAAAAAAAA"])[0],
            {"musicResponsiveListItemRenderer": {"flexColumns": []}}]))
        self.assertFalse(read["complete"], read)
        self.assertIn("videoId", read["why"])

    def test_an_unknown_continuation_representation_is_not_the_end_of_list(self):
        upstream, read = self.read_reply(shelf(
            rows(PAGES[0]), [{"nextContinuationData": {"cursor": "opaque"}}]))
        self.assertFalse(read["complete"], read)
        self.assertIn("continuation", read["why"])
        self.assertEqual(upstream.calls, 1, "the read asked for a page it cannot address")

    def test_a_row_marker_that_is_not_a_page_token_is_not_end_of_list(self):
        item = {"continuationItemRenderer": {"continuationEndpoint": {
            "continuationCommand": {"request": "CONTINUATION_REQUEST_TYPE_BROWSE"}}}}
        _, read = self.read_reply(shelf(rows(PAGES[0]) + [item]))
        self.assertFalse(read["complete"], read)

    def test_a_repeated_token_cannot_spin_forever(self):
        upstream = Stuck()
        with mock.patch("urllib.request.urlopen", upstream):
            read = backend.yt_playlist_read(PLID, COOKIE)
        self.assertEqual(upstream.calls, 2, "the echoed token must end the read, not loop")
        self.assertEqual(read["entries"], PAGES[0] + PAGES[1])
        self.assertFalse(read["complete"], "a repeated token proves nothing about the tail")

    def test_legacy_continuations_shape_walks_every_page(self):
        upstream, entries = self.read("legacy")
        self.assertEqual(entries, EXPECTED)
        self.assertEqual(upstream.tokens, TOKENS)
        self.assertEqual(upstream.pages, [0, 1, 2])

    def test_live_continuation_item_shape_walks_the_same_pages(self):
        upstream, entries = self.read("live")
        self.assertEqual(entries, EXPECTED)
        self.assertEqual(upstream.tokens, TOKENS)
        self.assertEqual(upstream.pages, [0, 1, 2])

    def test_live_append_continuation_items_shape_walks_the_same_pages(self):
        upstream, entries = self.read("live-append")
        self.assertEqual(entries, EXPECTED)
        self.assertEqual(upstream.tokens, TOKENS)
        self.assertEqual(upstream.pages, [0, 1, 2])

    def test_a_reply_without_a_shelf_is_not_an_empty_playlist(self):
        upstream = Shelfless()
        with mock.patch("urllib.request.urlopen", upstream):
            entries = backend.yt_playlist_entries(PLID, COOKIE)
            read = backend.yt_playlist_read(PLID, COOKIE)
        self.assertIsNone(entries)
        self.assertIsNone(read["entries"])
        self.assertFalse(read["complete"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
