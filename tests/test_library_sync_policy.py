"""Library drift fingerprints include the RF-fault recovery policy + the standalone
auto-restart flag (review fix #21).

`state/library_sync.diff_library` decides whether a unit's deployed sequence/task is "in
sync" with the canonical library via a fingerprint of the deploy-relevant fields. It used
to ignore `Sequence.recovery_policy`/`recovery_mode` (Phase 3) and
`TaskConfig.auto_restart_on_fault`/`max_fault_restarts` (Phase 3b), so a POLICY-ONLY change
in the library — operator-restart → auto-resync, or ticking "Auto-restart on fault" — never
showed as drift and never reconciled; and the reverse (auto → manual) left autonomy ON on the
unit. The fingerprints now carry both, normalised to the model defaults so an OLD unit that
never sends the fields (they default in the client model) fingerprints identically to an
explicit default — adding the fields must not make every unchanged deployment read drifted.
"""
from api import models as m
from state.library_sync import (
    UnitSnapshot, _seq_fingerprint, _task_fingerprint, diff_library, diff_state,
    parse_tasks_yaml,
)

_STEP = m.SequenceStep(anchor="start", offset_s=0.0, action="start", task_name="cw")


def _seq(**kw) -> m.Sequence:
    base = dict(id="seq-1", name="CW ramp", description="d", steps=[_STEP])
    base.update(kw)
    return m.Sequence(**base)


def _task(**kw) -> m.TaskConfig:
    base = dict(name="cw", description="tone", command=["python3", "cw_tx.py", "--rf", "on"],
                env={"A": "1"}, autostart=False, restart_on_crash=True)
    base.update(kw)
    return m.TaskConfig(**base)


def _lib(seqs=(), tasks=()) -> m.Library:
    return m.Library(sequences=list(seqs), tasks=list(tasks))


# ── (a)/(b) a policy-only sequence change is drift ─────────────────────────────

def test_sequence_differing_only_in_recovery_policy_is_drifted():
    canon = _lib(seqs=[_seq(recovery_policy="auto")])
    unit = _lib(seqs=[_seq(recovery_policy="manual")])
    d = diff_library(canon, unit)
    assert d.sequences_change == ["seq-1"]
    assert d.sequences_add == [] and d.sequences_remove == []
    assert not d.library_in_sync
    assert "sequences ~1" in d.summary()


def test_sequence_auto_on_unit_but_manual_in_library_is_drifted():
    # The reverse direction: the operator turned autonomy OFF in the library; the unit still
    # holds "auto" — must reconcile, else the unit keeps auto-restarting unattended.
    canon = _lib(seqs=[_seq(recovery_policy="manual")])
    unit = _lib(seqs=[_seq(recovery_policy="auto")])
    assert diff_library(canon, unit).sequences_change == ["seq-1"]


def test_sequence_differing_only_in_recovery_mode_is_drifted():
    canon = _lib(seqs=[_seq(recovery_policy="auto", recovery_mode="replay")])
    unit = _lib(seqs=[_seq(recovery_policy="auto", recovery_mode="resync")])
    d = diff_library(canon, unit)
    assert d.sequences_change == ["seq-1"]
    assert not d.library_in_sync


# ── (c) a policy-only task change is drift ──────────────────────────────────────

def test_task_differing_only_in_auto_restart_on_fault_is_drifted():
    canon = _lib(tasks=[_task(auto_restart_on_fault=True)])
    unit = _lib(tasks=[_task(auto_restart_on_fault=False)])
    d = diff_library(canon, unit)
    assert d.tasks_change == ["cw"]
    assert d.tasks_add == [] and d.tasks_remove == []
    assert not d.library_in_sync
    assert "tasks ~1" in d.summary()


def test_task_auto_restart_on_unit_but_off_in_library_is_drifted():
    canon = _lib(tasks=[_task(auto_restart_on_fault=False)])
    unit = _lib(tasks=[_task(auto_restart_on_fault=True)])
    assert diff_library(canon, unit).tasks_change == ["cw"]


def test_task_differing_only_in_max_fault_restarts_is_drifted():
    canon = _lib(tasks=[_task(auto_restart_on_fault=True, max_fault_restarts=5)])
    unit = _lib(tasks=[_task(auto_restart_on_fault=True, max_fault_restarts=2)])
    assert diff_library(canon, unit).tasks_change == ["cw"]


# ── (d) the normalisation: explicit defaults == omitted fields (an old unit) ────

