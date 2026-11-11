"""Release asset gate: the two README screenshots are real PNGs at 824x825 - the client area of
the shipped 840x864 window - neither is the 0.2.0 shot, and the docs no longer describe the
advanced drawer 0.2.0 removed.
Stdlib only, no network. Exit 0 green, 1 red. Prints both dimensions on success."""
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = ("assets/app.png", "assets/app-setup.png")
WANT = (824, 825)
BANNED = ("advanced drawer", "forget saved cookies", "#ytcookie")
TAG = "v0.2.0"
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def png_size(path: str):
    """(width, height) from IHDR, after proving the signature, the first chunk and the IEND tail."""
    with open(path, "rb") as f:
        head = f.read(33)
        f.seek(0, os.SEEK_END)
        end = f.tell()
        f.seek(max(0, end - 12))
        tail = f.read()
    if len(head) < 33 or not head.startswith(PNG_SIG):
        raise ValueError("not a PNG (bad signature)")
    if head[12:16] != b"IHDR":
        raise ValueError("first chunk is not IHDR")
    if len(tail) < 8 or tail[-8:-4] != b"IEND":
        raise ValueError("no IEND tail - the file is truncated")
    return struct.unpack(">II", head[16:24])


def tagged_len(rel: str):
    """(byte length of the same path at TAG, None) or (None, why it is unreachable).

    An unreachable tag is reported, never assumed equal: a shallow clone must stay green,
    but it must not print a comparison it did not make."""
    try:
        r = subprocess.run(["git", "show", f"{TAG}:{rel}"], cwd=HERE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        return None, "git not runnable (%s)" % e.__class__.__name__
    if r.returncode != 0:
        return None, "tag or shot absent from this clone - fetch full history"
    if not r.stdout:
        return None, "empty blob at the tag"
    return len(r.stdout), None


def main() -> int:
    bad = []
    dims = {}
    skips = []
    for rel in SHOTS:
        path = os.path.join(HERE, rel)
        if not os.path.isfile(path):
            bad.append(f"{rel}: missing")
            continue
        try:
            w, h = png_size(path)
        except (OSError, ValueError, struct.error) as e:
            bad.append(f"{rel}: {e}")
            continue
        if (w, h) != WANT:
            bad.append(f"{rel}: {w}x{h}, want {WANT[0]}x{WANT[1]}")
            continue
        old, why = tagged_len(rel)
        if old is not None and old == os.path.getsize(path):
            bad.append(f"{rel}: byte-identical to the {TAG} shot ({old} bytes) - recapture it")
            continue
        if old is None:
            skips.append(f"{rel}: {why}")
        dims[rel] = (w, h)

    readme = os.path.join(HERE, "README.md")
    try:
        with open(readme, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        bad.append(f"README.md: {e}")
        text = ""
    for phrase in BANNED:
        if phrase in text:
            bad.append(f'README.md: still says "{phrase}"')

    if bad:
        for line in bad:
            print(f"FAIL: {line}")
        return 1
    for rel in SHOTS:
        print(f"{rel}  {dims[rel][0]}x{dims[rel][1]}")
    print(f"README.md: clean of {len(BANNED)} retired phrases")
    for line in skips:
        print(f"skip: {TAG} shot comparison for {line}")
    if skips:
        print(f"tag comparison vs {TAG}: NOT verified for {len(skips)} of {len(SHOTS)} shots")
    else:
        print(f"tag comparison vs {TAG}: both shots differ from the tagged bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
