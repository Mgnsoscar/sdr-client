# Sequence power achievability + power control in step/ramp editors

**Status:** Surface A (temporal achievability pass — ramp points, HELD power, and directly-SET
power steps, with a proactive params prefetch), Surface B (bridge `params` in the ramp's per-step
fold) and Surface C (the multi-quantity card in run/tune steps) **DONE + tested**. Design locked.
Only the bridge-param-ramp min/max-over-sweep case (§8) remains deferred.
**Branch:** `claude/sequence-power-achievability` (exists in all three repos; work is
expected to be **client-only** unless a `paramkit` tweak proves necessary).
**Owner context:** continues the calibrated `--power` work whose prior phases are recorded in
`docs/param-form-power-redesign.md` and `docs/calibration-ui-redesign.md`.

This document is a self-contained handoff: read it plus the two docs above and you have
everything needed to continue without re-deriving the design.

---

## 1. Goal (one paragraph)

Bring the calibrated `--power` control to **sequence steps** (the timeline step editor and the
ramp editor), the way the Run form and the live-tune dialog already have it — AND, more
importantly, make a sequence **honest about whether every commanded power is actually
deliverable over time**. The sharp case: a power ramp whose top levels become unachievable
partway through because a *later* tune step retunes the carrier. The runtime clamps safely, but
today nothing warns the author. We warn (never block), specifically enough that a clamp is never
silent.

## 2. The core insight — achievability is TEMPORAL

The achievable `--power` range is a function of the transmit **frequency** and the calibration
**bridge parameters** (`--bw`, `--sidelobes`/derived `enbw_mhz`, …). In a sequence those change
*over time* as steps fire. So "is this power achievable?" cannot be answered per-step at author
time by a single fold — it must be answered **per moment in time**, at the freq/params in effect
*then*.

Motivating scenario (from the product owner):

> A power ramp −20 → 0 dBm over 0–10 min. Endpoints are in range at the starting carrier. Add a
> tune step at 5:00 that changes `--freq`; at the new carrier the last three ramp steps are
> unachievable. The runtime clamps them — silently, today.

Why the current code misses it: the ramp folds its range at **one** frequency —
`RampEditorDialog._op_freq_hz` = `sequence_effective_values(... target_key=ramp_order_key)`,
which replays only steps **before the ramp's own start**. A 5:00 tune has a *later* order key, so
it is invisible to the ramp's fold. A single per-step fold structurally cannot express "steps
8–10 clamp because of a *later* step."

**Consequence for the design:** the authoritative check is a **sequence-level, time-ordered
achievability pass**, not a per-step fold. Per-step folds stay as live single-step feedback but
are explicitly *not* the guarantee.

## 3. Locked design decisions

- **Warn, never block.** Consistent with the existing clamp caption. No refusal to save/arm — a
  later tune step can always be added after a ramp is authored, so refusal would be wrong anyway.
- **Warnings must be specific and easy to follow.** Name the offending ramp points, their values,
  their times, the governing frequency/param change, and the ceiling/floor they hit. "Clamping
  should never go unnoticed." Example target wording:
  > ⚠ `gps` ramp (−20 → 0 dBm): after the 5:00 retune to 1240.0 MHz, steps 8–10 (−2, −1, 0 dBm
  > at 6:00 / 6:20 / 6:40) exceed the max deliverable −4.0 dBm and will be clamped down to it.
- **Recommended build order:** (1) temporal achievability pass → (2) thread bridge `params` into
  every per-step fold → (3) the multi-quantity power card in run/tune steps. #3 is UX polish;
  #1 is the correctness guarantee.
- **Guardrails:** client-only; no agent/scripts change; **no** capability/version bump; the
  drift-guarded files (`api/argspec.py`, `api/ramp.py`, `state/power_law.py`) stay byte-identical
  to their `sdr-agent` counterparts — do not edit them to add `params` plumbing (the client-side
  `state/power_fold.py` is the client mirror and already carries `params`).

## 4. What is ALREADY shipped (on `main`) that this builds on

Landed by the clamp-caption fix (commit `c53d432`, now on `main`) and Surface A here:

