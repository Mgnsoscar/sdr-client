"""paths.py — the per-user data dir + resource location + first-run seeding.

Locks in the contract the packaging relies on: a frozen build writes to a
per-user data dir, a source checkout keeps writing to the repo root (so the rest
of the suite is unaffected), and an explicit env override always wins.
"""
import sys
from pathlib import Path

import pytest

import paths

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── data_dir resolution ──────────────────────────────────────────────────────

def test_source_mode_data_dir_is_the_repo_root(monkeypatch):
    """Running from a checkout (not frozen, no override) writes next to the
    source exactly as the stores did before paths.py existed."""
    monkeypatch.delenv("SDR_CLIENT_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert paths.data_dir() == REPO_ROOT
    # The default the stores actually use lands at the historical location.
    assert paths.data_file("plans.json") == REPO_ROOT / "plans.json"


def test_env_override_wins_and_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("SDR_CLIENT_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)   # override beats frozen
    assert paths.data_dir() == tmp_path / "state"


def test_env_override_expands_user(monkeypatch):
    monkeypatch.setenv("SDR_CLIENT_DATA_DIR", "~/sdr-data")
    assert paths.data_dir() == Path.home() / "sdr-data"


def test_frozen_uses_the_per_user_base(monkeypatch):
    monkeypatch.delenv("SDR_CLIENT_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paths.data_dir() == paths._per_user_base()
    # And the per-user base is named for the app, off the repo tree.
    assert paths._per_user_base().name == paths.APP_NAME
    assert REPO_ROOT not in paths._per_user_base().parents


def test_per_user_base_per_platform(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(sys, "platform", "win32")
    assert paths._per_user_base() == tmp_path / "Roaming" / paths.APP_NAME

    monkeypatch.setattr(sys, "platform", "darwin")
    assert paths._per_user_base() == Path.home() / "Library" / "Application Support" / paths.APP_NAME

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert paths._per_user_base() == tmp_path / "cfg" / paths.APP_NAME


# ── ensure / data_file create the directory ─────────────────────────────────

def test_data_file_creates_the_dir(monkeypatch, tmp_path):
    target = tmp_path / "does" / "not" / "exist"
    monkeypatch.setenv("SDR_CLIENT_DATA_DIR", str(target))
    p = paths.data_file("units.yaml")
    assert p == target / "units.yaml"
    assert target.is_dir()          # a store's tmp.replace(path) can now succeed


# ── resource_dir ─────────────────────────────────────────────────────────────

def test_resource_dir_source_is_repo_root(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert paths.resource_dir() == REPO_ROOT


def test_resource_dir_frozen_uses_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    assert paths.resource_dir() == tmp_path / "bundle"


# ── seed_defaults ────────────────────────────────────────────────────────────

def test_seed_copies_starter_when_missing(monkeypatch, tmp_path):
    src, dst = tmp_path / "res", tmp_path / "data"
    src.mkdir()
    (src / "units.yaml").write_text("units: []\n", encoding="utf-8")
    monkeypatch.setenv("SDR_CLIENT_DATA_DIR", str(dst))
    monkeypatch.setattr(paths, "resource_dir", lambda: src)

    paths.seed_defaults()
    assert (dst / "units.yaml").read_text(encoding="utf-8") == "units: []\n"


def test_seed_never_overwrites_existing(monkeypatch, tmp_path):
    src, dst = tmp_path / "res", tmp_path / "data"
    src.mkdir(); dst.mkdir()
    (src / "units.yaml").write_text("shipped\n", encoding="utf-8")
    (dst / "units.yaml").write_text("mine\n", encoding="utf-8")
    monkeypatch.setenv("SDR_CLIENT_DATA_DIR", str(dst))
    monkeypatch.setattr(paths, "resource_dir", lambda: src)

    paths.seed_defaults()
    assert (dst / "units.yaml").read_text(encoding="utf-8") == "mine\n"


def test_seed_is_a_noop_when_src_equals_dst(monkeypatch, tmp_path):
    """Source mode: the resource dir IS the data dir, so seeding must not try to
    copy units.yaml onto itself."""
    monkeypatch.setenv("SDR_CLIENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "resource_dir", lambda: tmp_path)
    (tmp_path / "units.yaml").write_text("x\n", encoding="utf-8")
    paths.seed_defaults()          # must not raise (SameFileError) or clobber
    assert (tmp_path / "units.yaml").read_text(encoding="utf-8") == "x\n"
