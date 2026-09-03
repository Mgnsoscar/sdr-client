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

## In progress — Run/tune power control redesign (mockup only, not built)
The operator's `--power` control (calibrated task Run/tune form) is getting a cleaner layout:
one primary quantity you set + each other quantity as its own read-only live display field,
replacing the `control in` dropdown + green `= … · name` lines. Design is locked as a mockup;
client implementation not started. See `docs/param-form-power-redesign.md` (handoff + code map)
and `docs/param-form-power-mockup.html` (the interactive mockup). Lives in
`ui/param_form.py` `_add_power_unit_ui` (+ `_field_row`). Client-only, no agent change.

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
