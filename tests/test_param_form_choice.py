"""Labelled choices: a ``{label: value}`` choice shows the human label in the dropdown
but sends the value to the script — the CLI arg carries the value token, live-tune JSON
carries the value in its real type, and a stored value round-trips back to the right item
(mirrors paramkit.Script.choice's {label: value} contract)."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QComboBox

from ui.param_form import ParamForm

_app = QApplication.instance() or QApplication([])


def _freq_spec(default=None):
    # As the agent schema serialises a choice("--freq", options={label: value}, unit="Hz").
    return {
        "dest": "freq", "flags": ["--freq"], "type": "str", "kind": "choice", "unit": "Hz",
        "choices": ["1575420000.0", "1227600000.0"],
        "choice_labels": {"1575420000.0": "1575.42 MHz", "1227600000.0": "1227.6 MHz"},
        "choice_values": {"1575420000.0": 1.57542e9, "1227600000.0": 1.2276e9},
        "default": default,
    }


def test_dropdown_shows_labels_not_values():
    f = ParamForm()
    f.set_params([_freq_spec(default=1.57542e9)])
    w, _s = f._widgets["freq"]
    assert isinstance(w, QComboBox)
    assert [w.itemText(i) for i in range(w.count())] == ["1575.42 MHz", "1227.6 MHz"]
    assert w.currentText() == "1575.42 MHz"           # default prefilled by its value


def test_build_args_sends_the_value_token_not_the_label():
    f = ParamForm()
    f.set_params([_freq_spec()])
    w, _s = f._widgets["freq"]
    w.setCurrentIndex(w.findText("1227.6 MHz"))
    assert f.build_args() == ["--freq", "1227600000.0"]   # the value, not "1227.6 MHz"


def test_live_values_carry_the_typed_value():
    f = ParamForm()
    f.set_params([_freq_spec()])
    w, _s = f._widgets["freq"]
    w.setCurrentIndex(w.findText("1575.42 MHz"))
    vals = f.values()
    assert vals["freq"] == 1.57542e9
    assert isinstance(vals["freq"], float)


def test_stored_value_round_trips_to_the_labelled_item():
    f = ParamForm()
    f.set_params([_freq_spec()])
    f.set_values(["--freq", "1227600000.0"])              # a saved task's CLI value token
    w, _s = f._widgets["freq"]
    assert w.currentText() == "1227.6 MHz"               # shows the label again
    assert f.build_args() == ["--freq", "1227600000.0"]  # and re-emits the value


def test_plain_sequence_choice_unchanged():
    f = ParamForm()
    f.set_params([{"dest": "band", "flags": ["--band"], "type": "str", "kind": "choice",
                   "choices": ["L1", "L2"], "default": "L2"}])
    w, _s = f._widgets["band"]
    assert [w.itemText(i) for i in range(w.count())] == ["L1", "L2"]
    assert w.currentText() == "L2"
    assert f.build_args() == ["--band", "L2"]
