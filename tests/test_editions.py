#!/usr/bin/env python3
"""Editions: the same song on an album and on its single is two rows.

Spotify hands the transfer rows, not songs. A playlist can hold the album cut and
the single, or the deluxe cut and the standard one, with one title, one artist and
often one duration between them. Three things keep those rows faithful:

  * the Spotify row carries `album` when the player query supplies
    `albumOfTrack.name` - the edition lives there, not in the title;
  * the search cache key includes the album, so two editions never share one
    lookup (and one edition's candidates never answer for the other);
  * an album-bearing query is tried LAST, only when the plain queries did not
    identify the row, and an edition word in the source album breaks a near-tie
    among candidates already within 0.05 of the leader.

None of that is a gate: a row the album query cannot identify skips exactly as it
would have, and no row is dropped or merged. With no album metadata every one of
these paths is inert, which is why the recorded fixtures are unaffected.

Also pinned here: compound version labels. "Slowed + Reverb" and "Super Slowed +
Reverb" are different versions and must never substitute for one another.

No network: one fake answers the Spotify reads and the InnerTube searches.

Run: python -X utf8 tests/test_editions.py   (exit 0 only when every check passes)
"""
import json
import os
import sys
import unittest
import urllib.parse
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import fixture_lib  # noqa: E402

import backend  # noqa: E402

PID = "PLeditionTest001"
BUNDLE_JS = "https://open.spotifycdn.com/cdn/build/web-player/web-player.test02.js"
QHASH = "1" * 64


class Resp:
    status = 200

    def __init__(self, body=b""):
        self._body = body if isinstance(body, bytes) else str(body).encode("utf-8")

    def read(self, *a):
        return self._body[:a[0]] if a else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def json_resp(obj):
    return Resp(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def card(video_id, title, artist, duration="3:00"):
    """One InnerTube shelf row in the shape the matcher reads."""
    return {"musicShelfRenderer": {"contents": [{"musicResponsiveListItemRenderer": {
        "playlistItemData": {"videoId": video_id},
        "flexColumns": [
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                {"text": title}]}}},
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                {"text": artist}, {"text": " \u2022 "}, {"text": duration}]}}},
        ],
        "fixedColumns": [{"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [
            {"text": duration}]}}}],
    }}]}}


class SpotifyFake:
    """The keyless Spotify read: embed page, player bundle, token, pathfinder."""

    def __init__(self, rows, declared=None):
        self.rows = rows
        self.declared = len(rows) if declared is None else declared
        self.pages = 0

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", "") or str(req)
        if "/embed/playlist/" in url:
            page = {"props": {"pageProps": {"state": {"data": {"entity": {
                "title": "Editions", "coverArt": {"sources": []}}}}}}}
            return Resp('<html><script id="__NEXT_DATA__" type="application/json">'
                        + json.dumps(page) + "</script></html>")
        if url.rstrip("/") == "https://open.spotify.com":
            return Resp('<html><script src="' + BUNDLE_JS + '"></script></html>')
        if url == BUNDLE_JS:
            return Resp('var a={secret:"test-secret",version:5};'
                        'var b=["fetchPlaylist","query","' + QHASH + '"];')
        if "open.spotify.com/api/server-time" in url:
            return json_resp({"serverTime": 1700000000})
        if "open.spotify.com/api/token" in url:
            return json_resp({"accessToken": "test-anon-token",
                              "accessTokenExpirationTimestampMs": 4102444800000})
        if "api-partner.spotify.com/pathfinder" in url:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            variables = json.loads((qs.get("variables") or ["{}"])[0] or "{}")
            off = int(variables.get("offset", 0) or 0)
            limit = int(variables.get("limit", 100) or 100)
            items = []
            for row in self.rows[off:off + limit]:
                data = {"__typename": "Track", "uri": row["uri"], "name": row["title"],
                        "artists": {"items": [{"profile": {"name": row["artist"]}}]},
                        "trackDuration": {"totalMilliseconds": row["duration_ms"]}}
                if row.get("album"):
                    data["albumOfTrack"] = {"name": row["album"]}
                items.append({"itemV2": {"data": data}})
            self.pages += 1
            return json_resp({"data": {"playlistV2": {
                "__typename": "Playlist", "name": "Editions",
                "content": {"totalCount": self.declared, "items": items}}}})
        raise AssertionError("no fake for " + url)


class SearchFake:
    """InnerTube search: answers by query text, exactly as recorded."""

    def __init__(self, table):
        self.table = table
        self.queries = []

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", "") or str(req)
        if "youtubei/v1/search" not in url:
            raise AssertionError("no fake for " + url)
        body = json.loads((getattr(req, "data", b"") or b"{}").decode("utf-8"))
        q = str(body.get("query", ""))
        self.queries.append(q)
        if q not in self.table:
            raise AssertionError("unexpected query: " + q)
        return json_resp(fixture_lib.envelope(self.table[q]))


