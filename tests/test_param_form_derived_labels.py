"""A derived readout may carry a per-value descriptive annotation: the derived field's
``formula`` gains an optional ``labels`` list ``[source_field, l0, l1, …]`` (a nearest-int
lookup on the source, the shape of a ``table`` formula but of strings), which the form appends
to the numeric readout — e.g. the BOC passband-bandwidth field reads ``14.32 MHz  (full TMBOC)``
as --sidelobes changes. The numeric compute is unaffected (labels are ignored by the fold), and
the label strings are NOT treated as formula source fields."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_form import ParamForm

_app = QApplication.instance() or QApplication([])

# L1C-shaped: --sidelobes 0..28 drives a derived passband bandwidth (±(n+2)·1.023 MHz → a
# 2.046·n + 4.092 MHz occupied width) annotated per count (last label covers 6..28).
_L1C_LABELS = ["sidelobes", "BOC(1,1) core", "core + 1 sidelobe", "core + 2 sidelobes",
               "core + 3 sidelobes", "incl. BOC(6,1) lobes", "full TMBOC", "full signal"]


def _specs(labels=_L1C_LABELS):
    formula = {"linear": ["sidelobes", 2.046, 4.092]}
    if labels is not None:
        formula["labels"] = labels
    return [
        {"dest": "sidelobes", "flags": ["--sidelobes"], "type": "int", "kind": "integer",
         "min": 0, "max": 28, "default": 5},
        {"dest": "passband_bw_mhz", "flags": ["-Passband-bandwidth"], "kind": "derived",
         "unit": "MHz", "formula": formula},
    ]


def _form(labels=_L1C_LABELS):
    f = ParamForm()
    f.set_params(_specs(labels))
    return f


def _readout(f):
    return f._derived["passband_bw_mhz"]["value_lbl"].text()


def test_readout_appends_the_label_for_the_current_count():
    f = _form()
    f.set_values(["--sidelobes", "5"])
    _app.processEvents()
    txt = _readout(f)
    assert "14.322 MHz" in txt          # numeric value still shown (2.046·5 + 4.092)
    assert "full TMBOC" in txt          # + the per-count annotation the user asked for


def test_label_tracks_the_count():
    f = _form()
    for n, want in [(0, "BOC(1,1) core"), (1, "core + 1 sidelobe"),
                    (5, "full TMBOC")]:
        f.set_values(["--sidelobes", str(n)])
        _app.processEvents()
        assert want in _readout(f), (n, _readout(f))


def test_last_label_covers_counts_past_the_table():
    # 6..28 all read the final label (all signal power captured past the TMBOC lobes).
    f = _form()
    for n in (6, 12, 28):
        f.set_values(["--sidelobes", str(n)])
        _app.processEvents()
        assert "full signal" in _readout(f), (n, _readout(f))


def test_numeric_value_unchanged_without_labels():
    # Same field, no labels → the readout is the bare numeric value (backward compatible).
    f = _form(labels=None)
    f.set_values(["--sidelobes", "5"])
    _app.processEvents()
    txt = _readout(f)
    assert txt.strip() == "14.322 MHz"
    assert "(" not in txt


def test_label_strings_are_not_treated_as_formula_sources():
    # The label words ("full TMBOC", …) must not be picked up as fields to re-fold on.
    f = _form()
    srcs = f._formula_sources(f._derived["passband_bw_mhz"]["spec"])
    assert srcs == ["sidelobes"]        # only the real source, none of the label strings
