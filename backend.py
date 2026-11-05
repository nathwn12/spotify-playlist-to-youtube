#!/usr/bin/env python3
"""sp2yt keyless web service - stdlib only. No API keys, no secrets stored.
Spotify: scrapes public embed __NEXT_DATA__ (optional user Cookie forwarded).
YTM: InnerTube WEB_REMIX + user-supplied Cookie (SAPISIDHASH derived server-side).
"""
import json, re, sys, time, base64, hashlib, hmac, struct, shutil, sqlite3, subprocess, tempfile, threading, unicodedata, urllib.request, urllib.parse, webbrowser
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
    "declared": 0,
    "summary": "",
    "playlist_count": 0,
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
        RUN["declared"] = 0
        RUN["summary"] = ""
        RUN["playlist_count"] = 0
        RUN["rows"] = []
        RUN["error"] = ""
        RUN["hint"] = ""
        RUN["kind"] = ""
        RUN["seq"] += 1
        return rid

def run_reset() -> bool:
    """Clear a finished run so a reload stays idle. Refuses while live.
    Never touches settings, the cookie vault, or any YouTube playlist —
    those are not local transfer state. created.json IS local transfer
    state (it feeds the reopen restore), so its removal lives next to
    this in the /api/reset handler, not in here (unit tests seed RUN
    without touching disk)."""
    with _RUN_LOCK:
        if RUN.get("phase") in ("spotify", "search", "create", "append", "verify"):
            return False
        RUN["id"] = ""
        RUN["phase"] = "idle"
        RUN["started_at"] = 0.0
        RUN["updated_at"] = time.time()
        RUN["spotify_id"] = ""
        RUN["title"] = ""
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
        RUN["declared"] = 0
        RUN["summary"] = ""
        RUN["playlist_count"] = 0
        RUN["rows"] = []
        RUN["error"] = ""
        RUN["hint"] = ""
        RUN["kind"] = ""
        RUN["seq"] += 1
        return True
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
    if "partial playlist" in m or "full track list" in m or "declared total" in m:
        return {"kind": "source", "hint": "Spotify's full list did not arrive — retry once; if it repeats, the upstream route changed and the run stopped before touching YouTube."}
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

_SP_TOK = {"token": "", "exp_ms": 0}
_SP_BUNDLE = {"js": "", "qhash": "", "at": 0.0}
_SP_HASH_FALLBACK = "21ea11a6ac75a5f3e01fd365d29c18e0a4a8b7dc09f89bbb5d73910190c6acaf"
_SP_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
         "Accept-Language": "en-US,en;q=0.9"}

def _sp_http(url, data=None, headers=None, timeout=25):
    """(status, body). HTTP errors return their status — callers quote it, never raise it away."""
    h = dict(_SP_UA)
    if headers:
        h.update(headers)
    try:
        req = urllib.request.Request(url, data=data, headers=h)
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""
    except Exception as e:
        raise RuntimeError(f"network: {type(e).__name__} {e}")

