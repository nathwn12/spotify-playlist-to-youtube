#!/usr/bin/env python3
"""sp2yt keyless web service - stdlib only. No API keys, no secrets stored.
Spotify: scrapes public embed __NEXT_DATA__ (optional user Cookie forwarded).
YTM: InnerTube WEB_REMIX + user-supplied Cookie (SAPISIDHASH derived server-side).
"""
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

# Test seams: every wait and every clock read the retry machinery reasons with
# goes through these two, so a test can exercise the backoff without spending it.
def _now() -> float:
    """Now, on the clock the retry machinery reasons with."""
    return time.time()

def _pause(seconds: float) -> None:
    """A short retry pause: one page retry, one add retry."""
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
    """The page's Retry now: end the standby wait and verify immediately.
    Only a standing-by run has a wait to cut short."""
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
    """Tab-separated cookie-file rows as field lists, '#HttpOnly_' prefix stripped."""
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
    """Which site a cookie blob belongs to: content markers win over the filename."""
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
    """Structural verdict on a cookie blob: valid | junk | empty | expired, plus a reason.
    A reason names cookie fields only - it never contains a value."""
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
    """Valid cookie files in the watched dirs, what was rejected and why, and the dirs scanned.
    `best` is the newest per site; `all` keeps every valid candidate per site, newest first,
    so a name the user picked still resolves when a newer export sits beside it."""
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
    """Compatibility wrapper: new code goes through cookie_site + cookie_shape."""
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
    """16 hex chars of sha256 over the normalized value - an identity, never a value."""
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
    """Validate, then latch a cookie vault-side, DPAPI-encrypted (this Windows user only).
    Plain values never touch disk. An empty, junk or expired blob writes nothing at all,
    so a bad paste can never replace the cookie already working."""
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
    """The stored cookies plus what a disk scan would adopt. The scan reads files outside the
    lock; every read-modify-write of the vault and its record happens inside it, so a completed
    removal can never be overwritten by an adoption that read the vault before it."""
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
    # re-latch: adopt a valid disk file only when it is newer than the value we already hold
    for site, cand in scan["best"].items():
        st = states.get(site) or {}
        h = cookie_hash(cand["raw"])
        # a rejected or removed value is never re-adopted by a scan; only the user clears the block
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
            # a different value is a different cookie: no verdict carries across the swap
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
    """Drop the stored value and remember which value was dropped: a later disk scan
    will not hand the same file back, and the user clears the block by choosing a file."""
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
    """Reset. Both removals and the deletion of both files are one critical section, so a save
    or an adoption that arrives mid-reset waits at the gate and lands whole, either before the
    reset or after it — it can never be deleted by the reset's own file removal."""
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
    """Shape, then network-check one cookie. Persists the verdict; a dead one is dropped.
    An empty probe persists nothing: there is no value, so there is no record to write.
    A verdict lands only on the record this call started from — if it changed while the
    network call ran (a remove, a different latch), the verdict goes back to the caller."""
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
    # The record may have moved while the network call ran, and a remove may land while this
    # verdict is being written: compare and write as one critical section shared with
    # vault_remove, so a check that began before Remove can never overwrite Remove.
    # The lock is never held across the network call above.
    with _COOKIE_LOCK:
        then = cookie_states().get(site) or {}
        if (then.get("hash"), then.get("checked_at")) != sig:
            # the record moved under this check: an old verdict is not evidence about the new value
            return {"ok": state == "connected", "state": state, "msg": msg, "detail": detail}
        _cookie_hook("check")
        src = source or _vault_source(site)
        held = _decode(_vault_read(), site)
        stray = bool(held) and cookie_hash(held) != cookie_hash(cookie)   # a probe, not the stored value
        if state in ("expired", "junk") and not stray:
            if not held or cookie_hash(held) == cookie_hash(cookie):
                vault_remove(site)   # auto-reject only the value that failed
            # the verdict is written after the removal so the same file is never re-adopted
        if not stray:
            _cookie_state_set(site, state, detail, src, cookie)
    return {"ok": state == "connected", "state": state, "msg": msg, "detail": detail}

def cookies_state() -> dict:
    """The /api/cookies payload: per-site state only, nothing that could reconstruct a value."""
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
    if "cannot be read" in m or "not a playlist record" in m:
        # Before the generic "cannot tell whether" branch below: that one is about a lost
        # create reply and tells the reader to delete the playlist. A record that cannot be
        # read may name that very playlist, so it is the one thing to keep.
        return {"kind": "write",
                "hint": ("The resume record cannot be read, so this app will not touch your "
                         "playlist. Copy that file somewhere safe before repairing it, check "
                         "your YouTube Music library for a playlist from this run, and add "
                         "what is missing by hand.")}
    if "returned no id" in m or "did not accept the create" in m or "cannot tell whether" in m:
        # A create whose reply was lost may have made the playlist that holds this work:
        # keep it, never delete it.
        return {"kind": "write",
                "hint": ("A playlist may have been made and this app cannot tell. Check your "
                         "YouTube Music library: if it is there, keep it and add what is "
                         "missing by hand (its link is in the address bar). No second "
                         "playlist is created.")}
    if "does not name the copies it was sent" in m or "not the plan they belong to" in m:
        # The record is the only copy of the log and the playlist link: the advice is the
        # manual check, never Reset and never removing anything.
        return {"kind": "user",
                "hint": ("That playlist's record does not name the copies it was sent, so "
                         "nothing was sent. Check the playlist in YouTube Music and add what "
                         "is missing by hand.")}
    if "older version of the app" in m:
        # First, and never the generic record branch: "delete created.json and start
        # again" would destroy the only copy of the log, the plan and the playlist link
        # that a person needs to finish this by hand.
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
    """kind/hint for a FATAL answer to an append, from the status alone.

    A 401 and a 403 are an auth-class answer and nothing more: the reply says the
    request was not authorised, and this app cannot tell an expired session from an
    account that may not edit that playlist. The hint names both instead of picking
    one - guessing "expired cookie" at a permission refusal sends a person to
    re-export a cookie that was never the problem, and a hint that overclaims is the
    same defect as a count that does."""
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
        raise RuntimeError(f"network: {type(e).__name__} {e}") from e

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
        raise RuntimeError("spotify keyless token unreadable (player api changed?)") from None
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
                # The album (and so the edition) travels with the row when the
                # player query carries it: it is what keeps a deluxe cut and an
                # album cut apart in the search key and the near-tie. Absent, the
                # row is matched exactly as before.
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


def _edition_sig(s: str) -> frozenset:
    """The edition words a title or an album name carries: deluxe, expanded,
    anniversary, remaster, single, ep, bonus. This is the signal that separates
    two pressings of one song (the album cut and the deluxe cut, the album and
    its single); it is read from the raw string because the edition is usually
    inside the brackets a fold would drop."""
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
    """Break a near-tie towards the edition the source row named.

    The same song on an album and on its single is two rows with one title, one
    artist and often one duration; what tells them apart is the edition the album
    name carries. When the source album names an edition and a candidate's own
    text names it too, that candidate is the pressing the row asked for - but only
    among candidates already within 0.05 of the leader, so this can neither promote
    a candidate the matcher would have refused nor reject one. Without album
    metadata (or without edition words in it) the order is untouched."""
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

# The legacy scorer above folds to ASCII, so any CJK title becomes "" on both
# sides and the row skips even when the right video is rank 1. These helpers
# keep the script, romanize kana deterministically, and compare characters.

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
    # digraphs: longest match wins, so these are looked up before the singles
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
    # loanword digraphs (katakana in practice, the shared table covers both)
    "\u3066\u3043": "ti", "\u3067\u3043": "di", "\u3068\u3045": "tu", "\u3069\u3045": "du",
    "\u3075\u3041": "fa", "\u3075\u3043": "fi", "\u3075\u3047": "fe", "\u3075\u3049": "fo",
    "\u3094\u3041": "va", "\u3094\u3043": "vi", "\u3094\u3047": "ve", "\u3094\u3049": "vo",
    "\u3057\u3047": "she", "\u3058\u3047": "je", "\u3061\u3047": "che",
    "\u3044\u3047": "ye", "\u3046\u3043": "wi", "\u3046\u3047": "we", "\u3046\u3049": "wo",
    "\u3064\u3041": "tsa", "\u3064\u3043": "tsi", "\u3064\u3047": "tse", "\u3064\u3049": "tso",
    "\u3059\u3043": "si", "\u305a\u3043": "zi", "\u304f\u3041": "kwa", "\u3050\u3041": "gwa",
}


def _kana_table() -> dict:
    """One table for both kana blocks: every hiragana entry plus its katakana
    twin (katakana is +0x60 from hiragana)."""
    table = dict(_KANA_BASE)
    for kana, romaji in _KANA_BASE.items():
        kata = "".join(chr(ord(c) + 0x60) if 0x3041 <= ord(c) <= 0x3096 else c for c in kana)
        table[kata] = romaji
    return table


_JA_ROMAJI = _kana_table()


def _fold_text(s: str) -> str:
    """Script-preserving fold: NFKC, casefold, drop bracket/promo/version zones
    exactly as _norm_text does, then keep Unicode word characters. Latin text
    lands on the same string _norm_text would give; CJK text survives."""
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
    """True when the two titles are spelled in different scripts."""
    return ((_has_cjk(want) and _has_latin(cand))
            or (_has_latin(want) and _has_cjk(cand)))


def _romanize_ja(s: str) -> str:
    """Hepburn-style kana romanization. Kanji and other characters pass through
    untouched: a kanji reading needs a dictionary this tool does not carry."""
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
    """Character similarity on the whitespace-free strings, so a romaji title can
    share a run ("ni"/"kakeru") with the kana it spells."""
    return difflib.SequenceMatcher(
        None, (a or "").replace(" ", ""), (b or "").replace(" ", "")).ratio()


def _strip_artist_prefix(title: str, artist: str) -> str:
    """YouTube cards often read 'ARTIST - TITLE'; drop that prefix before the
    title comparison. Unchanged when there is no such prefix."""
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
    """Title similarity that survives a script crossing: token overlap plus a
    containment bonus, character similarity when either side is CJK, and a
    kana-romanized comparison when either side is spelled in kana."""
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
    """Artist overlap over the wanted tokens, plus character similarity on the
    romanized strings when either side is spelled in kana (YTM often romanizes
    a Japanese artist while the title stays Japanese)."""
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
    """One candidate scored across scripts. d is None when duration is unknown;
    the weights drop the missing term instead of scoring it as a miss."""
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
    """The legacy three queries in order, then the kana romanization when the
    title is spelled in kana, then - last, only when the source row names its
    album - an album-bearing query. Kanji never gains a variant: without a reading
    it would only make the query worse. Deduped, empties dropped.

    The album variant goes last on purpose: it can rescue an edition-specific
    match (a deluxe track whose plain title also names the album version) without
    changing the outcome of any row the plain queries already identify. It is a
    widening of the search, never a gate: a row the album query cannot identify
    skips exactly as it would have."""
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
    """True when a candidate carries evidence besides its title: an artist that
    agrees, or a duration reading that is not plainly wrong.

    A title alone is the one feature the query itself supplied, so a candidate held
    up by it alone is the search engine's ranking, not this row's identity. Two
    features agreeing is a match; one feature agreeing with itself is a coincidence
    waiting to enter a playlist."""
    try:
        if float(c.get("a") or 0) > 0:
            return True
        d = c.get("d")
        return d is not None and float(d) > 0
    except Exception:
        return False


