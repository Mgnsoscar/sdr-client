# Calibration UI redesign — per-signal signal editor (handoff)

Status: **design locked, ready to build Phase 1.** No editor code written yet.
Branch (all three repos): `claude/todo-list-onboarding-86r5pg`.

This doc is a self-contained handoff so a fresh session can continue with no prior context.

---

## 1. Repos, branch, and what's already done this session

Work on branch `claude/todo-list-onboarding-86r5pg` in all three repos. Build on these tips
(fetch/checkout first):

| repo | branch tip to build on |
|------|------------------------|
| `sdr-agent`  | latest of `claude/todo-list-onboarding-86r5pg` |
| `sdr-client` | latest of `claude/todo-list-onboarding-86r5pg` |
| `sdr-scripts`| latest of `claude/todo-list-onboarding-86r5pg` |

Already completed and pushed on this branch (do NOT redo):
- **sdr-scripts**: consolidated the GPS C/A transmit scripts to exactly two, named by chip
  rate, with generic (band-agnostic) calibration IDs and both L1+L2 carrier presets:
  `gps_ca_code_1.023Mcps.py` and `gps_ca_code_10.23Mcps.py`. (The 10.23 script got the full
  spectral-density calibration treatment: max sidelobes = 2, enbw table, `--self-test`.)

The calibration-UI redesign below is the **next** task and has NOT been started in code.

---

## 2. Environment setup (deps + how to run the suites)

The container starts without the Python deps. Install once:

```bash
pip3 install numpy pytest PyQt6 httpx pydantic zeroconf websocket-client PyYAML paramiko \
             fastapi uvicorn "ruamel.yaml" starlette psutil python-multipart inotify-simple
# PyQt6 also needs system GL libs for offscreen tests:
apt-get update -q && apt-get install -y -q libegl1 libgl1 libglib2.0-0t64 libdbus-1-3 libxkbcommon0 libfontconfig1
```

Run the suites (all currently green):
```bash
cd sdr-agent  && python3 -m pytest -q                       # 347 passed
cd sdr-client && QT_QPA_PLATFORM=offscreen python3 -m pytest -q   # 535 passed
# GPS transmit self-tests (need numpy; paramkit is in sdr-agent):
cd "sdr-scripts/Raspberry pi + b206 mini-i/PRN GPS"
PYTHONPATH=/path/to/sdr-agent python3 gps_ca_code_1.023Mcps.py --self-test
```
Some test files (`tests/test_paramkit.py`, `tests/test_argspec_paramkit.py`,
`tests/test_shared_source_drift.py` in sdr-agent) are run directly (`python3 <file>`) OR via
pytest depending on style; the drift guard is pytest-only.

Drift guard: `sdr-agent/agent/argspec.py` and `sdr-client/api/argspec.py` MUST stay
**byte-identical** (`tests/test_shared_source_drift.py`). If you touch either, mirror it.

---

## 3. The task, in one paragraph

The Calibration tab's signal editor currently shows each signal's measured gain→power curve,
but the **Reported** and **Limiting** power-quantity readings are configured **once, globally**,
in a shared block on the operating (last) stage. The user wants readings to be **per-signal**
(different signals have different limiting factors), each signal an expandable panel owning its
own configuration, in a compact layout with the measured points behind a dialog.

