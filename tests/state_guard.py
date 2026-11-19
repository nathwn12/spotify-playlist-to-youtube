#!/usr/bin/env python3
import importlib
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ISOLATION_RC = 3


def _fail(msg):
    print(f"FAIL isolation: {msg}")
    raise SystemExit(ISOLATION_RC)


def _inside(path, root):
    root = os.path.normcase(os.path.abspath(root)) + os.sep
    return os.path.normcase(os.path.abspath(path)).startswith(root)


def main(argv):
    if not argv:
        _fail("no gate named; usage: state_guard.py tests\\<gate>.py")
    gate = argv[0]
    if not os.path.isfile(gate):
        _fail(f"gate not found: {gate}")

    scratch = os.environ.get("SP2YT_SCRATCH", "")
    if not scratch:
        _fail("SP2YT_SCRATCH is unset - run the gates through scripts\\acceptance.cmd")
    scratch = os.path.abspath(scratch)
    appdata = os.path.abspath(os.environ.get("APPDATA", ""))
    expect_appdata = os.path.join(scratch, "appdata")
    if os.path.normcase(appdata) != os.path.normcase(expect_appdata):
        _fail(f"APPDATA is {appdata}, not this run's {expect_appdata}")

    state = os.path.join(appdata, "sp2yt")
    try:
        os.makedirs(state, exist_ok=True)
    except OSError as e:
        _fail(f"cannot create the synthetic state dir {state}: {e}")

    sys.path.insert(0, ROOT)
    try:
        backend = importlib.import_module("backend")
    except Exception as e:  # a backend that cannot import is red, never the gate's
        _fail(f"backend did not import under the scratch root: {type(e).__name__}: {e}")

    escaped = {name: value for name, value in {
        "STATE": backend.STATE, "LOGF": backend.LOGF,
        "CREATEDF": backend.CREATEDF, "SETTINGF": backend.SETTINGF,
        "COOKIEF": backend.COOKIEF, "VAULTF": backend.VAULTF,
        "WATCHF": backend.WATCHF, "home_dir": backend.home_dir(),
    }.items() if not _inside(value, scratch)}
    if os.path.normcase(os.path.abspath(backend.STATE)) != os.path.normcase(state):
        escaped["STATE"] = f"{backend.STATE} (this run's state dir is {state})"
    if escaped:
        _fail(f"backend would write outside the scratch root: {escaped}")

    sys.argv = [gate] + argv[1:]
    runpy.run_path(gate, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
