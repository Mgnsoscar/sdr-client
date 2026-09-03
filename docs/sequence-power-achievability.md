# Sequence power achievability + power control in step/ramp editors

**Status:** PLANNED — design locked, no implementation started yet on this branch.
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

Landed by the clamp-caption fix (commit `c53d432`, now on `main`):

- **`param_form.eval_formula(formula, get_value)`** (`ui/param_form.py` ~208) — source-agnostic
  derived-formula evaluator (center/span/sum/diff/count/span_to/term/extent/linear/table). The
  widget path passes `_source_num`; a dict path passes `dict.get`. Single implementation, so the
  two can't drift.
- **`param_form.fold_params_from_values(artifact, specs, values)`** (`ui/param_form.py` ~272) —
  the **dict-path** resolver for the bridge-keyed `--power` params over a flat `{dest: value}`
  state. Handles `provides` stand-ins and derived keys (e.g. `enbw_mhz` = table lookup on
  `--sidelobes`). Returns `{dest: float}` or None. **This is the reusable core for BOTH the
  temporal pass and threading params into per-step folds.**
- The **sequence step editor's clamp caption** already folds through carried bridge params via
  the above (`timeline_editor._update_clamp_warning`, `ui/timeline_editor.py` ~1304).

So the mechanism to resolve "what bridge params apply given a flat effective state" exists and is
tested. The remaining work is (a) applying it *over time*, and (b) threading `params` into the
range folds that currently omit them.

## 5. The three surfaces

### Surface A — Temporal achievability pass (NEW; the guarantee) — DO FIRST

A pure function over the sequence, taking a calibration-resolver callback so the model stays
calibration-agnostic. Suggested home: `ui/timeline_model.py` (next to `sequence_effective_values`
~466 and `validate` ~344).

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

### Surface B — Thread bridge `params` into per-step range folds (NEEDED for correct live feedback)

The ramp editor already folds the `--power` From/To range at the carried **frequency** and
validates both endpoints (monotonic ⇒ whole sweep at that fold point). The gap: **no `params`**,
so a parameter-dependent chain folds at the law's *representative* bridge value, not the live one.

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

### Surface C — Multi-quantity power card in run/tune steps (UX; do LAST)

Wire the existing power card (`_add_power_unit_ui`, `ui/param_form.py` ~1220 — ALSO READS AS
companions, `Control in this →`, DEPENDS ON row, family chips, finest-step rounding) into the
step editor's `ParamForm`. The card is already gated purely on two `set_params` kwargs the step
editor doesn't pass yet:

- Cache `calibration_power_laws` in `TimelineEditor` (add `_script_power_laws[script]` in
  `_on_params`, `ui/timeline_editor.py` ~1225, next to `_script_cal_freq_params`).
- In `StepEditorDialog._build_form` (`~1231`):
  - **run/bar** step: pass `power_laws=…` (full schema already renders → `context_dests` empty).
  - **tune** step: pass the **full** schema + `context_dests` = non-live dests + `power_laws=…`,
    seeding each context dest's `default` from `_carried` (a `_seed_context_from_carried` helper
    mirroring live-tune's `_prepare_specs`, but sourcing `_carried` instead of a deployed command).
    Live params still render only. See `live_tune_dialog._prepare_specs`/`_maybe_build`
    (`ui/live_tune_dialog.py` ~263/287) for the exact pattern.
- **Position-dependent re-fold:** carried state changes when the step's anchor/offset changes.
  Add a surgical `ParamForm.set_fold_context(cal_freq_default=…, context_defaults={dest: val})`
  that updates `_cal_freq_default`/`_render_freq` + the context specs' defaults in `_base_specs`
  then calls `_do_refold()` (`~1898`, re-folds bounds + companions, preserving live edits). Wire
  the existing anchor/offset change signals (`ui/timeline_editor.py` ~1052-1053) to recompute
  `_carried` and call it.
- Save paths unchanged: run/bar → `build_args()` (`~2436`), tune → `values()` (`~2474`); both
  already emit `--power` in the base quantity (the `_power_offset` subtraction) and skip context
  dests (`_effective_specs` ~2118 excludes them). Add a test asserting no context leakage.

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
  achievability moves *across* the swept param. The temporal pass's step-4 standing-power
  re-check partially covers it if the bridge change is a discrete event; a *ramped* bridge needs
  a min/max-over-sweep check. Owner deferred this ("we'll get back to this") — leave a clear TODO.
- **Companion-quantity ramps**: ramp From/To are currently base-quantity only. If Surface C makes
  the ramp offer companion quantities, the temporal comparison must convert. Out of scope now.
- **Window-dependent timing precision**: see §5 caveats.

## 9. Immediate next actions for a fresh session

1. Confirm you're on `claude/sequence-power-achievability` in all three repos (client is where the
   work lives). `main` already carries all prior merged work.
2. Start with **Surface A**: add `achievability_warnings` to `timeline_model.py` + a stub-resolver
   unit test reproducing §2's scenario. Get the warning wording right first (owner cares about it).
3. Then **Surface B** (thread `params`), then **Surface C** (the card).
4. Keep `QT_QPA_PLATFORM=offscreen … pytest -q` green (run 3–5×). Client-only; drift guard intact.
