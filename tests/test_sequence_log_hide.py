"""The sequence run-log dialog's "Hide script output" toggle (ui/sequence_log_dialog.py).

Defaults to ON (deployed signals are chatty); toggling re-renders the buffered log so the script
lines appear/disappear without losing the agent's choreography.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from api import models as m
from ui.sequence_log_dialog import SequenceLogDialog

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


class _FakeFleet:
    def get(self, host):                              # no live unit — the dialog degrades gracefully
        raise KeyError(host)


class _FakeHub:
    fleet = _FakeFleet()


_LOG = (
    "===== run =====\n"
    "[10:00:00] armed\n"
    "[10:00:01] start mock_prn\n"
    "  mock_prn: INFO would command gain 0\n"          # a chatty script line
    "  mock_prn: INFO amplitude 0.5\n"
    "[10:00:02] ON AIR (T0)\n")


def _dialog():
    return SequenceLogDialog(_FakeHub(), "h1", m.Sequence(id="s1", name="Seq", steps=[]))


def test_hide_script_output_defaults_on():
    dlg = _dialog()
    try:
        assert dlg._hide_prog.isChecked() is True
        dlg._append(_LOG)
        shown = dlg._view.toPlainText()
        assert "would command gain 0" not in shown         # script output hidden by default
        assert "amplitude 0.5" not in shown
        assert "start mock_prn" in shown                   # choreography kept
        assert "ON AIR (T0)" in shown
    finally:
        dlg.deleteLater()


def test_toggle_shows_then_hides_script_output_re_rendering_the_buffer():
    dlg = _dialog()
    try:
        dlg._append(_LOG)
        dlg._hide_prog.setChecked(False)                   # show
        shown = dlg._view.toPlainText()
        assert "would command gain 0" in shown             # re-rendered from the raw buffer
        assert "amplitude 0.5" in shown
        assert "start mock_prn" in shown                   # choreography still there
        dlg._hide_prog.setChecked(True)                    # hide again
        assert "would command gain 0" not in dlg._view.toPlainText()
    finally:
        dlg.deleteLater()
