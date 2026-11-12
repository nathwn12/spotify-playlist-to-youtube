#!/usr/bin/env python3
"""Continuation paging: both token shapes YTM serves must walk the same pages.

yt_playlist_entries is the only source of truth for what a playlist holds, so a
read that stops at page 1 reports every later track as absent. InnerTube puts
the next-page token in one of two places, depending on the reply: the legacy
`continuations[].nextContinuationData.continuation` field, or the live
`continuationItemRenderer -> continuationEndpoint -> continuationCommand ->
token` item that ends the shelf, with later pages served under
`continuationContents`. Both shapes must walk the page sequence to the end.

No network: urlopen is replaced by a fake that serves three pages.

Run: python -X utf8 tests/test_playlist_paging.py   (exit 0 only when every check passes)
"""
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

# Three pages, and "AAAAAAAAAAA" held twice on purpose: a playlist may
# legitimately carry the same track twice, so a read may never dedupe by
# videoId - only a page token may be rejected as a repeat.
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
    """The live WEB_REMIX shape: a trailing continuation item, not a continuations[] field."""
    return {"continuationItemRenderer": {"continuationEndpoint": {
        "continuationCommand": {"token": token,
                                "request": "CONTINUATION_REQUEST_TYPE_BROWSE"}}}}


class Paged:
    """Serves PAGES one at a time in one of the two shapes YTM uses, and records
    every token and every page index it was asked for."""

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
            # What a real WEB_REMIX playlist continuation returns: no shelf node at
            # all - the rows and the next token arrive in appendContinuationItemsAction.
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
    """Answers every page with the same continuation token: without a guard the
    pager would request pages forever."""

    def __init__(self):
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        page = PAGES[0] if self.calls == 1 else PAGES[1]
        return Resp(json.dumps({"contents": {"musicPlaylistShelfRenderer": {
            "contents": rows(page),
            "continuations": [{"nextContinuationData": {"continuation": "tok-same"}}]}}}))


class Shelfless:
    """A reply with no playlist shelf at all."""

    def __call__(self, req, timeout=None):
        return Resp(json.dumps({"alerts": [{"text": "that playlist does not exist"}]}))


class Paging(unittest.TestCase):
    def read(self, shape):
        upstream = Paged(shape)
        with mock.patch("urllib.request.urlopen", upstream):
            entries = backend.yt_playlist_entries(PLID, COOKIE)
        return upstream, entries

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
        """The shape live YouTube Music actually serves: proven against
        PLI-3Je28SDV8 (150 entries), whose continuation reply holds no shelf node."""
        upstream, entries = self.read("live-append")
        self.assertEqual(entries, EXPECTED)
        self.assertEqual(upstream.tokens, TOKENS)
        self.assertEqual(upstream.pages, [0, 1, 2])

    def test_a_repeated_token_cannot_spin_forever(self):
        upstream = Stuck()
        with mock.patch("urllib.request.urlopen", upstream):
            entries = backend.yt_playlist_entries(PLID, COOKIE)
        self.assertEqual(upstream.calls, 2, "the echoed token must end the read, not loop")
        self.assertEqual(entries, PAGES[0] + PAGES[1])

    def test_a_reply_without_a_shelf_is_not_an_empty_playlist(self):
        """A reply that carries no playlist shelf (a refusal, a page shape change)
        proves nothing: it must read as unread, never as a playlist holding 0."""
        upstream = Shelfless()
        with mock.patch("urllib.request.urlopen", upstream):
            entries = backend.yt_playlist_entries(PLID, COOKIE)
            read = backend.yt_playlist_read(PLID, COOKIE)
        self.assertIsNone(entries)
        self.assertIsNone(read["entries"])
        self.assertFalse(read["complete"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
