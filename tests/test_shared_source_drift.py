"""Drift guard for logic that is duplicated between the agent and this client.

ramp.py (ramp expansion) and argspec.py (static script-parameter extraction) MUST be
byte-identical in both repos: the client previews/validates exactly what the agent
executes/serves. This test fails if they diverge — the exact drift that let the client
fall behind the agent's argspec once already.

It compares against a sibling sdr-agent checkout when one is present (a dev tree or a
CI job that checks out both repos); it skips when the agent repo isn't on disk, so a
client-only checkout still runs green.
"""
from pathlib import Path

import pytest

# (this-repo path, agent-repo path) for each shared file, relative to each repo root.
SHARED = [("api/ramp.py", "agent/ramp.py"),
          ("api/argspec.py", "agent/argspec.py")]

_CLIENT_ROOT = Path(__file__).resolve().parents[1]


def _agent_root() -> Path | None:
    for cand in (_CLIENT_ROOT.parent / "sdr-agent",          # siblings (dev/CI)
                 _CLIENT_ROOT.parent.parent / "sdr-agent"):
        if (cand / "agent").is_dir():
            return cand
    return None


@pytest.mark.parametrize("client_rel,agent_rel", SHARED)
def test_shared_file_matches_agent(client_rel, agent_rel):
    agent_root = _agent_root()
    if agent_root is None:
        pytest.skip("sibling sdr-agent checkout not found — drift check is a no-op here")
    ours = (_CLIENT_ROOT / client_rel).read_text(encoding="utf-8")
    theirs = (agent_root / agent_rel).read_text(encoding="utf-8")
    assert ours == theirs, (
        f"{client_rel} has drifted from {agent_rel} — these must stay byte-identical. "
        f"Sync the change into both repos.")