def _pick_best(query: str, want_title: str, want_artist: str, want_ms: int, cands: list,
               album: str = "") -> dict:
    """The best candidate, or a refusal that names what it saw.

    `score` is a similarity index in [0,1] built from title/artist/duration
    agreement. It is NOT a calibrated probability of being the right video, and
    no caller may present it as one: a 0.72 is "these features agree this much",
    nothing more. That is why a match is only taken on evidence - an exact fold,
    or an aligned title AND artist AND a duration that is not plainly wrong - and
    why a row that crosses scripts with no such evidence is skipped rather than
    guessed: rank 1 of a search is the search engine's opinion, not this row's
    identity.

    One feature is not evidence either. A candidate whose title matches exactly and
    whose artist does not agree at all, on a reply that carried no duration, is the
    title the query asked for and nothing else - a curator upload wearing it is the
    search engine's ranking, so it is refused by name ("title only") and the next
    query variant gets its turn. That is the shape that put an unrelated curator
    into a playlist at 0.70."""
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
    scored = _prefer_edition(scored, album)
    best = scored[0]
    evidence = _supported(best)
    if evidence and best["score"] >= 0.85:
        return best
    if evidence and best["score"] >= 0.60 and (len(scored) == 1
                                               or (best["score"] - scored[1]["score"]) >= 0.08):
        return best
    second = scored[1]["score"] if len(scored) > 1 else -1.0
    # Tier 2: aligned scoring, only when a script the legacy fold erases is in
    # play. Latin-only rows never reach this, so their outcomes cannot change.
    if _has_cjk(want_title) or any(_has_cjk(c.get("title", "")) for c in scored):
        aligned = [c for c in _score_aligned_candidates(want_title, want_artist, want_ms, cands)
                   if c.get("title")]
        if aligned:
            top = aligned[0]
            margin = top["score"] - (aligned[1]["score"] if len(aligned) > 1 else 0.0)
            if (_supported(top) and top["score"] >= 0.72 and top["t"] >= 0.45
                    and (len(aligned) == 1 or margin >= 0.10)):
                return {**top, "confidence": "aligned"}
            # No aligned winner. When the row really crosses scripts, rank 1 is
            # the only thing left and it is not evidence: the row is refused and
            # says why, instead of being guessed into the playlist. A row in one
            # script falls through to the ordinary "no confident match" refusal.
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


def _search_key(query: str, want_title: str, want_artist: str = "", want_ms: int = 0,
                album: str = "") -> tuple:
    """Cache key for one search row. The version signature is part of the key
    so a slowed lookup can never serve a sped-up row (or any other version), and
    the album is part of it so two editions that share a title, an artist and a
    duration cannot share one lookup: the same song on an album and on its single
    is two rows, and collapsing their lookups collapses their editions.

    The album enters as its folded name PLUS its edition signature, because the
    fold drops the brackets the edition lives in: "Night Drive" and "Night Drive
    (Deluxe Edition)" fold to one name and only the signature tells them apart."""
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
    """Identified match. Raises with best-candidate + score when nothing clears
    the bar; the score is a similarity index, never a probability.

    `tries` is how many requests this call may spend on a transport failure. Three by
    default for the matching pass, which can afford to wait out a blip; a caller with
    its own request budget (the soft second search) passes 1, so "one request per
    row" is the number actually spent. A cache hit spends none either way."""
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
    """One song, isolated. Legacy queries first, then kana romanization; first
    identified match wins. Never raises: no identified match -> videoId None
    (skip) with the reason.

    A candidate the matcher could not identify is never returned as a match: rank
    1 of a search is the search engine's opinion, and the row is skipped instead
    so nothing unproven enters the playlist. `album` is passed through to the
    query variants and the cache key when the source carries one."""
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
            # A reply flagged as a guess is not a match. The matcher no longer
            # produces one; this keeps a stale/other producer from writing an
            # unproven video into a playlist.
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
    """True when a skip says the search itself did not answer - a timeout, a closed
    connection, a 429 or a 5xx - so the song was never refused.

    A refusal about the song ("no confident match", "no ytm result") is an answer,
    and it is not one of these: asking it again would get the same answer, and a
    second pass that lowered the bar to get a different one would be the defect, not
    the fix."""
    low = (text or "").lower()
    if not low or "no confident match" in low or "no ytm result" in low:
        return False
    return _is_transport_text(low)

def _fatal_skip(text) -> bool:
    """True when a request was refused outright (bad request, dead cookie, gone
    playlist). The next row would be refused the same way, so a soft retry stops."""
    low = (text or "").lower()
    if "httperror" not in low and "forbidden" not in low and "unauthorized" not in low:
        return False
    return any(code in low for code in ("400", "401", "403", "404"))

def _second_search_skipped(items, max_rows: int = SECOND_SEARCH_MAX):
    """One more search for the rows whose first attempt never got an answer.

    Only a skip that says the wire failed is retried, and only with the row's own
    first query: nothing about the song changed between the two attempts, so the
    same question is asked again and the same evidence bar decides it.

    Bounded three ways, because a soft retry must not become a storm: a hard cap on
    rows, ONE request per row (`yt_search(..., tries=1)`, so the retry budget inside
    the search cannot multiply it), and a stop at the first row that still fails on
    the wire or is refused outright - a throttled API or a dead cookie answers every
    remaining row the same way, and a run must not spend its budget finding that out.
    Rows it never reaches keep the skip they already carry.

    items: [{"i": row position, "title", "artist", "duration_ms", "album",
             "skipped": the reason the first attempt left}]
    -> ({"<position>": result}, attempted positions, stop reason)"""
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
    """items: track dicts {title, artist, duration_ms} or plain query strings.
    Every item resolves independently; item failure is a skip, never a batch failure."""
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
    """Persist the settings. -> True when the bytes are on disk.

    A caller that acknowledges a setting must be able to tell a write that landed
    from one that did not: a page claiming a value the file never took is a promise
    the next transfer breaks."""
    try:
        with open(SETTINGF, "w", encoding="utf-8") as f:
            json.dump({k: d[k] for k in _SETTING_KEYS if k in d}, f)
        return True
    except Exception as e:
        blog("settings", f"write failed: {type(e).__name__}")
        return False

def _settings_payload(cur: dict) -> dict:
    """/api/settings, one key per setting."""
    out = {}
    for k in _SETTING_KEYS:
        if k == "privacy":
            out[k] = cur.get(k) or "PRIVATE"
        else:
            out[k] = cur.get(k) or ""
    return out

def clear_created(spotify_id: str = "") -> bool:
    """Forget the local created-playlist record(s) (created.json). -> True only when the
    record that was asked for is gone from disk (a missing file counts as gone).

    LOCAL ONLY: this forgets which YTM playlist ids belong to which
    Spotify ids. It never calls any YTM endpoint, so the remote
    playlists are untouched — a later transfer of the same Spotify
    link simply creates fresh instead of resuming/topping up.
    When spotify_id is given, only that key is dropped; other
    playlists keep their resume record.

    A rewrite that fails returns False and leaves the record as it was: the file is the
    only copy of the plan and the playlist link, so a caller that reports "reset" must be
    able to tell a record it removed from one still sitting there."""
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
    """YouTube Music answered the CALL with a verdict (400/401/403/404) and the run could
    not prove the playlist afterwards, so no result dict carries the status out.

    The status travels on the exception because the sentence that says "the rest could
    not be verified" is true of a dead cookie, a dead connection and a short read alike -
    reporting only that sentence hides the one thing the server actually said. kind/hint
    come from `_write_verdict`, which reads the status alone and picks no cause.

    `outcomes` is the sender's own {plan position: status}. It rides along so the rows can
    be settled by what the server said per copy instead of being flattened into one
    "unverified" sentence: a copy that came back refused is refused, a copy that may have
    been applied stays unknown, and a copy never sent stays unsent."""

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
    """Stop a run that cannot prove its playlist: raise the fatal verdict when the appends
    carried one, else the plain reason. Nothing is written here - the record already holds
    the write-ahead, and a stopped run must leave it exactly as it is."""
    fatal = sorted({str(s) for s in (attempts or {}).values() if is_fatal_status(s)})
    if fatal:
        raise FatalAppend(fatal[0], f"YouTube Music refused the append with {fatal[0]} — {msg}",
                          outcomes=attempts)
    raise RuntimeError(msg)


# The record's own version. A record written by this app always carries it (save_created
# stamps every record), so a record without it was written by a version that cleared its
# write-ahead before the playlist read proved the writes - and cannot say which copies it
# already sent. Letting it resume would re-add copies that may be on the playlist, so that
# path fails closed instead of retrying (see _LEGACY_UNRESUMABLE).
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
    """(safe, why) for a record written before RECORD_VERSION - verified, never assumed.

    Such a record cannot say which copies its run already wrote, so it may be used only
    when a COMPLETE read proves there is nothing ambiguous left to write: every copy its
    own plan asks for is on the playlist. Then the only writes left are tracks the source
    gained, which are new ids and cannot be second copies of anything, and the record's
    evidence can safely carry this protocol's version from then on.

    A record that says complete is not exempt: an older run interrupted while topping up
    left complete:true over writes whose outcomes it had already cleared. So the answer
    comes from a read of the playlist, not from the flag.

    Read only. A read that fails, that stops before the end, or that leaves any copy of
    the record's plan absent is not safe, and says which of those it was."""
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
    """The created-playlist record.

    A missing file is a first run and returns {}. A file that is present but unreadable
    raises `RecordUnreadable` - never an empty record - so every decision that could
    create or overwrite a playlist fails closed instead of guessing."""
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
    """Write created.json atomically: a temp file in the same folder, then a replace.
    Returns whether the record is durably on disk. A failed write must never look like
    a successful one - this record is what keeps a restart from re-sending a write
    nobody saw land - and it must never truncate the record it is replacing.

    It stamps NOTHING. A version stamped by whichever write happened to touch the file
    would promote a record whose evidence is still from an older protocol - a top-up's
    plan write would make an unsafe record look current. RECORD_VERSION is written where
    a record is BORN (a new playlist starts a new evidence set), and only there; a legacy
    record is promoted only after its writes are verified (see _legacy_safe)."""
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
    """Fresh auth headers per call — SAPISIDHASH goes stale within minutes."""
    yt_cookie = normalize_cookie(yt_cookie)
    return {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
            "Origin": YT_ORIGIN, "Referer": YT_ORIGIN + "/", "Cookie": yt_cookie,
            "Authorization": sapisid_hash(yt_cookie), "X-Origin": YT_ORIGIN}

CREATE_INLINE_MAX = 50  # playlist/create carries at most this many videoIds inline
TRANSFER_SLICE = 100    # source copies sent, then proved by one read-back, before the next slice
PLAYLIST_PAGE_MAX = 200  # browse pages per read: ~100-entry pages cover 20k tracks, well past the README's 5k
PLAYLIST_PAGE_TRIES = 2  # one retry per continuation page before the read is called short

def _shelf(node):
    """musicPlaylistShelfRenderer/_Continuation anywhere in a browse reply (shape
    drifts: a first browse wraps the shelf in contents, a continuation serves the
    same rows under continuationContents)."""
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
    """continuationCommand.token anywhere inside one shelf item (live WEB_REMIX shape)."""
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
    """continuationItems from an appendContinuationItemsAction anywhere in the reply.
    This is what a live WEB_REMIX playlist continuation actually returns: there is no
    shelf node in that reply at all, only the rows and the next token."""
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
    """(items, continuations, found) for one reply: a shelf node when the reply has
    one, else the continuation items an appendContinuationItemsAction carries.
    found False means the reply held no playlist rows in any known shape."""
    shelf = _shelf(j)
    if shelf is not None:
        return list(shelf.get("contents") or []), list(shelf.get("continuations") or []), True
    items = _continuation_items(j)
    if items is not None:
        return items, [], True
    return [], [], False

