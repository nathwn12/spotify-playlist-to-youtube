# sp2yt — agent notes

Usage lives in `README.md`; the transfer contract lives in `docs/PROTOCOL.md`. Neither explains the three things below.

## Non-discoverable commands

- Build **only** through `scripts\build.cmd`. It backs up the exe, stops running instances, compiles the single portable exe, smoke-tests `/health`, and restores the backup on red. Calling `pyinstaller` directly skips that safety logic.
- Run the gate through `scripts\acceptance.cmd`. It must print `36/36 fixtures` and finish with `acceptance: all gates green` before anything ships. Its Python gates go through `tests\state_guard.py` with a scratch `APPDATA`; invoking a gate directly lets `backend.py` write into the live app state — keep that wrapper.

## Landmines / do-not-touch

- The version shown in the UI has no literal in `index.html`: the page reads `GET /health`, which serves `backend.py`'s `APP_VERSION`. Never bake a version into the markup or add a second source of truth.
