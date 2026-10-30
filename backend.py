#!/usr/bin/env python3
"""sp2yt keyless web service - stdlib only. No API keys, no secrets stored.
Spotify: scrapes public embed __NEXT_DATA__ (optional user Cookie forwarded).
YTM: InnerTube WEB_REMIX + user-supplied Cookie (SAPISIDHASH derived server-side).
"""
import json, re, sys, time, base64, hashlib, shutil, sqlite3, subprocess, tempfile, threading, urllib.request, urllib.parse, webbrowser
import ctypes
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
try:
    from Crypto.Cipher import AES
    HAVE_AES = True
except Exception:
    HAVE_AES = False

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
LOGF = os.path.join(HERE, "server.log")
HISTORY = []

def blog(event: str, detail: str = ""):
    """Audit log. Never writes cookie values — lengths only."""
    line = f"{time.strftime('%H:%M:%S')} {event} {detail[:220]}".rstrip()
    HISTORY.append(line)
    del HISTORY[:-50]
    try:
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def scrub_cookie(c: str) -> str:
    c = normalize_cookie(c)
    n = len(re.findall(r"[A-Za-z0-9_-]+=", c))
    return f"cookie_pairs={n} chars={len(c)}" if c else "no-cookie"

def classify(msg: str) -> dict:
    m = (msg or "").lower()
    if "not a spotify" in m:
        return {"kind": "user", "hint": "That is not a Spotify playlist link — paste an open.spotify.com/playlist/… URL."}
    if "401" in m or "unauthorized" in m or "sapisid" in m or "login" in m:
        return {"kind": "auth", "hint": "YTM/Spotify login rejected it — cookie expired or wrong site. Re-export fresh cookie file and re-upload."}
    if "429" in m or "rate" in m or "quota" in m or "too many" in m:
        return {"kind": "throttle", "hint": "Rate-limited — auto-retrying with fewer workers."}
    if "timed out" in m or "timeout" in m or "urlerror" in m or "connection" in m:
        return {"kind": "transient", "hint": "Network blip — auto-retrying once."}
    if "embed parse" in m or "no ytm result" in m:
        return {"kind": "parse", "hint": "Upstream page format changed — run stopped for inspection."}
    return {"kind": "unknown", "hint": "Stopped. Copy this error line to the agent for diagnosis."}

YT_KEY = "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30"
YT_ORIGIN = "https://music.youtube.com"
CLIENT = {"clientName": "WEB_REMIX", "clientVersion": "1.20251027.01.00", "hl": "en", "gl": "US"}

def normalize_cookie(raw: str = "") -> str:
    """Accept typed 'a=b; c=d' header OR Netscape cookie-file content. Returns header."""
    if not raw:
        return ""
    s = raw.strip().strip('"').strip("'")
    if s[:7].lower() == "cookie:":
        s = s[7:].strip()
    if "# Netscape" not in s and "\t" not in s and "\n" not in s:
        return s
    pairs = []
    for line in s.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line[:7].lower() == "cookie:":
            line = line[7:].strip()
        if "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5].strip() and parts[6].strip() not in ("", "__Host-"):
                pairs.append(f"{parts[5].strip()}={parts[6].strip()}")
            continue
        if "=" in line and "://" not in line:
            for p in line.split(";"):
                p = p.strip()
                if "=" in p and not p.startswith("#"):
                    pairs.append(p)
    # de-dupe, keep last occurrence
    seen = {}
    for p in pairs:
        seen[p.split("=", 1)[0].strip()] = p.split("=", 1)[1].strip()
    return "; ".join(f"{k}={v}" for k, v in seen.items())

def extract_playlist_id(url: str) -> str:
    m = re.search(r"playlist/([A-Za-z0-9]+)", url or "")
    if not m:
        raise ValueError("not a spotify playlist url")
    return m.group(1)

def fetch_spotify_tracks(playlist_url: str, cookie: str = ""):
    pid = extract_playlist_id(playlist_url)
    embed = f"https://open.spotify.com/embed/playlist/{pid}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
    cookie = normalize_cookie(cookie)
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(embed, headers=headers)
    raw = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    m = re.search(r"__NEXT_DATA__[^>]*>(.*?)</script>", raw, re.S)
    if not m:
        raise RuntimeError("embed parse failed (spotify changed html?)")
    data = json.loads(m.group(1))
    entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    out = []
    for t in entity.get("trackList", []):
        out.append({
            "uri": t.get("uri", ""),
            "title": t.get("title", ""),
            "artist": t.get("subtitle", ""),
            "duration_ms": t.get("duration", 0),
        })
    return {"playlist_id": pid, "playlist_name": entity.get("title", ""), "tracks": out}

