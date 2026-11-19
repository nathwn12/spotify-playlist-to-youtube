#!/usr/bin/env python3
import base64
import ctypes
import difflib
import hashlib
import hmac
import json
import os
import random
import re
import struct
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_VERSION = "0.5.2"

def _now() -> float:
    return time.time()

def _pause(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)

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
RUN_WAKE = threading.Event()   # set by /api/retry: ends a standby wait early
RUN = {
    "id": "",
    "seq": 0,
    "phase": "idle",
    "state": "idle",
    "attempt": 0,
    "next_attempt_at": 0.0,
    "reason": "",
    "residual": [],
    "started_at": 0.0,
    "updated_at": time.time(),
    "spotify_id": "",
    "title": "",
    "cover_url": "",
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
    "unverified": 0,
    "duplicate": 0,
    "pending": 0,
    "unsure": 0,
    "tail_unverified": 0,
    "guessed": 0,
    "rows": [],
    "error": "",
    "hint": "",
    "kind": "",
    "declared": 0,
    "summary": "",
    "playlist_count": 0,
    "counts": {"source": 0, "unique": 0, "added": 0, "skipped": 0,
               "duplicate": 0, "unverified": 0, "refused": 0, "pending": 0},
    "order_ok": True,
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
        RUN["state"] = "running"
        RUN["attempt"] = 0
        RUN["next_attempt_at"] = 0.0
        RUN["reason"] = ""
        RUN["residual"] = []
        RUN["started_at"] = now
        RUN["updated_at"] = now
        RUN["spotify_id"] = spotify_id
        RUN["title"] = title
        RUN["cover_url"] = ""
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
        RUN["unverified"] = 0
        RUN["duplicate"] = 0
        RUN["pending"] = 0
        RUN["unsure"] = 0
        RUN["tail_unverified"] = 0
        RUN["guessed"] = 0
        RUN["order_ok"] = True
        RUN["declared"] = 0
        RUN["summary"] = ""
        RUN["playlist_count"] = 0
        RUN["counts"] = {"source": 0, "unique": 0, "added": 0, "skipped": 0,
                         "duplicate": 0, "unverified": 0, "refused": 0, "pending": 0}
        RUN["rows"] = []
        RUN["error"] = ""
        RUN["hint"] = ""
        RUN["kind"] = ""
        RUN["seq"] += 1
        return rid

def run_reset() -> bool:
    with _RUN_LOCK:
        if RUN.get("phase") in ("spotify", "search", "create", "append", "verify"):
            return False
        RUN["id"] = ""
        RUN["phase"] = "idle"
        RUN["state"] = "idle"
        RUN["attempt"] = 0
        RUN["next_attempt_at"] = 0.0
        RUN["reason"] = ""
        RUN["residual"] = []
        RUN["started_at"] = 0.0
        RUN["updated_at"] = time.time()
        RUN["spotify_id"] = ""
        RUN["title"] = ""
        RUN["cover_url"] = ""
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
        RUN["unverified"] = 0
        RUN["duplicate"] = 0
        RUN["pending"] = 0
        RUN["unsure"] = 0
        RUN["tail_unverified"] = 0
        RUN["guessed"] = 0
        RUN["order_ok"] = True
        RUN["declared"] = 0
        RUN["summary"] = ""
        RUN["playlist_count"] = 0
        RUN["counts"] = {"source": 0, "unique": 0, "added": 0, "skipped": 0,
                         "duplicate": 0, "unverified": 0, "refused": 0, "pending": 0}
        RUN["rows"] = []
        RUN["error"] = ""
        RUN["hint"] = ""
        RUN["kind"] = ""
        RUN["seq"] += 1
        return True

def run_retry_now() -> bool:
    with _RUN_LOCK:
        if RUN.get("state") != "standby":
            return False
        RUN["next_attempt_at"] = time.time()
        RUN["reason"] = "retry now"
        _run_touch_locked()
    RUN_WAKE.set()
    return True

VAULTF = os.path.join(STATE, "cookie_vault.json")
WATCHF = os.path.join(STATE, "cookie_watch.json")
COOKIEF = os.path.join(STATE, "cookie_state.json")
COOKIE_EXTS = (".txt", ".cookies")
COOKIE_MAX_FILES = 200
COOKIE_MAX_READ = 200000
COOKIE_MAX_SIZE = 1024 * 1024
YT_AUTH = ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")
SP_AUTH = ("sp_dc", "sp_key")
_COOKIE_LOCK = threading.RLock()
_COOKIE_HOOK = None   # test seam: hook(name) is called at every cookie-record read/write boundary
_STATE_MSG = {"connected": "Connected", "unverified": "Saved, not checked yet",
              "expired": "Needs a fresh file", "junk": "That file is not a cookie",
              "network": "Could not reach YouTube Music", "none": "Not connected yet"}

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

def _dir_source(d: str) -> str:
    b = os.path.basename(os.path.normpath(d)).lower()
    if b == "desktop":
        return "desktop"
    if b == "downloads":
        return "downloads"
    return "extra"

def _netscape_rows(raw: str) -> list:
    rows = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[10:].strip()
        if not line or line.startswith("#") or "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            rows.append(parts)
    return rows

def cookie_site(raw: str, filename: str = "") -> str:
    s = raw or ""
    fn = (filename or "").lower()
    rows = _netscape_rows(s)
    if any(m in s for m in YT_AUTH) or any(r[0].strip().lower().endswith(".youtube.com") for r in rows):
        return "yt"
    if any(m in s for m in SP_AUTH) or any(r[0].strip().lower().endswith(".spotify.com") for r in rows):
        return "sp"
    if "music.youtube.com_cookies.txt" in fn:
        return "yt"
    if "spotify" in fn:
        return "sp"
    return ""

def cookie_shape(raw: str, site: str) -> dict:
    norm = normalize_cookie(raw)
    if not norm:
        return {"ok": False, "state": "empty", "reason": "no cookie pairs in the file", "expires_at": 0}
    head = (raw or "").strip()[:400].lower()
    if head.startswith("<!doctype") or "<html" in head:
        return {"ok": False, "state": "junk", "reason": "looks like an HTML page", "expires_at": 0}
    if head.startswith("{") or head.startswith("["):
        return {"ok": False, "state": "junk", "reason": "looks like JSON", "expires_at": 0}
    pairs = [p.strip() for p in norm.split(";") if "=" in p]
    if len(pairs) < 2:
        return {"ok": False, "state": "junk", "reason": "fewer than 2 name=value pairs", "expires_at": 0}
    auth = YT_AUTH if site == "yt" else SP_AUTH
    found = [(p.split("=", 1)[0].strip(), p.split("=", 1)[1].strip()) for p in pairs]
    mine = [(n, v) for n, v in found if n in auth]
    if not mine:
        return {"ok": False, "state": "junk", "reason": "no login cookie for this site", "expires_at": 0}
    for name, val in mine:
        if len(val) < 10:
            return {"ok": False, "state": "junk", "reason": f"{name} is too short to be a cookie value", "expires_at": 0}
        if re.search(r"[<>\"'\s]", val) or "..." in val or "\u2026" in val:
            return {"ok": False, "state": "junk", "reason": f"{name} is a placeholder, not a cookie value", "expires_at": 0}
    rows = [r for r in _netscape_rows(raw) if r[5].strip() in auth]
    expiries = []
    for r in rows:
        try:
            expiries.append(int(r[4]))
        except Exception:
            pass
    if expiries and 0 not in expiries and max(expiries) < int(time.time()):
        return {"ok": False, "state": "expired", "reason": "its own expiry field is in the past",
                "expires_at": max(expiries)}
    return {"ok": True, "state": "valid", "reason": "", "expires_at": max([e for e in expiries if e > 0] or [0])}

def scan_disk_cookies() -> dict:
    allv, ignored = {}, []
    dirs = cookie_dirs()
    seen = 0
    for d in dirs:
        src = _dir_source(d)
        try:
            names = sorted(os.listdir(d))
        except Exception:
            continue
        for n in names:
            if seen >= COOKIE_MAX_FILES:
                break
            if not n.lower().endswith(COOKIE_EXTS):
                continue
            p = os.path.join(d, n)
            try:
                if not os.path.isfile(p) or os.path.getsize(p) > COOKIE_MAX_SIZE:
                    continue
                with open(p, encoding="utf-8", errors="ignore") as f:
                    raw = f.read(COOKIE_MAX_READ)
                mt = os.path.getmtime(p)
            except Exception:
                continue
            seen += 1
            site = cookie_site(raw, n)
            if not site:
                continue
            shp = cookie_shape(raw, site)
            if shp["state"] in ("junk", "expired"):
                ignored.append({"name": n, "site": site, "state": shp["state"], "reason": shp["reason"]})
                continue
            if shp["state"] != "valid":
                continue
            allv.setdefault(site, []).append({"path": p, "name": n, "raw": raw, "mtime": mt,
                                              "state": shp["state"], "expires_at": shp["expires_at"],
                                              "source": src})
    for cands in allv.values():
        cands.sort(key=lambda c: c["mtime"], reverse=True)
    best = {site: cands[0] for site, cands in allv.items()}
    return {"best": best, "all": allv, "ignored": ignored, "dirs": dirs}

def detect_site(cookie: str) -> str:
    return cookie_site(cookie)

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

def _dpapi_blob(fn, data: bytes) -> bytes:
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

def _vault_read() -> dict:
    try:
        with open(VAULTF, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}

def _vault_write(v: dict) -> bool:
    try:
        with open(VAULTF, "w", encoding="utf-8") as f:
            json.dump(v, f)
        return True
    except Exception:
        return False

def _decode(v: dict, site: str) -> str:
    try:
        return _dpapi_blob(
            ctypes.windll.crypt32.CryptUnprotectData, base64.b64decode(v[site])).decode() if v.get(site) else ""
    except Exception:
        return ""

def _vault_meta(v: dict, site: str) -> dict:
    m = v.get("_meta") if isinstance(v.get("_meta"), dict) else {}
    m = m.get(site) if isinstance(m.get(site), dict) else {}
    return {"latched_at": m.get("latched_at") or 0, "source": m.get("source") or ""}

def _cookie_hook(name: str):
    h = _COOKIE_HOOK
    if h:
        h(name)

def cookie_hash(cookie: str) -> str:
    return hashlib.sha256(normalize_cookie(cookie).encode()).hexdigest()[:16]

def cookie_states() -> dict:
    try:
        with open(COOKIEF, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _cookie_state_set(site: str, state: str, msg: str, source: str, cookie: str, checked_at=None):
    with _COOKIE_LOCK:
        d = cookie_states()
        d[site] = {"hash": cookie_hash(cookie), "state": state, "msg": (msg or "")[:200],
                   "checked_at": int(time.time()) if checked_at is None else int(checked_at),
                   "source": source or "unknown"}
        try:
            with open(COOKIEF, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except Exception:
            pass

def vault_save(which: str, cookie: str, source: str = "manual", latched_at: float = 0) -> dict:
    if which not in ("sp", "yt"):
        return {"ok": False, "state": "none", "msg": "unknown site"}
    shp = cookie_shape(cookie, which)
    if shp["state"] != "valid":
        return {"ok": False, "state": shp["state"], "msg": shp["reason"]}
    c = normalize_cookie(cookie)
    with _COOKIE_LOCK:
        try:
            v = _vault_read()
            _cookie_hook("save")
            v[which] = base64.b64encode(_dpapi_blob(ctypes.windll.crypt32.CryptProtectData, c.encode())).decode()
            meta = v.get("_meta") if isinstance(v.get("_meta"), dict) else {}
            meta[which] = {"latched_at": latched_at or time.time(), "source": source}
            v["_meta"] = meta
            if not _vault_write(v):
                return {"ok": False, "state": "unverified", "msg": "could not save the cookie"}
        except Exception:
            return {"ok": False, "state": "unverified", "msg": "could not save the cookie"}
        _cookie_state_set(which, "unverified", "saved", source, c, checked_at=0)
    return {"ok": True, "state": "unverified", "msg": "saved"}

def vault_load() -> dict:
    scan = scan_disk_cookies()
    with _COOKIE_LOCK:
        return _vault_load_adopt(scan)

def _vault_load_adopt(scan: dict) -> dict:
    v = _vault_read()
    out, refreshed = {}, []
    for k in ("sp", "yt"):
        out[k + "_cookie"] = _decode(v, k)
    meta = {site: _vault_meta(v, site) for site in ("sp", "yt")}
    states = cookie_states()
    for site, cand in scan["best"].items():
        st = states.get(site) or {}
        h = cookie_hash(cand["raw"])
        if st.get("state") in ("expired", "junk", "removed") and st.get("hash") == h:
            continue
        cur = out.get(site + "_cookie") or ""
        if cur and (cookie_hash(cur) == h or meta[site]["latched_at"] >= cand["mtime"]):
            continue
        _cookie_hook("adopt")
        norm = normalize_cookie(cand["raw"])
        try:
            v[site] = base64.b64encode(_dpapi_blob(
                ctypes.windll.crypt32.CryptProtectData, norm.encode())).decode()
        except Exception:
            continue
        out[site + "_cookie"] = norm
        meta[site] = {"latched_at": cand["mtime"], "source": cand["source"]}
        if st.get("hash") != h:
            msg = f"found {cand['name']}; checking"
            _cookie_state_set(site, "unverified", msg, cand["source"], norm, checked_at=0)
            states[site] = {"hash": h, "state": "unverified", "msg": msg,
                            "checked_at": 0, "source": cand["source"]}
        refreshed.append(site + ":" + cand["name"])
    if refreshed:
        v["_meta"] = meta
        if _vault_write(v):
            blog("relatch", "fresh disk files adopted: " + ",".join(refreshed))
    site_state = {}
    for site in ("sp", "yt"):
        cur = out.get(site + "_cookie") or ""
        entry = states.get(site) or {}
        st = entry.get("state") or ""
        if not cur:
            site_state[site] = "none"
        elif entry.get("hash") != cookie_hash(cur):
            site_state[site] = "unverified"   # the record describes some other value
        elif st in ("expired", "junk"):
            out[site + "_cookie"] = ""
            site_state[site] = st
        else:
            site_state[site] = st if st in ("connected", "unverified", "network") else "unverified"
    out["refreshed"] = refreshed
    out["meta"] = meta
    out["state"] = site_state
    return out

def vault_remove(which: str):
    if which not in ("sp", "yt"):
        return
    with _COOKIE_LOCK:
        v = _vault_read()
        held = _decode(v, which)
        _cookie_hook("remove")   # inside the section: a thread parked here already holds the lock
        v.pop(which, None)
        meta = v.get("_meta") if isinstance(v.get("_meta"), dict) else {}
        meta.pop(which, None)
        v["_meta"] = meta
        _vault_write(v)
        d = cookie_states()
        prev = d.get(which) or {}
        if not held and prev.get("state") == "removed" and prev.get("hash"):
            return   # a second remove keeps the block on the value already removed
        d[which] = {"hash": cookie_hash(held) if held else "", "state": "removed", "msg": "removed",
                    "checked_at": int(time.time()), "source": ""}
        try:
            with open(COOKIEF, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except Exception:
            pass

def vault_forget():
    with _COOKIE_LOCK:
        for site in ("sp", "yt"):
            vault_remove(site)
        _cookie_hook("forget")
        for p in (VAULTF, COOKIEF):
            try:
                os.remove(p)
            except Exception:
                pass

def _vault_source(site: str) -> str:
    return _vault_meta(_vault_read(), site)["source"] or "unknown"

def cookie_check(site: str, cookie: str, source: str = "") -> dict:
    seen = cookie_states().get(site) or {}
    sig = (seen.get("hash"), seen.get("checked_at"))
    shp = cookie_shape(cookie, site)
    if shp["state"] == "empty":
        return {"ok": False, "state": "none", "msg": _STATE_MSG["none"],
                "detail": "missing - upload the cookie file"}
    if shp["state"] == "junk":
        state, detail = "junk", shp["reason"]
    elif shp["state"] == "expired":
        state, detail = "expired", "expired - its own expiry field is in the past"
    else:
        r = check_yt_cookie(cookie) if site == "yt" else check_spotify_cookie(cookie)
        detail = str(r.get("msg") or "")
        low = detail.lower()
        if r.get("ok"):
            state = "connected"
        elif "could not reach" in low:
            state = "network"
        elif "no sapisid" in low:
            state = "junk"
        else:
            state = "expired"
    msg = _STATE_MSG.get(state, "")
    if state == "network":
        msg += " - your file is saved"
    with _COOKIE_LOCK:
        then = cookie_states().get(site) or {}
        if (then.get("hash"), then.get("checked_at")) != sig:
            return {"ok": state == "connected", "state": state, "msg": msg, "detail": detail}
        _cookie_hook("check")
        src = source or _vault_source(site)
        held = _decode(_vault_read(), site)
        stray = bool(held) and cookie_hash(held) != cookie_hash(cookie)   # a probe, not the stored value
        if state in ("expired", "junk") and not stray:
            if not held or cookie_hash(held) == cookie_hash(cookie):
                vault_remove(site)   # auto-reject only the value that failed
        if not stray:
            _cookie_state_set(site, state, detail, src, cookie)
    return {"ok": state == "connected", "state": state, "msg": msg, "detail": detail}

def cookies_state() -> dict:
    v = vault_load()
    scan = scan_disk_cookies()
    states = cookie_states()
    by_hash = {(site, cookie_hash(c["raw"])): c for site, c in scan["best"].items()}
    body = {}
    for site in ("sp", "yt"):
        cookie = v.get(site + "_cookie") or ""
        state = (v.get("state") or {}).get(site) or "none"
        meta = (v.get("meta") or {}).get(site) or {}
        cand = by_hash.get((site, cookie_hash(cookie))) if cookie else None
        known = bool(cookie) or state != "none"
        entry = states.get(site) or {}
        fresh = bool(cookie) and entry.get("hash") == cookie_hash(cookie)
        body[site] = {"has": bool(cookie), "state": state, "msg": _STATE_MSG.get(state, ""),
                      "source": (meta.get("source") or "") if known else "",
                      "file": cand["name"] if cand else "",
                      "latched_at": int(meta.get("latched_at") or 0),
                      "checked_at": int(entry.get("checked_at") or 0) if fresh else 0}
    detected = {}
    for site in ("sp", "yt"):
        c = scan["best"].get(site)
        detected[site] = {"name": c["name"], "path": c["path"], "mtime": int(c["mtime"])} if c else None
    return {"ok": True, "dirs": scan["dirs"], **body, "detected": detected, "ignored": scan["ignored"]}

def blog(event: str, detail: str = ""):
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
    if "cannot be read" in m or "not a playlist record" in m:
        return {"kind": "write",
                "hint": ("The resume record cannot be read, so this app will not touch your "
                         "playlist. Copy that file somewhere safe before repairing it, check "
                         "your YouTube Music library for a playlist from this run, and add "
                         "what is missing by hand.")}
    if "returned no id" in m or "did not accept the create" in m or "cannot tell whether" in m:
        return {"kind": "write",
                "hint": ("A playlist may have been made and this app cannot tell. Check your "
                         "YouTube Music library: if it is there, keep it and add what is "
                         "missing by hand (its link is in the address bar). No second "
                         "playlist is created.")}
    if "does not name the copies it was sent" in m or "not the plan they belong to" in m:
        return {"kind": "user",
                "hint": ("That playlist's record does not name the copies it was sent, so "
                         "nothing was sent. Check the playlist in YouTube Music and add what "
                         "is missing by hand.")}
    if "older version of the app" in m:
        return {"kind": "user",
                "hint": ("That playlist's record is from an older version and cannot prove "
                         "which copies were already written, so nothing was sent and the "
                         "playlist was not changed. Check the playlist in YouTube Music and "
                         "add what is missing by hand.")}
    if "resume record" in m or "created.json" in m:
        return {"kind": "write", "hint": "The resume record could not be written — check that the app data folder is writable. Nothing further was sent."}
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

def _write_verdict(status: str) -> dict:
    s = str(status or "")
    code = next((c for c in ("400", "401", "403", "404") if c in s), "")
    if code in ("401", "403"):
        named = "Unauthorized" if code == "401" else "Forbidden"
        return {"kind": "auth",
                "hint": (f"YouTube Music answered the write with HTTP {code} {named}, which "
                         f"is an auth-class refusal and nothing more: the session may have "
                         f"expired, this account may not be allowed to edit that playlist (a "
                         f"permission answer), or the playlist may no longer be reachable "
                         f"from this account. Nothing here can tell those apart, and sending "
                         f"again does not prove which one it was. If a safe recovery record "
                         f"exists for this source, follow its own guidance — it knows which "
                         f"copies were proved; otherwise preserve the playlist as it stands "
                         f"and check it before adding anything by hand. Do not start a new "
                         f"transfer over this source: with no record it cannot know which "
                         f"copies already landed.")}
    if code == "404":
        return {"kind": "write",
                "hint": ("YouTube Music answered HTTP 404 for that playlist id — it was "
                         "deleted, or the id is stale, and nothing here can tell which. "
                         "Check the playlist and the record that names it before anything "
                         "else: keep both as they are, do not reset the run or delete a "
                         "record to clear this error, because the record is the only account "
                         "of what was already sent.")}
    if code == "400":
        return {"kind": "write",
                "hint": ("YouTube Music rejected the write with HTTP 400 — the request "
                         "was refused as sent. Keep the record and the playlist as they "
                         "are, then copy this error line to the agent.")}
    return {"kind": "unknown",
            "hint": ("YouTube Music refused the write and the status carries no code this "
                     "app reads — copy this error line to the agent.")}

YT_KEY = "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30"
YT_ORIGIN = "https://music.youtube.com"
CLIENT = {"clientName": "WEB_REMIX", "clientVersion": "1.20251027.01.00", "hl": "en", "gl": "US"}

def normalize_cookie(raw: str = "") -> str:
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
        if line.startswith("#HttpOnly_"):
            line = line[10:].strip()   # HttpOnly prefix is data, not a comment
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
        raise RuntimeError(f"network: {type(e).__name__} {e}") from e

def _sp_totp(key: bytes, ts_ms: int) -> str:
    ctr = int(ts_ms // 1000 // 30)
    d = hmac.new(key, struct.pack(">Q", ctr), hashlib.sha1).digest()
    o = d[-1] & 0x0F
    return str((struct.unpack(">I", d[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

def _sp_bundle():
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
        raise RuntimeError("spotify keyless token unreadable (player api changed?)") from None
    if not tok:
        raise RuntimeError("spotify keyless token empty (player api changed?)")
    _SP_TOK["token"] = tok
    _SP_TOK["exp_ms"] = exp or (now_ms + 3600 * 1000)
    blog("spotify", f"anon token ok len={len(tok)}")
    return tok

def _sp_playlist_page(pid, offset, limit, token, qhash):
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
            raise RuntimeError("spotify full-list unreadable (partner shape changed?)") from None
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
                album = ((data.get("albumOfTrack") or {}).get("name") or "")
                tracks.append({
                    "uri": data.get("uri", ""),
                    "title": data.get("name", ""),
                    "artist": ", ".join(a.get("profile", {}).get("name", "") for a in (arts.get("items") or [])),
                    "duration_ms": int((data.get("trackDuration") or {}).get("totalMilliseconds", 0)),
                    "album": album,
                })
            else:
                tracks.append({"uri": "", "title": "", "artist": "", "duration_ms": 0,
                               "album": ""})

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
    t, _sub, _dur, typ = mrl_parts(mrl)
    return t, typ

def _version_zones(title: str) -> str:
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
    z = _version_zones(title).lower()
    try:
        z = unicodedata.normalize("NFKD", z).encode("ascii", "ignore").decode()
    except Exception:
        pass
    z = re.sub(r"[^a-z0-9\s]", " ", z)
    z = re.sub(r"\s+", " ", z).strip()
    if not z:
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


def _edition_sig(s: str) -> frozenset:
    try:
        f = unicodedata.normalize("NFKD", (s or "").casefold()).encode("ascii", "ignore").decode()
    except Exception:
        f = (s or "").casefold()
    words = set(re.sub(r"[^a-z0-9]+", " ", f).split())
    sig = set()
    if "deluxe" in words:
        sig.add("deluxe")
    if "expanded" in words:
        sig.add("expanded")
    if "anniversary" in words:
        sig.add("anniversary")
    if words & {"remaster", "remastered"}:
        sig.add("remaster")
    if "single" in words:
        sig.add("single")
    if "ep" in words:
        sig.add("ep")
    if words & {"bonus", "collector", "collectors", "special", "reissue"}:
        sig.add("bonus")
    return frozenset(sig)


def _prefer_edition(scored: list, album: str) -> list:
    try:
        want = _edition_sig(album)
        if not want or len(scored) < 2:
            return scored
        top = float(scored[0].get("score") or 0)
        near = [c for c in scored if top - float(c.get("score") or 0) <= 0.05]
        if len(near) < 2:
            return scored
        chosen = None
        for c in near:
            text = f"{c.get('title', '')} {c.get('artist', '')}"
            if _edition_sig(text) == want:
                chosen = c
                break
        if chosen is None:
            return scored
        return [chosen] + [c for c in scored if c is not chosen]
    except Exception:
        return scored


_CJK_RE = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_KANA_RE = re.compile("[\u3041-\u3096\u30a1-\u30fa]")

_KANA_BASE = {
    "\u3042": "a", "\u3044": "i", "\u3046": "u", "\u3048": "e", "\u304a": "o",
    "\u304b": "ka", "\u304d": "ki", "\u304f": "ku", "\u3051": "ke", "\u3053": "ko",
    "\u304c": "ga", "\u304e": "gi", "\u3050": "gu", "\u3052": "ge", "\u3054": "go",
    "\u3055": "sa", "\u3057": "shi", "\u3059": "su", "\u305b": "se", "\u305d": "so",
    "\u3056": "za", "\u3058": "ji", "\u305a": "zu", "\u305c": "ze", "\u305e": "zo",
    "\u305f": "ta", "\u3061": "chi", "\u3064": "tsu", "\u3066": "te", "\u3068": "to",
    "\u3060": "da", "\u3062": "ji", "\u3065": "zu", "\u3067": "de", "\u3069": "do",
    "\u306a": "na", "\u306b": "ni", "\u306c": "nu", "\u306d": "ne", "\u306e": "no",
    "\u306f": "ha", "\u3072": "hi", "\u3075": "fu", "\u3078": "he", "\u307b": "ho",
    "\u3070": "ba", "\u3073": "bi", "\u3076": "bu", "\u3079": "be", "\u307c": "bo",
    "\u3071": "pa", "\u3074": "pi", "\u3077": "pu", "\u307a": "pe", "\u307d": "po",
    "\u307e": "ma", "\u307f": "mi", "\u3080": "mu", "\u3081": "me", "\u3082": "mo",
    "\u3084": "ya", "\u3086": "yu", "\u3088": "yo",
    "\u3089": "ra", "\u308a": "ri", "\u308b": "ru", "\u308c": "re", "\u308d": "ro",
    "\u308f": "wa", "\u3092": "o", "\u3093": "n", "\u3094": "vu",
    "\u3090": "wi", "\u3091": "we",
    "\u304d\u3083": "kya", "\u304d\u3085": "kyu", "\u304d\u3087": "kyo",
    "\u304e\u3083": "gya", "\u304e\u3085": "gyu", "\u304e\u3087": "gyo",
    "\u3057\u3083": "sha", "\u3057\u3085": "shu", "\u3057\u3087": "sho",
    "\u3058\u3083": "ja", "\u3058\u3085": "ju", "\u3058\u3087": "jo",
    "\u3061\u3083": "cha", "\u3061\u3085": "chu", "\u3061\u3087": "cho",
    "\u306b\u3083": "nya", "\u306b\u3085": "nyu", "\u306b\u3087": "nyo",
    "\u3072\u3083": "hya", "\u3072\u3085": "hyu", "\u3072\u3087": "hyo",
    "\u3073\u3083": "bya", "\u3073\u3085": "byu", "\u3073\u3087": "byo",
    "\u3074\u3083": "pya", "\u3074\u3085": "pyu", "\u3074\u3087": "pyo",
    "\u307f\u3083": "mya", "\u307f\u3085": "myu", "\u307f\u3087": "myo",
    "\u308a\u3083": "rya", "\u308a\u3085": "ryu", "\u308a\u3087": "ryo",
    "\u3066\u3043": "ti", "\u3067\u3043": "di", "\u3068\u3045": "tu", "\u3069\u3045": "du",
    "\u3075\u3041": "fa", "\u3075\u3043": "fi", "\u3075\u3047": "fe", "\u3075\u3049": "fo",
    "\u3094\u3041": "va", "\u3094\u3043": "vi", "\u3094\u3047": "ve", "\u3094\u3049": "vo",
    "\u3057\u3047": "she", "\u3058\u3047": "je", "\u3061\u3047": "che",
    "\u3044\u3047": "ye", "\u3046\u3043": "wi", "\u3046\u3047": "we", "\u3046\u3049": "wo",
    "\u3064\u3041": "tsa", "\u3064\u3043": "tsi", "\u3064\u3047": "tse", "\u3064\u3049": "tso",
    "\u3059\u3043": "si", "\u305a\u3043": "zi", "\u304f\u3041": "kwa", "\u3050\u3041": "gwa",
}


def _kana_table() -> dict:
    table = dict(_KANA_BASE)
    for kana, romaji in _KANA_BASE.items():
        kata = "".join(chr(ord(c) + 0x60) if 0x3041 <= ord(c) <= 0x3096 else c for c in kana)
        table[kata] = romaji
    return table


_JA_ROMAJI = _kana_table()


def _fold_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\u3010[^\u3011]*\u3011", " ", s)
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
    s = re.sub(r"[^\w\s]", " ", s)
    s = s.replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


def _has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s or ""))


def _has_latin(s: str) -> bool:
    return bool(_LATIN_RE.search(s or ""))


def _scripts_cross(want: str, cand: str) -> bool:
    return ((_has_cjk(want) and _has_latin(cand))
            or (_has_latin(want) and _has_cjk(cand)))


def _romanize_ja(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    out = []
    i, n = 0, len(s)
    while i < n:
        pair = s[i:i + 2]
        if len(pair) == 2 and pair in _JA_ROMAJI:
            out.append(_JA_ROMAJI[pair])
            i += 2
            continue
        ch = s[i]
        if ch in ("\u3063", "\u30c3"):          # small tsu: double the next consonant
            nxt = _JA_ROMAJI.get(s[i + 1:i + 3]) or _JA_ROMAJI.get(s[i + 1:i + 2]) or ""
            if nxt.startswith("ch"):
                out.append("t")
            elif nxt and nxt[0] not in "aiueo":
                out.append(nxt[0])
            i += 1
            continue
        if ch == "\u30fc":                      # long vowel mark: repeat the vowel
            last = out[-1] if out else ""
            if last and last[-1] in "aiueo":
                out.append(last[-1])
            i += 1
            continue
        if ch in _JA_ROMAJI:
            out.append(_JA_ROMAJI[ch])
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _seq_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(
        None, (a or "").replace(" ", ""), (b or "").replace(" ", "")).ratio()


def _strip_artist_prefix(title: str, artist: str) -> str:
    t = (title or "").strip()
    a = _fold_text(artist)
    if not t or not a:
        return t
    for sep in (" - ", " \u2013 ", " \u2014 ", "|", ":", "\u2022"):
        if sep in t:
            head, tail = t.split(sep, 1)
            rest = tail.strip()
            if rest and _fold_text(head).startswith(a):
                return rest
    return t


def _title_aligned(want_title: str, want_artist: str, cand_title: str) -> float:
    ct = _strip_artist_prefix(cand_title, want_artist)
    wn = _fold_text(want_title)
    cn = _fold_text(ct)
    if not wn or not cn:
        return 0.0
    sw, sc = set(wn.split()), set(cn.split())
    jaccard = len(sw & sc) / len(sw | sc) if (sw | sc) else 0.0
    scores = [jaccard]
    short, long = (wn, cn) if len(wn) <= len(cn) else (cn, wn)
    if len(short) >= 4 and short in long:
        scores.append(min(1.0, jaccard + 0.2))
    if _has_cjk(wn) or _has_cjk(cn):
        scores.append(_seq_ratio(wn, cn))
    if _KANA_RE.search(want_title or "") or _KANA_RE.search(ct):
        scores.append(_seq_ratio(_fold_text(_romanize_ja(want_title or "")),
                                 _fold_text(_romanize_ja(ct))))
    return min(1.0, max(scores))


def _artist_aligned(want_artist: str, cand_artist: str) -> float:
    wa = _fold_text(want_artist)
    ca = _fold_text(cand_artist)
    if not wa or not ca:
        return 0.0
    wt = set(wa.split())
    scores = [len(wt & set(ca.split())) / len(wt) if wt else 0.0]
    if _KANA_RE.search(want_artist or "") or _KANA_RE.search(cand_artist or ""):
        scores.append(_seq_ratio(_fold_text(_romanize_ja(want_artist or "")),
                                 _fold_text(_romanize_ja(cand_artist or ""))))
    return min(1.0, max(scores))


def _aligned_candidate(want_title, want_artist, want_ms, cand: dict) -> dict:
    t = round(_title_aligned(want_title, want_artist, cand.get("title", "")), 3)
    a = round(_artist_aligned(want_artist, cand.get("artist", "")), 3)
    d = _duration_score(want_ms, cand.get("duration", ""))
    if d is not None:
        d = round(d, 3)
    if a > 0 and d is not None:
        score = 0.55 * t + 0.30 * a + 0.15 * d
    elif a > 0:
        score = 0.70 * t + 0.30 * a
    elif d is not None:
        score = 0.55 * t + 0.45 * d
    else:
        score = t
    return {**cand, "t": t, "a": a, "d": d, "score": round(score, 3)}


def _score_aligned_candidates(want_title, want_artist, want_ms, cands) -> list:
    out = [_aligned_candidate(want_title, want_artist, want_ms, c)
           for c in (cands or []) if isinstance(c, dict)]
    out.sort(key=lambda x: -x["score"])
    return out


def _query_variants(title: str, artist: str, album: str = "") -> list:
    title, artist, album = (title or "").strip(), (artist or "").strip(), (album or "").strip()
    out = [f"{title} {artist}".strip(), title, f"{artist} - {title}".strip()]
    romaji = _romanize_ja(title).strip()
    if romaji and romaji != title and not _has_cjk(romaji):
        out.append(f"{romaji} {artist}".strip())
        out.append(romaji)
    if album:
        out.append(f"{title} {artist} {album}".strip())
        out.append(f"{title} {album}".strip())
    seen, variants = set(), []
    for q in out:
        if q and q not in seen:
            seen.add(q)
            variants.append(q)
    return variants


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

def _supported(c: dict) -> bool:
    try:
        if float(c.get("a") or 0) > 0:
            return True
        d = c.get("d")
        return d is not None and float(d) > 0
    except Exception:
        return False


def _pick_best(query: str, want_title: str, want_artist: str, want_ms: int, cands: list,
               album: str = "") -> dict:
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
    scored = _prefer_edition(scored, album)
    best = scored[0]
    evidence = _supported(best)
    if evidence and best["score"] >= 0.85:
        return best
    if evidence and best["score"] >= 0.60 and (len(scored) == 1
                                               or (best["score"] - scored[1]["score"]) >= 0.08):
        return best
    second = scored[1]["score"] if len(scored) > 1 else -1.0
    if _has_cjk(want_title) or any(_has_cjk(c.get("title", "")) for c in scored):
        aligned = [c for c in _score_aligned_candidates(want_title, want_artist, want_ms, cands)
                   if c.get("title")]
        if aligned:
            top = aligned[0]
            margin = top["score"] - (aligned[1]["score"] if len(aligned) > 1 else 0.0)
            if (_supported(top) and top["score"] >= 0.72 and top["t"] >= 0.45
                    and (len(aligned) == 1 or margin >= 0.10)):
                return {**top, "confidence": "aligned"}
            if _scripts_cross(want_title, top.get("title", "")):
                raise RuntimeError(
                    f"no confident match (cross-script, not identified: best aligned "
                    f"{top['score']:.2f} '{top.get('title', '')}' | "
                    f"'{top.get('artist', '')}' — the rank-1 fallback is refused, "
                    f"not guessed) for: " + query)
    if not evidence:
        raise RuntimeError(
            f"no confident match (title only — no artist agreement and no usable "
            f"duration reading: '{best.get('title', '')}' | '{best.get('artist', '')}' "
            f"[{best.get('videoId', '')}], similarity {best['score']:.2f}) for: " + query)
    raise RuntimeError(
        f"no confident match (best {best['score']:.2f}: '{best.get('title', '')}' | "
        f"'{best.get('artist', '')}' [{best.get('videoId', '')}], "
        f"runner-up {second:.2f}) for: " + query)

def _card_candidate(sec: dict):
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


def _search_key(query: str, want_title: str, want_artist: str = "", want_ms: int = 0,
                album: str = "") -> tuple:
    return (query or "", _sig_label(_version_sig(want_title)),
            (want_artist or "").strip().lower(), int(want_ms or 0),
            _norm_text(album or ""), _sig_label(_edition_sig(album or "")))


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


def yt_search(query: str, title: str = "", artist: str = "", duration_ms: int = 0,
              album: str = "", tries: int = 3):
    want_title = (title or query or "").strip()
    want_artist = (artist or "").strip()
    want_ms = int(duration_ms or 0)
    tries = max(1, int(tries or 1))
    key = _search_key(query, want_title, want_artist, want_ms, album)
    hit = _search_cache_get(key)
    if hit is not None:
        best = _pick_best(query, want_title, want_artist, want_ms, hit, album=album)
        best.update({"query": query})
        return best
    body = json.dumps({"context": {"client": CLIENT}, "query": query,
                       "params": "EgWKAQIIAWoQEAMQBBAJEAkQBRAK"}).encode()
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                f"https://music.youtube.com/youtubei/v1/search?key={YT_KEY}&prettyPrint=false",
                data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
                                    "Origin": YT_ORIGIN, "Referer": YT_ORIGIN + "/"})
            r = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
            break
        except Exception as e:
            last = e
            if not _is_transport(e) or attempt == tries - 1:
                raise
            time.sleep(1.0 + attempt)
    else:
        raise last
    cands = _candidates_from_search_json(json.loads(r))
    _search_cache_put(key, cands)
    best = _pick_best(query, want_title, want_artist, want_ms, cands, album=album)
    best.update({"query": query})
    return best

def title_overlap(a: str, b: str) -> bool:
    wa = set(re.findall(r"[a-z0-9]{3,}", (a or "").lower()))
    wb = set(re.findall(r"[a-z0-9]{3,}", (b or "").lower()))
    return bool(wa & wb)

def yt_match(title: str, artist: str = "", duration_ms: int = 0, album: str = "") -> dict:
    title, artist = (title or "").strip(), (artist or "").strip()
    want_q = f"{title} {artist}".strip()
    last_err = ""
    for qi, q in enumerate(_query_variants(title, artist, album=album)):
        if not q:
            continue
        try:
            r = yt_search(q, title=title, artist=artist, duration_ms=int(duration_ms or 0),
                          album=album)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        if not r.get("videoId"):
            continue
        if r.get("best_guess"):
            last_err = last_err or "rank-1 guess refused"
            continue
        r["query"] = want_q
        r["via"] = q
        r["alt"] = qi > 0
        return {**{"ok": True}, **r}
    skip = (last_err[:220] if last_err else "no match on any variant")
    if "no confident match" not in skip and "no ytm result" not in skip:
        skip = "no match on any variant" + (f" ({skip})" if skip else "")
    return {"ok": True, "query": want_q, "videoId": None,
            "title": "", "duration": "", "skipped": skip[:220]}

SECOND_SEARCH_MAX = 8   # skipped rows one run may search again, a hard cap

def _retryable_skip(text) -> bool:
    low = (text or "").lower()
    if not low or "no confident match" in low or "no ytm result" in low:
        return False
    return _is_transport_text(low)

def _fatal_skip(text) -> bool:
    low = (text or "").lower()
    if "httperror" not in low and "forbidden" not in low and "unauthorized" not in low:
        return False
    return any(code in low for code in ("400", "401", "403", "404"))

def _second_search_skipped(items, max_rows: int = SECOND_SEARCH_MAX):
    out, attempted, stop = {}, [], ""
    cap = max(1, int(max_rows or 1))
    for it in items:
        if len(attempted) >= cap:
            stop = f"cap {cap}"
            break
        if not _retryable_skip(it.get("skipped")):
            continue
        pos = it.get("i")
        variants = _query_variants(it.get("title", ""), it.get("artist", ""),
                                   it.get("album", ""))
        q = variants[0] if variants else ""
        if not q:
            continue
        attempted.append(pos)
        try:
            r = yt_search(q, title=it.get("title", ""), artist=it.get("artist", ""),
                          duration_ms=int(it.get("duration_ms") or 0),
                          album=it.get("album", ""), tries=1)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            blog("research", f"row {pos} unresolved on a second search: {msg[:120]}")
            if _fatal_skip(msg) or _retryable_skip(msg):
                stop = msg[:120]
                break
            continue
        if r.get("videoId") and not r.get("best_guess"):
            out[str(pos)] = r
            blog("research", f"row {pos} resolved on a second search: {r.get('videoId')}")
    if attempted:
        blog("research", f"second search: {len(attempted)} skipped row(s) asked again, "
                         f"{len(out)} resolved" + (f", stopped: {stop}" if stop else ""))
    return out, attempted, stop

def yt_search_safe(query: str):
    try:
        r = yt_search(query, title=query)
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "query": query, "error": f"{type(e).__name__}: {e}"}

def yt_search_batch(items, workers: int = 8):
    from concurrent.futures import ThreadPoolExecutor
    workers = max(1, min(int(workers or 8), 10))

    def one(it):
        try:
            if isinstance(it, dict):
                return yt_match(it.get("title", ""), it.get("artist", ""), it.get("duration_ms", 0),
                                album=it.get("album", ""))
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

def save_settings(d: dict) -> bool:
    try:
        with open(SETTINGF, "w", encoding="utf-8") as f:
            json.dump({k: d[k] for k in _SETTING_KEYS if k in d}, f)
        return True
    except Exception as e:
        blog("settings", f"write failed: {type(e).__name__}")
        return False

def _settings_payload(cur: dict) -> dict:
    out = {}
    for k in _SETTING_KEYS:
        if k == "privacy":
            out[k] = cur.get(k) or "PRIVATE"
        else:
            out[k] = cur.get(k) or ""
    return out

def clear_created(spotify_id: str = "") -> bool:
    if spotify_id:
        created = load_created()          # RecordUnreadable propagates: nothing is removed
        if created.pop(spotify_id, None) is None:
            return True
        if not save_created(created):
            blog("reset", f"{spotify_id[:40]} record not rewritten - it is unchanged")
            return False
        return True
    try:
        os.remove(CREATEDF)
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        blog("reset", f"created record not removed: {type(e).__name__}")
        return False

class RecordUnreadable(RuntimeError):
    """created.json exists but cannot be read or parsed. Distinct from an empty record:
    a run that mistook this for "no record" would create a second playlist over the
    record it could not read, and then overwrite it."""


class FatalAppend(RuntimeError):

    def __init__(self, status: str, message: str, outcomes=None):
        super().__init__(message)
        self.status = str(status or "")
        self.outcomes = {}
        for k, v in (outcomes or {}).items():
            try:
                self.outcomes[int(k)] = str(v or "")
            except (TypeError, ValueError):
                continue


def _raise_outcome(msg: str, attempts) -> None:
    fatal = sorted({str(s) for s in (attempts or {}).values() if is_fatal_status(s)})
    if fatal:
        raise FatalAppend(fatal[0], f"YouTube Music refused the append with {fatal[0]} — {msg}",
                          outcomes=attempts)
    raise RuntimeError(msg)


RECORD_VERSION = 2

_LEGACY_UNRESUMABLE = (
    "this playlist was started by an older version of the app, whose record cannot say "
    "which copies it had already written: that version cleared its queue before the "
    "playlist read proved the writes. Writing from here could add a second copy of a song "
    "that is already there, so nothing was sent and nothing on the playlist was changed. "
    "The playlist is {url} — {gap}. Check that playlist in YouTube Music and add whatever "
    "is missing by hand; this app will not write to it while its record cannot prove what "
    "was already written.")


def _legacy_safe(rec, plid, yt_cookie) -> tuple:
    plan = [v for v in (rec.get("video_ids") or []) if isinstance(v, str) and v]
    if not plan:
        return False, "its record does not name the copies it was sent"
    try:
        read = yt_playlist_read(plid, yt_cookie)
    except Exception as e:
        return False, f"its contents could not be read ({type(e).__name__})"
    entries, complete = read.get("entries"), bool(read.get("complete"))
    if entries is None:
        return False, "its contents could not be read"
    if not complete:
        return False, (f"a read of it stopped after {len(entries)} entries, so it proves "
                       f"nothing about the rest")
    gap = sum(1 for e in _seed_ledger(plan, entries) if e["outcome"] != "landed")
    if gap:
        return False, (f"a complete read of it holds {len(entries)} entries, and {gap} of "
                       f"the {len(plan)} copies this record asks for are not in it")
    return True, (f"a complete read of it holds all {len(plan)} copies this record asks "
                  f"for")


def load_created() -> dict:
    try:
        with open(CREATEDF, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        raise RecordUnreadable(
            f"the resume record {CREATEDF} exists but cannot be read "
            f"({type(e).__name__}: {e}), so this app cannot tell whether a playlist was "
            f"already made for this run. Nothing was created or changed, and the file is "
            f"left exactly as it is: copy it somewhere safe before repairing it, check your "
            f"YouTube Music library for a playlist from this run, and add what is missing by "
            f"hand. This app will not create or overwrite a playlist over a record it cannot "
            f"read"
        ) from e
    if not isinstance(d, dict):
        raise RecordUnreadable(
            f"the resume record {CREATEDF} is not a playlist record, so this app cannot tell "
            f"whether a playlist was already made for this run. Nothing was created or "
            f"changed, and the file is left exactly as it is: copy it somewhere safe before "
            f"repairing it, check your YouTube Music library for a playlist from this run, "
            f"and add what is missing by hand. This app will not create or overwrite a "
            f"playlist over a record it cannot read")
    return d

def save_created(d: dict) -> bool:
    tmp = CREATEDF + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CREATEDF)
        return True
    except Exception as e:
        blog("created", f"save failed {type(e).__name__}: {e}")
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False

def vl_id(plid: str) -> str:
    plid = (plid or "").strip()
    return plid if plid.startswith("VL") else ("VL" + plid if plid else "")

def yt_headers(yt_cookie: str) -> dict:
    yt_cookie = normalize_cookie(yt_cookie)
    return {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
            "Origin": YT_ORIGIN, "Referer": YT_ORIGIN + "/", "Cookie": yt_cookie,
            "Authorization": sapisid_hash(yt_cookie), "X-Origin": YT_ORIGIN}

CREATE_INLINE_MAX = 50  # playlist/create carries at most this many videoIds inline
TRANSFER_SLICE = 100    # source copies sent, then proved by one read-back, before the next slice
PLAYLIST_PAGE_MAX = 200  # browse pages per read: ~100-entry pages cover 20k tracks, well past the README's 5k
PLAYLIST_PAGE_TRIES = 2  # one retry per continuation page before the read is called short

def _shelf(node):
    if isinstance(node, dict):
        for k in ("musicPlaylistShelfRenderer", "musicPlaylistShelfContinuation"):
            if k in node:
                return node[k]
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

def _continuation_command_token(node) -> str:
    if isinstance(node, dict):
        cmd = node.get("continuationCommand")
        if isinstance(cmd, dict) and cmd.get("token"):
            return str(cmd["token"])
        for v in node.values():
            t = _continuation_command_token(v)
            if t:
                return t
    elif isinstance(node, list):
        for v in node:
            t = _continuation_command_token(v)
            if t:
                return t
    return ""

def _continuation_items(node):
    if isinstance(node, dict):
        act = node.get("appendContinuationItemsAction")
        if isinstance(act, dict) and isinstance(act.get("continuationItems"), list):
            return act["continuationItems"]
        for v in node.values():
            r = _continuation_items(v)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _continuation_items(v)
            if r is not None:
                return r
    return None

def _playlist_page(j) -> tuple:
    shelf = _shelf(j)
    if shelf is not None:
        return list(shelf.get("contents") or []), list(shelf.get("continuations") or []), True
    items = _continuation_items(j)
    if items is not None:
        return items, [], True
    return [], [], False

def _next_page_token(continuations, items) -> str:
    for c in continuations or []:
        d = c.get("nextContinuationData") if isinstance(c, dict) else None
        if isinstance(d, dict) and d.get("continuation"):
            return str(d["continuation"])
    for item in items or []:
        if isinstance(item, dict) and "continuationItemRenderer" in item:
            t = _continuation_command_token(item)
            if t:
                return t
    for item in items or []:
        t = _continuation_command_token(item)
        if t:
            return t
    return ""

def _browse_playlist(body: dict, yt_cookie: str):
    req = urllib.request.Request(
        f"https://music.youtube.com/youtubei/v1/browse?key={YT_KEY}&prettyPrint=false",
        data=json.dumps(body).encode(), headers=yt_headers(yt_cookie))
    return json.loads(urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore"))

def yt_playlist_read(plid: str, yt_cookie: str) -> dict:
    out, seen, token, last = [], set(), "", None
    for _page in range(PLAYLIST_PAGE_MAX):
        body = {"context": {"client": CLIENT}}
        if token:
            body["continuation"] = token
        else:
            body["browseId"] = vl_id(plid)
        j = None
        for attempt in range(PLAYLIST_PAGE_TRIES):
            try:
                j = _browse_playlist(body, yt_cookie)
                break
            except Exception as e:
                last = e
                if not token:
                    break  # nothing proven yet: the caller's retry loop owns the whole read
                if attempt < PLAYLIST_PAGE_TRIES - 1:
                    _pause(1.0)
        if j is None:
            why = f"{type(last).__name__} {getattr(last, 'code', '')}".strip()
            if not token:
                return {"entries": None, "complete": False, "why": why}
            return {"entries": out, "complete": False,
                    "why": f"continuation failed after {len(out)} entries ({why})"}
        items, continuations, found = _playlist_page(j)
        if not found:
            if not token:
                return {"entries": None, "complete": False, "why": "no playlist rows in reply"}
            return {"entries": out, "complete": False,
                    "why": f"no playlist rows in continuation after {len(out)} entries"}
        for item in items:
            if not isinstance(item, dict):
                return {"entries": out, "complete": False,
                        "why": f"item that is not an object after {len(out)} entries"}
            if "continuationItemRenderer" in item:
                continue  # the page marker, not a playlist row
            mrl = item.get("musicResponsiveListItemRenderer")
            if not isinstance(mrl, dict):
                shape = next(iter(item), "empty item")
                return {"entries": out, "complete": False,
                        "why": f"unrecognised row renderer {shape} after {len(out)} entries"}
            v = mrl_video_id(mrl)
            if not v:
                return {"entries": out, "complete": False,
                        "why": f"row without a parsable videoId after {len(out)} entries"}
            out.append(v)
        nxt = _next_page_token(continuations, items)
        if not nxt:
            if continuations or any(isinstance(i, dict) and "continuationItemRenderer" in i
                                    for i in items):
                return {"entries": out, "complete": False,
                        "why": f"unrecognised continuation shape after {len(out)} entries"}
            return {"entries": out, "complete": True, "why": ""}
        if nxt in seen:
            return {"entries": out, "complete": False,
                    "why": f"continuation token repeated after {len(out)} entries"}
        seen.add(nxt)
        token = nxt
    return {"entries": out, "complete": False,
            "why": f"page cap {PLAYLIST_PAGE_MAX} reached after {len(out)} entries"}

def yt_playlist_entries(plid: str, yt_cookie: str):
    return yt_playlist_read(plid, yt_cookie)["entries"]

def _is_transport_text(s: str) -> bool:
    t = (s or "").lower()
    return any(h in t for h in _TRANSPORT_HINTS)

STATUS_SUCCEEDED = "STATUS_SUCCEEDED"
STATUS_DUPLICATE = "STATUS_DUPLICATE"   # a second copy of an id the playlist holds
STATUS_UNSURE = "STATUS_UNSURE"
_FATAL_HTTP = ("400", "401", "403", "404")  # a verdict, not a hiccup
_DUPLICATE_TOAST = "already in the playlist"

def is_unsure_status(st: str) -> bool:
    return str(st or "").startswith(STATUS_UNSURE)

def is_fatal_status(st: str) -> bool:
    last = str(st or "").strip().rsplit(" ", 1)[-1]
    return last in _FATAL_HTTP

def _retryable_error(err: Exception) -> bool:
    code = getattr(err, "code", None)
    if isinstance(code, int):
        return code == 429 or code >= 500
    return _is_transport(err)

def _is_duplicate_reply(body: str, status: str) -> bool:
    return ("FAILED" in (status or "").upper()
            and _DUPLICATE_TOAST in (body or "").lower())


def yt_add_one(plid: str, vid: str, yt_cookie: str) -> str:
    act = {"action": "ACTION_ADD_VIDEO", "addedVideoId": vid,
           "dedupeOption": "DEDUPE_OPTION_SKIP"}
    b2 = json.dumps({"context": {"client": CLIENT}, "playlistId": plid,
                     "actions": [act]}).encode()

    def once():
        try:
            er = urllib.request.urlopen(urllib.request.Request(
                f"https://music.youtube.com/youtubei/v1/browse/edit_playlist?key={YT_KEY}&prettyPrint=false",
                data=b2, headers=yt_headers(yt_cookie)), timeout=25).read().decode("utf-8", "ignore")
        except Exception as e:
            code = getattr(e, "code", None)
            st = f"{type(e).__name__} {getattr(e, 'code', '')}".strip()
            if isinstance(code, int) and str(code) in _FATAL_HTTP:
                return st, "", False   # a verdict: is_fatal_status reads the code
            if _retryable_error(e) or _is_transport_text(st) or code is None:
                return st, "", True
            return st, "", True
        try:
            return str(json.loads(er).get("status", "")), er, False
        except Exception:
            return er[:120], er, False

    st, body, unsure = once()
    if unsure:
        blog("add", f"{vid} outcome unknown after {st[:60]} — not resending")
        return f"{STATUS_UNSURE} {st}".strip()
    if _is_duplicate_reply(body, st):
        return STATUS_DUPLICATE
    return st

COVER_MAX = 8 * 1024 * 1024  # playlist thumbnail bytes accepted for upload

def yt_set_playlist_cover(plid: str, cover_url: str, yt_cookie: str = "") -> dict:
    if not (cover_url or "").strip():
        return {"ok": False, "why": "no cover"}
    try:
        req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            if int(getattr(r, "status", 200) or 0) != 200:
                return {"ok": False, "why": "download"}
            img = r.read(COVER_MAX + 1)
        if not img or len(img) > COVER_MAX:
            return {"ok": False, "why": "download"}
        if img.startswith(b"\xff\xd8\xff"):
            ctype = "image/jpeg"
        elif img.startswith(b"\x89PNG\r\n\x1a\n"):
            ctype = "image/png"
        else:
            return {"ok": False, "why": "no image"}
    except Exception:
        return {"ok": False, "why": "download"}
    try:
        hs = yt_headers(yt_cookie)
        hs.update({"Content-Type": "text/plain; charset=utf-8",
                   "X-Goog-Upload-Command": "start",
                   "X-Goog-Upload-Header-Content-Length": str(len(img)),
                   "X-Goog-Upload-Protocol": "resumable",
                   "X-Goog-AuthUser": "0"})
        hr = urllib.request.urlopen(urllib.request.Request(
            f"{YT_ORIGIN}/playlist_image_upload/playlist_custom_thumbnail?authuser=0",
            data=b"", headers=hs), timeout=25)
        up_url = (hr.headers.get("X-Goog-Upload-URL") or "") if hr.headers is not None else ""
        if not up_url:
            return {"ok": False, "why": "upload"}
        uh = {h: v for h, v in yt_headers(yt_cookie).items() if h != "Content-Type"}
        uh.update({"Content-Type": ctype, "X-Goog-Upload-Command": "upload, finalize",
                   "X-Goog-Upload-Offset": "0"})
        ur = urllib.request.urlopen(urllib.request.Request(
            up_url, data=img, headers=uh), timeout=25)
        j = json.loads(ur.read().decode("utf-8", "ignore"))
        blob = str(j.get("encryptedBlobId") or j.get("playlistScottyEncryptedBlobId") or "")
        if not blob:
            return {"ok": False, "why": "upload"}
    except Exception:
        return {"ok": False, "why": "upload"}
    try:
        body = json.dumps({"context": {"client": CLIENT}, "playlistId": plid, "actions": [
            {"action": "ACTION_SET_CUSTOM_THUMBNAIL", "addedCustomThumbnail": {
                "imageKey": {"type": "PLAYLIST_IMAGE_TYPE_CUSTOM_THUMBNAIL",
                             "name": "studio_square_thumbnail"},
                "playlistScottyEncryptedBlobId": blob}}]}).encode()
        eh = yt_headers(yt_cookie)
        eh["X-Goog-AuthUser"] = "0"
        er = urllib.request.urlopen(urllib.request.Request(
            f"{YT_ORIGIN}/youtubei/v1/browse/edit_playlist?key={YT_KEY}&prettyPrint=false",
            data=body, headers=eh), timeout=25)
        txt = er.read().decode("utf-8", "ignore")
        if int(getattr(er, "status", 200) or 0) not in range(200, 300) or '"error"' in txt:
            return {"ok": False, "why": "attach"}
        ej = json.loads(txt)
        if isinstance(ej, dict) and "status" in ej and "SUCCEEDED" not in str(ej.get("status", "")):
            return {"ok": False, "why": "attach"}
    except Exception:
        return {"ok": False, "why": "attach"}
    return {"ok": True, "why": ""}

def _repeat_count(video_ids: list, version_sigs=None) -> int:
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
    out = []
    try:
        for k, row in enumerate(rows or []):
            if isinstance(row, dict) and row.get("videoId") == video_id:
                out.append(k)
    except Exception:
        pass
    return out

def _ledger(video_ids: list, entries, attempts: dict) -> list:
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

def _persist_fields(spotify_id, **fields) -> bool:
    if not spotify_id:
        return True
    try:
        created = load_created()
        rec = created.get(spotify_id)
        if not isinstance(rec, dict):
            rec = {}
        if "expected_min" in fields:
            try:
                fields["expected_min"] = max(int(rec.get("expected_min") or 0),
                                             int(fields["expected_min"] or 0))
            except Exception:
                fields.pop("expected_min", None)
        if "unsure" in fields:
            try:
                merged = {int(p) for p in (rec.get("unsure") or [])}
                merged |= {int(p) for p in (fields["unsure"] or [])}
                fields["unsure"] = sorted(merged)
            except Exception:
                fields.pop("unsure", None)
        if "unsure_ids" in fields:
            try:
                merged = {str(v) for v in (rec.get("unsure_ids") or []) if v}
                merged |= {str(v) for v in (fields["unsure_ids"] or []) if v}
                fields["unsure_ids"] = sorted(merged)
            except Exception:
                fields.pop("unsure_ids", None)
        if "unsure_need" in fields:
            try:
                merged = {str(k): int(v or 0)
                          for k, v in (rec.get("unsure_need") or {}).items()}
                for k, v in (fields["unsure_need"] or {}).items():
                    merged[str(k)] = max(int(merged.get(str(k), 0)), int(v or 0))
                fields["unsure_need"] = merged
            except Exception:
                fields.pop("unsure_need", None)
        if "inflight_need" in fields:
            try:
                merged = {str(k): int(v or 0)
                          for k, v in (rec.get("inflight_need") or {}).items()}
                for k, v in (fields["inflight_need"] or {}).items():
                    merged[str(k)] = max(int(merged.get(str(k), 0)), int(v or 0))
                fields["inflight_need"] = merged
            except Exception:
                fields.pop("inflight_need", None)
        rec.update(fields)
        created[spotify_id] = rec
        return save_created(created)
    except Exception as e:
        blog("created", f"record failed {type(e).__name__}: {e}")
        return False

def _record(spotify_id) -> dict:
    if not spotify_id:
        return {}
    rec = load_created().get(spotify_id)
    return rec if isinstance(rec, dict) else {}

def _write_ahead(spotify_id, positions, floor, plan=None) -> bool:
    if not spotify_id:
        return True
    try:
        queued = {int(p) for p in (_record(spotify_id).get("inflight") or [])}
    except Exception:
        queued = set()
    queued |= {int(p) for p in (positions or [])}
    fields = dict(inflight=sorted(queued), expected_min=int(floor or 0), complete=False)
    if plan is not None:
        plan_ids = [v for v in plan if isinstance(v, str) and v]
        fields["video_ids"] = plan_ids
        queued_ids = [plan_ids[p] for p in sorted(queued) if 0 <= p < len(plan_ids)]
        fields["inflight_need"] = {v: plan_ids.count(v) for v in queued_ids}
    return _persist_fields(spotify_id, **fields)

def _note_outcomes(spotify_id, sent, unclear, floor, plan=None) -> bool:
    if not spotify_id:
        return True
    try:
        queued = {int(p) for p in (_record(spotify_id).get("inflight") or [])}
    except Exception:
        queued = set()
    queued -= {int(p) for p in (sent or [])}
    fields = dict(inflight=sorted(queued), unsure=[int(p) for p in (unclear or [])],
                  expected_min=int(floor or 0))
    if plan is not None:
        plan = list(plan)
        fields["unsure_ids"] = [plan[int(p)] for p in (unclear or [])
                                if 0 <= int(p) < len(plan)]
        need = {}
        for v in (fields["unsure_ids"] or []):
            need[v] = plan.count(v)
        fields["unsure_need"] = need
    return _persist_fields(spotify_id, **fields)

def _unknown_ids_in(rec) -> set:
    out = {str(v) for v in ((rec or {}).get("unsure_ids") or []) if v}
    saved = [v for v in ((rec or {}).get("video_ids") or []) if isinstance(v, str) and v]
    positions = list((rec or {}).get("unsure") or []) + list((rec or {}).get("inflight") or [])
    if not positions:
        return out
    if not saved:
        raise RecordUnreadable(
            "the resume record names copies whose outcome is unknown but not the plan they "
            "belong to, so this app cannot tell which songs they are — nothing was sent, and "
            "the record is left exactly as it is. Check the playlist in YouTube Music and add "
            "what is missing by hand.")
    for p in positions:
        try:
            i = int(p)
        except Exception:
            continue
        if 0 <= i < len(saved):
            out.add(saved[i])
    return out

def _unknown_need(rec) -> dict:
    out = {}
    for src in ("unsure_need", "inflight_need"):
        for k, v in (((rec or {}).get(src) or {}) or {}).items():
            try:
                out[str(k)] = max(int(out.get(str(k), 0) or 0), max(0, int(v or 0)))
            except Exception:
                continue
    return out

def _unresolved_ids(unknown_ids, entries, need=None) -> set:
    need = dict(need or {})
    have = {}
    for v in entries or []:
        have[v] = have.get(v, 0) + 1
    keep = set()
    for i in unknown_ids or ():
        try:
            must = int(need.get(i, 0) or 0)
        except Exception:
            must = 0
        if must <= 0 or have.get(i, 0) < must:
            keep.add(i)
    return keep

_NO_PLAN_MESSAGE = (
    "nothing was sent and nothing on {url} was changed: this interrupted run's record does "
    "not name the copies it was sent, so the app cannot tell which songs its unproven "
    "writes were about. Guessing would add a second copy of a song that is already there. "
    "Check that playlist in YouTube Music and add whatever is missing by hand; this app "
    "will not write to it while its record cannot name those copies.")

def _incomplete_record(spotify_id: str):
    if not spotify_id:
        return None
    rec = load_created().get(spotify_id)
    if isinstance(rec, dict) and rec.get("yt_id") and rec.get("complete") is not True:
        return rec
    return None

def _persisted_unknown(rec, intended) -> tuple:
    try:
        floor = max(0, int((rec or {}).get("expected_min") or 0))
    except Exception:
        floor = 0
    intended = list(intended or [])
    unknown_ids = _unknown_ids_in(rec)
    need = _unknown_need(rec)
    for v in unknown_ids:
        need.setdefault(v, 0)
    if not unknown_ids:
        return floor, set(), set(), need
    return floor, {i for i, v in enumerate(intended) if v in unknown_ids}, unknown_ids, need

def _set_unknown(spotify_id, plan, unknown_ids, floor, need=None) -> bool:
    if not spotify_id:
        return True
    plan = [v for v in (plan or []) if isinstance(v, str) and v]
    keep = {str(v) for v in (unknown_ids or []) if v}
    created = load_created()
    rec = created.get(spotify_id)
    rec = rec if isinstance(rec, dict) else {}
    old_need = _unknown_need(rec)
    merged = {v: max(int(old_need.get(v, 0) or 0),
                     int((need or {}).get(v, 0) or 0)) for v in keep}
    created[spotify_id] = {
        **rec,
        "unsure": sorted({i for i, v in enumerate(plan) if v in keep}),
        "unsure_ids": sorted(keep),
        "unsure_need": {k: int(v) for k, v in merged.items() if int(v) > 0},
        "inflight": [],
        "expected_min": max(int(rec.get("expected_min") or 0), int(floor or 0)),
    }
    return save_created(created)

def _adopt_plan(spotify_id, plan, slots, guesses, floor, unknown, unknown_ids,
                need=None) -> bool:
    if not spotify_id:
        return True
    created = load_created()
    rec = created.get(spotify_id)
    rec = rec if isinstance(rec, dict) else {}
    plan = [v for v in plan if isinstance(v, str) and v]
    slots = [v if isinstance(v, str) else "" for v in (slots or [])]
    if not slots or [v for v in slots if v] != plan:
        slots = list(plan)
    kept = {str(v) for v in (unknown_ids or []) if v}
    need = dict(need or {})
    merged_need = {str(k): int(v or 0)
                   for k, v in (rec.get("unsure_need") or {}).items()}
    for k, v in need.items():
        if int(v or 0) > 0:
            merged_need[str(k)] = max(int(merged_need.get(str(k), 0)), int(v))
    created[spotify_id] = {
        **rec,
        "video_ids": plan,
        "plan_slots": slots,
        "plan_guesses": {str(k): str(v)[:80] for k, v in (guesses or {}).items()},
        "unsure": sorted({int(p) for p in (unknown or [])}),
        "unsure_ids": sorted(kept),
        "unsure_need": {str(k): int(v) for k, v in merged_need.items() if int(v) > 0},
        "inflight": [],
        "expected_min": max(int(rec.get("expected_min") or 0), int(floor or 0)),
    }
    return save_created(created)

def _resume_plan(rec, n_rows: int):
    if not isinstance(rec, dict):
        return None
    ids = [v for v in (rec.get("video_ids") or []) if isinstance(v, str) and v]
    if not ids or not n_rows:
        return None
    slots = rec.get("plan_slots")
    if not isinstance(slots, list) and len(ids) == int(n_rows):
        slots = ids
    if not isinstance(slots, list) or len(slots) != int(n_rows):
        return None
    slots = [v if isinstance(v, str) else "" for v in slots]
    if [v for v in slots if v] != ids:
        return None
    guesses = {}
    for k, why in (rec.get("plan_guesses") or {}).items():
        try:
            guesses[int(k)] = str(why or "cross-script")[:80]
        except Exception:
            continue
    return slots, guesses

def _persist_incomplete(spotify_id, plid, url, title, expected, plan=None, plan_slots=None,
                        plan_guesses=None, expected_min=0, unsure=None,
                        unsure_ids=None, unsure_need=None) -> bool:
    fields = dict(yt_id=plid, url=url, title=title, expected=expected, complete=False,
                  create_pending=False, unsure=[], inflight=[],
                  expected_min=int(expected_min or 0))
    if plan is not None:
        fields["video_ids"] = [v for v in plan if isinstance(v, str) and v]
        fields["plan_slots"] = [v if isinstance(v, str) else "" for v in (plan_slots or [])]
        fields["plan_guesses"] = {str(k): str(v)[:80]
                                  for k, v in (plan_guesses or {}).items()}
    if not spotify_id:
        return True
    fields["record_version"] = RECORD_VERSION
    fields["unsure"] = sorted({int(p) for p in (unsure or [])})
    fields["unsure_ids"] = sorted({str(v) for v in (unsure_ids or []) if v})
    fields["unsure_need"] = {str(k): int(v or 0)
                             for k, v in (unsure_need or {}).items()}
    fields["inflight"] = []
    fields["expected_min"] = int(expected_min or 0)
    created = load_created()
    prev = created.get(spotify_id)
    created[spotify_id] = {**(prev if isinstance(prev, dict) else {}), **fields}
    return save_created(created)

def _persist_final(spotify_id, plid, url, title, expected, complete):
    _persist_fields(spotify_id, yt_id=plid, url=url, title=title,
                    expected=expected, complete=bool(complete), create_pending=False)

def _persist_progress(spotify_id, plid, url, title, expected, state, attempt,
                      next_run_at=0.0, pending=None, plan=None, expected_min=None,
                      unsure=None):
    fields = dict(yt_id=plid, url=url or "", title=title,
                  expected=expected, state=state, attempt=int(attempt or 0),
                  next_run_at=float(next_run_at or 0), pending=list(pending or []))
    if plan is not None:
        fields["video_ids"] = [v for v in plan if isinstance(v, str) and v]
    if expected_min is not None:
        fields["expected_min"] = int(expected_min or 0)
    if unsure is not None:
        positions = sorted({int(p) for p in unsure})
        fields["unsure"] = positions
        if plan is not None:
            slots = [v for v in plan if isinstance(v, str) and v]
            ids = [slots[p] for p in positions if 0 <= p < len(slots)]
            fields["unsure_ids"] = ids
            fields["unsure_need"] = {v: slots.count(v) for v in ids}
    _persist_fields(spotify_id, **fields)

def _honour_retry_clock(rec, reason="waiting out the paused retry time") -> float:
    try:
        nxt = float((rec or {}).get("next_run_at", 0) or 0)
    except Exception:
        return 0.0
    left = nxt - _now()
    if left <= 0:
        return 0.0
    run_set(state="standby", attempt=int((rec or {}).get("attempt", 0) or 0),
            next_attempt_at=nxt, reason=reason,
            residual=list((rec or {}).get("pending") or []))
    blog("standby", f"resume waits {left:.0f}s for the retry time the run persisted")
    _standby_wait(left)
    return left

def _read_back_retry(plid, yt_cookie, tries: int = 5, minimum: int = 0):
    best, best_complete, seen = None, False, set()
    for a in range(max(1, tries)):
        try:
            read = yt_playlist_read(plid, yt_cookie)
            entries, complete = read["entries"], bool(read["complete"])
        except Exception:
            entries, complete = None, False
        if entries is not None:
            if complete and (not minimum or len(entries) >= minimum):
                return entries, True
            if best is None or len(entries) > len(best):
                best, best_complete = entries, complete
            seen.add(len(entries))
            if not complete:
                break
        if a < tries - 1:
            _pause(1.5)
    if best is not None and len(seen) > 1:
        blog("readback", f"{plid} short reads {sorted(seen)} settled longest={len(best)}")
    if best is not None and not best_complete:
        blog("readback", f"{plid} incomplete read {len(best)} entries")
    short = bool(minimum) and best is not None and best_complete and len(best) < minimum
    if short:
        blog("readback", f"{plid} complete read {len(best)} entries is below the {minimum} expected")
    return best, best_complete and not short

def _reconcile(video_ids, entries, attempts: dict, complete: bool = True) -> list:
    left = {}
    for v in entries or []:
        left[v] = left.get(v, 0) + 1
    out = []
    for i, v in enumerate(video_ids):
        if left.get(v, 0) > 0:
            left[v] -= 1
            out.append({"i": i + 1, "videoId": v, "outcome": "landed", "status": ""})
            continue
        st = (attempts.get(i, "") or "")
        if is_unsure_status(st):
            out.append({"i": i + 1, "videoId": v, "outcome": "missing", "unverified": True,
                        "unsure": True,
                        "status": ("outcome unknown — check the playlist, then add by "
                                   "hand; not re-sent")[:80]})
            continue
        if is_fatal_status(st):
            out.append({"i": i + 1, "videoId": v, "outcome": "rejected", "status": st[:80]})
            continue
        if not complete:
            out.append({"i": i + 1, "videoId": v, "outcome": "missing", "unverified": True,
                        "status": (f"unverified — read-back stopped after {len(entries or [])} "
                                   f"of {len(video_ids)} entries")[:80]})
            continue
        if "DUPLICATE" in st.upper():
            out.append({"i": i + 1, "videoId": v, "outcome": "duplicate",
                        "status": "already on the playlist — a second copy cannot be added"})
        elif "FAILED" in st.upper() or is_fatal_status(st):
            out.append({"i": i + 1, "videoId": v, "outcome": "rejected", "status": st[:80]})
        elif st:
            out.append({"i": i + 1, "videoId": v, "outcome": "missing",
                        "status": (st[:80] + " — absent at read-back")[:80]})
        else:
            out.append({"i": i + 1, "videoId": v, "outcome": "missing", "status": ""})
    return out

def _done_summary(counts, n_pl, topped_up=None, sent=None, guessed=0, tail_unverified=0,
                  order_differs=False, unnameable=0):
    c = dict(counts or {})
    added = int(c.get("added", 0) or 0)
    unique = int(c.get("unique", 0) or 0)
    skipped = int(c.get("skipped", 0) or 0)
    dup = int(c.get("duplicate", 0) or 0)
    unverified = int(c.get("unverified", 0) or 0)
    refused = int(c.get("refused", 0) or 0)
    source = int(c.get("source", 0) or 0)
    n = int(n_pl or 0)
    line = (f"landed {added} of {source} \u00b7 {unique} unique \u00b7 {skipped} skipped "
            f"\u00b7 {dup} duplicate \u00b7 {unverified} unverified \u00b7 {refused} refused")
    aside = []
    if int(guessed or 0) > 0:
        aside.append(f"best guess {int(guessed)}")
    if topped_up is not None:
        aside.append(f"topped up {int(topped_up)} of {int(sent or 0)}")
    if order_differs:
        aside.append("playlist order differs from the source (repaired copies were "
                     "appended after later ones)")
    if added + skipped + dup + unverified + refused != source:
        aside.append(f"counts do not reconcile "
                     f"({added}+{skipped}+{dup}+{unverified}+{refused}!={source}) "
                     f"— playlist holds {n}")
        return line + " \u00b7 " + " \u00b7 ".join(aside)
    if int(tail_unverified or 0) > 0:
        return line + " \u00b7 " + " \u00b7 ".join(aside + [
            f"playlist read stopped at {n} entries, so the last {int(tail_unverified)} are "
            f"unverified (resume re-verifies, no re-transfer)"])
    if added == n:
        return line + " \u00b7 " + " \u00b7 ".join(aside + [
            f"verified against the playlist (holds {n})"])
    if added < n:
        why = ("writes this run made that no read names" if int(unnameable or 0) > 0
               else "outside adds?")
        return line + " \u00b7 " + " \u00b7 ".join(aside + [
            f"playlist holds {n} — {n - added} entries not attributed to this run "
            f"({why}) — refused rows proven absent from that read-back"])
    return line + " \u00b7 " + " \u00b7 ".join(aside + [
        f"playlist holds {n} — read-back short of {added} landed "
        f"(resume re-verifies, no re-transfer)"])

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
        if is_unsure_status(st) or is_fatal_status(st):
            blog("add", f"stopped after {idx + 1}/{len(video_ids)}: {st[:60]}")
            break
    return attempts


def _chunks(seq, size):
    seq = list(seq or [])
    size = max(1, int(size or 1))
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _frozen_positions(intended, frozen) -> set:
    base = set()
    for p in frozen or ():
        try:
            base.add(int(p))
        except Exception:
            continue
    ids = {intended[p] for p in base if 0 <= p < len(intended or [])}
    return {i for i, v in enumerate(intended or []) if i in base or v in ids}


def _copies_that_should_land(statuses) -> int:
    n = 0
    for st in (statuses or {}).values():
        s = str(st or "").upper()
        if not s or "FAILED" in s or "DUPLICATE" in s:
            continue
        if is_unsure_status(s) or is_fatal_status(s):
            continue
        n += 1
    return n



def _append_verify(plid, video_ids, todo, yt_cookie, hooks, entries, complete, floor=None,
                   spotify_id=""):
    attempts = {}
    on_verified = (hooks or {}).get("on_verified")
    on_phase = (hooks or {}).get("on_phase")
    todo = [int(i) for i in (todo or [])]
    floor = max(0, int(floor or 0))
    if entries is not None:
        floor = max(floor, len(entries))
    expected_min = floor
    for chunk in _chunks(todo, TRANSFER_SLICE):
        if entries is not None and not complete:
            break
        if on_phase:
            try:
                on_phase("append")
            except Exception:
                pass
        if not _write_ahead(spotify_id, chunk, expected_min, plan=video_ids):
            raise RuntimeError(
                "the resume record could not be written to disk, so the transfer stopped "
                "before sending anything further — check that the app data folder is "
                "writable, then start the transfer again")
        held_before = {}
        for v in entries or []:
            held_before[v] = held_before.get(v, 0) + 1
        sent = _append_todo(plid, video_ids, chunk, yt_cookie, hooks)
        attempts.update(sent)
        stopped = any(is_unsure_status(st) or is_fatal_status(st)
                      for st in sent.values())
        expected = floor + _copies_that_should_land(sent)
        expected_min = max(expected_min, expected)
        unclear = sorted({int(p) for p, st in sent.items() if is_unsure_status(st)})
        if not _note_outcomes(spotify_id, [], unclear, expected_min, plan=video_ids):
            raise RuntimeError(
                "the resume record could not be written to disk, so the transfer stopped "
                "after the append it could not record — check that the app data folder is "
                "writable, then check the playlist before starting the transfer again")
        if on_phase:
            try:
                on_phase("verify")
            except Exception:
                pass
        got, got_complete = _read_back_retry(plid, yt_cookie, minimum=expected)
        if got is None:
            complete = False
        else:
            entries, complete = got, got_complete
            floor = max(floor, len(got))
        blog("slice", f"{plid} sent={len(sent)} expected={expected} "
                      f"done={len(attempts)}/{len(todo)} "
                      f"read={-1 if entries is None else len(entries)} complete={complete}")
        chunk_proven = True
        if got_complete and entries is not None:
            before, after, promised = dict(held_before), {}, {}
            for v in got or []:
                after[v] = after.get(v, 0) + 1
            for i in chunk:
                st = attempts.get(i, "")
                if not st or _copies_that_should_land({i: st}) == 0:
                    continue
                v = video_ids[i]
                promised[v] = promised.get(v, 0) + 1
            for v, n in promised.items():
                want = before.get(v, 0) + n
                if after.get(v, 0) >= want:
                    continue
                chunk_proven = False
                blog("slice", f"{plid} {n} accepted copy(ies) of {v} are not on the "
                              f"playlist read (holds {after.get(v, 0)}, expected {want}) "
                              f"- no further slice is sent")
                break
        settled = [i for i, st in sent.items() if not _copies_that_should_land({i: st})]
        if got_complete and got is not None and chunk_proven:
            settled = list(sent)
        if not _note_outcomes(spotify_id, sorted(settled), [], expected_min,
                              plan=video_ids):
            raise RuntimeError(
                "the resume record could not be written to disk, so the transfer stopped "
                "after the read it could not record — check that the app data folder is "
                "writable, then check the playlist before starting the transfer again")
        if on_verified and entries is not None and got_complete:
            try:
                on_verified(_reconcile(video_ids, entries, attempts, True))
            except Exception:
                pass
        if not complete or stopped or not chunk_proven:
            break
    return attempts, entries, complete, expected_min

def _seed_ledger(video_ids, entries):
    return _ledger(video_ids, entries, {})

def _order_preserved(plan, entries) -> bool:
    plan = [v for v in (plan or []) if isinstance(v, str) and v]
    entries = [v for v in (entries or []) if isinstance(v, str) and v]
    i = 0
    for v in entries:
        if i < len(plan) and v == plan[i]:
            i += 1
    return i == len(plan)


def _ledger_counts(ledger):
    added = sum(1 for e in ledger if e["outcome"] == "landed")
    rejected = sum(1 for e in ledger if e["outcome"] == "rejected")
    missing = sum(1 for e in ledger if e["outcome"] == "missing")
    duplicate = sum(1 for e in ledger if e["outcome"] == "duplicate")
    return added, rejected, missing, duplicate

def _counts_from_rows(source, rows) -> dict:
    rows = list(rows or [])
    ids = [r.get("videoId") for r in rows if isinstance(r, dict) and r.get("videoId")]
    out = {"source": int(source or 0),
           "unique": max(0, len(ids) - _repeat_count(ids)),
           "added": 0, "skipped": 0, "duplicate": 0, "unverified": 0, "refused": 0,
           "pending": 0}
    for r in rows:
        state = (r or {}).get("state")
        if state == "landed":
            out["added"] += 1
        elif state == "skipped":
            out["skipped"] += 1
        elif state == "duplicate":
            out["duplicate"] += 1
        elif state == "refused":
            out["refused"] += 1
        elif state in ("pending", "appending"):
            out["pending"] += 1
        elif state == "missing":
            out["unverified"] += 1
    return out

def _run_counts_locked() -> dict:
    rows = list(RUN.get("rows") or [])
    counts = _counts_from_rows(int(RUN.get("total", 0) or 0), rows)
    RUN["landed"] = int(counts.get("added", 0) or 0)
    RUN["skipped"] = int(counts.get("skipped", 0) or 0)
    RUN["refused"] = int(counts.get("refused", 0) or 0)
    RUN["duplicate"] = int(counts.get("duplicate", 0) or 0)
    RUN["unverified"] = int(counts.get("unverified", 0) or 0)
    RUN["pending"] = int(counts.get("pending", 0) or 0)
    RUN["counts"] = counts
    RUN["missing"] = sum(1 for r in rows
                         if r.get("state") == "missing" and not r.get("unverified"))
    return counts

def _attempted_from(snap) -> int:
    c = (snap or {}).get("counts") or {}
    return sum(int(c.get(k, 0) or 0)
               for k in ("added", "skipped", "duplicate", "unverified", "refused", "pending"))


def _stop_rows_locked(msg: str) -> None:
    for row in RUN.get("rows") or []:
        if row.get("state") in ("landed", "refused", "missing", "skipped", "duplicate"):
            continue
        row["state"] = "missing"
        row["unverified"] = True
        row["status"] = f"stopped: {msg}"[:80]
        RUN["seq"] += 1
        row["row_seq"] = RUN["seq"]
    _run_counts_locked()

def _settle_verdict_rows_locked(outcomes, row_of_pos) -> int:
    n = 0
    for pos, st in (outcomes or {}).items():
        s = str(st or "")
        dup = "DUPLICATE" in s.upper()
        if not dup and not is_fatal_status(s) and "FAILED" not in s.upper():
            continue
        try:
            k = row_of_pos(int(pos))
        except (TypeError, ValueError):
            continue
        if k is None:
            continue
        row = RUN["rows"][k]
        if row.get("state") in ("landed", "refused", "duplicate", "missing", "skipped"):
            continue
        row["state"] = "duplicate" if dup else "refused"
        row["status"] = s[:80]
        row.pop("unverified", None)
        row.pop("unsure", None)
        RUN["seq"] += 1
        row["row_seq"] = RUN["seq"]
        n += 1
    return n

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

def _proven_empty(plid: str, yt_cookie: str, entries, complete: bool) -> bool:
    if not complete or not isinstance(entries, list) or entries:
        return False
    again, again_complete = _read_back_retry(plid, yt_cookie, tries=2)
    if again is None or not again_complete:
        return False
    return not again

def _create_new(yt_cookie, title, description, video_ids, privacy, spotify_id, hooks=None,
                version_sigs=None, plan_slots=None, plan_guesses=None):
    on_created = (hooks or {}).get("on_created")
    on_phase = (hooks or {}).get("on_phase")
    if on_phase:
        try:
            on_phase("create")
        except Exception:
            pass
    inline = video_ids[:CREATE_INLINE_MAX]
    if spotify_id and not _persist_fields(
            spotify_id, create_pending=True, state="creating",
            expected=len(video_ids), complete=False, expected_min=0,
            unsure=[], inflight=[], title=title,
            video_ids=[v for v in video_ids if isinstance(v, str) and v],
            plan_slots=[v if isinstance(v, str) else "" for v in (plan_slots or [])],
            plan_guesses={str(k): str(v)[:80] for k, v in (plan_guesses or {}).items()}):
        raise RuntimeError(
            "the resume record could not be written to disk, so no playlist was created "
            "— check that the app data folder is writable, then start the transfer again")
    body = json.dumps({"context": {"client": CLIENT}, "title": title,
                       "description": description or "", "privacyStatus": privacy,
                       "videoIds": inline}).encode()
    req = urllib.request.Request(
        f"https://music.youtube.com/youtubei/v1/playlist/create?key={YT_KEY}&prettyPrint=false",
        data=body, headers=yt_headers(yt_cookie))
    try:
        r = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    except Exception as e:
        raise RuntimeError(
            f"the playlist create's reply was lost ({type(e).__name__}), so the app cannot "
            f"tell whether a playlist with this title was made. Nothing else was sent. "
            f"Check your YouTube Music library: if that playlist is there, keep it — its "
            f"link is in the address bar, and adding what is missing by hand finishes the "
            f"transfer; if it is not there, nothing was created. This app will not create a "
            f"second playlist while it cannot tell which happened") from e
    try:
        reply = json.loads(r) or {}
    except Exception:
        reply = {}
    plid = reply.get("playlistId") or reply.get("playlistID")
    if not plid:
        raise RuntimeError(
            "the playlist create's reply carried no id, so the app cannot tell whether a "
            "playlist with this title was made. Nothing else was sent. Check your YouTube "
            "Music library: if that playlist is there, keep it — its link is in the address "
            "bar, and adding what is missing by hand finishes the transfer; if it is not "
            "there, nothing was created. This app will not create a second playlist while "
            "it cannot tell which happened")
    url = f"https://music.youtube.com/playlist?list={plid}"
    seen, dup_pos = set(), []
    for pos, v in enumerate(inline):
        if v in seen:
            dup_pos.append(pos)
        seen.add(v)
    dup_ids = {video_ids[p] for p in dup_pos}
    inline_positions = list(range(len(inline)))
    if not _persist_incomplete(spotify_id, plid, url, title, len(video_ids),
                               plan=video_ids, plan_slots=plan_slots,
                               plan_guesses=plan_guesses,
                               expected_min=len(set(inline)),
                               unsure=sorted(_frozen_positions(video_ids, inline_positions)),
                               unsure_ids=sorted(set(inline)),
                               unsure_need={v: video_ids.count(v) for v in set(inline)}):
        raise RuntimeError(
            "the playlist was created but the resume record could not be written to "
            f"disk, so the transfer stopped before appending — the playlist exists at "
            f"{url}. Note that link: this app could not save it, so it will not know about "
            f"this playlist next time. Check it in YouTube Music and add what is missing by "
            f"hand")
    if on_created:
        try:
            on_created(plid, url, False)
        except Exception:
            pass
    first, first_complete = _read_back_retry(plid, yt_cookie,
                                             minimum=len(set(inline)))
    blog("create", f"id={plid} inline={len(inline)} back={-1 if first is None else len(first)} "
                   f"complete={first_complete}")
    if first is None:
        blog("verify", "read-back after create returned nothing — no appends attempted")
        raise RuntimeError(f"playlist created at {url} — inline copy landed but the rest could not be verified (read-back failed)")
    seed_ledger = _seed_ledger(video_ids, first)
    if first_complete:
        if hooks and hooks.get("on_seed"):
            try:
                hooks["on_seed"](seed_ledger)
            except Exception:
                pass
    else:
        blog("create", f"id={plid} seed read stopped short — no row published as landed")
    todo = [int(e["i"]) - 1 for e in seed_ledger if e["outcome"] != "landed"]
    shown = {}
    for v in first or []:
        shown[v] = shown.get(v, 0) + 1
    faded = []
    for pos in inline_positions:
        v = inline[pos]
        if first_complete and shown.get(v, 0) > 0:
            shown[v] -= 1
            continue
        faded.append(pos)
    inline_need = {v: video_ids.count(v) for v in dup_ids}
    inline_ids = _unresolved_ids(dup_ids, first if first_complete else None,
                                 need=inline_need)
    for pos in faded:
        v = inline[pos]
        inline_ids.add(v)
        inline_need[v] = max(int(inline_need.get(v, 0) or 0), video_ids.count(v))
    inline_unknown = sorted(p for p, v in enumerate(video_ids) if v in inline_ids)
    if not _set_unknown(spotify_id, video_ids, inline_ids, len(set(inline)),
                        need=inline_need):
        raise RuntimeError(
            "the resume record could not be written to disk, so the transfer stopped "
            "before appending — check that the app data folder is writable, then start "
            "the transfer again")
    withheld = [p for p in todo if p in inline_unknown]
    if withheld:
        blog("create", f"id={plid} {len(withheld)} inline copy(ies) the read does not "
                       f"show — never re-sent: "
                       + ",".join(str(p + 1) for p in withheld[:8]))
        todo = [p for p in todo if p not in inline_unknown]
    if not first_complete:
        blog("create", f"id={plid} read incomplete — {len(todo)} append(s) withheld")
        todo = []
    if on_phase:
        try:
            on_phase("append")
        except Exception:
            pass
    attempts, entries, entries_complete, expected_min = _append_verify(
        plid, video_ids, todo, yt_cookie, hooks, first, first_complete,
        spotify_id=spotify_id)
    if on_phase:
        try:
            on_phase("verify")
        except Exception:
            pass
    for p in inline_unknown:
        attempts.setdefault(p, f"{STATUS_UNSURE} inline create: the create carried this "
                               f"copy and no complete read names the row it became")
    ledger = _reconcile(video_ids, entries, attempts, entries_complete)
    added, rejected, missing, duplicates = _ledger_counts(ledger)
    repeats = _repeat_count(video_ids, version_sigs)
    blog("parity", f"{added}/{len(video_ids)} landed, {rejected} refused, {missing} missing, "
                   f"{duplicates} duplicate, {repeats} repeat(s) in source, "
                   f"{len(todo)} append attempt(s)")
    if rejected or missing:
        bad = [f"{e['videoId']}={e['outcome']}" for e in ledger if e["outcome"] != "landed"][:8]
        blog("ledger", f"not-landed sample: {','.join(bad)}")
    _persist_final(spotify_id, plid, url, title, len(video_ids),
                   added == len(video_ids) and entries_complete and not inline_ids)
    if not added:
        if _proven_empty(plid, yt_cookie, entries, entries_complete):
            cleaned = yt_delete_playlist(plid, yt_cookie)
            _raise_outcome("nothing landed (playlist reads back empty"
                           + (", husk deleted" if cleaned else ", husk cleanup failed — delete it manually")
                           + f"). {rejected} refused by YTM, {missing} missing — see server.log add lines",
                           attempts)
        _raise_outcome(f"the playlist is at {url} and the rest of what was sent could not be "
                       f"verified ({rejected} refused by YTM, {missing} unverified because the "
                       f"read-back that follows used the same auth — see server.log add lines)",
                       attempts)
    return {"playlistId": plid, "url": url,
            "added": added, "sent": len(video_ids), "rejected": rejected,
            "missing": missing, "duplicates": duplicates, "repeats": repeats, "ledger": ledger,
            "attempts": dict(attempts),
            "entries": entries, "read_complete": bool(entries_complete),
            "fatal": sorted({str(s) for s in attempts.values() if is_fatal_status(s)}),
            "unsure": inline_unknown,
            "playlist_count": len(entries) if isinstance(entries, list) else 0,
            "expected_min": expected_min}

def _resume_incomplete(rec, yt_cookie, title, video_ids, spotify_id, hooks=None,
                       version_sigs=None):
    on_created = (hooks or {}).get("on_created")
    on_phase = (hooks or {}).get("on_phase")
    plid = rec.get("yt_id", "")
    url = rec.get("url", f"https://music.youtube.com/playlist?list={plid}")
    if _honour_retry_clock(rec) > 0:
        _persist_progress(spotify_id, plid, url, title, len(video_ids),
                          state="standby", attempt=int(rec.get("attempt", 0) or 0),
                          next_run_at=0.0, pending=rec.get("pending") or [],
                          plan=video_ids)
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
    floor, frozen, unknown_ids, unknown_need = _persisted_unknown(rec, video_ids)
    entries, entries_complete = _read_back_retry(plid, yt_cookie, minimum=floor)
    if entries is None:
        raise RuntimeError(f"playlist created at {url} — inline copy landed but the rest could not be verified (read-back failed)")
    if unknown_ids and entries_complete:
        keep = _unresolved_ids(unknown_ids, entries, need=unknown_need)
        if keep != unknown_ids:
            frozen = {i for i, v in enumerate(video_ids) if v in keep}
            unknown_ids = keep
            if not _set_unknown(spotify_id, video_ids, keep, floor, need=unknown_need):
                raise RuntimeError(
                    "the resume record could not be written to disk, so the transfer "
                    "stopped before appending — check that the app data folder is "
                    "writable, then start the transfer again")
    seed_ledger = _seed_ledger(video_ids, entries)
    if entries_complete:
        if hooks and hooks.get("on_seed"):
            try:
                hooks["on_seed"](seed_ledger)
            except Exception:
                pass
    else:
        blog("resume", f"id={plid} seed read stopped short — no row published as landed")
    todo = [int(e["i"]) - 1 for e in seed_ledger if e["outcome"] != "landed"]
    withheld = [p for p in todo if p in frozen]
    if withheld:
        blog("resume", f"id={plid} {len(withheld)} unknown write(s) frozen: "
                       + ",".join(str(p + 1) for p in withheld[:8]))
    if frozen:
        todo = [p for p in todo if p not in frozen]
    blog("resume", f"id={plid} have={len(entries)} todo={len(todo)} "
                   f"floor={floor} frozen={len(frozen)} complete={entries_complete}")
    if not entries_complete:
        blog("resume", f"id={plid} read incomplete — {len(todo)} append(s) withheld")
        todo = []
    attempts, entries2, entries2_complete, expected_min = _append_verify(
        plid, video_ids, todo, yt_cookie, hooks, entries, entries_complete, floor=floor,
        spotify_id=spotify_id)
    if on_phase:
        try:
            on_phase("verify")
        except Exception:
            pass
    ledger = _reconcile(video_ids, entries2, attempts, entries2_complete)
    added, rejected, missing, duplicates = _ledger_counts(ledger)
    repeats = _repeat_count(video_ids, version_sigs)
    blog("parity", f"resume {added}/{len(video_ids)} landed, {rejected} refused, "
                   f"{missing} missing, {duplicates} duplicate")
    _persist_final(spotify_id, plid, url, title, len(video_ids),
                   added == len(video_ids) and entries2_complete and not unknown_ids)
    if not added:
        if _proven_empty(plid, yt_cookie, entries2, entries2_complete):
            cleaned = yt_delete_playlist(plid, yt_cookie)
            _raise_outcome("nothing landed (playlist reads back empty"
                           + (", husk deleted" if cleaned else ", husk cleanup failed — delete it manually")
                           + f"). {rejected} refused by YTM, {missing} missing — see server.log add lines",
                           attempts)
        _raise_outcome(f"the playlist is at {url} and the rest of what was sent could not be "
                       f"verified ({rejected} refused by YTM, {missing} unverified because the "
                       f"read-back that follows used the same auth — see server.log add lines)",
                       attempts)
    return {"playlistId": plid, "url": url,
            "added": added, "sent": len(video_ids), "rejected": rejected,
            "missing": missing, "duplicates": duplicates, "repeats": repeats,
            "ledger": ledger, "resumed": True,
            "attempts": dict(attempts),
            "entries": entries2, "read_complete": bool(entries2_complete),
            "fatal": sorted({str(s) for s in attempts.values() if is_fatal_status(s)}),
            "unsure": sorted(frozen | {int(p) for p, st in attempts.items()
                                       if is_unsure_status(st)}),
            "playlist_count": len(entries2) if isinstance(entries2, list) else 0,
            "expected_min": max(int(expected_min or 0), floor)}

def transfer_create_append(yt_cookie, title, description, video_ids, privacy="PRIVATE",
                           spotify_id="", hooks=None, version_sigs=None,
                           plan_slots=None, plan_guesses=None):
    yt_cookie = normalize_cookie(yt_cookie)
    ids = [v for v in (video_ids or []) if isinstance(v, str) and v]
    if not ids:
        raise ValueError("no videoIds to send")
    if spotify_id:
        rec = load_created().get(spotify_id)
        if rec and not rec.get("yt_id") and rec.get("create_pending"):
            raise RuntimeError(
                "an earlier create for this playlist may have made a YouTube Music playlist "
                "and its record has no playlist id, so this app cannot tell whether it "
                "exists. Nothing was sent. Check your library: if a playlist with this title "
                "is there, keep it — its link is in the address bar, and adding what is "
                "missing by hand finishes the transfer. This app will not create a second "
                "one while it cannot tell which happened")
        if rec and rec.get("yt_id"):
            if int(rec.get("record_version") or 0) < RECORD_VERSION:
                safe, why = _legacy_safe(rec, rec["yt_id"], yt_cookie)
                blog("legacy", f"{rec['yt_id']} {'usable' if safe else 'not used'}: {why}")
                if not safe:
                    raise RuntimeError(_LEGACY_UNRESUMABLE.format(
                        url=(rec.get("url")
                             or f"https://music.youtube.com/playlist?list={rec['yt_id']}"),
                        gap=why))
                if spotify_id and not _persist_fields(spotify_id,
                                                      record_version=RECORD_VERSION):
                    blog("legacy", f"{rec['yt_id']} verified but the version stamp "
                                   f"could not be saved — it will be verified again")
            if rec.get("complete") is True:
                top_floor, top_frozen, top_ids, top_need = _persisted_unknown(rec, ids)
                if spotify_id and not _adopt_plan(spotify_id, ids, plan_slots, plan_guesses,
                                                  top_floor, top_frozen, top_ids,
                                                  need=top_need):
                    raise RuntimeError(
                        "the resume record could not be written to disk, so the top-up "
                        "stopped before appending — check that the app data folder is "
                        "writable, then start the transfer again")
                entries, entries_complete = _read_back_retry(
                    rec["yt_id"], yt_cookie, minimum=top_floor)
                if entries is None:
                    entries = []
                if entries is not None:
                    seed = _reconcile(ids, entries, {}, entries_complete)
                    todo = [int(e["i"]) - 1 for e in seed if e["outcome"] != "landed"]
                    if top_ids and entries_complete:
                        keep = _unresolved_ids(top_ids, entries, need=top_need)
                        if keep != top_ids:
                            top_ids = keep
                            top_frozen = {i for i, v in enumerate(ids) if v in keep}
                            if spotify_id and not _set_unknown(
                                    spotify_id, ids, keep, top_floor, need=top_need):
                                raise RuntimeError(
                                    "the resume record could not be written to disk, so "
                                    "the top-up stopped before appending — check that the "
                                    "app data folder is writable, then start the transfer "
                                    "again")
                    withheld = [p for p in todo if p in top_frozen]
                    if withheld:
                        blog("topup", f"id={rec['yt_id']} {len(withheld)} unknown write(s) "
                                      f"frozen: " + ",".join(str(p + 1) for p in withheld[:8]))
                        todo = [p for p in todo if p not in top_frozen]
                    if not todo:
                        added = sum(1 for e in seed if e["outcome"] == "landed")
                        missing = sum(1 for e in seed if e["outcome"] != "landed")
                        for e in seed:
                            if e["outcome"] != "landed":
                                e["status"] = "exists — not on playlist"
                        _persist_final(spotify_id, rec["yt_id"], rec.get("url", ""), title,
                                       len(ids), (missing == 0 and entries_complete
                                                  and not top_frozen and not top_ids))
                        blog("create", f"exists title={title} url={rec.get('url')} tracks={added}")
                        return {"playlistId": rec["yt_id"], "url": rec.get("url", ""),
                                "added": added, "sent": len(ids), "rejected": 0, "missing": missing,
                                "repeats": _repeat_count(ids, version_sigs), "existing": True, "ledger": seed,
                                "unsure": sorted(top_frozen),
                                "playlist_count": len(entries) if isinstance(entries, list) else 0,
                                "expected_min": max(top_floor,
                                                    len(entries) if isinstance(entries, list) else 0)}
                    on_phase = (hooks or {}).get("on_phase")
                    if on_phase:
                        try:
                            on_phase("append")
                        except Exception:
                            pass
                    plid = rec["yt_id"]
                    blog("topup", f"id={plid} have={len(entries)} missing={len(todo)} "
                                  f"floor={top_floor} frozen={len(top_frozen)} "
                                  f"complete={entries_complete}")
                    if not entries_complete:
                        blog("topup", f"id={plid} read incomplete — {len(todo)} append(s) withheld")
                        todo = []
                    attempts, entries2, entries2_complete, expected_min = _append_verify(
                        plid, ids, todo, yt_cookie, hooks, entries, entries_complete,
                        floor=max(top_floor, 0), spotify_id=spotify_id)
                    if on_phase:
                        try:
                            on_phase("verify")
                        except Exception:
                            pass
                    ledger = _reconcile(ids, entries2, attempts, entries2_complete)
                    added, rejected, missing, duplicates = _ledger_counts(ledger)
                    todo_set = set(todo)
                    newly = sum(1 for e in ledger
                                if e["outcome"] == "landed" and (int(e["i"]) - 1) in todo_set)
                    repeats = _repeat_count(ids, version_sigs)
                    blog("parity", f"topup {added}/{len(ids)} landed, {rejected} refused, "
                                   f"{missing} missing, {duplicates} duplicate")
                    _persist_final(spotify_id, plid, rec.get("url", ""), title, len(ids),
                                   added == len(ids) and entries2_complete and not top_ids)
                    return {"playlistId": plid, "url": rec.get("url", ""),
                            "added": added, "sent": len(ids), "rejected": rejected, "missing": missing,
                            "duplicates": duplicates, "repeats": repeats, "existing": True, "ledger": ledger,
                            "attempts": dict(attempts),
                            "entries": entries2, "read_complete": bool(entries2_complete),
                            "fatal": sorted({str(s) for s in attempts.values()
                                             if is_fatal_status(s)}),
                            "unsure": sorted(top_frozen | {int(p) for p, st in attempts.items()
                                                           if is_unsure_status(st)}),
                            "playlist_count": len(entries2) if isinstance(entries2, list) else 0,
                            "expected_min": max(int(expected_min or 0), top_floor),
                            "topped_up": newly}
            else:
                saved = [v for v in (rec.get("video_ids") or []) if isinstance(v, str) and v]
                if not saved:
                    raise RuntimeError(_NO_PLAN_MESSAGE.format(url=rec.get("url") or rec.get("yt_id", "")))
                ids = saved
                return _resume_incomplete(rec, yt_cookie, title, ids, spotify_id, hooks, version_sigs)
    r = _create_new(yt_cookie, title, description, ids, privacy, spotify_id, hooks, version_sigs,
                    plan_slots, plan_guesses)
    return r

def yt_create_playlist(yt_cookie: str, title: str, description: str, video_ids: list, privacy: str = "PRIVATE"):
    return transfer_create_append(yt_cookie, title, description, video_ids, privacy, "", None)

def yt_create_checked(spotify_id: str, yt_cookie: str, title: str, description: str,
                      video_ids: list, privacy: str = "PRIVATE") -> dict:
    return transfer_create_append(yt_cookie, title, description, video_ids, privacy, spotify_id or "", None)

RUN_RETRY_MAX = 6  # retries per run: the budget is bounded by policy, not by luck
CONVERGE_MAX_PASSES = 2 * RUN_RETRY_MAX
_RETRY_DELAYS = (30.0, 60.0, 120.0, 300.0, 600.0, 1200.0)  # 30s, 1m, 2m, 5m, 10m, 20m
_RETRY_CAP = 1200.0
_RETRY_JITTER = 0.2

def _retry_delay(attempt: int) -> float:
    base = _RETRY_DELAYS[min(max(int(attempt or 1), 1), len(_RETRY_DELAYS)) - 1]
    return max(1.0, min(_RETRY_CAP, base + random.uniform(-base * _RETRY_JITTER,
                                                          base * _RETRY_JITTER)))

def _standby_wait(seconds: float):
    RUN_WAKE.wait(timeout=max(0.0, seconds))
    RUN_WAKE.clear()

def _unproven(video_ids, ledger) -> list:
    out = []
    for i, v in enumerate(video_ids or []):
        e = ledger[i] if i < len(ledger or []) else None
        if isinstance(e, dict) and e.get("outcome") != "landed":
            out.append(v)
    return out


def _refused_positions(attempts, ledger) -> list:
    out = []
    for k, st in (attempts or {}).items():
        try:
            i = int(k)
        except Exception:
            continue
        s = str(st or "").upper()
        if "DUPLICATE" not in s and "FAILED" not in s:
            continue
        e = ledger[i] if i < len(ledger or []) else None
        if isinstance(e, dict) and e.get("outcome") == "landed":
            continue
        out.append(i)
    return out


def _converge(plid, yt_cookie, video_ids, spotify_id="", title="", url="",
              floor=0, residual=None, refused=None, unsure=None, hooks=None, sent=None):
    intended = [v for v in (video_ids or []) if isinstance(v, str) and v]
    attempt, prev = 0, {}
    if spotify_id:
        rec = load_created().get(spotify_id) or {}
        prev = rec if isinstance(rec, dict) else {}
        if str(prev.get("state", "")) in ("verifying", "repairing", "standby"):
            attempt = int(prev.get("attempt", 0) or 0)
            _honour_retry_clock(prev)
    statuses, entries, complete = {}, None, False
    for p, st in (sent or {}).items():
        try:
            i = int(p)
        except Exception:
            continue
        if 0 <= i < len(intended) and st:
            statuses[i] = str(st)
    last_need = [v for v in (residual or []) if isinstance(v, str) and v]
    blocked = [v for v in (residual or []) if isinstance(v, str) and v]
    refused_pos = {int(p) for p in (refused or []) if str(p).lstrip("-").isdigit()}
    frozen = set(_frozen_positions(intended, [int(p) for p in (unsure or [])
                                              if str(p).lstrip("-").isdigit()]))
    unresolved = set()   # unknown ids no COMPLETE read has accounted for
    unsure_ids = []   # copies this run cannot resolve, in plan order, for the residual
    short = 0   # reads that came back short while nothing was proven absent
    passes = 0  # loop iterations, whatever they did: the outer bound
    floor = max(0, int(floor or 0))  # entries this run's own evidence says must be there
    prev_present = None  # the copy count the previous complete read held
    prev_resolved = None  # plan copies the previous complete read accounted for
    applied = False  # the last repair was accepted in full, and landed nowhere
    stuck = 0   # passes whose complete read proved nothing new
    fresh = False  # the repair's own complete read is the newest state of the playlist

    def resolve_unknowns(got):
        nonlocal frozen, unresolved
        if spotify_id:
            rec_now = _record(spotify_id)
            known = _unknown_ids_in(rec_now) | {intended[p] for p in frozen
                                                if 0 <= p < len(intended)}
            need_map = _unknown_need(rec_now)
        else:
            known = {intended[p] for p in frozen if 0 <= p < len(intended)}
            need_map = {}
        for v in known:
            need_map[v] = max(int(need_map.get(v, 0) or 0), intended.count(v))
        keep = _unresolved_ids(known, got, need=need_map)
        unresolved = set(keep)
        if keep == known:
            return
        frozen = {p for p in frozen
                  if 0 <= p < len(intended) and intended[p] in keep}
        if spotify_id and not _set_unknown(spotify_id, intended, keep, floor,
                                           need=need_map):
            raise RuntimeError(
                "the resume record could not be written to disk, so the verification "
                "stopped — check that the app data folder is writable, then start the "
                "transfer again")

    def stand_by(reason, residual):
        nonlocal attempt
        if attempt >= RUN_RETRY_MAX:
            run_set(state="gaveup", attempt=attempt, next_attempt_at=0.0,
                    reason=reason, residual=residual)
            _persist_progress(spotify_id, plid, url, title, len(intended),
                              state="gaveup", attempt=attempt, pending=residual,
                              plan=intended, expected_min=floor, unsure=sorted(frozen))
            blog("gaveup", f"{plid} after {attempt} attempt(s): {reason}; still absent: "
                           + ",".join(residual[:8]))
            return True
        attempt += 1
        delay = _retry_delay(attempt)
        nxt = _now() + delay
        run_set(state="standby", attempt=attempt, next_attempt_at=nxt,
                reason=reason, residual=residual)
        _persist_progress(spotify_id, plid, url, title, len(intended), state="standby",
                          attempt=attempt, next_run_at=nxt, pending=residual,
                          plan=intended, expected_min=floor, unsure=sorted(frozen))
        blog("standby", f"{plid} attempt {attempt}/{RUN_RETRY_MAX} in {delay:.0f}s: {reason}")
        _standby_wait(delay)
        return False

    def finish(state, reason="", residual=None, final_complete=False):
        run_set(state=state, attempt=attempt, next_attempt_at=0.0, reason=reason,
                residual=list(residual or []))
        _persist_progress(spotify_id, plid, url, title, len(intended), state=state,
                          attempt=attempt, pending=residual,
                          plan=intended, expected_min=floor, unsure=sorted(frozen))
        _persist_final(spotify_id, plid, url, title, len(intended),
                       bool(final_complete and not frozen and not refused_pos
                            and not unresolved and complete))
        return {"state": state, "attempt": attempt, "reason": reason,
                "residual": list(residual or []), "entries": entries,
                "complete": complete, "statuses": statuses,
                "frozen": sorted(frozen)}

    while True:
        passes += 1
        if passes > CONVERGE_MAX_PASSES:
            reason = f"no progress after {CONVERGE_MAX_PASSES} passes"
            blog("gaveup", f"{plid} {reason}; still absent: " + ",".join(last_need[:8]))
            return finish("gaveup", reason, last_need)
        run_set(state="verifying", attempt=attempt, next_attempt_at=0.0, reason="", residual=[])
        if fresh:
            fresh = False
            got, got_complete = entries, complete
        else:
            got, got_complete = _read_back_retry(plid, yt_cookie, minimum=floor)
            if got is not None:
                entries, complete = got, got_complete
        if got is None or not got_complete:
            reason = "read failed" if got is None else "read incomplete"
            short += 1
            if stand_by(reason, last_need):
                return finish("gaveup", reason, last_need)
            continue
        resolve_unknowns(got)
        present = {}
        for v in got:
            present[v] = present.get(v, 0) + 1
        floor = max(floor, len(got))
        held = dict(present)
        need_pos = []
        blocked = []
        unsure_ids = []
        present_copies = 0   # plan copies this read accounts for, whatever their slot
        for pos, v in enumerate(intended):
            if held.get(v, 0) > 0:
                held[v] -= 1
                present_copies += 1
            elif pos in refused_pos:
                blocked.append(v)
            elif pos in frozen:
                unsure_ids.append(v)
            else:
                need_pos.append(pos)
        need = [intended[p] for p in need_pos]
        accepted_absent = [p for p in need_pos
                           if _copies_that_should_land({p: statuses.get(p, "")}) > 0]
        stalled = []
        for v in {intended[p] for p in accepted_absent}:
            quota = (intended.count(v)
                     - sum(1 for q in refused_pos if intended[q] == v)
                     - sum(1 for q in frozen if intended[q] == v))
            if present.get(v, 0) < max(0, quota):
                stalled.extend(p for p in accepted_absent if intended[p] == v)
        if stalled:
            frozen.update(_frozen_positions(intended, stalled))
            if spotify_id and not _persist_fields(
                    spotify_id, unsure=sorted(frozen), expected_min=floor,
                    unsure_ids=[intended[p] for p in sorted(frozen)
                                if 0 <= p < len(intended)],
                    unsure_need={intended[p]: intended.count(intended[p])
                                 for p in stalled if 0 <= p < len(intended)}):
                raise RuntimeError(
                    "the resume record could not be written to disk, so the verification "
                    "stopped — check that the app data folder is writable, then start the "
                    "transfer again")
            blog("converge", f"{plid} {len(stalled)} accepted copy(ies) no complete read "
                             f"shows — frozen, never re-sent: "
                             + ",".join(str(p + 1) for p in stalled[:8]))
            gone = set(stalled)
            gone = {p for p in need_pos if p in frozen} | gone
            need_pos = [p for p in need_pos if p not in gone]
            unsure_ids.extend(intended[p] for p in sorted(gone) if 0 <= p < len(intended))
            need = [intended[p] for p in need_pos]
        last_need = (need or unsure_ids
                     or [v for v in intended if v in unresolved] or blocked)
        if not need:
            if unsure_ids or unresolved:
                reason = ("an add whose outcome is unknown was not re-sent — check the "
                          "playlist, then add it by hand or run the transfer again")
                names = unsure_ids + [v for v in intended
                                      if v in unresolved and v not in unsure_ids]
                blog("gaveup", f"{plid} {reason}; unknown: " + ",".join(names[:8]))
                return finish("gaveup", reason, names + blocked)
            if blocked:
                blog("converge", f"{plid} {len(blocked)} copy(ies) the plan needs were "
                                 f"refused by YouTube Music: " + ",".join(blocked[:8]))
                return finish("gaveup", "a copy the plan needs was refused by YouTube Music",
                              blocked)
            blog("converge", f"{plid} verified {len(got)} entries, every copy present")
            return finish("done", final_complete=True)
        grew = (prev_resolved is None or present_copies > prev_resolved
                or any(prev_present.get(v, 0) > 0 for v in prev_present if v not in present))
        prev_resolved = present_copies
        prev_present = present
        if applied and not grew:
            stuck += 1
            reason = "append accepted but no read shows those copies"
            blog("converge", f"{plid} {reason} (pass {passes}, {stuck}/2, "
                             f"still absent: " + ",".join(need[:8]) + ")")
            if stuck >= 2:
                return finish("gaveup", reason, need)
            if stand_by(reason, need):
                return finish("gaveup", reason, need)
            continue
        stuck = 0   # the read changed: the stall counter starts over
        run_set(state="repairing", attempt=attempt, reason=f"repairing {len(need)} tracks")
        blog("converge", f"{plid} entry={len(got)} absent={len(need)}")
        sent, sent_entries, sent_complete, sent_min = _append_verify(
            plid, intended, need_pos, yt_cookie, hooks, entries, complete, floor=floor,
            spotify_id=spotify_id)
        statuses.update(sent)
        for pos, st in sent.items():
            s = str(st or "").upper()
            if "DUPLICATE" in s or "FAILED" in s:
                refused_pos.add(int(pos))
            elif is_unsure_status(st):
                frozen.update(_frozen_positions(intended, [int(pos)]))
                blog("converge", f"{plid} copy {int(pos) + 1} ({intended[pos]}) outcome "
                                 f"unknown — never re-sent by this run")
        floor = max(floor, int(sent_min or 0))
        if spotify_id and sent:
            _persist_fields(spotify_id, unsure=sorted(frozen), expected_min=floor)
        if bool(sent) and sent_entries is not None and sent_complete:
            entries, complete, fresh = sent_entries, sent_complete, True
        applied = bool(sent) and not any(is_unsure_status(s) or is_fatal_status(s)
                                         for s in sent.values())
        if any(is_fatal_status(s) for s in sent.values()):
            fatal = sorted({str(s) for s in sent.values() if is_fatal_status(s)})
            run_set(state="gaveup", attempt=attempt, next_attempt_at=0.0,
                    reason="append refused by YouTube Music", residual=need)
            _persist_progress(spotify_id, plid, url, title, len(intended),
                              state="gaveup", attempt=attempt, pending=need,
                              plan=intended, expected_min=floor, unsure=sorted(frozen))
            blog("gaveup", f"{plid} the append was refused with {fatal[0] if fatal else 'a fatal status'}"
                           f" — still absent: " + ",".join(need[:8]))
            return {"state": "gaveup", "attempt": attempt,
                    "reason": "append refused by YouTube Music", "residual": need,
                    "entries": entries, "complete": complete, "statuses": statuses,
                    "fatal": fatal, "frozen": sorted(frozen)}

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
            RUN["cover_url"] = sp.get("cover_url", "") or ""
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
        guesses = {}
        timeout_batches = 0
        num_batches = 0
        rec = _incomplete_record(sid)
        plan = _resume_plan(rec, len(tracks)) if rec is not None else None
        if rec is not None and plan is None:
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                RUN["phase"] = "failed"
                RUN["state"] = "failed"
                RUN["error"] = _NO_PLAN_MESSAGE.format(
                    url=rec.get("url") or rec.get("yt_id", ""))[:300]
                RUN["kind"] = "user"
                RUN["hint"] = ("That playlist's record does not name the copies it was sent, "
                               "so nothing was sent. Check the playlist in YouTube Music and "
                               "add what is missing by hand.")
                RUN["seq"] += 1
                RUN["updated_at"] = time.time()
            blog("fail", f"run={run_id} interrupted run without a persisted plan")
            return
        if plan is not None:
            slots, guesses = plan
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                for k, v in enumerate(slots):
                    row = RUN["rows"][k]
                    if v:
                        row["videoId"] = v
                        row["state"] = "matched"
                        row["status"] = ""
                        if k in guesses:
                            row["guess"] = True
                            row["status"] = ("best guess: " + guesses[k])[:80]
                    elif row.get("state") == "queued":
                        row["state"] = "skipped"
                        row["status"] = "no match in the interrupted run"
                    RUN["seq"] += 1
                    row["row_seq"] = RUN["seq"]
                matched_ids = list(slots)
                RUN["matched"] = sum(1 for v in matched_ids if v)
                RUN["skipped"] = sum(1 for v in matched_ids if not v)
                RUN["sent"] = RUN["matched"]
                RUN["updated_at"] = time.time()
            blog("resume", f"run={run_id} replays the persisted plan "
                           f"({len([v for v in slots if v])}/{len(slots)} rows matched)")
        for base in (() if plan is not None else range(0, len(tracks), 10)):
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
                        if (res or {}).get("best_guess"):
                            row["guess"] = True
                            why = str((res or {}).get("why") or "cross-script")[:80]
                            guesses[idx] = why
                            row["status"] = ("best guess: " + why)[:80]
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
        if plan is None:   # a resume replays its plan and matches nothing
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                retry = [{"i": k, "title": (tracks[k] or {}).get("title", ""),
                          "artist": (tracks[k] or {}).get("artist", ""),
                          "duration_ms": (tracks[k] or {}).get("duration_ms", 0),
                          "album": (tracks[k] or {}).get("album", ""),
                          "skipped": RUN["rows"][k].get("status", "")}
                         for k in range(min(len(tracks), len(RUN["rows"])))
                         if RUN["rows"][k].get("state") == "skipped"
                         and RUN["rows"][k].get("status")]
            if retry:
                with _RUN_LOCK:
                    if RUN.get("id") != run_id:
                        return
                    RUN["phase"] = "search"
                resolved, tried, stop = _second_search_skipped(retry)
                with _RUN_LOCK:
                    if RUN.get("id") != run_id:
                        return
                    for pos, r in resolved.items():
                        try:
                            k = int(pos)
                        except Exception:
                            continue
                        if k >= len(RUN["rows"]):
                            continue
                        RUN["rows"][k]["videoId"] = r.get("videoId", "")
                        RUN["rows"][k]["state"] = "matched"
                        RUN["rows"][k]["via_second"] = True
                        RUN["rows"][k]["status"] = "found on a second search"
                        RUN["seq"] += 1
                        RUN["rows"][k]["row_seq"] = RUN["seq"]
                        matched_ids[k] = r.get("videoId", "")
                    RUN["matched"] = sum(1 for v in matched_ids if v)
                    RUN["skipped"] = sum(1 for v in matched_ids if not v)
                    RUN["sent"] = RUN["matched"]
                    RUN["updated_at"] = time.time()
                blog("research", f"run={run_id} second search: {len(resolved)} of "
                                 f"{len(tried)} row(s) resolved"
                                 + (f", stopped: {stop}" if stop else ""))
        with _RUN_LOCK:
            if RUN.get("id") != run_id:
                return
            vids = [RUN["rows"][k]["videoId"] for k in range(len(RUN["rows"])) if RUN["rows"][k]["videoId"]]
            RUN["sent"] = len(vids)
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
                _run_counts_locked()
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
                _run_counts_locked()
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
                        row["state"] = "pending"
                else:
                    row["status"] = (st or "")[:80]
                    if row.get("state") != "landed":
                        row["state"] = "appending"
                _run_counts_locked()
                RUN["seq"] += 1
                row["row_seq"] = RUN["seq"]
                RUN["updated_at"] = time.time()
        def _on_seed(arg):
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                if isinstance(arg, list):
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
                        except Exception:
                            pass
                _run_counts_locked()
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
                 "on_append_one": _on_one, "on_phase": _on_phase, "on_seed": _on_seed,
                 "on_verified": _on_seed}
        try:
            res = transfer_create_append(yt_cookie, pname, "", vids, privacy, sid, hooks, sigs_for_pos,
                                         plan_slots=list(matched_ids) if plan is None else None,
                                         plan_guesses=dict(guesses) if plan is None else None)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            c = classify(msg)
            if isinstance(e, FatalAppend):
                c = _write_verdict(e.status)
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                RUN["phase"] = "failed"
                RUN["state"] = "failed"
                RUN["next_attempt_at"] = 0.0
                RUN["error"] = msg[:300]
                RUN["hint"] = c.get("hint", "")
                RUN["kind"] = c.get("kind", "unknown")
                if isinstance(e, FatalAppend) and e.outcomes:
                    _settle_verdict_rows_locked(e.outcomes, _row_of_pos)
                _stop_rows_locked(msg)
                RUN["seq"] += 1
                RUN["updated_at"] = time.time()
            blog("fail", f"run={run_id} {msg}")
            return
        if not res.get("existing") and (sp.get("cover_url") or ""):
            try:
                cv = yt_set_playlist_cover(res.get("playlistId", ""), sp.get("cover_url", ""), yt_cookie)
                blog("cover", f"run={run_id} {'ok' if cv.get('ok') else 'skip ' + str(cv.get('why', ''))}")
            except Exception:
                pass
        first_attempts = {int(k): str(v or "") for k, v in (res.get("attempts") or {}).items()}
        unsure_positions = {p for p, st in first_attempts.items() if is_unsure_status(st)}
        for p in res.get("unsure") or []:
            try:
                unsure_positions.add(int(p))
            except Exception:
                continue
        fatal_first = [str(s) for s in (res.get("fatal") or []) if s]
        if fatal_first:
            blog("converge", f"{res.get('playlistId', '')} not repaired: the append was "
                             f"refused with {fatal_first[0]}")
            _persist_progress(sid, res.get("playlistId", ""), res.get("url", ""), pname,
                              len(vids), state="gaveup", attempt=0,
                              pending=_unproven(vids, res.get("ledger") or []),
                              plan=vids,
                              expected_min=int(res.get("expected_min") or 0),
                              unsure=sorted(unsure_positions))
            conv = {"state": "gaveup", "attempt": 0,
                    "reason": "append refused by YouTube Music",
                    "residual": _unproven(vids, res.get("ledger") or []),
                    "entries": res.get("entries"),
                    "complete": bool(res.get("read_complete")),
                    "statuses": {}, "fatal": fatal_first,
                    "frozen": sorted(unsure_positions)}
        else:
            conv = _converge(res.get("playlistId", ""), yt_cookie, vids, sid, pname,
                             res.get("url", ""),
                             floor=int(res.get("expected_min") or 0),
                             residual=_unproven(vids, res.get("ledger") or []),
                             refused=_refused_positions(res.get("attempts") or {},
                                                        res.get("ledger") or []),
                             unsure=sorted(unsure_positions),
                             hooks=hooks,
                             sent=first_attempts)
        entries_final = conv.get("entries")
        attempts = dict(first_attempts)
        for pos, st in (conv.get("statuses") or {}).items():
            try:
                i = int(pos)
            except Exception:
                continue
            if st and 0 <= i < len(vids):
                attempts[i] = st
        for pos in conv.get("frozen") or []:
            try:
                i = int(pos)
            except Exception:
                continue
            st = str(attempts.get(i, "") or "")
            if 0 <= i < len(vids) and (not st or _copies_that_should_land({i: st}) > 0):
                attempts[i] = (f"{STATUS_UNSURE} accepted, and no complete read shows the "
                               f"copy it added")
        ledger = _reconcile(vids, entries_final or [], attempts, bool(conv.get("complete")))
        with _RUN_LOCK:
            if RUN.get("id") != run_id:
                return
            sent_rows = [k for k, row in enumerate(RUN["rows"]) if row.get("videoId")]
            for pos, k in enumerate(sent_rows):
                e = ledger[pos] if pos < len(ledger) else None
                if not e:
                    continue
                out = e.get("outcome")
                if out == "landed":
                    RUN["rows"][k]["state"] = "landed"
                    RUN["rows"][k]["status"] = ("found on a second search"
                                                if RUN["rows"][k].get("via_second") else "")
                elif out == "duplicate":
                    RUN["rows"][k]["state"] = "duplicate"
                    RUN["rows"][k]["status"] = str(e.get("status", ""))[:80]
                elif out == "rejected":
                    RUN["rows"][k]["state"] = "refused"
                    RUN["rows"][k]["status"] = str(e.get("status", ""))[:80]
                else:
                    RUN["rows"][k]["state"] = "missing"
                    RUN["rows"][k]["status"] = str(e.get("status", ""))[:80]
                    if e.get("unverified"):
                        RUN["rows"][k]["unverified"] = True
                        if e.get("unsure"):
                            RUN["rows"][k]["unsure"] = True
                    elif pos not in attempts:
                        RUN["rows"][k]["unverified"] = True
                        RUN["rows"][k]["not_sent"] = True
                        RUN["rows"][k]["status"] = (
                            "not sent — the run stopped before this copy")[:80]
                RUN["seq"] += 1
                RUN["rows"][k]["row_seq"] = RUN["seq"]
            RUN["playlist_id"] = res.get("playlistId", RUN.get("playlist_id", ""))
            RUN["url"] = res.get("url", RUN.get("url", ""))
            RUN["created"] = True
            RUN["existing"] = bool(res.get("existing", False))
            RUN["state"] = conv.get("state", "done")
            RUN["attempt"] = int(conv.get("attempt", 0) or 0)
            RUN["next_attempt_at"] = 0.0
            RUN["reason"] = conv.get("reason", "")
            RUN["residual"] = list(conv.get("residual") or [])
            for row in RUN["rows"]:
                if row.get("state") not in ("landed", "skipped", "refused", "missing", "duplicate"):
                    row["state"] = "missing"
                    row["status"] = (row.get("status") or "reconciled as missing")[:80]
                    RUN["seq"] += 1
                    row["row_seq"] = RUN["seq"]
            RUN["landed"] = sum(1 for row in RUN["rows"] if row.get("state") == "landed")
            RUN["skipped"] = sum(1 for row in RUN["rows"] if row.get("state") == "skipped")
            RUN["refused"] = sum(1 for row in RUN["rows"] if row.get("state") == "refused")
            RUN["duplicate"] = sum(1 for row in RUN["rows"] if row.get("state") == "duplicate")
            RUN["unverified"] = sum(1 for row in RUN["rows"]
                                    if row.get("state") == "missing" and row.get("unverified"))
            RUN["missing"] = sum(1 for row in RUN["rows"]
                                 if row.get("state") == "missing" and not row.get("unverified"))
            n_pl = (len(entries_final) if isinstance(entries_final, list)
                    else int(res.get("playlist_count", 0) or 0))
            RUN["playlist_count"] = n_pl
            RUN["guessed"] = sum(1 for row in RUN["rows"]
                                 if row.get("guess") and row.get("state") == "landed")
            RUN["order_ok"] = _order_preserved(vids, entries_final or [])
            RUN["unsure"] = sum(1 for row in RUN["rows"] if row.get("unsure"))
            RUN["tail_unverified"] = sum(1 for row in RUN["rows"]
                                         if row.get("unverified") and not row.get("unsure")
                                         and not row.get("not_sent"))
            RUN["counts"] = _counts_from_rows(len(tracks), RUN["rows"])
            RUN["summary"] = _done_summary(
                RUN["counts"], n_pl,
                topped_up=(int(res.get("topped_up", 0)) if res.get("topped_up") is not None else None),
                sent=int(res.get("sent", 0) or 0), guessed=RUN["guessed"],
                tail_unverified=RUN["tail_unverified"],
                order_differs=(not RUN["order_ok"]) and RUN["landed"] == len(vids),
                unnameable=RUN["unsure"])
            RUN["phase"] = "done"
            RUN["error"] = ""
            fatal = [str(s) for s in (conv.get("fatal") or []) if s]
            if fatal:
                RUN["error"] = fatal[0][:300]
                note = _write_verdict(fatal[0])
                RUN["kind"] = note["kind"]
                RUN["hint"] = note["hint"]
            RUN["seq"] += 1
            RUN["updated_at"] = time.time()
        blog("create", f"run={run_id} ok playlist={res.get('playlistId')} confirmed={res.get('added')}/{res.get('sent')} "
                       f"refused={res.get('rejected')} missing={res.get('missing')} existing={res.get('existing', False)}")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        c = classify(msg)
        if isinstance(e, FatalAppend):
            c = _write_verdict(e.status)
        with _RUN_LOCK:
            try:
                if RUN.get("id") == run_id:
                    RUN["phase"] = "failed"
                    RUN["state"] = "failed"
                    RUN["next_attempt_at"] = 0.0
                    RUN["error"] = msg[:300]
                    RUN["hint"] = c.get("hint", "")
                    RUN["kind"] = c.get("kind", "unknown")
                    _stop_rows_locked(msg)
                    RUN["seq"] += 1
                    RUN["updated_at"] = time.time()
            except Exception:
                pass
        blog("fail", f"run={run_id} {msg}")

def _latest_incomplete():
    try:
        created = load_created() or {}
    except Exception:
        return None
    for sid, rec in (created or {}).items():
        try:
            if isinstance(rec, dict) and rec.get("yt_id") and rec.get("complete") is not True:
                legacy = int(rec.get("record_version") or 0) < RECORD_VERSION
                return {"spotify_id": sid, "playlist_id": rec.get("yt_id", ""),
                        "url": rec.get("url", ""), "title": rec.get("title", ""),
                        "expected": int(rec.get("expected", 0) or 0),
                        "state": rec.get("state", ""),
                        "attempt": int(rec.get("attempt", 0) or 0),
                        "resumable": not legacy,
                        "blocked": ("" if not legacy else
                                    "this record is from an older version and cannot prove "
                                    "which copies it already wrote, so it will not be "
                                    "written to; check the playlist and add what is "
                                    "missing by hand"),
                        "pending": list(rec.get("pending") or [])}
        except Exception:
            continue
    return None

_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")
_BLOCKED = set()   # (method, reason) already written to the log

def _host_name(value: str) -> str:
    h = (value or "").strip().lower()
    if not h:
        return ""
    if h.startswith("["):                    # [::1]:8787
        end = h.find("]")
        h = h[1:end] if end != -1 else h[1:]
    elif h.count(":") == 1:                  # 127.0.0.1:8787
        h = h.split(":", 1)[0]
    return h.rstrip(".")

def _origin_is_local(origin: str, port: int) -> bool:
    try:
        parts = urllib.parse.urlsplit(origin)
        got = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False                         # "null", file:, chrome-extension:, ...
    if _host_name(parts.netloc) not in _LOOPBACK_NAMES:
        return False
    return (got or (443 if parts.scheme == "https" else 80)) == int(port)

def request_is_local(headers, port: int) -> tuple:
    if _host_name(headers.get("Host", "")) not in _LOOPBACK_NAMES:
        return False, "host"
    origin = (headers.get("Origin") or "").strip()
    if origin and not _origin_is_local(origin, port):
        return False, "origin"
    return True, ""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def local_only(self) -> bool:
        allowed, why = request_is_local(self.headers, self.server.server_address[1])
        if not allowed:
            key = (self.command, why)
            if key not in _BLOCKED:
                _BLOCKED.add(key)
                blog("block", f"{self.command} {self.path.split('?')[0][:60]} rejected: {why}")
            self.send_json({"ok": False, "error": "not a local request"}, 403)
        return allowed
    def send_json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        if not self.local_only():
            return
        if self.path in ("/", "/index.html"):
            p = os.path.join(HERE, "index.html")
            with open(p, "rb") as f:
                b = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/health":
            self.send_json({"ok": True, "version": APP_VERSION})
        elif self.path == "/api/status":
            with _HIST_LOCK:
                recent = list(HISTORY[-20:])
            self.send_json({"ok": True, "recent": recent})
        elif self.path == "/api/settings":
            self.send_json({"ok": True, **_settings_payload(load_settings())})
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
                            "state": snap.get("state", "idle"),
                            "attempt": int(snap.get("attempt", 0) or 0),
                            "next_attempt_at": float(snap.get("next_attempt_at", 0) or 0),
                            "reason": snap.get("reason", ""),
                            "residual": list(snap.get("residual") or []),
                            "started_at": snap.get("started_at", 0),
                            "updated_at": snap.get("updated_at", 0),
                            "age": max(0.0, now - float(snap.get("updated_at", now) or now)),
                            "spotify_id": snap.get("spotify_id", ""),
                            "title": snap.get("title", ""),
                            "cover_url": snap.get("cover_url", ""),
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
                            "attempted": _attempted_from(snap),
                            "refused": int(snap.get("refused", 0) or 0),
                            "skipped": int(snap.get("skipped", 0) or 0),
                            "missing": int(snap.get("missing", 0) or 0),
                            "unverified": int(snap.get("unverified", 0) or 0),
                            "duplicate": int(snap.get("duplicate", 0) or 0),
                            "pending": int(snap.get("pending", 0) or 0),
                            "counts": dict(snap.get("counts") or {}),
                            "order_ok": bool(snap.get("order_ok", True)),
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
        elif self.path == "/api/cookies":
            self.send_json(cookies_state())
        else:
            self.send_json({"error": "not found"}, 404)
    def do_POST(self):
        if not self.local_only():
            return
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
                                      "duration_ms": int(it.get("duration_ms", 0) or 0),
                                      "album": str(it.get("album", ""))[:200]})
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
                yt = str(payload.get("yt_cookie", "") or "")
                if not normalize_cookie(yt):
                    yt = vault_load().get("yt_cookie", "")
                if not normalize_cookie(yt):
                    return self.send_json({"ok": False, "error": "connect YouTube Music first",
                                           "state": "none"}, 400)
                if cookie_shape(yt, "yt")["state"] != "valid":
                    blog("transfer", "yt cookie rejected: not a cookie")
                    return self.send_json({"ok": False, "state": "junk",
                                           "error": "that file is not a YouTube Music cookie - export a fresh one"}, 400)
                rid = run_begin("", title)
                blog("transfer", f"run={rid} started")
                threading.Thread(target=_transfer_thread,
                                 args=(url, yt, title, privacy, rid),
                                 daemon=True).start()
                return self.send_json({"ok": True, "run_id": rid})
            if self.path == "/api/retry":
                if not run_retry_now():
                    return self.send_json({"ok": False, "error": "nothing is standing by"}, 409)
                blog("retry", "retry now requested")
                return self.send_json({"ok": True})
            if self.path == "/api/check":
                sp = check_spotify_cookie(payload.get("spotify_cookie", ""))
                yt_raw = payload.get("yt_cookie", "")
                if not normalize_cookie(yt_raw):
                    yt_raw = vault_load().get("yt_cookie", "")
                yt = cookie_check("yt", yt_raw)
                blog("check", f"spotify={sp['ok']} ytm={yt['state']}")
                return self.send_json({"ok": True, "spotify": sp,
                                       "ytm": {"ok": yt["ok"], "msg": yt["detail"] or yt["msg"],
                                               "state": yt["state"]}})
            if self.path == "/api/cookies/check":
                site = str(payload.get("site") or "yt").strip().lower()
                if site not in ("sp", "yt"):
                    return self.send_json({"ok": False, "error": "site must be sp or yt"}, 400)
                v = vault_load()
                cookie, src = v.get(site + "_cookie") or "", ""
                if not cookie:
                    cand = scan_disk_cookies()["best"].get(site)
                    if cand:
                        st = cookie_states().get(site) or {}
                        if st.get("state") == "removed" and st.get("hash") == cookie_hash(cand["raw"]):
                            blog("cookiecheck", f"{site}=none (removed)")
                            return self.send_json({"ok": False, "site": site, "state": "none",
                                                   "msg": "Not connected yet"})
                        cookie, src = cand["raw"], cand["source"]
                r = cookie_check(site, cookie, src)
                blog("cookiecheck", f"{site}={r['state']}")
                return self.send_json({"ok": r["ok"], "site": site, "state": r["state"], "msg": r["msg"]})
            if self.path == "/api/cookies/remove":
                site = str(payload.get("site") or "yt").strip().lower()
                if site not in ("sp", "yt"):
                    return self.send_json({"ok": False, "error": "site must be sp or yt"}, 400)
                vault_remove(site)
                blog("cookieremove", f"{site} removed")
                return self.send_json({"ok": True, "site": site, "state": "none"})
            if self.path == "/api/cookies/use":
                site = str(payload.get("site") or "yt").strip().lower()
                if site not in ("sp", "yt"):
                    return self.send_json({"ok": False, "error": "site must be sp or yt"}, 400)
                want = str(payload.get("file") or "").strip().lower()
                scan = scan_disk_cookies()
                cand = None
                if want:
                    for c in scan["all"].get(site) or []:
                        if c["name"].lower() == want:
                            cand = c
                            break
                else:
                    cand = scan["best"].get(site)
                if not cand:
                    return self.send_json({"ok": False, "state": "none",
                                           "error": "no valid cookie file found"}, 400)
                r = vault_save(site, cand["raw"], source=cand["source"])
                if not r["ok"]:
                    return self.send_json({"ok": False, "state": r["state"], "error": r["msg"]}, 400)
                blog("cookieuse", f"{site} from {cand['source']}")
                return self.send_json({"ok": True, "site": site, "state": "unverified", "msg": "saved",
                                       "source": cand["source"], "file": cand["name"]})
            if self.path == "/api/reset":
                if run_is_live():
                    return self.send_json({"ok": False, "error": "a transfer is already running"}, 409)
                run_reset()
                if not clear_created(str(payload.get("spotify_id", "") or "").strip()):
                    return self.send_json(
                        {"ok": False,
                         "reset_partial": True,
                         "error": "the run was cleared, but the playlist record could not "
                                  "be cleared - it is unchanged and may still be used on a "
                                  "later transfer"}, 500)
                blog("reset", "run + created link cleared; settings/vault/watch kept")
                return self.send_json({"ok": True})
            if self.path == "/api/vault":
                v = vault_load()
                meta = v.get("meta") if isinstance(v.get("meta"), dict) else {}
                state = v.get("state") if isinstance(v.get("state"), dict) else {}
                return self.send_json({
                    "ok": True,
                    "has_sp": bool(v.get("sp_cookie")),
                    "has_yt": bool(v.get("yt_cookie")),
                    "sp_len": len(v.get("sp_cookie") or ""),
                    "yt_len": len(v.get("yt_cookie") or ""),
                    "sp_state": state.get("sp", "none"),
                    "yt_state": state.get("yt", "none"),
                    "state": {k: state.get(k) for k in ("sp", "yt")},
                    "meta": {k: dict(meta.get(k) or {}) for k in ("sp", "yt")},
                    "refreshed": list(v.get("refreshed") or [])})
            if self.path == "/api/latch":
                if "yt_cookie" in payload and not normalize_cookie(str(payload.get("yt_cookie") or "")):
                    blog("latch", "yt empty rejected")
                    return self.send_json({"ok": False, "state": "empty",
                                           "error": "missing - upload the cookie file"}, 400)
                sp_raw = str(payload.get("spotify_cookie", "") or "")
                spr = (vault_save("sp", sp_raw) if sp_raw.strip()
                       else {"ok": False, "state": "none", "msg": "no Spotify cookie sent"})
                sp_bad = bool(sp_raw.strip()) and not spr["ok"]
                yt_raw = payload.get("yt_cookie", "")
                yt_saved = False
                if normalize_cookie(yt_raw):
                    r = vault_save("yt", yt_raw, source="manual")
                    if not r["ok"]:
                        blog("latch", f"yt {r['state']} rejected")
                        return self.send_json({"ok": False, "state": r["state"],
                                               "error": r["msg"], "sp": spr}, 400)
                    yt_saved = True
                if sp_bad:
                    blog("latch", f"sp {spr['state']} rejected" + ("; yt saved" if yt_saved else ""))
                    return self.send_json({"ok": False, "state": spr["state"],
                                           "error": spr["msg"], "sp": spr}, 400)
                blog("latch", "cookies latched to vault")
                if yt_saved:
                    return self.send_json({"ok": True, "state": "unverified", "msg": "saved", "sp": spr})
                return self.send_json({"ok": True, "sp": spr})
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
                if not save_settings(cur):
                    return self.send_json({"ok": False,
                                           "error": "could not save the setting - it will not "
                                                    "survive a restart",
                                           **_settings_payload(load_settings())}, 500)
                blog("settings", "saved")
                return self.send_json({"ok": True, **_settings_payload(cur)})
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
