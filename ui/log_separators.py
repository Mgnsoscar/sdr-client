"""A tiny helper that inserts a dashed separator line above each top-level log entry.

The task / sequence / plan log views all stream text into a QPlainTextEdit. To visually
separate entries we put a line of ``-`` above each STEP — for the sequence/plan logs that's
each ``[HH:MM:SS…] …`` choreography line (its indented device output stays grouped beneath,
un-separated); for the task log it's each line that STARTS A NEW TIMESTAMPED RECORD
(continuation lines — indented output, tracebacks, banners, ``RESULT …`` lines, blanks —
stay grouped beneath the preceding record).

The stream arrives in arbitrary chunks that may split mid-line, so we can't decide "is this a
step?" on a chunk boundary. ``StepSeparators.feed`` buffers a trailing partial line until its
newline arrives, then prepends the separator to whole lines the predicate flags. No leading
separator is emitted before the very first line (so the view has no stray top rule).
"""
from __future__ import annotations

import re
from typing import Callable

SEPARATOR = "-" * 56


class StepSeparators:
    def __init__(self, is_step: Callable[[str], bool], separator: str = SEPARATOR):
        self._is_step = is_step
        self._sep = separator
        self._pending = ""       # trailing partial line, held until its newline arrives
        self._emitted = False    # any line emitted yet? (suppresses a leading separator)

    def reset(self) -> None:
        """Forget buffered state — call when the view is cleared."""
        self._pending = ""
        self._emitted = False

    def feed(self, chunk: str) -> str:
        """Transform a streamed chunk: return the text to append, with a separator line
        inserted above each step line. Buffers an incomplete trailing line for next time."""
        data = self._pending + chunk
        parts = data.split("\n")
        self._pending = parts.pop()          # last element is the incomplete line (or "")
        out = []
        for line in parts:
            if self._emitted and self._is_step(line):
                out.append(self._sep)
            out.append(line)
            self._emitted = True
        return ("\n".join(out) + "\n") if out else ""


def _bracket_step(line: str) -> bool:
    """A sequence/plan top-level entry — the agent annotations, e.g. '[10:53:47] HELD …'.
    Indented device output ('    mock_prn: …') and value rows are grouped beneath, not
    separated."""
    return line.startswith("[")


# A task-log RECORD START: a line beginning with a full 'YYYY-MM-DD HH:MM:SS' (optionally a
# 'T' date/time separator and a ',mmm'/'.mmm' fraction) OR a bare 'HH:MM:SS[,mmm]'. Anchored
# at column 0, so INDENTED continuation lines never match. A real mock task-log record looks
# like "2026-09-09 11:32:00,465 INFO   power (target) : -190 …".
_TIMESTAMP_RE = re.compile(
    r"""^(?:
        \d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?   # YYYY-MM-DD HH:MM:SS[,mmm]
      | \d{2}:\d{2}:\d{2}(?:[.,]\d+)?                        # HH:MM:SS[,mmm]
    )""",
    re.VERBOSE,
)


def _timestamp_step(line: str) -> bool:
    """A task-log entry — a line that STARTS A NEW TIMESTAMPED RECORD. Continuation lines
    (indented output, tracebacks, banners like the mock's '── … ──' rule, 'RESULT …' lines,
    blanks) don't match, so they stay grouped under the preceding record."""
    return bool(_TIMESTAMP_RE.match(line))


def sequence_separators() -> StepSeparators:
    return StepSeparators(_bracket_step)


def task_separators() -> StepSeparators:
    return StepSeparators(_timestamp_step)
