#!/usr/bin/env python3
"""Offline checks for the advice watchdog prints.

watchdog.get is stubbed, so nothing here binds a port or reads the audit log on
disk. What is checked is the CLAIM, not the log: the heuristic matches text in a
`fail` line, and that line names an exception - never which request threw it, and
never whether the request was one of the transfer's own. So a 429, a timeout, a
401 and a 403 all say the same two things: what cannot be known, and where to
look before deciding anything.

Three sentences were once printed from that same log text and none of them was
knowable: "already retried at 4 workers" (no code path sets a worker count),
"already retried once automatically" (an add's failure is never re-sent), and
"cookie expired/wrong site ... re-run" (an expired session and a cookie without
permission answer identically, and a failed add may have landed). Each is pinned
below, so it cannot come back as prose.
"""

import contextlib
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import watchdog  # noqa: E402


def run(events, health=True):
    """(exit code, printed text) for one audit window, against a stubbed server."""

    def fake_get(path, timeout=10):
        if path == "/health":
            if not health:
                raise OSError("connection refused")
        elif path == "/api/status":
            return json.dumps({"recent": list(events)})
        else:
            raise AssertionError(path)
        return json.dumps({"ok": True})

    watchdog.get = fake_get
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = watchdog.check_once()
    return rc, buf.getvalue()


def advice(text):
    """The fix block, flattened: what a reader copies into their next action."""
    i = text.find("fix:")
    return "" if i < 0 else " ".join(text[i:].split())


def close(ad):
    """The closing advice every branch must end on, without its own opening."""
    i = ad.find("Check the run's status")
    return ad[i:] if i >= 0 else ""


def fail_line(msg):
    return "12:00:09 fail run=r1 " + msg


fails = []
n = [0]


def ok(name, cond, detail=""):
    n[0] += 1
    print(("pass " if cond else "FAIL ") + name + ("" if cond else "  <- " + detail))
    if not cond:
        fails.append(name)


# Every failure shape the heuristic recognises, with tokens its own opening must carry.
BRANCHES = {
    "429": (fail_line("HTTPError: HTTP Error 429: Too Many Requests"), ("429", "throttl")),
    "timeout": (fail_line("URLError: <urlopen error timed out>"), ("timed out", "connection")),
    "401": (fail_line("HTTPError: HTTP Error 401: Unauthorized"), ("auth", "permission")),
    "403": (fail_line("HTTPError: HTTP Error 403: Forbidden"), ("auth", "permission")),
}

got = {}
for name, (line, _tokens) in BRANCHES.items():
    got[name] = run([line])

# 1 every branch is diagnosed from its fail line, and opens on its own subject
for name, (_line, tokens) in BRANCHES.items():
    rc, text = got[name]
    ad = advice(text)
    ok("%s: a fail line is a failure verdict" % name, rc == 2, "check_once returned %r" % (rc,))
    ok("%s: advice names its own subject" % name, bool(ad) and any(t in ad.lower() for t in tokens), ad[:90])
    ok("%s: opens on a fix" % name, ad.startswith("fix:"), ad[:60])

# 2 nothing asserts what the log cannot know, in ANY branch
FORBIDDEN = [
    (r"\b\d+\s*workers?\b", "worker count no code path sets"),
    (r"already retried", "claim that a retry ran"),
    (r"auto-retr", "claim of an automatic retry"),
    (r"re-?asks", "claim that the search re-asked"),
    (r"retried (once|at|with)", "claim of a retry count or shape"),
    (r"never re-sent", "verdict on a write path the log cannot identify"),
    (r"only what is absent", "promise about what a retry adds"),
    (r"re-?run", "instruction to re-run instead of looking first"),
    (r"whether a retry is allowed", "claim that the status settles retryability"),
    (r"cookie expired", "claim that the cookie expired"),
    (r"wrong site", "claim about which cookie fault it is"),
    (r"re-?upload", "instruction to replace the cookie before anything was checked"),
    (r"fresh cookie|fixes the first|not the problem", "claim that a fresh cookie settles which fault it was"),
]
for name in BRANCHES:
    ad = advice(got[name][1])
    for pattern, what in FORBIDDEN:
        ok("%s: no %s" % (name, what), not re.search(pattern, ad.lower()), ad)

# 3 every branch sends the reader to the same two places, before deciding anything
for name in BRANCHES:
    low = advice(got[name][1]).lower()
    ok("%s: check the run's status" % name, "run's status" in low, low[:90])
    ok("%s: check the playlist" % name, "playlist" in low, low[:90])
    ok("%s: decide after looking, not before" % name, "before deciding whether to retry" in low, low[:90])
    ok("%s: keep the log" % name, "keep the log" in low, low[:90])
    ok("%s: keep created.json" % name, "created.json" in low, low[:90])
    ok("%s: refuses Reset" % name, "do not reset" in low, low[:90])
    ok("%s: refuses deleting the playlist" % name, "delete the playlist" in low, low[:90])

# 4 the closing advice is one block, so a fifth branch cannot drift alone
closes = {name: close(advice(got[name][1])) for name in BRANCHES}
first = closes["429"]
ok("the closing advice is non-empty", bool(first), repr(first))
for name, tail in closes.items():
    ok("%s: closing advice is the shared one" % name, tail == first, "%r vs %r" % (tail[:70], first[:70]))

# 5 the auth branch stays conditional: both causes, no verdict, no cookie instruction
auth = advice(got["401"][1]).lower()
ok("auth: names an expired session", "expired" in auth, auth[:90])
ok("auth: names a permission answer", "permission" in auth, auth[:90])
ok("auth: says it cannot tell them apart", "cannot say" in auth, auth[:90])
ok("auth: keeps prior copies in view", "unproven" in auth, auth[:90])
ok(
    "auth: prescribes no cookie change",
    not re.search(r"re-?upload|replace|re-?export|upload (a )?new", auth),
    auth[:90],
)
ok(
    "auth: 403 gets the same advice as 401",
    advice(got["403"][1]) == advice(got["401"][1]),
    "%r vs %r" % (advice(got["403"][1])[:60], advice(got["401"][1])[:60]),
)

# 6 the 429 and the timeout still say the log cannot name the request
for name in ("429", "timeout"):
    ad = advice(got[name][1]).lower()
    ok("%s: says the log cannot name the request" % name, "cannot say which request" in ad, ad[:90])
    ok("%s: an add may have landed anyway" % name, "may have landed" in ad, ad[:90])

# 7 the accepted batch shape is unchanged, so the advice above stays the subject
ok(
    "search batch still defaults to 8 workers",
    "workers: int = 8" in open(os.path.join(ROOT, "backend.py"), encoding="utf-8").read(),
    "yt_search_batch's default changed; re-check what the advice should say",
)

# 8 the fall-through still answers, so an edit here cannot swallow it
unknown_rc, unknown_text = run([fail_line("WeirdError: something new")])
ok(
    "an unknown failure still says to paste the line",
    unknown_rc == 2 and "paste" in advice(unknown_text).lower(),
    advice(unknown_text)[:80],
)

# 9 a clean window advises nothing at all
clean_rc, clean_text = run(["12:00:01 create run=r1 ok playlist=PL1"])
ok("a clean window prints no fix", clean_rc == 0 and "fix:" not in clean_text, clean_text[-80:])

print()
if fails:
    sys.exit("WATCHDOG RED - %d of %d checks failed: %s" % (len(fails), n[0], ", ".join(fails)))
print("watchdog: %d checks green - advice names no request the log cannot identify" % n[0])