def _next_page_token(continuations, items) -> str:
    """The next page token in either shape YTM serves: the legacy
    continuations[].nextContinuationData.continuation field, or the live
    continuationItemRenderer -> continuationEndpoint -> continuationCommand -> token
    item that ends the page. "" means this was the last page."""
    for c in continuations or []:
        d = c.get("nextContinuationData") if isinstance(c, dict) else None
        if isinstance(d, dict) and d.get("continuation"):
            return str(d["continuation"])
    # A dedicated continuation item first, any other item only as a fallback: a
    # track row can carry endpoints of its own, and a stray token that is not the
    # page's would end the read early.
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
    """One browse reply, parsed."""
    req = urllib.request.Request(
        f"https://music.youtube.com/youtubei/v1/browse?key={YT_KEY}&prettyPrint=false",
        data=json.dumps(body).encode(), headers=yt_headers(yt_cookie))
    return json.loads(urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore"))

def yt_playlist_read(plid: str, yt_cookie: str) -> dict:
    """Every entry on the playlist, in order, repeats included, following the
    continuation token to the last page.
    -> {"entries": list|None, "complete": bool, "why": str}

    entries is None when even the first page could not be read. A continuation that
    stalls or repeats leaves complete False and entries holding only the pages that
    were proven, so no caller can mistake an unread tail for an empty one.

    Entries accumulate positionally and are never deduped: a playlist may hold the
    same videoId twice (the owner's does, 28 pairs), so a re-served page is rejected
    by its token, never by videoId identity."""
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
            # A reply with no playlist rows (a refusal, a page shape change) proves
            # nothing: report it unread, never as a playlist holding zero entries.
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
                # A row renderer we do not know may be a track: unread, never
                # skipped and never "end of list".
                return {"entries": out, "complete": False,
                        "why": f"unrecognised row renderer {shape} after {len(out)} entries"}
            v = mrl_video_id(mrl)
            if not v:
                # A recognised row we cannot name is a track we cannot place: a
                # caller that reads it as absent re-adds what the list holds.
                return {"entries": out, "complete": False,
                        "why": f"row without a parsable videoId after {len(out)} entries"}
            out.append(v)
        nxt = _next_page_token(continuations, items)
        if not nxt:
            if continuations or any(isinstance(i, dict) and "continuationItemRenderer" in i
                                    for i in items):
                # A continuation we cannot read says more rows follow; it cannot
                # say this page is the whole playlist.
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
    """Every entry on the playlist, in order, repeats included. None on any failure.
    A read cut short returns only the entries it proved — yt_playlist_read carries
    the completeness flag, and callers that report absence must use it.
    Reads playlist rows only — not recommendations or headers — so this is the real
    playlist length, unlike a bare videoId regex over the whole reply."""
    return yt_playlist_read(plid, yt_cookie)["entries"]

def _is_transport_text(s: str) -> bool:
    t = (s or "").lower()
    return any(h in t for h in _TRANSPORT_HINTS)

# Append outcomes. YTM itself answers "STATUS_SUCCEEDED" / "STATUS_FAILED"; the
# other two are ours, and they exist so no caller has to parse exception text.
STATUS_SUCCEEDED = "STATUS_SUCCEEDED"
STATUS_DUPLICATE = "STATUS_DUPLICATE"   # a second copy of an id the playlist holds
# Every append failure that is not a plain request verdict lands here: a timeout, a
# closed connection, a 5xx, and a 429. The endpoint that carries the write is
# unofficial, so none of those is proof the request was not applied - a 429 can be
# answered after the write was taken - and an append whose application is unproven
# is never sent again automatically.
STATUS_UNSURE = "STATUS_UNSURE"
_FATAL_HTTP = ("400", "401", "403", "404")  # a verdict, not a hiccup
_DUPLICATE_TOAST = "already in the playlist"

def is_unsure_status(st: str) -> bool:
    """True when an append's outcome is unknown: it may have been applied, and no
    read can prove it stayed out (a token-complete reply can be a stale snapshot
    taken before the write). Such a copy is never re-sent automatically."""
    return str(st or "").startswith(STATUS_UNSURE)

def is_fatal_status(st: str) -> bool:
    """True when YTM refused the call itself: bad request, dead cookie, or the
    playlist is gone. Retrying cannot fix any of those."""
    last = str(st or "").strip().rsplit(" ", 1)[-1]
    return last in _FATAL_HTTP

def _retryable_error(err: Exception) -> bool:
    """A transport failure worth a second try: a timeout, 429 or 5xx. HTTPError is
    a URLError subclass, so the code decides first: 400/401/403/404 are verdicts."""
    code = getattr(err, "code", None)
    if isinstance(code, int):
        return code == 429 or code >= 500
    return _is_transport(err)

def _is_duplicate_reply(body: str, status: str) -> bool:
    """YTM refuses a second copy of a videoId it already holds with HTTP 200,
    "status": "STATUS_FAILED", and an add-toast reading "This song is already in
    the playlist". That signature is the authority for the duplicate outcome; a
    plain STATUS_FAILED carries no such toast and stays a refusal."""
    return ("FAILED" in (status or "").upper()
            and _DUPLICATE_TOAST in (body or "").lower())


def yt_add_one(plid: str, vid: str, yt_cookie: str) -> str:
    """Append one entry. -> YTM's own status string ("STATUS_SUCCEEDED" /
    "STATUS_FAILED"), STATUS_DUPLICATE for a copy YTM already holds, or
    "STATUS_UNSURE <why>" for a failure whose application is unproven. Fresh
    SAPISIDHASH per call: it goes stale within minutes.

    Every add carries "dedupeOption": "DEDUPE_OPTION_SKIP", which is what asks
    YouTube Music to accept a second copy of a videoId the playlist already holds.
    A transfer is 1:1 with the Spotify playlist, always: the same song on an album
    and on its single is two source rows, so it is two entries, and no setting can
    turn that into one. Nothing else is attested, so no other dedupe option is sent.

    A failure that is not a plain request verdict is STATUS_UNSURE: a timeout, a
    closed connection, a 5xx, and a 429 as well. The write endpoint is unofficial, so
    none of those proves the request was not applied - a 429 can be produced after
    the write was taken - and nothing in here can tell. Such a copy is never success
    and never re-sent automatically: a complete read cannot prove it stayed out
    either, and a duplicate source row cannot be told from a repair. The run reports
    it unverified for the user to check. A 400/401/403/404 is a verdict, returned
    as-is so `is_fatal_status` ends the run instead of retrying."""
    act = {"action": "ACTION_ADD_VIDEO", "addedVideoId": vid,
           "dedupeOption": "DEDUPE_OPTION_SKIP"}
    b2 = json.dumps({"context": {"client": CLIENT}, "playlistId": plid,
                     "actions": [act]}).encode()

    def once():
        """-> (status, body, unsure). `unsure` says the request may have been
        applied: no reply arrived, or the reply was a failure we cannot read as "not
        applied". The body is kept: a refusal is an HTTP 200, and only the body says
        whether it was the duplicate refusal."""
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
                # Everything else is unknown, including a 429: for this endpoint a
                # rate-limit reply is not proof the write was refused unapplied.
                return st, "", True
            return st, "", True
        try:
            return str(json.loads(er).get("status", "")), er, False
        except Exception:
            return er[:120], er, False

    st, body, unsure = once()
    if unsure:
        # A lost or unreadable reply is not a lost add: the request may have been
        # applied, so re-sending is how a transfer ends up holding two copies of one
        # source row. Nothing here can tell, and no single complete read can either,
        # so the copy is reported unknown and never sent again by this run.
        blog("add", f"{vid} outcome unknown after {st[:60]} — not resending")
        return f"{STATUS_UNSURE} {st}".strip()
    if _is_duplicate_reply(body, st):
        return STATUS_DUPLICATE
    return st

COVER_MAX = 8 * 1024 * 1024  # playlist thumbnail bytes accepted for upload

def yt_set_playlist_cover(plid: str, cover_url: str, yt_cookie: str = "") -> dict:
    """Best-effort: give a new YTM playlist the Spotify cover.

    Downloads the image, uploads the bytes through the resumable playlist-thumbnail
    handshake (start -> X-Goog-Upload-URL -> upload, finalize -> encryptedBlobId),
    then attaches it with one browse/edit_playlist ACTION_SET_CUSTOM_THUMBNAIL call.
    Always returns {"ok": bool, "why": str} and never raises: some accounts get a
    403 here by design, and the transfer must not care."""
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

def _persist_fields(spotify_id, **fields) -> bool:
    """Additive write: created.json keeps every field it already holds, so the
    state-machine's progress never erases the resume record it rides on. Returns
    whether the record is durably on disk (True when there is no record to write).

    Three fields can only move one way, and are clamped here so no caller can undo
    what an earlier one proved: `expected_min` only rises, `unsure` and `unsure_ids`
    only grow, and `inflight` is the one list a caller owns (a slice ends, so its
    positions leave). That is what keeps a repair pass from writing an older floor over
    a raised one, or a new write-ahead from erasing an unknown an earlier slice recorded.

    `unsure_ids` is the durable half of that: a videoId whose write outcome is unknown,
    kept by ID so it survives a plan the song has left - a later plan that names the song
    again must still know its copy is unproven."""
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
    """The persisted record for this playlist, or {}: only for read-modify-write of
    the lists a caller owns."""
    if not spotify_id:
        return {}
    rec = load_created().get(spotify_id)
    return rec if isinstance(rec, dict) else {}

def _write_ahead(spotify_id, positions, floor, plan=None) -> bool:
    """Put a slice's positions on disk BEFORE its first add goes out, and mark the run
    incomplete in the same write. A close after this point leaves those copies looking
    unknown rather than unsent. Merges with whatever is already there, so a save that
    failed earlier cannot be papered over by the next slice.

    `plan` is the list those positions index. It is written in the same call, so the
    indices and the plan they mean are never persisted apart: a top-up against a fresh
    match would otherwise queue positions that point into an older record's plan, and a
    restart would freeze the wrong copies. The MULTIPLICITY of each queued id comes with
    it (`inflight_need`), because an unknown that reaches a later run with no recorded
    multiplicity can never be resolved - a complete read showing every copy would still
    leave the song frozen forever."""
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
    """Record what a slice established: the copies whose outcome is unknown join the
    unknown set (by position and by videoId, when the plan is known), the floor can only
    rise, and the positions this slice classified leave the write-ahead list. A position
    whose save never landed stays queued."""
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
    """Every videoId whose write outcome the record says is unknown: the durable ID set,
    plus the ids behind the saved positions.

    A record that carries positions but not the plan they index cannot be read at all -
    guessing which songs those positions name is how the wrong copy is frozen and the
    right one is re-sent - so that record fails closed."""
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
    """The per-id multiplicity each unknown was recorded against. Resolution is measured
    against this, never against a plan that has since shrunk: an unknown for the second
    copy of a song is not answered by a read that shows the first.

    Both halves count: `unsure_need` for a classified unknown, and `inflight_need` for a
    copy a crashed run had queued but never classified."""
    out = {}
    for src in ("unsure_need", "inflight_need"):
        for k, v in (((rec or {}).get(src) or {}) or {}).items():
            try:
                out[str(k)] = max(int(out.get(str(k), 0) or 0), max(0, int(v or 0)))
            except Exception:
                continue
    return out

def _unresolved_ids(unknown_ids, entries, need=None) -> set:
    """The unknown ids a COMPLETE read does not account for.

    A read proves MULTIPLICITY without attributing copies: when it holds at least as many
    copies of an id as were RECORDED as unknown, the doubt is answered - whatever produced
    those entries, the playlist has what the doubt was about. The bar is that recorded
    multiplicity alone: a plan that has since shrunk must not lower it (one visible copy
    would "resolve" the unknown second copy, and a later run would add a third), and a
    plan that has grown adds no doubt to the copies already recorded.

    An id with no recorded multiplicity is never resolved: the original evidence that
    would answer it was not kept, and clearing a doubt nobody can prove is how a later
    plan resends a copy that already landed."""
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
    """The persisted record of an interrupted run of this Spotify playlist, or
    None. Only a record that already holds a playlist id is one: a run that never
    created a playlist left nothing to resume."""
    if not spotify_id:
        return None
    rec = load_created().get(spotify_id)
    if isinstance(rec, dict) and rec.get("yt_id") and rec.get("complete") is not True:
        return rec
    return None

def _persisted_unknown(rec, intended) -> tuple:
    """(floor, frozen positions, unknown videoIds, recorded need) for a run picking up an
    existing record.

    The floor is what the earlier run's own evidence said the playlist must hold; the
    frozen positions are every copy in `intended` whose id the record says was written
    without seeing the answer - both the classified unknowns (`unsure`) and the
    write-ahead list of a slice it never finished (`inflight`).

    The saved positions index the plan the RECORD wrote; `intended` may be a different
    plan (a top-up runs against a fresh match). Unknowns are therefore resolved to their
    videoIds first (`_unknown_ids_in`) and frozen by ID in `intended`: the copy keeps its
    song across a rematch, and an id the new plan does not contain is still returned -
    with need 0, so it is never resolved and a later plan that names it again cannot
    resend it. A record whose positions cannot be read against a plan fails closed."""
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
    """Replace the record's unknown set with exactly these ids (and their positions in
    `plan`). Used when a complete read has RESOLVED some of them: the durable set must be
    able to shrink, or a resolved song stays blocked forever, and the write is one call so
    positions, ids and need never disagree."""
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
    """Adopt the plan this run writes against, with the unknown copies re-expressed in
    its own positions and kept by videoId.

    A top-up may run against a fresh match, so the record's plan, its unknown positions
    and the durable unknown IDs move TOGETHER: a saved position read against another plan
    names another song, and an id the plan no longer contains must survive as an id. One
    write, so no restart can ever see one without the other."""
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
        # Only a POSITIVE recorded multiplicity can resolve a doubt; an id with no entry
        # is never resolved (see `_unresolved_ids`), so a zero is never written here.
        "unsure_need": {str(k): int(v) for k, v in merged_need.items() if int(v) > 0},
        "inflight": [],
        "expected_min": max(int(rec.get("expected_min") or 0), int(floor or 0)),
    }
    return save_created(created)

