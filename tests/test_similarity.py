"""Near-duplicate detection: same content hashes close, different does not."""

from __future__ import annotations

from app.index.similarity import fingerprint, hamming, similar_to, simhash64

JOB_A = (
    "炽橙数字 智能体工程师 薪资 18-35K 职责 负责智能体平台的设计与开发 "
    "要求 熟练掌握 Python 三年以上经验 有 AI 工具作品优先"
)
JOB_A_REPRINT = (
    "炽橙数字 智能体工程师 薪资 18-35K 职责 负责智能体平台的设计与开发 "
    "要求 熟练掌握 Python 三年以上经验 有 AI 工具作品优先"
)
JOB_B = (
    "海康威视 嵌入式工程师 薪资 20-40K 职责 负责摄像头固件开发 "
    "要求 精通 C 语言 五年以上经验"
)


def test_identical_text_has_zero_distance():
    left = fingerprint("炽橙", JOB_A)
    right = fingerprint("炽橙", JOB_A_REPRINT)
    assert left is not None
    assert hamming(left, right) == 0


def test_unrelated_postings_are_far_apart():
    left = fingerprint("炽橙", JOB_A)
    right = fingerprint("海康", JOB_B)
    assert hamming(left, right) > 3


def test_short_text_is_not_fingerprinted():
    assert simhash64("只有几个词") is None
    assert fingerprint("标题", "短") is None


def test_similar_to_flags_a_near_duplicate():
    own = fingerprint("炽橙", JOB_A)
    other = fingerprint("炽橙", JOB_A_REPRINT)
    matches = similar_to(
        title="炽橙",
        body=JOB_A,
        simhash=own,
        candidates=[{"doc_id": "d1", "title": "炽橙", "simhash": other}],
    )
    assert len(matches) == 1
    assert matches[0]["doc_id"] == "d1"
    assert matches[0]["strong"] is True


def test_similar_to_matches_on_document_key():
    matches = similar_to(
        title="ACME 100.56-2023 方法",
        body="",
        simhash=None,
        candidates=[
            {"doc_id": "d2", "title": "ACME_100.56-2023 方法", "simhash": None}
        ],
    )
    assert matches and matches[0]["reason"] == "same document key"
    assert matches[0]["strong"] is True


def test_similar_to_returns_empty_for_unrelated():
    matches = similar_to(
        title="炽橙",
        body=JOB_A,
        simhash=fingerprint("炽橙", JOB_A),
        candidates=[{"doc_id": "d3", "title": "海康", "simhash": fingerprint("海康", JOB_B)}],
    )
    assert matches == []
