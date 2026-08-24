"""Deleting a library task that a sequence still references must warn the operator UP
FRONT (which sequences use it) and abort — instead of only failing after they confirm,
or silently dangling those sequence steps."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from api import models as m
from ui import library_panels
from ui.library_panels import LibraryTasksPanel

_app = QApplication.instance() or QApplication([])


class FakeStore:
    def __init__(self, users):
        self._users = users

    def sequences_using_task(self, name):
        return list(self._users.get(name, []))


class FakeFleet:
    def __init__(self, store):
        self._store = store

    def library_store(self):
        return self._store


def _panel(users):
    # Bare instance — we exercise _on_delete's guard without building the widget tree.
    p = LibraryTasksPanel.__new__(LibraryTasksPanel)
    p.hub = type("H", (), {"fleet": FakeFleet(FakeStore(users))})()
    return p


def test_delete_blocked_and_warned_when_task_in_use(monkeypatch):
    warned = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: warned.setdefault("args", a))
    # confirm_delete / _client must NOT be reached when the task is in use.
    monkeypatch.setattr(library_panels, "confirm_delete",
                        lambda *a, **k: pytest.fail("confirm_delete reached"))
    p = _panel({"tx": ["morning-broadcast", "evening-broadcast"]})
    p._client = lambda: pytest.fail("delete_task reached")

    p._on_delete(m.ProcessStatus(name="tx", description="", state="stopped"))

    body = warned["args"][2]                    # (self, title, text)
    assert "morning-broadcast" in body and "evening-broadcast" in body
    assert "2 sequence" in body


def test_unused_task_reports_no_referencing_sequences():
    # An unused task returns no references, so _on_delete's `if used:` guard is skipped
    # and the normal confirm/delete path runs.
    p = _panel({"other": ["some-seq"]})         # a DIFFERENT task is in use
    assert p._sequences_using("tx") == []


def test_sequences_using_survives_missing_store():
    p = LibraryTasksPanel.__new__(LibraryTasksPanel)
    p.hub = type("H", (), {"fleet": type("F", (), {"library_store": lambda s: None})()})()
    assert p._sequences_using("tx") == []       # degrades to [] (delete path backstops)