def _resume_plan(rec, n_rows: int):
    """(slots, guesses) for an interrupted run, or None when its record cannot say
    what it sent.

    `slots` holds one entry per source row: the videoId that row was sent, or "".
    A resume rebuilds its rows from it and matches nothing. A record that cannot
    name its plan is not resumable at all: rebuilding its rows from a fresh match
    would reconcile them against copies the playlist never held, and 1:1 would
    append a second copy of every track."""
    if not isinstance(rec, dict):
        return None
    ids = [v for v in (rec.get("video_ids") or []) if isinstance(v, str) and v]
    if not ids or not n_rows:
        return None
    slots = rec.get("plan_slots")
    if not isinstance(slots, list) and len(ids) == int(n_rows):
        # A record written before the slots existed, holding one id per source row:
        # every row matched, in order, so the slots are the ids themselves.
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
        # From the moment the playlist exists, the plan it was sent is on disk:
        # a resume replays it and never matches the source again.
        fields["video_ids"] = [v for v in plan if isinstance(v, str) and v]
        fields["plan_slots"] = [v if isinstance(v, str) else "" for v in (plan_slots or [])]
        fields["plan_guesses"] = {str(k): str(v)[:80]
                                  for k, v in (plan_guesses or {}).items()}
    if not spotify_id:
        return True
    # A new playlist starts a new evidence set, so this is the one write that REPLACES
    # the old one: a stale floor or a stale unknown from a record whose playlist is
    # gone would otherwise make the new playlist look short from its first read. The
    # inline copies the create carried are the new set's own unknowns - all of them,
    # because no read has proved any of them yet.
    #
    # This is also where a record is BORN, so this is where the protocol's version is
    # written: nowhere else stamps it, so a record without it is honestly one an older
    # version left behind.
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
    """Where the run stands, so a close and a reopen resume the same state
    instead of starting a second playlist.

    The plan it was sent is part of that state: a resume replays it instead of
    rematching into a different plan. So is the least the playlist must hold
    (`expected_min`) and which copies were written with an unknown outcome
    (`unsure`): a resume that forgot either would treat a stale read as complete and
    re-send a copy nobody saw land."""
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
            # The durable half: an unknown outlives the plan it was recorded in, and the
            # multiplicity it was recorded against is what a later read must match before
            # the doubt can be cleared.
            fields["unsure_ids"] = ids
            fields["unsure_need"] = {v: slots.count(v) for v in ids}
    _persist_fields(spotify_id, **fields)

def _honour_retry_clock(rec, reason="waiting out the paused retry time") -> float:
    """A resume must not buy a shorter backoff than the interrupted run chose: wait
    out whatever of its persisted next retry time is still in the future, so a
    close and a reopen cannot be used to skip the wait and resend early.
    -> seconds waited (0 when nothing was due)."""
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
    """The first full read wins: a read that followed its continuations to the end
    AND holds at least `minimum` entries is returned at once, so the common case
    costs one pass instead of N re-reads of page one. A read that came back short
    but complete is retried — InnerTube browse reads go short straight after an
    inline create (an empty list was observed live) — and the longest of those wins.
    A read whose continuation failed is not retried here: it already retried its own
    page, and the pages it proved are the honest answer, returned with complete
    False so no caller scores the unread tail as absent.

    `minimum` is the least a complete read of this playlist must hold. A read that
    is token-complete but below it proves the playlist is short, never that the
    pages it served are all of it, so it is returned with complete False: the
    entries a stale snapshot hides are exactly the ones a caller would re-add.
    0 means no floor is genuinely known — the window right after a create, when the
    list is still filling — and then a short complete read is the honest answer.
    All None -> (None, False), and callers keep their read-back-failed errors.
    -> (entries|None, complete: bool)"""
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
    """Final ledger: the read-back multiset is the only source of truth.
    Present (unclaimed copy) -> landed, whatever the append said: a FAILED
    duplicate-reject of an inline-seeded id, or a timed-out-then-applied
    append, is still on the playlist. Absent -> refused when YTM said FAILED,
    else missing ("sent but not on the playlist at read-back"). SUCCEEDED but
    absent is missing, never landed: landed means read back, nothing else.

    complete False means the read stopped before the playlist ended, so it proves
    nothing about the tail: every absent copy is then unverified — never refused
    and never missing, because the pages that would prove it were never read.

    An absent copy of a videoId the playlist already holds is `duplicate` when --
    and only when -- YouTube Music said so: STATUS_FAILED plus the already-on-the-
    playlist toast is the whole proof, and `yt_add_one` reduces that signature to
    STATUS_DUPLICATE. The copy count only corroborates it. A plain STATUS_FAILED
    on a repeated id stays `refused`, and an absent copy nobody sent a verdict for
    stays `missing`: multiplicity is never a substitute for the server's answer.
    A verdict on the CALL (400/401/403/404) is a refusal too, never "sent but
    absent": the server answered and the copy was not added, and a read that did
    not show it was never going to.

    A copy whose write may have landed (STATUS_UNSURE) and that this read does not
    show is `unverified`, never `missing` and never blamed on a short read: the read
    cannot prove absence of a write it may not have seen, so the copy is unknown and
    says how to recover it."""
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
            # The write may have been applied and its reply was lost, or it was
            # accepted and no read can name the row it became. No read can prove it
            # stayed out, so the copy is unknown: unverified and named for recovery,
            # never missing, refused or re-sent - and never blamed on a short read,
            # which is a different uncertainty.
            out.append({"i": i + 1, "videoId": v, "outcome": "missing", "unverified": True,
                        "unsure": True,
                        "status": ("outcome unknown — check the playlist, then add by "
                                   "hand; not re-sent")[:80]})
            continue
        if is_fatal_status(st):
            # A verdict on the CALL is a refusal, and it does not depend on how much of
            # the playlist was read: the server answered and the copy was not added.
            # Filing it under "the read stopped short" instead hid the refusal - the
            # counts said "0 refused", which is the opposite of what arrived.
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
            # A plain STATUS_FAILED is a refusal; so is a verdict on the CALL itself
            # (400/401/403/404). The server answered, and the copy was not added, so
            # it is refused - never "sent but absent", which claims a write happened.
            out.append({"i": i + 1, "videoId": v, "outcome": "rejected", "status": st[:80]})
        elif st:
            out.append({"i": i + 1, "videoId": v, "outcome": "missing",
                        "status": (st[:80] + " — absent at read-back")[:80]})
        else:
            out.append({"i": i + 1, "videoId": v, "outcome": "missing", "status": ""})
    return out

def _done_summary(counts, n_pl, topped_up=None, sent=None, guessed=0, tail_unverified=0,
                  order_differs=False, unnameable=0):
    """The human done line: one line, and the seven counts in the shape the page
    reads. The counted head is exactly

        landed {added} of {source} · {unique} unique · {skipped} skipped ·
        {duplicate} duplicate · {unverified} unverified · {refused} refused

    and the sentence that follows it names the proof those counts were reconciled
    against - the read-back's holds count - so the line can never print N landed
    beside a different holds count without naming the split in the same sentence.
    A labelled guess and a topped-up run are named there too, never dropped.

    The denominator is `source`, the tracks the Spotify read returned, repeats
    included: the question the line answers is whether every source track made
    it. A distinct-id denominator contradicts the numerator the moment the 1:1
    toggle accepts a repeat copy ("landed 508 of 480" was truthful and
    meaningless); `unique` is reported beside it instead.

    `unverified` is one word for both halves of "present in no completed read": a
    copy absent from a read that reached the end of the playlist, and a copy the
    read never reached. `tail_unverified` is the second half alone, which is what
    decides whether the read itself stopped short."""
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
        # A repaired copy can only be appended, so it can land after a later copy:
        # the playlist holds every intended copy, in a different order than the
        # source. Say it, rather than let "verified" imply a faithful sequence.
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
        # The entries the read holds and this run cannot account for have two known
        # causes, and the line says which one this run has evidence for: an accepted
        # write whose row the read names differently (the run froze that copy), or a
        # row this run never asked for.
        why = ("writes this run made that no read names" if int(unnameable or 0) > 0
               else "outside adds?")
        return line + " \u00b7 " + " \u00b7 ".join(aside + [
            f"playlist holds {n} — {n - added} entries not attributed to this run "
            f"({why}) — refused rows proven absent from that read-back"])
    return line + " \u00b7 " + " \u00b7 ".join(aside + [
        f"playlist holds {n} — read-back short of {added} landed "
        f"(resume re-verifies, no re-transfer)"])

def _append_todo(plid, video_ids, todo, yt_cookie, hooks=None):
    """Send one add per position in `todo`, in the order given. Serial by
    construction: each add is one call with its own verdict, which is the only
    way a copy that repeats a videoId can be counted per copy. Stops at the first
    transport or fatal answer - hammering a throttled API re-adds what a lost
    reply already applied, and a dead cookie will not accept the next one either.
    -> {position: status} for the positions it actually sent."""
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
    """Every position a run must not send: the frozen ones, and every other position
    that carries one of their videoIds.

    Copies of one videoId are indistinguishable in a playlist read: an entry for that id
    may be the unknown write or the copy a run is about to send, so attribution is not
    available. Rather than guess which copy is which, no further copy of that id is sent
    by this run - the read count is what the run reports, and the shortfall is named."""
    base = set()
    for p in frozen or ():
        try:
            base.add(int(p))
        except Exception:
            continue
    ids = {intended[p] for p in base if 0 <= p < len(intended or [])}
    return {i for i, v in enumerate(intended or []) if i in base or v in ids}