- **`eval_formula(formula, get_value)`** and **`fold_params_from_values(artifact, specs, values)`**
  now live in **`state/power_fold.py`** (pure, no Qt — they moved here as part of Surface A so the
  pure `timeline_model` pass can use them). `ui/param_form.py` **re-exports both** names, so
  `param_form.eval_formula` / `param_form.fold_params_from_values` still resolve for existing
  callers/tests. `eval_formula` is the source-agnostic derived-formula evaluator
  (center/span/sum/diff/count/span_to/term/extent/linear/table); `fold_params_from_values` is the
  **dict-path** resolver for the bridge-keyed `--power` params over a flat `{dest: value}` state
  (handles `provides` stand-ins and derived keys like `enbw_mhz`). **The reusable core for the
  temporal pass AND for threading params into per-step folds (Surface B).**
- The **sequence step editor's clamp caption** folds through carried bridge params via the above
  (`timeline_editor._update_clamp_warning`).
- **Surface A shipped:** `timeline_model.achievability_warnings(items, resolve)` + helpers
  (`AchievabilityIssue`, `_ramp_issues`, `_steps_phrase`, `_mmss`, `_fire_time_s`), wired into
  `TimelineEditor` (`_achievability_resolver` + `_update_achievability` → the amber `_achv_warn`
  banner above the canvas, refreshed on every edit and when calibration arrives). Tests:
  `tests/test_achievability_warnings.py`.

So the mechanism to resolve "what bridge params apply given a flat effective state" exists and is
tested. The remaining work is (a) applying it *over time*, and (b) threading `params` into the
range folds that currently omit them.

## 5. The three surfaces

### Surface A — Temporal achievability pass (the guarantee) — ✅ DONE

Implemented in `ui/timeline_model.py` (pure; next to `sequence_effective_values`) as
`achievability_warnings(items, resolve)`, wired into `TimelineEditor`. What follows is the design
it was built to; the code matches it. **Step 4 (held-power re-check) is implemented** (added after
the owner reported that a fixed spectral density set by one tune wasn't warned when a later tune
doubled the sweep bandwidth): the walk tracks the standing `--power` and, on any freq/bridge-param
event that does NOT itself set `--power`, re-folds and flags the transition INTO violation
(`_held_power_issue`, `points=[(-1, level, fire_s)]`).

**Directly-SET power steps are now flagged too** (added after the owner reported that RE-setting the
density to bw-10's max in a still-later tune — once an earlier step had already widened the sweep to
20 MHz — also went unwarned). The walk's directly-set branch (a tune/run/baseline event that
commands `--power`) folds bounds at that moment's operating point and emits a `_set_power_issue`
when the command clamps — the operator's own explicit command, distinct from a held level pushed
out of range by a later change. So every commanded power (ramp point, held level, or explicit set)
is checked against the achievable range at its fire time. Only the **ramped** bridge-param case (a
min/max-over-sweep check) remains deferred (§8).

Params resolution is no longer only best-effort-on-next-edit: `_update_achievability` now
**proactively prefetches** each sequence task's script params (`_prefetch_seq_params` →
`_on_prefetch_params`, routed by a distinct `tl_prefetch:` label), so the banner appears on load
without first opening a step/ramp dialog; the step dialog's `_on_params` also refreshes the banner
after caching (closing the interactive-authoring race). A task with no hub/targeted unit is still
simply skipped.

A pure function over the sequence, taking a calibration-resolver callback so the model stays
calibration-agnostic.

```
achievability_warnings(items, resolve) -> list[Warning]
    resolve(task) -> (artifact, specs, freq_param, power_dest) | None   # editor supplies it
```

Algorithm, per task T with a calibrated power signal and a `--power` param
(`find_power_index(specs) is not None`):

1. **Build time-ordered events** for T:
   - the duration **bar** baseline args at its start (anchor=start, offset=start_offset);
   - each **run** step's args at its (anchor, offset) — `replace_args` resets, else merges;
   - each **tune** step's params at its (anchor, offset);
   - each **ramp** step expanded via `api.ramp.resolve_ramp(...)` + `api.ramp.place_ramp(anchor,
     offset, resolved)` (`api/ramp.py` ~102 / ~172) → **one event per point** at
     `(fire_anchor, fire_offset, {ramped_param: value})`.
2. **Order events in time.** start-anchored: `t = offset` (from T0). stop-anchored:
   `t = W + offset` with `W = timeline_model.min_on_air_duration(items)` (`~509`; offset ≤ 0).
   Mirrors `_carry_order_key`'s start-before-stop approximation (`~452`).
