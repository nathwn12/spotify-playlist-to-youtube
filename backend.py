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
STATE = os.path.join(os.environ.get("APPDATA", HERE), "sp2yt")
try:
    os.makedirs(STATE, exist_ok=True)
except Exception:
    STATE = HERE
LOGF = os.path.join(STATE, "server.log")
HISTORY = []
_HIST_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()
_RUN_COUNTER = [0]
RUN = {
    "id": "",
    "seq": 0,
    "phase": "idle",
    "started_at": 0.0,
    "updated_at": time.time(),
    "spotify_id": "",
    "title": "",
    "playlist_id": "",
    "url": "",
    "created": False,
    "existing": False,
    "total": 0,
    "matched": 0,
    "sent": 0,
    "landed": 0,
    "refused": 0,
    "skipped": 0,
    "missing": 0,
    "rows": [],
    "error": "",
    "hint": "",
    "kind": "",
}

def _run_touch_locked():
    RUN["seq"] += 1
    RUN["updated_at"] = time.time()

def run_set(**kw):
    with _RUN_LOCK:
        for k, v in kw.items():
            RUN[k] = v
        _run_touch_locked()

def run_row_set(idx, **kw):
    with _RUN_LOCK:
        try:
            row = RUN["rows"][idx]
        except Exception:
            return
        for k, v in kw.items():
            row[k] = v
        RUN["seq"] += 1
        row["row_seq"] = RUN["seq"]
        RUN["updated_at"] = time.time()

def run_is_live():
    with _RUN_LOCK:
        return RUN.get("phase") in ("spotify", "search", "create", "append", "verify")

def run_snapshot():
    with _RUN_LOCK:
        snap = {k: v for k, v in RUN.items() if k != "rows"}
        snap["rows"] = [dict(r) for r in RUN["rows"]]
        return snap

def run_begin(spotify_id="", title=""):
    with _RUN_LOCK:
        _RUN_COUNTER[0] += 1
        rid = "r%d" % _RUN_COUNTER[0]
        now = time.time()
        RUN["id"] = rid
        RUN["phase"] = "spotify"
        RUN["started_at"] = now
        RUN["updated_at"] = now
        RUN["spotify_id"] = spotify_id
        RUN["title"] = title
        RUN["playlist_id"] = ""
        RUN["url"] = ""
        RUN["created"] = False
        RUN["existing"] = False
        RUN["total"] = 0
        RUN["matched"] = 0
        RUN["sent"] = 0
        RUN["landed"] = 0
        RUN["refused"] = 0
        RUN["skipped"] = 0
        RUN["missing"] = 0
        RUN["rows"] = []
        RUN["error"] = ""
        RUN["hint"] = ""
        RUN["kind"] = ""
        RUN["seq"] += 1
        return rid
VAULTF = os.path.join(STATE, "cookie_vault.json")
WATCHF = os.path.join(STATE, "cookie_watch.json")

def home_dir() -> str:
    return os.path.expanduser("~")

def cookie_dirs() -> list:
    dirs = [os.path.join(home_dir(), "Desktop"), os.path.join(home_dir(), "Downloads")]
    try:
        with open(WATCHF, encoding="utf-8") as f:
            extra = (json.load(f) or {}).get("extra", "")
            if extra and os.path.isdir(extra) and extra not in dirs:
                dirs.append(extra)
    except Exception:
        pass
    return [d for d in dirs if os.path.isdir(d)]

