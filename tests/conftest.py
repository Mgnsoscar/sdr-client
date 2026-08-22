"""Shared test fixtures. Redirect the process-wide calibration cache to a tmp file
so no test writes calibration_cache.json into the repo working tree."""
import pytest


@pytest.fixture(autouse=True)
def cal_cache(tmp_path, monkeypatch):
    try:
        import state.calibration_cache as cc
    except Exception:               # state pkg unavailable in a minimal env
        yield None
        return
    inst = cc.CalibrationCache(tmp_path / "calibration_cache.json")
    monkeypatch.setattr(cc, "_CACHE", inst)
    yield inst
