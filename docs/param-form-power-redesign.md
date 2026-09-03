# Run/tune power control redesign — handoff

Status: **design mockup done; client implementation NOT started.**
Branch: `claude/sdr-setup-validation-vrdhfu` (sdr-client).

The operator's Run/tune **power** control (the `--power` field on a calibrated task) looks
unfinished: the other power quantities are shown as a run of green `= <value> <unit> · <name>`
text lines under a `control in` dropdown. The redesign gives each quantity its own read-only
display field and makes one primary quantity the clear thing you set.

- **Mockup (locked design):** `docs/param-form-power-mockup.html` (self-contained, interactive).
  Live artifact: https://claude.ai/code/artifact/706b56b0-5c9f-401b-977f-f5615d9607ea
- **Palette/type:** the app's own — `ui/theme.py` (`Palette`, IBM Plex Sans/Mono). The mockup
  copies those hex values so it reads as the real light-themed client.

## The design (what to build)
One **Power** card:
- A prominent **primary control** = the quantity currently being set: quantity name + a
  family-coloured unit chip (slate for absolute `dBm`, teal for a spectral density), a large
  mono editable value with ▲/▼ steppers + unit, a **range rail** with labelled `MIN`/`MAX`
  endpoints, the `Range at <f> MHz · moves with frequency` caption, a clamp warning line, and a
  `LIVE` pill when the field is live-tunable.
- An **"ALSO READS AS"** grid of the *other* quantities, each its own read-only field: quantity
  name, family-coloured unit chip, big mono value, a `● live` marker, and a **`Control in this →`**
  button that promotes it to the primary (the commanded output never changes — only which
  quantity you type in).

## Where it lives in code (`sdr-client/ui/param_form.py`)
The calibrated `--power` field is built by `_field_row` (~line 940): the name label +
`_power_chip_label()` unit chip (already `quantity [unit]`), the input widget, the `RangeRail`
+ `LimitChip` (`_wire_rail`), the "moves with frequency" note, and the `LIVE` badge. When the
spec is the power field it calls **`_add_power_unit_ui`** (~line 996) which appends today's
`control in` dropdown + the green companion `= …` labels. **That method is the main thing to
replace.**

Data model (unchanged — reuse it):
- `_power_views()` → `[{id, name, unit, law}]`: the quantities `--power` can be expressed in.
  `id=None` is the base (measured/reported) quantity; the rest are declared laws.
- `_reported_base()` → `(law_id|None, unit, name)` of the base.
- `_selected_view()` → the view currently controlled (`self._power_view`).
- `_view_delta(view, live_params)` → dB a view adds over the measured value; a companion value =
  `controlled_value + (view_delta(other) − view_delta(selected))` (see `_add_power_unit_ui`).
- `_on_power_view_changed(combo)` / setting `self._power_view` + `_do_refold()` → switch which
  view is controlled, holding the displayed value; `_shift_power_spec` re-labels/re-ranges the
  field into the selected view's unit. `build_args()` always sends the base quantity.
- `_unit_family()` for the chip colour lives in `ui/calibration_panel.py`
  (`_UNIT_FAMILY`/`_unit_family`); either import it or add a tiny local map (`dBm`→abs, `dBm/*`→density).

## Implementation plan
1. **Primary control:** keep the existing `--power` widget + `RangeRail` + `LimitChip` + note +
   `LIVE` from `_field_row`; wrap them in the primary block styling. The chip already shows
   `quantity [unit]` (`_power_chip_label`). Add a small "CONTROLLING" tag.
2. **Companions:** in `_add_power_unit_ui`, drop the `control in` combo and the `= …` QLabels.
   For each non-selected view build a read-only **field widget** (name + family unit chip + mono
   value + `● live` + a `Control in this →` QPushButton). Keep the live-recompute callback
   (`self._pw_companion_update`) but update each field's value label instead of one `= …` label.
3. **Switch control:** the `Control in this →` button calls the existing
   `_on_power_view_changed`-equivalent (set `self._power_view = view_id`; `_do_refold()`).
4. **Family colours:** slate chip for `abs`, teal for `density` — reuse `_unit_family`.
5. **No agent/scripts changes.** Client-only; no capability/version bump.

## Tests to update/add (`tests/test_param_form_power_units.py`)
- `_companions(f)` currently finds labels starting with `=`; the redesign changes that format —
  update the helper to read the new companion field widgets (e.g. by object name / a value
  label), and keep the existing assertions (density↔total tracking `--bw`, unit views, held
  value across a bandwidth change, `Control in this` swaps the controlled quantity keeping the
  sent base value). Add: each non-controlled view renders its own field; `Control in this`
  promotes it.
- The `_power_chip_label` tests (quantity [unit], no dots) still apply to the primary.

## Guardrails
- Keep `QT_QPA_PLATFORM=offscreen python3 -m pytest -q` green (~572 tests).
- Match the compact app style (`ui/theme.py`): 11–12px labels, `Palette.ACCENT`/`TEXT_MUTED`,
  `Fonts.MONO` for values. Don't introduce a new visual language — this is the existing client.
