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


def test_task_separator_above_timestamped_record_starts_only():
    s = task_separators()
    # Two real records, each with a non-timestamped continuation line beneath it. A separator
    # sits above each record START; the continuation lines stay grouped (no separator).
    text = s.feed(
        "2026-09-09 11:32:00,465 INFO   power (target) : -190\n"
        "    gain would be 0.0 dB\n"
        "RESULT gain_db=0.0 power_dbm=-190 source=cal\n"
        "2026-09-09 11:32:01,001 INFO   power (target) : -180\n"
        "── mock_cw self-test ──\n")
    lines = text.split("\n")
    assert lines[0] == "2026-09-09 11:32:00,465 INFO   power (target) : -190"   # first, no leading sep
    assert lines[1] == "    gain would be 0.0 dB"                               # continuation — no sep
    assert lines[2] == "RESULT gain_db=0.0 power_dbm=-190 source=cal"          # continuation — no sep
    assert lines[3] == SEPARATOR and lines[4].endswith("power (target) : -180")  # next record start
    assert lines[5] == "── mock_cw self-test ──"                               # banner continuation
    assert lines.count(SEPARATOR) == 1                                          # one per record, none leading


def test_bare_hhmmss_is_a_record_start():
    s = task_separators()
    text = s.feed("11:32:00 first record\n  indented continuation\n11:32:01 second record\n")
    lines = text.split("\n")
    assert lines[0] == "11:32:00 first record"                # first, no leading sep
    assert lines[1] == "  indented continuation"              # continuation — no sep
    assert lines[2] == SEPARATOR and lines[3] == "11:32:01 second record"
    assert lines.count(SEPARATOR) == 1


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


def test_blank_and_plain_task_lines_are_not_record_starts():
    s = task_separators()
    text = s.feed(
        "2026-09-09 11:32:00,465 INFO   started\n"
        "\n"                                             # blank — not a record start
        "plain continuation line\n"                      # non-timestamped — not a record start
        "2026-09-09 11:32:02,000 INFO   done\n")
    lines = text.split("\n")
    assert lines[0].endswith("started")                  # first record, no leading sep
    assert lines[1] == "" and lines[2] == "plain continuation line"   # neither gets a separator
    assert lines[3] == SEPARATOR and lines[4].endswith("done")        # only the next record start
    assert lines.count(SEPARATOR) == 1
