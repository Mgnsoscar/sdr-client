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


class _FakeExecutor:
    def __init__(self):
        self.submitted = 0

    def submit(self, fn):
        self.submitted += 1

    def shutdown(self, **kwargs):
        pass


def test_run_async_after_stop_does_not_raise():
    # Stopping shuts the executor down; a stream-status callback firing during
    # teardown then calls run_async. It must not raise "cannot schedule new
    # futures after shutdown".
    hub = _hub()
    hub.stop()
    hub.run_async("late", lambda: 1)      # must be a silent no-op
    assert hub._stopped is True


def test_run_async_after_stop_never_touches_the_executor():
    hub = _hub()
    hub.stop()
    fake = _FakeExecutor()
    hub._executor = fake                   # would record any submit attempt
    hub.run_async("late", lambda: 1)
    assert fake.submitted == 0             # guarded out before submitting


def test_run_async_before_stop_still_submits():
    # The guard must not break normal operation.
    hub = _hub()
    fake = _FakeExecutor()
    hub._executor = fake
    hub.run_async("x", lambda: 1)
    assert fake.submitted == 1
    hub.stop()