def yt_search(query: str):
    body = json.dumps({"context": {"client": CLIENT}, "query": query,
                       "params": "EgWKAQIIAWoQEAMQBBAJEAkQBRAK"}).encode()
    req = urllib.request.Request(
        f"https://music.youtube.com/youtubei/v1/search?key={YT_KEY}&prettyPrint=false",
        data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
                            "Origin": YT_ORIGIN, "Referer": YT_ORIGIN + "/"})
    r = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    j = json.loads(r)
    # walk to first song result: contents -> tabbedSearchResultsRenderer -> ...
    try:
        sections = j["contents"]["tabbedSearchResultsRenderer"]["tabs"][0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"]
        for sec in sections:
            ms = sec.get("musicShelfRenderer", {}).get("contents", [])
            for item in ms:
                mrl = item.get("musicResponsiveListItemRenderer", {})
                pid = (mrl.get("playlistItemData", {}) or {}).get("videoId")
                # fallback: overlay / navigation endpoints
                if not pid:
                    ov = json.dumps(mrl)
                    mm = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', ov)
                    if mm:
                        pid = mm.group(1)
                if pid:
                    # title = first text run
                    flex = mrl.get("flexColumns", [])
                    title = ""
                    try:
                        title = flex[0]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"][0]["text"]
                    except Exception:
                        pass
                    # duration from fixedColumns
                    dur = ""
                    try:
                        dur = mrl["fixedColumns"][0]["musicResponsiveListItemFixedColumnRenderer"]["text"]["runs"][0]["text"]
                    except Exception:
                        pass
                    return {"videoId": pid, "title": title, "duration": dur, "query": query}
    except Exception as e:
        pass
    # regex fallback (proven to work keyless)
    m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', r)
    if m:
        return {"videoId": m.group(1), "title": "", "duration": "", "query": query}
    raise RuntimeError("no ytm result for: " + query)

def parse_dur(s: str = ""):
    """'3:45' -> 225. None when unknown."""
    try:
        p = [int(x) for x in str(s).strip().split(":")]
        if len(p) == 2:
            return p[0] * 60 + p[1]
        if len(p) == 3:
            return p[0] * 3600 + p[1] * 60 + p[2]
    except Exception:
        pass
    return None

def title_overlap(a: str, b: str) -> bool:
    wa = set(re.findall(r"[a-z0-9]{3,}", (a or "").lower()))
    wb = set(re.findall(r"[a-z0-9]{3,}", (b or "").lower()))
    return bool(wa & wb)

def yt_match(title: str, artist: str = "", duration_ms: int = 0) -> dict:
    """One song, isolated. Tries query variants, prefers duration match.
    Never raises: no match -> videoId None (skip), never a batch failure."""
    title, artist = (title or "").strip(), (artist or "").strip()
    want = (duration_ms or 0) // 1000
    variants = [f"{title} {artist}".strip(), title, f"{artist} - {title}".strip()]
    first = None
    for q in variants:
        if not q:
            continue
        try:
            r = yt_search(q)
        except Exception:
            continue
        want_q = f"{title} {artist}".strip()
        if r.get("title") and not title_overlap(want_q, r["title"]):
            continue  # parsed shelf title shares nothing with the song — wrong result, next variant
        else:
            if first is None:
                first = {**r, "via": q}
            got = parse_dur(r.get("duration", ""))
            if want and got and abs(got - want) <= 12:
                return {"ok": True, "query": f"{title} {artist}".strip(), **r, "via": q, "alt": q != variants[0]}
            if not want or got is None:
                return {"ok": True, "query": f"{title} {artist}".strip(), **r, "via": q, "alt": q != variants[0]}
    if first:
        return {"ok": True, "query": f"{title} {artist}".strip(), **first, "alt": first.get("via") != variants[0]}
    return {"ok": True, "query": f"{title} {artist}".strip(), "videoId": None,
            "title": "", "duration": "", "skipped": "no match on any variant"}

def yt_search_safe(query: str):
    try:
        r = yt_search(query)
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "query": query, "error": f"{type(e).__name__}: {e}"}

