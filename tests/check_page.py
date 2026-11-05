#!/usr/bin/env python3
"""Parse-check the inline page script shipped in index.html.

The build gate and CI both run this, so a broken <script> cannot ship.
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
m = re.search(r"<script>(.*?)</script>", html, re.S)
if not m:
    sys.exit("PAGE SCRIPT RED - no inline script in index.html")
path = os.path.join(tempfile.gettempdir(), "sp2yt-page.js")
with open(path, "w", encoding="utf-8") as f:
    f.write(m.group(1))
if subprocess.call(["node", "--check", path]) != 0:
    sys.exit("PAGE SCRIPT RED - index.html inline script does not parse")
print("page script: parses")
