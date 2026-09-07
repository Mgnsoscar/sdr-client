# Design: the Hold step — an operator-gated pause in a sequence

Status: **proposal / design.** Not yet implemented. This document is the plan we agree on
*before* building. Cross-repo (agent runtime + client UI); no `sdr-scripts` change.

---

## 1. Problem

An operator wants to run a **loss-of-lock / reacquisition** test against a live GNSS receiver:

1. Ramp a GPS C/A signal **up** from a very low power, holding each step ~30 s, to find the
   power at which an already-tracking receiver **loses lock**.
2. At the top of the ramp, **restart the receiver** while the signal stays at its peak level.
3. After the receiver has restarted, ramp the signal **back down** and find the power at which it
   **reacquires**.

The blocker: restarting the receiver takes an **unknown 2–10 minutes**. Today the gap between the
up-ramp and the down-ramp is a *fixed dwell* that must be chosen **before arming** — you have to
guess a wait time you cannot know in advance. Guess short and the down-ramp starts before the
receiver is back; guess long and you waste a limited test window.

The operator wants to *pause the sequence indefinitely at the top, keep the signal exactly where it
is, and resume on their own command* once the receiver is back.

## 2. Scope and non-goals (agreed)

**In scope (v1):**
- A **Hold** step: a sequence pauses when it reaches the Hold, holding system state exactly, until
  the operator chooses to proceed.
- **Single unit only.** One sequence, one box.
- **Operator-present execution only** — running a sequence/plan directly from the **Library** tab.
- Enhancements that make the test workflow real: **Proceed** control, **edit-while-holding**, and
  **Fast-Forward-to-Hold**.

**Explicit non-goals (v1):**
- **No effect in the schedule.** A Hold reaching the unattended scheduler is a footgun (a scheduled
  run would hang forever with RF on). When a plan/sequence containing a Hold is put on the schedule,
  the Hold is **compiled out** — it becomes a 0-second pass-through and the run executes straight
  through, never pausing. The agent's scheduled path is unchanged and can never enter the holding
  state. (§7.)
- **No multi-unit / cross-unit hold synchronization.** Possibly later; see §10. v1 is single-unit.
- **No generic condition engine.** We design the *data shape* so "wait for trigger / condition /
  event" can arrive later without a schema change, but v1 ships exactly one resume trigger:
  **operator proceeds** (§10).

## 3. How sequence execution works today (grounding)

The design reuses machinery that already exists, so it's worth stating precisely.

A **Sequence** (`agent/models.py::Sequence`) is a relative-timed choreography around **one on-air
window**, defined by **two anchors**:

- `start` (T0 = on-air / RF live) — warm-up steps use negative `offset_s`, the RF-on step is at 0.
- `stop` (on-air end) — the RF-off step is at 0, cool-down steps use positive offsets.

Steps (`SequenceStep`, `agent/models.py:294`) carry `anchor ∈ {start, stop, both}`, `offset_s`, and
an `action ∈ {START, STOP, RUN, TUNE, RAMP}` (`StepAction`, `agent/models.py:257`). A **RAMP** step
(`anchor="both"` fills the window) expands at arm time into many `TUNE` fires
(`sequence_runner._resolve_ramp`).

**The agent runs the sequence** (`agent/sequence_runner.py`). At **arm**
(`SequenceRunner.arm`, `sequence_runner.py:460`):
- `on_air_at` (T0) and `on_air_end` are supplied as absolute UTC.
- `_resolve_steps` (`:353`) computes an absolute `fire_at` for every step: start-anchored from
  `on_air_at`, stop-anchored from `on_air_end`.
- A tick loop (`_run_loop`/`_tick`, `:660`/`:670`) fires any step whose `fire_at <= now`, in time
  order, once (`fired_actual`).

Four existing capabilities we will lean on directly:

| Existing capability | Where | How the Hold uses it |
|---|---|---|
| **Open-ended run** — fires start-anchored steps and stays on-air until aborted, no `on_air_end` | `arm(..., open_ended)`, `SequenceRun.open_ended` | The holding portion of a run is effectively open-ended: no scheduled off-air while paused. |
| **Runtime window patch** — move `on_air_end`, rebuild stop-anchored steps from the new end, keep already-fired steps | `patch_on_air_end`, `sequence_runner.py:576` | Resolving the *post-hold* window at proceed is the same operation with a different base anchor. |
| **Resume offset** — inject an offset into resumable steps so a ramp begins partway through | `ArmSequenceRequest.resume_offset_s`, `build_resume_request` (`process_manager.py:648`) | Related plumbing; not required for v1 but the pattern (resume a run from a chosen point) is the same. |
| **Per-run step override / plan-local step list** — arm a run with a step list that replaces the stored sequence for this run only | `ArmSequenceRequest.steps` / `step_overrides` (`arm`, `:471`) | **Edit-while-holding** sends the edited post-hold steps to `proceed` exactly the way a plan sends per-run steps. |

Client side, the timeline is authored in `ui/timeline_model.py` (`BarItem`/`RunItem`, `:44`/`:67`)
and compiled to the flat `SequenceStep` list by `items_to_steps` (`:271`); `TimelineEditor.steps()`
(`ui/timeline_editor.py:2018`) is the deploy entry point. Arming from the Library goes through
`ui/sequences_panel.py::_arm_at` (`:98`) using `ui/arm_dialog.py::ArmDialog` (the ASAP / next
half-minute / next minute / nudge picker). Scheduled/plan arming goes through a *different* path
(`ui/timeline_tab.py::_arm_scheduled`, `ui/plans_tab.py::_arm_plan`) — which is what lets us make
the Hold behave differently in the schedule without touching the interactive path.

## 4. Core concept: the Hold is a **third anchor**

Today a sequence has two anchors (`start`, `stop`). **A Hold introduces a third anchor, `hold`,
that sits between them** and splits the sequence into two windows:

```
   warm-up        ON-AIR (T0)        up-ramp            HOLD            down-ramp        OFF-AIR
     │   start-anchored steps          │        (operator-gated)          │     stop-anchored steps
     ●───────────────●─────────────────●══════════ pause ══════════●───────────────────●
   (fixed at arm; absolute)          (reached      (resolved at PROCEED; absolute
                                      when up-ramp   times only known once the operator
                                      finishes)      proceeds)
```

- **Window A (pre-hold):** `start`-anchored steps + the up-ramp. Absolute times fixed at arm, exactly
  as today.
- **The hold anchor `T_hold`:** the instant the pre-hold content finishes. Its *offset* from T0 is
  known at arm (it's where the up-ramp ends); its *absolute wall-clock time is meaningless as an end*
  because the run pauses there indefinitely.
- **Window B (post-hold):** `hold`-anchored steps + the down-ramp + the `stop`/off-air steps. Their
  absolute times are **unknown at arm** and are resolved **at proceed**, anchored to the resume
  instant `T_resume`.

**Why state is preserved "for free":** a transmit task **holds its last commanded value** between
live changes (the live-control loop; and for density, the client-side `hold_control_quantity`
precompute). While the run is paused at the Hold, *no further TUNE/RUN steps fire*, so the signal
simply stays at the up-ramp's final value. We do **not** need a new "freeze" mechanism — holding is
the *absence* of further steps plus the fact that nothing tells the task to change. RF stays on.

This is the whole trick: **a Hold is "stop scheduling window B until the operator says go, then
resolve window B relative to that instant."** It composes out of `open_ended` (no scheduled stop
while paused) + `patch_on_air_end`/`_resolve_steps` (resolve window B from a new base) + a new
run state.

## 5. Runtime design (agent)

### 5.1 Data model

`agent/models.py`:

- **`StepAction.HOLD = "hold"`** — a marker step. `anchor="start"`, `offset_s` = its position after
  the up-ramp (its *offset* is the natural end of window A). It carries no task work; it is a
  boundary. `args`/`params` empty. Validation (`_validate_steps`, `sequence_runner.py:189`): at most
  **one** HOLD per sequence (v1); it must sit after all `start`-anchored work and before any
  `stop`-anchored step in offset order.
- **`SequenceState.HOLDING = "holding"`** — a new run state between `RUNNING` and the eventual
  `RUNNING`-again/`COMPLETED`. Reached when all window-A steps have fired and the run is parked at
  the hold; left when the operator proceeds (or aborts).
