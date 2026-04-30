"""Tests for the built-in parser dict."""

from __future__ import annotations

from magicwand import parsers


def test_known_includes_bcftools():
    assert "bcftools" in parsers.known()


def test_bcftools_records():
    out = list(parsers.parse_line("SN  0  number of records:    15234", "bcftools"))
    assert out == [{"bcftools.records": 15234}]


def test_bcftools_tstv():
    out = list(parsers.parse_line("SN  0  ts/tv:    2.10", "bcftools"))
    assert out == [{"bcftools.tstv": 2.10}]


def test_bcftools_unrelated_line():
    assert list(parsers.parse_line("noise", "bcftools")) == []


def test_unknown_parser_returns_nothing():
    assert list(parsers.parse_line("anything", "does-not-exist")) == []