def yt_search_batch(items, workers: int = 8):
    """items: track dicts {title, artist, duration_ms} or plain query strings.
    Every item resolves independently; item failure is a skip, never a batch failure."""
    from concurrent.futures import ThreadPoolExecutor
    workers = max(1, min(int(workers or 8), 10))

    def one(it):
        try:
            if isinstance(it, dict):
                return yt_match(it.get("title", ""), it.get("artist", ""), it.get("duration_ms", 0))
            return yt_search_safe(str(it))
        except Exception as e:
            q = it.get("title", "?") if isinstance(it, dict) else str(it)
            return {"ok": True, "query": q, "videoId": None, "title": "", "duration": "", "skipped": str(e)}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, items))

def check_spotify_cookie(cookie: str = "") -> dict:
    c = normalize_cookie(cookie)
    if not c:
        return {"ok": True, "msg": "not provided (optional for public playlists)"}
    try:
        req = urllib.request.Request(
            "https://open.spotify.com/get_access_token?reason=transport&productType=web-player",
            headers={"User-Agent": "Mozilla/5.0", "Cookie": c})
        r = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        if r.get("accessToken"):
            return {"ok": True, "msg": "valid"}
        return {"ok": False, "msg": "expired — export a fresh cookie file from open.spotify.com"}
    except Exception as e:
        return {"ok": False, "msg": f"expired or rejected (HTTP {getattr(e, 'code', '?')}) — export a fresh cookie file from open.spotify.com"}

def check_yt_cookie(cookie: str = "") -> dict:
    c = normalize_cookie(cookie)
    if not c:
        return {"ok": False, "msg": "missing — upload your music.youtube.com cookie file"}
    if not re.search(r"(?:__Secure-3PAPISID|SAPISID)=([^;]+)", c):
        return {"ok": False, "msg": "no SAPISID in file — re-export the full Cookie from music.youtube.com"}
    try:
        body = json.dumps({"context": {"client": CLIENT}, "browseId": "FEmusic_library_landing"}).encode()
        req = urllib.request.Request(
            f"https://music.youtube.com/youtubei/v1/browse?key={YT_KEY}&prettyPrint=false",
            data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
                                "Origin": YT_ORIGIN, "Referer": YT_ORIGIN + "/", "Cookie": c,
                                "Authorization": sapisid_hash(c), "X-Origin": YT_ORIGIN})
        txt = urllib.request.urlopen(req, timeout=20).read().decode()
        if '"logged_in","value":"1"' in txt:
            return {"ok": True, "msg": "valid"}
        return {"ok": False, "msg": "expired — export a fresh cookie file from music.youtube.com and re-upload"}
    except Exception as e:
        return {"ok": False, "msg": f"rejected ({type(e).__name__} {getattr(e, 'code', '')}) — export a fresh cookie file and re-upload"}

def sapisid_hash(cookie: str, origin: str = YT_ORIGIN) -> str:
    cookie = normalize_cookie(cookie)
    m = re.search(r"(?:__Secure-3PAPISID|SAPISID)=([^;]+)", cookie)
    if not m:
        raise ValueError("yt cookie missing SAPISID (paste full Cookie from music.youtube.com DevTools)")
    sid = m.group(1)
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"