- **`SequenceStep`**: a step gains an optional **`window: "A" | "B"`** classification (derived at
  compile from its position relative to the HOLD; stored so the runner needn't re-derive). Post-hold
  steps are `window="B"` and are `hold`-anchored (their `offset_s` is relative to `T_hold`).
  - *Alternative considered:* a fourth anchor value `anchor="hold"` for window-B steps instead of a
    `window` tag. Cleaner conceptually (post-hold steps literally anchor to the hold), and it mirrors
    the existing `start`/`stop` model. **Leaning toward `anchor="hold"`** (see Open Questions §12).
- **`SequenceRun`**: add
  - `hold_at_offset_s: Optional[float]` — window A's end offset (the hold's position), copied at arm.
  - `held_actual: Optional[str]` — wall-clock when HOLDING was entered.
  - `resumed_actual: Optional[str]` — wall-clock when the operator proceeded (`T_resume`).
  - `hold_aware: bool` — **the switch that makes the Hold real.** True only for interactive
    (Library) arms; False for scheduled arms, where the run resolves straight through (§7).
- **`ArmSequenceRequest`**: add `hold_aware: bool = False`. Absent/False ⇒ today's behavior exactly.

### 5.2 Arming a hold-aware run

`SequenceRunner.arm` (`sequence_runner.py:460`), when `req.hold_aware` and the sequence has a HOLD:

1. Resolve **only window A** to absolute `fire_at` (start-anchored steps + the up-ramp), from
   `on_air_at`, exactly as `_resolve_steps` does today.
2. **Do not** resolve window B yet — those steps get `fire_at = None` (deferred). The run is armed
   `open_ended`-style: there is no `on_air_end` and no scheduled off-air until proceed.
3. Store `hold_at_offset_s` and the (unresolved) window-B step definitions on the run.

