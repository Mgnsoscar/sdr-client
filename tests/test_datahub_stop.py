"""DataHub.stop() must tear down cleanly — the path closeEvent takes when the
window closes. Regression for an AttributeError on a non-existent shared log
tailer that surfaced as a traceback after the UI had already closed."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from api import Fleet
from ui.qt_adapter import DataHub

_app = QApplication.instance() or QApplication([])


def _hub():
    return DataHub(Fleet(), api_secret="")


def test_stop_on_never_started_hub_does_not_raise():
    hub = _hub()
    hub.stop()                 # closeEvent calls this; must not raise
    assert hub._stopped is True


def test_stop_is_idempotent():
    # closeEvent and main() both call stop(); the second call must be a no-op.
    hub = _hub()
    hub.stop()
    hub.stop()
    assert hub._stopped is True
