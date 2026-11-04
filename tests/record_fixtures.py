#!/usr/bin/env python3
"""Record the owner-data acceptance fixtures from the real services.

Budget: at most 40 real YouTube Music searches, 300 ms apart. The playlist
read is one keyless Spotify fetch. Everything the gate later does is offline.

Run from the repo root:  python3 tests\\record_fixtures.py
"""
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import backend  # noqa: E402
import fixture_lib  # noqa: E402

PLAYLIST_URL = "https://open.spotify.com/playlist/0RpUj3uJ5DhWDSUTUeq2k6"
MAX_SEARCHES = 40  # hard cap; this pass spent two probe searches before recording
SPACING_S = 0.3
OUT = fixture_lib.FIXTURE

# The ten version titles that previously mismatched. Four share one title,
# which is the distinct-id check in the gate.
VERSION_ROWS = [
    ("MONTAGEM RETINAL PLATINA - Sped Up", None),
    ("MONTAGEM RETINAL PLATINA - Slowed", None),
    ("MONTAGEM RETINAL PLATINA - Super Slowed", None),
    ("MONTAGEM RETINAL PLATINA - Ultra Slowed", None),
    ("YOLANDA RAYO Vol. 2 - Slowed", ("DJ DYLANFK, DJ KCH", 0)),
    ("YOLANDA RAYO Vol. 2", ("DJ DYLANFK, DJ KCH", 0)),
    ("FAZ A FINTA - Slowed", None),
    ("FAZ A FINTA - Ultra Slowed", None),
    ("MONTAGEM RENICHT MIX - Alternative Version Ultra Slowed", None),
    ("OGAME", None),
]
VERSION_TITLES = {t for t, _ in VERSION_ROWS}
SAMPLE_SIZE = 20


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "row"