The tick loop is unchanged for window A. When the last window-A step has fired **and** `now` has
reached `on_air_at + hold_at_offset_s`, a new check in `_tick` transitions the run
`RUNNING → HOLDING`, stamps `held_actual`, and emits a new `sequence_hold` event (§5.5). No steps
are due (window B isn't scheduled), so nothing else fires — the signal holds.

### 5.3 Proceed

New API `POST /sequence-runs/{id}/proceed` → `SequenceRunner.proceed(run_id, req)`, where
`req` mirrors an arm's timing (`ProceedRequest { proceed_at: str, steps?: [SequenceStep] }`):

1. Require `run.state == HOLDING` (else 409).
2. `T_resume = _parse(req.proceed_at)` — the operator's chosen resume instant (immediate / next
   half-minute / next minute / closest, chosen in the client's Proceed dialog, §6).
3. Take the window-B step set: `req.steps` if the operator **edited while holding** (§6), else the
   run's stored window-B definitions. Validate it (`_validate_steps` on the B subset).
4. Resolve window B with the **hold anchor bound to `T_resume`**: reuse `_resolve_steps`/`_resolve_ramp`
   with `hold`-anchored steps taking `base = T_resume` (the exact shape of stop-anchored resolution
   today, just a different base). The down-ramp's duration determines where the `stop`/off-air steps
   land; set `run.on_air_end = T_resume + (window-B content duration)`.
5. Append the resolved window-B `StepFire`s to `run.steps`, clear `open_ended`, stamp
   `resumed_actual`, transition `HOLDING → RUNNING`, persist, emit `sequence_proceed`.

From here the existing tick loop drives window B to completion exactly as a normal run. Extending or
shortening window B later still works via `patch_on_air_end`.

### 5.4 Fast-Forward-to-Hold

This is the highest-value enhancement for the real workflow (you estimate loss-of-lock at −10 dBm;
it actually happens at −50 dBm with 10 minutes of up-ramp still to run — that time is pure waste).

New API `POST /sequence-runs/{id}/hold-now` → `SequenceRunner.hold_now(run_id)`:

1. Require `run.state == RUNNING` and a pending Hold (window A not yet complete).
2. **Stop advancing window A:** mark all un-fired window-A steps `fired_actual = "skipped"` so the
   loop won't fire them; the in-progress up-ramp simply stops emitting further TUNE points. The task
   keeps its **current** live value (last TUNE that fired) — that is the state we freeze.
3. Transition `RUNNING → HOLDING` immediately, stamp `held_actual = now`.

The current signal settings are preserved because, again, holding = no further steps + the task
holding its last value. The operator then edits window B (e.g. retarget the down-ramp's start to the
actual −50 dBm) and proceeds (§5.3). Net effect: the test jumps to the interesting state without
waiting out a ramp whose outcome is already known.

### 5.5 Events, logging, restart safety

- New run events: `sequence_hold` (entered HOLDING), `sequence_proceed` (resumed), reusing the
  existing `_fire(run, kind, detail)` event pipe (`sequence_runner.py:940`) and run-log annotations
  ("HELD", "PROCEED @ …") like the existing `ON AIR (T0)` / `OFF AIR (T_end)` markers.
- **Fail-safe on restart is unchanged and correct:** the agent aborts any in-flight run on reboot
  (`_reconcile_on_startup`, `:910`; "we never resume across a crash automatically"). A HOLDING run is
  in-flight → it is aborted on agent restart (RF dropped, tasks stopped). This is the safe default and
  we keep it; a held run does not survive an agent crash. The client shows the run as aborted.
- **Abort while holding** (`cancel_or_abort`, `:652`) works as-is: it stops every task the sequence
  touches and halts remaining (deferred) steps. A HOLDING run must be abortable from the client at any
  time (RF off, done).

### 5.6 Safety: a bounded max-hold (deadman)

Even attended, an indefinite on-air hold deserves a guard. Add an **optional per-run `max_hold_s`**
(operator-set at arm, default e.g. 30 min, "0 = unlimited" allowed but discouraged): if a run stays
HOLDING longer than `max_hold_s`, the runner **auto-aborts** (RF off, tasks stopped) and emits
`sequence_hold_timeout`. This prevents a walk-away from leaving a live signal on-air forever. It
costs one field and one comparison in `_tick`; I recommend shipping it in v1.

## 6. Client / UI design

### 6.1 Timeline authoring (the third anchor)

`ui/timeline_model.py` + the timeline canvas gain a **Hold marker** as a third anchor between ON-AIR
and OFF-AIR. Concretely:

- A new lightweight item (or a distinguished `RunItem` with `action="hold"`) that the canvas draws as
  a labeled divider in the on-air band. `compute_anchors`/geometry (`:132`) grows to place it after
  the furthest window-A item and before the first window-B item.
- Window-B items (down-ramp, cool-down) anchor to the **hold** instead of `stop`/`start`. The
  step-editor's anchor picker (`StepEditorDialog`, `ui/timeline_editor.py:930`) gains "Hold" as an
  anchor option once a Hold exists on the timeline.
- `items_to_steps` (`:271`) emits the HOLD `SequenceStep` and tags window-B steps; `steps_to_items`
  (`:351`) round-trips it. The canvas stays clean of runtime-only fields as it does today.

### 6.2 Arming a sequence that contains a Hold

The arm dialog (`ui/arm_dialog.py`) already offers T0 timing (ASAP / next half-minute / next minute /
nudge). For a hold-bearing sequence, run directly from the Library:

- The dialog makes clear **execution will pause at the Hold and await you** — the single "total
  duration" concept no longer describes the whole run (window B's length isn't known until proceed).
  Show window A's planned length ("runs to Hold in ~9:00") and state that the down-ramp is scheduled
  when you proceed.
- The arm posts `ArmSequenceRequest(hold_aware=True, open_ended=True, …)` (interactive path only).
- Offer the `max_hold_s` safety field here (§5.6).

### 6.3 "Proceed" instead of "Run" while holding

When the client sees the run in `HOLDING`, the sequence's **Run** button becomes **Proceed**.
Clicking it opens the **same `ArmDialog`** (relabeled) so the operator picks the resume instant:
immediate / next half-minute / next minute / closest — the identical timing control they have at
start. The dialog also shows **elapsed run time**, **time held**, and (once a resume time is picked)
the **planned remaining time** (window B length). Proceed posts `POST …/proceed` with the chosen
`proceed_at`.

### 6.4 Edit-while-holding (Library/direct only)

While a run is HOLDING (and only for a directly-executed sequence — the operator owns the timeline),
**all steps after the Hold remain editable.** Because window B hasn't been scheduled on the agent,
the client can let the operator change the down-ramp's start/stop, hold times, etc., and send the
**edited window-B step list** as `ProceedRequest.steps`. This reuses the existing per-run step
mechanism (`ArmSequenceRequest.steps`, `arm(...:471)`) — the stored sequence is never mutated; only
this run's window B is replaced. This is the flexibility that makes the test method work: you discover
the receiver lost lock at −50 dBm, you retarget the down-ramp to −50 dBm, then proceed.

Window A (already executed) is immutable.

### 6.5 Achievability warnings across the hold

`achievability_warnings` (`ui/timeline_model.py:632`) does a **time-ordered** walk of commanded power
at each step's fire-time operating point. With a hold, absolute post-hold times are indeterminate, but
the walk stays valid if we treat the hold as a **clock-reset boundary**: window B is analyzed relative
to `T_hold`/`T_resume`, and the operating point *at* the hold (the up-ramp's final value + live bridge
params) seeds window B's walk. The held level itself is a "directly-set power held across a boundary" —
already a case the walk models (`_held_power_issue`). No absolute wall-clock is needed to validate that
the down-ramp's top is deliverable at its fire-time bandwidth/carrier.

## 7. Scheduled / plan behavior (the no-op requirement)

A Hold must have **no effect** when a plan/sequence is added to the **schedule** (unattended). We get
this by **compiling the Hold out** on the scheduled/plan path, so the agent is never armed
`hold_aware` and never enters HOLDING:

- The interactive Library arm (`ui/sequences_panel.py::_arm_at`) sets `hold_aware=True` and the Hold
  is real.
- The scheduled/plan arm (`ui/timeline_tab.py::_arm_scheduled`, `ui/plans_tab.py::_arm_plan`) compiles
  the timeline with the **Hold collapsed to a 0-second pass-through**: window-B steps are re-anchored
  as if the hold were a zero-length dwell right after window A (the down-ramp starts immediately after
  the up-ramp), producing an ordinary **two-anchor** `SequenceStep` list. `hold_aware` stays False.
- Because the scheduled arm never sets `hold_aware`, the agent's runner resolves all steps up front
  and runs straight through — its scheduled path is **byte-for-byte unchanged**. A HOLDING state is
  unreachable without an interactive `hold_aware` arm.

This is enforced in **one place** (the step-compile for the scheduled path) and is the single rule
that keeps an operator-gated pause out of automation. Belt-and-suspenders: the agent can also reject
`hold_aware=True` from any non-interactive arm surface if we want a hard server-side guarantee (Open
Questions §12).

## 8. New API surface

Agent (`agent/main.py` + `api/client.py` + `api/models.py`):

- `POST /sequence-runs/{id}/proceed` — body `ProceedRequest { proceed_at: str, steps?: [SequenceStep], }`.
  HOLDING → RUNNING; resolves window B from `proceed_at`.
- `POST /sequence-runs/{id}/hold-now` — Fast-Forward-to-Hold. RUNNING → HOLDING now.
- `ArmSequenceRequest` gains `hold_aware: bool` and (optional) `max_hold_s: float`.
- `SequenceRun` gains `hold_at_offset_s`, `held_actual`, `resumed_actual`, `hold_aware`
  (and surfaces `HOLDING` in `state`).
- New run-event kinds: `sequence_hold`, `sequence_proceed`, `sequence_hold_timeout`.

The existing `cancel_or_abort`, `patch_on_air_end`, and the run-log/event stream need no new shape —
they already cover a HOLDING run (abort) and window-B rescheduling (patch).

## 9. The generic "wait point" framing (design, don't build)

Model the Hold as a **pause point** with a `resume_trigger`, even though v1 implements exactly one:

```
HOLD step  ≙  { action: "hold", resume_trigger: "operator", max_hold_s?: float }
```

`resume_trigger` is `"operator"` in v1. The *runtime* already generalizes cleanly: HOLDING is "the run
is parked; some event will resume it." Later triggers — `"timer"` (a fixed dwell; also the exact shape
the scheduler wants), `"external"` (a webhook/GPIO), `"task_complete"`, `"condition"` — become new ways
to leave HOLDING **without changing the state model, the two-window resolution, or the client's
timeline**. We ship `operator` only and carry the field so the door stays open. We do **not** build a
condition engine, trigger inputs, or their UI now — that is unbounded scope for use cases we can't test
yet.

