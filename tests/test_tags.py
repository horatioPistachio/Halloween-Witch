"""Tag parser tests."""

from witch.tags import MAX_TTS_CHARS, parse, speech_text


def test_leading_tag_is_recorded_and_removed():
    r = parse("[cackle] Ooh, a little ghost!")
    assert r.tags == ("cackle",)
    assert r.clean_text == "Ooh, a little ghost!"
    assert [s.tag for s in r.segments] == ["cackle"]


def test_middle_and_trailing_tags():
    r = parse("Come closer [whispers] dearie [pause]")
    assert r.tags == ("whispers", "pause")
    assert r.clean_text == "Come closer dearie"
    assert [(s.tag, s.text) for s in r.segments] == [
        (None, "Come closer"),
        ("whispers", "dearie"),
        ("pause", ""),
    ]


def test_tag_order_is_preserved():
    r = parse("[gasp] Boo! [cackle] Ha!")
    assert r.tags == ("gasp", "cackle")


def test_unknown_tag_is_removed_and_reported():
    r = parse("Hi [wizard] there")
    assert r.clean_text == "Hi there"
    assert r.tags == ()
    assert r.unknown_tags == ("wizard",)


def test_no_tags_gives_single_plain_segment():
    r = parse("  Trick or treat.  ")
    assert r.tags == ()
    assert r.clean_text == "Trick or treat."
    assert [(s.tag, s.text) for s in r.segments] == [(None, "Trick or treat.")]


def test_empty_input_returns_empty_response():
    r = parse("")
    assert r.clean_text == ""
    assert r.tags == ()
    assert r.segments == ()


def test_whitespace_and_punctuation_normalised():
    assert parse("Hello [cackle] !").clean_text == "Hello!"


def test_uppercase_tag_is_recognised():
    assert parse("[CACKLE] hi").tags == ("cackle",)


def test_multiword_tag():
    r = parse("[evil laugh] Ha ha")
    assert r.tags == ("evil laugh",)
    assert r.clean_text == "Ha ha"


def test_tag_inside_word_does_not_add_space():
    assert parse("Hel[pause]lo").clean_text == "Hello"


def test_malformed_brackets_do_not_raise():
    assert "hi" in parse("[[cackle] hi").clean_text
    r = parse("[cackle hi")
    assert r.tags == ()
    assert r.clean_text == "[cackle hi"


def test_speech_text_keeps_tags_in_place():
    r = parse("Ooh, hello. [whispers] Come closer. [evil laugh]")
    assert speech_text(r, keep_tags=True) == "Ooh, hello. [whispers] Come closer. [evil laugh]"


def test_speech_text_without_tags_matches_clean_text():
    r = parse("[cackle] Hi [pause] there")
    assert speech_text(r, keep_tags=False) == r.clean_text


def test_speech_text_never_contains_unknown_tags():
    r = parse("Hi [wizard] there [whispers] ok")
    for keep in (True, False):
        assert "wizard" not in speech_text(r, keep_tags=keep)


def test_speech_text_empty_input():
    assert speech_text(parse("  "), keep_tags=True) == ""
    assert speech_text(parse("  "), keep_tags=False) == ""


def test_speech_text_truncates_at_sentence_end():
    text = speech_text(parse("Hello there. " * 100), keep_tags=False)
    assert len(text) <= MAX_TTS_CHARS
    assert text.endswith(".")