def _copies_that_should_land(statuses) -> int:
    """How many of the statuses this slice received promise a new playlist entry.

    A copy the server refused (STATUS_FAILED), one it said was already there
    (STATUS_DUPLICATE), and one whose outcome is unknown promise nothing: the
    playlist is not expected to grow by them. Every other copy was accepted and
    must show up in the next read."""
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
    """Send the outstanding copies in slices of TRANSFER_SLICE, and prove each
    slice with its own read-back before the next one is sent.

    One slice is 100 source copies: small enough that a run says what it has
    verified long before it is over, large enough that the read-backs do not
    outnumber the adds. The order inside a slice, and the order of the slices,
    is the source's order, so the playlist ends up holding the plan in order.

    A slice is only followed by another slice when the read it bought shows that
    slice. Two things enforce that, because one is not enough:

      * the read's floor is raised by the copies this slice should have added, so a
        token-complete read that is a stale snapshot of the playlist from before the
        appends is *short* - it is retried, and it stops the run instead of
        authorising the next slice;
      * every copy this slice sent, and that the server did not refuse, must then
        be present in that read, counted BY ID and not by plan slot: the read must
        hold every copy it held before the slice plus the copies the slice sent. A
        total that happens to match while a specific copy is missing (an outside add
        padding the count) is not evidence.

    A read that never reached the end of the playlist proves nothing about its
    tail either, so no further slice is sent on it: appending off a short read is
    how a playlist ends up holding a track twice.

    `entries`/`complete` are the read that authorised this call (a create's seed
    read, or a resume's first read); `floor` is the least a later complete read
    must still hold.

    Verified progress is published only by a read that reached the end: a partial
    read is evidence about its own pages, and the final ledger accounts for that
    once, in the open - it never moves the live "added" counter mid-run.

    -> (attempts, entries, complete: bool, expected_min: int), where attempts
    is {position: status} for every copy this call sent, and `expected_min` is the
    least number of entries the playlist must hold now: every copy the server
    accepted, on top of what the authorising read proved. A caller that repairs
    the playlist later must start from that floor, or a stale read can authorise
    it to re-send the plan.

    `entries` is the read it was GIVEN, or the last good read-back, and is never None:
    a failed read-back clears `complete` and leaves the previous read in place, because
    "the read died" and "the playlist is empty" are different facts. Callers guard the
    first read themselves; there is no third state to report here.

    With `spotify_id`, each slice is written ahead: its positions go on disk before
    the first add goes out, and the floor and any unknown outcome replace them after
    it. A close mid-slice therefore resumes knowing that those copies may already be
    on the playlist, instead of treating them as never sent. A write-ahead that cannot
    be saved stops the run BEFORE it writes: a run whose record is not durable cannot
    tell a restart what it sent, so it is not allowed to send."""
    attempts = {}
    on_verified = (hooks or {}).get("on_verified")
    on_phase = (hooks or {}).get("on_phase")
    todo = [int(i) for i in (todo or [])]
    # The floor is what the run's own evidence says the playlist must hold: whatever a
    # previous run persisted, and never less than the read that authorised this call
    # actually showed. A stored floor lower than that read would let a later, shorter
    # read pass as complete.
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
        # Write ahead: these positions are about to be sent, and a close before
        # their outcomes are on disk must leave them looking unknown, not unsent.
        # A record that cannot be saved is a run that may not write: without it a
        # restart cannot tell what was sent, and guessing is how a copy lands twice.
        if not _write_ahead(spotify_id, chunk, expected_min, plan=video_ids):
            raise RuntimeError(
                "the resume record could not be written to disk, so the transfer stopped "
                "before sending anything further — check that the app data folder is "
                "writable, then start the transfer again")
        # What the authorising read holds, by id, BEFORE this slice's writes go out:
        # the proof below needs the read as it stood, not the read-back that has the
        # slice in it.
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
        # Record the unknown outcomes now, but leave this slice's positions QUEUED until
        # the read-back has proved them. Clearing them here - before any read - was a
        # crash window in which a write the server had already accepted came back as a
        # write never attempted, and the resume re-sent it.
        #
        # The save is checked. An unknown that is not on disk is lost: the next save
        # reads the record back, merges its own (empty) unknown set over it, and writes
        # the doubt away - so a run may not go on past a save it could not make.
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
            # A slice is proved by MULTIPLICITY, never by which slot of the plan a
            # read's entry is attributed to. Copies of one videoId are
            # indistinguishable in a read, so asking whether slot i landed reports a
            # copy that DID land as absent whenever an earlier occurrence of its id
            # was never sent (a refused or frozen copy takes the entry first) - and
            # that falsehood stops the transfer on a slice that was fine. What a read
            # can prove is per id: for every id this slice had accepted, the read
            # holds every copy the authorising read held PLUS the copies this slice
            # sent. A copy the chunk never sent promises nothing and is not counted.
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
        # A read has answered the slice now, so its positions can leave the queue - but
        # only the ones it SETTLED. A position with a final verdict (refused, duplicate,
        # unknown reply) will never be sent again, so it is settled whatever the read
        # says. A copy this slice accepted stays queued unless the chunk was proved: if
        # no complete read shows it, the next run must inherit the doubt, not the copy.
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
            # Verified progress: the rows this completed read proves are landed
            # now, not when the whole run ends.
            try:
                on_verified(_reconcile(video_ids, entries, attempts, True))
            except Exception:
                pass
        if not complete or stopped or not chunk_proven:
            break
    return attempts, entries, complete, expected_min

def _seed_ledger(video_ids, entries):
    """One ledger row per requested copy for the inline seed (no attempts yet)."""
    return _ledger(video_ids, entries, {})

def _order_preserved(plan, entries) -> bool:
    """True when the plan appears in the playlist in the plan's own order.

    `edit_playlist` can only append, so a copy repaired after a later copy already
    landed sits at the end: plan [A,B,A,C] repaired from [A,C] becomes [A,C,B,A].
    Nothing safe can rewrite a playlist in place, so this is the check that stops
    the run claiming a faithful transfer when the copies are all there but in a
    different order. An entry the plan does not ask for is ignored; the plan's own
    copies must appear as a subsequence, repeats included."""
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
    """The eight integers /api/run serves as `counts`, each read from what the
    run observed: {source, unique, added, skipped, duplicate, unverified,
    refused, pending}.

    `source` is every track the Spotify read returned, repeats included - the read
    number, never a sum of the others, so a capped or resumed run still reports
    the real one. `unique` is the distinct videoIds of the intended plan, counted
    through _repeat_count so the repeat information lives in one place.

    `added` is verified progress and nothing else: a copy a COMPLETE read-back
    proved present. `pending` is the other half of the same honesty: a copy that
    was sent (or is being sent) and is not proved either way yet. It is counted
    apart so no caller reads a sent copy as landed, and none reads it as never
    attempted. `unverified` is a copy absent from a completed read, or one the
    read never reached.

    A row that has not been sent yet (queued, matched) is in no bucket: it has
    not been attempted, and inventing a bucket for it would be the same lie in
    the other direction. So added+skipped+duplicate+unverified+refused+pending
    is the attempted shape mid-run, and the reconciled total once the run ends."""
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
    """Recount every counter /api/run serves from the run's own rows. The caller
    holds _RUN_LOCK. One place decides how a row state becomes a count, so the
    counters can never drift from the ledger the page renders - and a row that may
    be on the playlist is never counted as one that was never attempted."""
    rows = list(RUN.get("rows") or [])
    counts = _counts_from_rows(int(RUN.get("total", 0) or 0), rows)
    # A copy that was sent and not yet read back is pending, not unverified: it is
    # not proven absent, it is not proven present, and it is not untouched. The
    # count comes from the rows, in _counts_from_rows, so the page's pending chip
    # and the rows in flight cannot disagree.
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
    """Rows that were sent at least once, read from the run's own counts.

    `attempted` is progress for the page, so it must include a copy that is in
    flight (pending) or proven absent: a row the run has touched. A row still
    queued or matched is not attempted, and counting it would claim work the run
    has not done."""
    c = (snap or {}).get("counts") or {}
    return sum(int(c.get(k, 0) or 0)
               for k in ("added", "skipped", "duplicate", "unverified", "refused", "pending"))


