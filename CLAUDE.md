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

## Packaging as a standalone no-admin app: NOT STARTED (context ready)
Owner wants an installer to send coworkers who install `sdr-client` as a desktop app, under two hard
constraints: **no admin rights** (per-user install/run only) and **unknown-publisher execution is
often blocked** (SmartScreen/Gatekeeper). Full handoff context — entry point (`main.py`, app name
"SDR Broadcaster Control"), the real runtime deps (`requirements.txt`, NOT the agent-test list),
data files to collect (`ui/assets/fonts/*.ttf`; optional `bundles/sdr-agent-*.tar.gz`), the
load-bearing prerequisite (**relocate writable state off `Path(__file__).parent` to a per-user data
dir** — `units.yaml`, `plans.json`, `schedule.json`, `library.json`, `components.json`, the caches,
`unit_ledger.json`), open decisions to confirm with the owner, and the honest no-admin/unsigned
options — lives in **`docs/packaging-standalone.md`**. Start there.

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