def test_explicit_defaults_and_omitted_fields_fingerprint_identically():
    explicit_seq = _seq(recovery_policy="manual", recovery_mode="resync")
    omitted_seq = _seq()
    assert _seq_fingerprint(explicit_seq) == _seq_fingerprint(omitted_seq)
    explicit_task = _task(auto_restart_on_fault=False, max_fault_restarts=2)
    omitted_task = _task()
    assert _task_fingerprint(explicit_task) == _task_fingerprint(omitted_task)
    d = diff_library(_lib([explicit_seq], [explicit_task]), _lib([omitted_seq], [omitted_task]))
    assert d.library_in_sync and d.in_sync
    assert d.summary() == "in sync"


def test_old_unit_library_payload_without_the_fields_is_in_sync():
    # snapshot_unit builds m.Library(**GET /library). A pre-Phase-3/3b agent's payload has
    # neither the sequence policy nor the task flag; the client model defaults them, and the
    # fingerprint must read that exactly like the canonical library's explicit defaults.
    payload = {
        "scripts": [],
        "tasks": [{"name": "cw", "description": "tone",
                   "command": ["python3", "cw_tx.py", "--rf", "on"], "env": {"A": "1"},
                   "autostart": False, "restart_on_crash": True}],
        "sequences": [{"id": "seq-1", "name": "CW ramp", "description": "d",
                       "steps": [_STEP.model_dump(mode="json")]}],
    }
    unit = m.Library(**payload)
    assert "recovery_policy" not in payload["sequences"][0]
    assert "auto_restart_on_fault" not in payload["tasks"][0]
    canon = _lib(seqs=[_seq(recovery_policy="manual", recovery_mode="resync")],
                 tasks=[_task(auto_restart_on_fault=False, max_fault_restarts=2)])
    d = diff_library(canon, unit)
    assert d.sequences_change == [] and d.tasks_change == []
    assert d.library_in_sync
    # …while a real policy change against that same old-style payload still surfaces.
    canon_auto = _lib(seqs=[_seq(recovery_policy="auto")], tasks=[_task(auto_restart_on_fault=True)])
    d2 = diff_library(canon_auto, unit)
    assert d2.sequences_change == ["seq-1"] and d2.tasks_change == ["cw"]


def test_old_unit_tasks_yaml_without_the_flag_is_in_sync():
    # pull_library parses the unit's tasks.yaml; an older agent's _spec_to_entry never wrote
    # auto_restart_on_fault / max_fault_restarts. Must fingerprint as the default.
    yaml_text = (
        "tasks:\n"
        "  - name: 'cw'\n"
        "    description: 'tone'\n"
        "    command: ['python3', 'cw_tx.py', '--rf', 'on']\n"
        "    env: {A: '1'}\n"
        "    autostart: false\n"
        "    restart_on_crash: true\n"
    )
    unit_tasks = parse_tasks_yaml(yaml_text)
    assert len(unit_tasks) == 1
    canon = _lib(tasks=[_task(auto_restart_on_fault=False, max_fault_restarts=2)])
    assert diff_library(canon, _lib(tasks=unit_tasks)).library_in_sync


def test_blank_recovery_fields_normalise_to_the_defaults():
    # An empty string (the PlanItem "inherit" sentinel leaking onto a Sequence, or a hand-edited
    # store) reads as the default, not as a third distinct policy.
    blank = _seq(recovery_policy="", recovery_mode="")
    assert _seq_fingerprint(blank) == _seq_fingerprint(_seq())
    assert diff_library(_lib(seqs=[blank]), _lib(seqs=[_seq()])).library_in_sync


def test_existing_fingerprint_fields_are_unchanged():
    # The new elements are appended; the leading tuple is byte-identical to the old fingerprint.
    t = _task()
    assert _task_fingerprint(t)[:5] == (
        t.description, tuple(t.command), tuple(sorted(t.env.items())),
        bool(t.autostart), bool(t.restart_on_crash))
    assert _task_fingerprint(t)[5:] == (False, 2)
    s = _seq()
    assert _seq_fingerprint(s)[:3] == (
        s.name, s.description, tuple(tuple(st.model_dump(mode="json").items()) for st in s.steps))
    assert _seq_fingerprint(s)[3:] == ("manual", "resync")


# ── the UI consumer path (library_tab → diff_state) ─────────────────────────────

def test_diff_state_reports_a_policy_only_change_as_drift():
    canon = _lib(seqs=[_seq(recovery_policy="auto", recovery_mode="resync")],
                 tasks=[_task(auto_restart_on_fault=True)])
    snap = UnitSnapshot(library=_lib(seqs=[_seq()], tasks=[_task()]))
    d = diff_state(canon, [], [], snap)
    assert not d.in_sync
    assert d.sequences_change == ["seq-1"] and d.tasks_change == ["cw"]
    assert d.seq_names["seq-1"] == "CW ramp"
    # And nothing else moves: plans/schedule untouched, no add/remove.
    assert not (d.plans_add or d.plans_change or d.plans_remove)
    assert not (d.sequences_add or d.sequences_remove or d.tasks_add or d.tasks_remove)