def _sp_totp(key: bytes, ts_ms: int) -> str:
    ctr = int(ts_ms // 1000 // 30)
    d = hmac.new(key, struct.pack(">Q", ctr), hashlib.sha1).digest()
    o = d[-1] & 0x0F
    return str((struct.unpack(">I", d[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

def _sp_bundle():
    """Web-player JS + fetchPlaylist persisted-query hash. Refreshed every 10 min (bundle name rotates)."""
    now = time.time()
    if _SP_BUNDLE["js"] and now - _SP_BUNDLE["at"] < 600:
        return _SP_BUNDLE["js"], _SP_BUNDLE["qhash"] or _SP_HASH_FALLBACK
    st, home = _sp_http("https://open.spotify.com/")
    if st != 200:
        raise RuntimeError(f"spotify home refused (HTTP {st})")
    m = re.search(r"https?://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[A-Za-z0-9]+\.js",
                  home.decode("utf-8", "ignore"))
    if not m:
        raise RuntimeError("spotify player bundle not found (open.spotify.com layout changed?)")
    st, jsb = _sp_http(m.group(0), timeout=60)
    if st != 200:
        raise RuntimeError(f"spotify player bundle refused (HTTP {st})")
    js = jsb.decode("utf-8", "ignore")
    pairs = re.findall(r"secret:(?:\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)')\s*,\s*version:(\d+)", js)
    if not pairs:
        raise RuntimeError("spotify totp secrets not found (player bundle changed?)")
    hm = re.search(r'"fetchPlaylist","query","([0-9a-f]{64})"', js)
    _SP_BUNDLE["js"] = js
    _SP_BUNDLE["qhash"] = hm.group(1) if hm else ""
    _SP_BUNDLE["at"] = now
    _SP_BUNDLE["_secrets"] = {int(v): (a or b) for a, b, v in pairs}
    return js, _SP_BUNDLE["qhash"] or _SP_HASH_FALLBACK

def _sp_anon_token():
    """Keyless web-player token (TOTP from the bundle's own secret). Memory only, never logged or stored."""
    now_ms = int(time.time() * 1000)
    if _SP_TOK["token"] and now_ms < _SP_TOK["exp_ms"] - 120000:
        return _SP_TOK["token"]
    js, _ = _sp_bundle()
    secrets = _SP_BUNDLE.get("_secrets", {})
    if not secrets:
        raise RuntimeError("spotify totp secrets not found (player bundle changed?)")
    ver = max(secrets)
    raw = secrets[ver].encode().decode("unicode_escape")
    key = "".join(str(ord(c) ^ ((i % 33) + 9)) for i, c in enumerate(raw)).encode()
    st, sb = _sp_http("https://open.spotify.com/api/server-time")
    try:
        server_ms = int(json.loads(sb.decode())["serverTime"]) * 1000
    except Exception:
        server_ms = 0
    qs = urllib.parse.urlencode({"reason": "transport", "productType": "web-player",
                                 "totp": _sp_totp(key, now_ms), "totpVer": str(ver),
                                 "ts": str(int(now_ms / 1000)),
                                 "totpServer": _sp_totp(key, server_ms) if server_ms else "unavailable"})
    st, tb = _sp_http("https://open.spotify.com/api/token?" + qs)
    if st != 200:
        raise RuntimeError(f"spotify keyless token refused (HTTP {st})")
    try:
        j = json.loads(tb.decode())
        tok, exp = j["accessToken"], int(j.get("accessTokenExpirationTimestampMs") or 0)
    except Exception:
        raise RuntimeError("spotify keyless token unreadable (player api changed?)")
    if not tok:
        raise RuntimeError("spotify keyless token empty (player api changed?)")
    _SP_TOK["token"] = tok
    _SP_TOK["exp_ms"] = exp or (now_ms + 3600 * 1000)
    blog("spotify", f"anon token ok len={len(tok)}")
    return tok

def _sp_playlist_page(pid, offset, limit, token, qhash):
    """One pathfinder fetchPlaylist page. Adapts to new required variables (bounded)."""
    variables = {"uri": f"spotify:playlist:{pid}", "offset": offset, "limit": limit,
                 "enableWatchFeedEntrypoint": False, "includeEpisodeContentRatingsV2": False}
    last = "unknown"
    for _ in range(6):
        url = ("https://api-partner.spotify.com/pathfinder/v1/query?operationName=fetchPlaylist"
               "&variables=" + urllib.parse.quote(json.dumps(variables))
               + "&extensions=" + urllib.parse.quote(json.dumps(
                   {"persistedQuery": {"version": 1, "sha256Hash": qhash}})))
        st, sb = _sp_http(url, headers={"Authorization": "Bearer " + token})
        txt = sb.decode("utf-8", "ignore")
        if st != 200:
            last = f"HTTP {st}"
            if st in (401, 403):
                raise RuntimeError(f"spotify full-list refused ({last})")
            time.sleep(1.0)
            continue
        try:
            j = json.loads(txt)
        except Exception:
            last = "bad json"
            time.sleep(1.0)
            continue
        errs = j.get("errors") or []
        missing = []
        for e in errs:
            missing += re.findall(r"missing variable `\$(\w+)`: for required GraphQL type `(\S+?)`",
                                  str(e.get("message", "")))
        if missing:
            for vname, vtyp in missing:
                if "Boolean" in vtyp:
                    variables[vname] = False
                elif "Int" in vtyp or "Float" in vtyp:
                    variables[vname] = 0
                else:
                    variables[vname] = ""
            last = "missing vars: " + ",".join(n for n, _t in missing)
            continue
        if errs:
            raise RuntimeError(f"spotify full-list refused ({str(errs[0].get('message', errs[0]))[:120]})")
        try:
            return j["data"]["playlistV2"]
        except Exception:
            raise RuntimeError("spotify full-list unreadable (partner shape changed?)")
    raise RuntimeError(f"spotify full-list refused ({last})")

def _sp_cover(pl):
    for node in (pl.get("images"), pl.get("coverArt")):
        srcs = None
        if isinstance(node, dict):
            srcs = node.get("sources")
        elif isinstance(node, list):
            srcs = node
        if srcs:
            for s in srcs:
                if isinstance(s, dict) and s.get("url"):
                    return s["url"]
    return ""

def fetch_spotify_tracks(playlist_url: str, cookie: str = ""):
    """Full keyless fetch: embed = metadata/fast path, api-partner pathfinder = full track list.
    api.spotify.com answers anonymous web-player tokens with HTTP 429 (verified live),
    so the partner pathfinder query is the full-fetch route. stdlib only, token memory-only."""
    pid = extract_playlist_id(playlist_url)
    name, cover = "", ""
    try:
        embed = f"https://open.spotify.com/embed/playlist/{pid}"
        req = urllib.request.Request(embed, headers=_SP_UA)
        raw = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
        m = re.search(r"__NEXT_DATA__[^>]*>(.*?)</script>", raw, re.S)
        if m:
            entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
            name = entity.get("title", "") or ""
            cover = (((entity.get("coverArt", {}) or {}).get("sources", [{}]) or [{}])[0] or {}).get("url", "") or ""
    except Exception as e:
        blog("spotify", f"embed metadata missed: {type(e).__name__}")
    token = _sp_anon_token()
    _js, qhash = _sp_bundle()
    first = _sp_playlist_page(pid, 0, 100, token, qhash)
    if not isinstance(first, dict) or first.get("__typename") != "Playlist":
        typename = first.get("__typename") if isinstance(first, dict) else "no playlist"
        raise RuntimeError(f"spotify full-list refused ({typename} — private/deleted link?)")
    name = first.get("name", "") or name
    cover = _sp_cover(first) or cover
    declared = int((first.get("content") or {}).get("totalCount") or 0)
    if not declared:
        raise RuntimeError("Spotify did not report a declared total — refusing to transfer a possibly partial playlist")
    tracks = []

    def _take(items):
        for it in items or []:
            data = ((it or {}).get("itemV2") or {}).get("data") or {}
            if data.get("__typename") in ("Track", "Episode"):
                arts = data.get("artists") or {}
                tracks.append({
                    "uri": data.get("uri", ""),
                    "title": data.get("name", ""),
                    "artist": ", ".join(a.get("profile", {}).get("name", "") for a in (arts.get("items") or [])),
                    "duration_ms": int((data.get("trackDuration") or {}).get("totalMilliseconds", 0)),
                })
            else:
                tracks.append({"uri": "", "title": "", "artist": "", "duration_ms": 0})

    _take((first.get("content") or {}).get("items"))
    if len(tracks) > declared:
        tracks = tracks[:declared]
    off = len(tracks)
    while off < declared:
        page = _sp_playlist_page(pid, off, 100, token, qhash)
        items = (page.get("content") or {}).get("items", [])
        if not items:
            break
        _take(items)
        if len(tracks) > declared:
            tracks = tracks[:declared]
            break
        off += len(items)
        time.sleep(0.2)
    if len(tracks) < declared:
        raise RuntimeError(f"Spotify reports {declared} tracks, fetched {len(tracks)} — refusing to transfer a partial playlist")
    if not tracks:
        raise RuntimeError("empty playlist (no tracks in spotify)")
    return {"playlist_id": pid, "playlist_name": name,
            "cover_url": cover, "tracks": tracks,
            "declared_total": declared}

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

_TRANSPORT_HINTS = ("timed out", "timeout", "urlerror", "connection", "reset by peer",
                     "temporary failure", "gaierror", "unreachable", "dns",
                     "429", "500", "502", "503", "504")

def _is_transport(err: Exception) -> bool:
    if isinstance(err, (urllib.error.URLError, TimeoutError)):
        return True
    s = f"{type(err).__name__} {getattr(err, 'code', '')} {err}".lower()
    return any(h in s for h in _TRANSPORT_HINTS)

def mrl_parts(mrl: dict):
    """(title, subtitle, duration, type). subtitle = artist/type/duration blob."""
    flex = mrl.get("flexColumns", []) or []

    def col_full(i):
        try:
            runs = flex[i]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"] or []
            return "".join((r or {}).get("text", "") for r in runs if isinstance(r, dict))
        except Exception:
            return ""

    title = col_full(0).strip()
    sub = col_full(1).strip()
    extra = " ".join(col_full(i) for i in range(2, len(flex))).strip()
    fixed_texts = []
    for fc in (mrl.get("fixedColumns", []) or []):
        try:
            runs = fc["musicResponsiveListItemFixedColumnRenderer"]["text"]["runs"] or []
            fixed_texts.append("".join((r or {}).get("text", "") for r in runs if isinstance(r, dict)))
        except Exception:
            continue
    fixed = " ".join(fixed_texts).strip()
    typ = sub.split("\u2022")[0].strip() if sub else ""
    m = re.search(r"\b(?:\d+:)?\d{1,2}:\d{2}\b", (fixed + " " + sub + " " + extra).strip())
    return title, sub, (m.group(0) if m else ""), typ

def mrl_texts(mrl: dict):
    """Compat: (title, type)."""
    t, _sub, _dur, typ = mrl_parts(mrl)
    return t, typ

def _version_zones(title: str) -> str:
    """Version phrase only: parenthesised/bracketed text plus the '- suffix'."""
    s = title or ""
    zones = []
    try:
        zones += re.findall(r"\(([^)]*)\)", s)
        zones += re.findall(r"\[([^\]]*)\]", s)
        parts = re.split(r"\s+[-\u2013\u2014]\s+", s)
        if len(parts) > 1:
            zones.append(" ".join(parts[1:]))
    except Exception:
        pass
    return " ".join(zones)


def _version_sig(title: str) -> frozenset:
    """Normalised set of version modifiers found in the version zones."""
    z = _version_zones(title).lower()
    try:
        z = unicodedata.normalize("NFKD", z).encode("ascii", "ignore").decode()
    except Exception:
        pass
    z = re.sub(r"[^a-z0-9\s]", " ", z)
    z = re.sub(r"\s+", " ", z).strip()
    if not z:
        # No parenthesised/bracketed/suffix zone: fall back to the full title
        # so bare "TITLE SUPER SLOWED" still reads as versioned.
        try:
            f = (title or "").lower()
            f = unicodedata.normalize("NFKD", f).encode("ascii", "ignore").decode()
            f = re.sub(r"[^a-z0-9\s]", " ", f)
            z = re.sub(r"\s+", " ", f).strip()
        except Exception:
            pass
    if not z:
        return frozenset()
    sig = set()
    if re.search(r"\bsped\s*up\b", z) or re.search(r"\bspeed\s*up\b", z):
        sig.add("sped up")
    if re.search(r"\bslowed\b", z):
        sig.add("slowed")
    if re.search(r"\bsuper\b", z):
        sig.add("super")
    if re.search(r"\bultra\b", z):
        sig.add("ultra")
    if re.search(r"\breverb\b", z):
        sig.add("reverb")
    if re.search(r"\bnightcore\b", z):
        sig.add("nightcore")
    if re.search(r"\b8\s*d\b", z):
        sig.add("8d")
    if re.search(r"\bbass\s*boost\w*\b", z):
        sig.add("bass boosted")
    if re.search(r"\binstrumental\b", z):
        sig.add("instrumental")
    if re.search(r"\bkaraoke\b", z):
        sig.add("karaoke")
    if re.search(r"\bacoustic\b", z):
        sig.add("acoustic")
    if re.search(r"\blive\b", z):
        sig.add("live")
    if re.search(r"\bremix\b", z):
        sig.add("remix")
    if re.search(r"\bedit\b", z):
        sig.add("edit")
    if re.search(r"\bextended\b", z):
        sig.add("extended")
    if re.search(r"\bdemo\b", z):
        sig.add("demo")
    if re.search(r"\bcover\b", z):
        sig.add("cover")
    if re.search(r"\balternat\w*\b", z):
        sig.add("alternative")
    return frozenset(sig)


def _sig_label(sig) -> str:
    try:
        s = sorted(set(sig or []))
    except Exception:
        return "plain"
    return "+".join(s) if s else "plain"


_PLAIN_FORBIDDEN = frozenset({"slowed", "sped up", "nightcore", "reverb", "8d", "bass boosted"})

def _norm_text(s: str) -> str:
    s = (s or "").lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"\b(?:feat\.?|ft\.?|featuring)\b.*$", " ", s)
    for pat in (r"\bremaster\w*\b.*$", r"\bofficial\b.*$", r"\blyric\w*\b.*$",
                r"\bsped\s*up\b.*$", r"\bspeed\s*up\b.*$", r"\bslowed\b.*$",
                r"\bsuper\b.*$", r"\bultra\b.*$", r"\balternat\w*\b.*$",
                r"\bremix\b.*$", r"\breverb\b.*$", r"\bnightcore\b.*$",
                r"\b8\s*d\b.*$", r"\bbass\s*boost\w*\b.*$",
                r"\binstrumental\b.*$", r"\bkaraoke\b.*$", r"\bacoustic\b.*$",
                r"\blive\b.*$", r"\bedit\b.*$", r"\bextended\b.*$",
                r"\bdemo\b.*$", r"\bcover\b.*$", r"\baudio\b.*$",
                r"\bvisualizer\b.*$"):
        s = re.sub(pat, " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _title_score(want_title: str, cand_title: str) -> float:
    wn = _norm_text(want_title)
    cn = _norm_text(cand_title)
    wt, ct = wn.split(), cn.split()
    if not wt or not ct:
        return 0.0
    sw, sc = set(wt), set(ct)
    j = len(sw & sc) / len(sw | sc) if (sw | sc) else 0.0
    bonus = 0.2 if (wn and cn and (wn in cn or cn in wn)) else 0.0
    return min(1.0, j + bonus)

def _artist_score(want_artist: str, cand_sub: str) -> float:
    wa = _norm_text(want_artist).split()
    ca = _norm_text(cand_sub).split()
    if not wa or not ca:
        return 0.0
    swa = set(wa)
    return len(swa & set(ca)) / len(swa)

def _parse_dur_s(s: str):
    m = re.search(r"\b(?:(\d+):)?(\d{1,2}):(\d{2})\b", (s or "").strip())
    if not m:
        return None
    h, mn, se = m.groups()
    try:
        return (int(h or 0) * 3600 + int(mn) * 60 + int(se))
    except Exception:
        return None

def _duration_score(want_ms: int, cand_dur: str):
    if not want_ms or not cand_dur:
        return None
    cs = _parse_dur_s(cand_dur)
    if cs is None:
        return None
    diff = abs(cs * 1000 - int(want_ms)) / 1000.0
    if diff <= 3:
        return 1.0
    if diff <= 8:
        return 0.5
    if diff > 20:
        return 0.0
    return 0.0

def _combined(t: float, a: float, d):
    if d is None:
        return 0.7 * t + 0.3 * a
    return 0.55 * t + 0.30 * a + 0.15 * d

def _score_candidates(want_title: str, want_artist: str, want_ms: int, cands: list) -> list:
    out = []
    for c in cands or []:
        t = _title_score(want_title, c.get("title", ""))
        a = _artist_score(want_artist, c.get("artist", ""))
        d = _duration_score(want_ms, c.get("duration", ""))
        out.append({**c, "t": round(t, 3), "a": round(a, 3),
                    "d": (None if d is None else round(d, 3)),
                    "score": round(_combined(t, a, d), 3)})
    out.sort(key=lambda x: -x["score"])
    return out

def _pick_best(query: str, want_title: str, want_artist: str, want_ms: int, cands: list) -> dict:
    cands = [c for c in (cands or []) if isinstance(c, dict)]
    want_sig = _version_sig(want_title)
    if want_sig:
        want_label = _sig_label(want_sig)
        matching = [c for c in cands if _version_sig(c.get("title", "")) == want_sig]
        if not matching:
            found = sorted({_sig_label(_version_sig(c.get("title", ""))) for c in cands if c.get("title")})
            raise RuntimeError(
                f"no confident match (version mismatch: want '{want_label}', "
                f"found [{', '.join(found[:6]) or 'none'}]) for: " + query)
        cands = matching
    else:
        # Plain source: the version IS the song, so a versioned row can never
        # substitute for it. Filter to plain-signature rows whenever any
        # exist; when only slowed/sped-up rows exist the row skips naming
        # what was found instead of falling back to a wrong version.
        plain = [c for c in cands
                 if not (_version_sig(c.get("title", "")) & _PLAIN_FORBIDDEN)]
        if plain:
            cands = plain
        elif cands:
            found = sorted({_sig_label(_version_sig(c.get("title", ""))) for c in cands if c.get("title")})
            raise RuntimeError(
                f"no confident match (only {', '.join(found[:6]) or 'slowed/sped-up'} "
                f"versions found) for: " + query)
    scored = _score_candidates(want_title, want_artist, want_ms, cands)
    scored = [c for c in scored if c.get("title")]
    if not scored:
        raise RuntimeError("no ytm result for: " + query)
    best = scored[0]
    if best["score"] >= 0.85:
        return best
    if best["score"] >= 0.60 and (len(scored) == 1 or (best["score"] - scored[1]["score"]) >= 0.08):
        return best
    second = scored[1]["score"] if len(scored) > 1 else -1.0
    raise RuntimeError(
        f"no confident match (best {best['score']:.2f}: '{best.get('title', '')}' | "
        f"'{best.get('artist', '')}' [{best.get('videoId', '')}], "
        f"runner-up {second:.2f}) for: " + query)

def _card_candidate(sec: dict):
    """Top-result card (musicCardShelfRenderer) as a candidate. The card is the
    one place these search responses carry a duration (subtitle tail mm:ss),
    so harvesting it is what makes duration scoring real; shelf rows keep the
    0.7/0.3 renormalisation when they carry none."""
    try:
        card = (sec or {}).get("musicCardShelfRenderer", {})
        if not card:
            return None
        runs = (card.get("title", {}) or {}).get("runs", []) or []
        if not runs:
            return None
        title = (runs[0].get("text", "") or "").strip()
        pid = ""
        try:
            pid = runs[0]["navigationEndpoint"]["watchEndpoint"]["videoId"]
        except Exception:
            pid = ""
        if not pid:
            mm = re.search(r'"videoId":\s*"([A-Za-z0-9_-]{11})"', json.dumps(card))
            pid = mm.group(1) if mm else ""
        if not pid or not title:
            return None
        sub = "".join((r or {}).get("text", "")
                      for r in ((card.get("subtitle", {}) or {}).get("runs", []) or []))
        m = re.search(r"\b(?:\d+:)?\d{1,2}:\d{2}\b", sub or "")
        typ = (sub or "").split("\u2022")[0].strip() if sub else ""
        return {"videoId": pid, "title": title or "", "artist": (sub or "").strip(),
                "duration": (m.group(0) if m else ""), "type": typ or ""}
    except Exception:
        return None


def _candidates_from_search_json(j: dict) -> list:
    try:
        sections = j["contents"]["tabbedSearchResultsRenderer"]["tabs"][0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"]
    except Exception:
        return []
    cands = []
    for sec in sections or []:
        card = _card_candidate(sec)
        if card:
            cands.append(card)
        items = sec.get("musicShelfRenderer", {}).get("contents", [])
        if not items and "itemSectionRenderer" in sec:
            items = sec["itemSectionRenderer"].get("contents", [])
        for item in items or []:
            mrl = (item or {}).get("musicResponsiveListItemRenderer", {})
            if not mrl:
                continue
            pid = mrl_video_id(mrl)
            if not pid:
                continue
            title, sub, dur, typ = mrl_parts(mrl)
            cands.append({"videoId": pid, "title": title or "", "artist": sub or "",
                          "duration": dur or "", "type": typ or ""})
    return cands

_SEARCH_CACHE = {}
_SEARCH_CACHE_MAX = 500


def _search_key(query: str, want_title: str, want_artist: str = "", want_ms: int = 0) -> tuple:
    """Cache key for one search row. The version signature is part of the key
    so a slowed lookup can never serve a sped-up row (or any other version):
    two source rows that differ only by version have different keys."""
    return (query or "", _sig_label(_version_sig(want_title)),
            (want_artist or "").strip().lower(), int(want_ms or 0))


def _search_cache_get(key):
    try:
        return _SEARCH_CACHE.get(key)
    except Exception:
        return None


def _search_cache_put(key, cands):
    try:
        if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
            _SEARCH_CACHE.clear()
        _SEARCH_CACHE[key] = [dict(c) for c in (cands or [])]
    except Exception:
        pass


def yt_search(query: str, title: str = "", artist: str = "", duration_ms: int = 0):
    """Confident match. Raises with best-candidate + score when nothing clears the bar."""
    want_title = (title or query or "").strip()
    want_artist = (artist or "").strip()
    want_ms = int(duration_ms or 0)
    key = _search_key(query, want_title, want_artist, want_ms)
    hit = _search_cache_get(key)
    if hit is not None:
        best = _pick_best(query, want_title, want_artist, want_ms, hit)
        best.update({"query": query})
        return best
    body = json.dumps({"context": {"client": CLIENT}, "query": query,
                       "params": "EgWKAQIIAWoQEAMQBBAJEAkQBRAK"}).encode()
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"https://music.youtube.com/youtubei/v1/search?key={YT_KEY}&prettyPrint=false",
                data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
                                    "Origin": YT_ORIGIN, "Referer": YT_ORIGIN + "/"})
            r = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
            break
        except Exception as e:
            last = e
            if not _is_transport(e) or attempt == 2:
                raise
            time.sleep(1.0 + attempt)
    else:
        raise last
    cands = _candidates_from_search_json(json.loads(r))
    _search_cache_put(key, cands)
    best = _pick_best(query, want_title, want_artist, want_ms, cands)
    best.update({"query": query})
    return best

def title_overlap(a: str, b: str) -> bool:
    wa = set(re.findall(r"[a-z0-9]{3,}", (a or "").lower()))
    wb = set(re.findall(r"[a-z0-9]{3,}", (b or "").lower()))
    return bool(wa & wb)

def yt_match(title: str, artist: str = "", duration_ms: int = 0) -> dict:
    """One song, isolated. Up to three query variants; first confident match wins.
    Never raises: no match -> videoId None (skip), never a batch failure."""
    title, artist = (title or "").strip(), (artist or "").strip()
    want_q = f"{title} {artist}".strip()
    last_err = ""
    for qi, q in enumerate([want_q, title, f"{artist} - {title}".strip()]):
        if not q or qi >= 3:
            continue
        try:
            r = yt_search(q, title=title, artist=artist, duration_ms=int(duration_ms or 0))
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        if r.get("videoId"):
            r["query"] = want_q
            r["via"] = q
            r["alt"] = qi > 0
            return {**{"ok": True}, **r}
    skip = (last_err[:220] if last_err else "no match on any variant")
    if "no confident match" not in skip and "no ytm result" not in skip:
        skip = "no match on any variant" + (f" ({skip})" if skip else "")
    return {"ok": True, "query": want_q, "videoId": None,
            "title": "", "duration": "", "skipped": skip[:220]}

def yt_search_safe(query: str):
    try:
        r = yt_search(query, title=query)
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

def clear_created() -> None:
    """Delete the local created-playlist store (created.json).

    LOCAL ONLY: this forgets which YTM playlist ids belong to which
    Spotify ids. It never calls any YTM endpoint, so the remote
    playlists are untouched — a later transfer of the same Spotify
    link simply creates fresh instead of resuming/topping up.
    Missing file is success (already clear)."""
    try:
        os.remove(CREATEDF)
    except FileNotFoundError:
        pass
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

def _repeat_count(video_ids: list, version_sigs=None) -> int:
    """Genuine repeats only: the same version, same videoId, twice in the source.
    Different versions resolve to different videoIds (the matcher rejects cross-
    version substitution), so a bare duplicate videoId is already genuine; when
    parallel version labels are supplied, pairs are counted to make it explicit."""
    ids = [v for v in (video_ids or []) if isinstance(v, str) and v]
    try:
        if version_sigs is not None and len(version_sigs) == len(video_ids):
            pairs = [(ids[i], str(version_sigs[i]))
                     for i in range(len(video_ids)) if isinstance(video_ids[i], str) and video_ids[i]]
            return len(pairs) - len(set(pairs))
    except Exception:
        pass
    return len(ids) - len(set(ids))


def _rows_for_video(video_id: str, rows: list) -> list:
    """All row indices carrying one videoId, in row order. A list, never a
    single index, because one videoId may legitimately appear twice in the
    source (a genuine repeat: same version, same videoId)."""
    out = []
    try:
        for k, row in enumerate(rows or []):
            if isinstance(row, dict) and row.get("videoId") == video_id:
                out.append(k)
    except Exception:
        pass
    return out

def _ledger(video_ids: list, entries, attempts: dict) -> list:
    """One outcome per requested copy, in send order. A copy counts as landed while the
    playlist still holds an unclaimed entry for that videoId — 4 copies need 4 entries.
    STATUS_SUCCEEDED is landed immediately (the append was accepted) and never renders
    as a refusal reason; only an explicit YTM STATUS_FAILED is 'rejected'."""
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
            if "SUCCEEDED" in su:
                out.append({"i": i + 1, "videoId": v, "outcome": "landed", "status": ""})
            elif "FAILED" in su:
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

def _read_back_retry(plid, yt_cookie, tries: int = 3):
    last = None
    for a in range(max(1, tries)):
        try:
            entries = yt_playlist_entries(plid, yt_cookie)
        except Exception:
            entries = None
        if entries is not None:
            return entries
        last = entries
        if a < tries - 1:
            time.sleep(1.5)
    return last

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

def _seed_ledger(video_ids, entries):
    """One ledger row per requested copy for the inline seed (no attempts yet)."""
    return _ledger(video_ids, entries, {})

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

def _create_new(yt_cookie, title, description, video_ids, privacy, spotify_id, hooks=None,
                version_sigs=None):
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
    seed_ledger = _seed_ledger(video_ids, first)
    if hooks and hooks.get("on_seed"):
        try:
            hooks["on_seed"](seed_ledger)
        except Exception:
            pass
    todo = [int(e["i"]) - 1 for e in seed_ledger if e["outcome"] != "landed"]
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
    repeats = _repeat_count(video_ids, version_sigs)
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
            "missing": missing, "repeats": repeats, "ledger": ledger,
            "playlist_count": len(entries) if isinstance(entries, list) else 0}

def _resume_incomplete(rec, yt_cookie, title, video_ids, spotify_id, hooks=None,
                       version_sigs=None):
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
    seed_ledger = _seed_ledger(video_ids, entries)
    if hooks and hooks.get("on_seed"):
        try:
            hooks["on_seed"](seed_ledger)
        except Exception:
            pass
    todo = [int(e["i"]) - 1 for e in seed_ledger if e["outcome"] != "landed"]
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
    repeats = _repeat_count(video_ids, version_sigs)
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
            "missing": missing, "repeats": repeats, "ledger": ledger, "resumed": True,
            "playlist_count": len(entries2) if isinstance(entries2, list) else 0}

def transfer_create_append(yt_cookie, title, description, video_ids, privacy="PRIVATE",
                           spotify_id="", hooks=None, version_sigs=None):
    """One implementation, two entry points: sync /api/create-playlist and the
    async /api/transfer worker. Never dedupes video_ids. The ledger works on
    the videoId multiset: a present Slowed never satisfies a missing Super
    Slowed, because versions resolve to distinct videoIds (or skip)."""
    yt_cookie = normalize_cookie(yt_cookie)
    ids = [v for v in (video_ids or []) if isinstance(v, str) and v]
    if not ids:
        raise ValueError("no videoIds to send")
    if spotify_id:
        rec = load_created().get(spotify_id)
        if rec and rec.get("yt_id"):
            if rec.get("complete") is True:
                entries = yt_playlist_entries(rec["yt_id"], yt_cookie)
                if entries is not None:
                    seed = _seed_ledger(ids, entries)
                    todo = [int(e["i"]) - 1 for e in seed if e["outcome"] != "landed"]
                    if not todo:
                        added = sum(1 for e in seed if e["outcome"] == "landed")
                        missing = sum(1 for e in seed if e["outcome"] != "landed")
                        for e in seed:
                            if e["outcome"] != "landed":
                                e["status"] = "exists — not on playlist"
                        blog("create", f"exists title={title} url={rec.get('url')} tracks={added}")
                        return {"playlistId": rec["yt_id"], "url": rec.get("url", ""),
                                "added": added, "sent": len(ids), "rejected": 0, "missing": missing,
                                "repeats": _repeat_count(ids, version_sigs), "existing": True, "ledger": seed,
                                "playlist_count": len(entries) if isinstance(entries, list) else 0}
                    on_phase = (hooks or {}).get("on_phase")
                    if on_phase:
                        try:
                            on_phase("append")
                        except Exception:
                            pass
                    plid = rec["yt_id"]
                    blog("topup", f"id={plid} have={len(entries)} missing={len(todo)}")
                    attempts = _append_todo(plid, ids, todo, yt_cookie, hooks)
                    if on_phase:
                        try:
                            on_phase("verify")
                        except Exception:
                            pass
                    entries2 = _read_back_retry(plid, yt_cookie)
                    if entries2 is None:
                        raise RuntimeError(f"playlist topped up at {rec.get('url')} — appends sent but the rest could not be verified (final read-back failed)")
                    ledger = _ledger(ids, entries2, attempts)
                    added, rejected, missing = _ledger_counts(ledger)
                    todo_set = set(todo)
                    newly = sum(1 for e in ledger
                                if e["outcome"] == "landed" and (int(e["i"]) - 1) in todo_set)
                    repeats = _repeat_count(ids, version_sigs)
                    blog("parity", f"topup {added}/{len(ids)} landed, {rejected} refused, {missing} missing")
                    _persist_final(spotify_id, plid, rec.get("url", ""), title, len(ids), added == len(ids))
                    return {"playlistId": plid, "url": rec.get("url", ""),
                            "added": added, "sent": len(ids), "rejected": rejected, "missing": missing,
                            "repeats": repeats, "existing": True, "ledger": ledger,
                            "playlist_count": len(entries2) if isinstance(entries2, list) else 0,
                            "topped_up": newly}
                    return {"playlistId": rec["yt_id"], "url": rec.get("url", ""),
                            "added": rec.get("expected", len(ids)), "sent": len(ids),
                            "rejected": 0, "missing": 0,
                            "repeats": _repeat_count(ids, version_sigs), "existing": True, "ledger": [],
                            "playlist_count": 0}
            else:
                return _resume_incomplete(rec, yt_cookie, title, ids, spotify_id, hooks, version_sigs)
    r = _create_new(yt_cookie, title, description, ids, privacy, spotify_id, hooks, version_sigs)
    return r

def yt_create_playlist(yt_cookie: str, title: str, description: str, video_ids: list, privacy: str = "PRIVATE"):
    """1:1 with the Spotify playlist: 48 tracks in => 48 entries out, repeats included.
    Nothing here drops, dedupes or membership-checks an id: every copy of the input is
    one copy we ask YTM to hold, and whatever YTM refuses is reported per copy."""
    return transfer_create_append(yt_cookie, title, description, video_ids, privacy, "", None)

def yt_create_checked(spotify_id: str, yt_cookie: str, title: str, description: str,
                      video_ids: list, privacy: str = "PRIVATE") -> dict:
    """Dupe-aware create: complete record + full live YTM copy -> return it, create nothing.
    Complete but short -> top up the same playlist, never a second playlist.
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
        declared = int(sp.get("declared_total") or 0)
        if declared and len(tracks) < declared:
            raise RuntimeError(f"Spotify reports {declared} tracks, fetched {len(tracks)} — refusing to transfer a partial playlist")
        with _RUN_LOCK:
            if RUN.get("id") != run_id:
                return
            RUN["spotify_id"] = sid
            RUN["title"] = pname
            RUN["total"] = len(tracks)
            RUN["declared"] = declared or len(tracks)
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
            for k, t in enumerate(tracks):
                if not (t.get("title") or "").strip() and not (t.get("uri") or "").strip():
                    RUN["rows"][k]["state"] = "skipped"
                    RUN["rows"][k]["status"] = "unavailable on Spotify — no title in source"
                    RUN["seq"] += 1
                    RUN["rows"][k]["row_seq"] = RUN["seq"]
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
                    if not (tracks[idx].get("title") or "").strip() and not (tracks[idx].get("uri") or "").strip():
                        matched_ids[idx] = ""
                        continue
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
        # video_ids position -> row index. video_ids holds one entry per matched
        # row (repeats included), in row order; skipped rows have no entry.
        # The version signature is part of each row's identity: two rows that
        # differ only by version are two distinct tracks needing two distinct
        # videoIds, so rows are joined to ledger entries by position, never by
        # collapsing on videoId. vid_rows maps each videoId to its list of row
        # indices (a list, because one videoId may legitimately appear twice
        # as a genuine repeat: same version, same videoId).
        with _RUN_LOCK:
            row_for_pos = [k for k, row in enumerate(RUN["rows"]) if row.get("videoId")]
            vid_rows = {}
            for k, row in enumerate(RUN["rows"]):
                v = row.get("videoId")
                if v:
                    vid_rows.setdefault(v, []).append(k)
            sigs_for_pos = [_sig_label(_version_sig(RUN["rows"][k].get("title", "")))
                            for k in row_for_pos]
        def _row_of_pos(pos):
            try:
                p = int(pos)
            except Exception:
                return None
            if 0 <= p < len(row_for_pos):
                return row_for_pos[p]
            return None
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
                rk = _row_of_pos(idx)
                if rk is None:
                    return
                try:
                    row = RUN["rows"][rk]
                except Exception:
                    return
                if row.get("state") == "landed":
                    return
                row["state"] = "appending"
                RUN["seq"] += 1
                row["row_seq"] = RUN["seq"]
                RUN["updated_at"] = time.time()
        def _on_one(idx, st):
            ok = "SUCCEEDED" in (st or "").upper()
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                rk = _row_of_pos(idx)
                if rk is None:
                    return
                try:
                    row = RUN["rows"][rk]
                except Exception:
                    return
                if ok:
                    row["status"] = ""
                    if row.get("state") != "landed":
                        row["state"] = "landed"
                        RUN["landed"] = int(RUN.get("landed", 0)) + 1
                else:
                    row["status"] = (st or "")[:80]
                    if row.get("state") != "landed":
                        row["state"] = "appending"
                RUN["seq"] += 1
                row["row_seq"] = RUN["seq"]
                RUN["updated_at"] = time.time()
        def _on_seed(arg):
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                if isinstance(arg, list):
                    n = 0
                    for e in arg:
                        if (e or {}).get("outcome") != "landed":
                            continue
                        try:
                            pos = int((e or {}).get("i", 0)) - 1
                        except Exception:
                            continue
                        rk = _row_of_pos(pos)
                        if rk is None:
                            continue
                        try:
                            RUN["rows"][rk]["state"] = "landed"
                            RUN["rows"][rk]["status"] = ""
                            RUN["seq"] += 1
                            RUN["rows"][rk]["row_seq"] = RUN["seq"]
                            n += 1
                        except Exception:
                            pass
                    RUN["landed"] = int(n)
                else:
                    RUN["landed"] = int(arg or 0)
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
            res = transfer_create_append(yt_cookie, pname, "", vids, privacy, sid, hooks, sigs_for_pos)
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
                for row in RUN["rows"]:
                    if row.get("state") not in ("landed", "refused", "missing", "skipped"):
                        row["state"] = "skipped"
                        row["status"] = f"stopped: {msg}"[:80]
                RUN["skipped"] = sum(1 for row in RUN["rows"] if row.get("state") == "skipped")
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
            # every row must carry a terminal state; reconcile strays as missing
            for row in RUN["rows"]:
                if row.get("state") not in ("landed", "skipped", "refused", "missing"):
                    row["state"] = "missing"
                    row["status"] = (row.get("status") or "reconciled as missing")[:80]
                    RUN["seq"] += 1
                    row["row_seq"] = RUN["seq"]
            # recount from rows so /api/run always agrees with the ledger chips
            RUN["landed"] = sum(1 for row in RUN["rows"] if row.get("state") == "landed")
            RUN["skipped"] = sum(1 for row in RUN["rows"] if row.get("state") == "skipped")
            RUN["refused"] = sum(1 for row in RUN["rows"] if row.get("state") == "refused")
            RUN["missing"] = sum(1 for row in RUN["rows"] if row.get("state") == "missing")
            buckets = {}
            for row in RUN["rows"]:
                if row.get("state") == "skipped":
                    s = (row.get("status") or "no reason").lower()
                    if "ambigu" in s:
                        b = "ambiguous"
                    elif "unavailable" in s:
                        b = "unavailable"
                    elif ("transport" in s or "timed out" in s or "timeout" in s or "urlopen" in s
                          or "urlerror" in s or "connection" in s or " 429" in s or "429 " in s
                          or "503" in s or "temporary failure" in s or "reset by peer" in s):
                        b = "transport"
                    elif "no match" in s or "confident" in s or "no ytm result" in s:
                        b = "no match"
                    else:
                        b = "other"
                    buckets[b] = buckets.get(b, 0) + 1
            skipped_n = int(RUN.get("skipped", 0))
            parts = ", ".join(f"{buckets[k]} {k}" for k in sorted(buckets) if buckets[k]) if skipped_n else "none"
            dec = int(RUN.get("declared", 0) or RUN.get("total", 0))
            n_pl = int(res.get("playlist_count", 0) or 0)
            RUN["playlist_count"] = n_pl
            if res.get("topped_up") is not None:
                summary = f"topped up {int(res.get('topped_up', 0))} of {int(res.get('sent', 0))} — {int(RUN.get('landed', 0))} of {dec} on the playlist (playlist holds {n_pl}), {skipped_n} skipped ({parts})"
            else:
                summary = f"done — {int(RUN.get('landed', 0))} of {dec} on the playlist (playlist holds {n_pl}), {skipped_n} skipped ({parts})"
            if int(RUN.get("refused", 0)):
                summary += f", {int(RUN.get('refused', 0))} refused"
            if int(RUN.get("missing", 0)):
                summary += f", {int(RUN.get('missing', 0))} missing"
            RUN["summary"] = summary
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
                    for row in RUN["rows"]:
                        if row.get("state") not in ("landed", "refused", "missing", "skipped"):
                            row["state"] = "skipped"
                            row["status"] = f"stopped: {msg}"[:80]
                    RUN["skipped"] = sum(1 for row in RUN["rows"] if row.get("state") == "skipped")
                    RUN["seq"] += 1
                    RUN["updated_at"] = time.time()
            except Exception:
                pass
        blog("fail", f"run={run_id} {msg}")

def _latest_incomplete():
    """Most recent incomplete created.json record (process-restart resume)."""
    try:
        created = load_created() or {}
    except Exception:
        return None
    for sid, rec in (created or {}).items():
        try:
            if isinstance(rec, dict) and rec.get("yt_id") and rec.get("complete") is not True:
                return {"spotify_id": sid, "playlist_id": rec.get("yt_id", ""),
                        "url": rec.get("url", ""), "title": rec.get("title", ""),
                        "expected": int(rec.get("expected", 0) or 0)}
        except Exception:
            continue
    return None

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
            out = {"ok": True, "run_id": snap.get("id", ""), "seq": snap.get("seq", 0),
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
                            "declared": int(snap.get("declared", 0) or 0),
                            "summary": snap.get("summary", ""),
                            "matched": int(snap.get("matched", 0) or 0),
                            "sent": int(snap.get("sent", 0) or 0),
                            "landed": int(snap.get("landed", 0) or 0),
                            "attempted": int(snap.get("landed", 0) or 0) + int(snap.get("refused", 0) or 0) + int(snap.get("missing", 0) or 0) + int(snap.get("skipped", 0) or 0),
                            "refused": int(snap.get("refused", 0) or 0),
                            "skipped": int(snap.get("skipped", 0) or 0),
                            "missing": int(snap.get("missing", 0) or 0),
                            "playlist_count": int(snap.get("playlist_count", 0) or 0),
                            "rows": rows,
                            "error": snap.get("error", ""),
                            "hint": snap.get("hint", ""),
                            "kind": snap.get("kind", "")}
            if not snap.get("id") and snap.get("phase") == "idle":
                try:
                    inc = _latest_incomplete()
                except Exception:
                    inc = None
                if inc:
                    out["interrupted"] = inc
            self.send_json(out)
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
                blog("spotify", f"ok name={r.get('playlist_name')} n={len(r.get('tracks', []))} declared={r.get('declared_total')} {scrub_cookie(payload.get('spotify_cookie', ''))}")
                return self.send_json({"ok": True, **r})
            if self.path == "/api/search":
                q = str(payload.get("query", ""))[:300]
                if not q.strip():
                    return self.send_json({"ok": False, "error": "empty query"}, 400)
                r = yt_search(q, title=q)
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
            if self.path == "/api/reset":
                if run_is_live():
                    return self.send_json({"ok": False, "error": "a transfer is already running"}, 409)
                run_reset()
                clear_created()
                blog("reset", "run + created link cleared; settings/vault/watch kept")
                return self.send_json({"ok": True})
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