def real_search(query):
    body = json.dumps({"context": {"client": backend.CLIENT}, "query": query,
                       "params": "EgWKAQIIAWoQEAMQBBAJEAkQBRAK"}).encode()
    req = urllib.request.Request(
        f"https://music.youtube.com/youtubei/v1/search?key={backend.YT_KEY}&prettyPrint=false",
        data=body, headers={"Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0",
                            "Origin": backend.YT_ORIGIN,
                            "Referer": backend.YT_ORIGIN + "/"})
    return json.loads(urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore"))


def runs(node):
    return [{"text": r.get("text", "")} for r in ((node or {}).get("runs") or [])
            if isinstance(r, dict)]


def compact_card(card):
    title = card.get("title") or {}
    title_runs = []
    for r in title.get("runs") or []:
        if not isinstance(r, dict):
            continue
        rr = {"text": r.get("text", "")}
        vid = ((((r.get("navigationEndpoint") or {}).get("watchEndpoint")) or {}).get("videoId"))
        if vid:
            rr["navigationEndpoint"] = {"watchEndpoint": {"videoId": vid}}
        title_runs.append(rr)
    out = {"title": {"runs": title_runs}}
    if card.get("subtitle") is not None:
        out["subtitle"] = {"runs": runs(card.get("subtitle"))}
    return out


def compact_mrl(mrl):
    out = {}
    pid = (mrl.get("playlistItemData") or {}).get("videoId")
    if pid:
        out["playlistItemData"] = {"videoId": pid}
    flexible = []
    for col in mrl.get("flexColumns") or []:
        node = col.get("musicResponsiveListItemFlexColumnRenderer") or {}
        flexible.append({"musicResponsiveListItemFlexColumnRenderer":
                         {"text": {"runs": runs(node.get("text"))}}})
    if flexible:
        out["flexColumns"] = flexible
    fixed = []
    for col in mrl.get("fixedColumns") or []:
        node = col.get("musicResponsiveListItemFixedColumnRenderer") or {}
        fixed.append({"musicResponsiveListItemFixedColumnRenderer":
                      {"text": {"runs": runs(node.get("text"))}}})
    if fixed:
        out["fixedColumns"] = fixed
    nav = ((mrl.get("navigationEndpoint") or {}).get("watchEndpoint") or {}).get("videoId")
    if nav:
        out["navigationEndpoint"] = {"watchEndpoint": {"videoId": nav}}
    overlay_node = (((mrl.get("overlay") or {}).get("musicItemThumbnailOverlayRenderer") or {})
                    .get("content") or {}).get("musicPlayButtonRenderer") or {}
    overlay = ((overlay_node.get("playNavigationEndpoint") or {})
               .get("watchEndpoint") or {}).get("videoId")
    if overlay:
        out["overlay"] = {"musicItemThumbnailOverlayRenderer": {"content": {
            "musicPlayButtonRenderer": {"playNavigationEndpoint":
                                        {"watchEndpoint": {"videoId": overlay}}}}}}
    return out


def compact_sections(sections):
    out = []
    for sec in sections or []:
        if not isinstance(sec, dict):
            continue
        cs = {}
        card = sec.get("musicCardShelfRenderer")
        if isinstance(card, dict):
            cs["musicCardShelfRenderer"] = compact_card(card)
        for key in ("musicShelfRenderer", "itemSectionRenderer"):
            node = sec.get(key)
            if not isinstance(node, dict):
                continue
            items = []
            for item in node.get("contents") or []:
                mrl = (item or {}).get("musicResponsiveListItemRenderer")
                if isinstance(mrl, dict):
                    items.append({"musicResponsiveListItemRenderer": compact_mrl(mrl)})
            if items:
                cs[key] = {"contents": items}
        if cs:
            out.append(cs)
    return out


def sections_of(raw):
    try:
        return raw["contents"]["tabbedSearchResultsRenderer"]["tabs"][0]["tabRenderer"][
            "content"]["sectionListRenderer"]["contents"]
    except Exception:
        return []


def main():
    print("spotify: reading", PLAYLIST_URL)
    sp = backend.fetch_spotify_tracks(PLAYLIST_URL)
    tracks = sp.get("tracks") or []
    if not tracks:
        raise SystemExit("record: spotify returned no tracks")
    by_title = {}
    for t in tracks:
        by_title.setdefault(t.get("title", ""), t)
    print(f"spotify: {sp.get('playlist_name')} declared={sp.get('declared_total')} fetched={len(tracks)}")

    rows = []
    for title, fallback in VERSION_ROWS:
        hit = by_title.get(title)
        artist = hit.get("artist", "") if hit else fallback[0]
        duration = int(hit.get("duration_ms", 0)) if hit else int(fallback[1])
        rows.append({"id": slug(title), "kind": "version",
                     "source": {"title": title, "artist": artist,
                                "duration_ms": duration}})

    picked = []
    n = len(tracks)
    for i in range(SAMPLE_SIZE):
        idx = round(i * (n - 1) / (SAMPLE_SIZE - 1))
        while idx < n:
            t = tracks[idx]
            title = (t.get("title") or "").strip()
            if title and title not in VERSION_TITLES and title not in [p[0] for p in picked]:
                break
            idx += 1
        if idx >= n:
            raise SystemExit(f"record: could not sample track {i + 1}")
        picked.append((t["title"], t))
    for title, t in picked:
        rows.append({"id": slug(title) + "-" + slug(t.get("artist", ""))[:20],
                     "kind": "playlist",
                     "source": {"title": title, "artist": t.get("artist", ""),
                                "duration_ms": int(t.get("duration_ms", 0))}})

    responses = {}
    searches = 0

    def search(query):
        nonlocal searches
        if query in responses:
            return responses[query]
        if searches >= MAX_SEARCHES:
            raise SystemExit(f"record: search budget {MAX_SEARCHES} exhausted at: {query}")
        if searches:
            time.sleep(SPACING_S)
        raw = real_search(query)
        searches += 1
        responses[query] = raw
        print(f"search {searches:02d}/{MAX_SEARCHES}  {query}")
        return raw

    failures = []
    for row in rows:
        title = row["source"]["title"]
        artist = row["source"]["artist"]
        ms = row["source"]["duration_ms"]
        sig = backend._sig_label(backend._version_sig(title))
        variants = [f"{title} {artist}".strip(), title, f"{artist} - {title}".strip()]
        backend._SEARCH_CACHE.clear()
        queries, best, last_error = [], None, ""
        for q in variants:
            if not q or q in queries:
                continue
            raw = search(q)
            queries.append(q)
            candidates = backend._candidates_from_search_json(raw)
            backend._search_cache_put(backend._search_key(q, title, artist, ms), candidates)
            try:
                best = backend.yt_search(q, title=title, artist=artist, duration_ms=ms)
                break
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
        expect = {"signature": sig}
        if best:
            chosen_sig = backend._sig_label(backend._version_sig(best.get("title", "")))
            expect.update({"videoId": best["videoId"], "video_title": best.get("title", ""),
                           "duration": best.get("duration", "")})
            if chosen_sig != sig:
                failures.append(f"{row['id']}: matched signature {chosen_sig} != source {sig}")
            print(f"ok   {row['id']}: {sig} -> {best['videoId']} | {best.get('title', '')}")
        else:
            cls = fixture_lib.skip_class(last_error)
            expect.update({"skip": cls, "skip_reason": last_error[:200]})
            print(f"skip {row['id']}: {sig} -> {cls} | {last_error[:120]}")
        row["queries"] = queries
        row["expect"] = expect

    if failures:
        raise SystemExit("record: refusing to write\n" + "\n".join(failures))

    compact = {}
    for query, raw in responses.items():
        full = backend._candidates_from_search_json(raw)
        trimmed = compact_sections(sections_of(raw))
        got = backend._candidates_from_search_json(fixture_lib.envelope(trimmed))
        if got != full:
            raise SystemExit(f"record: compaction changed candidates for: {query}")
        compact[query] = trimmed

    data = {
        "version": 1,
        "recorded_at": time.strftime("%Y-%m-%d"),
        "playlist": {"id": sp.get("playlist_id", ""), "name": sp.get("playlist_name", ""),
                     "declared_total": sp.get("declared_total", 0)},
        "search_budget": {"real_searches": searches, "limit": MAX_SEARCHES,
                          "spacing_ms": int(SPACING_S * 1000)},
        "rows": rows,
        "responses": compact,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    size = os.path.getsize(OUT)
    print(f"wrote {OUT} rows={len(rows)} searches={searches} bytes={size}")


if __name__ == "__main__":
    main()
