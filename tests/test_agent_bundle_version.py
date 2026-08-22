"""Agent-bundle version ordering must be NUMERIC, not alphabetical: 1.0.10 is newer
than 1.0.9, and find_bundle() must pick the newest bundle when several coexist."""
import tarfile
from pathlib import Path

from state.agent_bundle import find_bundle, is_newer, parse_version


def test_parse_version_numeric_order():
    assert parse_version("1.0.10") > parse_version("1.0.9")
    assert parse_version("1.2.0") > parse_version("1.1.99")
    assert parse_version("") == (-1,)


def test_is_newer():
    assert is_newer("1.0.10", "1.0.9") is True
    assert is_newer("1.0.9", "1.0.10") is False
    assert is_newer("1.1.0", "1.1.0") is False        # equal is not newer
    assert is_newer("1.0.0", None) is True             # unknown current → any is newer
    assert is_newer(None, "1.0.0") is False


def _make_bundle(path: Path, version: str) -> None:
    import io
    with tarfile.open(path, "w:gz") as tar:
        data = version.encode()
        info = tarfile.TarInfo("VERSION")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


def test_find_bundle_picks_highest_version(tmp_path, monkeypatch):
    d = tmp_path / "bundles"
    d.mkdir()
    for v in ("1.0.9", "1.0.10", "1.0.2"):
        _make_bundle(d / f"sdr-agent-{v}.tar.gz", v)
    monkeypatch.setattr("state.agent_bundle.bundle_dir", lambda: d)
    picked = find_bundle()
    assert picked is not None and picked.name == "sdr-agent-1.0.10.tar.gz"