Source of the request: a Word doc ("Improvements to calibration UI") + two screenshots, refined
over four mockup iterations (below). The **final** design is in §5; the mockup HTML is committed
alongside this doc: `docs/calibration-signal-editor-mockup.html`
(live artifact: https://claude.ai/code/artifact/99c40a40-77c2-4e44-ad60-1d961487bcbc ).

---

## 4. KEY backend finding — most of this is already supported

The resolver and document schema **already support per-signal reported/limiting readings**. This
redesign is largely **UI surfacing**, not a data-model rewrite. Evidence in
`sdr-agent/agent/calibration.py`:

- `resolve()` reads readings **per-signal first**, with the operating-plane spec only as a
  fallback default — the `_reading(key)` helper (~line 987): `sig.get(key)` then
  `op_spec.get(key)`. Doc paths: `signals.<id>.reported` / `signals.<id>.limiting`.
  Comment there: *"A reading is per-SIGNAL first … the signal entry wins per key."*
- Reading `kind`s already map onto the three limiting options:
  `same` = "Same as measurement", `law` = "Derived", `own` = "Separate measurement"
  (see `_reading_block()` in `sdr-client/ui/calibration_panel.py` ~line 238, and
  `parse_bridge` in `sdr-agent/paramkit/power_law.py`).
- Per-signal own curves already resolve: `_Measured.reported_own` / `limiting_own`
  (`calibration.py` ~line 93).
- The current editor attaches the reading block only to the operating plane
  (`calibration_panel.py` `_render_detail` ~line 2417: `if row is rows[-1]:
  self._reading_editor(row)`), and literally says *"Advanced or per-signal readings: use JSON…"*
  (~line 2687). So the GUI is the gap.

**Genuinely new (needs the agent, Phase 2):** a per-signal **measurement quantity + unit**. Today
the measured curve is assumed absolute power — `_CurveTable(headers=("gain (dB)","power (dBm)"))`
(~line 774) and the quantity is plane-level (`operating_quantity`, shared). Making it per-signal
(one signal `dBm`, another `dBm/MHz`) means the resolver must read the measured value in a
per-signal declared quantity/unit and thread it into the density→dBm law bridges.

Capability gating pattern to follow (constants at top of `calibration_panel.py`, e.g.
`CAL_MEASUREMENT_DEEMBED_CAPABILITY = "calibration-measurement-deembed"` agent ≥ 1.11.0): a new
agent feature advertises a capability flag + bumps `AGENT_VERSION` (in `sdr-agent/agent/config.py`),
and the client gates the UI on it with a "too old" message.

---

## 5. The locked design (final, after 4 iterations)

Each signal is an **expandable/collapsible card** owning its full config. **No stage-level shared
defaults.** Compact layout matching the app's existing look (light theme, teal field labels,
slate/amber section markers).

Per signal, two sections:

**Measurement** (marker slate, tag "WHAT YOU TOOK ON THE ANALYZER")
- `quantity` — a single free-text label (there is **no** separate "description" field).
- `unit` — per-signal: `dBm`, `dBm/Hz`, `dBm/MHz`, `dBm/kHz`, …
- a live **"shows as"** preview: `quantity [unit]` — exactly what the parameter form renders
  above `--power` (e.g. `Full-band power [dBm]`).
- `frequency` (MHz) — already per-signal today; keep it.
- `curve` — measured points open in a **dialog** (table + sparkline), not inline.
- What reaches the parameter form to control `--power` by: the **measurement quantity+unit**
  PLUS any **derived-law** quantities whose law input matches the measurement unit. (This is
  the whole operator-facing story now — see Reported below.)