## 10. Multi-unit (future, explicitly deferred)

When it's needed, the comprehensible model is a **plan-level barrier / rendezvous**: any sequence
reaching its Hold pauses **all** units in the plan; a single Proceed releases them together, and each
sequence's window B runs relative to the shared `T_resume`. The third-anchor model composes with this
directly — the Hold anchor is exactly the barrier point, and cross-unit steps anchor to it. It
requires every unit to support HOLDING and a plan-level coordinator, which is real work for a use case
v1 doesn't have. Not in v1.

## 11. Capability gating & versioning

- Agent advertises a capability string, e.g. **`sequence-hold`**, in `AGENT_CAPABILITIES`
  (`agent/config.py`), and bumps `AGENT_VERSION` (safety gate: a hold-aware arm sent to an agent that
  lacks the capability must be refused, not silently run-through with RF stuck).
- The client feature-gates the Hold authoring + Proceed UI on `_supports("sequence-hold")`, and
  **blocks saving/arming** a hold-aware run against an agent that lacks it (the established
  `_blocks_on_*` pattern). Scheduled/plan compilation strips the Hold regardless, so an older agent
  still runs the collapsed sequence correctly.

## 12. Open questions (decide before building)

1. **`anchor="hold"` vs a `window` tag** on window-B steps (§5.1). I lean `anchor="hold"` — it mirrors
   `start`/`stop`, and `_resolve_steps` already switches base by anchor, so window B "just works" with
   a third base. Confirm.
