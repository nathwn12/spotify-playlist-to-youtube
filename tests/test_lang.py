#!/usr/bin/env python3
"""Offline language tests for the script-aware matcher.

Pure unit checks: no network, no files, no server. They prove the fold keeps
Japanese script, kana romanizes deterministically, the query variants do not
drift, and the _pick_best ladder accepts a script crossing only on evidence or
labels it a best guess.

Run: python -X utf8 tests/test_lang.py   (exit 0 only when every check passes)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import backend  # noqa: E402

_failures = []


def eq(got, want, label):
    if got != want:
        raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def ge(got, least, label):
    if not got >= least:
        raise AssertionError(f"{label}: got {got!r}, want >= {least!r}")


def le(got, most, label):
    if not got <= most:
        raise AssertionError(f"{label}: got {got!r}, want <= {most!r}")


def ok(name):
    print("ok - " + name)


def cand(video_id, title, artist, duration=""):
    return {"videoId": video_id, "title": title, "artist": artist,
            "duration": duration, "type": ""}


def raises(fn, contains, label):
    try:
        fn()
    except RuntimeError as e:
        if contains not in str(e):
            raise AssertionError(f"{label}: raised {e!r}, want text containing {contains!r}") from e
        return str(e)
    raise AssertionError(f"{label}: did not raise")


def test_fold():
    eq(backend._fold_text("夜に駆ける (Official Video)"), "夜に駆ける", "bracket zone")
    eq(backend._fold_text("ＡＢＣ！"), "abc", "nfkc + casefold + punctuation")
    eq(backend._fold_text("夜に駆ける！"), "夜に駆ける", "punctuation after kana")
    eq(backend._fold_text("Lemon feat. 米津玄師"), "lemon", "feat tail")
    eq(backend._fold_text("Yoru ni Kakeru [Remastered 2024]"), "yoru ni kakeru", "remaster zone")
    ok("fold")


def test_scripts():
    eq(backend._has_cjk("夜に駆ける"), True, "kana+kanji is cjk")
    eq(backend._has_cjk("Yoru ni Kakeru"), False, "latin is not cjk")
    eq(backend._has_latin("夜に駆ける"), False, "cjk is not latin")
    eq(backend._has_latin("Yoru ni Kakeru"), True, "latin")
    eq(backend._scripts_cross("夜に駆ける", "Yoru ni Kakeru"), True, "ja -> romaji crosses")
    eq(backend._scripts_cross("Yoru ni Kakeru", "夜に駆ける"), True, "romaji -> ja crosses")
    eq(backend._scripts_cross("夜に駆ける", "群青"), False, "same script does not cross")
    eq(backend._scripts_cross("Lemon", "Lemon (Live)"), False, "latin does not cross")
    ok("scripts")


def test_romanize():
    eq(backend._romanize_ja("すし"), "sushi", "sushi")
    eq(backend._romanize_ja("がっこう"), "gakkou", "small tsu gemination")
    eq(backend._romanize_ja("きょう"), "kyou", "digraph")
    eq(backend._romanize_ja("しゃしん"), "shashin", "sha + n")
    eq(backend._romanize_ja("コーヒー"), "koohii", "long vowel mark")
    eq(backend._romanize_ja("ティー"), "tii", "katakana digraph + mark")
    eq(backend._romanize_ja("ヴァイオリン"), "vaiorin", "vu digraph")
    eq(backend._romanize_ja("ん"), "n", "n")
    # The plan's value here is "夜nikakeru", which is what 夜にかける gives: the
    # kanji 駆 passes through untouched (same plan, prose above), so it stays.
    eq(backend._romanize_ja("夜に駆ける"), "夜ni駆keru", "kanji passes through")
    ok("romanize")


def test_aligned_title():
    ge(backend._title_aligned("夜に駆ける", "YOASOBI", "YOASOBI - 夜に駆ける"), 0.90, "artist prefix")
    ge(backend._title_aligned("夜に駆ける", "YOASOBI", "Yoru ni Kakeru"), 0.45, "ja vs romaji")
    ge(backend._title_aligned("Yoru ni Kakeru", "YOASOBI", "夜に駆ける"), 0.45, "romaji vs ja")
    le(backend._title_aligned("夜に駆ける", "YOASOBI", "群青"), 0.40, "different song")
    le(backend._title_aligned("群青", "YOASOBI", "Ultramarine"), 0.40, "kanji has no reading")
    eq(backend._strip_artist_prefix("YOASOBI - 夜に駆ける", "YOASOBI"), "夜に駆ける", "prefix stripped")
    eq(backend._strip_artist_prefix("夜に駆ける", "YOASOBI"), "夜に駆ける", "no prefix, untouched")
    ge(backend._artist_aligned("YOASOBI", "YOASOBI \u2022 4:21"), 0.90, "artist with duration tail")
    ok("aligned title + artist")


def test_variants():
    eq(backend._query_variants("Yoru ni Kakeru", "YOASOBI"),
       ["Yoru ni Kakeru YOASOBI", "Yoru ni Kakeru", "YOASOBI - Yoru ni Kakeru"], "latin three")
    v = backend._query_variants("アイドル", "YOASOBI")
    eq(v[:3], ["アイドル YOASOBI", "アイドル", "YOASOBI - アイドル"], "kana first three")
    eq("aidoru YOASOBI" in v and "aidoru" in v and len(v) == 5, True, "kana gets romaji variants")
    eq(backend._query_variants("夜に駆ける", "YOASOBI"),
       ["夜に駆ける YOASOBI", "夜に駆ける", "YOASOBI - 夜に駆ける"], "kanji stays three")
    ok("query variants")


def test_pick_best_ladder():
    q = "夜に駆ける YOASOBI"
    ja = [cand("JaJaMatch01", "夜に駆ける", "YOASOBI \u2022 4:21", "4:21")]
    r = backend._pick_best(q, "夜に駆ける", "YOASOBI", 261000, ja)
    eq(r.get("videoId"), "JaJaMatch01", "same-script japanese picks the card")
    eq(r.get("confidence"), "aligned", "same-script japanese is an aligned match")
    eq(bool(r.get("best_guess")), False, "a confident match is never a guess")

    romaji = [cand("JpRomaji001", "Yoru ni Kakeru", "YOASOBI \u2022 4:21", "4:21")]
    r = backend._pick_best(q, "夜に駆ける", "YOASOBI", 261000, romaji)
    eq(r.get("videoId"), "JpRomaji001", "japanese source, romaji card")
    eq(r.get("confidence"), "aligned", "japanese -> romaji is aligned")
    eq(bool(r.get("best_guess")), False, "japanese -> romaji is not a guess")

    r = backend._pick_best("Yoru ni Kakeru YOASOBI", "Yoru ni Kakeru", "YOASOBI", 261000, ja)
    eq(r.get("videoId"), "JaJaMatch01", "romaji source, japanese card")
    eq(r.get("confidence"), "aligned", "romaji -> japanese is aligned")

    raises(lambda: backend._pick_best(q, "夜に駆ける", "YOASOBI", 261000,
                                     [cand("OtherSong1", "群青", "YOASOBI \u2022 4:08", "4:08")]),
           "no confident match", "different song never guesses")

    raises(lambda: backend._pick_best(q, "夜に駆ける", "YOASOBI", 261000,
                                     [cand("Slowed00001", "夜に駆ける (Slowed)", "YOASOBI \u2022 4:40", "4:40")]),
           "versions found", "a plain source never falls back to a slowed copy")

    g = backend._pick_best("群青 YOASOBI", "群青", "YOASOBI", 252000,
                           [cand("BestGuess01", "Ultramarine", "YOASOBI")])
    eq(g.get("videoId"), "BestGuess01", "crossing with no evidence lands rank 1")
    eq(bool(g.get("best_guess")), True, "crossing with no evidence is a guess")
    eq(bool(g.get("why")), True, "a guess records why")

    near = [cand("LatinNearA1", "Hello There", "Adele"),
            cand("LatinNearB1", "Hello Again", "Adele")]
    raises(lambda: backend._pick_best("Hello Adele", "Hello", "Adele", 0, near),
           "no confident match", "latin-only near-equal still raises exactly as before")
    ok("pick_best ladder")


def test_yt_match_policy():
    """A guess on an early variant never blocks a confident hit on a later one."""
    real = backend.yt_search
    try:
        def fake(q, title="", artist="", duration_ms=0):
            if q == "Yoru ni Kakeru YOASOBI":
                return {"videoId": "GuessFirst1", "title": "夜に駆ける", "artist": "YOASOBI",
                        "duration": "4:21", "score": 0.31, "best_guess": True,
                        "why": "cross-script best guess"}
            if q == "YOASOBI - Yoru ni Kakeru":
                return {"videoId": "Confident03", "title": "夜に駆ける", "artist": "YOASOBI",
                        "duration": "4:21", "score": 0.87, "confidence": "aligned"}
            raise RuntimeError("no confident match (best 0.20) for: " + q)
        backend.yt_search = fake
        r = backend.yt_match("Yoru ni Kakeru", "YOASOBI", 261000)
        eq(r.get("videoId"), "Confident03", "confident variant 3 beats a guess on variant 1")
        eq(r.get("via"), "YOASOBI - Yoru ni Kakeru", "via names the variant that hit")
        eq(bool(r.get("best_guess")), False, "a confident hit is not a guess")

        def only_guess(q, title="", artist="", duration_ms=0):
            score = 0.31 if q == "Yoru ni Kakeru YOASOBI" else 0.44
            return {"videoId": "Guess" + str(len(q)), "title": "夜に駆ける", "artist": "YOASOBI",
                    "duration": "", "score": score, "best_guess": True, "why": "cross-script best guess"}
        backend.yt_search = only_guess
        r = backend.yt_match("Yoru ni Kakeru", "YOASOBI", 261000)
        eq(bool(r.get("best_guess")), True, "with no confident variant the guess is returned")
        eq(r.get("score"), 0.44, "the highest-scoring guess wins")
        eq(bool(r.get("why")), True, "the guess keeps its reason")

        def nothing(q, title="", artist="", duration_ms=0):
            raise RuntimeError("no ytm result for: " + q)
        backend.yt_search = nothing
        r = backend.yt_match("Yoru ni Kakeru", "YOASOBI", 261000)
        eq(r.get("videoId"), None, "nothing found still skips")
        eq(bool(r.get("skipped")), True, "a skip keeps its reason")
    finally:
        backend.yt_search = real
    ok("yt_match guess policy")


def main():
    tests = (test_fold, test_scripts, test_romanize, test_aligned_title,
             test_variants, test_pick_best_ladder, test_yt_match_policy)
    for fn in tests:
        try:
            fn()
        except Exception as e:
            _failures.append(f"FAIL {fn.__name__}: {e}")
    for line in _failures:
        print(line)
    if _failures:
        return 1
    print(f"{len(tests)}/{len(tests)} language checks pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
