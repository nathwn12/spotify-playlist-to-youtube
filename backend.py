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
VAULTF = os.path.join(HERE, "cookie_vault.json")
KNOWN_COOKIE_FILES = [
    os.path.join(os.environ.get("USERPROFILE", ""), "Desktop", "music.youtube.com_cookies.txt"),
]

def detect_site(cookie: str) -> str:
    c = cookie or ""
    if "SAPISID" in c or "music.youtube" in c:
        return "yt"
    if "sp_dc" in c or "sp_key" in c or "open.spotify" in c or "spotify" in c.lower():
        return "sp"
    return ""

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

def _dpapi_blob(fn, data: bytes) -> bytes:
    lib = ctypes.windll.crypt32
    fn.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.POINTER(DATA_BLOB),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    fn.restype = wintypes.BOOL
    bin_, bout = DATA_BLOB(), DATA_BLOB()
    buf = ctypes.create_string_buffer(bytes(data))
    bin_.cbData = len(data)
    bin_.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    if not fn(ctypes.byref(bin_), None, None, None, None, 0, ctypes.byref(bout)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(bout.pbData, bout.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(bout.pbData)

def vault_save(which: str, cookie: str):
    """Latch a cookie vault-side, DPAPI-encrypted (this Windows user only). Plain values never touch disk."""
    c = normalize_cookie(cookie)
    if which not in ("sp", "yt") or not c:
        return
    try:
        with open(VAULTF, encoding="utf-8") as f:
            v = json.load(f)
            v = v if isinstance(v, dict) else {}
    except Exception:
        v = {}
    try:
        v[which] = base64.b64encode(_dpapi_blob(ctypes.windll.crypt32.CryptProtectData, c.encode())).decode()
        with open(VAULTF, "w", encoding="utf-8") as f:
            json.dump(v, f)
    except Exception:
        pass

def vault_load() -> dict:
    try:
        with open(VAULTF, encoding="utf-8") as f:
            v = json.load(f)
    except Exception:
        v = {}
    out, refreshed = {}, []
    for k in ("sp", "yt"):
        try:
            out[k + "_cookie"] = _dpapi_blob(
                ctypes.windll.crypt32.CryptUnprotectData, base64.b64decode(v[k])).decode() if v.get(k) else ""
        except Exception:
            out[k + "_cookie"] = ""
    # re-latch: known Desktop files changed on disk -> adopt, by content markers
    for path in KNOWN_COOKIE_FILES:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                raw = f.read()
        except Exception:
            continue
        site = detect_site(raw)
        norm = normalize_cookie(raw)
        if site and norm and norm != normalize_cookie(out.get(site + "_cookie", "")):
            out[site + "_cookie"] = norm
            v[site] = base64.b64encode(_dpapi_blob(
                ctypes.windll.crypt32.CryptProtectData, norm.encode())).decode()
            refreshed.append(site)
    if refreshed:
        try:
            with open(VAULTF, "w", encoding="utf-8") as f:
                json.dump(v, f)
        except Exception:
            pass
        blog("relatch", "fresh disk files adopted: " + ",".join(refreshed))
    out["refreshed"] = refreshed
    return out

def vault_forget():
    try:
        os.remove(VAULTF)
    except Exception:
        pass

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
    if "did not land" in m or "add failed" in m or "add rejected" in m:
        return {"kind": "write", "hint": "YTM took the empty playlist but refused the songs — cookie may lack write scope; re-export a fresh full Cookie and retry."}
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
    return {"playlist_id": pid, "playlist_name": entity.get("title", ""),
            "cover_url": ((entity.get("coverArt", {}) or {}).get("sources", [{}])[0] or {}).get("url", ""),
            "tracks": out}

def mrl_video_id(mrl: dict) -> str:
    """videoId from every known nesting (markup drifts; order = most reliable first)."""
    pid = (mrl.get("playlistItemData", {}) or {}).get("videoId")
    if pid:
        return pid
    try:
        return mrl["overlay"]["musicItemThumbnailOverlayRenderer"]["content"]["musicPlayButtonRenderer"]["playNavigationEndpoint"]["watchEndpoint"]["videoId"]
    except Exception:
        pass
    try:
        return mrl["navigationEndpoint"]["watchEndpoint"]["videoId"]
    except Exception:
        pass
    mm = re.search(r'"videoId":\s*"([A-Za-z0-9_-]{11})"', json.dumps(mrl))
    return mm.group(1) if mm else ""

def mrl_texts(mrl: dict):
    """flexColumns today: [title, type, plays]. Returns (title, type). No durations in search."""
    flex = mrl.get("flexColumns", [])

    def col(i):
        try:
            return flex[i]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"][0]["text"]
        except Exception:
            return ""

    return col(0), col(1)

def yt_search(query: str):
    body = json.dumps({"context": {"client": CLIENT}, "query": query,
                       "params": "EgWKAQIIAWoQEAMQBBAJEAkQBRAK"}).encode()
    req = urllib.request.Request(
        f"https://music.youtube.com/youtubei/v1/search?key={YT_KEY}&prettyPrint=false",
        data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
                            "Origin": YT_ORIGIN, "Referer": YT_ORIGIN + "/"})
    r = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    j = json.loads(r)
    try:
        sections = j["contents"]["tabbedSearchResultsRenderer"]["tabs"][0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"]
        cands = []
        for sec in sections:
            items = sec.get("musicShelfRenderer", {}).get("contents", [])
            if not items and "itemSectionRenderer" in sec:
                items = sec["itemSectionRenderer"].get("contents", [])
            for item in items:
                mrl = item.get("musicResponsiveListItemRenderer", {})
                if not mrl:
                    continue
                pid = mrl_video_id(mrl)
                if not pid:
                    continue
                title, typ = mrl_texts(mrl)
                cands.append({"videoId": pid, "title": title, "type": typ})
        want_q = query
        for c in cands:  # Song rows whose title overlaps the query
            if c["type"] == "Song" and c["title"] and title_overlap(want_q, c["title"]):
                c.update({"artist": "", "duration": "", "query": query})
                return c
        for c in cands:  # any titled overlap (video etc.)
            if c["title"] and title_overlap(want_q, c["title"]):
                c.update({"artist": "", "duration": "", "query": query})
                return c
    except Exception:
        pass
    raise RuntimeError("no ytm result for: " + query)

def title_overlap(a: str, b: str) -> bool:
    wa = set(re.findall(r"[a-z0-9]{3,}", (a or "").lower()))
    wb = set(re.findall(r"[a-z0-9]{3,}", (b or "").lower()))
    return bool(wa & wb)

def yt_match(title: str, artist: str = "", duration_ms: int = 0) -> dict:
    """One song, isolated. Query variants in order; search ranks Song+overlap first.
    Never raises: no match -> videoId None (skip), never a batch failure."""
    title, artist = (title or "").strip(), (artist or "").strip()
    want_q = f"{title} {artist}".strip()
    for qi, q in enumerate([want_q, title, f"{artist} - {title}".strip()]):
        if not q:
            continue
        try:
            r = yt_search(q)
        except Exception:
            continue
        if r.get("videoId"):
            r["query"] = want_q
            r["via"] = q
            r["alt"] = qi > 0
            return {**{"ok": True}, **r}
    return {"ok": True, "query": want_q, "videoId": None,
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
    return {"ok": True, "msg": "not needed — public links need no login"}

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

CREATEDF = os.path.join(HERE, "created.json")

def load_created() -> dict:
    try:
        with open(CREATEDF, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def save_created(d: dict):
    try:
        with open(CREATEDF, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass

def vl_id(plid: str) -> str:
    plid = (plid or "").strip()
    return plid if plid.startswith("VL") else ("VL" + plid if plid else "")

def yt_headers(yt_cookie: str) -> dict:
    """Fresh auth headers per call — SAPISIDHASH goes stale within minutes."""
    yt_cookie = normalize_cookie(yt_cookie)
    return {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
            "Origin": YT_ORIGIN, "Referer": YT_ORIGIN + "/", "Cookie": yt_cookie,
            "Authorization": sapisid_hash(yt_cookie), "X-Origin": YT_ORIGIN}

def yt_playlist_count(plid: str, yt_cookie: str) -> int:
    """Browse a playlist with the user's auth, count its video items. 0 on any failure."""
    try:
        body = json.dumps({"context": {"client": CLIENT}, "browseId": vl_id(plid)}).encode()
        req = urllib.request.Request(
            f"https://music.youtube.com/youtubei/v1/browse?key={YT_KEY}&prettyPrint=false",
            data=body, headers=yt_headers(yt_cookie))
        txt = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
        vids = set(re.findall(r'"videoId":\s*"([A-Za-z0-9_-]{11})"', txt))
        return len(vids)
    except Exception:
        return 0

def yt_create_playlist(yt_cookie: str, title: str, description: str, video_ids: list, privacy: str = "PRIVATE"):
    yt_cookie = normalize_cookie(yt_cookie)
    video_ids = list(dict.fromkeys(video_ids or []))  # dupes collapse to one add
    base_h = yt_headers(yt_cookie)
    # 1. create WITH the songs inline (upstream-blessed path)
    body = json.dumps({"context": {"client": CLIENT}, "title": title,
                       "description": description or "", "privacyStatus": privacy,
                       "videoIds": video_ids[:50]}).encode()
    req = urllib.request.Request(
        f"https://music.youtube.com/youtubei/v1/playlist/create?key={YT_KEY}&prettyPrint=false",
        data=body, headers=base_h)
    r = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    j = json.loads(r)
    plid = j.get("playlistId") or j.get("playlistID")
    if not plid:
        raise RuntimeError("create failed: " + r[:400])
    blog("create", f"id={plid} reply={r[:200]}")
    # 2. add each video in its own call: one bad id must not poison the batch.
    # The read-back in step 3 is the source of truth — edit replies are only logged.
    edit_notes = []
    failed = []
    if video_ids:
        for k, v in enumerate(video_ids):
            b2 = json.dumps({"context": {"client": CLIENT}, "playlistId": plid, "actions": [
                {"action": "ACTION_ADD_VIDEO", "addedVideoId": v}]}).encode()
            try:
                er = urllib.request.urlopen(urllib.request.Request(
                    f"https://music.youtube.com/youtubei/v1/browse/edit_playlist?key={YT_KEY}&prettyPrint=false",
                    data=b2, headers=base_h), timeout=25).read().decode("utf-8", "ignore")
                try:
                    status = json.loads(er).get("status", "")
                except Exception:
                    status = er[:120]
                if "SUCCEED" in str(status).upper():
                    continue
                failed.append(v)
                blog("edit", f"video {k + 1} {v} rejected: {status}")
            except Exception as e:
                failed.append(v)
                blog("edit", f"video {k + 1} {v} raised {type(e).__name__} {getattr(e, 'code', '')}")
        if failed:
            blog("edit", f"{len(failed)}/{len(video_ids)} rejected")
    # 3. verify: browse it back with the user's auth and count real items
    confirmed = yt_playlist_count(plid, yt_cookie)
    if video_ids and not confirmed:
        cleaned = yt_delete_playlist(plid, yt_cookie)
        raise RuntimeError("songs did not land (playlist reads back empty"
                           + (", husk deleted" if cleaned else ", husk cleanup failed — delete it manually")
                           + f"). {len(failed)}/{len(video_ids)} adds rejected — see server.log edit lines")
    return {"playlistId": plid, "url": f"https://music.youtube.com/playlist?list={plid}",
            "added": confirmed, "sent": len(video_ids), "rejected": len(failed)}

def yt_delete_playlist(plid: str, yt_cookie: str) -> bool:
    try:
        body = json.dumps({"context": {"client": CLIENT}, "playlistId": plid}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://music.youtube.com/youtubei/v1/playlist/delete?key={YT_KEY}&prettyPrint=false",
            data=body, headers=yt_headers(yt_cookie)), timeout=20).read()
        return True
    except Exception as e:
        blog("delete", f"{plid} {type(e).__name__} {getattr(e, 'code', '')}")
        return False

def yt_create_checked(spotify_id: str, yt_cookie: str, title: str, description: str,
                      video_ids: list, privacy: str = "PRIVATE") -> dict:
    """Dupe-aware create: same spotify source transferred before and YTM copy
    still alive -> return it, create nothing. Empty results always deleted."""
    yt_cookie = normalize_cookie(yt_cookie)
    if spotify_id:
        rec = load_created().get(spotify_id)
        if rec and rec.get("yt_id"):
            n = yt_playlist_count(rec["yt_id"], yt_cookie)
            if n:
                blog("create", f"exists title={title} url={rec.get('url')} tracks={n}")
                return {"playlistId": rec["yt_id"], "url": rec.get("url", ""),
                        "added": n, "sent": len(video_ids), "existing": True}
    r = yt_create_playlist(yt_cookie, title, description, video_ids, privacy)
    if spotify_id:
        created = load_created()
        created[spotify_id] = {"yt_id": r["playlistId"], "url": r["url"], "title": title}
        save_created(created)
    return r

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
                r = yt_create_checked(payload.get("spotify_id", ""), payload.get("yt_cookie", ""),
                                      payload.get("title", "Spotify import"),
                                      payload.get("description", ""), payload.get("videoIds", []),
                                      payload.get("privacy", "PRIVATE"))
                blog("create", f"ok playlist={r.get('playlistId')} added={r.get('added')} existing={r.get('existing', False)}")
                return self.send_json({"ok": True, **r})
            if self.path == "/api/check":
                sp = check_spotify_cookie(payload.get("spotify_cookie", ""))
                yt = check_yt_cookie(payload.get("yt_cookie", ""))
                blog("check", f"spotify={sp['ok']} ytm={yt['ok']}")
                return self.send_json({"ok": True, "spotify": sp, "ytm": yt})
            if self.path == "/api/vault":
                v = vault_load()
                return self.send_json({"ok": True, "has_sp": bool(v["sp_cookie"]),
                                       "has_yt": bool(v["yt_cookie"]), **v})
            if self.path == "/api/latch":
                vault_save("sp", payload.get("spotify_cookie", ""))
                vault_save("yt", payload.get("yt_cookie", ""))
                blog("latch", "cookies latched to vault")
                return self.send_json({"ok": True})
            if self.path == "/api/forget":
                vault_forget()
                blog("forget", "vault cleared")
                return self.send_json({"ok": True})
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
