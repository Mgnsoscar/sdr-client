"""In selectable (tune-step) mode, build_args() must emit only TICKED params — so a
power-mode toggle can't silently carry across params the operator left unchecked."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_form import ParamForm

_app = QApplication.instance() or QApplication([])

SPECS = [
    {"dest": "freq", "flags": ["-Frequency", "--freq"], "type": "float",
     "unit": "Hz", "default": 1575420000.0, "live": True},
    {"dest": "amplitude", "flags": ["-Amp", "--amp"], "type": "float",
     "default": 0.8, "live": True},
]


def test_build_args_selectable_only_ticked():
    f = ParamForm()
    f.set_params(SPECS, selectable=True)
    # Nothing ticked → no args, even though both have default values.
    assert f.build_args() == []
    # Tick only freq.
    f._checks["freq"].setChecked(True)
    args = f.build_args()
    assert "-Frequency" in args and "-Amp" not in args


def test_build_args_non_selectable_emits_all():
    f = ParamForm()
    f.set_params(SPECS, selectable=False)
    args = f.build_args()
    assert "-Frequency" in args and "-Amp" in args