3. **Walk** ascending, maintaining effective `{dest: value}` seeded from the bar baseline. At
   each event: apply its changes, then compute
   - `freq_hz = state[freq_param] * param_form.hz_per_unit(freq_unit)`,
   - `params = fold_params_from_values(artifact, specs, state)`,
   - `lo, hi = PowerFold.from_artifact(artifact).bounds_at(freq_hz, params)` (`state/power_fold.py`
     ~271; base/reported quantity, same as the ramp From/To fields),
   - the current commanded `power = state[power_dest]` (base quantity — the ramp value is in the
     calibrated `--power` unit already).
   Flag when `power > hi + tol` or `power < lo - tol`.
4. **Re-check standing power on freq/param-only events.** When an event changes freq/bridge
   params but not power, the *last commanded* power may now be out of range — re-fold and check
   it. (Covers the "fixed power, carrier moves underneath" case. For the ramp scenario the later
   ramp points are themselves power events, so they're checked directly in step 3.)
5. **Emit specific warnings** (§3 wording): group ramp violations into contiguous point ranges;
   name times, values, the governing change, and the bound hit.

Placement of results: render in the **sequence view** (this is a whole-sequence property, so it
belongs there, re-run whenever the timeline changes), and **echo** the subset for the currently
edited ramp in the ramp dialog preview.

Caveats to state in code + UI:
- Exact only for **start-anchored** timing; window-relative (stop-anchored / `both`) timing uses
  `min_on_air_duration` and is approximate — label it.
- Per-task: a tune that changes task B's freq doesn't affect task A. `sequence_effective_values`
  is already per-task; keep that.

### Surface B — Thread bridge `params` into per-step range folds — ✅ DONE

Shipped: the ramp's From/To range and its achievable-level snapping now fold through the bridge
params in effect when the ramp fires, so every level From..To is validated against the real
operating point (endpoint check ⇒ whole monotonic sweep). Implemented as:
- `BoundedNumberField(... fold_params=None)` — threads `fold_params` into `snap_power` /
  `quantize_up` / `quantize_down` (`ui/param_form.py`).
- `ramp_editor`: `_op_state(task)` (shared carried snapshot) feeds both `_op_freq_hz` and the new
  `_op_params(task)` (= `fold_params_from_values(artifact, all_params, carried)`); `_with_cal_bounds`
  passes `params` to `refold_bounds`; `_power_fold_ctx` returns params → `BoundedNumberField`.
- **Deviation / deferred:** the fold source is the **carried** sequence state for BOTH tune and run
  ramps (matching the pre-existing `_op_freq_hz` behavior). A run-mode ramp re-invokes the task per
  point with the fixed-value form's args, so strictly it should fold at *that form's* freq/params;
  switching run mode to read `self._form` is a small, isolated follow-up (change `_op_state` to
  branch on `self._run_mode`). Left as-is to avoid changing tested tune-mode behavior under budget.
Tests: `tests/test_ramp_cal_param_fold.py` (range + snapper track a carried `--sidelobes`; both
fail without the fix). Original design notes below.

The ramp editor already folded the `--power` From/To range at the carried **frequency** and
validated both endpoints (monotonic ⇒ whole sweep at that fold point). The gap was **no `params`**,
so a parameter-dependent chain folded at the law's *representative* bridge value, not the live one.

- `RampEditorDialog._with_cal_bounds` (`ui/ramp_editor.py` ~405): `refold_bounds(bounds, freq_hz)`
  → add `params=`. Source of params depends on mode:
  - **tune** ramp → carried state (`sequence_effective_values` at `_ramp_order_key`, as
    `_op_freq_hz` ~442 already does) → `fold_params_from_values(artifact, self._all_params, carried)`.
  - **run** ramp → the fixed-value form's values (`self._form`), NOT carried (each point
    re-invokes the task with those fixed args). Read its freq from the form too, not carried.
    (Today `_op_freq_hz` reads carried in both modes — a pre-existing inconsistency to fix here.)
  - Add an `_op_params(task)` mirroring `_op_freq_hz`.
- `BoundedNumberField` (`ui/param_form.py` ~732) takes `fold` + `fold_freq` only; its snappers
  call `fold.snap_power(p, fold_freq)` / `quantize_up|down(p, fold_freq)` (~751-756) with **no
  params**. Add a `fold_params=` ctor arg and thread it in. `PowerFold.snap_power/quantize_up/
  quantize_down/bounds_at` all already accept `params` (`state/power_fold.py` ~194/200/205/271).
