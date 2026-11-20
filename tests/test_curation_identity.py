from __future__ import annotations

from types import SimpleNamespace

from app.curation.identity import assign_anchored_ids


def node(node_id, heading_num=None, title="", page_start=0, parent_id=None):
    return SimpleNamespace(
        node_id=node_id,
        heading_num=heading_num,
        title=title,
        page_start=page_start,
        parent_id=parent_id,
    )


def test_numbered_heading_anchors_on_number():
    nodes = [node("d:0000", "1", "1 范围"), node("d:0001", "1.1", "1.1 目的")]
    assert assign_anchored_ids("d", nodes) == ["d:h:1", "d:h:1.1"]


def test_unnumbered_heading_anchors_on_title_hash():
    nodes = [node("d:0000", None, "前言")]
    [node_id] = assign_anchored_ids("d", nodes)
    assert node_id.startswith("d:t:")
    assert len(node_id.split(":t:")[1]) == 8


def test_duplicate_number_disambiguates_by_page():
    nodes = [
        node("d:0000", "附录A", "附录A", page_start=10),
        node("d:0001", "附录A", "附录A", page_start=20),
    ]
    ids = assign_anchored_ids("d", nodes)
    assert ids == ["d:h:附录a@10", "d:h:附录a@20"]
    assert len(set(ids)) == 2


def test_same_number_and_page_gets_counter_suffix():
    nodes = [
        node("d:0000", "6", "6 试验", page_start=5),
        node("d:0001", "6", "6 试验", page_start=5),
        node("d:0002", "6", "6 试验", page_start=5),
    ]
    ids = assign_anchored_ids("d", nodes)
    assert len(set(ids)) == 3
    assert ids[0] == "d:h:6@5"


def test_ids_are_stable_when_a_sibling_is_inserted_before():
    """The whole point: inserting a node must not move the others' ids."""
    before = [node("d:0000", "1", "1 范围"), node("d:0001", "2", "2 引用")]
    after = [
        node("d:0000", "1", "1 范围"),
        node("d:0001", "1.1", "1.1 新增"),
        node("d:0002", "2", "2 引用"),
    ]
    before_ids = {n.title: i for n, i in zip(before, assign_anchored_ids("d", before))}
    after_ids = {n.title: i for n, i in zip(after, assign_anchored_ids("d", after))}
    assert after_ids["1 范围"] == before_ids["1 范围"]
    assert after_ids["2 引用"] == before_ids["2 引用"]


def test_legacy_fallback_root_keeps_its_id():
    nodes = [node("d:root", None, "root", page_start=0)]
    assert assign_anchored_ids("d", nodes) == ["d:root"]


def test_unnumbered_untitled_falls_back_to_page():
    nodes = [node("d:0000", None, "", page_start=7)]
    assert assign_anchored_ids("d", nodes) == ["d:p:7:0"]
