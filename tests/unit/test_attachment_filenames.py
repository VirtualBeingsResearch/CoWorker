from __future__ import annotations

from pathlib import Path

from coworker.channels.filenames import safe_attachment_filename


def _safe(value: str) -> str:
    return safe_attachment_filename(value, fallback="attachment")


def test_replaces_characters_that_windows_rejects() -> None:
    assert _safe('report:final?.pdf') == "report_final_.pdf"
    assert _safe("a<b>c|d*e.txt") == "a_b_c_d_e.txt"
    assert _safe('quoted"name".csv') == "quoted_name_.csv"
    assert _safe("control\x00char\x1f.txt") == "control_char_.txt"


def test_keeps_only_the_last_path_segment() -> None:
    assert _safe("../../evil.png") == "evil.png"
    assert _safe(r"..\..\evil.png") == "evil.png"
    assert _safe("/etc/passwd") == "passwd"
    assert _safe(r"C:\Windows\notepad.exe") == "notepad.exe"


def test_falls_back_when_nothing_usable_remains() -> None:
    assert _safe("") == "attachment"
    assert _safe("   ") == "attachment"
    assert _safe("..") == "attachment"
    assert _safe(r"..\..") == "attachment"


def test_clamps_length_but_keeps_a_short_suffix() -> None:
    clamped = _safe("x" * 300 + ".png")

    assert len(clamped) == 180
    assert clamped.endswith(".png")


def test_no_illegal_character_survives() -> None:
    for name in ('a:b', "a?b", "a*b", "a|b", "a<b>c", '"a"', "a\x00b", "a\\b", "a/b"):
        cleaned = _safe(name)

        assert Path(cleaned).name == cleaned
        assert not set(cleaned) & set('<>:"/\\|?*')


def test_sanitized_names_can_actually_be_written(tmp_path: Path) -> None:
    for name in ('report:final?.pdf', "a<b>c|d*e.txt", "pipe|name.csv", "a\x00b.txt"):
        target = tmp_path / safe_attachment_filename(name, fallback="attachment")

        target.write_bytes(b"payload")

        assert target.read_bytes() == b"payload"