def scan_disk_cookies() -> dict:
    """Newest cookie-like .txt per site across watched dirs. Reads metadata+markers only."""
    best = {}
    for d in cookie_dirs():
        try:
            names = [n for n in os.listdir(d) if n.lower().endswith(".txt")]
        except Exception:
            continue
        for n in names[:100]:
            p = os.path.join(d, n)
            try:
                if os.path.getsize(p) > 1024 * 1024:
                    continue
                with open(p, encoding="utf-8", errors="ignore") as f:
                    raw = f.read(200000)
            except Exception:
                continue
            site = detect_site(raw)
            if not site:
                continue
            mt = os.path.getmtime(p)
            if site not in best or mt > best[site][0]:
                best[site] = (mt, p, raw)
    return best

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
    # re-latch: watched dirs may hold a regenerated file -> adopt by content markers
    for site, (_mt, path, raw) in scan_disk_cookies().items():
        norm = normalize_cookie(raw)
        if site and norm and norm != normalize_cookie(out.get(site + "_cookie", "")):
            out[site + "_cookie"] = norm
            v[site] = base64.b64encode(_dpapi_blob(
                ctypes.windll.crypt32.CryptProtectData, norm.encode())).decode()
            refreshed.append(site + ":" + os.path.basename(path))
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
    with _HIST_LOCK:
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
    if "nothing landed" in m:
        return {"kind": "write", "hint": "YTM created the empty playlist but no songs could be added — the cookie has create-but-cannot-add scope; re-export a fresh full Cookie from music.youtube.com and retry."}
    if "did not land" in m or "add failed" in m or "add rejected" in m:
        return {"kind": "write", "hint": "YTM took the empty playlist but the songs could not be added — cookie has create-but-cannot-add scope; re-export a fresh full Cookie and retry."}
    if "returned no id" in m or "did not accept the create" in m:
        return {"kind": "write", "hint": "YTM did not accept the playlist create; retry once, then re-export the cookie if it repeats."}
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
    last_err = ""
    for qi, q in enumerate([want_q, title, f"{artist} - {title}".strip()]):
        if not q:
            continue
        try:
            r = yt_search(q)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        if r.get("videoId"):
            r["query"] = want_q
            r["via"] = q
            r["alt"] = qi > 0
            return {**{"ok": True}, **r}
    skip = "no match on any variant" + (f" ({last_err})" if last_err else "")
    return {"ok": True, "query": want_q, "videoId": None,
            "title": "", "duration": "", "skipped": skip}

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
        t = type(e).__name__
        ml = (t + " " + str(e or "")).lower()
        if ("urlerror" in ml or "timeout" in ml or "timed out" in ml or "connection" in ml
                or "dns" in ml or "gaierror" in ml or "unreachable" in ml or "temporary failure" in ml):
            return {"ok": False, "msg": "could not reach music.youtube.com — the cookie is not the problem"}
        return {"ok": False, "msg": f"rejected ({t} {getattr(e, 'code', '')}) — export a fresh cookie file and re-upload"}

def sapisid_hash(cookie: str, origin: str = YT_ORIGIN) -> str:
    cookie = normalize_cookie(cookie)
    m = re.search(r"(?:__Secure-3PAPISID|SAPISID)=([^;]+)", cookie)
    if not m:
        raise ValueError("yt cookie missing SAPISID (paste full Cookie from music.youtube.com DevTools)")
    sid = m.group(1)
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"

CREATEDF = os.path.join(STATE, "created.json")
SETTINGF = os.path.join(STATE, "settings.json")
_SETTING_KEYS = ("privacy",)

