"""The offline LibraryClient's get_script_params surfaces calibration_freq_param (the
freq field the --power range folds at), the same as a live unit's /scripts/{name}/params.
Without it, a plan authored against the library couldn't fold the ramp/step --power range
at the operating frequency."""
import tempfile
from pathlib import Path

import api.models as m
from state.library_client import LibraryClient
from state.library_store import LibraryStore

_SRC = '''
from paramkit import Script
CAL_SIGNAL_ID = "mock"
CAL_FREQ_PARAM = "freq"
SPEC = (Script("demo")
        .number("-Center", "--freq", unit="MHz", min=70.0, max=6000.0, default=1500.0, live=True)
        .number("-Power", "--power", unit="dBm", min=-140.0, max=60.0, default=-20.0, live=True))
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
        assert [p["dest"] for p in out["params"]] == ["freq", "power"]