def _stop_rows_locked(msg: str) -> None:
    """A run stopped early. The caller holds _RUN_LOCK.

    A row whose copy may already be on the playlist is unverified, never `skipped`:
    a matched row rode the inline create, an appending row was sent, and either add
    may have been applied before the failure. `skipped` says the copy was never
    attempted, and that is a claim this run cannot make. The unproven shape is the
    one every other path uses and the page reads - absent from no completed read,
    flagged unverified - so the row chip and the count agree."""
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
    """Give every row whose append came back with a VERDICT its own terminal state, before
    `_stop_rows_locked` flattens whatever is still open into "unverified".

    A 400/401/403/404, a plain FAILED, or a duplicate toast is the server saying it did not
    add that copy: the row is `refused` (or `duplicate`), and calling it unverified while
    the status beside it says 401 is a contradiction inside one row. Anything else - an
    accepted add no complete read proved, or an unknown reply - is left open on purpose, so
    `_stop_rows_locked` keeps it unknown rather than inventing a refusal.

    Returns how many rows were settled. The caller holds _RUN_LOCK."""
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
    """True only when the playlist is proven empty: one COMPLETE read that held no
    entries, confirmed by a second complete read.

    Neither half alone is proof. A read whose continuation failed proves nothing
    about its tail, and a browse served straight after a create comes back
    token-complete and holding nothing for a playlist that does hold rows
    (observed live) - so a single empty complete reply may not authorise a
    delete either."""
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
    # A create is a write with no id until it answers. Its intent goes on disk first, so
    # a lost reply or a crash leaves a record that says "a create was attempted" instead
    # of nothing at all - and a later run refuses to make a second playlist over it.
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
        # The reply was lost: the playlist may exist. Never advise a retry - a retry is
        # how a second playlist gets made - and never guess an id. Keeping that playlist
        # is the point: if it is there it holds the copies this create carried, so the
        # advice is to keep it and finish it by hand, never to delete it.
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
    # The create's own write is the inline batch, and a repeat inside it cannot be
    # ATTRIBUTED: the create carried a second copy of that id and a read cannot say which
    # copy it kept. A complete read can still prove MULTIPLICITY, though - if it holds as
    # many copies of the id as the plan asks for, the doubt is answered - so nothing is
    # frozen until the first read has been seen (below).
    seen, dup_pos = set(), []
    for pos, v in enumerate(inline):
        if v in seen:
            dup_pos.append(pos)
        seen.add(v)
    dup_ids = {video_ids[p] for p in dup_pos}
    # The create's own write is the inline batch: the playlist was accepted holding
    # those ids, so the floor from this moment is their count. It is on disk before the
    # read-back, or this run may not go on: a restart that forgot the inline floor would
    # read a short playlist as an empty plan and append the lot.
    #
    # EVERY inline copy is unknown at this instant, not only the repeats: no read has
    # looked at the playlist yet, so none of those writes is proved, and a process that
    # dies here must leave all of them looking uncertain. The first complete read below
    # narrows this set to the copies it does not account for - a set that is never
    # cleared by anything but a read.
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
    # The create accepted these distinct ids, so a complete read must hold them:
    # anything less is a stale snapshot of a playlist that is still filling.
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
            # A read that stopped before the end of the playlist proves its own pages
            # and nothing about the tail, so it publishes no verified row: mid-run,
            # `added` only moves on a read that reached the end, and a partial read's
            # presence is accounted for once, by the final ledger, in the open.
            try:
                hooks["on_seed"](seed_ledger)
            except Exception:
                pass
    else:
        blog("create", f"id={plid} seed read stopped short — no row published as landed")
    todo = [int(e["i"]) - 1 for e in seed_ledger if e["outcome"] != "landed"]
    # The read settles as much as it can: an id whose multiplicity the read already
    # accounts for is answered, and only the ids it does not account for stay unknown.
    # "Accounts for" is measured against the multiplicity the unknown was recorded with,
    # so a plan that later shrinks cannot answer it with a single visible copy.
    # Which inline copies a COMPLETE read does NOT account for. An inline copy the read
    # does not show is not a copy the create failed to write - the create's reply named
    # the playlist, so the batch was taken - and it is not a copy that was never sent.
    # It is a copy the read cannot name (a read is padded with rows, so its length still
    # matches while a specific id is gone), and appending it is one source row becoming
    # two entries. A read that stopped short proves nothing about any of them, so the
    # whole batch stays unknown until a read can clear it.
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
        # The create carried these copies and a complete read cannot name the rows they
        # became: they are unknown, not absent, and this run may not send them again.
        blog("create", f"id={plid} {len(withheld)} inline copy(ies) the read does not "
                       f"show — never re-sent: "
                       + ",".join(str(p + 1) for p in withheld[:8]))
        todo = [p for p in todo if p not in inline_unknown]
    if not first_complete:
        # A read that never reached the end proves nothing about its tail:
        # appending off it is how a playlist ends up holding a track twice.
        blog("create", f"id={plid} read incomplete — {len(todo)} append(s) withheld")
        todo = []
    if on_phase:
        try:
            on_phase("append")
        except Exception:
            pass
    # The rest of the plan goes out in slices of TRANSFER_SLICE, each proved by
    # its own COMPLETE read-back before the next slice is sent: progress is what
    # the playlist shows, not what a queue claims.
    attempts, entries, entries_complete, expected_min = _append_verify(
        plid, video_ids, todo, yt_cookie, hooks, first, first_complete,
        spotify_id=spotify_id)
    if on_phase:
        try:
            on_phase("verify")
        except Exception:
            pass
    # `_append_verify` returns the read it was given, never None: a failed read-back only
    # clears `complete`. The guard that used to sit here could not fire, and its sentence
    # ("final read-back failed") was a second way to lose the reason the run stopped.
    # `first is None` above is the real gate, and it stays.
    # The inline copies a repeat made unattributable are carried as unknown outcomes,
    # so a read that does not show one files it unverified (with the manual recovery)
    # rather than as a proven absence this run would send again.
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
    # complete is a claim about the playlist, so it may only be made by a read that
    # reached the end and found every intended copy: a copy YTM refused is a source
    # row the playlist does not hold, and a partial read proves nothing about its tail.
    _persist_final(spotify_id, plid, url, title, len(video_ids),
                   added == len(video_ids) and entries_complete and not inline_ids)
    if not added:
        if _proven_empty(plid, yt_cookie, entries, entries_complete):
            cleaned = yt_delete_playlist(plid, yt_cookie)
            _raise_outcome("nothing landed (playlist reads back empty"
                           + (", husk deleted" if cleaned else ", husk cleanup failed — delete it manually")
                           + f"). {rejected} refused by YTM, {missing} missing — see server.log add lines",
                           attempts)
        # A dead cookie makes the read-back fail too, so the sentence about an
        # unverified rest is all this path could say - and it used to say exactly that
        # while the 401 that caused it went unreported, under a hint that guessed the
        # cookie had the wrong scope. The verdict travels now.
        _raise_outcome(f"the playlist is at {url} and the rest of what was sent could not be "
                       f"verified ({rejected} refused by YTM, {missing} unverified because the "
                       f"read-back that follows used the same auth — see server.log add lines)",
                       attempts)
    return {"playlistId": plid, "url": url,
            "added": added, "sent": len(video_ids), "rejected": rejected,
            "missing": missing, "duplicates": duplicates, "repeats": repeats, "ledger": ledger,
            "attempts": dict(attempts),
            "entries": entries, "read_complete": bool(entries_complete),
            # A verdict on the CALL ends the run where it was given: the caller must not
            # go on to a repair pass and ask the same dead cookie for another write.
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
    # The interrupted run left a retry time behind. Nothing may be sent before it:
    # a resume that appends first turns a restart into a way round the backoff.
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
    # A resume knows what the interrupted run proved: the least the playlist must
    # hold, and which copies it wrote with an unknown outcome. Both come off disk,
    # because neither can be re-derived from a read - a read that does not show an
    # unknown write is exactly the stale page that would re-send it.
    floor, frozen, unknown_ids, unknown_need = _persisted_unknown(rec, video_ids)
    entries, entries_complete = _read_back_retry(plid, yt_cookie, minimum=floor)
    if entries is None:
        raise RuntimeError(f"playlist created at {url} — inline copy landed but the rest could not be verified (read-back failed)")
    if unknown_ids and entries_complete:
        # A complete read that holds as many copies of an id as the unknown was recorded
        # against has answered the doubt for that song; only the rest stay unknown.
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
            # Same rule as a create's seed: a read that stopped before the end proves
            # no row landed mid-run - the final ledger accounts for what it holds.
            try:
                hooks["on_seed"](seed_ledger)
            except Exception:
                pass
    else:
        blog("resume", f"id={plid} seed read stopped short — no row published as landed")
    todo = [int(e["i"]) - 1 for e in seed_ledger if e["outcome"] != "landed"]
    withheld = [p for p in todo if p in frozen]
    if withheld:
        # The previous run wrote these and never saw the answer (or died mid-slice):
        # they are unknown, not absent, and a resume may not send them again.
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
    # As in `_create_new`: the sender returns the read it was given, so this guard could
    # not fire; `entries is None` above is the real gate.
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
    """One implementation, two entry points: sync /api/create-playlist and the
    async /api/transfer worker. Never dedupes video_ids: every source occurrence
    is one intended copy, so the same song on an album and on its single is two
    copies, and the adds ask YouTube Music to keep both.

    `plan_slots` (one entry per source row) and `plan_guesses` are persisted with
    the create, so an interrupted run can be resumed from its own plan."""
    yt_cookie = normalize_cookie(yt_cookie)
    ids = [v for v in (video_ids or []) if isinstance(v, str) and v]
    if not ids:
        raise ValueError("no videoIds to send")
    if spotify_id:
        rec = load_created().get(spotify_id)
        if rec and not rec.get("yt_id") and rec.get("create_pending"):
            # A create was attempted and its reply never named a playlist. It may exist.
            # Creating again would make a second one, so this stops for a person - and the
            # playlist it may have made is the only copy of that work, so it is kept.
            raise RuntimeError(
                "an earlier create for this playlist may have made a YouTube Music playlist "
                "and its record has no playlist id, so this app cannot tell whether it "
                "exists. Nothing was sent. Check your library: if a playlist with this title "
                "is there, keep it — its link is in the address bar, and adding what is "
                "missing by hand finishes the transfer. This app will not create a second "
                "one while it cannot tell which happened")
        if rec and rec.get("yt_id"):
            if int(rec.get("record_version") or 0) < RECORD_VERSION:
                # Verified, never assumed: a record from a version that cleared its queue
                # before proving the write may be used only when a complete read shows
                # every copy its own plan asks for - then there is nothing ambiguous left
                # to send. The flag `complete` is not that proof: an older run
                # interrupted while topping up left complete:true over writes whose
                # outcomes it had already cleared.
                safe, why = _legacy_safe(rec, rec["yt_id"], yt_cookie)
                blog("legacy", f"{rec['yt_id']} {'usable' if safe else 'not used'}: {why}")
                if not safe:
                    raise RuntimeError(_LEGACY_UNRESUMABLE.format(
                        url=(rec.get("url")
                             or f"https://music.youtube.com/playlist?list={rec['yt_id']}"),
                        gap=why))
                # Only now, and only because a read proved the ambiguity is gone, does the
                # record carry this protocol's version - so the verification is paid once.
                if spotify_id and not _persist_fields(spotify_id,
                                                      record_version=RECORD_VERSION):
                    blog("legacy", f"{rec['yt_id']} verified but the version stamp "
                                   f"could not be saved — it will be verified again")
            if rec.get("complete") is True:
                # A record that says complete is still not a licence to append blindly:
                # a run interrupted while topping up leaves it complete until the end, so
                # the unknown copies it persisted must be honoured here too.
                top_floor, top_frozen, top_ids, top_need = _persisted_unknown(rec, ids)
                # The record's plan and its unknown positions move together: the unknown
                # copies are re-expressed in THIS plan's positions and kept by id, in one
                # write, before the first send and before the early "nothing to do"
                # return, so no later read can see a saved position against another plan
                # and an id the new plan dropped is never forgotten.
                if spotify_id and not _adopt_plan(spotify_id, ids, plan_slots, plan_guesses,
                                                  top_floor, top_frozen, top_ids,
                                                  need=top_need):
                    raise RuntimeError(
                        "the resume record could not be written to disk, so the top-up "
                        "stopped before appending — check that the app data folder is "
                        "writable, then start the transfer again")
                # The read must reach what the LAST run's evidence proved, not what this
                # run intends: a plan that has grown (a track added to the source) can
                # never be held yet, and demanding it would refuse every new track.
                entries, entries_complete = _read_back_retry(
                    rec["yt_id"], yt_cookie, minimum=top_floor)
                if entries is None:
                    entries = []
                if entries is not None:
                    seed = _reconcile(ids, entries, {}, entries_complete)
                    todo = [int(e["i"]) - 1 for e in seed if e["outcome"] != "landed"]
                    # A complete read that holds as many copies of an id as the unknown
                    # was recorded against has answered that doubt; only the rest stay
                    # unknown. The bar is the RECORDED multiplicity, not the current plan
                    # count, so a plan that shrank cannot resolve a second copy with one
                    # visible entry.
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
                        # Nothing to send, but an unknown copy is not proof of a whole
                        # playlist, and neither is a partial or below-floor read: the
                        # record may only say complete when the read itself is complete,
                        # every copy was read back, and none is unknown.
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
                    # The plan and its re-expressed unknown set were adopted above, before
                    # the read; the sender writes ahead against that same plan.
                    attempts, entries2, entries2_complete, expected_min = _append_verify(
                        plid, ids, todo, yt_cookie, hooks, entries, entries_complete,
                        floor=max(top_floor, 0), spotify_id=spotify_id)
                    if on_phase:
                        try:
                            on_phase("verify")
                        except Exception:
                            pass
                    # Same invariant as above: the sender returns the read it was given,
                    # and `entries` was already normalised to a list before the call.
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
                            # A verdict on the CALL ends the run where it was given, here
                            # too: without it the caller goes on to the repair pass and
                            # asks the same dead cookie for another write.
                            "fatal": sorted({str(s) for s in attempts.values()
                                             if is_fatal_status(s)}),
                            "unsure": sorted(top_frozen | {int(p) for p, st in attempts.items()
                                                           if is_unsure_status(st)}),
                            "playlist_count": len(entries2) if isinstance(entries2, list) else 0,
                            "expected_min": max(int(expected_min or 0), top_floor),
                            "topped_up": newly}
            else:
                # The interrupted run's plan and toggle are part of its state: replay
                # them, so a resume cannot rematch into a different plan (its
                # playlist was built from this one). A record that cannot name its
                # plan is not resumable: adopting the fresh match instead is how
                # copies already on the playlist get appended.
                saved = [v for v in (rec.get("video_ids") or []) if isinstance(v, str) and v]
                if not saved:
                    raise RuntimeError(_NO_PLAN_MESSAGE.format(url=rec.get("url") or rec.get("yt_id", "")))
                ids = saved
                return _resume_incomplete(rec, yt_cookie, title, ids, spotify_id, hooks, version_sigs)
    r = _create_new(yt_cookie, title, description, ids, privacy, spotify_id, hooks, version_sigs,
                    plan_slots, plan_guesses)
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

RUN_RETRY_MAX = 6  # retries per run: the budget is bounded by policy, not by luck
# One convergence pass is a complete read-back plus the repair it proves. The
# budgeted stalls already stop a run at RUN_RETRY_MAX, so this bound is for a
# loop that is not standing by at all: twice the budget, and it only fires on a
# loop that ignores both the counter and the no-progress proof below.
CONVERGE_MAX_PASSES = 2 * RUN_RETRY_MAX
_RETRY_DELAYS = (30.0, 60.0, 120.0, 300.0, 600.0, 1200.0)  # 30s, 1m, 2m, 5m, 10m, 20m
_RETRY_CAP = 1200.0
_RETRY_JITTER = 0.2

def _retry_delay(attempt: int) -> float:
    """Seconds to wait before retry `attempt` (1-based), with +/-20% jitter and a
    20m cap, so a wall of runs cannot march in lockstep."""
    base = _RETRY_DELAYS[min(max(int(attempt or 1), 1), len(_RETRY_DELAYS)) - 1]
    return max(1.0, min(_RETRY_CAP, base + random.uniform(-base * _RETRY_JITTER,
                                                          base * _RETRY_JITTER)))

def _standby_wait(seconds: float):
    """Wait where the transfer already waits: inside its own thread. The page is
    not blocked; it polls /api/run for next_attempt_at and counts down itself.
    /api/retry sets RUN_WAKE and ends the wait early."""
    RUN_WAKE.wait(timeout=max(0.0, seconds))
    RUN_WAKE.clear()

def _unproven(video_ids, ledger) -> list:
    """The copies a first pass could not prove, in plan order: what a repair pass
    must carry as its residual even before it can complete a read of its own."""
    out = []
    for i, v in enumerate(video_ids or []):
        e = ledger[i] if i < len(ledger or []) else None
        if isinstance(e, dict) and e.get("outcome") != "landed":
            out.append(v)
    return out


