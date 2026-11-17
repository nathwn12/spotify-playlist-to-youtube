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

def _evidence_advice():
    """The close every failure branch ends on. None of them can see the request, so
    none promises that a retry is available - the record can refuse to resume - and
    none replaces evidence: the log, created.json and the playlist are the only
    record of what happened."""
    print("     Check the run's status and the playlist before deciding whether to retry;")
    print("     keep the log and created.json as they are, and do not Reset or delete the")
    print("     playlist.")

def _unproven_advice(what):
    """A 429 and a timeout look the same to this heuristic: either can hit a search,
    a read, or an add, and the exception text names none of them."""
    print(f"fix: {what}.")
    print("     The log cannot say which request it was, and an add that failed this way may")
    print("     have landed.")
    _evidence_advice()

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
    fails = [ev for ev in recent if ev.split(" ", 1)[-1].startswith("fail") or "FAIL" in ev]
    if not fails:
        print("verdict: no failures in window. If your browser shows an error, copy the DIAGNOSIS line here.")
        return 0
    print(f"verdict: {len(fails)} failure(s). Latest:")
    last = fails[-1]
    print(" ", last)
    m = last.lower()
    if "401" in m or "403" in m or "forbidden" in m or "unauthorized" in m or "sapisid" in m:
        print("fix: an auth or permission answer — possibly an expired session, possibly a")
        print("     cookie without permission for that action. This log cannot say which, and")
        print("     copies an earlier run sent may still be unproven.")
        _evidence_advice()
    elif "429" in m or "rate" in m:
        _unproven_advice("throttled by YouTube Music (429)")
    elif "timed out" in m or "timeout" in m or "urlerror" in m:
        _unproven_advice("a request timed out or lost its connection")
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
