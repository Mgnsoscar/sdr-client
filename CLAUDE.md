# sdr-client — Claude working notes

PyQt6 **desktop client** for controlling a fleet of SDR units (each runs `sdr-agent`) and
authoring their power **calibration**. Talks to agents over HTTP (mDNS discovery). Part of a
three-repo system: **`sdr-agent`** (on-unit HTTP agent + calibration resolver), **`sdr-client`**
(this GUI), **`sdr-scripts`** (the transmit scripts the agent runs).

## Environment setup (container starts without deps)
```bash
pip3 install numpy pytest PyQt6 httpx pydantic zeroconf websocket-client PyYAML paramiko \
             fastapi uvicorn "ruamel.yaml" starlette psutil python-multipart inotify-simple
apt-get update -q && apt-get install -y -q libegl1 libgl1 libglib2.0-0t64 libdbus-1-3 \
             libxkbcommon0 libfontconfig1        # PyQt6 offscreen needs these GL/X libs
```

## Run the tests (always green on `main`)
```bash
QT_QPA_PLATFORM=offscreen python3 -m pytest -q        # ~556 tests, headless Qt
```
`QT_QPA_PLATFORM=offscreen` is **required** — tests instantiate real Qt widgets.

## Cross-repo invariants (do not break)
- **Drift guard (enforced by `sdr-agent/tests/test_shared_source_drift.py`):**
  `api/argspec.py` and `api/ramp.py` MUST stay **byte-identical** to `sdr-agent/agent/argspec.py`
  and `sdr-agent/agent/ramp.py`. If you touch one, mirror it exactly.
- **Power-law mirror (manual convention):** `state/power_law.py` is a verbatim copy of
  `sdr-agent/paramkit/power_law.py` (pure stdlib so Python/JS parity holds). Keep them in step.
- **Capability gating:** a feature the agent must understand is gated behind a
  `CAL_*_CAPABILITY` string advertised by the agent's `GET /info`; the client blocks saving a
  document that uses it on an agent that lacks it (`_supports(cap)` + a `_blocks_on_*` guard
  wired into `_on_save`). Never send a document a feature-older agent would silently mishandle.

## Where things live
- `ui/calibration_panel.py` (~4.7k lines) — the **Calibration tab**: the RF chain builder and
  the **per-signal signal editor** (each signal's Measurement + Limiting reading). The heart of
  recent work.
- `ui/param_form.py` — the arm/task **parameter form**, including the calibrated `--power`
  field: unit "views" (control `--power` in the measured quantity or a declared conversion law),
  the range rail, and the `quantity [unit]` label.
- `state/power_fold.py` / `state/power_law.py` — client-side re-fold of the resolved artifact at
  the operator's live frequency/parameters (mirrors the agent + transmit script math).
- `api/` — the agent HTTP client + the drift-guarded `argspec.py` / `ramp.py`.
- `ui/theme.py` — `Palette` (colors) and `Fonts` (`Fonts.MONO`/`SANS`). Match the existing
  compact style: `font-size:11–12px`, teal field labels (`Palette.ACCENT`/`TEXT_MUTED`), amber
  = `Palette.ARMED`.

## Calibration model (one paragraph)
A unit's calibration is a **chain of planes** (measured SDR output → derived hops: cables,
amps, antenna). Each **signal** carries its own **measurement** (`{quantity, unit}` — dBm or a
spectral density) and a **limiting reading** (how the dBm safety ceiling is gauged: same as
measurement / a declared **law** that returns dBm / a separate dBm curve). `--power` is
controlled in the measured quantity; declared **power laws** (`CAL_POWER_LAWS` in a transmit
script, `in`/`out` families abs↔density) convert between quantities. A single dBm ceiling on
the source stage's **limits list** caps every signal (each signal's limiting reading is dBm).
The agent's resolver publishes a per-signal **artifact** the client/script re-fold at runtime.

