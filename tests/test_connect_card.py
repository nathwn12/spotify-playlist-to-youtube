#!/usr/bin/env python3
"""Structure gate for the always-visible YouTube Music connect card in index.html.

Static assertions only. The page script's parse gate is tests/check_page.py,
which acceptance.cmd runs before this file; runtime behaviour against
/api/cookies needs the live server and is not covered here.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
m = re.search(r"<script>(.*?)</script>", html, re.S)
if not m:
    sys.exit("CONNECT CARD RED - no inline script in index.html")
script = m.group(1)

fails = []
n = [0]


def ok(name, cond, detail=""):
    n[0] += 1
    print(("pass " if cond else "FAIL ") + name + ("" if cond else "  <- " + detail))
    if not cond:
        fails.append(name)


# 1 the advanced collapse is gone and nothing from it survives
ok("no details drawer", 'class="adv"' not in html, "details.adv is still in index.html")
ok("no details element at all", "<details" not in html, "<details> still in the rail")
ok("no summary element", "<summary" not in html, "<summary> still in the rail")
ok("no raw cookie textarea", 'id="ytcookie"' not in html, "#ytcookie textarea still present")
ok("no forget button", "forgetbtn" not in html, "forget saved cookies still present")
ok("no drawer css", "details.adv" not in html, "details.adv rules still in <style>")
ok("page never calls /api/forget", "/api/forget" not in script, "forget endpoint still wired")

tag = re.search(r"<input[^>]*\bid=\"ckfile\"[^>]*>", html)
ok("hidden cookie file input exists", bool(tag), "no #ckfile input for the card to open")
ok("cookie file input is not visible", bool(tag) and ("hidden" in tag.group(0) or 'class="sr"' in tag.group(0)),
   "the file input must stay out of sight")

# 2 every id the contract names
IDS = ["acct", "acctbadge", "acctsub", "accthelp", "acctactions",
       "pickcookie", "pastecookie", "replacecookie", "removecookie",
       "detectedrow", "detectedname", "usecookie",
       "pastesheet", "pasteinput", "moresheet", "watchdir", "watchbtn", "copybtn"]
for i in IDS:
    ok('id="%s"' % i, 'id="%s"' % i in html, "missing from index.html")
ok('data-state="none" at rest', 'data-state="none"' in html, "#acct has no initial state")

# 3 the card talks to the cookies API only
ok("/api/cookies is read", '"/api/cookies"' in script, "no GET /api/cookies")
ok("/api/cookies/check is used", '"/api/cookies/check"' in script, "no check call")
ok("/api/cookies/remove is used", '"/api/cookies/remove"' in script, "no remove call")
ok("/api/cookies/use is used", '"/api/cookies/use"' in script, "no use-detected call")
ok("/api/vault is gone", "/api/vault" not in script, "the page still reads the raw vault")
ok("one latch call site only", script.count('"/api/latch"') == 1,
   "expected exactly one /api/latch call, found %d" % script.count('"/api/latch"'))

# the rejection path must ask what is really stored before it claims failure:
# /api/latch can save a valid yt cookie and still 400 on a junk spotify cookie.
_i = script.index("async function latchText(")
latch = script[_i:script.index("\n}", _i)]
tail = latch[latch.index("return true;"):]
JUNK = 'S.ytState="junk"'
GUARD = 'S.ytState==="none"||S.ytState==="junk"||S.ytState==="expired"'
ok("latch rejection re-reads the record", "await refreshAcct()" in tail,
   "a 400 is turned into junk without GET /api/cookies")
ok("latch re-reads before the junk fallback",
   "await refreshAcct()" in tail and JUNK in tail
   and tail.index("await refreshAcct()") < tail.index(JUNK),
   "junk is set before the stored state is known")
ok("latch shows the rejection only when nothing usable is stored",
   GUARD in tail and JUNK in tail and tail.index(GUARD) < tail.index(JUNK),
   "the backend error can cover a cookie that was really stored")

# 4 the transfer surface is intact
for i in ("srcurl", "startbtn", "ledger"):
    ok('id="%s" survives' % i, 'id="%s"' % i in html, "transfer surface broken")
ok("/api/transfer still called", '"/api/transfer"' in script, "start no longer posts a transfer")
ok("start reads the card state", "S.ytState" in script, "startTransfer has no cookie gate")

# the six states, in the contract's words
STATES = {
    "none": ("Setup", "Not connected yet"),
    "checking": ("Checking\u2026", "Checking your file\u2026"),
    "connected": ("Connected", "Your account is ready"),
    "expired": ("Expired", "Needs a fresh file"),
    "junk": ("Try again", "That file didn't work"),
    "network": ("Saved", "Couldn't check right now"),
}
ok("all six card states known", "CARD_STATES" in script, "no state list")
for st, (badge, sub) in STATES.items():
    ok("state %-9s badge" % st, '"%s"' % badge in script, "badge %r missing" % badge)
    ok("state %-9s sub" % st, '"%s"' % sub in script, "sub %r missing" % sub)

ok("start gate sentence", "Connect YouTube Music first \u2014 choose your cookie file above." in script,
   "the not-connected pin is missing")
ok("using-file line", "Using ${" in script, "no `Using <file>` line")
ok("needs pulse", "needs" in script and ".acct.needs" in html, "no .needs pulse on #acct")

# 5 the link row: one height, one line, label welded to its control
GROUP = re.search(r'<div class="privgroup"[^>]*>(.*?)</div>\s*<button id="morebtn"', html, re.S)
ok("privacy label and control share a wrapper",
   bool(GROUP) and 'id="privcap"' in GROUP.group(1) and 'class="privseg"' in GROUP.group(1),
   "PRIVACY and .privseg are still separate children of .side, so the label strands on wrap")
ok("segmented control is still labelled by #privcap", 'aria-labelledby="privcap"' in html,
   "the group lost its accessible name")
ok("one shared chip height token", re.search(r"--chiph:\s*\d+px", html) is not None,
   "no --chiph token for the row chips to share")
ok("input, cover block and privacy group take that height",
   html.count("height:var(--chiph)") >= 3,
   "expected .srcwrap, .pident and .privgroup to share --chiph")
ok("placeholder caption balances its lines", "text-wrap:balance" in html,
   "#pname may still orphan a single word")
ok("caption keeps its two-line clamp", "-webkit-line-clamp:2" in html,
   "a real playlist title lost its second line")
ok("kbd chips are not clipped by .keys",
   re.search(r"\.keys\{[^}]*line-height:16px", html) is not None
   and re.search(r"\.keys kbd\{[^}]*vertical-align:top", html) is not None,
   ".keys is still shorter than the kbd inline boxes")

# 6 no cookie value is ever rendered
ok("no cookie sample in the page", "SAPISID=" not in html, "a cookie value sample is in the markup")
ok("no cookie-looking placeholder",
   not re.search(r'placeholder="[^"]*(SAPISID|cookie=|sp_dc=)', html), "a placeholder leaks a cookie shape")
ok("paste box starts empty",
   re.search(r"<textarea[^>]*\bid=\"pasteinput\"[^>]*>\s*</textarea>", html) is not None,
   "#pasteinput carries content at rest")

print()
if fails:
    sys.exit("CONNECT CARD RED - %d of %d checks failed: %s" % (len(fails), n[0], ", ".join(fails)))
print("connect card: %d checks green - %d ids, 6 states, cookies API only, no cookie value in the page"
      % (n[0], len(IDS)))