def yt_create_playlist(yt_cookie: str, title: str, description: str, video_ids: list, privacy: str = "PRIVATE"):
    yt_cookie = normalize_cookie(yt_cookie)
    auth = sapisid_hash(yt_cookie)
    base_h = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
              "Origin": YT_ORIGIN, "Referer": YT_ORIGIN + "/", "Cookie": yt_cookie,
              "Authorization": auth, "X-Origin": YT_ORIGIN}
    # 1. create
    body = json.dumps({"context": {"client": CLIENT}, "title": title,
                       "description": description or "", "privacyStatus": privacy}).encode()
    req = urllib.request.Request(
        f"https://music.youtube.com/youtubei/v1/playlist/create?key={YT_KEY}&prettyPrint=false",
        data=body, headers=base_h)
    r = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    j = json.loads(r)
    plid = j.get("playlistId") or j.get("playlistID")
    if not plid:
        raise RuntimeError("create failed: " + r[:400])
    # 2. add videos in chunks of 50
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i+50]
        actions = [{"action": "ACTION_ADD_VIDEO", "addedVideoId": v} for v in chunk]
        b2 = json.dumps({"context": {"client": CLIENT}, "playlistId": plid, "actions": actions}).encode()
        req2 = urllib.request.Request(
            f"https://music.youtube.com/youtubei/v1/browse/edit_playlist?key={YT_KEY}&prettyPrint=false",
            data=b2, headers=base_h)
        # NOTE: correct endpoint is playlist/edit ; try edit_playlist then fallback
        try:
            urllib.request.urlopen(req2, timeout=25).read()
        except Exception:
            req3 = urllib.request.Request(
                f"https://music.youtube.com/youtubei/v1/playlist/edit?key={YT_KEY}&prettyPrint=false",
                data=b2, headers=base_h)
            urllib.request.urlopen(req3, timeout=25).read()
    return {"playlistId": plid, "url": f"https://music.youtube.com/playlist?list={plid}", "added": len(video_ids)}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send_json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            p = os.path.join(HERE, "index.html")
            with open(p, "rb") as f:
                b = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/health":
            self.send_json({"ok": True})
        elif self.path == "/api/status":
            self.send_json({"ok": True, "recent": HISTORY[-20:]})
        else:
            self.send_json({"error": "not found"}, 404)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 2_000_000:
            return self.send_json({"ok": False, "error": "body too large"}, 413)
        try:
            payload = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return self.send_json({"error": "bad json"}, 400)
        try:
            if self.path == "/api/spotify":
                r = fetch_spotify_tracks(payload.get("url", ""), payload.get("spotify_cookie", ""))
                blog("spotify", f"ok name={r.get('playlist_name')} n={len(r.get('tracks', []))} {scrub_cookie(payload.get('spotify_cookie', ''))}")
                return self.send_json({"ok": True, **r})
            if self.path == "/api/search":
                q = str(payload.get("query", ""))[:300]
                if not q.strip():
                    return self.send_json({"ok": False, "error": "empty query"}, 400)
                r = yt_search(q)
                return self.send_json({"ok": True, **r})
            if self.path == "/api/search-batch":
                items = payload.get("tracks") or payload.get("queries", [])
                if not isinstance(items, list) or not items:
                    return self.send_json({"ok": False, "error": "tracks/queries must be a non-empty list"}, 400)
                if len(items) > 25:
                    return self.send_json({"ok": False, "error": "max 25 items per batch"}, 400)
                clean = []
                for it in items:
                    if isinstance(it, dict):
                        clean.append({"title": str(it.get("title", ""))[:200],
                                      "artist": str(it.get("artist", ""))[:200],
                                      "duration_ms": int(it.get("duration_ms", 0) or 0)})
                    else:
                        clean.append(str(it)[:300])
                results = yt_search_batch(clean, payload.get("workers", 8))
                hit = sum(1 for x in results if x.get("videoId"))
                skip = sum(1 for x in results if not x.get("videoId"))
                blog("batch", f"n={len(clean)} matched={hit} skipped={skip} workers={payload.get('workers', 8)}")
                return self.send_json({"ok": True, "results": results})
            if self.path == "/api/create-playlist":
                r = yt_create_playlist(payload.get("yt_cookie", ""), payload.get("title", "Spotify import"),
                                       payload.get("description", ""), payload.get("videoIds", []),
                                       payload.get("privacy", "PRIVATE"))
                blog("create", f"ok playlist={r.get('playlistId')} added={r.get('added')}")
                return self.send_json({"ok": True, **r})
            if self.path == "/api/check":
                sp = check_spotify_cookie(payload.get("spotify_cookie", ""))
                yt = check_yt_cookie(payload.get("yt_cookie", ""))
                blog("check", f"spotify={sp['ok']} ytm={yt['ok']}")
                return self.send_json({"ok": True, "spotify": sp, "ytm": yt})
        except ValueError as e:
            blog("fail", f"{self.path} ValueError: {e}")
            return self.send_json({"ok": False, "error": str(e), **classify(str(e))}, 400)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            blog("fail", f"{self.path} {msg}")
            return self.send_json({"ok": False, "error": msg, **classify(msg)}, 500)
        return self.send_json({"error": "unknown route"}, 404)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    print(f"sp2yt listening on http://127.0.0.1:{port}", flush=True)
    if getattr(sys, "frozen", False):
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