2. **One Hold per sequence** in v1 (recommended) vs. many. One keeps the state machine binary
   (RUNNING/HOLDING) and the UI legible. Multiple holds = a small counter, but I'd defer.
3. **Server-side guard:** should the agent *reject* `hold_aware=True` from the scheduled surface, or
   do we trust the client to compile the Hold out (§7)? A hard server guard is cheap insurance.
4. **`max_hold_s` default** (§5.6): 30 min? And is "0 = unlimited" allowed at all in attended mode?
5. **Proceed timing floor:** the up-ramp's last value is live; is there any settle/lead time needed
   before window B's first TUNE, or is "immediate" truly immediate? (Reuse the arm safety-lead.)

## 13. Phased implementation plan

- **Phase 0** — data model + no behavior: `StepAction.HOLD`, `SequenceState.HOLDING`, the `SequenceRun`
  fields, `hold_aware` on arm; validation. Round-trip through `items_to_steps`/`steps_to_items`. Agent
  capability + version bump. Green tests, zero behavior change when no Hold is present.
- **Phase 1** — runtime hold + proceed: arm resolves window A only, `_tick` enters HOLDING, `proceed`
  resolves window B from `T_resume`, events + logging, restart-abort + max-hold deadman. Agent tests
  (`test_sequence_*`): a hold-aware run parks at the hold; proceed schedules window B relative to the
  resume time; abort/timeout while holding drop RF.
- **Phase 2** — client authoring + Proceed: third-anchor timeline, arm-dialog messaging, Proceed
  button + reused ArmDialog with elapsed/held/remaining, run-state plumbing. Scheduled path collapses
  the Hold (the no-op guarantee) — test it explicitly.
- **Phase 3** — edit-while-holding + Fast-Forward-to-Hold: `hold-now` API + button; window-B edits sent
  via `ProceedRequest.steps`; achievability walk across the hold boundary.

Each phase is independently shippable and leaves `main` green. Phase 1 already solves the operator's
stated problem (pause at the top, restart the receiver, proceed when ready); Phases 2–3 are the UX and
the time-savings.

## 14. Test strategy

- **Agent** (`sdr-agent/tests/`): hold-aware arm defers window B; the loop enters HOLDING at the hold
  offset; `proceed` resolves window-B fire times from the supplied `proceed_at` and computes
  `on_air_end` from the down-ramp; `hold-now` skips remaining window-A steps and holds the last value;
  abort and `max_hold_s` timeout drop RF; a NON-hold-aware arm of the same sequence runs straight
  through (the scheduled no-op); restart aborts a HOLDING run.
- **Client** (`sdr-client/tests/`): `items_to_steps`/`steps_to_items` round-trip a Hold; window-B
  steps anchor to the hold; the scheduled compile collapses the Hold to a contiguous two-anchor list;
  the Proceed dialog reuses ArmDialog and posts the chosen instant; edit-while-holding sends edited
  window-B steps; `achievability_warnings` validates window B at its fire-time operating point across
  the hold.

---

*Author's note:* the reason this is tractable is that the Hold is not a new scheduler — it's the
**absence** of scheduling for window B until the operator resolves it, layered on the runner's existing
`open_ended` + window-patch + per-run-step-override machinery. The scariest-sounding parts (multi-unit
sync, generic conditions) are deliberately out of v1; the parts that save real test time
(fast-forward, edit-while-holding) fall out of the two-window model almost for free.
