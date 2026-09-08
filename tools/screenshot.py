#!/usr/bin/env python3
"""screenshot.py — grab a PNG of the real client (built exactly like main.py)
driving a live local agent, headless via Qt's offscreen platform. Handy to
*see* a change without a monitor. Recipe: sdr-agent/docs/local-integration-run.md.

Usage (start sdr-agent/deploy/run_local.sh first, backgrounded):

    QT_QPA_PLATFORM=offscreen python3 tools/screenshot.py --tab units --out /tmp/units.png

It builds its OWN throwaway client instance (it does not attach to a separately
running one), waits for the poller to populate, switches to the requested tab,
grabs the window, then quits. If SDR_CLIENT_DATA_DIR has no units.yaml it seeds
one pointing at --agent (default: this host's first IP), so it is self-contained.
"""
from __future__ import annotations
import argparse
import logging
import os
import subprocess
import sys

# Make `import paths` / `import ui...` work no matter the CWD or PYTHONPATH:
# the client repo root is this file's parent-of-parent (tools/ is one level down).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Default the client's writable state to the shared scratch dir (matching
# tools/run_local.sh), NOT the repo root — this MUST be set before importing
# `paths`/`config`, which capture the units.yaml path at import time. Keeps the
# repo clean and makes the harness self-contained.
_RUN_DIR = os.environ.get("SDR_LOCAL_RUN_DIR", "/tmp/sdr-local")
os.environ.setdefault("SDR_CLIENT_DATA_DIR", os.path.join(_RUN_DIR, "client-data"))

from PyQt6.QtCore import Qt, QTimer          # noqa: E402
from PyQt6.QtWidgets import QApplication      # noqa: E402

import paths                                  # noqa: E402
from api import AgentClient, Fleet            # noqa: E402
from config import ClientConfig               # noqa: E402
from state import LibraryStore, LibraryClient  # noqa: E402
from ui.click_focus import ClickFocusFilter   # noqa: E402
from ui.no_wheel import NoWheelFilter         # noqa: E402
from ui.main_window import MainWindow         # noqa: E402
from ui.qt_adapter import DataHub             # noqa: E402
from ui.theme import apply_theme              # noqa: E402

logging.basicConfig(level=logging.WARNING)
_TABS = {"timeline": 0, "units": 1, "library": 2}
# "calibration" is a pseudo-tab: Units → first unit → Calibration sub-tab.
_CHOICES = sorted(_TABS) + ["calibration"]


def _seed_units_if_missing(agent_host: str) -> None:
    data_dir = paths.data_dir()
    units = data_dir / "units.yaml"
    if units.exists():
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    units.write_text(
        "api_key: \"\"\n"
        "units:\n"
        "  - label: Broadcaster Lab\n"
        "    type: broadcaster\n"
        "    addresses:\n"
        f"      - {agent_host}\n"
    )
    print(f"seeded {units} -> agent {agent_host}:8765")


def _build_fleet(cfg):
    fleet = Fleet()
    for e in cfg.units:
        c = AgentClient(e.uid, label=e.label, addresses=e.addresses,
                        api_key=e.api_key, unit_type=e.type)
        c.machine_id = e.machine_id
        fleet.add(c)
    return fleet


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="/tmp/client.png", help="output PNG path")
    ap.add_argument("--tab", default="units", choices=_CHOICES,
                    help="tab to show (default: units); 'calibration' drills into "
                         "the first unit's Calibration panel")
    ap.add_argument("--agent", default=os.environ.get("SDR_AGENT_HOST", ""),
                    help="agent host to seed into units.yaml if absent "
                         "(default: this host's first IP)")
    ap.add_argument("--settle-ms", type=int, default=7000,
                    help="ms to wait for the poller before grabbing")
    ap.add_argument("--size", default="1500x950", help="window size WxH")
    args = ap.parse_args()

    agent_host = args.agent
    if not agent_host:
        try:
            agent_host = subprocess.check_output(["hostname", "-I"]).split()[0].decode()
        except Exception:
            agent_host = "127.0.0.1"
    _seed_units_if_missing(agent_host)
    w, h = (int(x) for x in args.size.lower().split("x"))

    paths.seed_defaults()
    cfg = ClientConfig.load()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_Use96Dpi, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("SDR Broadcaster Control")
    apply_theme(app)
    app.installEventFilter(ClickFocusFilter(app))
    app.installEventFilter(NoWheelFilter(app))

    fleet = _build_fleet(cfg)
    fleet.set_library(LibraryClient(LibraryStore()))
    hub = DataHub(fleet, api_secret=cfg.api_key)
    window = MainWindow(hub)
    window.resize(w, h)
    window.show()
    hub.start()
    hub.run_async("warmup_all", lambda: fleet.warmup_all())

    def switch():
        window.resize(w, h)
        settle = 2500
        try:
            if args.tab == "calibration":
                window._select_tab(1)                    # Units
                ut = window.units_tab
                units = list(hub.fleet.units())
                if units:
                    host = units[0].hostname
                    ut._detail.set_unit(host)
                    ut._stack.setCurrentIndex(1)         # detail view
                    ut._detail._select_subtab(2)         # Calibration sub-tab
                settle = 4000                            # panel fetches /calibration async
            else:
                window._select_tab(_TABS[args.tab])
        except Exception as exc:            # noqa: BLE001 — best-effort tab switch
            print(f"tab switch failed: {exc}")
        app.processEvents()
        QTimer.singleShot(settle, grab)     # let the tab fetch its data

    def grab():
        window.resize(w, h)
        app.processEvents()
        pm = window.grab()
        pm.save(args.out, "PNG")
        print(f"SAVED {args.out} {pm.width()}x{pm.height()} (tab={args.tab})")
        hub.stop()
        app.quit()

    QTimer.singleShot(args.settle_ms, switch)
    QTimer.singleShot(args.settle_ms + 12000, app.quit)   # safety
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