def _refused_positions(attempts, ledger) -> list:
    """Positions whose copy the server refused in the first pass (duplicate toast or a
    plain refusal) and that is still absent: a repair pass must not re-send THOSE
    copies, because the verdict is already in.

    Keyed by position, never by videoId: a plan may hold the same id twice, and a
    verdict on one occurrence says nothing about another occurrence that was never
    sent. Blocking the id would deny a write the server might accept."""
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
    """Realign the playlist to the intended copies: verify, repair, verify again,
    bounded and quiet.

    Every pass begins with a COMPLETE paginated read-back. A read that never
    reached the end proves nothing about its tail, so nothing is appended on it -
    appending off a short read is exactly what double-adds.

    The retry set counts COPIES, not ids. A plan may hold the same videoId twice
    (the same song on an album and on its single); when the playlist holds it once,
    one copy is absent and re-sending it - with the skip every add carries - is the
    repair. Counting distinct ids made that a false success: [A,B,A,C] against
    [A,B,C] "had nothing absent", so the run persisted complete over a playlist
    missing a row. The repair follows the plan's own order, so the missing earlier
    copy is sent before a later one.

    The repair is the same sender as the first pass: `_append_verify`, which sends
    at most TRANSFER_SLICE copies and proves them with their own complete read
    before the next slice goes out. One unbounded batch would lose that proof - a
    stale read is caught per slice, and the floor rises by the copies each slice
    promised - and it would also lose the per-copy accounting: two copies of one
    videoId are two sends with two verdicts, so the statuses this pass returns are
    keyed by the copy's position in the plan, never by its videoId.

    A repaired copy can only be appended, so it can land after a later copy: the
    converged playlist may hold every copy in a different order. That is not
    fixable in place and is not hidden - `_order_preserved` reports it, and the run
    says so instead of claiming a faithful transfer.

    `refused` and `unsure` name copies by POSITION in the plan, because a plan may
    hold one videoId several times and each occurrence is its own write: a verdict on
    one copy never blocks another.

    `refused` copies were refused by the server (duplicate toast or plain refusal):
    re-sending them is the spin this pass exists to avoid, so they stay named as a
    residual.

    `unsure` copies may have been written and their outcome is unknown, and so is any
    such copy this pass sends itself. Such a copy is frozen for the rest of the run: a
    complete read that does not show it is not proof it stayed out - the reply can be
    a snapshot taken before the write - and a second copy of a source row cannot be
    told from a repair. No append failure is retried here, not even a 429: for this
    endpoint a throttling reply is not proof the write was refused unapplied. The run
    ends `gaveup` naming them, so they land as `unverified` rows with a manual
    recovery rather than a guessed resend. The frozen positions and the accepted floor
    are written to the resume record, so a close and a reopen cannot turn an unknown
    write into a fresh one.

    Bounded twice, because the append's reply is never the truth. `prev_present` is
    the copy count a COMPLETE read has already proved: a pass whose repair added
    nothing to that count proved nothing new, so the repair it just sent was
    dropped and progress is zero. That is detected on the read, not waited out on a
    count: one standby names the stall, a second no-progress pass gives up carrying
    the residual. CONVERGE_MAX_PASSES is the outer bound for anything the proof
    misses.

    -> {"state": "done"|"gaveup", "attempt": n, "reason": str,
        "residual": [videoId...], "entries": [...]|None, "complete": bool,
        "statuses": {position in the plan: status}}

    `hooks` is the caller's live-progress set, the same one the first pass takes:
    the repair publishes its phase, the copies it sends and the rows the read after
    it proves. A repair is the longest thing a large run does, and a page that
    cannot see it reads as a stall."""
    intended = [v for v in (video_ids or []) if isinstance(v, str) and v]
    # An unfinished run resumes at the attempt it stopped at; a finished one starts
    # with a fresh budget (it still never creates a second playlist).
    attempt, prev = 0, {}
    if spotify_id:
        rec = load_created().get(spotify_id) or {}
        prev = rec if isinstance(rec, dict) else {}
        if str(prev.get("state", "")) in ("verifying", "repairing", "standby"):
            attempt = int(prev.get("attempt", 0) or 0)
            # The interrupted run's own retry time still stands: a restart does not
            # buy a shorter backoff than it chose.
            _honour_retry_clock(prev)
    statuses, entries, complete = {}, None, False
    # What the CALLER's pass already heard from the server, by position. A first-pass
    # append it accepted is exactly as unknown as one this pass accepts: the copy may be
    # on the playlist under a name a read cannot give it. Without them the first repair
    # read sees a copy the run already sent as one it never sent, and sends it again -
    # which is a second accepted write for one source row.
    for p, st in (sent or {}).items():
        try:
            i = int(p)
        except Exception:
            continue
        if 0 <= i < len(intended) and st:
            statuses[i] = str(st)
    last_need = [v for v in (residual or []) if isinstance(v, str) and v]
    blocked = [v for v in (residual or []) if isinstance(v, str) and v]
    # Positions whose copy the server refused, and positions whose write may have
    # landed. Both are keyed by position: two copies of one videoId are two different
    # writes, and a verdict or an unknown reply on one says nothing about the other.
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
        """Clear only the doubts this COMPLETE read actually answers.

        A doubt is answered when the read holds at least as many copies of the id as the
        doubt was recorded against - never fewer. An id the record cannot measure (no
        recorded multiplicity and not in this plan) stays unresolved. `frozen` and
        `unresolved` then describe exactly the same copies, so the run cannot claim
        `done` while its own record says a copy is unknown."""
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
        """One wait, or the end of the run. Never both.

        This is a READ budget. No append is ever waited out, because waiting does
        not make a read fresh and a stale read is what re-adds."""
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
        # complete is what a later run starts from: only a run whose read reached
        # the end of the playlist and found nothing absent may claim it, and an
        # unknown write is never "nothing absent".
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
        # A playlist never shrinks while a run is repairing it, so the entries an
        # earlier complete read proved are the floor this read must still reach.
        # Below it the reply is a stale page, and a stale page is what re-adds.
        if fresh:
            # The repair's own read-back already is a complete read at or above the
            # floor: reading the same playlist again would buy nothing.
            fresh = False
            got, got_complete = entries, complete
        else:
            got, got_complete = _read_back_retry(plid, yt_cookie, minimum=floor)
            if got is not None:
                entries, complete = got, got_complete
        if got is None or not got_complete:
            reason = "read failed" if got is None else "read incomplete"
            # Nothing is appended off this read, so nothing is proven absent. An
            # unread playlist is not a finished run either: spend the budget on a
            # read that could reach the end, then stop in a terminal state.
            short += 1
            if stand_by(reason, last_need):
                return finish("gaveup", reason, last_need)
            continue
        # This read reached the end of the playlist: it is the only evidence that can
        # answer an unknown, and it answers only what it can count.
        resolve_unknowns(got)
        present = {}
        for v in got:
            present[v] = present.get(v, 0) + 1
        floor = max(floor, len(got))
        # One entry per missing COPY, in the plan's order: a repeated videoId the
        # playlist holds once is one copy short, and that copy is the repair.
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
                # The server has already answered about THIS copy: re-sending it is
                # the spin this pass exists to avoid. Another occurrence of the same
                # videoId was never answered and is still free to be sent.
                blocked.append(v)
            elif pos in frozen:
                # This copy's write may have landed and its reply was lost, or it is
                # another occurrence of a videoId whose write is unknown. No read can
                # prove it stayed out - a token-complete reply can be a stale snapshot
                # taken before the write - and copies of one id cannot be told apart in
                # a read, so nothing more of that id is sent by this run. It is carried
                # as an unverified residual with a manual recovery.
                unsure_ids.append(v)
            else:
                need_pos.append(pos)
        need = [intended[p] for p in need_pos]
        # A copy this run sent, that the server ACCEPTED, and that a complete read
        # taken after the send does not hold is not absent: the playlist grew by that
        # write, so it may hold that copy under an id this read cannot name. Nothing
        # here can tell it did not land, and the only safe verdict is unknown - frozen
        # by id (copies of one id cannot be told apart), named in the residual, and
        # never re-sent by this run or a later one. Re-sending it is how a repair pass
        # grows the playlist by the same copies forever while the intended plan never
        # resolves: observed live, 100 copies a cycle, the read growing by exactly the
        # accepted count each time, the same copies going out again every cycle.
        #
        # Measured by MULTIPLICITY, never by plan slot: copies of one id are
        # indistinguishable in a read, so a copy a read attributes to an earlier slot
        # (one the server refused, or an unknown write) is not missing - the doubt is
        # only real while the playlist holds fewer copies of that id than the run may
        # still send. A refusal or a freeze lowers that bar, and nothing else does.
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
                # A freeze that is not on disk is not a freeze: a resume would read the
                # record, find nothing unknown, and send these copies again. A run whose
                # record cannot be written sends nothing further.
                raise RuntimeError(
                    "the resume record could not be written to disk, so the verification "
                    "stopped — check that the app data folder is writable, then start the "
                    "transfer again")
            blog("converge", f"{plid} {len(stalled)} accepted copy(ies) no complete read "
                             f"shows — frozen, never re-sent: "
                             + ",".join(str(p + 1) for p in stalled[:8]))
            gone = set(stalled)
            # Freezing is by ID, so every occurrence of those ids is now un-sendable -
            # including the ones already queued in this pass under another position. Remove
            # them all from the repair: sending a second copy of an id whose write nobody
            # can account for is exactly what the freeze exists to prevent, and pruning
            # only the stalled positions left the others in the batch.
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
            # Progress is the plan's own copies, never the playlist's row count: a
            # write the server accepted and no read shows still adds a row (under an id
            # this plan does not ask for, when the read names it differently), so
            # counting rows as progress is how a repair pass keeps going while nothing
            # it intended resolves. The copies themselves are frozen above as soon as a
            # read fails to show them; this is the backstop for a pass that provably
            # changed nothing at all.
            #
            # Compared with the read just before this one, never with the union of
            # every read so far: {A} -> {B} -> {A} is a playlist that changed twice,
            # and a union makes the third read look like no change at all.
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
        # The repair goes out through the same sender the first pass uses: at most
        # TRANSFER_SLICE copies, each batch proved by the complete read that follows
        # it before the next one is sent, and the floor raised by the copies the
        # server accepted. A repair off one unbounded batch has no such proof.
        sent, sent_entries, sent_complete, sent_min = _append_verify(
            plid, intended, need_pos, yt_cookie, hooks, entries, complete, floor=floor,
            spotify_id=spotify_id)
        statuses.update(sent)
        for pos, st in sent.items():
            s = str(st or "").upper()
            if "DUPLICATE" in s or "FAILED" in s:
                # The server gave its verdict on THIS copy: do not ask again in this
                # run. It is carried as a residual the run names.
                refused_pos.add(int(pos))
            elif is_unsure_status(st):
                # The write may have landed and its reply was lost (or unreadable, a
                # 429 included). Freeze this copy and every other occurrence of the
                # same videoId for the rest of the run - absence in a read is not proof
                # it stayed out, and copies of one id cannot be told apart.
                frozen.update(_frozen_positions(intended, [int(pos)]))
                blog("converge", f"{plid} copy {int(pos) + 1} ({intended[pos]}) outcome "
                                 f"unknown — never re-sent by this run")
        # The playlist must now hold every copy this repair was promised: a read
        # that does not show them is a stale snapshot, and a stale snapshot is
        # what makes a repair pass re-send the plan. The floor only rises: written
        # here AFTER it is raised, and the record clamps it, so a mixed slice cannot
        # put an older floor back over a higher one another writer proved.
        floor = max(floor, int(sent_min or 0))
        if spotify_id and sent:
            _persist_fields(spotify_id, unsure=sorted(frozen), expected_min=floor)
        if bool(sent) and sent_entries is not None and sent_complete:
            # That last read is a complete read at or above the floor, so the next
            # pass starts from it instead of spending another read burst on it.
            entries, complete, fresh = sent_entries, sent_complete, True
        # Only a repair the wire and the server both answered is evidence that
        # nothing changed; an unknown one is frozen and verified instead.
        applied = bool(sent) and not any(is_unsure_status(s) or is_fatal_status(s)
                                         for s in sent.values())
        if any(is_fatal_status(s) for s in sent.values()):
            fatal = sorted({str(s) for s in sent.values() if is_fatal_status(s)})
            run_set(state="gaveup", attempt=attempt, next_attempt_at=0.0,
                    reason="append refused by YouTube Music", residual=need)
            _persist_progress(spotify_id, plid, url, title, len(intended),
                              state="gaveup", attempt=attempt, pending=need,
                              plan=intended, expected_min=floor, unsure=sorted(frozen))
            # The status itself travels with the result: `reason` is a fixed sentence
            # with no status in it, so the caller - and the page - can only tell an
            # auth answer from a refused track when the status is carried somewhere.
            blog("gaveup", f"{plid} the append was refused with {fatal[0] if fatal else 'a fatal status'}"
                           f" — still absent: " + ",".join(need[:8]))
            return {"state": "gaveup", "attempt": attempt,
                    "reason": "append refused by YouTube Music", "residual": need,
                    "entries": entries, "complete": complete, "statuses": statuses,
                    "fatal": fatal, "frozen": sorted(frozen)}
        # A repair whose outcome is unknown, or that the server accepted, is verified
        # below and never re-sent: the read is the only way to know what landed. No
        # append is re-sent on a timer, because no timer makes a stale read fresh.

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
        # A resume replays the plan the interrupted run persisted, and matches
        # nothing: its playlist was built from those ids, so a fresh match would
        # reconcile the rows against copies it never held — and, with 1:1 on,
        # append a second copy of every track. A record that cannot name its plan
        # is reported as interrupted instead; guessing is what this removes.
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
        # No plan replayed means a fresh run: match the source once, and keep the
        # per-row plan so a resume can replay it instead.
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
                            # On disk with the plan: a restart must not lose the
                            # label that says this copy was a rank-1 guess.
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
        # A soft second search for the rows whose search never answered: one query
        # each, capped, and never on a row the matcher answered. Matched rows and
        # their progress are untouched, and a row this cannot resolve keeps its own
        # skip - a skip is honest, a second-chance guess is not.
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
                        # Truthful provenance for the page: this row is in the plan
                        # because a second search answered, not on the first pass. The
                        # flag outlives the terminal ledger, which clears a landed
                        # row's transient text.
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
                # The rows are the truth even here: a failed run that read 4 rows and
                # matched none reports source 4 / skipped 4, never four zeros.
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
                    # An accepted add is not an add: the copy is on the playlist only
                    # when a COMPLETE read shows it, so the row waits as pending and
                    # the counter follows the rows instead of the reply.
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
                # The seed is a completed read, so those rows are landed - and the
                # counter is recounted from them, never set from an argument.
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
                 # Each slice's COMPLETE read-back publishes the rows it proves:
                 # the same marking a seed read does, once per slice.
                 "on_verified": _on_seed}
        try:
            res = transfer_create_append(yt_cookie, pname, "", vids, privacy, sid, hooks, sigs_for_pos,
                                         plan_slots=list(matched_ids) if plan is None else None,
                                         plan_guesses=dict(guesses) if plan is None else None)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            c = classify(msg)
            if isinstance(e, FatalAppend):
                # The run stopped on a verdict YouTube Music gave: report that verdict,
                # not a sentence about a failed read. kind/hint come from the status
                # alone, through the same mapping a completed run uses, so a dead cookie
                # cannot arrive as a guess about the cookie's scope.
                c = _write_verdict(e.status)
            with _RUN_LOCK:
                if RUN.get("id") != run_id:
                    return
                RUN["phase"] = "failed"
                # A stopped run is not running: phase and state say the same thing.
                RUN["state"] = "failed"
                RUN["next_attempt_at"] = 0.0
                RUN["error"] = msg[:300]
                RUN["hint"] = c.get("hint", "")
                RUN["kind"] = c.get("kind", "unknown")
                if isinstance(e, FatalAppend) and e.outcomes:
                    # The copies this run actually sent are settled by their own verdict:
                    # a refused append is a refused row, not an unverified one. Everything
                    # else stays as the sender left it - unknown where a write may have
                    # landed, unsent where nothing was ever asked for.
                    _settle_verdict_rows_locked(e.outcomes, _row_of_pos)
                # A row whose add may already be applied is unverified, never
                # skipped, and the counters are recounted from those rows.
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
        # Phase 2: whatever the first pass could not prove, the playlist now
        # decides. Bounded, verified, and never a second playlist.
        first_attempts = {int(k): str(v or "") for k, v in (res.get("attempts") or {}).items()}
        # No append failure is retried: a 429 on this endpoint is not proof the write
        # was refused unapplied, so every unknown outcome is frozen for the whole run
        # and reported unverified. The positions a resume inherited from the record
        # are frozen the same way - that is the whole point of persisting them.
        unsure_positions = {p for p, st in first_attempts.items() if is_unsure_status(st)}
        for p in res.get("unsure") or []:
            try:
                unsure_positions.add(int(p))
            except Exception:
                continue
        fatal_first = [str(s) for s in (res.get("fatal") or []) if s]
        if fatal_first:
            # YouTube Music refused the CALL, not one track: the cookie is dead or the
            # playlist is gone, so a repair pass would write into the same refusal. The
            # run ends here, on the status the server actually gave, and a fresh cookie
            # plus a resume is the way forward.
            blog("converge", f"{res.get('playlistId', '')} not repaired: the append was "
                             f"refused with {fatal_first[0]}")
            # The record says where the run stopped, not "creating": the create did
            # answer, and what is on disk is a playlist that exists with copies still
            # absent, which is what a resume (and the page) must read.
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
                             # What the first pass already heard, by position. A copy it
                             # accepted is as unknown as one the repair accepts, and the
                             # repair's first read is the evidence that freezes it -
                             # without these it sees the copy as never sent and writes
                             # it again.
                             sent=first_attempts)
        entries_final = conv.get("entries")
        # The first pass's verdicts are evidence too: a copy YTM refused stays
        # refused, so its status is carried into the final ledger instead of being
        # re-derived from the plan's id multiplicity. The convergence pass only
        # overwrites a position it sent again itself, and its statuses are keyed by
        # the copy's position in the plan: joining them on videoId gave one copy's
        # verdict to every copy of that id.
        attempts = dict(first_attempts)
        for pos, st in (conv.get("statuses") or {}).items():
            try:
                i = int(pos)
            except Exception:
                continue
            if st and 0 <= i < len(vids):
                attempts[i] = st
        # A copy the repair pass froze is an unknown write, and the final ledger has to
        # be told: its last status is "the server accepted it", so a read that cannot
        # name the row would otherwise file it as an entry that vanished instead of a
        # write nobody can account for. Same shape the create path uses for an inline
        # repeat it could not attribute.
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
                        # Present in no completed read: either the read stopped before
                        # its page, or the write's outcome is unknown. The second is
                        # flagged apart so the done line never says the read was short
                        # when it reached the end.
                        RUN["rows"][k]["unverified"] = True
                        if e.get("unsure"):
                            RUN["rows"][k]["unsure"] = True
                    elif pos not in attempts:
                        # Never sent: the run stopped before this copy. Nothing asked
                        # the playlist for it, so it is not an entry a read proved
                        # absent - it is work the run did not do, and it says so
                        # instead of being counted as a missing entry.
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
            # The state machine's own words, served by /api/run: a run that stood
            # by and realigned says so; a run that ran out of budget says gaveup
            # and names what is still absent.
            RUN["state"] = conv.get("state", "done")
            RUN["attempt"] = int(conv.get("attempt", 0) or 0)
            RUN["next_attempt_at"] = 0.0
            RUN["reason"] = conv.get("reason", "")
            RUN["residual"] = list(conv.get("residual") or [])
            # every row must carry a terminal state; reconcile strays as missing
            for row in RUN["rows"]:
                if row.get("state") not in ("landed", "skipped", "refused", "missing", "duplicate"):
                    row["state"] = "missing"
                    row["status"] = (row.get("status") or "reconciled as missing")[:80]
                    RUN["seq"] += 1
                    row["row_seq"] = RUN["seq"]
            # recount from rows so /api/run always agrees with the ledger chips.
            # A row the read-back never reached is unverified, counted apart from
            # missing (which a read that reached the end of the playlist proved).
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
            # Derived, never accumulated: rows were recounted from the reconciled
            # ledger above, so added+skipped+duplicate+unverified+refused == source
            # and the holds count is the read-back the ledger consumed. `source` is
            # the Spotify read itself, so a capped or resumed plan still reports it.
            # Guesses are landed rows that were labelled, counted so the line can
            # name them.
            RUN["guessed"] = sum(1 for row in RUN["rows"]
                                 if row.get("guess") and row.get("state") == "landed")
            # Order is a separate claim from presence: a copy repaired after a
            # later one landed sits at the end, and the line must say so instead of
            # calling the transfer faithful.
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
                # Order can only be judged on a playlist that holds every copy: a run
                # that stopped short has an unproven order, not a different one, and
                # saying "repaired copies were appended after later ones" invents
                # repairs that never happened.
                order_differs=(not RUN["order_ok"]) and RUN["landed"] == len(vids),
                unnameable=RUN["unsure"])
            RUN["phase"] = "done"
            RUN["error"] = ""
            # A run that stopped because YouTube Music refused the WRITE carries that
            # status into /api/run: `reason` is a fixed sentence with no status in it,
            # so this is the only field that can tell an auth answer from a refused
            # track. kind/hint come from the status alone - never from a guess about
            # which cause produced it.
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
            # The run stopped on a verdict YouTube Music gave: report that verdict, not a
            # sentence about a failed read. kind/hint come from the status alone, through
            # the same mapping the completed run uses, so a dead cookie cannot arrive as
            # a guess about the cookie's scope.
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
    """Most recent incomplete created.json record (process-restart resume)."""
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
                        # A record from before RECORD_VERSION cannot be resumed: the page
                        # must not offer a resume it would have to refuse.
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