class Editions(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(backend, k) for k in ("_SEARCH_CACHE", "_SP_TOK", "_SP_BUNDLE")}
        backend._SEARCH_CACHE = {}
        backend._SP_TOK = {"token": "", "exp_ms": 0}
        backend._SP_BUNDLE = {"js": "", "qhash": "", "at": 0.0}
        self._sleep = mock.patch("time.sleep", lambda *a, **k: None)
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        for k, v in self._saved.items():
            setattr(backend, k, v)

    def test_the_spotify_read_carries_the_album_when_the_player_supplies_it(self):
        rows = [
            {"uri": "spotify:track:1", "title": "Neon", "artist": "Artist",
             "duration_ms": 180000, "album": "Night Drive (Deluxe Edition)"},
            {"uri": "spotify:track:2", "title": "Neon", "artist": "Artist",
             "duration_ms": 180000, "album": "Night Drive"},
            {"uri": "spotify:track:3", "title": "No Album", "artist": "Artist",
             "duration_ms": 170000, "album": ""},
        ]
        fake = SpotifyFake(rows)
        with mock.patch("urllib.request.urlopen", fake):
            got = backend.fetch_spotify_tracks("https://open.spotify.com/playlist/" + PID, "")
        self.assertEqual([t["album"] for t in got["tracks"]],
                         ["Night Drive (Deluxe Edition)", "Night Drive", ""])
        self.assertEqual(len(got["tracks"]), 3)
        # The edition is on the row, so the transfer can tell the two Neons apart;
        # nothing was dropped or merged on the way.
        self.assertEqual([t["uri"] for t in got["tracks"]],
                         ["spotify:track:1", "spotify:track:2", "spotify:track:3"])

    def test_two_editions_never_share_one_search_lookup(self):
        a = backend._search_key("Neon Artist", "Neon", "Artist", 180000,
                                album="Night Drive (Deluxe Edition)")
        b = backend._search_key("Neon Artist", "Neon", "Artist", 180000,
                                album="Night Drive")
        c = backend._search_key("Neon Artist", "Neon", "Artist", 180000,
                                album="Night Drive (Deluxe Edition)")
        self.assertNotEqual(a, b, "two editions shared one cache key")
        self.assertEqual(a, c, "the same edition must reuse its own lookup")
        # No album metadata at all: the key is exactly what it was before.
        self.assertEqual(backend._search_key("Neon Artist", "Neon", "Artist", 180000),
                         backend._search_key("Neon Artist", "Neon", "Artist", 180000))

    def test_the_album_query_is_tried_last_and_only_when_the_plain_ones_fail(self):
        plain = backend._query_variants("Neon", "Artist")
        self.assertEqual(plain, ["Neon Artist", "Neon", "Artist - Neon"],
                         "a row with no album must keep the legacy variants exactly")
        with_album = backend._query_variants("Neon", "Artist", "Night Drive (Deluxe)")
        self.assertEqual(with_album[:3], plain)
        self.assertEqual(with_album[3:], ["Neon Artist Night Drive (Deluxe)",
                                          "Neon Night Drive (Deluxe)"],
                         "the album variants are appended last")

    def test_a_plain_query_that_identifies_the_row_is_never_re_asked(self):
        table = {"Neon Artist": [card("ALBUMCUT001", "Neon", "Artist")]}
        fake = SearchFake(table)
        with mock.patch("urllib.request.urlopen", fake):
            r = backend.yt_match("Neon", "Artist", 180000, album="Night Drive (Deluxe)")
        self.assertEqual(r["videoId"], "ALBUMCUT001")
        self.assertEqual(fake.queries, ["Neon Artist"],
                         "the album variant was sent although the plain query identified the row")

    def test_the_album_query_rescues_an_edition_the_plain_queries_cannot_name(self):
        table = {
            "Encore Artist": [card("OTHERCUT001", "A Different Song", "Someone Else")],
            "Encore": [card("OTHERCUT002", "A Different Song", "Someone Else")],
            "Artist - Encore": [card("OTHERCUT003", "A Different Song", "Someone Else")],
            "Encore Artist Night Drive (Deluxe)": [
                card("DELUXECUT01", "Encore", "Artist")],
        }
        fake = SearchFake(table)
        with mock.patch("urllib.request.urlopen", fake):
            r = backend.yt_match("Encore", "Artist", 200000, album="Night Drive (Deluxe)")
        self.assertEqual(r["videoId"], "DELUXECUT01", r)
        self.assertEqual(fake.queries[-1], "Encore Artist Night Drive (Deluxe)")

    def test_a_row_the_album_query_cannot_identify_still_skips(self):
        table = {
            "Encore Artist": [card("OTHERCUT001", "A Different Song", "Someone Else")],
            "Encore": [card("OTHERCUT002", "A Different Song", "Someone Else")],
            "Artist - Encore": [card("OTHERCUT003", "A Different Song", "Someone Else")],
            "Encore Artist Night Drive (Deluxe)": [
                card("OTHERCUT004", "A Different Song", "Someone Else")],
        }
        fake = SearchFake(table)
        with mock.patch("urllib.request.urlopen", fake):
            r = backend.yt_match("Encore", "Artist", 200000, album="Night Drive (Deluxe)")
        self.assertIsNone(r.get("videoId"), r)
        self.assertTrue(r.get("skipped"), "a skip must carry its reason")

    # --- 4: the edition breaks a near-tie, and only a near-tie --------------
    def test_the_named_edition_wins_a_near_tie(self):
        cands = [
            {"videoId": "ALBUMCUT001", "title": "Neon", "artist": "Artist \u2022 Night Drive",
             "duration": "3:00"},
            {"videoId": "DELUXECUT01", "title": "Neon",
             "artist": "Artist \u2022 Night Drive (Deluxe Edition)", "duration": "3:00"},
        ]
        scored = backend._score_candidates("Neon", "Artist", 180000, cands)
        self.assertLessEqual(abs(scored[0]["score"] - scored[1]["score"]), 0.05,
                             "the fixture must be a near-tie to prove anything")
        want_deluxe = backend._prefer_edition(list(scored), "Night Drive (Deluxe Edition)")
        self.assertEqual(want_deluxe[0]["videoId"], "DELUXECUT01")
        want_album = backend._prefer_edition(list(scored), "Night Drive")
        self.assertEqual(want_album[0]["videoId"], "ALBUMCUT001",
                         "an album with no edition word must not reorder anything")
        no_album = backend._prefer_edition(list(scored), "")
        self.assertEqual([c["videoId"] for c in no_album],
                         [c["videoId"] for c in scored],
                         "without album metadata the matcher's order stands")

    def test_the_edition_never_promotes_a_candidate_the_matcher_refused(self):
        """Only candidates within 0.05 of the leader can be reordered: a deluxe
        card that scores far below the album cut stays where it is."""
        scored = [
            {"videoId": "ALBUMCUT001", "title": "Neon", "artist": "Artist \u2022 Night Drive",
             "duration": "3:00", "score": 0.95, "t": 1.0, "a": 1.0, "d": 1.0},
            {"videoId": "DELUXECUT01", "title": "Neon", "artist": "Artist \u2022 Deluxe",
             "duration": "9:59", "score": 0.55, "t": 1.0, "a": 0.5, "d": 0.0},
        ]
        out = backend._prefer_edition(list(scored), "Night Drive (Deluxe)")
        self.assertEqual(out[0]["videoId"], "ALBUMCUT001")

    def test_single_deluxe_and_ep_are_distinct_editions(self):
        for label, want in (("Night Drive (Deluxe Edition)", {"deluxe"}),
                            ("Neon (Single)", {"single"}),
                            ("Neon - EP", {"ep"}),
                            ("Night Drive (Remastered 2011)", {"remaster"}),
                            ("Night Drive", set())):
            self.assertEqual(set(backend._edition_sig(label)), want, label)

    def test_compound_version_labels_are_distinct_versions(self):
        rows = ["Song - Slowed + Reverb", "Song - Super Slowed + Reverb", "Song - Slowed",
                "Song - Reverb", "Song"]
        sigs = [backend._sig_label(backend._version_sig(t)) for t in rows]
        self.assertEqual(len(set(sigs)), len(rows), sigs)
        self.assertEqual(sigs[0], "reverb+slowed", sigs[0])
        self.assertEqual(sigs[1], "reverb+slowed+super", sigs[1])

    def test_a_compound_source_picks_the_identical_compound_candidate(self):
        cands = [
            {"videoId": "SLOWREV001", "title": "Song - Slowed + Reverb",
             "artist": "Artist \u2022 3:20", "duration": "3:20"},
            {"videoId": "SUPSREV001", "title": "Song - Super Slowed + Reverb",
             "artist": "Artist \u2022 3:45", "duration": "3:45"},
        ]
        r = backend._pick_best("Song - Super Slowed + Reverb Artist",
                               "Song - Super Slowed + Reverb", "Artist", 225000, cands)
        self.assertEqual(r["videoId"], "SUPSREV001", r)
        r = backend._pick_best("Song - Slowed + Reverb Artist",
                               "Song - Slowed + Reverb", "Artist", 200000, cands)
        self.assertEqual(r["videoId"], "SLOWREV001", r)

    def test_a_plain_source_never_takes_a_compound_slowed_copy(self):
        cands = [{"videoId": "SLOWREV001", "title": "Song - Slowed + Reverb",
                  "artist": "Artist", "duration": "3:20"}]
        with self.assertRaises(RuntimeError) as ctx:
            backend._pick_best("Song Artist", "Song", "Artist", 180000, cands)
        self.assertIn("versions found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
