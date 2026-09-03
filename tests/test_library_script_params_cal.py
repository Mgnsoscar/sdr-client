"""The offline LibraryClient's get_script_params surfaces the SAME calibration metadata a live
unit's /scripts/{name}/params does — calibration_signal, calibration_freq_param AND
calibration_power_laws. Without them, a plan/sequence authored against the library (the plan
editor fetches params from the library, not the unit) couldn't fold the ramp/step --power range
at the operating frequency, nor offer the multi-quantity --power card (companions / 'Control in
this →')."""
import tempfile
from pathlib import Path

import api.models as m
from state.library_client import LibraryClient
from state.library_store import LibraryStore

_SRC = '''
from paramkit import Script
CAL_SIGNAL_ID = "mock"
CAL_FREQ_PARAM = "freq"
CAL_POWER_LAWS = [
    {"id": "psd_live", "name": "Spectral density", "unit": "dBm/MHz",
     "in": "density", "out": "density", "restates_measurement": True,
     "param": "bw", "coeff": -10.0, "ref": 10.0, "rep": 10.0},
    {"id": "fbw_power", "name": "Full-bandwidth (total) power", "unit": "dBm",
     "in": "density", "out": "abs", "k": 10.0, "rep": 10.0},
]
SPEC = (Script("demo")
        .number("-Center", "--freq", unit="MHz", min=70.0, max=6000.0, default=1500.0, live=True)
        .number("-Power", "--power", unit="dBm", min=-140.0, max=60.0, default=-20.0, live=True)
        .number("-Sweep-BW", "--bw", unit="MHz", min=0.1, max=55.0, default=10.0, live=True))
'''


def _client(tmp_path):
    store = LibraryStore(tmp_path / "lib.json")
    store.upsert_script(m.LibraryScript(name="demo.py", content=_SRC, params=[]))
    return LibraryClient(store)


def test_get_script_params_includes_calibration_freq_param():
    with tempfile.TemporaryDirectory() as d:
        out = _client(Path(d)).get_script_params("demo.py")
        assert out["calibration_freq_param"] == "freq"
        assert out["calibration_signal"] == "mock"
        assert [p["dest"] for p in out["params"]] == ["freq", "power", "bw"]


def test_get_script_params_includes_calibration_power_laws():
    # Regression: the library client dropped calibration_power_laws, so a plan/sequence step editor
    # (which fetches params from the LIBRARY, not the unit) never got the laws and silently omitted
    # the --power card's companions — while the Run/live-tune forms (talking to the unit) had them.
    with tempfile.TemporaryDirectory() as d:
        out = _client(Path(d)).get_script_params("demo.py")
        laws = out.get("calibration_power_laws")
        assert laws is not None
        assert [law["id"] for law in laws] == ["psd_live", "fbw_power"]
        assert any(law["name"] == "Full-bandwidth (total) power" for law in laws)