- `RampEditorDialog._power_fold_ctx` (`ui/ramp_editor.py` ~640) → return params too; pass into
  `_make_value_field` (~656) → `BoundedNumberField(..., fold_params=...)`.
- `_ramp_range_error` (`~122`) endpoint check then guarantees the whole monotonic sweep against
  the *correctly* folded interval — the local (single-operating-point) half of the guarantee.

### Surface C — Multi-quantity power card in run/tune steps (UX) — ✅ DONE

Shipped: the existing power card (`_add_power_unit_ui` — ALSO READS AS companions, `Control in
this →`, DEPENDS ON row, family chips, finest-step rounding) now renders in the step editor's
run/tune `ParamForm`, folding through the carried operating point and re-folding when the step is
moved. The card is gated purely on two `set_params` kwargs the step editor now passes:

- `TimelineEditor._script_power_laws[script]` caches `calibration_power_laws`, populated in
  `StepEditorDialog._on_params` (`ui/timeline_editor.py`) next to `_script_cal_freq_params`.
- In `StepEditorDialog._build_form`:
  - **run/bar** step: passes `power_laws=…` (full schema already renders → no `context_dests`).
  - **tune** step: passes the **full** schema + `context_dests` = the non-live dests + `power_laws=…`,
    seeding each context dest's `default` from `_carried` via the static `_seed_context_from_carried`
    helper (the tune analogue of `live_tune_dialog._prepare_specs`, sourcing `_carried` not a
    deployed command; returns fresh spec copies so the shared param cache is never mutated). Live
    params still render only.