**Limiting** (marker amber, tag "GAUGED IN dBm") — **always resolves to dBm**
- `follows by`: `Same as measurement` · `Derived (convert → dBm)` · `Separate measurement (dBm)`.
  - `Same as measurement` is only offered/selectable when the measurement is already `dBm`
    (a density can't be a dBm limit). Auto-disable + fall back otherwise.
  - `Derived` offers **only laws whose OUTPUT is dBm** and whose input matches the measurement
    unit. If no such law exists for the unit, the **Derived option is not selectable**
    (disabled).
  - `Separate measurement` = an `own` curve measured in dBm (shares the signal's frequency).
- Shows a `limit reading: <quantity> [dBm]` readout.
- **No ceiling field on the signal.** (removed)

**Reported: removed entirely.** There is no Reported field/override anywhere.

**The ceiling** (dBm cap) is **not** per-signal. It is a single **dBm output limit on the Source
stage**, entered in the **existing stage limits list** (the app already has this — "Maximum power
permitted at that stage boundary", `calibration_panel.py` ~line 1660). It applies to **all
signals**; because every signal's limiting reading is now dBm, one stage limit gauges them all.
**No new UI for this** — the mockup shows a Source-stage-limit block for context ONLY; the real
implementation reuses the existing limits list. The build change is: **remove the per-signal
`max_dbm` cap** from the reading, and rely on the stage limit.

Measurement → Limiting order (this is the order the user wants).

### Iteration history (what each round changed, so intent is traceable)
1. First pass: per-signal cards with Measurement / Limiting / optional Reported; points dialog;
   unit-matched Derived laws; Reported off by default.
2. Smaller type; merged "quantity"+"description" into one `quantity` field; added the live
   "shows as" `quantity [unit]` preview.
3. Dropped Reported entirely; the param form gets measurement quantity+unit + derived-law
   quantities. Limiting laws constrained to **return dBm**; "Same as measurement" disabled for
   density. Removed the per-signal ceiling; ceiling → a Source-stage dBm limit for all signals.
4. Removed the "capped by the stage ceiling" caption; **Derived not selectable when no dBm law**
   exists for the unit; clarified the stage-limit block is illustration only (real one = existing
   limits list).

---

## 6. Build plan

### Phase 1 — client-only, no agent bump (start here)
All in `sdr-client/ui/calibration_panel.py` (~4530 lines). Reuses backend capability that already
exists (§4).
1. Move the reading editor out of the shared operating-plane block (`_render_detail` ~2417) and
   render a per-signal **Measurement + Limiting** panel inside each signal's expandable card,
   bound to the per-signal doc keys `signals.<id>.limiting` (and measurement fields), not
   `rows[-1]["reading"]`.
2. Surface the `own` ("Separate measurement") kind in the GUI (today it's JSON-only per the
   ~line 2687 comment). Its curve shares the signal frequency.
3. Constrain the Limiting **Derived** law list to laws that return dBm (output == absolute dBm),
   input-matched to the measurement unit; disable Derived when none exist; disable "Same as
   measurement" unless the measurement unit is dBm.
4. Move measured points into a modal dialog; collapsed signal = compact summary row.
5. Remove Reported from the editor entirely.
6. Remove the per-signal `max_dbm` ceiling; the dBm cap lives in the existing stage limits list
   (no new UI). Verify the resolver gauges each signal's dBm limiting reading against the stage
   limit (it should, since limiting is now always dBm/absolute — confirm against `calibration.py`).
7. `argspec.py` likely untouched; if it moves, keep it byte-identical in both repos (drift guard).

Ships against today's agent because per-signal `limiting` is already resolved (§4).

### Phase 2 — per-signal measurement quantity/unit (agent + client, gated)
The one genuinely new mechanic. `sdr-agent/agent/calibration.py`: read
`signals.<id>.measurement = {quantity, unit}` and thread the measured quantity/unit into the
bridge resolution so a signal measured in a density feeds the density→dBm laws. Advertise a new
capability flag + bump `AGENT_VERSION` (`agent/config.py`). Client gates the measurement
quantity/unit UI on it. Confirmed with the user: **Derived/limiting laws must return dBm**, and
the offered laws are only those matching the measurement unit — this already matches the script's
`CAL_POWER_LAWS` "in"/"out" fields (e.g. GPS `full_power`: in=density, out=abs).

---

## 7. Open items / things to confirm while building
- Exact doc shape for `signals.<id>.measurement` (Phase 2) — pick keys and update the resolver +
  `_reading_block`-style normalizer together.
- Confirm the stage limits list already applies its dBm cap to a per-signal limiting reading
  without a per-signal cap (read the limit-inversion path in `calibration.py`).
- Collapsed signal summary row content (currently unit chip + point count — user OK'd this).
