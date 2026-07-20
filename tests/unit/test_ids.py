from coworker.core.ids import new_compact_id


def test_compact_id_has_fixed_url_safe_length():
    value = new_compact_id()

    assert len(value) == 12
    assert all(ch.isalnum() or ch in "-_" for ch in value)


def test_compact_id_prefix_does_not_reduce_entropy():
    value = new_compact_id("req_")

    assert value.startswith("req_")
    assert len(value) == 16
