import pytest
from evolution.reflexion.retriever import format_reflections_block


def test_xml_escaping():
    refs = [{"category": "style", "content": "<script>alert('xss')</script>", "group_folder": None}]
    result = format_reflections_block(refs)
    assert "&lt;script&gt;" in result
    assert "<script>" not in result


def test_trusted_false_when_no_group():
    # Cross-group (no folder) has widest blast radius — untrusted.
    refs = [{"category": "style", "content": "test", "group_folder": None}]
    result = format_reflections_block(refs, group_folder=None)
    assert 'trusted="false"' in result


def test_trusted_true_when_group():
    # Group-scoped content is more constrained — trusted.
    refs = [{"category": "style", "content": "test", "group_folder": "whatsapp_123"}]
    result = format_reflections_block(refs, group_folder="whatsapp_123")
    assert 'trusted="true"' in result


def test_content_capped_at_500():
    refs = [{"category": "style", "content": "x" * 600, "group_folder": None}]
    result = format_reflections_block(refs)
    # Each line has prefix like "[1] (style) " so content portion should be 500 chars
    for line in result.split("\n"):
        if line.startswith("[1]"):
            content_part = line.split(") ", 1)[1]
            assert len(content_part) <= 500


def test_max_10_reflections():
    refs = [{"category": "style", "content": f"ref {i}", "group_folder": None} for i in range(15)]
    result = format_reflections_block(refs)
    assert result.count("[") == 10  # only 10 numbered entries


def test_empty_list_returns_empty():
    assert format_reflections_block([]) == ""
