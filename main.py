"""
SDR Broadcaster Control — application entry point.

Run:
    python main.py

Loads units.yaml, builds the fleet, warms up connections in the background,
opens an outbound SSE event stream per unit and starts the poller, then shows
the window.
"""
from __future__ import annotations

import logging
import sys

from PyQt6.QtWidgets import QApplication

from api import AgentClient, Fleet
from config import ClientConfig
from state import LibraryStore, LibraryClient
from ui.main_window import MainWindow
from ui.qt_adapter import DataHub
from ui.theme import apply_theme

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sdr-client")


def build_fleet(cfg: ClientConfig) -> Fleet:
    fleet = Fleet()
    for entry in cfg.units:
        client = AgentClient(entry.uid, label=entry.label, addresses=entry.addresses,
                             api_key=entry.api_key)
        client.machine_id = entry.machine_id     # seed the known fingerprint
        fleet.add(client)
    return fleet


def main() -> int:
    cfg = ClientConfig.load()

    app = QApplication(sys.argv)
    app.setApplicationName("SDR Broadcaster Control")
    apply_theme(app)

    fleet = build_fleet(cfg)
    # The offline shared library, resolvable as fleet.get(LIBRARY_HOST) so the
    # unit-card panels/editors can author it without a unit connected.
    fleet.set_library(LibraryClient(LibraryStore()))
    hub = DataHub(fleet, api_secret=cfg.api_key)

    window = MainWindow(hub)
    window.show()

    # Start data flow after the window is up.
    hub.start()

    # Warm up connections off the GUI thread so the slow first mDNS resolves don't
    # freeze the UI. The SSE streams connect on their own threads; the poller and
    # streams populate the UI as data arrives.
    hub.run_async("warmup_all", lambda: fleet.warmup_all())

    exit_code = app.exec()
    hub.stop()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())