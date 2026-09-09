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
from datetime import datetime, timezone
from typing import Callable, Optional

SEPARATOR = "-" * 56


class StepSeparators:
    def __init__(self, is_step: Callable[[str], bool], separator: str = SEPARATOR,
                 transform: Optional[Callable[[str], str]] = None,
                 drop: Optional[Callable[[str], bool]] = None):
        self._is_step = is_step
        self._sep = separator
        self._transform = transform          # optional per-line rewrite (e.g. timezone localize)
        self._drop = drop                    # optional per-line filter (e.g. hide script output)
        self._pending = ""       # trailing partial line, held until its newline arrives
        self._emitted = False    # any line emitted yet? (suppresses a leading separator)

    def reset(self) -> None:
        """Forget buffered state — call when the view is cleared."""
        self._pending = ""
        self._emitted = False
        if self._transform is not None and hasattr(self._transform, "reset"):
            self._transform.reset()

    def feed(self, chunk: str) -> str:
        """Transform a streamed chunk: return the text to append, with a separator line
        inserted above each step line. Buffers an incomplete trailing line for next time."""
        data = self._pending + chunk
        parts = data.split("\n")
        self._pending = parts.pop()          # last element is the incomplete line (or "")
        out = []
        for line in parts:
            if self._transform is not None:  # applied to WHOLE lines only (no chunk-split hazard)
                line = self._transform(line)
            if self._drop is not None and self._drop(line):
                continue                     # a dropped line gets no separator and never "emits"
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


# ── Timezone localization for the sequence/plan run log ────────────────────────────────────────
# The agent stamps the run log in UTC — each '[HH:MM:SS(.mmm)] …' step line and the ISO on-air
# times in the '===== … on-air <ISO> → <ISO> =====' header. The operator reads it on their PC, so
# we rewrite those to the machine's LOCAL timezone at display time (the PC's timezone is only known
# here). The run's UTC date, captured from the header, reconstructs a full instant for each
# time-only [HH:MM:SS] so the offset is the right one for that date; before a header is seen it
# falls back to today. Purely presentational — nothing but the displayed digits changes.
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)")
_LEAD_CLOCK_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})(\.\d+)?\]")


def _parse_iso_utc(s: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class _SeqTimeLocalizer:
    """Rewrite a sequence/plan run-log line's UTC timestamps to the PC's local timezone."""

    def __init__(self):
        self._date = None                    # the run's UTC date, from the header (for [HH:MM:SS])

    def reset(self) -> None:
        self._date = None

    def __call__(self, line: str) -> str:
        if _ISO_RE.search(line):             # a header line: convert its ISO on-air times to local
            return _ISO_RE.sub(self._iso_sub, line)
        if _LEAD_CLOCK_RE.match(line):        # a '[HH:MM:SS] …' step line
            return _LEAD_CLOCK_RE.sub(self._clock_sub, line, count=1)
        return line

    def _iso_sub(self, m: "re.Match") -> str:
        dt = _parse_iso_utc(m.group(0))
        if dt is None:
            return m.group(0)
        if self._date is None:
            self._date = dt.date()
        return dt.astimezone().isoformat()

    def _clock_sub(self, m: "re.Match") -> str:
        d = self._date or datetime.now(timezone.utc).date()
        try:
            utc = datetime(d.year, d.month, d.day, int(m.group(1)), int(m.group(2)),
                           int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return m.group(0)
        return f"[{utc.astimezone().strftime('%H:%M:%S')}{m.group(4) or ''}]"


# ── Hide script-produced output in the sequence/plan run log ───────────────────────────────────
# The agent interleaves each transmit script's own stdout into the run log, prefixed '  <task>: '
# (two spaces + the task name + a colon — see agent RunLog._emit_line). The agent's own
# choreography is either a '[HH:MM:SS] …' line or a deeper-indented value row, so this prefix
# uniquely marks a program-output line. Deployed signals can be very chatty, so the run-log views
# offer a "Hide script output" toggle (default on) that drops exactly these lines, leaving the
# clean armed / ON AIR / step / OFF AIR choreography.
_PROGRAM_LINE_RE = re.compile(r"^  [^\s:]+: ")


def is_program_output(line: str) -> bool:
    """A run-log line that is a transmit script's own stdout (agent-prefixed '  <task>: …')."""
    return bool(_PROGRAM_LINE_RE.match(line))


def sequence_separators(hide_program: bool = False) -> StepSeparators:
    """Separators for the sequence/plan run log. ``hide_program`` drops the interleaved script
    stdout lines (leaving the agent's choreography), and localizes UTC timestamps to the PC's tz."""
    return StepSeparators(_bracket_step, transform=_SeqTimeLocalizer(),
                          drop=is_program_output if hide_program else None)


def task_separators() -> StepSeparators:
    return StepSeparators(_timestamp_step)
