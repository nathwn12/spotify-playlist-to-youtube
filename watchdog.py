#!/usr/bin/env python3
"""watchdog: inspects sp2yt audit log + live status, diagnoses failures.
Usage: python3 watchdog.py [--watch]  (single check; --watch loops every 15s)
Never prints cookie values — log contains lengths only by design."""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("SP2YT", "http://127.0.0.1:8787")

def get(p, timeout=10):
    return urllib.request.urlopen(BASE + p, timeout=timeout).read().decode()

def check_once():
    try:
        h = json.loads(get("/health"))
        print("server: UP" if h.get("ok") else "server: BAD /health")
    except Exception as e:
        print(f"server: DOWN ({e}) — restart: python3 {os.path.join(HERE, 'backend.py')}")
        return 1
    try:
        st = json.loads(get("/api/status"))
    except Exception as e:
        print(f"status endpoint unreachable: {e}")
        return 1
    recent = st.get("recent", [])
    print(f"--- last {len(recent)} events ---")
    for line in recent[-20:]:
        print(" ", line)
    fails = [l for l in recent if l.split(" ", 1)[-1].startswith("fail") or "FAIL" in l]
    if not fails:
        print("verdict: no failures in window. If your browser shows an error, copy the DIAGNOSIS line here.")
        return 0
    print(f"verdict: {len(fails)} failure(s). Latest:")
    last = fails[-1]
    print(" ", last)
    m = last.lower()
    if "401" in m or "unauthorized" in m or "sapisid" in m:
        print("fix: cookie expired/wrong site. Re-export fresh cookie .txt from the site, re-upload, re-run.")
    elif "429" in m or "rate" in m:
        print("fix: throttled. Frontend already retried at 4 workers; wait 60s and re-run.")
    elif "timed out" in m or "timeout" in m or "urlerror" in m:
        print("fix: transient network. Re-run; already retried once automatically.")
    else:
        print("fix: unknown — paste this log line to the agent for a code fix.")
    return 2 if fails else 0

if __name__ == "__main__":
    if "--watch" in sys.argv:
        while True:
            print("=" * 50, time.strftime("%H:%M:%S"))
            check_once()
            time.sleep(15)
    else:
        sys.exit(check_once())
