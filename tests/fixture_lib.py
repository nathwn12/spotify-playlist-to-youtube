import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "playlist.json")


def envelope(sections):
    return {"contents": {"tabbedSearchResultsRenderer": {"tabs": [{"tabRenderer": {
        "content": {"sectionListRenderer": {"contents": sections}}}}]}}}


def skip_class(text):
    t = (text or "").lower()
    if "version mismatch" in t:
        return "version_mismatch"
    if "versions found" in t and "only " in t:
        return "only_versioned"
    if "not identified" in t:
        return "unidentified"
    if "title only" in t:
        return "title_only"
    if "no confident match (best" in t:
        return "low_confidence"
    if "no ytm result" in t:
        return "no_result"
    return "other" if t else "none"


def load(path=FIXTURE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