def load_settings() -> dict:
    try:
        with open(SETTINGF, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def save_settings(d: dict):
    try:
        with open(SETTINGF, "w", encoding="utf-8") as f:
            json.dump({k: d[k] for k in _SETTING_KEYS if k in d}, f)
    except Exception:
        pass

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

CREATE_INLINE_MAX = 50  # playlist/create carries at most this many videoIds inline

def _shelf(node):
    """musicPlaylistShelfRenderer anywhere in a browse reply (shape drifts)."""
    if isinstance(node, dict):
        if "musicPlaylistShelfRenderer" in node:
            return node["musicPlaylistShelfRenderer"]
        for v in node.values():
            r = _shelf(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _shelf(v)
            if r:
                return r
    return None

def yt_playlist_entries(plid: str, yt_cookie: str):
    """Every entry on the playlist, in order, repeats included. None on any failure.
    Reads playlist rows only — not recommendations or headers — so this is the real
    playlist length, unlike a bare videoId regex over the whole reply."""
    try:
        body = json.dumps({"context": {"client": CLIENT}, "browseId": vl_id(plid)}).encode()
        req = urllib.request.Request(
            f"https://music.youtube.com/youtubei/v1/browse?key={YT_KEY}&prettyPrint=false",
            data=body, headers=yt_headers(yt_cookie))
        j = json.loads(urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore"))
        out = []
        for item in (_shelf(j) or {}).get("contents", []) or []:
            mrl = item.get("musicResponsiveListItemRenderer", {}) if isinstance(item, dict) else {}
            v = mrl_video_id(mrl) if mrl else ""
            if v:
                out.append(v)
        return out
    except Exception:
        return None

def yt_playlist_count(plid: str, yt_cookie: str) -> int:
    """Entries on the playlist. 0 on any failure."""
    entries = yt_playlist_entries(plid, yt_cookie)
    return len(entries) if entries is not None else 0

def yt_add_one(plid: str, vid: str, yt_cookie: str) -> str:
    """Append one entry. -> YTM's status string ("STATUS_SUCCEEDED"/"STATUS_FAILED"),
    or the exception text. Fresh SAPISIDHASH per call: it goes stale within minutes."""
    b2 = json.dumps({"context": {"client": CLIENT}, "playlistId": plid, "actions": [
        {"action": "ACTION_ADD_VIDEO", "addedVideoId": vid}]}).encode()
    try:
        er = urllib.request.urlopen(urllib.request.Request(
            f"https://music.youtube.com/youtubei/v1/browse/edit_playlist?key={YT_KEY}&prettyPrint=false",
            data=b2, headers=yt_headers(yt_cookie)), timeout=25).read().decode("utf-8", "ignore")
    except Exception as e:
        return f"{type(e).__name__} {getattr(e, 'code', '')}".strip()
    try:
        return str(json.loads(er).get("status", ""))
    except Exception:
        return er[:120]

def _ledger(video_ids: list, entries, attempts: dict) -> list:
    """One outcome per requested copy, in send order. A copy counts as landed while the
    playlist still holds an unclaimed entry for that videoId — 4 copies need 4 entries.
    Honest outcomes: only an explicit YTM STATUS_FAILED is 'rejected' (YTM said so);
    STATUS_SUCCEEDED-but-absent and transport errors are 'missing' (asked, not on the
    playlist), with the raw status kept verbatim — no line claims a refusal that did
    not happen."""
    left = {}
    for v in entries or []:
        left[v] = left.get(v, 0) + 1
    out = []
    for i, v in enumerate(video_ids):
        if left.get(v, 0) > 0:
            left[v] -= 1
            out.append({"i": i + 1, "videoId": v, "outcome": "landed", "status": ""})
        else:
            st = (attempts.get(i, "") or "")
            su = st.upper()
            if "FAILED" in su:
                out.append({"i": i + 1, "videoId": v,
                            "outcome": "rejected", "status": st[:80]})
            elif st:
                out.append({"i": i + 1, "videoId": v,
                            "outcome": "missing", "status": st[:80]})
            else:
                out.append({"i": i + 1, "videoId": v,
                            "outcome": "missing", "status": ""})
    return out

def _persist_incomplete(spotify_id, plid, url, title, expected):
    if not spotify_id:
        return
    try:
        created = load_created()
        created[spotify_id] = {"yt_id": plid, "url": url, "title": title,
                               "expected": expected, "complete": False}
        save_created(created)
    except Exception:
        pass

def _persist_final(spotify_id, plid, url, title, expected, complete):
    if not spotify_id:
        return
    try:
        created = load_created()
        created[spotify_id] = {"yt_id": plid, "url": url, "title": title,
                               "expected": expected, "complete": bool(complete)}
        save_created(created)
    except Exception:
        pass

def _read_back_retry(plid, yt_cookie):
    first = yt_playlist_entries(plid, yt_cookie)
    if first is None:
        first = yt_playlist_entries(plid, yt_cookie)
    return first

def _append_todo(plid, video_ids, todo, yt_cookie, hooks=None):
    attempts = {}
    on_start = (hooks or {}).get("on_append_start")
    on_one = (hooks or {}).get("on_append_one")
    for idx in todo:
        if on_start:
            try:
                on_start(idx)
            except Exception:
                pass
        st = yt_add_one(plid, video_ids[idx], yt_cookie)
        attempts[idx] = st
        blog("add", f"copy {idx + 1}/{len(video_ids)} {video_ids[idx]} -> {st}")
        if on_one:
            try:
                on_one(idx, st)
            except Exception:
                pass
    return attempts

def _ledger_counts(ledger):
    added = sum(1 for e in ledger if e["outcome"] == "landed")
    rejected = sum(1 for e in ledger if e["outcome"] == "rejected")
    missing = sum(1 for e in ledger if e["outcome"] == "missing")
    return added, rejected, missing

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

def _create_new(yt_cookie, title, description, video_ids, privacy, spotify_id, hooks=None):
    on_created = (hooks or {}).get("on_created")
    on_phase = (hooks or {}).get("on_phase")
    if on_phase:
        try:
            on_phase("create")
        except Exception:
            pass
    inline = video_ids[:CREATE_INLINE_MAX]
    body = json.dumps({"context": {"client": CLIENT}, "title": title,
                       "description": description or "", "privacyStatus": privacy,
                       "videoIds": inline}).encode()
    req = urllib.request.Request(
        f"https://music.youtube.com/youtubei/v1/playlist/create?key={YT_KEY}&prettyPrint=false",
        data=body, headers=yt_headers(yt_cookie))
    r = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    j = json.loads(r)
    plid = j.get("playlistId") or j.get("playlistID")
    if not plid:
        raise RuntimeError("playlist create returned no id — YTM did not accept the create; retry once, then re-export the cookie if it repeats")
    url = f"https://music.youtube.com/playlist?list={plid}"
    _persist_incomplete(spotify_id, plid, url, title, len(video_ids))
    if on_created:
        try:
            on_created(plid, url, False)
        except Exception:
            pass
    first = _read_back_retry(plid, yt_cookie)
    blog("create", f"id={plid} inline={len(inline)} back={-1 if first is None else len(first)}")
    if first is None:
        blog("verify", "read-back after create returned nothing — no appends attempted")
        raise RuntimeError(f"playlist created at {url} — inline copy landed but the rest could not be verified (read-back failed)")
    if on_phase:
        try:
            on_phase("append")
        except Exception:
            pass
    seeded = sum(1 for e in _ledger(video_ids, first, {}) if e["outcome"] == "landed")
    if hooks and hooks.get("on_seed"):
        try:
            hooks["on_seed"](seeded)
        except Exception:
            pass
    todo = [int(e["i"]) - 1 for e in _ledger(video_ids, first, {}) if e["outcome"] != "landed"]
    attempts = _append_todo(plid, video_ids, todo, yt_cookie, hooks)
    if on_phase:
        try:
            on_phase("verify")
        except Exception:
            pass
    entries = _read_back_retry(plid, yt_cookie)
    if entries is None:
        raise RuntimeError(f"playlist created at {url} — inline copy landed but the rest could not be verified (final read-back failed)")
    ledger = _ledger(video_ids, entries, attempts)
    added, rejected, missing = _ledger_counts(ledger)
    repeats = len(video_ids) - len(set(video_ids))
    blog("parity", f"{added}/{len(video_ids)} landed, {rejected} refused, {missing} missing, "
                   f"{repeats} repeat(s) in source, {len(todo)} append attempt(s)")
    _persist_final(spotify_id, plid, url, title, len(video_ids), added == len(video_ids))
    if not added:
        if isinstance(entries, list) and len(entries) == 0:
            cleaned = yt_delete_playlist(plid, yt_cookie)
            raise RuntimeError("nothing landed (playlist reads back empty"
                               + (", husk deleted" if cleaned else ", husk cleanup failed — delete it manually")
                               + f"). {rejected} refused by YTM, {missing} missing — see server.log add lines")
        raise RuntimeError(f"playlist created at {url} — inline copy landed but the rest could not be verified "
                           f"({rejected} refused by YTM, {missing} missing — see server.log add lines)")
    return {"playlistId": plid, "url": url,
            "added": added, "sent": len(video_ids), "rejected": rejected,
            "missing": missing, "repeats": repeats, "ledger": ledger}

def _resume_incomplete(rec, yt_cookie, title, video_ids, spotify_id, hooks=None):
    on_created = (hooks or {}).get("on_created")
    on_phase = (hooks or {}).get("on_phase")
    plid = rec.get("yt_id", "")
    url = rec.get("url", f"https://music.youtube.com/playlist?list={plid}")
    if on_created:
        try:
            on_created(plid, url, False)
        except Exception:
            pass
    if on_phase:
        try:
            on_phase("append")
        except Exception:
            pass
    entries = _read_back_retry(plid, yt_cookie)
    if entries is None:
        raise RuntimeError(f"playlist created at {url} — inline copy landed but the rest could not be verified (read-back failed)")
    seeded = sum(1 for e in _ledger(video_ids, entries, {}) if e["outcome"] == "landed")
    if hooks and hooks.get("on_seed"):
        try:
            hooks["on_seed"](seeded)
        except Exception:
            pass
    todo = [int(e["i"]) - 1 for e in _ledger(video_ids, entries, {}) if e["outcome"] != "landed"]
    blog("resume", f"id={plid} have={len(entries)} todo={len(todo)}")
    attempts = _append_todo(plid, video_ids, todo, yt_cookie, hooks)
    if on_phase:
        try:
            on_phase("verify")
        except Exception:
            pass
    entries2 = _read_back_retry(plid, yt_cookie)
    if entries2 is None:
        raise RuntimeError(f"playlist created at {url} — inline copy landed but the rest could not be verified (final read-back failed)")
    ledger = _ledger(video_ids, entries2, attempts)
    added, rejected, missing = _ledger_counts(ledger)
    repeats = len(video_ids) - len(set(video_ids))
    blog("parity", f"resume {added}/{len(video_ids)} landed, {rejected} refused, {missing} missing")
    _persist_final(spotify_id, plid, url, title, len(video_ids), added == len(video_ids))
    if not added:
        if isinstance(entries2, list) and len(entries2) == 0:
            cleaned = yt_delete_playlist(plid, yt_cookie)
            raise RuntimeError("nothing landed (playlist reads back empty"
                               + (", husk deleted" if cleaned else ", husk cleanup failed — delete it manually")
                               + f"). {rejected} refused by YTM, {missing} missing — see server.log add lines")
        raise RuntimeError(f"playlist created at {url} — inline copy landed but the rest could not be verified "
                           f"({rejected} refused by YTM, {missing} missing — see server.log add lines)")
    return {"playlistId": plid, "url": url,
            "added": added, "sent": len(video_ids), "rejected": rejected,
            "missing": missing, "repeats": repeats, "ledger": ledger, "resumed": True}

def transfer_create_append(yt_cookie, title, description, video_ids, privacy="PRIVATE",
                           spotify_id="", hooks=None):
    """One implementation, two entry points: sync /api/create-playlist and the
    async /api/transfer worker. Never dedupes video_ids."""
    yt_cookie = normalize_cookie(yt_cookie)
    ids = [v for v in (video_ids or []) if isinstance(v, str) and v]
    if not ids:
        raise ValueError("no videoIds to send")
    if spotify_id:
        rec = load_created().get(spotify_id)
        if rec and rec.get("yt_id"):
            if rec.get("complete") is True:
                entries = yt_playlist_entries(rec["yt_id"], yt_cookie)
                if entries is not None and len(entries) > 0:
                    ledger = _ledger(ids, entries, {})
                    added = sum(1 for e in ledger if e["outcome"] == "landed")
                    missing = sum(1 for e in ledger if e["outcome"] != "landed")
                    for e in ledger:
                        if e["outcome"] != "landed":
                            e["status"] = "exists — not on playlist"
                    if added < len(ids):
                        blog("create", f"exists title={title} url={rec.get('url')} {added}/{len(ids)} from this playlist on it — source grew, leaving playlist alone")
                    else:
                        blog("create", f"exists title={title} url={rec.get('url')} tracks={added}")
                    return {"playlistId": rec["yt_id"], "url": rec.get("url", ""),
                            "added": added, "sent": len(ids), "rejected": 0, "missing": missing,
                            "repeats": len(ids) - len(set(ids)), "existing": True, "ledger": ledger}
                if entries is None:
                    blog("create", f"exists-unreadable title={title} url={rec.get('url')} — returning record without new create")
                    return {"playlistId": rec["yt_id"], "url": rec.get("url", ""),
                            "added": rec.get("expected", len(ids)), "sent": len(ids),
                            "rejected": 0, "missing": 0,
                            "repeats": len(ids) - len(set(ids)), "existing": True, "ledger": []}
            else:
                return _resume_incomplete(rec, yt_cookie, title, ids, spotify_id, hooks)
    r = _create_new(yt_cookie, title, description, ids, privacy, spotify_id, hooks)
    return r

def yt_create_playlist(yt_cookie: str, title: str, description: str, video_ids: list, privacy: str = "PRIVATE"):
    """1:1 with the Spotify playlist: 48 tracks in => 48 entries out, repeats included.
    Nothing here drops, dedupes or membership-checks an id: every copy of the input is
    one copy we ask YTM to hold, and whatever YTM refuses is reported per copy."""
    return transfer_create_append(yt_cookie, title, description, video_ids, privacy, "", None)

def yt_create_checked(spotify_id: str, yt_cookie: str, title: str, description: str,
                      video_ids: list, privacy: str = "PRIVATE") -> dict:
    """Dupe-aware create: complete record + live YTM copy -> return it, create nothing.
    Incomplete record -> resume the same playlist, never a second playlist."""
    return transfer_create_append(yt_cookie, title, description, video_ids, privacy, spotify_id or "", None)

def _transfer_thread(url, yt_cookie, title, privacy, run_id):
    try:
        with _RUN_LOCK:
            if RUN.get("id") != run_id:
                return
            RUN["phase"] = "spotify"
            RUN["seq"] += 1
            RUN["updated_at"] = time.time()
        if not normalize_cookie(yt_cookie):
            raise ValueError("yt cookie missing (paste full Cookie from music.youtube.com)")
        sp = fetch_spotify_tracks(url, "")
        pname = (title or "").strip() or sp.get("playlist_name", "") or "Spotify import"
        tracks = sp.get("tracks", []) or []
        sid = sp.get("playlist_id", "")
        with _RUN_LOCK:
            if RUN.get("id") != run_id:
                return
            RUN["spotify_id"] = sid
            RUN["title"] = pname
            RUN["total"] = len(tracks)
            RUN["sent"] = 0
            RUN["matched"] = 0
            RUN["skipped"] = 0
            RUN["landed"] = 0
            RUN["refused"] = 0
            RUN["missing"] = 0
            RUN["rows"] = [{"i": k + 1, "title": t.get("title", ""), "artist": t.get("artist", ""),
                             "videoId": "", "state": "queued", "status": "", "row_seq": 0}
                            for k, t in enumerate(tracks)]
            for r in RUN["rows"]:
                RUN["seq"] += 1
                r["row_seq"] = RUN["seq"]
            RUN["phase"] = "search" if tracks else "failed"
            RUN["updated_at"] = time.time()
            if not tracks:
                RUN["error"] = "empty playlist (no tracks in spotify embed)"
                RUN["kind"] = "parse"
                RUN["hint"] = "Upstream page format changed — run stopped for inspection."
        if not tracks:
            blog("fail", "transfer empty playlist")
            return
        blog("spotify", f"run={run_id} ok name={pname} n={len(tracks)}")
        matched_ids = ["" for _ in tracks]
        timeout_batches = 0
        num_batches = 0
        for base in range(0, len(tracks), 10):
            with _RUN_LOCK:
                if RUN.get("id") != run_id or RUN.get("phase") == "failed":
                    return
            chunk = tracks[base:base + 10]
            num_batches += 1
            batch_timeout = False
            try:
                results = yt_search_batch(chunk, 8)
            except Exception as e:
                results = [{"ok": True, "query": "", "videoId": None, "skipped": str(e)} for _ in chunk]
                batch_timeout = True
            if not batch_timeout and results and all(not (r or {}).get("videoId") for r in results):
                for r in results:
                    ml = str((r or {}).get("skipped") or (r or {}).get("error") or "").lower()
                    if ("timeout" in ml or "timed out" in ml or "urlerror" in ml
                            or "connection" in ml or "temporary failure" in ml or "gaierror" in ml):
                        batch_timeout = True
                        break
            if batch_timeout:
                timeout_batches += 1
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                for k, res in enumerate(results):
                    idx = base + k
                    if idx >= len(RUN["rows"]):
                        break
                    vid = (res or {}).get("videoId") or ""
                    row = RUN["rows"][idx]
                    row["videoId"] = vid
                    if vid:
                        row["state"] = "matched"
                        row["status"] = ""
                    else:
                        row["state"] = "skipped"
                        row["status"] = str((res or {}).get("skipped") or (res or {}).get("error") or "no match")[:80]
                    RUN["seq"] += 1
                    row["row_seq"] = RUN["seq"]
                    matched_ids[idx] = vid
                RUN["matched"] = sum(1 for v in matched_ids if v)
                RUN["skipped"] = sum(1 for v in matched_ids if not v)
                RUN["sent"] = RUN["matched"]
                RUN["updated_at"] = time.time()
        with _RUN_LOCK:
            if RUN.get("id") != run_id:
                return
            vids = [RUN["rows"][k]["videoId"] for k in range(len(RUN["rows"])) if RUN["rows"][k]["videoId"]]
            RUN["sent"] = len(vids)
        if not vids:
            with _RUN_LOCK:
                RUN["phase"] = "failed"
                if timeout_batches:
                    RUN["error"] = f"no tracks matched — {timeout_batches} of {num_batches} search batches timed out; check the connection"
                    RUN["kind"] = "transient"
                    RUN["hint"] = "Search requests timed out — check the connection and retry."
                else:
                    RUN["error"] = "zero matches, nothing to create."
                    RUN["kind"] = "parse"
                    RUN["hint"] = "No YTM match for any track — nothing to create."
                RUN["seq"] += 1
                RUN["updated_at"] = time.time()
            return
        def _on_created(plid, purl, _existing=False):
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                RUN["playlist_id"] = plid
                RUN["url"] = purl
                RUN["created"] = True
                RUN["phase"] = "append"
                RUN["seq"] += 1
                RUN["updated_at"] = time.time()
        def _on_start(idx):
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                try:
                    row = RUN["rows"][idx]
                except Exception:
                    return
                row["state"] = "appending"
                RUN["seq"] += 1
                row["row_seq"] = RUN["seq"]
                RUN["updated_at"] = time.time()
        def _on_one(idx, st):
            ok = "SUCCEEDED" in (st or "")
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                try:
                    row = RUN["rows"][idx]
                except Exception:
                    return
                row["status"] = (st or "")[:80]
                if ok:
                    row["state"] = "landed"
                    RUN["landed"] = int(RUN.get("landed", 0)) + 1
                else:
                    row["state"] = "appending"
                RUN["seq"] += 1
                row["row_seq"] = RUN["seq"]
                RUN["updated_at"] = time.time()
        def _on_seed(n):
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                RUN["landed"] = int(n or 0)
                RUN["seq"] += 1
                RUN["updated_at"] = time.time()
        def _on_phase(ph):
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                RUN["phase"] = ph
                RUN["seq"] += 1
                RUN["updated_at"] = time.time()
        hooks = {"on_created": _on_created, "on_append_start": _on_start,
                 "on_append_one": _on_one, "on_phase": _on_phase, "on_seed": _on_seed}
        try:
            res = transfer_create_append(yt_cookie, pname, "", vids, privacy, sid, hooks)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            c = classify(msg)
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                RUN["phase"] = "failed"
                RUN["error"] = msg[:300]
                RUN["hint"] = c.get("hint", "")
                RUN["kind"] = c.get("kind", "unknown")
                RUN["seq"] += 1
                RUN["updated_at"] = time.time()
            blog("fail", f"run={run_id} {msg}")
            return
        with _RUN_LOCK:
            if RUN.get("id") != run_id:
                return
            ledger = res.get("ledger", []) or []
            sent_rows = [k for k, row in enumerate(RUN["rows"]) if row.get("videoId")]
            for pos, k in enumerate(sent_rows):
                e = ledger[pos] if pos < len(ledger) else None
                if not e:
                    continue
                out = e.get("outcome")
                if out == "landed":
                    RUN["rows"][k]["state"] = "landed"
                    RUN["rows"][k]["status"] = ""
                elif out == "rejected":
                    RUN["rows"][k]["state"] = "refused"
                    RUN["rows"][k]["status"] = str(e.get("status", ""))[:80]
                else:
                    RUN["rows"][k]["state"] = "missing"
                    RUN["rows"][k]["status"] = str(e.get("status", ""))[:80]
                RUN["seq"] += 1
                RUN["rows"][k]["row_seq"] = RUN["seq"]
            RUN["playlist_id"] = res.get("playlistId", RUN.get("playlist_id", ""))
            RUN["url"] = res.get("url", RUN.get("url", ""))
            RUN["created"] = True
            RUN["existing"] = bool(res.get("existing", False))
            RUN["landed"] = int(res.get("added", 0))
            RUN["refused"] = int(res.get("rejected", 0))
            RUN["missing"] = int(res.get("missing", 0))
            RUN["phase"] = "done"
            RUN["error"] = ""
            RUN["seq"] += 1
            RUN["updated_at"] = time.time()
        blog("create", f"run={run_id} ok playlist={res.get('playlistId')} confirmed={res.get('added')}/{res.get('sent')} "
                       f"refused={res.get('rejected')} missing={res.get('missing')} existing={res.get('existing', False)}")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        c = classify(msg)
        with _RUN_LOCK:
            try:
                if RUN.get("id") == run_id:
                    RUN["phase"] = "failed"
                    RUN["error"] = msg[:300]
                    RUN["hint"] = c.get("hint", "")
                    RUN["kind"] = c.get("kind", "unknown")
                    RUN["seq"] += 1
                    RUN["updated_at"] = time.time()
            except Exception:
                pass
        blog("fail", f"run={run_id} {msg}")

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
            with _HIST_LOCK:
                recent = list(HISTORY[-20:])
            self.send_json({"ok": True, "recent": recent})
        elif self.path == "/api/settings":
            cur = load_settings()
            self.send_json({"ok": True, **{k: cur.get(k, "") for k in _SETTING_KEYS}})
        elif self.path == "/api/run" or self.path.startswith("/api/run?"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                since = int((qs.get("since", ["0"]) or ["0"])[0] or 0)
            except Exception:
                since = 0
            snap = run_snapshot()
            now = time.time()
            rows = snap.get("rows", []) or []
            if since:
                rows = [r for r in rows if int(r.get("row_seq", 0) or 0) > since]
            self.send_json({"ok": True, "run_id": snap.get("id", ""), "seq": snap.get("seq", 0),
                            "phase": snap.get("phase", "idle"),
                            "started_at": snap.get("started_at", 0),
                            "updated_at": snap.get("updated_at", 0),
                            "age": max(0.0, now - float(snap.get("updated_at", now) or now)),
                            "spotify_id": snap.get("spotify_id", ""),
                            "title": snap.get("title", ""),
                            "playlist_id": snap.get("playlist_id", ""),
                            "url": snap.get("url", ""),
                            "created": bool(snap.get("created", False)),
                            "existing": bool(snap.get("existing", False)),
                            "total": int(snap.get("total", 0) or 0),
                            "matched": int(snap.get("matched", 0) or 0),
                            "sent": int(snap.get("sent", 0) or 0),
                            "landed": int(snap.get("landed", 0) or 0),
                            "refused": int(snap.get("refused", 0) or 0),
                            "skipped": int(snap.get("skipped", 0) or 0),
                            "missing": int(snap.get("missing", 0) or 0),
                            "rows": rows,
                            "error": snap.get("error", ""),
                            "hint": snap.get("hint", ""),
                            "kind": snap.get("kind", "")})
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
                blog("create", f"ok playlist={r.get('playlistId')} confirmed={r.get('added')}/{r.get('sent')} "
                               f"refused={r.get('rejected')} missing={r.get('missing')} "
                               f"repeats={r.get('repeats')} existing={r.get('existing', False)}")
                return self.send_json({"ok": True, **r})
            if self.path == "/api/transfer":
                if run_is_live():
                    snap = run_snapshot()
                    return self.send_json({"ok": False, "error": "a transfer is already running",
                                           "run_id": snap.get("id", "")}, 409)
                url = str(payload.get("url", "") or "")
                if not url.strip():
                    return self.send_json({"ok": False, "error": "missing url"}, 400)
                title = str(payload.get("title", "") or "")
                privacy = str(payload.get("privacy", "PRIVATE") or "PRIVATE")
                rid = run_begin("", title)
                blog("transfer", f"run={rid} started")
                threading.Thread(target=_transfer_thread,
                                 args=(url, payload.get("yt_cookie", ""), title, privacy, rid),
                                 daemon=True).start()
                return self.send_json({"ok": True, "run_id": rid})
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
                try:
                    os.remove(WATCHF)
                except Exception:
                    pass
                blog("forget", "vault + watch path cleared")
                return self.send_json({"ok": True})
            if self.path == "/api/settings":
                cur = load_settings()
                for k in _SETTING_KEYS:
                    if k not in payload:
                        continue
                    if k == "privacy":
                        v = str(payload.get(k) or "").upper()
                        if v not in ("PRIVATE", "PUBLIC", "UNLISTED"):
                            continue
                        cur[k] = v
                    else:
                        cur[k] = payload.get(k)
                save_settings(cur)
                blog("settings", "saved")
                return self.send_json({"ok": True, **{k: cur.get(k, "") for k in _SETTING_KEYS}})
            if self.path == "/api/watch":
                if not payload.get("extra"):
                    return self.send_json({"ok": True, "dirs": cookie_dirs()})
                extra = str(payload.get("extra", "")).strip().strip('"')
                if not os.path.isdir(extra):
                    return self.send_json({"ok": False, "error": "not a folder: " + extra[:120]}, 400)
                try:
                    with open(WATCHF, "w", encoding="utf-8") as f:
                        json.dump({"extra": extra}, f)
                except Exception as e:
                    return self.send_json({"ok": False, "error": str(e)}, 500)
                blog("watch", f"extra={extra[:120]}")
                return self.send_json({"ok": True, "dirs": cookie_dirs()})
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
