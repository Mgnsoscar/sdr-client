"""The dashed step separators inserted into the task / sequence / plan log views
(ui/log_separators.py) — pure text transform, no Qt needed."""
from ui.log_separators import (
    SEPARATOR, StepSeparators, sequence_separators, task_separators,
)


def test_sequence_separator_above_bracket_steps_only():
    s = sequence_separators()
    text = s.feed(
        "===== sequence 'x' run r =====\n"
        "[10:00:00] armed\n"
        "[10:00:01] start mock_prn\n"
        "    mock_prn: 2026 INFO gain 0\n"
        "    mock_prn: 2026 INFO amp 0.5\n"
        "[10:00:02] tune mock_prn\n")
    lines = text.split("\n")
    # No leading separator (header is first). A separator sits above each [..] step, and the
    # indented device output stays grouped beneath its step (no separator above those).
    assert lines[0] == "===== sequence 'x' run r ====="
    assert lines[1] == SEPARATOR and lines[2] == "[10:00:00] armed"
    assert lines[3] == SEPARATOR and lines[4] == "[10:00:01] start mock_prn"
    assert lines[5] == "    mock_prn: 2026 INFO gain 0"        # device output — no separator
    assert lines[6] == "    mock_prn: 2026 INFO amp 0.5"
    assert lines[7] == SEPARATOR and lines[8] == "[10:00:02] tune mock_prn"
    assert lines.count(SEPARATOR) == 3                          # one per step, none leading


def test_task_separator_above_each_line():
    s = task_separators()
    text = s.feed("line one\nline two\nline three\n")
    lines = text.split("\n")
    assert lines == ["line one", SEPARATOR, "line two", SEPARATOR, "line three", ""]


def test_partial_lines_buffered_across_chunks():
    s = sequence_separators()
    # First chunk ends mid-line; the partial is held until the newline arrives.
    a = s.feed("[10:00:00] armed\n[10:00:01] star")
    assert a == "[10:00:00] armed\n"                            # only the complete line (no leading sep)
    b = s.feed("t mock_prn\n")
    assert b == SEPARATOR + "\n[10:00:01] start mock_prn\n"     # the rejoined line gets its separator


def test_reset_clears_pending_and_leading_suppression():
    s = sequence_separators()
    s.feed("[10:00:00] armed\n[10:00:01] star")                 # leaves a pending partial + emitted=True
    s.reset()
    # After reset the next first line has no leading separator and the old partial is gone.
    assert s.feed("[10:00:02] start\n") == "[10:00:02] start\n"


def test_blank_task_lines_are_not_steps():
    s = task_separators()
    text = s.feed("first\n\nsecond\n")
    # A blank line is not a step (no separator above it); the next real line still gets one.
    assert text.split("\n") == ["first", "", SEPARATOR, "second", ""]
