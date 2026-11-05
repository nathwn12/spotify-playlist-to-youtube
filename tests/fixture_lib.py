"""Shared fixture helpers for the sp2yt acceptance gate.

The fixtures store the real InnerTube search responses the matcher consumed.
Only keys the parser reads are kept; the recorder proves the trimmed response
yields exactly the same candidates as the full recording.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "playlist.json")


def envelope(sections):
    """The one path backend._candidates_from_search_json reads."""
    return {"contents": {"tabbedSearchResultsRenderer": {"tabs": [{"tabRenderer": {
        "content": {"sectionListRenderer": {"contents": sections}}}}]}}}


def skip_class(text):
    """Map yt_match's skip text to the class recorded in the fixture."""
    t = (text or "").lower()
    if "version mismatch" in t:
        return "version_mismatch"
    if "versions found" in t and "only " in t:
        return "only_versioned"
    if "no confident match (best" in t:
        return "low_confidence"
    if "no ytm result" in t:
        return "no_result"
    return "other" if t else "none"


def load(path=FIXTURE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