# Binding to 127.0.0.1 is not access control. Every page in the user's own browser can
# address http://127.0.0.1:8787, and a name that resolves to loopback (DNS rebinding)
# reaches this server under its own Host while the browser still treats it as that
# site's origin - so a Host/Origin check is what separates this app's own page from a
# page that decided to talk to it. Neither check can tell two processes of the same
# Windows user apart: a local program that speaks HTTP is not distinguishable here.
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")
_BLOCKED = set()   # (method, reason) already written to the log

def _host_name(value: str) -> str:
    """Host part of a Host header or URL authority: lowercased, brackets and port removed."""
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
    """True only for this server's own page: a loopback host on this server's port."""
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
    """(allowed, reason) for one request. Both halves are needed: Host refuses a page whose
    own name was pointed at loopback (it must name loopback, and a DNS name never can), and
    Origin refuses a browser write sent from another site to this one. A request with no
    Origin is not a browser cross-site write - the native shell, the build smoke test and
    a command-line client send none - so it is allowed, which is a deliberate limit rather
    than isolation from local software."""
    if _host_name(headers.get("Host", "")) not in _LOOPBACK_NAMES:
        return False, "host"
    origin = (headers.get("Origin") or "").strip()
    if origin and not _origin_is_local(origin, port):
        return False, "origin"
    return True, ""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def local_only(self) -> bool:
        """Refuse anything that is not this app's own page, before the route runs."""
        allowed, why = request_is_local(self.headers, self.server.server_address[1])
        if not allowed:
            # One line per (method, reason): a page that keeps knocking must not be able
            # to grow the audit log by making this server write to it.
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
            # The shell must never come from a cache: a rebuilt exe ships a new
            # page under the same URL, and a cached older page can outlive it.
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
                            # Attempted is every row that was sent at least once:
                            # a copy waiting on its read-back (pending) is
                            # attempted work, and leaving it out made a run that
                            # was mid-slice report the seed count and nothing else.
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
                            # a removed value is not re-opened by a check; only choosing it is
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
                # latched_at is now, not the file's mtime: an explicit choice must survive the
                # next scan, otherwise a newer export silently undoes what the user just picked
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
                    # The run state is cleared, the record is not. Say only that: a record
                    # can be blocked or non-resumable, and an empty spotify_id concerns
                    # every record, so no sentence here may promise what a later transfer
                    # will do with it.
                    # reset_partial is the contract the page keys on: the sentence below can
                    # be reworded without changing behaviour, and a caller that does not
                    # understand the field falls back to reading the 500 as unexplained.
                    return self.send_json(
                        {"ok": False,
                         "reset_partial": True,
                         "error": "the run was cleared, but the playlist record could not "
                                  "be cleared - it is unchanged and may still be used on a "
                                  "later transfer"}, 500)
                blog("reset", "run + created link cleared; settings/vault/watch kept")
                return self.send_json({"ok": True})
            if self.path == "/api/vault":
                # Flags, lengths and timestamps only. This route used to spread the vault
                # dict into its body, which published both cookie values in plaintext.
                # Nothing reads a value from here: the page uses /api/cookies, which
                # never carried one.
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
                    # The file kept the old values, so that is what the page must be
                    # told: no acknowledgement for a setting that did not persist.
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
