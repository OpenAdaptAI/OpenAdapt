"""
Tests the 2 helper functions (clean_ascii and compare_text) in summary_mixin.py
"""
from puterbot.strategies.summary_mixin import clean_ascii, compare_text


################################################################
# Clean ASCII tests
################################################################


def test_clean_ascii_empty():
    empty_text = ""
    expected = ""
    actual = clean_ascii(empty_text)
    assert actual == expected


def test_clean_ascii_no_symbols_or_stopwords():
    no_symbols = "wow no symbols"
    expected = "wow no symbols"
    actual = clean_ascii(no_symbols)
    assert actual == expected


def test_clean_ascii_some_symbols_and_stopwords():
    many_symbols = "wow this! has some... symbols"
    expected = "wow has some symbols"
    actual = clean_ascii(many_symbols)
    assert actual == expected


def test_clean_ascii_all_symbols_and_stopwords():
    all_symbols = "&*@#($)#!| ~~  this  \\"
    expected = ""
    actual = clean_ascii(all_symbols)
    assert actual == expected


################################################################
# Compare text tests
################################################################


def test_compare_text_empty():
    text1 = ""
    text2 = ""
    expected = 0
    actual = compare_text(text1, text2)
    assert actual == expected


def test_compare_text_similar():
    text1 = "I love sunshine so much."
    text2 = "I adore the sun."
    expected = 50
    actual = compare_text(text1, text2)
    assert actual > expected


def test_compare_text_not_similar():
    text1 = "I love sunshine so much"
    text2 = "Once upon a time, there was a princess."
    expected = 50
    actual = compare_text(text1, text2)
    assert actual < expected