## Current state — Hold step Phase 2 (client authoring + Proceed UI): COMPLETE (branch `claude/hold-step-phase-0-wwwxf7`, client-only)
Design doc: **`docs/sequence-hold-step.md`** (cross-repo; the authoritative spec + owner decisions).
A new **Hold** sequence step pauses a running sequence at the hold, holding system state exactly, until
the operator proceeds — for the GNSS loss-of-lock/reacquire test where the receiver-restart wait
(2–10 min) isn't known in advance. v1 scope: **single unit, Library/operator-present execution only, no
effect in the schedule**. Core model: the Hold is a **third anchor** (`anchor="hold"`) splitting the
sequence into window A (pre-hold, fixed at arm) and window B (post-hold, resolved only at **proceed**).
Phase 0 (data-model mirror + lossless round-trip) and Phase 1 (agent HOLDING runtime, agent `1.17.0`)
are done; **Phase 2 is the client authoring + arm/Proceed UI** (this note). Shipped, client-only, no
agent/scripts change, drift-guarded files untouched:
- **Third-anchor canvas** (`ui/timeline_model.py` + `ui/timeline_editor.py`): geometry helpers
  `hold_offset`/`has_hold`/`effective_anchor_offset` map a window-B (`anchor="hold"`) item to the
  hold's on-air side (placed as start-anchored at `hold_offset + its offset`); `compute_anchors`/
  `ramp_span` honor them, so the band widens to keep window B left of off-air. The canvas paints the
  Hold as a dashed **⏸ HOLD** divider spanning the band (`_paint_hold`), it owns no lane, and it wins
  hit-testing over a bar body sharing its x (`_hit` checks holds first) so it's clickable + draggable
  (`hold_body` in `DRAG_PARTS`). A **`+ Hold`** toolbar button (`add_new("hold")`, one per sequence,
  disabled once one exists via `_sync_hold_button`) opens the tiny **`HoldEditorDialog`** (offset +
  Remove only — a Hold carries no task). `set_hold_authoring(False)` hides `+ Hold` in the plan-item
  editor (a plan's Hold is compiled out — see below). `TimelineEditor.has_hold()`.
- **Step-editor Hold anchor** (`StepEditorDialog`, `ui/timeline_editor.py`): the anchor picker gains a
  **"hold (after Hold)"** option when a Hold exists on the timeline (or when editing a step already
  anchored to it); its window-fit check is skipped for a hold-anchored tune (window B is timed at
  proceed). `validate()` excludes the taskless HOLD marker from the "every step needs a task" / on-air
  checks.
- **Arm/Proceed dialog** (`ui/arm_dialog.py`): `ArmDialog` gained keyword-only knobs so the SAME picker
  serves arm and Proceed — `accept_label`/`title` (relabel to "Proceed"), `body_note` (arm messaging),
  `show_stop` (hidden for a hold-aware arm — open-ended — and for Proceed — off-air is agent-derived),
  `max_hold_default_s` (the arm-time deadman field → `max_hold_s()`, 0 = unlimited), and
  `status_provider` (a live elapsed/held/remaining line for Proceed).
- **Run→Proceed plumbing** (`ui/sequences_panel.py`): `_ACTIVE` now includes `HOLDING`; a **holding
  run's row relabels "Arm" → "Proceed"** (enabled) wired to `_on_proceed`. `_on_start` routes a
  Hold-bearing sequence to `_arm_hold_aware` — **gated on `_supports(SEQUENCE_HOLD_CAPABILITY)`** (a
  safety gate: an older agent would refuse the arm) — which arms `hold_aware=True, open_ended=True,
  max_hold_s`. `_on_proceed` opens the relabeled ArmDialog (live `_hold_status_text`) and posts
  `_proceed_run` → the new **`api/client.py::proceed_sequence_run`** (`POST …/proceed`,
  `ProceedRequest{proceed_at}`; `steps` left None — edit-while-holding is Phase 3). "holding" is an
  amber `StatusPill` (`ui/theme.py`).
- **Scheduled/plan collapse-the-Hold no-op** (design §7): `api/models.py::collapse_hold(steps)` (+
  `has_hold`) drops the HOLD marker and re-anchors every window-B step to `start` at
  `hold_offset + its offset` — a zero-length pass-through. `_arm_scheduled` (`ui/timeline_tab.py`) and
  `_arm_plan` (`ui/plans_tab.py`) send the COLLAPSED steps and **never set `hold_aware`**, so the agent
  runs its normal two-anchor path. Up-front notices: at schedule-add (`_ScheduleDialog._accept`), at
  scheduled arm (`_finish_preflight`), and at direct plan arm (`_finish_arm_preflight`).
Achievability across the hold (§6.5), edit-while-holding, and Fast-Forward-to-Hold are **Phase 3**.
Tests: `tests/test_timeline_hold_phase2.py` (geometry, canvas paint/hit/add, `+ Hold` gating, step-editor
anchor option, `validate` tolerates the marker) + `tests/test_hold_arm_proceed.py` (collapse, arm/proceed
helpers, `proceed_sequence_run`, ArmDialog variants, the capability gate, the Proceed row) on top of
Phase 0's `tests/test_timeline_hold_model.py`; suite 755 → 780 offscreen.
**Agent contract Phase 2 builds on** (agent `1.17.0`; `sdr-agent/CLAUDE.md` "Hold step Phase 1"): arm
`ArmSequenceRequest(hold_aware=True, open_ended=True, max_hold_s=…)` parks at the hold; a Hold-bearing
arm WITHOUT `hold_aware` is refused (hence the scheduled/plan collapse); `POST /sequence-runs/{id}/
proceed` resolves window B (409 if not holding); a holding run has `held_actual` set,
`on_air_end=None`/`open_ended=True` until proceed; abort with the existing DELETE; capability
`sequence-hold`.

## Current state — Hold step Phase 3a (achievability across the hold): COMPLETE (branch `claude/hold-step-phase-0-wwwxf7`, client-only)
Design §6.5. The temporal power walk now treats the Hold as a **clock-reset boundary**: a window-B
(`anchor="hold"`) event is placed as if start-anchored at `hold_offset + its offset`, so it fires
AFTER every window-A step and inherits the operating point held at the hold (the up-ramp's final
`--power` + bridge params). Ordering — not absolute wall-clock — is all the walk needs; the held level
then seeds window B automatically (a "directly-set power held across a boundary", already modelled by
`_held_power_issue`). Both walks in `ui/timeline_model.py` got the same 3-line fix (compute
`h_off = hold_offset(items)`, route tune/run/ramp placement through `effective_anchor_offset(it, h_off)`):
`achievability_warnings` (the amber banner — a window-B density command / down-ramp is validated at the
bandwidth CARRIED across the hold, and a density held from window A is re-checked by a window-B `--bw`
change) and `hold_control_quantity` (the deploy-time precompute — a window-B `--bw` change re-derives the
held base in the right order). Byte-identical for a Hold-free timeline (`h_off` is None → the existing
path). Client-only; no agent/scripts change; drift-guarded files untouched. Tests:
`tests/test_achievability_hold_boundary.py` (held density clamps on a window-B widen, in-range stays
silent, the hold orders window B after window A, a window-B down-ramp clamps at its fire-time width, the
deploy precompute injects a window-B `--bw` change, a lone marker doesn't perturb a normal walk); suite
780 → 788 offscreen. **NEXT — Phase 3b/3c** (cross-repo, need `sdr-agent`): Fast-Forward-to-Hold
(`POST /sequence-runs/{id}/hold-now` + a "Hold now" button, design §5.4) and edit-while-holding (the
agent's `proceed` honours `ProceedRequest.steps` + a client window-B edit flow, §6.4).

## Current state — Tune form renders live-sourced derived readouts: COMPLETE (branch `claude/l1c-sidelobes-slider`)
`ui/live_tune_dialog.py` `_prepare_specs` used to route EVERY non-live spec — derived fields
included — to fold context (never rendered). Now a VISIBLE (non-hidden) derived field whose formula
reads only LIVE knobs is RENDERED read-only in the tune form (kept out of `_context_dests`), so a
display readout that just tracks a live knob shows and updates while retuning — e.g. L1C's
`passband_bw_mhz` tracking the live `--sidelobes` slider. Sources are checked via
`ParamForm._formula_sources`; every source must be a live (rendered) field, else it stays fold
context. Hidden derived fields (a power law's `enbw_mhz` key) are unaffected — still context-only.
Client-only; no agent/scripts change. Tests: `tests/test_live_tune_power.py` (visible derived
readout renders + tracks the live source; hidden one stays context). The scripts side (L1C
whole-sidelobe slider + labels + max 13) lives in `sdr-scripts` (see its CLAUDE.md).

## Current state — source stage name fixed to "Source"; no empty stage names: COMPLETE (branch `claude/source-stage-name`)
Bug: renaming a chain stage to `""` deleted it — `_read_planes` skips a nameless row (`if not
name: continue`). Fix (all in `ui/calibration_panel.py`): (1) the SOURCE (first) stage has a
fixed, reserved id `SOURCE_PLANE_NAME = "Source"` — its name field is read-only
(`_doc_to_form`), and `_on_plane_name_changed` refuses a rename of row 0 (snaps back). (2)
`_normalize_source_plane(doc)` renames a legacy/other source id to "Source" consistently (via
`_rename_plane_in_doc` — curves, limits, operating_plane, downstream `from`/`of`) when a doc is
loaded (`_set_doc`, `_apply_json_text`); the `_template()` seeds "Source" directly. (3) Any
stage cleared to `""` or renamed to a duplicate (incl. the reserved "Source") reverts to its
last valid name in `_on_plane_name_changed`; `_read_planes` also falls back to the row's `orig`
name defensively so a momentarily-blank field never drops the stage. Tests:
`test_calibration_panel.py` (name locked/read-only, source can't be renamed, legacy source
migrated on load, clearing a name reverts instead of deleting, a stage can't take the reserved
name). Client-only; no agent/scripts/capability change.

## Current state — derived readout per-value labels: COMPLETE (branch `claude/gps-calibration`)
A derived (computed, read-only) field's readout can now carry a per-value descriptive
annotation. `ui/param_form.py` `_recompute_derived` appends `  (label)` to the numeric value
via a new `_derived_label(spec)`: the field's `formula` may carry an optional `labels` list
`[source_field, l0, l1, …]` — a nearest-int lookup on the source (the shape of a `table`
formula, but of strings) whose last entry covers any higher count. So the BOC scripts' passband
bandwidth reads e.g. `14.32 MHz  (full TMBOC)` at L1C `--sidelobes` 5, `30.69 MHz  (both main
lobes)` at M-code 0. The label rides through the `formula` dict verbatim on both param paths
(paramkit `--describe-params` and the drift-guarded static `argspec`), so it is scripts + client
only — no argspec/paramkit change, no agent version bump; the numeric fold ignores it (the compute
op stays first in the dict), and `_formula_sources` skips the `labels` key so its strings are not
mistaken for source fields. Backward compatible (no `labels` → the bare numeric readout). Scripts:
`sdr-scripts` `MCode.py`, `gps_l1c_tx.py`. Tests: `test_param_form_derived_labels.py`.

## Sequence power achievability + step/ramp power control: COMPLETE (multi-session)
Branch `claude/sequence-power-achievability` (all three repos; client-only). Full design + code
map live in **`docs/sequence-power-achievability.md`**. In one line: sequence power achievability
is **temporal** (a power ramp's top levels can become unachievable when a *later* tune step retunes
the carrier), so the guarantee is a sequence-level, time-ordered **`achievability_warnings`** pass
(warn, never block; name the clamped points/times/ceiling), NOT a per-step fold.
**Surface A** — `timeline_model.achievability_warnings(items, resolve)` + `TimelineEditor` wiring
(amber banner above the canvas), tests `tests/test_achievability_warnings.py`. The pure fold helpers
`eval_formula` / `fold_params_from_values` live in **`state/power_fold.py`** (re-exported from
`param_form` for compatibility). **Surface B** — the ramp editor's From/To range + achievable-level
snapping fold through the live bridge params (`BoundedNumberField(fold_params=…)`,
`ramp_editor._op_state`/`_op_params`), tests `tests/test_ramp_cal_param_fold.py`.
**Surface C** — the multi-quantity power card (`param_form._add_power_unit_ui`: ALSO READS AS
companions, `Control in this →`, DEPENDS ON, family chips, finest-step rounding) now renders in the
step editor's run/tune steps. `TimelineEditor._script_power_laws` caches `calibration_power_laws`;
`StepEditorDialog._build_form` passes `power_laws` to both branches and, for a tune step, the full
schema + `context_dests` (non-live dests) seeded from `_carried` via the static
`_seed_context_from_carried` (the tune analogue of `live_tune_dialog._prepare_specs`; fresh spec
copies — never mutates the shared param cache). A new `ParamForm.set_fold_context(cal_freq_default=,
context_defaults=)` updates `_cal_freq_default` + the context specs' defaults then re-folds
(`_do_refold(hold_display=True)`) only when the fold point actually moved; `_refold_for_position`
recomputes `_carried` and calls it, wired to the anchor/offset change signals. Saves are unchanged
(run/bar → `build_args`, tune → `values`, both base-quantity `--power`, no context leakage). Tests:
`tests/test_step_editor_power_units.py`. Client-only; no agent/scripts/capability change; drift
guard intact.
**Post-review fixes (owner testing).** (1) **Power card vanished in the step editor after a ramp
editor was opened**: the param cache had two writers (`StepEditorDialog._on_params`,
`ramp_editor._on_params`) and only the step one recorded `_script_power_laws`, so a ramp-warmed
cache left the step editor with no laws → no card (and `--bw` no longer re-folded the limits). Now
BOTH write through one `TimelineEditor.cache_script_meta(script, result)` that populates params +
cal signal + freq param + power laws together, so a warm cache is always complete. (2) **Held-power
achievability** (`timeline_model` step 4): a fixed `--power` (e.g. spectral density at its max) set
by one tune is now flagged when a LATER tune changes freq/bandwidth and pushes it out of range — the
walk re-checks the standing power on freq/bridge-param events (`_held_power_issue`). (3) **The card
never appeared in a PLAN's step editor** (the real owner-reported case): a plan/sequence fetches
script params from the offline **LibraryClient**, not the unit, and `LibraryClient.get_script_params`
passed through `calibration_signal` + `calibration_freq_param` but DROPPED `calibration_power_laws` —
so the plan step editor got no laws (the Run/live-tune forms, talking to the unit directly, were
unaffected). Now it surfaces the laws too. Tests:
`test_step_editor_power_units.py` (ramp-warmed cache still yields the card),
`test_achievability_warnings.py` (held-power clamps / silent / warn-once),
`test_library_script_params_cal.py` (the library surfaces `calibration_power_laws`).
**Temporal warnings completed (owner testing).** The banner now flags EVERY commanded power at its
fire-time operating point, not just ramp points and held levels: (1) **Directly-SET power steps** —
a tune/run/baseline step that COMMANDS a `--power` the unit can't deliver at the operating point in
effect when it fires (e.g. re-setting the density to bw-10's max after an earlier step widened the
sweep to 20 MHz) is flagged via `timeline_model._set_power_issue`, distinct from `_held_power_issue`
(a standing level pushed out of range by a later change). The walk's directly-set branch folds
bounds through the carried freq/bridge params and warns when the command clamps; in-range commands
stay silent. (2) **Proactive params prefetch** — `TimelineEditor._update_achievability` demand-drives
`_prefetch_seq_params` (→ `_on_prefetch_params`, routed by a distinct `tl_prefetch:` label, its own
`_prefetch_inflight`), so the banner surfaces on load WITHOUT opening a step/ramp dialog first; the
step dialog's `_on_params` also refreshes the banner after caching. Together these fix the owner
report "I set the spectral density to max at bw 10, later doubled the bw / re-set the same power,
and got no warning." Tests: `test_achievability_warnings.py` (directly-set clamp at the carried bw;
held-then-re-set → two warnings; in-range silent; run step folds at its own args; prefetch surfaces
the banner from an empty cache without a dialog).
**ROOT CAUSE found — the chirp base density is BANDWIDTH-INVARIANT (design doc §10).** The owner still
saw no chirp warning/limit because none of the above helped: the real FM-chirp calibration's operating
/base quantity is the FIXED-reference measured density, and (constant-amplitude ⇒ bw-invariant total
power, ceiling via the constant `fbw` law) `PowerFold.param_dependent` is **False** — `bounds_at` is the
same at every `--bw`. The bandwidth dependence lives ONLY in the `psd_live` `restates_measurement` VIEW
law (`param_form._view_delta`), so the whole base-quantity machinery (`achievability_warnings`, the step
/ramp limits) can't see it and the walk's `param_dependent` gate skips the task. Fixture tests missed
this because their `_CHIRP_ART` puts the density as a *reported bridge* (making it param_dependent);
`tests/test_step_editor_carried_bw.py` pins the REAL structure (artifact verbatim from the resolver).
**Fixed this session (committed):** the STEP-EDITOR limit now folds the `psd_live` view at the CARRIED
sweep bandwidth — a live bridge param the view keys on (`--bw`) that a power-only tune step isn't
setting is seeded from carried on open (`StepEditorDialog._fold_bridge_dests` in `_build_form`) and on
move (`_refold_for_position`); `ParamForm.set_fold_context` accepts those bridge dests. So a step
carrying bw 20 caps the density ~3 dB below the bw-10 max, blocking an undeliverable density from being
authored. **NEXT (owner chose HOLD-LIVE-DENSITY + WARN — design doc §10):** make `achievability_warnings`
and the RAMP editor fold the CONTROLLED (view) quantity at each step's fire-time `--bw` and warn when a
held/commanded density is undeliverable there; decide whether the transmit path must actually hold live
density (agent/scripts change) or client-only limit+warn suffices. Reproduce real artifacts via
`PYTHONPATH=/home/user/sdr-agent … agent.calibration.resolve`.
**HOLD-LIVE-DENSITY: COMPLETE (branch `claude/temporal-power-warnings-zemu9p`, client-only).** Owner
decided: hold the LATEST-SET control quantity (density / total power / gain — the Run/Tune power card
on a timeline), via CLIENT PRECOMPUTE (key finding: the Run/Tune form already holds the quantity
client-side — `_do_refold(hold_display=True)` keeps the displayed value and `values()` re-sends the
recomputed base; the unit just applies base), clamp+warn, SET-TIME-bandwidth intended value. Shipped:
(1) **Warn** — `achievability_warnings` folds the controlled view at each event's fire-time `--bw`
(`_view_law_of`/`_view_delta`/`_clamp`; resolver surfaces `view_law`+`view_laws`); the real bw-invariant
chirp now warns (`state/power_fold.resolve_keyed_values` factored out). (2) **Persist** the per-step
control view (`SequenceStep.power_view` + `BarItem`/`RunItem`, round-tripped; `ParamForm.power_view()`/
`set_power_view()`; step editor saves/restores it). (3) **Latest-set-wins** — the walk holds whichever
view the latest power-setting step chose (bw-keyed → density held; total power/gain → base held, no
false warn). (4) **The hold** — `timeline_model.hold_control_quantity` injects the base `--power` the
unit needs at each `--bw` change to keep the density constant, clamped; wired into
`TimelineEditor.steps()` (deploy), stripped on load (`power_hold_dest`) so the canvas stays clean and
upstream edits propagate (idempotent). (5) **Ramp editor** authors `--power` in the live-density view
(`BoundedNumberField(view_offset)`, `_control_view`/`_view_offset`; stores base, records `power_view`).
No agent/scripts/capability change; drift-guarded files untouched. Tests: `test_achievability_view_fold`,
`test_step_power_view_persistence`, `test_hold_control_quantity`, `test_ramp_view_fold`. Docs: §10.
**Owner testing fixes (round 1):** (A) **Rounding blocked the max density.** A --power view shifted by
a log10 view_offset gives a bound finer than the display (a −7.24 base max → −10.2503 at bw 20, shown
as −10.25); the spinbox reads its value back rounded to −10.25 which then read as ABOVE −10.2503 and
falsely warned "clamped", so the operator couldn't select the max. Fixed by tolerating half a display
step in the over/under checks (`ParamForm._wire_rail`, `BoundedNumberField._on_change`); tests
`test_bounded_number_field.py`. (B) **The tune pill showed the raw base** (−7.something) not the
controlled density. `TimelineEditor._pill_power_display` now shows a bw-keyed --power in its view (base
+ view_delta at the carried bw) with its unit; `_run_label` uses it; test `test_step_editor_carried_bw.py`.
**Owner testing fixes (round 2, branch `claude/temporal-power-warnings-fixes-je1r9b`, client-only).**
Three bugs from the hold-live-density testing (see `docs/sequence-power-achievability.md` §10). (1) **A
density RAMP going undeliverable after a LATER `--bw` widen wasn't warned.** The walk's `ramp_point`
branch expressed each point as `base + view_delta(FIRE-time bw)` while `_clamp` shifted the range by the
SAME delta → they CANCELLED to a bare base-range check (bandwidth-invariant → never fires). A ramp is
AUTHORED once at its start width, so its intended density is CONSTANT across the ramp: `achievability_
warnings` now captures the authoring view-delta at the ramp's first point (`ramp_auth_vd[uid]`) and
checks `base + view_delta(authoring_bw)` against the range folded at each point's OWN fire-time `--bw`.
Static-bw ramps and the no-view path stay byte-identical. Tests: `test_achievability_view_fold.py`.
(2) **The ramp step editor couldn't author `--power` in other quantities.** Added a "Set power in"
picker (the ramp analogue of the card's `Control in this →`): `ramp_editor._power_views`/`_selected_view`
/`_populate_power_views`/`_on_power_view_changed` let the operator ramp in density / total power / dBm/Hz
(base still stored, `power_view` recorded, values convert on switch). Tests: `test_ramp_view_fold.py`.
(3) **The max density still couldn't be selected.** A calibrated `--power` field with no default renders
as a **QLineEdit** (the real chirp `--power` has no `step`); round 1's half-display-step tolerance only
covered spinboxes, so the QLineEdit view-max (−10.3903 shown as −10.39) still tripped the amber over-warn
AND failed `validate()` ("out of range"), blocking the save. `_wire_rail` now applies the same
`0.5·10^-_power_decimals()` tolerance to a QLineEdit power field, and `ParamForm.validate()` tolerates it
when range-checking a `snap_role="power"` field. A genuine overage still warns/blocks. Tests:
`test_range_rail.py`. No agent/scripts/capability/version change; drift-guarded files untouched.
(4) **The tune pill showed the raw base for a FULL-BANDWIDTH-power step.** `_pill_power_display`
(`ui/timeline_editor.py`) only rendered a bw-KEYED view (density) and bailed on `not law.params()`,
so a total-power step (`fbw_power`, a constant-offset law) showed `power=<base>` on the canvas pill.
A param-less view now uses the law's representative delta → the pill shows total power (base + ~10 dB,
bw-invariant) with its unit; density/`dBm/Hz` unchanged, no-view still raw. Test:
`test_step_editor_carried_bw.py`.
(5) **The ramp editor's per-step listing was a cramped box with its own scrollbar** (one line at a
time). The whole ramp dialog body is now wrapped in ONE `QScrollArea` (buttons pinned below), and the
listing is a `QLabel` (`ui/ramp_editor.py` `_build`, `_refresh_steps_view`) that expands to its full
height inside that shared scroll — so a long ramp shows every step and the dialog scrolls as a whole.
The dialog is capped to ~90% of screen height so the scroll engages when the body is tall. Test:
`test_ramp_view_fold.py` (`…_step_listing_shares_the_form_scroll_and_expands`).

## Current state — opt-in measured-curve extrapolation: COMPLETE (branch `claude/calibration-extrapolate`)
Cross-repo (agent + client). A signal's measured curve may set `extrapolate: down|up|both` (default
`none`) on its curve entry to continue the end-segment slope past the measured gain endpoints instead
of clamping flat — so `--power` reaches a gain that wasn't measured (motivating case: extrapolate DOWN
because the low-gain measurement sits in the analyzer's noise floor). Gain is still clamped to the
ceiling, so it extends the *curve*, not the limits. Client pieces: (1) **`state/power_fold.py`** mirrors
the fold via a new `_interp_ex` on the operating anchor (reads the artifact's top-level `extrapolate`),
so the form's `--power` range matches what the unit's `calkit` delivers. (2) **`ui/calibration_panel.py`**
adds a per-signal picker (None/Down/Up/Both) on the measured-points dialog (`_open_points_dialog`),
stored on the persistent `_CurveTable` widget (`tbl._extrapolate`, seeded in `_render_signal_form`,
serialized in `_read_form`) so every edit surface preserves it and the doc stays clean when `none`.
(3) A save-time capability gate `_blocks_on_extrapolate` / `_doc_uses_extrapolate` on
`CAL_EXTRAPOLATE_CAPABILITY` ("calibration-extrapolate", agent ≥ 1.15.0) — a safety gate: an older
agent would clamp, delivering a different power than the range shown. Drift-guarded files untouched.
Tests: `test_power_fold_extrapolate.py`, `test_calibration_extrapolate.py`. Agent side: resolver +
`calkit` + `docs/calibration.md` §7.5 (see sdr-agent CLAUDE.md).

## Packaging as a standalone no-admin Windows app: COMPLETE (branch `claude/packaging-standalone`)
Owner decisions: **Windows 10/11 x64 only · no code-signing cert · Provision-unit IN scope · build on
the owner's own Windows PC** (I write the config; they run it). Full build/install/distribute guide is
**`docs/packaging-standalone.md`**. Shipped, client-only, additive (no capability/version bump, drift
guard intact):
- **Writable-state relocation (the load-bearing prerequisite)** — new **`paths.py`** is the single
  source of truth: `data_dir()` = `$SDR_CLIENT_DATA_DIR` override → per-user OS dir when **frozen**
  (`%APPDATA%\SDR Broadcaster Control`) → **repo root from source**. Every store's default now reads
  `paths.data_file(...)` (`units.yaml`, `plans.json`, `schedule.json`, `library.json`,
  `components.json`, `address_cache.json`, `calibration_cache.json`, `unit_ledger.json`).
  `resource_dir()`/`seed_defaults()` seed a starter `units.yaml` on first frozen launch; `main()`
  calls it and sets the window icon. **Source mode returns the repo root byte-for-byte**, so dev + the
  whole suite are unaffected (tests: `tests/test_paths.py`; 680 passed, zero edits to existing tests).
  Fonts + agent bundle stay resource-relative (read-only), not moved.
- **Freeze** — **`sdr_client.spec`** (force-added past the `*.spec` gitignore): PyInstaller `--onedir`,
  windowed, icon; collects Qt plugins, IBM Plex fonts, icon, starter `units.yaml`, and staged
  `bundles/sdr-agent-*.tar.gz`; whole-package submodule collection for the native-wheel gotchas
  (`zeroconf`, `paramiko`, `cryptography` — collected whole so the floating version's paramiko-optional
  submodules like `hazmat.decrepit` are present — `pydantic`, `pydantic_core`); UPX off. **`packaging/
  make_icon.py`** generates the placeholder `ui/assets/app.{ico,png}` (swap the files to rebrand).
  Validated by a **Linux `--onedir` smoke build**: frozen app boots headless, seeds `units.yaml`,
  loads fonts, starts zeroconf discovery + poller, no import errors.
- **Installer** — **`packaging/installer.iss`** (Inno Setup, `PrivilegesRequired=lowest`,
  `%LOCALAPPDATA%\Programs\…`, per-user shortcuts + uninstall, unsigned) and **`packaging/build.ps1`**
  (venv → pip → PyInstaller → portable ZIP fallback → ISCC). **Unknown-publisher (no cert):** honest
  mitigations only — IT allow-list / software portal first, portable ZIP + shortcut over the installer
  exe, "Run anyway"; self-signed doesn't help. **Open:** must still be smoke-tested on a clean Windows
  10/11 VM (no-admin install, icon, real-unit discovery, Provision) — the Linux build can't cover that.
- **`packaging/README.md`** — the one-glance build cheat-sheet: the PowerShell command
  (`powershell -ExecutionPolicy Bypass -File packaging\build.ps1`), its `-Clean`/`-SkipInstaller`/
  `-Python` variations, prerequisites, and where the artifacts + version come from. Points at
  `docs/packaging-standalone.md` for the full guide.

## Current state — source-bias table fills its dialog: COMPLETE (branch `claude/table-and-ramp-fixes`, client-only)
Bug: the freq→dBm grid in the Source-bias editor (`_edit_source_bias`) only ever showed one row —
expanding the window grew the space ABOVE the grid, not the grid. Root cause: `_CurveTable._fit_height`
caps `maximumHeight` to ~content (≈ header + up-to-12 rows), so a one-row grid stayed one row tall.
Fix (`ui/calibration_panel.py`): `_CurveTable` gained a `fill=False` flag; with `fill=True` it takes an
Expanding vertical size policy and `_fit_height` drops the max-height cap (keeps a ~3-row floor), so the
table grows to whatever the layout gives it. `_edit_source_bias` builds the grid `fill=True`, adds it
with a stretch factor (`lay.addWidget(tbl, 1)`), and opens the dialog at 460×520. The measured-points
dialog and every other `_CurveTable` are unchanged (default `fill=False` still caps to content). Tests:
`tests/test_curve_table_ux.py` (a fill table doesn't cap its height + takes an Expanding policy; a
non-fill table still caps).

## Current state — attenuator engagement no longer caps the minimum power: COMPLETE (branch `claude/table-and-ramp-fixes`, cross-repo)
Bug: the minimum achievable power of a signal tracked the programmable attenuator's `engage_pct`
(lower engagement → lower min, and vice versa). It shouldn't — the engagement % should decide only
WHEN the attenuator starts being used, not the absolute floor. Root cause in the shared
achievable-power resolver (`state/achievable.py`, mirrored verbatim from `sdr-agent/paramkit/
achievable.py`): `AchievableGrid._gain_points` clamped the SDR gain floor to the engagement
threshold gain `_g_thr`, and `realize`/bounds clamped the target to `_thr − _span`, so the min was
`P_base(_g_thr) − max_atten` instead of `P_base(min_gain) − max_atten`. Fix: the achievable SET
spans the whole SDR grid down to min gain (`_gain_points` floors at `_lo_g`; `realize` clamps the
target at `_s_lo − _span`); the threshold now only steers `realize`'s (gain, reduction) CHOICE via
a tiebreak — ABOVE the threshold least-reduction wins (SDR-first, attenuator at rest), and once the
attenuator is MAXED the SDR drops below the threshold (most-reduction wins there) to extend the low
end. So the floor is `SDR@min_gain + attenuator@max` for every `engage_pct`, while the engaged
region still holds the SDR at the threshold. Both copies kept byte-identical (manual mirror, like
`power_law.py`); the agent resolver + `calkit` transmit fold get the same fix, so the unit actually
delivers the extended low end. Tests: client `tests/test_power_fold.py` (min independent of
engage_pct; SDR drops below threshold when the attenuator is maxed); agent
`tests/test_calibration_active.py`, `tests/test_achievable_grid.py`.

## Current state — ramp editor fixes (size, slider max, gain): COMPLETE (branch `claude/table-and-ramp-fixes`)
Three owner-reported ramp editor fixes (client-only):
- **Opens too small.** `RampEditorDialog._build` now `resize()`s to a size that fits the power card
  horizontally and shows a large part of the form vertically (700×820, clamped to 0.95×/0.9× the
  screen; `setMinimumWidth(560)`), instead of collapsing to the scroll area's tiny min-size hint.
- **Slider couldn't reach the max — root cause: a phantom max.** When a stage limit (e.g. an
  amplifier INPUT limit) caps the ceiling gain BETWEEN the SDR's grid steps, the agent reports the
  CONTINUOUS ceiling power (40.33) as `max_power_dbm`, but the SDR can only set grid steps, so the
  top the chain can actually deliver is one step below (40.08). The shown max was thus a phantom
  the slider could never reach. **Fix (`state/power_fold.refold_bounds`):** on a constant chain
  (freq/param-dependent chains already fold through `bounds_at`, which snaps the ceiling gain via
  `_snap`), pin `max_power_dbm` to `snap_power(max)` — the top REALISABLE level — so the displayed
  max is a real, reachable step, not a between-steps value. A no-op when the ceiling is already
  on-grid or the chain has no gain grid. Secondary: `BoundedNumberField.snap()` and
  `ParamForm._wire_rail` treat the exact `min`/`max` as reachable snap-stops (nearest of {grid
  level, lo, hi}) so a full-right drag lands on the max despite its 2-decimal display rounding
  sitting a hair below the true folded level. Tests: `tests/test_power_fold.py`
  (refold pins a between-steps ceiling to the realisable max; leaves an on-grid ceiling untouched).
- **Relative-power (gain) step + overshoot.** The ramp editor's `_with_cal_bounds` only applied
  `apply_power_bounds` (→ `--gain` kept the script's 0..90 / step 1 and the slider overshot to 90 >
  the 89.75 ceiling, still startable). It now ALSO applies `apply_gain_bounds`, narrowing `--gain`
  to the calibrated `[min_gain_db, max_gain_db]` on the SDR's real `gain_step_db` (0.25) — so the
  field/slider clamp to 89.75 and step by 0.25. `snap()` gained uniform-step-grid snapping (with the
  same reach-the-extremes rule) for stepped non-power fields. Tests:
  `tests/test_bounded_number_field.py` (snap reaches a between-steps ceiling; step-grid snap reaches
  the max), `tests/test_ramp_cal_bounds.py` (a --gain ramp narrows to the calibrated range + 0.25
  step and clamps at 89.75). Full headless suite: 738 passed.

## Current state — calibration grid cell selection (copy / clear a block): COMPLETE (branch `claude/table-and-ramp-fixes`)
The calibration curve grids (`_CurveTable` in `ui/calibration_panel.py` — measured points, gain/power
curves, the source-bias grid) now support spreadsheet-style **cell-range selection**: click-drag a
block, **Ctrl+A** selects all, **Ctrl+C** copies the selection as a tab/newline block (pastes into a
spreadsheet), **Del/Backspace** clears every selected editable cell in one undo step. Was
`NoSelection` (only a focus outline); now `ExtendedSelection` + `SelectItems`, with the selection
dropped on focus-out (`_on_focus_changed` also `clearSelection()`) so nothing lingers after
click-away. `keyPressEvent` gained Copy/SelectAll handling; `_copy_selection` builds the bounding-rect
TSV (unselected cells inside the block come through blank; no selection → the current cell) and
`_clear_selection_contents` empties the selected cells (falls back to the current cell), both keeping
the existing Ctrl+V paste / Ctrl+Z-Y undo / right-click menu. The Signals table (a click-to-open
navigation control with a hand-painted highlight) is intentionally left `NoSelection`. Client-only.
Tests: `tests/test_curve_table_ux.py` (Ctrl+A/Ctrl+C block + single-column + current-cell copy,
Del clears the selection in one undo step and spares unselected cells, focus-out clears selection).

## Current state — offline calibration fallback in plans/sequences: COMPLETE (branch `claude/ramp-power-quantities`)
Authoring a plan/sequence for a calibrated unit that is OFFLINE now folds absolute power from the
unit's **last-known cached calibration** (`state/calibration_cache.py`, keyed by hostname; already
populated whenever a unit's `/calibration` is seen — the Calibration tab and the timeline both `put`
it), instead of dropping to no calibration. The machinery existed (`TimelineEditor._on_cal_result`
falls back to `cache.get(host)` and flags `_cal_stale`), but only for `AgentConnectionError`; a unit
never seen THIS session isn't in the fleet so `Fleet.get` raises **`KeyError`**, which fell through to
the "reachable but uncalibrated" branch → no bounds. Fixed by reclassifying `_on_cal_result`: a
`dict` (valid → use+cache; invalid → none) and an **`AgentHTTPError`** (a real 404 "uncalibrated" →
none, never stale) are the only "don't use cache" cases; **every other failure** (connection error,
`KeyError`/undiscovered, timeout) falls back to the cached calibration, marked stale. It refreshes to
live the next time the unit is reachable (`set_calibration` re-fetches). **Clear to the user** in
three places: a timeline-level accent banner (`TimelineEditor._cal_stale_banner`/
`_update_cal_stale_banner`: "<unit> is offline — absolute power uses its last-known calibration, last
seen <ts>…"), the step editor's existing stale status line, and a new ramp-editor notice
(`RampEditorDialog._cal_note`/`_update_cal_note`). Client-only; no agent/scripts/capability change;
drift-guarded files untouched. Tests: `test_calibration_cache.py` (undiscovered `KeyError` → cache +
stale; offline-no-cache → none; banner shows offline / hides online), `test_ramp_cal_bounds.py`
(ramp offline note shows when stale, hides when fresh).

## Current state — ramp step editor power card: COMPLETE (branch `claude/ramp-power-quantities`)
The ramp step editor's calibrated `--power` control now renders the **multi-quantity power card** —
the ramp analogue of the Run/Tune card — instead of a plain "Set power in" dropdown + From/To boxes.
Design record: `docs/ramp-power-mockup.html` (interactive mockup). In `ui/ramp_editor.py`, the From/To
fields live in ONE full-width `_power_area` that `_render_power_area()` rebuilds into EITHER the card
(calibrated `--power` with ≥2 views) OR plain From/To rows (any other parameter / a single view). The
card matches the mockup: a **RAMP POWER** header (+ LIVE), a **RAMPING IN** primary block with a
vertical accent-soft→surface **gradient**, the swept quantity's name + family-coloured unit chip, the
two `BoundedNumberField` From/To fields (kept verbatim — all calibrated folding/snapping/clamping/
view-offset preserved — but built with `pinput=True` so they render as the mockup's **`.p-input`**: a
bordered box with a big right-aligned mono value, a unit segment, and stacked ▲/▼ steppers that route
through the spinbox's achievable-level stepping), their sub-labels showing the **actual fire times**
(`_ft_sublabels`: anchor start → `on-air +Xs` / `ramp end`; stop → `ramp start` / `off-air −Xs`; both →
insets), **ONE shared dual-handle rail**
(new `param_widgets.DualRangeRail` — two handles over MIN..MAX with the swept span filled; a drag snaps
to an achievable level via `BoundedNumberField.snap()`/`bounds()` and writes the field, which re-syncs
the handle), MIN/MAX labels, a rising/falling **span** read-out, and a **DEPENDS ON** chip row of the
fold frequency + carried bridge knobs (`_ramp_dep_chips`/`_resolve_dep_source`). Then an **ALSO READS
AS** grid of full-width read-only companion tiles (`_companion_card`), each showing that quantity's live
From → To (`_update_power_readouts`) with a **Ramp in this →** button (`_set_ramp_power_view` → drives
the now-HIDDEN `_power_unit` combo, so `_on_power_view_changed`'s value-conversion wiring runs
unchanged). The combo is kept as the state/logic holder (tests still read its item ids/data); the visible
switch is the card. `_card_active`/`_companion_labels`/`_span_lbl`/`_pwr_rail`/`_pwr_min`/`_pwr_max`
expose the card for tests. The sibling fields (Task, Parameter, Anchor, Offset, Define by,
steps/step/hold/duration, Include) render as contained **`.ofield` rows** (module `_ofield`: a
bordered surface-alt box with a fixed-width accent-ink label + the flattened control + an optional
faint unit hint), stacked in a `QVBoxLayout` — replacing the old `QFormLayout` — so they flow
edge-to-edge with the power card like the mockup; the controls keep their identity (`_task`,
`_anchor`, `_mode`, … unchanged) and are flattened by the dialog's `#ofield` QSS, and mode/anchor
toggles now show/hide whole rows (`_row_steps`/`_off_row`/`_offend_row`/`_inc_row`). Everything is
untouched and fully functional. Client-only; no agent/scripts/capability change;
drift-guarded files untouched. Tests: `tests/test_ramp_view_fold.py` (card lists/defaults, hidden for
non-power, companion From→To live, "Ramp in this →" promotes, span direction, plain rows for a non-power
param, one shared dual rail not per-field, dual-rail drag snaps + clamps, MIN/MAX reflect bounds).

## Current state — Run/tune power control redesign: COMPLETE
The calibrated `--power` control (Run/tune form) is now the mockup's power card: one PRIMARY
quantity you set (large step-rounded value + range rail with labelled MIN/MAX + a family-coloured
`quantity [unit]` chip + `LIVE`) and each OTHER quantity as its own
read-only live field in an "ALSO READS AS" grid, each with a `Control in this →` button
(promotes it via `_set_power_view` → `_power_view`/`_do_refold`) — replacing the old
`control in` dropdown + green `= … · name` lines. Below the grid a `DEPENDS ON` row surfaces the
fold inputs (the fold frequency in MHz when freq-dependent, plus every field the range depends on
via `_dep_param_dests`), superseding the power field's old "moves with frequency" rail note
(`--gain` keeps it). A bridge param with no input field of its own — an INTERNAL derived quantity
a law keys on (e.g. GPS C/A's full-power law keyed on an equivalent-noise bandwidth `enbw_mhz`
that's a hidden table lookup on `--sidelobes`) — is resolved to the SOURCE knob behind it
(`--sidelobes`, its count), not the derived intermediate (`_is_input_field` reads `_base_specs` so
it's correct mid-render, before later fields are in `_widgets`).
Unit chips are family-coloured (slate = absolute dBm, teal = a spectral density; `_unit_family`),
and every power read-out (value, MIN/MAX, companions) is rounded to the chain's finest achievable
step (`_decimals_for(fold.finest_step())`). Reuses the existing data model unchanged (`_power_views`
/ `_reported_base` / `_selected_view` / `_view_delta` / `_shift_power_spec`); `--power` is still
SENT in the base quantity (`build_args`). Lives in `ui/param_form.py` `_add_power_unit_ui` (+ the
`_field_frame` power branch and the `_dep_specs`/`_deps_row`/`_companion_card` helpers). Client-only,
no agent/scripts change, no capability/version bump, drift-guarded files untouched. Design record:
`docs/param-form-power-redesign.md` + `docs/param-form-power-mockup.html`. Tests:
`test_param_form_power_units.py`, `test_range_rail.py`.
The **live-tune ("Tune…") form** now offers the same card: `ui/live_tune_dialog.py` reads the
script's `calibration_power_laws` from `get_script_params` and forwards it as `power_laws=` to
`ParamForm.set_params` (it already forwarded `cal_freq_param`), so the ALSO READS AS companions +
`Control in this →` switch render while retuning — a companion tracks a live bridge param (e.g.
the chirp's `--bw`) exactly as in Run. Was missing because the dialog dropped `power_laws` from
the `set_params` call.
It also folds against the DEPLOYED FIXED PARAMS, not just the live ones. The tune form edits only
live params, but a law/limiting reading can key on a quantity behind a NON-live knob — GPS C/A's
LIMITING reading keys on `enbw_mhz`, a hidden derived table lookup on the (live) `--sidelobes`,
and the carrier `--freq` is fixed per run. So `set_params` gained `context_dests=`: dests kept in
`_base_specs` for FOLDING but never rendered as editable fields (`_effective_specs` skips them,
`_is_input_field` excludes them). `live_tune_dialog` now passes the FULL schema + its non-live
dests as `context_dests`, seeds each fixed param's DEPLOYED value (parsed from the task command)
onto its spec default, and passes the deployed `--freq` as `cal_freq_default`. Result: retuning
`--sidelobes` re-folds the `--power` range/ceiling through the limiting reading (enbw tracks the
live count), and the range folds at the deployed carrier — the limits/power match the running
task. `_fold_freq_now` falls back to the schema-default freq (never `None`) when no freq field is
rendered; `_dep_specs` still names a context-only `--freq` in DEPENDS ON. Tests:
`test_live_tune_power.py` (only-live knobs render; ceiling tracks `--sidelobes`; deployed freq
parsed). Its autouse `_flush_deferred_deletes` fixture drains Qt's DeferredDelete queue after each
dialog test (a pre-existing headless-Qt teardown SIGABRT that leaked into a later module).
The `--power` DISPLAY DECIMALS read the chain's finest DEVICE step, NOT the slope-folded power
increment. `PowerFold.finest_step()` returns the smaller of the SDR gain step and each active
component's step, in dB (a 0.25 dB gain grid → 2 decimals, a 0.001 dB attenuator → 3); it no longer
folds the gain step through the calibration curve. A non-unit slope turned 0.25 dB into a messy
0.25125 dB power step, so the field showed 4–5 spurious decimals (a slider stop read −21.2325, the
limits −26.7600 / −12.51000) implying a precision the hardware lacks — and on a multi-segment,
frequency-dependent chain that folded step even swung with the carrier. The device step is clean and
frequency-independent, so `_power_decimals` (= `_decimals_for(finest_step())`) is stable across
frequency, `--bw` and Run/Tune; only the BOUNDS/levels fold at the live carrier. Every power
read-out honors it: the spinbox (`setDecimals` from the same `finest_step()` in `apply_power_bounds`),
MIN/MAX + companions (`_power_bound_fmt`/`_fmt_power`), and a rail drag into a `QLineEdit` power
field (`_wire_rail`'s `set_widget` now formats a `snap_role="power"` value at `_power_decimals()`
instead of `:g`). `finest_step` dropped its unused `freq` arg. Tests: `test_power_fold.py`
(`..._finest_step_is_the_device_step_not_the_slope_folded_increment`), `test_range_rail.py`
(`..._power_lineedit_rail_drag_rounds_...`), `test_live_tune_power.py`
(`..._power_decimals_read_the_device_step`, `..._power_decimals_match_the_run_form`).
The clamp warning tolerates a MID-TYPED value: an uncalibrated-default `--power`/`--freq` renders
as a `QLineEdit`, so `_form.values()` hands `clamp_warning` the raw text — a lone `-` (or `''`,
`'1e'`) while the operator is still typing. `state/power_fold.py` `clamp_warning` now coerces
`freq_hz`/`power_dbm` via `_as_float` (unparseable → treated as "unknown" → silent) instead of a
bare `float()`, which raised `ValueError` and crashed the tune dialog on every keystroke of a
negative power. Tests: `test_power_fold.py` (`..._tolerates_partially_typed_values`).
The clamp warning also folds at the SAME frequency + params as the `--power` range now. It used to
read the freq field straight from `values()` (in MHz, so it folded at ~0 Hz) and passed NO params,
so a fixed-carrier signal (GPS C/A — `--freq` is fold context, absent from `values()`) never warned
at all and it never tracked `--sidelobes`. `ParamForm` exposes `fold_freq_hz()` (=`_fold_freq_now`,
Hz — the live field scaled, a context carrier, or the schema default) and `fold_params()`
(=`_live_params`, the bridge-keyed values), and `live_tune_dialog._update_clamp_warning` folds
`clamp_warning` through both, so the caption's ceiling matches the displayed range (tracks the live
`--sidelobes`/`--bw`, at the deployed carrier). Tests: `test_live_tune_power.py`
(`..._clamp_warning_folds_at_the_range_frequency_and_params`).
Frequency units reach the fold as Hz everywhere now. The fold helpers (`refold_bounds`, `snap_power`,
`clamp_warning`) all expect **Hz**, but a freq field's value is in its OWN unit (usually MHz), so it
must be scaled first. The SEQUENCE step editor (`timeline_editor._update_clamp_warning`) was the one
path that skipped the scale — it passed the raw MHz value straight in, so the caption folded at ~0 Hz
and read "0.001 MHz" for a 1227.6 MHz carrier. It now converts via `hz_per_unit(freq_unit)` before
folding. The field-unit→Hz map (`{hz,khz,mhz,ghz}`) is consolidated into ONE `param_form.hz_per_unit`
helper, used by `ParamForm._freq_unit_factor`, `ramp_editor._freq_unit_factor` and the timeline
editor — so no fold path can carry a mis-scaled carrier. (Audited every `refold_bounds`/`snap_power`/
`clamp_warning`/`bounds_at` caller: the run form (`_render_freq`), live tune (`fold_freq_hz`) and ramp
editor (`_op_freq_hz`) were already Hz.) Tests: `test_timeline_calibration.py`
(`..._clamp_warning_folds_at_hz_not_the_raw_mhz_value`, `test_hz_per_unit_maps_field_units_to_hz`).
The SEQUENCE step editor's clamp caption also folds through the live BRIDGE PARAMS now (a chirp's
`--bw`, GPS C/A's `enbw_mhz` behind `--sidelobes`), matching the run/live-tune forms. It used to pass
NO `params` to `clamp_warning`, so its ceiling stuck at the limiting law's REPRESENTATIVE value and
never tracked the knob. The step editor has no single `ParamForm` holding the full effective state
(a bridge-keyed source may be CARRIED from an earlier same-task step, not in this step's form; and a
DERIVED key like `enbw_mhz` — a table lookup on `--sidelobes` — isn't in the raw carried state at
all), so `fold_params()` off `_form`'s widgets alone is insufficient. Instead `timeline_editor.
_update_clamp_warning` resolves the keyed params over the `effective` dict (carried ∪ this step's
form) via the new `param_form.fold_params_from_values(artifact, specs, values)` — the dict-path mirror
of `fold_params`/`_live_params` (`provides` stand-in → own value → own derived `formula` → default;
None when unresolvable → representative fold). Its `formula` evaluation shares ONE source-agnostic
`param_form.eval_formula(formula, get_value)` extracted from `_eval_formula`/`_arg_value` — the widget
path passes `_source_num`, the timeline passes `effective.get`, so the two can't drift (same principle
as the `hz_per_unit` consolidation). `_live_params`/`_keyed_param_value` behaviour is byte-identical.
Client-only; no agent/scripts/capability change; drift-guarded files untouched. Tests:
`test_timeline_calibration.py` (`..._folds_through_live_bridge_params`, `test_eval_formula_reads_from_
a_dict_source`, `test_fold_params_from_values_resolves_derived_enbw`).

## Current state — --power never emits/snaps a base above the field max: COMPLETE (branch `claude/power-clamp-field-max`)
Bug (Run form, GPS C/A in the full-signal-power view): drag to max, change `--sidelobes`, drag to
the new max → the slider turned orange with NO warning, and Start crashed the script with "power
−49.1772 dBm is above the maximum −49.18 dBm". Root cause: the calibrated `--power` field renders
as a plain **QLineEdit** (no clamp) when the script gives no `step`; the achievable-level snap
returns a TRUE folded level (−49.1772) that rounds UP past the field max (`round(base,2)` =
−49.18, the SAME rounding `calkit.power_field_kwargs` uses, so it's exactly what the script's
argparse rejects); the round-2 display tolerance let `validate()` pass; and `build_args`/`values`
emitted the unclamped base. Fix (`ui/param_form.py`, client-only): `_power_field_bounds()` =
`round(cal bounds, 2)` (the script's field bounds); `_clamp_power_base()` clamps to them.
`_power_snappers()` clamps each snapped base to those bounds (so the top selectable level is the
field max — no phantom orange), and `build_args`/`values` clamp the emitted base (bulletproof, and
covers the QLineEdit-no-clamp path + the base quantity, not just views). Tests:
`test_param_form_power_units.py` (`..._drag_to_max_never_emits_a_base_over_the_field_max`,
`..._power_snapper_top_is_clamped_to_the_field_max`). No agent/scripts/capability change.

## Current state — start/stop sweep folds at the real span (`provides`): COMPLETE
`ui/param_form.py` resolves a law-keyed parameter through a visible derived stand-in when the
parameter's own field is hidden by a mode: `_provider_spec`/`_keyed_param_value` back a rewritten
`_live_params`, and `_wire_freq_refold` wires the stand-in's source fields so an edit re-folds.
Fixes the chirp reading total power at the stale `--bw` in start/stop mode instead of the actual
stop − start span. Driven by a new `paramkit` `.derived(provides="<dest>")` kwarg (the bandwidth
analogue of `is_freq`), carried through the drift-guarded `argspec`. Tests:
`test_param_form_provides.py`. Client-only display fix (the transmit fold was already correct).

## Prior state — param-form "control in" honours `restates_measurement`: COMPLETE
`ui/param_form.py` `_power_views` drops the raw MEASURED quantity from the --power "control in"
picker when a script law is flagged `restates_measurement` (and there's no reported override) —
the law re-expresses the measured reading live (e.g. a chirp's spectral density at the live sweep
vs the fixed calibration sweep), so the bw-frozen measured density no longer sits confusingly
beside its live twin. Explicit flag, never inferred from unit/family, so a same-unit distinct
reading (main-lobe vs total-in-band, both dBm) keeps the measured view. Tests:
`test_param_form_power_units.py`. Script side: `sdr-scripts` `fm_chirp_tx.py`.

## Current state — measurement de-embed pickers (per-signal + source bias): COMPLETE
`ui/calibration_panel.py` gained a **"measurement cable" component picker** (`_deembed_combo`:
"(no measurement cable)" + every catalog component, preserving a missing id or a JSON-authored inline
table as a locked entry) in TWO places, so each measurement records the bench cable it was taken
through and the agent removes its loss (docs/calibration-v2 §14.1; agent ≥ 1.14.0). (1) **Each
signal's Measurement card** sets `signals.<id>.curves.<plane>.measurement_deembed` — seeded per plane
into the signal editor state (`w["deembed"]`) at entry construction, written back in `_read_form`
(overrides any stored value; "" clears it). A per-signal power curve is a gain sweep at ONE frequency,
so its de-embed is a constant at the signal's measured-at freq — each signal keeps its own cable, and
a signal added later through a different cable is corrected independently. (2) **The Source-bias ("SDR
flatness") editor** sets `source_bias.measurement_deembed`, removed frequency-by-frequency (only a
frequency-dependent cable reshapes the flatness). (3) **A signal's LIMITING "Separate measurement
(dBm)" reading** (`_limiting_section` own branch) sets `signals.<id>.limiting.measurement_deembed`
(round-tripped via `_reading_block`) — its own bench cable, de-embedded INDEPENDENTLY of the signal's
primary curve (the agent shifts the own curve by `primary − own`; same cable == inheriting, different
cable overrides; only the ceiling moves, not the `--power` axis). Saving any placement is gated on
`CAL_DEEMBED_PER_SIGNAL_CAPABILITY` (`calibration-deembed-per-signal`) via
`_blocks_on_deembed_per_signal` (a safety gate; `_doc_uses_deembed_per_signal` scans curves, the
own limiting/reported readings, and the source bias). The pre-existing plane-level de-embed
(`_blocks_on_deembed`) is untouched and stays as a backward-compatible fallback (per-signal wins).
Client-only UI + gate; the agent does the math. Tests: `tests/test_calibration_deembed_client.py`.
**Persistence — a deleted de-embed cable is KEPT on units that use it.** `referenced_components`
(`state/component_catalog.py`) now counts every measurement-de-embed component id (plane, per-signal
curve, own limiting/reported reading, source bias) — not just chain-stage `component` refs — so deleting a measurement cable from
the shared library never strips it off a unit whose calibration still de-embeds it (the unit holds
its own `components.yaml`). The calibration panel's own saves route through a new
`CalibrationPanel._push_components(client, doc)` (fetches the unit's components + `plan_unit_deploy`
prune=True) instead of a blind `upload_components(to_wire())`, so a re-save keeps referenced parts
the catalog dropped, matching the fleet-deploy path (`api/fleet.py::_deploy_components_to`). Tests:
`test_component_catalog.py` (referenced counts de-embed; a deleted cable is kept),
`test_calibration_deembed_client.py` (`_push_components` keeps a unit-only cable).

## Prior state — stage limits gauged through the limiting reading: COMPLETE
The client mirror of the agent 1.13.0 fix: `state/power_fold.py` `_ceiling()` folds a stage
limit's `via_limiting` entry (or its own dBm `anchor_curve`) through the signal's limiting reading
at the live task parameter — so the form's `--power` range/ceiling match the transmit path. Saving
a document whose stage limit is gauged through a non-trivial limiting reading is gated on the new
agent capability (`CAL_LIMIT_THROUGH_READING_CAPABILITY` / `_blocks_on_limit_through_reading`, a
safety gate). The **Signals table** gained a per-row "Shown in" dropdown (`_quantity_views`): read
each signal's range in its measured quantity or in the dBm quantity its safety limit is gauged in
(the range column moved to index 2, the picker is index 3). Tests: `test_power_fold_bridges.py`,
`test_calibration_limit_reading_client.py`, `test_calibration_panel.py`.

## Prior state — per-signal signal editor redesign: COMPLETE (this branch → main)
See `docs/calibration-ui-redesign.md` (full record) and
`docs/calibration-signal-editor-mockup.html` (the locked design). Phase 1 (client) + Phase 2
(agent `≥1.12.0` + client, gated on `calibration-measurement-quantity`) shipped: per-signal
Measurement/Limiting cards, measured points in a dialog, Reported bridge + per-signal ceiling
removed, limiting laws constrained to dBm and scoped to the signal, and the agent publishes each
signal's measured quantity/unit as the operating `--power` axis. No open items in that redesign.