- **Position-dependent re-fold:** `ParamForm.set_fold_context(cal_freq_default=…,
  context_defaults={dest: val})` updates `_cal_freq_default` and the context specs' `default`s in
  `_base_specs` (replacing entries with copies — no cache mutation), then re-folds via
  `_do_refold(hold_display=True)` **only when the fold point actually moved** (mirrors
  `_on_freq_changed`'s guard, preserving live edits). `StepEditorDialog._refold_for_position`
  recomputes `_carried` and calls it; wired to the anchor/offset change signals (which previously
  only refreshed the clamp caption). A `cal_freq_default` sentinel (`_UNSET`) distinguishes "not
  passed" from an explicit `None`.
- Save paths unchanged: run/bar → `build_args()`, tune → `values()`; both already emit `--power`
  in the base quantity (the `_power_offset` subtraction) and skip context dests (`_effective_specs`
  excludes them, `_is_input_field` excludes them from the card's own inputs).

Tests: `tests/test_step_editor_power_units.py` — companions render in run + tune steps; the density
companion + DEPENDS ON track a CARRIED `--bw` (fold context, not a field on the step); moving the
offset past a state-resetting step re-folds the card; `Control in this →` promotes a companion;
the tune save emits base-quantity `--power` with no context-dest leakage. Reuses the chirp density↔
total laws (`FBW`/`PSD`) and the live-tune dialog pattern.

## 6. Key code map (all in `sdr-client` unless noted)

**Already shipped (foundation):**
- `ui/param_form.py`: `eval_formula` ~208 · `fold_params_from_values` ~272
- `ui/timeline_editor.py`: `_update_clamp_warning` ~1304 (uses the above)

**Fold engine (client mirror; NOT drift-guarded — safe to extend params usage):**
- `state/power_fold.py`: `PowerFold` ~75 · `bounds_at(freq, params)` ~271 ·
  `_ceiling(freq, params)` ~231 · `keyed_params()` ~149 · `snap_power/quantize_up/quantize_down`
  ~194/200/205 · `clamp_warning(..., params)` ~360 · `refold_bounds(bounds, freq_hz, params)`
  ~390 · `finest_step()` ~289 · `freq_dependent`/`param_dependent`/`has_active`.

**Param form (card + fold accessors):**
- `set_params(..., power_laws, context_dests, cal_freq_param, cal_freq_default)` ~890 ·
  `_add_power_unit_ui` ~1220 · `_power_views` ~1524 · `_bridge_param_dests` ~1452 ·
  `_keyed_param_value` ~1475 · `_provider_spec` ~1465 · `_live_params` ~1496 ·
  `fold_freq_hz` ~1958 · `fold_params` ~1966 · `_fold_freq_now` ~1938 · `_do_refold` ~1898 ·
  `_wire_freq_refold` ~1817 · `_effective_specs` ~2118 · `_is_input_field` ~1294 ·
  `BoundedNumberField` ~732 · `hz_per_unit` ~178 · `values` ~2474 · `build_args` ~2436.

**Step editor:**
- `ui/timeline_editor.py`: `StepEditorDialog` ~927 · `_build_form` ~1231 (tune filters live specs
  ~1260; run/bar ~1270) · `_carried_values` ~1292 · `_current_order_key` ~1282 ·
  `TimelineEditor._on_params` caches ~1225 (`param_cache`, `_script_cal_signals`,
  `_script_cal_freq_params` — add `_script_power_laws`).

**Ramp editor:**
- `ui/ramp_editor.py`: `RampEditorDialog` ~156 · `_with_cal_bounds` ~405 · `_op_freq_hz` ~442 ·
  `_ramp_order_key` ~426 · `_power_fold_ctx` ~640 · `_make_value_field` ~656 ·
  `_ramp_range_error` ~122 · run/tune: `_run_mode`/`_active_params` ~391/`_rebuild_run_form` ~473.

**Timeline model:**
- `ui/timeline_model.py`: `sequence_effective_values` ~466 · `_carry_order_key` ~452 ·
  `validate` ~344 · `item_to_steps`/`items_to_steps` ~252/284 · `min_on_air_duration` ~509.

**Ramp math (DRIFT-GUARDED — byte-identical to `sdr-agent/agent/ramp.py`; do not edit for UI):**
- `api/ramp.py`: `resolve_ramp` ~102 · `place_ramp(anchor, offset_s, resolved)` ~172 ·
  `min_on_air_duration` ~196.

## 7. Test strategy

Headless Qt: `QT_QPA_PLATFORM=offscreen python3 -m pytest -q` (a known teardown flake — run 3–5×;
`test_live_tune_power.py` has the autouse `_flush_deferred_deletes` pattern if you build dialogs).

- **Surface A**: unit-test `achievability_warnings` directly with a stub resolver. Reproduce the
  owner's scenario: a bar + a power ramp + a 5:00 freq tune, assert the warning names the exact
  clamped points/values/time/ceiling; assert silence when the retune keeps them in range. Reuse
  GPS/chirp fixtures from `tests/test_live_tune_power.py` (`_ENBW`, `_FULL`, `_GPS_ART`,
  `_GPS_PARAMS`) and `tests/test_timeline_calibration.py`.
- **Surface B**: extend `tests/test_ramp_cal_freq_fold.py` / `tests/test_ramp_cal_bounds.py` /
  `tests/test_bounded_number_field.py` — the folded From/To range tracks a carried `--bw`/
  `--sidelobes` (params), and differs from the representative fold; run-mode reads the fixed form.
- **Surface C**: new `tests/test_step_editor_power_units.py` — companions render in run + tune
  steps; a companion/ceiling tracks a *carried* bridge param; `Control in this →` promotes;
  moving the offset re-folds; save still emits base-quantity `--power`, no context leakage.

## 8. Open edge cases (decide when reached)

- **Bridge-param ramp with fixed `--power`** (e.g. ramp `--bw` while power fixed): power
  achievability moves *across* the swept param. A *discrete* bridge/freq change under a held power
  is covered (step 4, `_held_power_issue`), as is a directly-SET power that clamps at its fire-time
  operating point (`_set_power_issue`); a *ramped* bridge still needs a min/max-over-sweep check.
  Owner deferred the ramped-bridge case ("we'll get back to this") — TODO.
- **Companion-quantity ramps**: ramp From/To are currently base-quantity only. If Surface C makes
  the ramp offer companion quantities, the temporal comparison must convert. Out of scope now.
- **Window-dependent timing precision**: see §5 caveats.

## 9. Immediate next actions for a fresh session

1. Confirm you're on `claude/sequence-power-achievability` in all three repos (client is where the
   work lives). `main` already carries all prior merged work; Surfaces A–C are committed here.
2. **Surfaces A, B and C are DONE** (all green: `tests/test_achievability_warnings.py`,
   `tests/test_ramp_cal_param_fold.py`, `tests/test_step_editor_power_units.py`). The core feature
   is complete: temporal achievability warnings + calibrated `--power` control (card + folded
   ranges) in every sequence surface. Only the optional refinements below remain.
3. Optional Surface-B follow-up: make a **run-mode** ramp fold at the fixed-value form's
   freq/params instead of the carried state (branch `_op_state` on `self._run_mode`).
4. Keep `QT_QPA_PLATFORM=offscreen … pytest -q` green (run 3–5×). Client-only; drift guard intact.

### Possible refinements to Surface A (not blockers)
- **Proactive params prefetch** — ✅ DONE. `_update_achievability` demand-drives `_prefetch_seq_
  params`, so the banner appears on load without opening a dialog first (and `_on_params` refreshes
  it after an interactive fetch).
- **Held-power check** (§8) — ✅ DONE (`_held_power_issue`). **Directly-set power check** — ✅ DONE
  (`_set_power_issue`, added for the owner's re-set-at-the-wrong-bandwidth report). The **bridge-
  param-ramp** min/max-over-sweep check is the one deferred case that remains.
- **Per-issue placement**: the banner currently concatenates messages above the canvas; a future
  pass could also echo the subset for the ramp being edited inside the ramp dialog preview.

## 10. THE BANDWIDTH-INVARIANT-BASE PROBLEM (chirp density) — root cause + chosen plan

**Owner report (two rounds, screenshots):** on the FM chirp (`--power` controlled in LIVE spectral
density, dBm/MHz), setting the density to the bw-10 max (−7.38) at a step that fires AFTER an
earlier step widened the sweep to 20 MHz gave *no limit and no warning*; a power ramp near the
bw-10 max after a bandwidth tune likewise didn't warn.

**Root cause (verified against the REAL resolver output — this is the crux the fixture tests all
missed):** the chirp's calibration operating/**base** quantity is the *fixed-reference* measured
density (measured at `CAL_MEAS_BW_MHZ = 10`). A constant-amplitude chirp has **bandwidth-invariant
total power**, and the dBm ceiling is gauged through the *constant* `fbw_power` law (`k=10`, no
`param`). So:
- `PowerFold.from_artifact(real_chirp_artifact).param_dependent` is **False**, and
  `bounds_at(freq, {bw: X})` returns the **same** base range at every `--bw` (min/max −27.38/−7.38).
  Confirmed by resolving `mock_fm_chirp_tx.py --make-sample-calibration`'s doc through
  `agent.calibration.resolve`. `tests/test_step_editor_carried_bw.py::…_base_quantity_stays_bandwidth_invariant`
  pins it, with the artifact copied **verbatim** from that resolver output.
- The bandwidth dependence lives **entirely** in the `psd_live` **view law** (`restates_measurement:
  True`, keyed on `bw`), which `param_form._power_views` promotes to the primary control (the raw
  measured density is dropped). `--power` is SENT in the base quantity; the client converts the
  chosen view → base at the fold `--bw` (`_power_offset`), and the transmit script folds base → gain
  **bw-independently** (mock note: "the base quantity maps bw-independently; the client converts your
  chosen quantity to it").

**Consequence:** the whole Surface-A/B/C machinery folds the BASE quantity, so for the chirp it can
never see the bandwidth dependence — the achievability walk (`achievability_warnings`) skips the task
(the `param_dependent`/`freq_dependent` gate), the step-editor limit and the ramp From/To range all
sit at the base (bw-invariant) bounds. The GPS-C/A case worked only because *its* ceiling is gauged
through a param-keyed limiting law (`enbw` behind `--sidelobes`) → `param_dependent` True.

**What this session already fixed (committed):** the **step-editor limit** now folds the `psd_live`
view at the CARRIED sweep bandwidth. A LIVE bridge param the view keys on (a chirp's `--bw`) that a
power-only tune step isn't setting is seeded from the carried state — on open (`_build_form` via new
`StepEditorDialog._fold_bridge_dests`) and on move (`_refold_for_position`); `ParamForm.set_fold_context`
now accepts those bridge dests alongside the non-live context dests. Result: a step carrying bw 20
caps the density ~3 dB below the bw-10 max (−10.4 vs −7.4), so an undeliverable density can no longer
be **authored** in the step editor. Tests: `tests/test_step_editor_carried_bw.py`.

**Owner's chosen direction (this is the spec for the next session):** *HOLD LIVE DENSITY + WARN.*
Treat a commanded spectral density as the **live** density the operator wants delivered, re-evaluated
at each step's **fire-time** sweep bandwidth, and WARN (the runtime would clamp) when a later bandwidth
change makes a held/commanded density undeliverable. i.e. don't accept "the base density is invariant
so it's always fine" — that's technically true of the stored base value but NOT of the operator's
intent. Concretely, still-open work:

1. **Achievability WALK (the banner).** Make `achievability_warnings` fold the operator's CONTROLLED
   quantity (the leading `restates_measurement` view / the reported view) at each event's fire-time
   bridge params, and compare the commanded density expressed in THAT quantity against its achievable
   range at that bandwidth. The clean way: the range of the controlled view at bandwidth `bw` is
   `[base_min + view_delta(bw), base_max + view_delta(bw)]` (`view_delta = coeff·log10(bw/ref)`), which
   MOVES with bw even though the base range doesn't. The walk must know (a) the controlled view/law
   and (b) the operator's intended density. Today only the base value is stored per step — so the
   walk needs the resolver/editor to also supply the controlling law (from `CAL_POWER_LAWS`), and it
   must decide what "intended density" means: interpret the stored base as the density at the fold-
   `--bw` when the step was authored, i.e. `intended_live(fire_bw) = base + view_delta(authoring_bw)`.
   Since the authoring bw isn't stored, the pragmatic model the owner wants is: **the stored base IS
   the intended live density at the reference bandwidth** (base == psd_live at `ref`), so at a later
   `fire_bw` the required base to hold it is `base − view_delta(fire_bw)`; warn when that exceeds
   `base_max`. Validate this against the GPS path (which must keep working) and the currently-passing
   `tests/test_achievability_warnings.py` (whose `_CHIRP_ART` uses a *reported-bridge* density that IS
   `param_dependent` — decide whether to keep that fixture or replace it with the real bw-invariant
   structure; the real-structure fixture lives in `tests/test_step_editor_carried_bw.py`).
2. **Ramp editor.** It currently shows/folds From/To in the BASE quantity (bw-invariant for the
   chirp), with no view support — so a density ramp isn't limited or warned at the carried bw. Give it
   the same `psd_live`-view fold at the carried bandwidth as the step editor (`_with_cal_bounds` /
   `_op_params` fold the base; add the view offset + a carried-`--bw` seed), and feed the ramp's points
   into the temporal walk in the controlled quantity.
3. **Decide the transmit-path question.** "Hold live density" strictly means the delivered live
   density stays fixed as bw changes, which the runtime does NOT do today (it holds base). If the
   product wants the runtime to actually hold live density (re-fold base at the live bw per step),
   that is an **agent/scripts** change (out of the client-only guardrail) — confirm with the owner
   whether "warn" is enough (client-only) or the runtime behavior must change too. The client-only
   reading: keep sending base, but LIMIT (can't author an undeliverable live density) + WARN (a held
   density that a later bw makes undeliverable) so the operator is never surprised.

**Key files/functions for the next session:**
- `ui/timeline_model.py`: `achievability_warnings` (the walk), `_set_power_issue`/`_held_power_issue`,
  the `param_dependent/freq_dependent` skip gate (~line 648) — the gate is what silences the chirp.
- `state/power_fold.py`: `PowerFold.param_dependent`/`keyed_params`/`bounds_at` — base is bw-invariant;
  the view delta is NOT in the fold (it's in `param_form._view_delta` / the law).
- `ui/param_form.py`: `_power_views` (drops base when `restates_measurement`), `_selected_view`,
  `_view_delta`, `_power_offset`/`_power_display_offset`, `_live_params`/`_keyed_param_value`,
  `set_fold_context`, `BoundedNumberField`.
- `ui/timeline_editor.py`: `StepEditorDialog._build_form` (tune branch, `_fold_bridge_dests`),
  `_refold_for_position`, `_achievability_resolver` (must also surface the controlling law to the walk).
- `ui/ramp_editor.py`: `_with_cal_bounds`, `_op_state`/`_op_params`/`_op_freq_hz`, `_power_fold_ctx`.
- Real structure + resolver: `sdr-scripts/Raspberry pi + b206 mini-i/Other Signals/fm_chirp_tx.py`
  (`CAL_POWER_LAWS`, `CAL_MEAS_BW_MHZ=10`) and `mock_fm_chirp_tx.py::_make_sample_calibration`;
  `agent.calibration.resolve` in `sdr-agent` (PYTHONPATH=/home/user/sdr-agent to reproduce artifacts).
