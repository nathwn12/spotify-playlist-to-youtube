#!/usr/bin/env python3
"""Offline acceptance gate: replay the owner's recorded data through backend.py.

Each fixture row is the real Spotify source row and the real InnerTube search
responses seen for it. Nothing here reaches the network: urlopen is replaced
with a lookup keyed by the search query.

Exit 0 only when every row resolves exactly as recorded and the four versions
of one title resolve to four distinct videoIds with zero repeats.
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import backend  # noqa: E402
import fixture_lib  # noqa: E402


class MissingResponse(BaseException):
    """BaseException on purpose: yt_match must not swallow a fixture gap."""


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._body


def main():
    try:
        data = fixture_lib.load()
    except Exception as e:
        print(f"FAIL fixtures: cannot read {fixture_lib.FIXTURE} ({type(e).__name__}: {e})")
        print("0/0 fixtures")
        return 1

    rows = data.get("rows") or []
    responses = data.get("responses") or {}
    if not rows:
        print("FAIL fixtures: no rows recorded")
        print("0/0 fixtures")
        return 1

    def fake_urlopen(req, timeout=None):
        try:
            query = json.loads((req.data or b"{}").decode("utf-8", "ignore")).get("query", "")
        except Exception:
            query = ""
        if query not in responses:
            raise MissingResponse(f"no recorded response for search: {query!r}")
        return FakeResponse(fixture_lib.envelope(responses[query]))

    real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    failures, failed_ids, replayed = [], set(), {}
    try:
        for row in rows:
            rid = row.get("id", "?")
            src = row.get("source") or {}
            title = src.get("title", "")
            artist = src.get("artist", "")
            ms = int(src.get("duration_ms") or 0)
            expect = row.get("expect") or {}
            want_sig = expect.get("signature", "")

            missing = [q for q in row.get("queries") or [] if q not in responses]
            if missing:
                failures.append(f"FAIL {rid}: fixture gap, no response recorded for {missing[0]!r}")
                failed_ids.add(rid)
                continue
            try:
                backend._SEARCH_CACHE.clear()
                result = backend.yt_match(title, artist, ms)
            except MissingResponse as e:
                failures.append(f"FAIL {rid}: {e}")
                failed_ids.add(rid)
                continue
            except Exception as e:
                failures.append(f"FAIL {rid}: matcher raised {type(e).__name__}: {e}")
                failed_ids.add(rid)
                continue

            result = result or {}
            got_sig = backend._sig_label(backend._version_sig(title))
            if got_sig != want_sig:
                failures.append(f"FAIL {rid}: source signature is {got_sig}, fixture recorded {want_sig}")
                failed_ids.add(rid)
                continue

            vid = result.get("videoId") or ""
            replayed[rid] = result
            if expect.get("videoId"):
                if not vid:
                    failures.append(f"FAIL {rid}: expected videoId {expect['videoId']}, skipped: "
                                    f"{str(result.get('skipped', ''))[:160]}")
                    failed_ids.add(rid)
                    continue
                chosen_sig = backend._sig_label(backend._version_sig(result.get("title", "")))
                if chosen_sig != want_sig:
                    failures.append(f"FAIL {rid}: chosen version {chosen_sig}, want {want_sig} "
                                    f"('{result.get('title', '')}')")
                    failed_ids.add(rid)
                    continue
                if vid != expect["videoId"]:
                    failures.append(f"FAIL {rid}: expected {expect['videoId']}, got {vid} "
                                    f"('{result.get('title', '')}')")
                    failed_ids.add(rid)
                    continue
                seen = []
                for q in row.get("queries") or []:
                    env = fixture_lib.envelope(responses[q])
                    seen += [c.get("videoId") for c in backend._candidates_from_search_json(env)]
                if expect["videoId"] not in seen:
                    failures.append(f"FAIL {rid}: recorded responses do not contain {expect['videoId']}")
                    failed_ids.add(rid)
                    continue
            elif expect.get("skip"):
                if vid:
                    failures.append(f"FAIL {rid}: expected skip ({expect['skip']}), got {vid} "
                                    f"('{result.get('title', '')}')")
                    failed_ids.add(rid)
                    continue
                got_class = fixture_lib.skip_class(result.get("skipped", ""))
                if got_class != expect["skip"]:
                    failures.append(f"FAIL {rid}: skip class {got_class}, want {expect['skip']} "
                                    f"({str(result.get('skipped', ''))[:140]})")
                    failed_ids.add(rid)
                    continue
            else:
                failures.append(f"FAIL {rid}: fixture has neither videoId nor skip")
                failed_ids.add(rid)
                continue

        # Four versions of one title must be four videos, not four copies of one.
        group = [r for r in rows if (r.get("source") or {}).get("title", "").startswith(
            "MONTAGEM RETINAL PLATINA - ")]
        if len(group) != 4:
            failures.append(f"FAIL versions: expected 4 MONTAGEM RETINAL PLATINA rows, found {len(group)}")
        else:
            ids = [(replayed.get(r["id"]) or {}).get("videoId") or "" for r in group]
            sigs = [backend._sig_label(backend._version_sig(r["source"]["title"])) for r in group]
            repeats = backend._repeat_count(ids, sigs)
            if not all(ids):
                failures.append(f"FAIL versions: {sum(1 for v in ids if v)}/4 versions resolved")
            elif len(set(ids)) != 4:
                failures.append(f"FAIL versions: 4 versions collapsed to {len(set(ids))} ids: {ids}")
            elif repeats:
                failures.append(f"FAIL versions: {repeats} repeat(s): {ids}")
            else:
                print("versions: 4/4 distinct, 0 repeats")
    finally:
        urllib.request.urlopen = real_urlopen

    for line in failures:
        print(line)
    print(f"{len(rows) - len(failed_ids)}/{len(rows)} fixtures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
