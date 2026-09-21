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

## Run it live, headless (real client + agent, no hardware)
Drive the real client against a live local `sdr-agent`, no unit and no monitor — full recipe + gotchas in
**`../sdr-agent/docs/local-integration-run.md`**. Start the agent first (`../sdr-agent/deploy/run_local.sh`,
backgrounded), then:
```bash
bash tools/run_local.sh                                       # client headless (Qt offscreen)
python3 tools/screenshot.py --tab units --out /tmp/units.png  # …or a one-shot PNG (builds its own client)
```
Both write a scratch `units.yaml` into `/tmp/sdr-local/client-data` (never the repo). Two gotchas that
bite: (1) the client DROPS loopback/colon addresses (`config._parse_unit`), so `units.yaml` must point at
a **bare host IP, no port, not `127.*`** — the helpers use `hostname -I`; (2) `SDR_CLIENT_DATA_DIR` is
captured at import (`config.DEFAULT_UNITS_FILE`), so `screenshot.py` sets it BEFORE importing `paths`/
`config`. A connected client shows "clocks: synced ✓" + the unit **online**; `sdr: none` is expected
(no radio). The agent seeds a sample calibration (Source flatness → cable → 0–95 dB attenuator → output
cable) so the unit is calibrated by default — `screenshot.py --tab calibration` drills into the unit's
Calibration panel to render it.

## Current state — live UI: Units tab + unit detail refresh on a pushed task/crash event: COMPLETE (branch `claude/synced-clock`, client-only)
Owner ask (after confirming the SSE stream already exists): make the UI update as things happen instead
of waiting up to a poll cycle. Survey finding: the alert feed, Sequences, Plans and the schedule Timeline
already react to pushed events (`SequenceWebhook`); the one gap was the **Units tab + unit detail task
rows / running-count**, which were fed only by the 3 s fast poll (`on_fast_update`). The agent already
pushes `task_started`/`task_stopped`/`task_restarted` (`TaskEvent`) + `CrashEvent` over the existing SSE
stream (`/events/stream`), and the client already receives them (`DataHub.event_received`) — that view
just didn't listen. Fix (client-only; no agent/wire/capability change; drift-guarded files untouched):
- **`ui/units_tab.py`** — new `UnitsTab.on_event(ev)`: on a `TaskEvent`/`CrashEvent`, map the event's
  advertised `unit_id` to the Fleet's hostname key (`_host_for_unit_id`, iterates `fleet.units()`) and
  call `hub.refresh_now(host)` — the existing scoped one-unit fast poll — so the change shows at once.
  An unattributable event (no matching unit — shouldn't happen) falls back to a full `refresh_now()`.
  This covers the EXTERNALLY-caused changes the poll lagged on: a crash, a task finishing, another
  operator, or a schedule/sequence launching a task. A start/stop from THIS client already updated
  optimistically via `_on_task_action_done`; the 3 s poll stays as the backstop.
- **`ui/main_window.py`** — `_on_event` now also calls `self.units_tab.on_event(ev)` (it already fed the
  alert feed). Sequences/Plans/Timeline keep reacting to sequence events on their own.
Tests: `tests/test_units_live_refresh.py` (a task event refreshes only the matching unit; every lifecycle
kind nudges; a crash refreshes; an unmatched unit_id → full refresh; non-lifecycle / junk events ignored).
Suite 1108 → 1116 offscreen.

## Current state — clock jitter: the seconds flip is pinned to the true boundary: COMPLETE (branch `claude/synced-clock`, client-only)
Owner report: the synced clock kept correct time but "swings" every ~4–5 s vs time.is, then re-aligns.
Cause: the display shows whole seconds but the redraw tick was a free-running `QTimer` at a fixed 200 ms,
not aligned to the real second boundary — so each flip landed 0–200 ms late, and the 200 ms grid beat
against the 1 s grid (nudged by GUI work from the poll / SSE) into a slow visible swing; re-applying the
chip stylesheet (a Qt re-polish) + rebuilding the tooltip 5×/s added churn that delayed the very tick.
Fix (`ui/clock_widget.py`, no behaviour change to the time source): the `_tick` is now **single-shot,
re-armed each time to just after the next whole second** (`_schedule_tick` = ms to the next boundary +
`TICK_GUARD_MS` 20 ms, from `clock.now()`), so the flip self-corrects onto the boundary every second
regardless of timer slop or a momentary hitch; and `_render(full=False)` on a tick updates only the
`HH:MM:SS` text — the sub-line, the source chip's text/stylesheet, and the tooltip are touched only when
they actually change (or on a `full` redraw at init/sync/failure), so a plain tick does no re-polish.
`refresh()` stays the full-redraw entry point (init + `_apply_sample`/`_apply_failure`). Tests:
`tests/test_synced_clock.py` (the tick re-arms to ~the next boundary + guard and stays single-shot;
`stop()` disarms it; a plain tick advances the time with zero chip-stylesheet re-applies + zero tooltip
rebuilds; a real source change still restyles). Suite 1108 → 1116 offscreen (with the live-refresh work).

## Current state — internet-synchronized clock in the top bar (NTP, PC-clock fallback): COMPLETE (branch `claude/synced-clock`, client-only)
Owner ask: a clearly visible, internet-synchronized clock so an operator doesn't keep a `time.is`
tab open; NTP-synced, not the PC clock, and falling back to the PC clock when NTP can't be reached.
Client-only; no agent/scripts/wire/capability change; drift-guarded files untouched.
- **`state/ntp_clock.py`** (new, pure stdlib, no Qt) — SNTP (RFC 4330): `query_ntp(server)` does one
  UDP/123 exchange and `parse_ntp_response` derives the PC clock's OFFSET from true time via the
  four-timestamp formula (rejecting an untrusted reply — wrong mode, leap-alarm `LI==3`, stratum 0
  kiss-o'-death, empty transmit stamp); `sync(servers)` tries `pool.ntp.org` → Google → Cloudflare →
  NIST, first to answer wins. **`SyncedClock`** stores only the offset (never a frozen time), so
  `now()` = `time.time() + offset` advances smoothly between syncs; a failed re-sync KEEPS the last
  good offset (the PC drifts far less than it's likely to be wrong outright). `source` is `"ntp"` or
  `"local"`; `uncertainty_s()` = rtt/2; `status_text()`/`describe()` render the chip + tooltip.
- **`ui/clock_widget.py`** (new) — `SyncedClockWidget` in the top bar: local `HH:MM:SS` (mono) + a
  `ZONE · UTC hh:mm:ss · date` sub-line (UTC read-out suppressed when the PC's own zone is UTC — the
  units + run timestamps are UTC, so it's worth pairing) + a source chip: green **`NTP ✓ ±N ms`**,
  amber **`PC clock`**, muted **`syncing…`**. A daemon-thread `_NtpSyncer` runs `sync()` off the UI
  thread and hands the result back over Qt signals (never blocks the GUI, a failure is a fallback not
  a crash); re-sync every 600 s after success / 60 s after a failure; a click on the chip re-syncs now.
  `autostart=False` keeps the timers + network off for tests.
- **`ui/main_window.py`** — the widget sits in the top bar between the tab buttons and the existing
  unit-vs-PC `clocks:` skew indicator (which is unchanged — that one compares each UNIT's clock to
  this PC for scheduling; this one shows the true wall-clock time).
Tests: `tests/test_synced_clock.py` (the four-timestamp math + every reject case; a real loopback fake
NTP server measures a known offset; the closed-port fallback; first-answer-wins; `SyncedClock` keeps a
good offset through a later failure; the widget shows NTP time + chip, falls back to the PC clock, and
delivers an off-thread sync to the GUI). Suite 1095 → 1108 offscreen. NTP needs outbound UDP/123 (a
locked-down LAN blocks it → the widget shows `PC clock` and keeps retrying, which is the intended
fallback).

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

## Current state — PLAN EDITOR REDESIGN: the sequence editor with one more level (agent 1.35.0, capability `plan-item-anchors`): COMPLETE (branch `claude/system-familiarization-f5mezz`, cross-repo)
Owner ask: make the plan editor look and work like the sequence editor — every unit's sequences on ONE
timeline, each sequence collapsible to a pill or opened to show its steps (colour by task, clear which
sequence/unit a step belongs to), steps edited IN the plan editor, overlapping sequences on a unit never
drawn on top of each other, warm-up/cool-down visible with each sequence's own on-/off-air anchors obvious,
and "anything anchorable to anything" (a sequence to another sequence / task / step of another unit); unit
banners stay visible when scrolling. Holds are deferred (owner). Mockup approved (v2) → **`docs/plan-editor-
mockup.html`** is the visual spec; the anchor lines are drawn by the SEQUENCE EDITOR'S OWN ROUTER.
- **Model (`api/models.py`)** — `PlanItem` gains `id` (stable, `pi-xxxxxxxx`, assigned by the editor; blank
  on old plans) + `on_air_anchor`/`off_air_anchor` (`"plan"` default | `"item"` | `"step"`) with
  `*_anchor_item` (target item id; `""` = self for `"item"` — a FIXED-LENGTH sequence hangs its off-air off
  its own on-air), `*_anchor_edge` (`on`/`off` for plan/item, `start`/`end` for step), `*_anchor_step`, and
  `expanded` (presentation). The two offsets keep their names, now measured from the anchor — defaults
  reproduce the old meaning exactly. `SequenceStep.anchor_item` (non-empty = a step anchored INTO another
  plan item; `anchor_step_id` a step id there or `""` = that item's window edge via `anchor_edge`
  start=on-air/end=off-air). Mirrored on the agent (`agent/models.py`, 1.35.0, `plan-item-anchors`) so the
  plan REPLICA round-trips; an older unit drops the fields → a plan using anchors reads as drifted until OTA.
- **`ui/plan_graph.py`** (pure) — `PlanGraph(items)` resolves every item edge / wire-step edge to `(clock, t)`
  on the plan's ON/OFF clocks (memoised, loop-safe → `None` + `describe_fault`); `extents()` (minute-rounded
  defined regions), `earliest_on_clock_s` (the preflight lead-in), `pack_lanes`, `channel_conflicts`, and
  **`compile_plan(items, t0, t_end)`** → per-item absolute `on_air_at`/`off_air_at` + steps with every
  CROSS-ITEM anchor rewritten to a plain on-air offset of its own sequence (the agent only ever sees one
  sequence); raises `PlanResolveError` (loop / missing target / an on-air timed from the plan's off-air in
  an open-ended arm).
- **`ui/timeline_model.py`** — `RunItem.anchor_item` / `BarItem.start_anchor_item` (+ `anchor_item_of`,
  `is_cross_item`), carried by `items_to_steps`/`steps_to_items`/`TimelineEditor.steps()/set_steps()`;
  `_resolve_step_clocked`/`resolve_step_offsets(_off)`/`validate` take an `ext` `{uid: (clock, offset)}` for
  cross-item sources (the plan stage supplies it on the sequence's own clocks); `_reaches`/`step_anchor_fault`
  stop at a cross-item source. Byte-identical without cross-item anchors.
- **`ui/timeline_editor.py`** — `TimelineEditor.CANVAS_CLS` (a subclass swaps its canvas), the canvas's
  `_ext_bases`, `_paint_connectors` skips cross-item sources (the stage draws them), the anchor makers/
  `_revert_to_root` clear `anchor_item`, `_RowHeader(embedded=True)` (rows only, indented). Drift-guarded
  files untouched.
- **`ui/plan_editor.py`** (rewritten) — `_EmbeddedCanvas(_TimelineCanvas)` (plan-dictated on/off x, rows-only
  paint on top of `stage.paint_under` — the sequence's window tint/guide lines + every cross connector,
  translated to its row; a connect-drag that finds no local target asks the stage for an external one;
  selection/zoom/wheel forwarded), `_EmbeddedEditor(TimelineEditor)` (the full editor "brain" — context,
  calibration, params, dialogs, achievability — with its canvas + an embedded `_RowHeader` taken out and
  hosted on the stage; never shown; Hold authoring off), `_SeqNode`, **`_PlanStage`** (unit bands with sticky
  labels, collapsed pills / expanded slim window bars with offset chips + green/red edge handles, hatched
  warm-up/cool-down ears, the dashed group frame + per-sequence on/off-air guide lines + tint, the plan's
  three windows + ON-AIR/OFF-AIR + axis, cross-sequence connectors via the canvas router
  (`_connector_points`/`_ortho_path`/`_draw_connector_path`/`_draw_connector_head`, obstacles from the rows
  between, an expanded target's edge leaves from the group edge nearest the dependent), drag-to-anchor from
  any handle onto any handle / step edge / guide line (`external_target`, `apply_cross_anchor` — offset = the
  pixel gap, a loop refused), `detach_edge`/fixed-length, sequence body drag (clamped at the plan's anchors),
  channel conflicts (red outline + pill), folding into lanes, dangling refs healed in place,
  context menus), `_PlanHeaderColumn` (sticky; hosts the embedded row headers), `_PlanSheet`,
  `PlanTimelineEditor` (toolbar: Add sequence…, Duration/One-shot/Tune/Ramp on the SELECTED sequence,
  Expand/Collapse all, Fit, zoom; legend; `set_items/items/is_empty/validate`), `PlanItemDialog` (now the
  unit + source + recovery picker; steps are edited on the stage), `PlanEditorDialog` (refuses to save an
  untimeable plan). `PlanBar`/`_bar_from_item`/`_item_from_bar` kept as round-trip shims.
- **Arm paths** — `plans_tab._arm_plan` and `timeline_tab._arm_scheduled` COMPILE the graph first (direct
  arm: `T_end = T0 + duration`, each sequence runs its resolved window — a plain item the whole window as
  before; open-ended: a fixed-length sequence keeps its duration, an off-clock on-air is refused);
  `_finish_arm_preflight` derives the lead-in from `earliest_on_clock_s` and blocks an untimeable plan;
  `_step_anchor_block_lines` ignores cross-item anchors (compiled away).
- **Owner follow-ups (round 2)** — (1) a cross-sequence wire no longer loops back left to the anchor's
  on-air before heading right: `_PlanStage._route_obstacles` drops every intervening obstacle the router could
  only dodge by pushing its drop column back PAST the anchor (a duration bar spanning its sequence's whole
  window, a collapsed pill in between), so the line CROSSES it and leaves straight toward the dependent; a
  genuine obstacle strictly between the two is still routed around. `_routed_connectors()` exposes the routed
  polylines (the painter draws them). (2) Every sequence carries its OWN defined-on-air / relative / defined-
  off-air windows: `PlanGraph.item_times` / `item_windows(item_id)` → `(last on-clock s, first off-clock s)`
  → `_SeqNode.def_end_x` / `bwd_start_x`, painted by `_paint_regions` inside the expanded group frame AND the
  collapsed pill (green up to its last on-clock instant, the hatched relative middle with dashed boundaries,
  red from its first off-clock instant; a fixed-length sequence is all green, an all-off-clock one all red).
  The plan-wide tint/hatch that started at the LAST sequence's relative region is gone; the axis still spans
  the global extents.
- **Owner follow-ups (round 3)** — (1) "each sequence has a great horizontal area, collapsed or not" was
  NOT the hatched windows (the owner likes those; a first reading moved them onto the slim bar and was
  reverted): it was a full-width GREY STRIPE on every sequence-level row (an expanded sequence's header row,
  a collapsed sequence's row) — the stage never painted its own background, so the app's grey ground
  (`Palette.BG`) showed through wherever no embedded canvas (white) covered it. `_PlanStage.paintEvent` now
  fills the stage `SURFACE` first, and `_paint_unit_band` paints the mockup's soft grey gradient fading out
  to the right (anchored at the viewport's left like the sticky label, border above / hairline below) so the
  unit bands stay legible on the white ground. The round-2 windows are unchanged: washed across the expanded
  frame and the collapsed pill by `_paint_regions`. (2) A wire dropping through another wire's ENTRY (its
  chip + arrowhead into the dependent): `_routed_connectors` routes in two passes — every other wire's entry
  run (`pts[-2].x .. x2` on its dependent's row, ±6 px), the stage's cross-sequence wires AND each expanded
  canvas's own step wires (`_TimelineCanvas._routed_connectors()`, factored out of its painter), is an
  obstacle for a wire dropping through that row, so its column lands left of the run instead of on the chip.
  (3) A wire whose ANCHOR is a sequence's on-/off-air LINE (`line_anchor` in `_connector_specs`: a sequence
  edge hung off another sequence's edge, or a step hung off another item's window edge) with a dependent
  too close for the chip's entry run drew the shared router's near-zero-offset WRAP — a stub along the
  group's bottom, a hook back over the line, a drop, then in. A guide line has no body at its exit point, so
  `_PlanStage._line_anchor_route` drops STRAIGHT from the exit point to the dependent's row and runs in (the
  chip rides the drop; `_draw_connector_head` backs the arrow + chip off along the short entry run), or jogs
  to the column with no stub when that column is blocked / the dependent sits behind the line; a far
  dependent keeps the general route (along the exit row, down, in).
  (4) "Is chaining sequences on one unit disallowed?" — no: `pg.channel_conflicts` only flags sequences whose
  TRUE extents (window + every step's lead-in / tail) OVERLAP on one unit, and touching spans are a legal
  chain. Two things were wrong, though: a tune PIN's ±6 px drawing pad leaked into `_SeqNode.span`, so a
  legal chain with exactly the lead-in + tail gap read red (and the verdict changed with zoom) — the span is
  now the pin's instant; and the banner said only "overlapping sequences". `pg.channel_conflict_pairs`
  (the set is derived from it) + `_PlanStage.conflict_message()` name the first clashing pair and its kind:
  "“A” and “B” are on air at the same time" (their windows overlap) or "“B”'s warm-up starts before “A”'s
  cool-down ends … leave ≥ N s between “A” off-air and “B” on-air" (N = tail + lead-in, the agent's arm rule),
  "(+n more)" when several. The demo plan's red is a genuine overlap (Galileo anchored INTO the chirp's window).
  (5) **Stacking is ALLOWED** (owner: "right now the rule should allow stacking of sequences … I know what's
  compatible"; a task-aware rule is the eventual goal). The agent's arm guard A is task-aware from 1.36.0
  (capability `sequence-stacking`, `../sdr-agent/CLAUDE.md`): two overlapping runs on a unit are refused ONLY
  when both LAUNCH the same task (a task runs once); runs launching different tasks, or a tune-only run, may
  stack. The stage mirrors it in `_relayout`: `pg.channel_conflict_pairs` still finds every overlapping pair
  (its docstring now says "overlap" — conflict vs stack is the stage's call), then `_PlanStage._launched_tasks`
  (each node's bars + one-shots → task names) splits them into `_conflict_pairs` (a shared launched task →
  RED, `conflict_message()` names the task: "“A” and “B” both launch “tx” while on air at the same time — a
  task runs once; the unit will refuse the arm" / "…warm-up starts before…cool-down ends and both launch “tx”
  … leave ≥ N s") and `_stacked_pairs` (`stacked()`, AMBER `Palette.ARMED` frame + banner via
  `stacked_message()`: "⚑ Stacked on {unit} — “A” and “B” are on air at the same time · make sure their tasks
  are compatible" / "…fine if their tasks are compatible", plus " · {unit}'s agent refuses stacked runs until
  updated (needs ≥ 1.36.0)" when `_unit_supports_stacking(host)` reads a cached `/info` WITHOUT the
  capability — None (never read / not in the fleet) adds nothing). A stack never blocks the save; the
  tooltip reads "⚑ stacked with another sequence on this unit — their tasks must be compatible" (a conflict:
  "⚠ launches the same task as an overlapping sequence … the unit refuses the arm"). The demo plan's
  Galileo-inside-chirp overlap is now amber (different tasks).
Tests: `tests/test_plan_graph.py` (11), `tests/test_plan_editor_stage.py` (18; a wire clears another wire's
entry run — the naive route is shown to land on it; a pixel test that the windows wash the frame + pill and
no sequence row is a grey stripe while the unit band keeps its wash; a line-anchored wire to a near dependent
drops straight while the shared router would hook, and a far one keeps the general route; a 2 s-gap
chain on one unit is clean, a touching chain names the warm-up / cool-down gap, a nested window reads "at the
same time" and names the task both launch, other units never clash; a different-task overlap and a tune-only
overlap STACK — amber banner + frame, no conflict, the save not blocked; the old-agent note appears only for a
unit whose cached `/info` lacks `sequence-stacking`), `tests/test_plan_canvas_paint.py` (rewritten, 3),
`test_plan_hold_arm.py` (updated: Hold authoring deferred, a Hold survives). Suite 1185 → 1214 offscreen; agent
711 → 714. **Rollout:** OTA units to 1.35.0 before deploying an anchored plan (else the replica drifts) and to
1.36.0 before arming a STACKED plan (an older agent refuses the overlap); `docs/plan-editor-mockup.html` is the spec. Deferred: Holds in the plan editor, plan-level
undo for sequence moves, per-unit online state from a live fleet (bands show the cached state).

## Current state — paramkit MARKER deploy gate (`api/script_markers.py`): a script needing a newer paramkit is refused, not shipped: COMPLETE (branch `claude/system-familiarization-f5mezz`, client-only)
Review finding (HIGH, cross-repo): paramkit ships INSIDE the agent release, so `cw_drift_tx.py`'s new
`number(..., is_elapsed=True)` CRASHES at `build_script()` on every launch on a unit still running agent
≤ 1.31.1 — while the agent's static upload validator and this client's static reader both accept the file
(the old reader ignores an unknown kwarg). Nothing in the deploy path checked the ordering. Now
**`api/script_markers.py`** maps each marker key the static argspec exposes (`is_elapsed` →
`paramkit-is-elapsed`, agent 1.32.0) to the agent capability that proves its paramkit takes the kwarg;
(and `resets_elapsed` → `paramkit-resets-elapsed`, agent 1.33.0 — the `--restart` trigger that resets the
drift's clock; and `is_clock_origin` → `paramkit-clock-origin`, agent 1.34.0 — the ABSOLUTE origin that makes
the RF-fault resume exact, `../sdr-agent/docs/rf-fault-recovery.md` §14j); `AgentClient.upload_script` (the Scripts panel save/upload) and `AgentClient.deploy_library` (the fleet
deploy) call `_check_script_markers` and raise an `AgentError` naming the script, the marker, the
capability and the unit's version ("update the unit's agent first, then deploy the library") BEFORE any
request — the same shape as the `CAL_*`/`SEQUENCE_*` gates. Cached `/info` capabilities are used;
never-read ones are fetched once. A plain script is never gated. Also: the fault dialog's "no pref file"
hint names the pin knob (it said a ≥ 1.31.1 agent always writes the file); the run-form hidden-checkbox
test asserts `isHidden()` (the `isVisible()` form was vacuous on an unshown dialog). Tests:
`tests/test_script_marker_gate.py` (6; the gate test now uses a script carrying all three markers and asserts
each capability is required). Suite 1179 → 1185.

## Current state — argspec mirror: the `is_elapsed` / `resets_elapsed` / `is_clock_origin` param markers (agent 1.32.0 → 1.34.0): COMPLETE (branch `claude/system-familiarization-f5mezz`, mirror-only)
`api/argspec.py` re-mirrored byte-for-byte from `sdr-agent/agent/argspec.py` (drift guard): every param dict
now carries **`is_elapsed`** (plus `resets_elapsed` and `is_clock_origin`, re-mirrored at 1.33.0 / 1.34.0 — the client
reads nothing off them either) (paramkit `Param.is_elapsed` — the ONE parameter of a time-dependent script that
takes the seconds already elapsed on its own timeline; cw_drift's `--elapsed`). The agent bakes it on an
RF-fault restart so the drift resumes at the right point (`../sdr-agent/docs/rf-fault-recovery.md` §14g). The
client reads nothing off it — the param renders as an ordinary launch number field (default 0; an operator
may set it to start part-way). No UI/model change; suite 1179 unchanged.

## Current state — fault diagnosis reads GR's pref-file backend (agent review fix #1, client half): COMPLETE (branch `claude/system-familiarization-f5mezz`, cross-repo with agent 1.31.1)
The agent review (`../sdr-agent/docs/rf-fault-recovery.md` §14f) established — against the upstream GNU Radio
3.8/3.10 sources — that GR selects its vmcircbuf backend from a **pref FILE** (`vmcircbuf_default_factory`
under the task HOME), never from the `GR_CONF_VMCIRCBUF_DEFAULT_FACTORY` env var the P0 pin exported; agent
1.31.1 now writes that file per launch and reports its content as **`FaultSnapshot.vmcircbuf_backend_pref`**
(the EFFECTIVE backend). Client: `api/models.py` mirrors the field (defaulted, skew-safe);
`ui/fault_detail_dialog._diagnosis_rows` keys the "GR buffer backend" row on it — an mmap pref is clean, a
SysV pref is the leaky suspect (whatever the env var says, which is shown as `env: …`), and a MISSING pref
(every pre-1.31.1 snapshot, or an unreadable HOME) is flagged with a "no GR pref file — GR chose its own
backend" hint, because that is exactly the inert-pin condition. Tests: `tests/test_rf_fault_ui.py` (the
healthy case now carries the pref; the env-unset case is suspect without a pref and clean with one; a new
pref-driven test). Client-only; drift-guarded files untouched.

## Current state — library drift fingerprints include the recovery policy + auto-restart flag (review fix #21): COMPLETE (branch `claude/system-familiarization-f5mezz`, client-only)
Review finding (MEDIUM): `state/library_sync.py`'s `_seq_fingerprint` / `_task_fingerprint` — what
`diff_library`/`diff_state` (the Library tab's drift check + reconcile) compare a unit's deployed
definitions against — ignored `Sequence.recovery_policy`/`recovery_mode` (Phase 3) and
`TaskConfig.auto_restart_on_fault`/`max_fault_restarts` (Phase 3b). A POLICY-ONLY library edit (operator-
restart → auto-resync; ticking "Auto-restart on fault") read as "in sync" and never reconciled, and the
reverse (auto → manual) left autonomy ON on the unit. Fixed: both tuples APPEND `(policy or "manual",
mode or "resync")` / `(bool(auto_restart), int(budget))` — the leading elements are byte-identical — with
the defaults read off `model_fields` so an OLD unit that never sends the fields (`m.Library(**json)` /
`parse_tasks_yaml` default them) fingerprints identically to an explicit default: adding the fields makes
no unchanged deployment read drifted. The deploy already sends `library.model_dump()` (both fields) and
the agent persists them, so a policy drift converges on the next deploy. Tests:
`tests/test_library_sync_policy.py` (12: policy-only / mode-only / flag-only / budget-only changes are
drift in BOTH directions; explicit defaults == omitted fields; an old-agent `/library` payload and an
old `tasks.yaml` without the fields are in sync while a real change against them still surfaces; blank
fields normalise; the pre-existing tuple prefix is unchanged; the `diff_state` consumer path). Suite
1166 → 1178 offscreen.

## Current state — RF-fault RECOVERY Phase 3b (client STANDALONE-task auto-restart checkbox): COMPLETE (branch `claude/system-familiarization-f5mezz`, cross-repo with agent 1.31.0)
Client half of the standalone-task auto-restart (`../sdr-agent/docs/rf-fault-recovery.md` §7.1/§14e). The
agent (1.31.0, capability `task-auto-restart`) relaunches a standalone (non-run-owned) task whose
`TaskConfig.auto_restart_on_fault` is set when it RF-faults, budget-limited; the Run… form can override it
per launch via `StartRequest`. This authors the flag, gated on the capability so a task never claims an
auto-restart an older agent would silently drop. Client-only; drift-guarded files untouched. Suite
1151 → 1166 offscreen.
- **`api/models.py`** — `TaskConfig` mirrors `auto_restart_on_fault` (bool=False) + `max_fault_restarts`
  (int=2); `StartRequest` mirrors `auto_restart_on_fault` (`Optional[bool]`=None, the per-launch override).
  All defaulted (skew-safe).
- **`ui/timeline_model.py`** — `TASK_AUTO_RESTART_CAPABILITY = "task-auto-restart"` +
  `task_auto_restart_supported(client)` (capability-only, no version floor).
- **`ui/task_editor.py`** — an **"Auto-restart on fault"** checkbox by Autostart/Restart-on-crash. The
  offline library ALWAYS offers it (a stored definition; the agent is the deploy-time backstop); a live
  unit gates it on the capability from `/info` (`_apply_auto_restart_support`, with `_auto_restart_want`
  from the stored task + `_auto_restart_supported` from `/info` kept apart so the two async loads converge
  in any order). `_on_save` writes the flag **only when the box is enabled** — so editing an unrelated
  field on an unsupported / unreachable (`/info` failed) unit preserves a flag set for capable units
  (a review MEDIUM), rather than clobbering it via `{**_orig_entry, **edited}`.
- **`ui/run_task_dialog.py`** — the same checkbox in the Run… footer, shown only when the unit advertises
  the capability (`_supports_auto_restart` reads the cached `/info`, no network), seeded from the stored
  task, sending `StartRequest.auto_restart_on_fault` on `_on_run` only when supported.
Tests: `tests/test_task_auto_restart_ui.py` (15: model mirror + StartRequest override; the capability
gate; the task-editor checkbox — library / supported / unsupported / edit-seed / edit-on-old-unit / save
round-trip / preserve-on-unsupported / preserve-when-info-fails; the Run-form checkbox — hidden / shown+
seeded / sends the override / None when unsupported). **Adversarial review**: the client clobber (MEDIUM)
fixed + pinned; the agent-side review fixes (persistence, wedge-relaunch concurrency, the armed-run
double-TX gate, the attenuator-positioning launch hook) are in `sdr-agent` 1.31.0. **NEXT — Phase 3b other
half** (cross-repo): the fast-warm IQ cache.

## Current state — RF-fault RECOVERY Phase 3 (client UNATTENDED auto-restart policy + pill): COMPLETE (branch `claude/system-familiarization-f5mezz`, cross-repo with agent 1.30.0)
Client half of the unattended recovery (`../sdr-agent/docs/rf-fault-recovery.md` §7.1/§14d). The agent
(1.30.0, capability `sequence-auto-restart`) auto-fires `restart_run` for a faulted run armed with
`restart_policy="auto"` (budget-limited, breaker trips loudly when exhausted). This authors the policy,
sends it at arm, and surfaces the recovery state. Client-only; drift-guarded files untouched. Suite
1136 → 1149 offscreen. The standalone-task Auto-restart checkbox + fast-warm cache are Phase 3b.
- **`api/models.py`** — mirrors the agent wire FIELD-FOR-FIELD (all defaulted, skew-safe):
  `Sequence`/`CreateSequenceRequest` gain `recovery_policy`/`recovery_mode` (authored); `ArmSequenceRequest`
  gains `restart_policy`/`restart_mode` (the AGENT wire names, sent at arm); `SequenceRun` mirrors
  `restart_policy`/`restart_mode`/`auto_restart_count`/`auto_restart_task`/`auto_restart_healthy_since`;
  `PlanItem` gains `recovery_policy`/`recovery_mode` (`""` = INHERIT its seeded sequence).
- **`state/library_client.py`** — `create_sequence`/`update_sequence` CARRY `recovery_policy`/
  `recovery_mode` (the offline library is the primary plan-authoring surface; dropping them made a plan
  item inheriting from a library sequence resolve to "manual" — a review HIGH, fixed).
- **`ui/timeline_model.py`** — `SEQUENCE_AUTO_RESTART_CAPABILITY` + `sequence_auto_restart_supported(client)`;
  **`resolve_arm_recovery(client, policy, mode)`** DOWNGRADES `"auto"` → `"manual"` when the unit lacks the
  capability (so a run's pill never over-claims autonomy a unit can't perform); **`fault_pill(run)`** decides
  the row pill from the run fields — red **RF FAULT** on a fault (an auto fault that gave up names the attempt
  count in its tooltip), amber **AUTO-RESTART ×n** on a recovered run (`auto_restart_count>0`, no fault; the
  agent's healthy-settle reset zeroes the count ~60 s after recovery, so the amber pill is transient), else
  None.
- **`ui/sequence_editor.py`** — a compact **recovery combo** (`_RECOVERY_CHOICES`: operator-restart /
  auto-resync / auto-replay) in the header; `_recovery_choice`/`_set_recovery` read/seed it; `_on_save`
  writes `recovery_policy`/`recovery_mode` onto the `CreateSequenceRequest`; **`_auto_restart_block()`** is a
  save/Ready-pill gate (like the Hold gates) — an auto policy authored for a UNIT lacking the capability is
  blocked (the Library / a manual policy is never blocked).
- **Arm paths carry the resolved policy** — `sequences_panel._arm_at` (from `seq.recovery_policy`, resolved;
  covers Library + hold-aware); `plans_tab._item_recovery(fleet, client, item)` (item override else INHERIT
  the stored sequence's authored policy, then resolve) wired into `_arm_plan` (both branches); and
  **`timeline_tab._arm_scheduled`** (the schedule — the PRIMARY unattended surface, also via `_arm_scheduled_many`
  "Arm all"). `AgentClient.supports` reads cached `/info` caps (no worker-thread network).
- **`ui/theme.py`** — an amber `auto_restart` status (`Palette.ARMED`); **`_SequenceRow`** (`sequences_panel`)
  + **`_PlanRow`** (`plans_tab`) route their pill through `tlm.fault_pill` (a plan picks the first faulted run,
  else the first recovered run). The Phase-2 Restart button still gates on `run.fault` (a tripped auto run
  shows RF FAULT + Restart).
Tests: `tests/test_auto_restart_ui.py` (15: model defaults + round-trip; the capability gate; the
auto→manual downgrade; the fault/recovery pill decision; the theme; `_arm_at` / `_item_recovery` / the plan
+ schedule arm paths carry the resolved policy; the LibraryClient policy round-trip; the sequence-editor
combo load/save + `_on_save` copies the policy + the save gate blocks an unsupported unit; the row pills).
**Adversarial review** (find→verify, cross-repo): the `LibraryClient` recovery-policy DROP (HIGH) fixed +
pinned; `fault_pill`'s tooltip softened to not over-claim "gave up" (the client can't know the agent's
budget). The agent-side review fixes (the restart-race guard, the RF-safety pre-stop defer, the agent
`Sequence` policy round-trip, the breaker-state resets) are in `sdr-agent` 1.30.0. Suite 1149 → 1151.
**NEXT — Phase 3b** (cross-repo): the standalone-task Auto-restart checkbox + the fast-warm IQ cache.

## Current state — RF-fault RECOVERY Phase 2 (client Restart button + resync/replay): COMPLETE (branch `claude/system-familiarization-f5mezz`, cross-repo with agent 1.29.0)
Client half of RF-fault recovery (`../sdr-agent/docs/rf-fault-recovery.md` §7/§14c). The agent (1.29.0,
capability `sequence-restart`) recovers a faulted run via `POST /sequence-runs/{id}/restart` — relaunch
the faulted task at its crash-time level (RF on) and re-instate the ramp remainder + STOP, on the
original schedule (resync) or shifted later (replay). Client-only; drift-guarded files untouched. Suite
1129 → 1136 offscreen. The UNATTENDED auto-restart trigger + fast-warm cache are Phase 3.
- **`api/models.py`** — `RestartRunRequest{mode='resync'|'replay'}` (mirrors agent `RestartRequest`;
  `restart_at` is server-set so the client omits it).
- **`api/client.py`** — `restart_sequence_run(run_id, request)` POSTs to `/sequence-runs/{id}/restart`.
- **`ui/timeline_model.py`** — `SEQUENCE_RESTART_CAPABILITY = "sequence-restart"` +
  `sequence_restart_supported(client)` (capability-only).
- **`ui/sequences_panel.py`** — a **"Restart"** button on a FAULTED active run row (`_SequenceRow`),
  gated on BOTH `task-rf-health` (the fault must be detectable) AND `sequence-restart` (the agent must
  understand recovery); shown only when `run.fault` is set (the pill already reads RF FAULT). `_on_restart`
  finds the faulted run, poses the **resync/replay** choice (a two-accept-button `QMessageBox`), and
  fires `seq_restart:{host}:{seq}` → `_on_task_done` shows the agent's refusal reason verbatim + refreshes.
- **`ui/plans_tab.py`** — the plan row (`_PlanRow`) gains the **RF-FAULT pill override it was missing**
  (any faulted run in the plan → red pill) plus the same Restart button, `_fault_run_for(plan)`,
  `_on_restart(plan)`, and the `plan_restart` router branch. **KNOWN LIMITATION**: a multi-unit plan
  recovers the FIRST faulted unit's run (single-unit is exact); per-unit `run_id` fan-out is the known
  TODO, mirroring the plan-export run-id TODO.
Tests: `tests/test_restart_ui.py` (the model + wrapper post; the capability gate; the Restart button
visibility on both rows — only a faulted run with both caps, pill red regardless of the cap; the
resync/replay/cancel routing; the plan fault pill + `_fault_run_for`). **NEXT — Phase 3** (cross-repo):
the task Auto-restart-on-fault checkbox + the sequence/plan auto-restart policy (unattended, budget 2)
and the fast-warm IQ cache.

## Current state — RF-fault DETECTION Phase 1 (client alarm + pills + diagnosis): COMPLETE (branch `claude/system-familiarization-f5mezz`, cross-repo with agent 1.28.0)
Client half of the RF-fault detection layer (`../sdr-agent/docs/rf-fault-recovery.md` §5-6/§14b). The
agent (1.28.0, capability `task-rf-health`) detects a dead-but-alive flowgraph (radio silent while the
task reads RUNNING), fires a `TaskHealthEvent` over SSE, stamps `ProcessStatus.health` / `SequenceRun.fault`
on the poll, and auto-drops RF; this surfaces it. All client-only; drift-guarded files untouched. Suite
1108 → 1128 offscreen.
- **`api/models.py`** — mirrors the agent's health axis FIELD-FOR-FIELD (all defaulted, skew-safe):
  `TaskHealth` enum (OK/STALLED/RF_FAULT/UNKNOWN), `ProcessStatus.health`/`health_detail`/`last_output_at`,
  `FaultSnapshot`, `TaskHealthEvent`, `SequenceRun.fault`/`fault_task`/`fault_at`, `sequence_rf_fault`
  webhook type.
- **`webhook/classify.py`** — routes `"task_health"` → `TaskHealthEvent` with an EXPLICIT branch BEFORE
  the generic `"task_"` → `TaskEvent` rule (task_health starts with task_); `sequence_rf_fault` →
  `SequenceWebhook`.
- **`ui/main_window.py`** — implements the alarm stub the design named: `_on_alert` (beep + taskbar
  flash `QApplication.alert`, every alert-level event) and the FAULT-only `_on_fault` (un-minimise +
  raise + activate + a persistent `QSystemTrayIcon` balloon via `_notify_tray`). All guarded — headless
  / no-audio / no-tray is a clean no-op. The `AlertFeed.fault_raised(event)` signal (distinct from
  `alert_raised(str)`) carries the full event; it fires on the GUI thread (`event_received` is already
  marshalled there).
- **`ui/fault_detail_dialog.py`** (new) — the payoff for the agent's snapshot: renders a fault's
  self-diagnosis (`_diagnosis_rows`: GR buffer backend env-vs-compiled, `/dev/shm` used/total%, VMA
  maps vs `vm.max_map_count`, fd limit, HOME, SysV IPC, notes — with the `vmcircbuf` SUSPECTS flagged
  red + a hint) plus the log tail at detection. `_pct` guards div-by-zero; a snapshot-less event still
  builds. Opened by DOUBLE-CLICKING the RF-fault row in the activity feed (the event is stashed on the
  `QListWidgetItem` UserRole).
- **`ui/alert_feed.py`** — a `TaskHealthEvent` is red + alert-level (`_describe`), emits `fault_raised`,
  and its row opens the diagnosis on double-click; `sequence_rf_fault` / `sequence_hold*` verbs added.
- **Fault pills** — a fault OVERRIDES the state pill (red "RF FAULT", `set_status("RF FAULT",
  "rf_fault")`): `ui/unit_detail.py` `_TaskRow.update_status` (off `task.health`; relabels "Log" →
  "Fault log"; resets when health returns to OK), `ui/sequences_panel.py` `_SequenceRow` (off
  `run.fault`), `ui/unit_card.py` `update_tasks` (a fault WINS over a crash on the fleet card's task
  line). `ui/theme.py` — `rf_fault`/`fault` status colour (red, `Palette.CRASH`).
- **`ui/timeline_model.py`** — `TASK_RF_HEALTH_CAPABILITY = "task-rf-health"` + `task_rf_health_supported(client)`
  (capability-only). The Phase-1 pill/alarm render UNCONDITIONALLY (they only reflect data an older
  agent never sends); the gate is reserved for the Phase-2 "Restart" affordance the agent must understand.
Tests: `tests/test_rf_fault_ui.py` (21: models parse + defaults; classify routes task_health not
TaskEvent; alert feed red + fault_raised + double-click diagnosis; the diagnosis-row suspects +
div-by-zero + the leaky-COMPILED-default flag; task/sequence/card pills override + reset; theme; the
capability gate; headless-safe alarm handlers). Suite 1108 → 1129. **Adversarial review** (find→verify):
one LOW finding FIXED — `fault_detail_dialog._diagnosis_rows` flagged a leaky backend on the env value
ALONE, so a sysv_shm COMPILED default with the P0 env pin disabled (env "") showed unflagged; now the
suspect check keys on the EFFECTIVE backend (`env or compiled`), so the leaky compiled default is caught
in exactly the config where sysv is active (regression test added). **NEXT — Phase 2** (cross-repo): the
"Restart" button (gated on `task-rf-health`) + resync/replay.

## Current state — schedule timeline: a countdown pill on the now-line: COMPLETE (branch `claude/system-familiarization-f5mezz`, client-only)
Owner ask: near the red now-line, a countdown (on the same UI-wide clock as the now-line) to the NEXT
scheduled plan, or to the END of a currently-running one — so there's no need to eyeball the gap. Owner
approved the published mockup, chose the **"on the line"** placement (right end), and said the pill needs
NO task-name text (the block the line sits on already names it). All in **`ui/timeline_tab.py`**
`_DayPlanner`; client-only, no agent/scripts/wire/capability change, drift-guarded files untouched.
- **`_countdown_target(now)`** → `("onair", off-air − now)` if a non-reference plan is on air now (the one
  ending soonest), else `("next", on-air − now)` for the soonest upcoming non-reference plan, else None.
  **Reference (note) windows are skipped** — never a target, and being inside one doesn't read as "on air"
  (they aren't transmitted). Computed from the day's laid-out blocks (`start`/`stop`/`reference`), so it's a
  TODAY feature (the only day with a now-line); nothing upcoming today ⇒ no pill.
- **`_paint_countdown(p, now, y, w)`** draws a compact pill riding the RIGHT end of the now-line
  (`_paint_action`-style geometry): a state-coloured left rail + hairline border on `Palette.SURFACE`, a
  hollow **ring** (`Palette.CRASH`) + `STARTS IN` for a pending next, or a filled **dot** (`Palette.ONLINE`)
  + `ENDS IN` for a running plan, then the `mono_font` countdown (`_fmt_countdown`: `M:SS` / `H:MM:SS`,
  clamps negatives). No task name. Bails when the lane is too narrow. Drawn right after the now-line + dot
  so it sits on top.
- **Ticking**: a per-instance **1-second `QTimer`** (`self._sec_timer` → `self.update()`) armed in `set_day`
  only while today is shown (stopped on any other day), so the now-line and its countdown advance smoothly;
  off-tab the widget is hidden so `update()` schedules no paint. Uses the SAME `datetime.now()` as the
  now-line (already the app's clock the unit arm/stop times are set against) — no separate NTP path.
Tests: `tests/test_timeline_countdown.py` (target = running-ends / next-starts / soonest-of-several /
skips references incl. while inside one / none once past the last plan / reference-only day is empty;
`_fmt_countdown` M:SS + H:MM:SS + clamp; the 1 s timer runs on today and stops off it; paint smoke for both
pill states + the no-target no-pill case). Suite 1098 → 1108 offscreen. Verified by a headless
`_DayPlanner.render()` (green "ENDS IN 08:46" dot-pill over the on-air block; hollow-ring "STARTS IN 14:31"
skipping a violet reference to the next plan). Mockup published as an Artifact for the owner's sign-off.

## Current state — schedule timeline: reference (note) entries — external tests we don't transmit for: COMPLETE (branch `claude/system-familiarization-f5mezz`, client-only)
Owner ask: put the WHOLE test-area transmission plan on the schedule, including the ~half of tests the
team does NOT transmit for, so there's no need to keep a separate window open. Such a **reference** entry
carries "only a name, a description, and a time slot", is "visually distinct from the actual plans
assigned to our units", and is "not armable". Client-only — no agent/scripts/capability change;
drift-guarded files untouched.
- **Model** (`api/models.py`): `ScheduledPlan` gains `reference: bool = False` + `description: str = ""`,
  and `plan_id` now defaults to `""` so a reference validates with no plan (`plan_id=""`, `plan=None`).
  Additive/back-compat — an ordinary scheduled plan is `reference=False` and unchanged.
- **Authoring** (`ui/timeline_tab.py`): new **`_ReferenceDialog`** (name + description + start/stop only,
  no plan/unit picker) builds a `ScheduledPlan(reference=True, plan_name=name, description=…, plan_id="")`;
  a **"+ Add reference"** secondary/ghost button (violet) sits beside "+ Add plan" in the header and works
  **regardless of the library** (`_on_add_reference` needs no plans — unlike `_on_add`). `_on_block` routes
  a reference block to `_ReferenceDialog` (edit / Remove); an ordinary entry still opens `_ScheduleDialog`.
- **Distinct + non-armable**: `_resolve` returns `(plan_name, description)` for a reference; `_entry_state`
  returns a new **`"reference"`** state; `_entries_on` stamps `reference=True` and forces `armable=False`;
  `_armable_entries` (hence **Arm all** and its count) excludes references; `_on_arm` early-returns on a
  reference (defensive — no button is ever drawn). `_DayPlanner._TINT["reference"]` is a muted violet
  (`_REF_INK`/`_REF_SOFT` — a deliberately non-status hue, never green/amber/red/accent), painted as a
  **DASHED** outline with **no colour rail** and a small inert **"REFERENCE"** tag where the Arm pill would
  sit (`_paint_ref_badge`, registers no hit rect — the body still edits). The compact day list mirrors it
  (dashed violet row + an inert "REFERENCE" chip); the calendar dot + legend gain a violet "Reference" key
  (`_DOT_COLOR["reference"]`).
Tests: `tests/test_schedule_reference.py` (model round-trip + no-plan validity; the dialog builds/validates/
edits a reference; `_resolve`/`_entry_state`; a future reference is still non-armable; Arm-all ignores
references; `_on_arm` is a no-op; "+ Add reference" works with zero plans; a reference block opens the
reference dialog, not the plan dialog; a reference block registers no Arm hit rect). Suite 1098 offscreen.
Verified by a headless `_DayPlanner.render()` (dashed violet block + REFERENCE tag above the solid,
rail-and-Arm real plans).

## Current state — schedule timeline: vertical zoom (Ctrl+scroll / header −/+ buttons): COMPLETE (branch `claude/system-familiarization-f5mezz`, client-only)
Owner ask: let the schedule day-planner (the vertical day view, hours top→bottom) be zoomed in/out
vertically. All in **`ui/timeline_tab.py`** `_DayPlanner`:
- **`HOUR_PX` is now a per-instance vertical scale** (was a class constant), clamped to
  `[HOUR_PX_MIN=20, HOUR_PX_MAX=220]`. `content_height` casts to int (the scale is now a float).
  Everything that positions time (`_to_y`, `_layout`'s min-block-duration floor, the paint's block
  rects + hour lines) already read `self.HOUR_PX`, so a scale change + re-layout + repaint rescales the
  whole day; block hit-rects recompute in `paintEvent` so clicks stay correct.
- **`set_hour_px(px, keep_dt=, keep_vp_y=)`** re-lays out at the new scale and, when hosted in a scroll
  area (`set_scroll_area`, wired in `_build_timeline_card`), keeps `keep_dt` pinned at viewport-y
  `keep_vp_y` (default: the time at the viewport centre stays centred) so the zoom feels anchored — it
  sets the scrollbar synchronously AND via `QTimer.singleShot(0)` (the pattern the existing
  scroll-to-anchor uses, since the scrollbar range updates after the layout settles). `zoom_by(factor)`
  multiplies. New `_from_y` inverts `_to_y`.
- **Interaction:** `wheelEvent` — **Ctrl+scroll** zooms, anchored on the time under the cursor
  (`1.0015**angleDelta`); a plain wheel falls through to the parent scroll area. Two compact **−/+
  buttons** in the timeline card header (left of "Arm all"), `zoom_by(0.8)` / `zoom_by(1.25)`, anchored
  on the viewport centre.
- Zoom persists across `set_day` refreshes (it never touches `HOUR_PX`). No agent/scripts/wire/capability
  change; drift-guarded files untouched. Tests: `tests/test_timeline_zoom.py` (scale + content-height +
  `_to_y` scale with zoom; clamp to MIN/MAX; `zoom_by`; `_from_y` inverts `_to_y`; zoom survives a
  refresh; the anchor keeps a time at a fixed viewport-y incl. the default viewport-centre anchor;
  Ctrl+wheel zooms in/out, a plain wheel doesn't). Suite +9 offscreen. Verified by a headless
  `_DayPlanner.grab()` render at 52 vs 110 px/hour.

## Current state — schedule tab "Arm all (N)": arm a whole day of plans in one go: COMPLETE (branch `claude/schedule-arm-all`, cross-repo with agent 1.27.3)
Owner ask: four non-overlapping plans in the schedule — arm ALL of them and let them start/stop by
themselves. Scheduled runs already fire at absolute times on the agent; the blockers were (a) the
agent's two arm guards refusing a LATER, disjoint window once an earlier plan was on air / touching
it (fixed in `sdr-agent` 1.27.3, `sequence_runner.arm`: a task on air BECAUSE of an active run is
exempt from "task(s) already running", and the channel span now counts the stop tail after off-air —
see its CLAUDE.md), and (b) the client arming one block per click, each with its own confirm. Client
side (`ui/timeline_tab.py`, no wire/capability change; works against any agent — an older one just
refuses the later arm and it's reported per plan):
- **`_arm_scheduled_many(fleet, jobs)`** (worker) — `jobs = [(entry, plan, start_utc, stop_utc)]`,
  armed in START order (a unit's agent admits each later window against the runs armed before it) via
  the existing `_arm_scheduled` per plan; a whole-plan failure (e.g. a unit unreachable for its clock)
  becomes an error string, a refused sequence stays the per-item error — a refusal never stops the rest.
- **`Arm all (N)`** button in the timeline card header (`_tl_arm_all`, amber, left of Back-to-today):
  `_refresh_planner` relabels/enables it from **`_armable_entries(day)`** = the day's rows that are
  `armable` (plan exists, start in the future) AND `idle` (no active run of theirs — an armed / on-air
  block is left alone), whose plan has items, in start order.
- **`_on_arm_all`** → drops entries whose units aren't in the fleet (listed as Skipped, not fatal),
  ONE preflight over all their units (`tl_preflight_all:<day>`: `clock_skew` + `sequences_all`) →
  **`_finish_preflight_all`**: per entry the step-anchor gate (skipped with the reason) + the Hold
  notice (collected by plan name), ONE confirm listing `• HH:MM → HH:MM   Plan   (n units)` + the skew
  note + Skipped → `tl_arm_all:<day>` → **`_report_arm_all`**: "armed X plan(s)" or a partial box naming
  each failed plan with the agent's reason (a plan with any refused sequence counts as failed; the
  others armed). `_on_task_done` routes the two `_all` ops before the per-entry lookup. The per-block
  path is unchanged; its preflight pieces were factored into `_split_preflight` / `_skew_note` /
  `_HOLD_NOTE` / `_step_anchor_block` and shared.
Tests: `tests/test_schedule_arm_all.py` (worker order + continue past a refusal + whole-plan error;
the button counts only idle not-yet-started entries with a plan and reads "Arm all" disabled on a past
day; a click arms both plans of the day with one confirm in start order at their absolute windows and
the blocks read armed; a refused plan is reported and the rest arm (the refused one stays retryable);
a missing unit is skipped; nothing armable → an info box). Suite 1086 → 1092 offscreen (off `main`;
the plan-canvas paint fix branch adds its 3 on top).
## Current state — plan editor crash on open ("New plan" / "Edit"): FIXED (branch `claude/plan-canvas-paint-fix`, client-only)
Owner report: opening the plan editor crashed the app — `TypeError: _TimelineCanvas._paint_anchor()
missing 1 required positional argument: 'color'` from `ui/plan_editor.py` `_PlanCanvas.paintEvent`.
Root cause: `_PlanCanvas` subclasses the SEQUENCE canvas and paints through its helpers; the
sequence-editor redesign gave `_TimelineCanvas._paint_anchor` a `top` argument (the row band's top,
for the pill) and the plan canvas's own `paintEvent` still called the old five-argument form. PyQt
aborts the process on an unhandled exception inside a virtual override, hence the hard crash. Nothing
in the suite ever RENDERED the plan canvas (its tests covered round-trips + the dialogs' logic), so
the drift stayed green. Fix: the plan canvas passes `top = LANES_TOP − 8` exactly as the base does
(`_paint_bar(p, it)` and the rest of the shared helpers were still in step). Regression net:
`tests/test_plan_canvas_paint.py` renders the plan canvas EMPTY (New plan), with placed bars (Edit),
and the whole `PlanEditorDialog` both ways, capturing paint-time exceptions through a `sys.excepthook`
fixture (PyQt calls a custom hook instead of aborting; the hook keeps only the formatted TEXT — holding
the traceback keeps the failed paintEvent's frame and its still-active `QPainter` alive, which is fatal
for the next render) and failing on any — so a future helper-signature drift between the two canvases
fails here, not in the field. Suite 1086 → 1089 offscreen.

## Current state — right-click context menus (per-kind actions · inline offsets · Tune…/Ramp… at the click · empty-canvas Add…): COMPLETE (branch `claude/context-menu-actions`, client-only)
Owner ask: right-clicking a duration task should offer edit · set the offset from its anchor · tune… ·
ramp… · delete. Implemented in `ui/timeline_editor.py` (`_TimelineCanvas`), client-only — no wire /
agent / capability change; drift-guarded files untouched. Suite 1074 → 1086 offscreen.
- **One shape per kind** (`_context_menu_spec`, pure/tested): a DURATION task → `Edit… · Start offset… ·
  Stop offset… · [Remove anchor] · Tune… · Ramp… · Delete` (never Duplicate — one bar per task, rule E);
  a tune / ramp / one-shot → `Edit… · Offset… · Duplicate · [Remove anchor] · Delete`; the Hold →
  `Edit… · Offset… · Delete`; a window-filling ("both") ramp has no `Offset…` (`_offset_entries`); a
  multi-selection keeps `Delete selected`. `_run_context_action(it, label, x, global_pos)` dispatches.
- **Inline offset entry** (`_OffsetPopup`, a frameless `Popup` QFrame at the cursor — caption + one
  `DurationSpinBox`, Enter applies / Esc or a click elsewhere cancels; no dialog): the caption names
  the reference (`_offset_reference`: `Start — from on-air` / `from resume` / `from <target>'s end`,
  `Stop — from off-air`, `Offset — from the pause (≤ 0)`, `Pause at — from on-air`). `_prompt_offset`
  builds it; `_apply_offset(it, which, value)` commits through the SAME clamps a drag gets
  (`_clamp_tune_offset` + `_clamp_for_dependents` for a tune/ramp, a window-B bar start ≥ 0, the bar's
  `_auto_rf_gate`), one undo step, a no-op pushes none.
- **Tune… / Ramp… on a duration task** (`_add_step_on`): a new step on THAT task, seeded at the time
  under the cursor — `_window_at_x(x)` → the `(anchor, offset)` of the WINDOW clicked (on-air offset in
  the green window + warm-up, resume offset in the post-hold window / 0 on the Hold band, off-air
  offset in the red window + cool-down; the nearer absolute window in the hatched stretch), snapped
  to the 1 s grid, then clamped inside the task by `_seed_item` (extended with `task` / `anchor` /
  `offset`; `add_new` passes them through, a Hold takes an `offset`). The step / ramp editor opens
  pre-filled (task, anchor, offset); Cancel adds nothing.
- **Empty canvas** (`contextMenuEvent` → `_open_canvas_menu` / `_canvas_menu_spec` /
  `_run_canvas_action`): `Add duration task… · one-shot… · tune… · ramp… · [Add Hold]` at the clicked
  time; a tune / ramp takes the task of the ROW under the cursor (`_task_at_y`, a bar or its tune/ramp
  rows), else the default; `Add Hold` only while none exists and Hold authoring is on.
- **Hold-edit (locked)**: a fired step / the Hold band / the running task's ELAPSED stretch (x before
  the resume edge, `_on_post_hold_stretch`) → the lock notice only; the task running THROUGH the Hold
  on its POST-HOLD stretch → `Tune… · Ramp… · Stop offset…` (unselected; Edit / start / Delete stay
  refused — `_can_set_offset` backs the mutation layer, `_seed_item` coerces an on-air/enter seed to
  `hold` while locked); a window-B step keeps its full menu; the empty-canvas menu is offered only on
  the post-hold side, without `Add Hold`.
Tests: `tests/test_context_menu_actions.py` (12: the shape per kind + conditional items; `_window_at_x`
without / with a Hold incl. merged; Tune…/Ramp… seed + clamp + the real dialogs pre-filled; the canvas
menu + `_task_at_y`; right-click routing; `_apply_offset` clamps / undo / refusals; popup captions +
Enter commit; locked-mode menus + refusals). `test_timeline_step_anchor_ui.py` /
`test_hold_edit_locked.py` updated to the new shape (the running task's live stop grip now opens the
restricted menu instead of the notice).

## Current state — Hold-edit: the ELAPSED window is frosted + LOCKED (option A): COMPLETE (branch `claude/step-to-step-anchoring`, client-only)
Owner ask: while a run is HOLDING, the edit dialog should grey out the already-elapsed window and make
it impossible to even try to edit those steps. Mockup `docs/hold-edit-elapsed-mockup.html` (published
Artifact; three treatments) — owner picked **A · Frosted & locked, WITHOUT the fired clock times**.
Client-only (no wire/agent/capability change; drift-guarded files untouched):
- **Canvas mode** (`ui/timeline_editor.py`, `_TimelineCanvas.set_elapsed_locked(True)`): `elapsed_kind(it)`
  says what already happened — `"full"` (every fire at/before the pause: a window-A tune / one-shot /
  ramp incl. an `enter` step — a ramp with a point still to fire after the pause is NOT full, but the
  dialog loads a crossing ramp split so its run-up is), `"start"` (a duration task RUNNING since before
  the pause — its stop is post-hold and stays editable), `"hold"` (the marker: it is NOW), None (window
  B / off-air / a window-B bar). Always None when not locked.
- **Gates.** `_hit` wraps `_hit_free`: a locked part returns `"locked"` (a running bar keeps `bar_stop`) →
  `mousePressEvent` / `mouseDoubleClickEvent` / `contextMenuEvent` show only `_lock_notice` (a
  `QToolTip`: "Already ran…" / "Running since before the Hold…" / "The Hold is now…"), no selection,
  no drag, no editor; the hover cursor is Forbidden. Ctrl+A / the marquee skip locked items; `_drop_target`
  refuses an elapsed edge, a running bar's START edge and the on-air / pause root lines; `_clamp_tune_
  offset` keeps a window-B step ≥ 0 from resume and an off-air step no earlier than the resume edge;
  `_auto_rf_gate` leaves a running task's launch gate alone (only the stop-side auto tune applies);
  `_seed_item` (the `+ Tune/Ramp/One-shot/Duration` buttons) seeds new steps in the POST-HOLD window
  (`anchor="hold"` / `start_anchor="hold"`).
- **Paint.** `_item_colors` → one grey (`ELAPSED_HUE`/`ELAPSED_INK`) for a full item; no edge dots on a
  locked ramp, none on a running bar's start (its elapsed stretch start→enter is greyed, only the stop
  grip stays); tune chips / one-shot names muted; `_paint_elapsed_wash` (after the rows, under the Hold's
  edges) frosts `0..enter_x` with a translucent wash + 135° hairlines and paints the
  `✓ ELAPSED — ran before the Hold · locked` ribbon on the anchor row (elided to `✓ ELAPSED · locked` /
  `✓ ELAPSED`, dropped when it can't clear the ON-AIR pill + Hold tab; `_elapsed_ribbon` for tests); the
  Hold tab reads `⏸ HOLDING` (`_hold_tag`, merged marker too); the ON-AIR pill dims (`_paint_anchor(alpha)`).
  Row header (`_RowHeader`): grey name/swatch + a padlock (`_paint_lock`) and `… · ran` for a full item;
  `runs through the Hold` for the running task. Tooltip prefixes `Locked · already ran before the Hold` /
  `Running · started before the Hold`; the Hold's reads `holding now`.
- **Dialog** (`ui/hold_edit_dialog.py`): `set_elapsed_locked(True)` right after the split load; the banner
  says window A is locked; `TimelineEditor.set_elapsed_locked` swaps the hint (`LOCKED_HINT`, kept by
  `set_tasks`). Backstop: `_window_a_signature()` = the raw canvas items' wire dicts whose anchor isn't
  `hold`/`stop` (incl. the Hold), order-independent, no deploy-time power precompute (so calibration
  arriving later can't change it) — `_accept` refuses when it differs from the one taken at load.
- **`/code-review` follow-ups.** (1) The lock held only per gesture: a press on the running task's LIVE
  stop grip selected the whole bar, after which Delete / Ctrl+D / the right-click menu / the editor's
  Remove acted on it. Now the MUTATION layer refuses what already happened — `_delete_uids` drops
  elapsed uids before cascading, `_duplicate_item` / `edit_item` bail (the editor shows the notice),
  `contextMenuEvent` treats any elapsed-kind item as locked — and a live-grip press on the running bar
  never selects it (`mousePressEvent` clears the selection instead). (2) `_window_a_signature` returned
  `[]` on a probe failure, which two failures would falsely match; it now returns None and `_accept`
  refuses an unsignable window A. (3) `elapsed_kind` re-derived a ramp's point list on every paint /
  hover / header row; it is now cached per geometry rebuild (`_refresh_elapsed` in `_rebuild_geom` +
  `set_elapsed_locked`, `_elapsed_kind_of` does the work, a not-yet-laid-out item is derived on demand).
  The agent-side findings (proceed's off-air landing on the last fire; stop-anchored window-B content
  resolving before resume; PATCH on-air-end dropping hold fires) are fixed in `sdr-agent` 1.27.1.
Tests: `tests/test_hold_edit_locked.py` (15: classification incl. enter / window-B bar / crossing vs.
at-pause ramp / unlocked; hit-testing incl. the live stop grip + the Hold band; press/dbl-click → notice
only; Ctrl+A + marquee; drop targets; drag clamps; the RF gate left alone; post-hold seeding; paint —
grey/hue, ribbon placement, header meta, unlocked has no ribbon; tooltips; the dialog locks on load,
accepts a window-B edit, refuses a window-A change; the running task is untouchable through its stop
grip at the mutation layer; the classification cache follows edits + un/re-locking; an unsignable
window A is refused). Suite 1059 → 1074 offscreen. Verified by a headless render
(`tools/hold_edit_shot.py`, a committed dev harness like `tools/_seqshot.py`).

## Current state — a ramp ACROSS the Hold is PAUSED there and resumes after Proceed: COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo)
Owner question: a ramp could be placed with its middle inside the Hold window, and its hover
duration / end time read wrong. Decision (owner-approved): ALLOW it — the Hold freezes the ramp at
the level it has reached, and it continues after Proceed shifted by the pause's length (design
`docs/sequence-hold-step.md` §5.7; agent `1.27.0`, capability `sequence-hold-ramp-pause` — a ≤1.26
agent silently DELAYED the pause until the ramp finished, hence the gate). Client side:
- **Model** (`ui/timeline_model.py`): `ramp_hold_cross(it, h_off, …)` → the on-air `(start, end)` of a
  window-A ramp that starts at/before the pause and ends after it (hold/enter/'both' ramps never
  cross); `ramp_crosses_hold(items)`; the wire-level `ramp_crosses_hold_steps(steps)` (stored
  SequenceSteps or dicts, for the arm gates); `ramp_level_at_pause(it, h_off)` = the last point fired
  at/before the pause (the level held). Gate `hold_ramp_pause_supported(client)` (cap + ≥ 1.27.0).
- **Canvas** (`ui/timeline_editor.py`): `_resume_shift(it, offset)` now also shifts any on-air-clock
  time PAST the pause by `HOLD_BAND_PX` (via `_place_x`), so a crossing ramp's END lands on the RESUME
  side at `resume_x + (end − h_off)·eff` — the geometry and the axis agree again. `_rebuild_geom` /
  `_live_relayout` stamp `g["cut_x"]` (= `_enter_x`, via `_ramp_cut_x`) for a crossing ramp, and
  `_paint_ramp` draws it as TWO capsule pieces flanking the Hold window — the run-up ending at the
  enter edge (text: range · the ramp's OWN duration) and the remainder starting at the resume edge
  (the slope end-cap) — threaded by a dashed line across the band. `_post_hold_extents` already
  floats off-air past the resumed end. Tooltip (`_absolute_timing_lines`): `starts X after on-air ·
  pauses Y in, holding L · ends Z after resume` (+ the off-air line). Drag: the split is derived, so a
  ramp dragged into / out of the Hold splits / rejoins live.
- **Gates**: `sequence_editor._hold_ramp_pause_block` (save + Ready pill), `sequences_panel.
  _hold_ramp_pause_ok` (hold-aware arm), `plans_tab._arm_hold_aware_plan` — all on
  `ramp_crosses_hold(_steps)`; the schedule/plan collapse path runs the ramp straight through.
- **Crossing is defined by FIRES, not extent** (matches the agent's split): `ramp_hold_cross` /
  `ramp_crosses_hold_steps` require a POINT after the pause (`_ramp_fires_after`); a window-A ramp
  whose last level's hold merely spills past the pause is absorbed by it — `_ramp_edges` draws it
  ending AT the enter edge, `_post_hold_extents` ignores its tail, nothing is split.
- **Edit-while-holding presents the remainder as its OWN post-hold ramp** (owner ask):
  `api/models.split_ramps_at_hold(steps)` (wire-level, via the drift-guarded `api.ramp`) turns a
  crossing ramp into its run-up (a window-A ramp over exactly the points at/before the pause, the
  original `id` kept) + its remainder as a NEW `anchor="hold"` ramp with `offset_s` = the first
  deferred point's time after the pause, start = that level, stop = the original stop, the original
  dwell (steps = points − 1, duration = points × hold), `power_view` carried, `id` cleared; a lone
  point on either side becomes the single tune it is (a run-mode ramp's → the one-shot run). Unedited,
  its fires equal the agent's paused remainder exactly; since the run-up no longer crosses, the agent
  derives no second remainder from the edited window A. `HoldEditDialog` loads
  `split_ramps_at_hold(sequence.steps)` (its banner says so). Needed a conflict-rule refinement:
  `_spans_overlap` now treats a ramp as `[lo, hi)` (its last hold ENDS at hi, so a same-control step at
  exactly hi follows it; two points at one instant / a step at a ramp's start still collide) — the two
  pieces touch at the pause boundary and were falsely flagged as "same time".
Tests: `tests/test_hold_ramp_pause.py` (detection incl. a ramp starting AT the pause; the held level;
wire-level detection on models + dicts; the gate; geometry end-on-resume-side + two capsules that
flank the band; live drag splits/rejoins; tooltip lines; `split_ramps_at_hold` fire-equality / lone
points / non-crossing untouched; the Hold-edit dialog loads split + accepts), `tests/test_step_
conflicts.py` (a step at a ramp's exact end follows it). Suite 1046 → 1059 offscreen; agent 491 → 496.

## Current state — owner-testing round 3 (readouts · tooltips · task-name-free rows · Hold-START anchor · bar anchor handle · RF auto-gating · drag clamps · live band): COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo)
A third Word doc (`Issues_and_wishes_v3`, 9 items). Suite 1019 → 1046 offscreen; agent 485 → 491 (`1.26.0`).
- **#1/#6 Drag readout = the OFFSET only.** `_paint_drag_readout` shows just `±M:SS` (via `_fmt_offset`) for
  every part (pin / ramp body / bar start·stop / Hold) — the canvas already shows WHAT it's measured from
  (the anchor line, the Hold edge, the connector), so no `· on-air` suffix and never "on-air" for a
  step-anchored pin. `_paint_root_anchor_hint` (the selected tie chip `on-air +0:35`) skips its chip while
  a drag is in progress, so the two never double up.
- **#2 Tooltip.** `_tooltip_text` header names the STEP (`<b>Ramp</b> · power −50→−90 · 6 s`, `<b>Tune</b> ·
  bw=12`; a duration task / one-shot keeps its task), a step-anchored item reads `⚓ [its end ]X after|before|
  at the anchor's start|end` (the anchor is visible, so it's not named), then `_absolute_timing_lines`:
  `fires/starts/ends X after on-air` (or `after resume` past a Hold) / `before off-air` / `before the pause`
  — and past a Hold BOTH `after resume` and `before off-air` (off-air floats to `fwd + bwd` s after resume,
  the canvas's post-hold window without its pixel pad; an off-air-anchored step reads both too).
- **#3 No task names on tune/ramp rows.** `_RowHeader._meta` → `bw tune` / `power ramp` (the indent + hue
  say the task); `_paint_ramp` dropped the parent-task badge.
- **#4 The Hold's START edge — `anchor="enter"`** (cross-repo; agent `1.26.0`, capability
  `sequence-hold-enter`). A ramp's END (never a start / a tune) may tie to the LEFT edge of the Hold window
  (the pause's start); a start / a tune ties to the resume edge as before. `offset` is the END's offset from
  the pause (≤ 0 — nothing may reach INTO the pause; `validate()` + `_clamp_tune_offset` enforce it), the
  ramp runs backward from it (`ramp_span` → `("start", h_off+off−dur)..("start", h_off+off)`), window A
  on the on-air clock (`effective_anchor_offset`, `carry_order_key` (0, …), `_ANCHOR_ON_AIR`). Canvas:
  `_root_anchor_at(x, for_end)` returns `"enter"` only for a drag from a ramp's END over the enter edge
  (`"hold"` only for a start over the resume edge); `_make_root_anchor("enter")` ties the END; `_root_x`,
  `_anchor_base_x`, `_def_x`, `_end_tied`, `_paint_root_anchor_hint` (`pause −0:10`), the connect-drag label
  (`the pause`), `_timing_text(side="enter")` → `at pause` / `X · before pause`, `_ramp_end_side_off`.
  Dialogs: StepEditor "hold start (before the pause)" + ramp editor "Hold start (ends at the pause)"
  (offered with a Hold; window-fit check skipped; `_ft_sublabels` "the pause"). Wire: `SequenceStep.anchor`
  accepts `"enter"`; `collapse_hold` compiles it out to `start` at `hold_off + offset` (a ramp by its START:
  minus its resolved duration via `api.ramp`), so the schedule/plan path never sends the anchor. Gates:
  `hold_enter_supported(client)` (cap + agent ≥ 1.26.0) / `uses_hold_enter(items)`; `sequence_editor.
  _hold_enter_block` (save + Ready pill), `sequences_panel._hold_enter_ok` (hold-aware arm), `plans_tab.
  _arm_hold_aware_plan` (direct plan arm).
- **#5 A duration task's START dot is its ANCHOR handle** (`_edge_at` → `(bar, "start")` within
  `BAR_DOT_HIT` = 6 px; `_is_anchor_source` True for bars; `_make_anchor` sets `start_anchor="step"` +
  `start_anchor_step_id/edge` + `start_offset` = the pixel gap; `_make_root_anchor` re-roots a bar on-air /
  at resume). The RESIZE grips sit just inside the capsule (`_hit`: `bar_start` from −HANDLE_HIT to
  HANDLE_W+8 inside; the stop dot stays a resize grip) and are painted as two hairlines at each end
  (`_paint_bar`) so the two are visually distinct.
- **#7 RF auto-gating** (`ui/rf_gate.py`, the client mirror of `paramkit/rf.py` + `gate_tokens` /
  `gate_flag` / `gate_arg_state` / `set_gate_arg`). On a bar drag's RELEASE (`mouseReleaseEvent` →
  `_auto_rf_gate`, in the same undo step): a task whose script declares an RF gate (`is_rf` marker or the
  `--rf` on/off convention, via `TimelineEditor.task_param_specs`) dragged to START BEFORE on-air gets its
  launch args set to the gate's OFF token + a tune turning it ON at on-air `0`; dragged back the tune goes
  and the launch gate is restored ON. STOP PAST off-air adds a gate-OFF tune at off-air `0`; back, it goes.
  The auto tunes are recognised by SHAPE (gate-only tune at the anchor instant) — no marker field, a
  reloaded sequence behaves the same. No gate → no-op.
- **#8 Drag clamps.** `_task_range(it)` bounds a start-/stop-anchored tune or ramp to its task's DRAWN
  span on both ends (a ramp keeps its whole extent inside); `_clamp_tune_offset` applies it (an `enter`
  step is capped at 0). `_clamp_for_dependents(target, offset)` narrows a TARGET's drag so every tune/ramp
  hanging off it (transitively, `_dependents_of`) stays inside ITS task (the dependent's fixed distance
  translates its range onto the target). Wired into the `run_body` / `ramp_body` drags.
- **#9 Live band expansion.** `relayout` split into `_recompute_band` (Hold + step bases + `compute_anchors`
  + the floating Hold band) and placement; `_live_expand()` (called by `_live_move` / `_group_move` on
  every drag move) re-measures the band from the live offsets with ON-AIR PINNED (`_on` kept, `_off =
  _on + band`, the canvas min-width only GROWS), then `_set_hold_edges` + `_rebuild_geom` — so the on-air /
  off-air / Hold windows expand AS you drag. On release `relayout(keep_on=True)` holds on-air where it was
  (`_place(keep_on)` clamps the shift to the range that keeps every item on-canvas) and keeps the grown
  width; `resizeEvent` pins on-air mid-drag too.
Tests: `tests/test_owner_v3.py` (all nine, 27 tests) + `tests/test_timeline_step_anchor_ui.py` (three
expectations updated: the bar start dot is a handle; tooltip wording); agent `tests/test_sequence_hold_enter.py`.
Verified by a headless render (`scratchpad/v3_issues.py`): muted launch + auto RF tunes, an END-tied
pause ramp with its `pause −10s` tie, task-name-free rows/ramps, grip hairlines on the bar.

## Current state — owner-testing round 2 (connector exit sides · END-tied ramps · delete cascade · bar-source connectors): COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo)
A second Word doc (3 issues + the bar-source gap from #6). Suite 1006 → 1019 offscreen; agent 483 → 485.
- **Connector exit side / ramp entry (owner images).** A ramp anchored to a TUNE at +0 s had the tune's
  line exit RIGHT and wrap around; a ramp anchored at a NEGATIVE offset was entered from the right
  THROUGH its own capsule. Router (`_TimelineCanvas._connector_points` + new `_drop_column`,
  `_dep_entry`, `_point_exit_dir`, `_chip_w`, `_dep_offset`): a POINT target (both sides free) now
  exits TOWARD the dependent's drop column (left when the dependent starts at/just after/before the
  pin — no room for the chip on the right; right when it fits), except a pin that is ITSELF a
  dependent keeps the right exit + under-caption duck (option C) for a left-entry dependent. A
  TWO-SIDED dependent (ramp/bar) is always entered from OUTSIDE its body — a start tie from the left,
  an end tie from the right — never through the capsule; the #9 stub route applies only when the
  entry side faces the stub, else the general wrap. `_pin_conn_sides` uses the SAME decision, so a
  pin's caption never sits on a side a line uses. The router honours a given `exit_dir` and wraps
  when the column is on the body side; a point target with a right-entry dependent leaves straight
  toward the column (the old validated route).
- **A ramp dragged by its END is tied by its END (#C).** New `RunItem.anchor_own_edge`
  ("start"|"end", ramps only) + `SequenceStep.anchor_own_edge` on BOTH wire models (agent `1.25.3`
  carries it through; the runtime never reads it). An end-tied ramp's END sits at `target edge +
  offset` and the ramp runs BACKWARD from it; `offset` is the END's. The wire `offset_s` is ALWAYS
  the START's offset (`step_wire_offset` = offset − duration), so the agent places it unchanged — an
  older agent drops the field and the ramp reloads start-tied at identical timing. Model:
  `own_edge_shift`, `step_wire_offset`, `_ramp_item_offset` (load), `is_step_source`/`step_source_ref`
  (a run via `anchor_*`, a bar via `start_anchor_*` — used everywhere a step source is detected).
  Canvas: `_make_anchor(…, from_edge)` ties the grabbed edge (offset = its pixel gap), the connector
  enters at the END from the right (at the capsule's VISUAL right edge — a short ramp is padded to
  `RAMP_MIN_W`, so the true stop x sits inside it), `_edge_linked` fills the tied dot, tooltip says
  "its end", detach/`_reanchor_deps` go through `_revert_to_root` (on-air at the resolved START, or
  OFF-AIR at the off-air base for an off-air-rooted chain; resets the tie). Ramp editor: a **Tie**
  picker ("the ramp's start" / "the ramp's end") under Anchor to / Relative to, relabels the offset
  row, `_ft_sublabels` reads "ramp start" / "the step ±X". `TimelineEditor.steps()/set_steps()` +
  `sequence_editor._step_anchor_block` (negative check on the WIRE offset) carry it.
- **Deleting a duration task deletes its tunes/ramps (#D).** `_delete_uids` (shared by
  `_delete_with_reanchor`, `_delete_selection`, the editor's Remove button via `edit_item`) expands a
  bar to its task's tunes/ramps (`_cascade_uids`; one-shots of the task are independent launches and
  stay), re-roots every surviving dependent of a deleted item at its fire time, ONE undo step.
- **Bar-source connectors.** A bar whose START hangs off a step (#6) drew no connector and had no
  Remove-anchor: `_paint_connectors`, `_is_anchor_target`, `_pin_conn_sides`, `_edge_linked`,
  `_reanchor_deps`, `_detach_anchor`, `_context_menu_spec`, `_tooltip_text`, `_anchor_base_x` (now the
  target's DRAWN edge — clock-agnostic), the `bar_start` drag, `_group_move`/`_live_move`, and
  `_paint_root_anchor_hint` all key on `tlm.is_step_source`.
Tests: `tests/test_timeline_step_anchor.py` (end-tied resolve/wire/helpers), `tests/test_timeline_
step_anchor_ui.py` (exit-left for a +0 / negative ramp, far dependent still exits right, end-tie
drag keeps the end in place + connector from the right + detach, steps() round-trip, ramp-editor
Tie picker, delete cascade single + selection, bar-source connector/detach), `tests/test_step_editor_
carried_bw.py` (a near dependent → left exit, caption stays right); agent `tests/test_sequence_step_
anchor.py` (field round-trips/defaults; an end-tied ramp fires exactly like a start-tied one). Verified
live: the owner's two layouts + an end-tied ramp + a bar source render as asked (`validate` clean).

## Current state — sequence-editor owner-testing fixes (all 13 done): COMPLETE (branch `claude/step-to-step-anchoring`, client-only)
Owner testing surfaced 13 issues (a 10-issue Word doc + 3 follow-ups). All shipped, client-only;
drift-guarded files untouched. Suite 976 → 1006 offscreen. The final anchoring-expansion
(off-air/bar targets + bar sources + ramp-end drag) is at the bottom of this note.
- **#2 Ramps drag to MOVE (never resize).** A ramp body is now a `ramp_body` drag part that shifts the
  ramp's `offset` (its duration is fixed; a window-filling "both" ramp has no free offset and stays put);
  its start/end dots remain anchor handles. `_live_relayout` now updates a ramp's `start_x`/`stop_x`
  (previously only bars + pins). `mousePressEvent` stores `off0`.
- **#5 Step-anchor dependents move in REAL TIME.** New `_live_move(it)` recomputes `_step_bases` from the
  live offsets and re-places every step-anchored dependent (and chains) as a target is dragged (and a
  dragged dependent now tracks the cursor too) — not just on release. Wired into the single drag +
  `_group_move`.
- **#3 A ramp END-handle drag gets a NOTICE, not silence.** Dragging from a ramp's end dot (which can't
  begin an anchor — a ramp is positioned by its start) shows a brief non-interrupting `QToolTip`
  (`_edge_notice`, naming the already-anchored start side when anchored).
- **#8 Validity pill "Needs a fix" → "Needs correction"** (`sequence_editor._revalidate`).
- **#4 A step-anchored dependent that resolves BEFORE its task's on-air start is blocked.** `validate()`
  Rule A now also checks a step-anchored tune/ramp at its RESOLVED on-air time (via the anchor chain);
  before-on-air → rejected with a clear message (Save/arm blocked). Only the on-air lower bound is
  checkable at authoring (off-air floats); an off-air-rooted chain stays the agent's runtime check.
- **#10 Drag-anchor to the ROOT lines + a selected tie.** `_drop_target` returns `("__root__",
  "start"|"stop"|"hold")` over the on-air/off-air/Hold-resume line (`_root_anchor_at`/`ROOT_SNAP`);
  `_make_root_anchor` sets that anchor keeping the item in place. A SELECTED root-anchored step draws a
  discreet dashed tie to its anchor line + an "on-air/off-air/resume +M:SS" chip
  (`_paint_root_anchor_hint`, selected-only so the default view stays uncluttered).
- **#9 A two-sided target's connector exits AWAY from its body.** `_connector_points` gained a
  `two_sided` flag: for a ramp/bar target whose dependent sits on the BODY side (an end edge with the
  dependent to its left, a start edge with it to the right — between the two sides), it exits a stub the
  other way first (end→right, start→left), drops to the dependent's row, and runs in — never behind the
  bar. Entry stays horizontal (arrow/chip placement unchanged).
- **#11 A tune/one-shot pin drags by DELTA, not to the cursor.** `run_body` is now delta-based like the
  bar/ramp body (grabbing the caption beside the dot no longer jumps the dot under the mouse).
Tests: `tests/test_timeline_step_anchor_ui.py` (ramp body moves without resizing; `_live_move`
repositions a dependent + a chain; the ramp-end notice; root drop targets + `_make_root_anchor` + selected
hint; #9 two-sided exit; #11 delta drag), `tests/test_timeline_step_anchor.py` (#4 before-on-air block).
Suite → 1000 offscreen.
**Bar-anchoring MODEL groundwork** (`tests/test_timeline_step_anchor.py`): `BarItem` gains
`step_id` + `start_anchor="step"`/`start_anchor_step_id`/`start_anchor_edge`; a bar flattens to two wire
steps with distinct ids (start = the bar id, stop = id + `BAR_STOP_SUFFIX`) so a dependent can anchor to
either edge; `items_to_steps`/`steps_to_items` encode/decode (`_encode_anchor_ref`/`_decode_anchor_ref`),
`resolve_step_offsets`/`_item_edge_offset`/`step_edge_offset`/`bar_start_placement`/`compute_anchors` resolve
a bar as source + target. Backward-compatible (a plain sequence's wire is byte-identical).

**Off-air-clock resolution (the shared groundwork for #1/#6/#7/#12/#13).** A step-anchored item now
resolves to a **(clock, offset)** — `_resolve_step_clocked` returns `'start'` (on-air) or `'stop'`
(off-air), so a chain rooted at a stop step / a bar's off-air stop edge draws relative to off-air (its
absolute time set at arm). `resolve_step_offsets` (on-air) is unchanged for its ~15 float consumers;
a parallel `resolve_step_offsets_off` (off-air) feeds `effective_anchor_offset`/`ramp_span`/
`bar_start_placement` an `off_bases` dict, threaded through the canvas as `self._step_off_bases`.
`eligible_step_targets` now admits off-air steps AND bars (start/stop edges), excluding only self /
the Hold / a window-filling 'both' ramp; `validate()` accepts a chain resolving on EITHER clock.

**Canvas + dialog wiring (the anchoring-expansion feature).** Client-only; no agent/scripts/capability
change; drift-guarded files untouched.
- **Off-air + bar edges as drag targets (#1/#7/#12).** `_make_anchor` computes the drop offset from
  the canvas GEOMETRY (the pixel gap between the source's start and the target edge, snapped) instead
  of `step_drop_offset`, so a drop keeps the source in place whatever CLOCK the target sits on and works
  for a bar's start/stop edge as readily as a point/ramp. New `_drop_edge_at` offers a bar's start/stop
  dot as a DROP target (its dots are resize handles → bars stay out of `_edge_at`; a step still anchors
  TO them). The connect-drag readout is geometry-based too.
- **Drag-anchor from a ramp's END dot (#13).** The mousePress gate begins a connect-drag from EITHER
  ramp edge (the ramp is still positioned by its start); the rubber-band starts from the grabbed edge
  (`from_edge`). The old #3 edge-end "notice" is superseded (`_edge_notice` removed).
- **A bar's START hung off another step (#6 — "only the start anchors"; the stop stays off-air).** The
  step editor offers "after another step…" in the bar's Start-anchor picker when an eligible target
  exists (hidden with a Hold), reuses the shared target/edge pickers (`_sync_start_anchor` reveals them),
  and `_accept` persists `start_anchor_step_id`/`start_anchor_edge` (and now preserves the bar's own
  `step_id` so editing it doesn't orphan its dependents). The save/arm gate `_step_anchor_block`
  (`sequence_editor`) now detects a bar source (`start_anchor="step"`, offset = `start_offset`).
Tests: `tests/test_timeline_step_anchor_ui.py` (bar-edge / off-air / ramp-end drops keep the source in
place via the geometry gap; `_drop_edge_at`/`_drop_target` find a bar edge + an off-air tune; the
ramp-end press begins a connect-drag; the bar Start-anchor picker offers + saves a start step anchor).
Verified live: a scene with a ramp-end dependent, an off-air-tune dependent, a probe dropped on a bar's
stop edge, and a bar whose start hangs off a lead tune all resolve + draw correctly (`validate` clean).

## Current state — Hold step rendered as a WINDOW + a forward-from-resume post-hold axis: COMPLETE (branch `claude/step-to-step-anchoring`, client-only)
Owner ask (mockup `docs/sequence-hold-step-mockup.html`, published Artifact): present the Hold not as a
single divider but as a **two-edged tinted WINDOW** (like the relative band), make a task that runs
THROUGH the Hold stay visible (not hidden behind it), and make the whole region after the Hold **one
off-air-styled window** whose time axis counts **forward from resume** (resume is a Hold's fixed T0),
with off-air **floating** to Proceed and MERGING with the resume edge when nothing follows. Implemented
in **`ui/timeline_editor.py`** (`_TimelineCanvas`), fully gated on `has_hold` so the non-hold path +
`_PlanCanvas` are byte-identical; **no model/agent/scripts/capability change** (drift-guarded files
untouched). Only the STEP visuals (pins/chips/ramps/anchoring) were left as-is per the owner — this is
the Hold-rendering + window/axis handling only.
- **Fixed-width Hold window.** New `HOLD_BAND_PX = 48`: with a Hold present, a constant-width hatched +
  amber band is inserted at the hold (`enter_x` = the hold's on-air x, `resume_x = enter_x + HOLD_BAND_PX`),
  and resume-side content is shifted right of it. `_place_x(it, anchor, offset)` adds the band to a
  resume-side START x (`_resume_shift`: HOLD_BAND for a window-B `anchor="hold"` step/ramp, a window-B bar,
  or a step-anchored item resolved past the hold; 0 for the Hold marker + window-A + off-air content, which
  rides `self._off`). Routed through `_run_cx`/`_item_left`/`_span`/`_place`/`_live_relayout`.
- **Off-air FLOATS + MERGES.** `relayout` overrides `_c_off` for the hold case: `off_x = resume_x +
  (fwd+bwd)·eff + POST_HOLD_PAD` where `_post_hold_extents()` returns the furthest resume-forward
  (window-B) and off-air-backward times; **0/0 ⇒ off_x == resume_x** (the merged case, `_hold_merged`).
  The bar's stop (off-air) rides the floated `off_x`, so a duration task runs visibly THROUGH the Hold.
- **Paint** (`_paint_hold_windows`): green on-air (`on_x..enter_x`), hatch+amber Hold (`enter_x..resume_x`),
  red off-air-styled post-hold (`resume_x..off_x`); `_paint_hold_axis` draws on-air ticks forward from
  on-air then post-hold ticks **forward from resume** (0 at resume); `_paint_hold_gridlines` matches;
  `_paint_floating_offair` is a DASHED red OFF-AIR + "floats". `_paint_hold` now draws the two dashed band
  edges + a centred ⏸ HOLD tab ON TOP of the bar (so a held bar stays visible), or — when merged — a
  combined **⏸ HOLD │ OFF-AIR** marker (`_paint_merged_marker`) so the two labels never collide.
- **Interaction.** `_anchor_base_x` for a window-B (`anchor="hold"`) item / a past-hold step target now
  returns the **resume edge** (offset measured forward from resume, where the axis reads 0); the window-B
  bar-start drag + drag snapping (`_snap_targets` gains `resume_x`) match. `_hit` grabs the Hold anywhere
  across the band (enter→resume). Tests: `tests/test_timeline_hold_phase2.py` (fixed-width window +
  floating off-air, merge-when-empty, window-B sits right of the band, bar runs through the hold, band-wide
  hit, non-hold geometry unchanged, full + merged paint; the window-B drag test updated to the resume-edge
  contract). Suite 976 → 983 offscreen.

## Current state — timeline axis: separate on-air / relative / off-air windows: COMPLETE (branch `claude/step-to-step-anchoring`, client-only)
Owner ask: the RELATIVE band should be ONLY where time is actually relative. A STOP-anchored (off-air)
step with a negative offset fires at a FIXED offset before off-air (its time IS absolute), so it belongs
in an ABSOLUTE off-air window — tinted RED (mirroring the green on-air window) with real ticks — and the
hatched relative band (length set at arm) should carry NO ticks. Fix (`ui/timeline_editor.py`, paint-only),
splitting the canvas into THREE regions:
- **`_off_def_x()`** (new, mirrors `_def_x()`): the LEFTMOST off-air x pinned by a stop-anchored step
  (a point's `cx`, a ramp's `start_x` = its `offset − dur`); `off_x` when nothing is off-air-anchored (a
  bar's own stop is off-air itself, not content — bars ignored). `paintEvent` computes
  `off_def_x = max(def_x, _off_def_x())`.
- **Three windows** in `paintEvent`: on-air ABSOLUTE `on_x..def_x` (green tint, `Palette.ONLINE` α11),
  the truly-RELATIVE middle `def_x..off_def_x` (diagonal hatch, dashed boundary at BOTH edges, the
  "relative — length set at arm" badge centred on it), and off-air ABSOLUTE `off_def_x..off_x` (red tint,
  `Palette.CRASH` α11).
- **`_paint_axis`** (gained `off_def_x`): the off-air-relative `−M:SS` ticks now stop at `off_def_x`
  (`off_x - t·eff >= off_def_x - 1`) — they fill the red window, NOT the relative band. On-air ticks/
  warm-up/cool-down unchanged.
- **`_paint_gridlines`** (gained `off_def_x`): off-air-relative gridlines likewise bounded to
  `off_def_x`, so the relative middle stays clear.
Backward-compatible: with no stop-anchored step `off_def_x == off_x`, so the red window vanishes and the
hatch runs `def_x..off_x` exactly as before (no off-air ticks). No geometry/model/serialisation change;
drift-guarded files untouched. Tests: `tests/test_timeline_step_anchor_ui.py::
test_paint_gridlines_cover_both_windows_not_the_relative_band` (a stop-anchored −60 s tune opens the
off-air window; gridlines fill both absolute windows, the relative middle stays clear, all vertical).
Suite still 976 offscreen.
**Relative band: no label at all.** Owner: the "relative — length set at arm" text pill cluttered the
band (and a trial SPRING motif was rejected). Both removed — `_paint_rel_badge` is deleted and nothing is
drawn in the relative band beyond the existing diagonal hatch + the dashed boundary at each edge. The
three-region contrast (green ticked window / hatched gap / red ticked window) carries the meaning on its
own. Paint-only; still 976.

## Current state — timeline canvas: discreet vertical gridlines: COMPLETE (branch `claude/step-to-step-anchoring`, client-only)
Owner ask: discreet but informative gridlines. New **`_TimelineCanvas._paint_gridlines`** (`ui/timeline_editor.py`),
called from `paintEvent` right after the on-air tint + hatch and BEFORE the anchors/axis/rows (so it
sits behind everything). Faint vertical lines (`Palette.BORDER`, alpha 150 major / 70 minor) span the
row band `top..baseline`, aligned to the axis's MAJOR ticks (`_paint_axis`'s own loops: defined region,
warm-up, cool-down) with fainter half-tick minors in the defined region only. Drawn only where time is
REAL — the on-air anchor (t=0) and off-air get their own strong lines and are skipped, and the hatched
"relative" band (`def_x..off_x`, length set at arm) is left clear. Antialiasing off for crisp 1-px
hairlines (save/restore around it). Colour `Palette.BORDER_STRONG` (NOT the lighter `BORDER`) at
alpha 175 major / 95 minor: pixel-measured, the light `BORDER` washed out under the green on-air tint
(contrast ~6 major / ~3 minor → minors invisible, and the low contrast is what made a phone downscaler
drop some lines and thicken others — the reported "double lines"); `BORDER_STRONG` restores a clear
split (~18 major / ~10 minor over the tint) with both readable. Bars + ramps stopped the gridlines
bleeding THROUGH them: `_capsule` now paints an OPAQUE `Palette.SURFACE` rounded-rect under the
translucent hue gradient, so a duration/ramp capsule hides whatever is behind it (gridlines, the on-air
tint). Paint-only; no geometry/model/serialisation change; drift-guarded files untouched. Tests:
`tests/test_timeline_step_anchor_ui.py::
test_paint_gridlines_are_vertical_and_skip_the_hatch_band` (a stub painter records the drawn x's: every
line vertical, none inside the hatch band, a defined-region tune pins a major there). Suite 975 → 976
offscreen.

## Current state — Gantt rows: ramps above tunes; one-shots stand alone at the bottom: COMPLETE (branch `claude/step-to-step-anchoring`, client-only)
Two owner asks about `timeline_model.display_order` row grouping/ordering:
- **Ramps above tunes under a task** — the within-group sort keys on a new `_row_kind_rank(it)` FIRST
  (bar 0 → ramp 1 → tune 2), with `_row_fire` (resolved fire time) breaking ties inside each kind — so a
  task's ramps always sit directly under its bar, above the tunes, regardless of authored order or
  cross-kind fire time.
- **One-shot runs are NOT grouped under a duration task, and sink to the BOTTOM** — a one-shot `run`
  launches a task once; it does not modify a running duration task (a tune/ramp does), so it stands on
  its own row rather than nest under a bar. New `_is_oneshot(it)` (`action=="run"` and not a bar) +
  `_group_key_of(it)`: a one-shot gets a unique per-item group key (`("\x00oneshot", uid)`) so it never
  merges into a bar's group — even a bar that happens to share its `task_name`. In `display_order`'s
  group-ordering key a `band` field (0 = duration-task group, 1 = one-shot) puts ALL one-shots after
  every duration-task group; within each band, earliest fire (then first-seen) decides. So the duration
  tasks + their ramps/tunes render first and the one-shots collect at the bottom in fire-time order,
  never interleaved. Tunes/ramps still group under their parent task (`task_name`). The row header
  already rendered a one-shot as a top-level row (`child` is only true for tune/ramp), so this is
  ordering-only.
Duration-task group ordering by earliest fire is unchanged; pure, non-mutating, never used for
serialisation. Client-only; drift-guarded files untouched. Tests: `tests/test_timeline_redesign_model.py`
(ramps before tunes even when a tune fires first; bar→ramp→tune ranks; a one-shot sharing a task_name
stays its own group; an early one-shot still sinks below its task group; all one-shots collect at the
bottom in fire order; two one-shots on one task are separate rows). Suite 970 → 975 offscreen.

## Current state — sequence step-conflict validation (in-task / same-control): COMPLETE (branch `claude/step-to-step-anchoring`, client-only)
Owner ask: block invalid sequences at save/arm — a tune/ramp outside its parent task, two tunes setting
the same param at the same time, and a power/gain tune where a ramp already controls power/gain; plus
(owner-chosen extras) ramp-vs-ramp on the same control, and a task started more than once. Enforcement:
**save & arm gate only** (no live canvas hard-block). All in **`timeline_model.validate()`** (which already
gates the editor Save button + Ready pill), via new `_step_conflict_error(items)`:
- **Rule A (in-task)** — a `start`/`stop`/`both`-anchored tune/ramp must fire inside its task's on-air
  span (reuses `step_within_task_error` per the task's bar window). Hold/step-anchored steps are timed
  relative to another point (off-air/cross-clock), left to the agent's runtime check.
- **Rule B (tune·tune)** / **C (tune·ramp)** / **D (ramp·ramp)** — one pairwise check per task: any two
  tune/ramp steps whose CONTROL KEYS intersect AND whose time spans overlap conflict. `_controlled_keys`
  = a tune's changed params / a ramp's swept param, normalised by `_control_key` so **`power` and `gain`
  collapse to one `"level"` key** (either drives the output level). `_step_time_span` returns
  `(clock, lo, hi)` on the on-air clock (start/hold/step), the off-air clock (stop), or `"both"` (a
  window-filling ramp overlaps everything); `_spans_overlap` treats different clocks as non-comparable
  (the honest authoring limit — the agent is the arm-time backstop for cross-clock cases).
- **Rule E (one bar/task)** — a duration task may have at most one bar (can't run one task twice; two
  bars would share the on-air window, and "overlap" isn't computable until the window is fixed at arm, so
  a second bar is refused outright — retune with a tune/ramp instead of restarting).
Also a belt-and-suspenders **arm-time re-check** in `sequences_panel._on_start` (`steps_to_items` on the
stored `model_dump`ed steps → `validate()`), so a sequence saved BEFORE these rules is caught at arm too
(fail-open on any converter error). Client-only; no agent/scripts/capability change; drift-guarded files
untouched. Tests: `tests/test_step_conflicts.py` (all five rules + power/gain level equivalence + the
arm-time wire round-trip); one existing paint test fixture updated to use distinct params (it had three
tunes on `bw` at one instant — a real conflict the new rule now flags). Suite 954 → 970 offscreen.
**Deliberately NOT blocked** (owner): a tune param the task doesn't accept (editor already restricts the
picker), and a duplicate no-op tune.

## Current state — sequence editor `/code-review` round 2 (6 findings): COMPLETE (branch `claude/step-to-step-anchoring`, client-only)
A second `/code-review` of the full sequence-editor branch surfaced 6 findings; all fixed client-only
(no agent/scripts/capability change; drift-guarded files untouched). Suite 949 → 954 offscreen.
- **#1 (correctness) — dragging a step-anchored pin's BODY corrupted its offset.** `_anchor_base_x`
  returned off-air (`self._off`) for `anchor="step"` (it only handled start/stop/hold), so a body-drag
  set `it.offset = (x − off_air)/eff` — a large wrong value that then armed/saved. Fixed: for a
  step-anchored item `_anchor_base_x` returns its TARGET's referenced edge x (via `tlm.step_edge_offset`,
  resolved independently of the item's live offset so it's stable across the drag), so the offset is
  measured from the target edge and the pin lands where dropped.
- **#2 (correctness) — group-move double-shifted a step-anchored dependent.** When a selection held
  BOTH a target and a dependent anchored to it, `_group_move` shifted the dependent's offset by `ds`
  while the target edge also moved `ds` → the dependent moved 2·ds (gap grew). Fixed: a step-anchored
  member whose target is also in the moving group is skipped (it follows the target); fix #1 also
  corrects `ref0_x` when the dragged primary is step-anchored.
- **#3 (display) — a negative step offset read "+−M:SS".** The connect-drag readout and the hover
  tooltip prepended a literal "+" to `_mmss` (which already prints "−" for negatives). Both now use
  `_offset_chip_text` (the same helper the connector chip uses).
- **#4 (UX gate) — the plan / scheduled arm bypassed the step-anchor capability gate.** Library
  save/arm gate step anchoring, but `plans_tab._finish_arm_preflight` and `timeline_tab._finish_preflight`
  didn't, so arming a step-anchored plan/scheduled sequence to a < 1.24.0 (or a negative offset to a
  < 1.25.0) agent hit a raw 400. New shared `plans_tab._step_anchor_block_lines(items_steps, fleet)`
  (mirrors the Library gate) is now called by both interactive arm paths, blocking with a clear message.
- **#5 (perf) — `_step_anchor_block` ran the full `steps()` fold on every keystroke.** `_revalidate`
  called it on each name/canvas change and it called `steps()` (folding calibration per task) BEFORE the
  LIBRARY_HOST / any-step-anchor short-circuits. Now it short-circuits on LIBRARY_HOST and checks the
  raw `items()` for a step anchor (its `offset` == the `offset_s` that would be sent) before ever
  folding — no `steps()` for a plain sequence.
- **#6 (hit-testing) — a tune pin's hit region was a symmetric band while its chips draw to one side.**
  `_hit` used `abs(x − cx) ≤ w/2`, so far chips were unclickable and the empty space on the dot's other
  side was a false hit. New `_pin_footprint(it, g)` returns the real drawn extent (dot + chips/caption on
  the `_pin_caption_side`), and `_hit` uses it.
Tests: `tests/test_timeline_step_anchor_ui.py` (`_anchor_base_x` uses the target edge; group-move doesn't
double-shift; negative offset has no doubled sign; `_pin_footprint` matches the chips; the plan gate
`_step_anchor_block_lines` blocks an old agent). Files: `ui/timeline_editor.py`, `ui/sequence_editor.py`,
`ui/plans_tab.py`, `ui/timeline_tab.py`.

## Current state — window-filling ("both") ramp holds its LAST level before off-air: COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo, drift-guarded)
Owner ask: a dual-anchor ("both") ramp filling the on-air window reached its top exactly AT off-air with
0 hold. Now it holds the last level one dwell before off-air (like a single-anchor / "stop" ramp). Fix
is in the **drift-guarded** `api/ramp.py::resolve_ramp` window branch — the window is divided by LEVELS
(`hold = D/N`), so the last value fires at `D − hold` and is held to off-air; `place_ramp` and
`duration_s` (= the full window `D`) are unchanged, so `ramp_span` still draws the "both" bar across the
window and the discrete last-point timing (which the canvas doesn't show for "both") is the only thing
that moved — **no client UI change**. Mirrored byte-identically to `sdr-agent/agent/ramp.py` (agent
`1.25.2`); drift guard green. Tests: agent side (see `sdr-agent/CLAUDE.md`); no client test asserted the
old "both" timing.

## Current state — sequence editor `/code-review` fixes (7 findings): COMPLETE (branch `claude/step-to-step-anchoring`, client-only)
A `/code-review` of the sequence editor surfaced 7 findings; all fixed client-only (no agent/scripts/
capability change; drift-guarded files untouched). Suite 945 → 949 offscreen.
- **#2 — the reported save-blocker: a step anchored to an OFF-AIR target gave a cryptic error.** A step
  anchored (directly or via a chain) to a `stop`/`both`-anchored target can't be timed on the on-air
  clock (off-air time is only fixed at arm — a deliberate Phase-1 limit), so `validate()` rejected it
  with a catch-all "step-to-step anchors form a cycle, or a step anchors to one that can't be timed" —
  the "needed a step even though everything looked correct" report. New **`timeline_model.step_anchor_
  fault(item, by_sid)`** walks the item's single anchor chain to its root and returns a SPECIFIC,
  actionable message naming the offending step + reason: a cycle/loop, a deleted target, or an off-air
  root (`"… anchors to an off-air step (Ramp · chirp), whose time isn't known until the sequence is
  armed — anchor it to an on-air step instead"`). `validate()`'s unresolved-step branch now returns it.
- **#3 — editing a step whose target left the eligible set SILENTLY re-pointed it.** The StepEditor /
  ramp-editor target combo was built from `eligible_step_targets` only; if the stored target had since
  become ineligible (edited to off-air/a bar, or now reads as a cycle) the combo fell to index 0 and
  saved a DIFFERENT target with no warning. New **`timeline_model.step_targets_for_edit(items, item)`**
  = eligible targets PLUS the item's stored target (when it still exists), so editing never swaps the
  anchor behind the operator's back; `validate()`/the agent stay the backstop for a target that
  genuinely can't be timed. Both dialogs (`ui/timeline_editor.py` StepEditorDialog, `ui/ramp_editor.py`)
  now use it.
- **#1 — client/agent ramp END edge divergence (RESOLVED at duration-end, agent fixed to match — see
  the ramp-end-hold note below).** A step anchored to a ramp's `end` was placed at `start + duration_s`
  on the client but `start + (duration_s − hold_s)` (the last tune fire) on the agent. The FIRST fix
  aligned the client DOWN to the agent's last-fire; owner testing then clarified the correct semantics —
  the ramp's final level must be HELD its full dwell before the ramp is "finished", so a dependent
  anchored to the end fires at `start + duration_s` (after the final hold). So the client keeps
  `_ramp_duration` at the three END-edge sites (`resolve_step_offsets`/`step_edge_offset`/
  `_item_edge_offset`) and the **AGENT was fixed** to `last fire + hold` (agent `1.25.1`); both now
  agree at the ramp's full-duration end. `ramp_span` already spanned the full duration for the visual
  bar, so a dependent draws at the ramp bar's right edge.
- **#4 — a rapid double-arm armed the WRONG sequence.** `sequences_panel._pending_arm` was a single slot
  resolved by op name, so arming seq B before seq A's running-task pre-check returned made A's result
  arm B. Now a **`Dict[str, Sequence]` keyed by sequence id** (the async label already carries the id);
  the `seq_precheck`/`seq_stoptasks` handlers `pop(seq_id)`.
- **#5 — the Ready pill could say "Ready" while Save would refuse.** `sequence_editor._revalidate`
  checked only `_current_error()`; `_on_save` also checks `_step_anchor_block()` (the ≥1.24/≥1.25 agent
  capability gate). `_revalidate` now includes it, so the pill matches Save.
- **#6 — the Gantt ordered step-anchored rows by RAW offset-from-edge.** `display_order`/`_row_fire` now
  order a step-anchored item by its RESOLVED base (`resolve_step_offsets`), so a `+5 s` dependent of a
  `200 s` step sits after it, not at the top of the group.
- **#7 — `_make_anchor` pushed a dead no-op undo entry** when `ensure_step_id` early-returned; `_record()`
  moved after the early-returns.
Tests: `tests/test_timeline_step_anchor.py` (off-air actionable message + `step_anchor_fault`;
`step_targets_for_edit` preserves an ineligible stored target / matches eligible otherwise; ramp end =
last fire), `tests/test_timeline_step_anchor_ui.py` (make/detach/delete use the last-fire edge),
`tests/test_timeline_redesign_model.py` (`display_order` by resolved time), `tests/test_run_conflict_ui.py`
(pending arm keyed by id). #5 is a one-line consistency fix (no fleet-stub harness exists) verified by
inspection.

## Current state — step anchors accept a NEGATIVE offset + arrow-placement / caption-flip: COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo)
Owner ask (a 4-step sketch): a step anchored to another step should be able to fire BEFORE its target's
edge (a negative offset — the warm-up lead-in the owner already uses with on-/off-air anchors), and the
connector ARROWS should point at the dependent (the "anchorer"), placed as close to it as possible
WITHOUT colliding with other lines so at a junction it's unambiguous which line an arrow belongs to.
**Agent side** (`sdr-agent` 1.25.0, capability `sequence-step-anchor-negative`): the `offset_s < 0`
rejection is removed; the graph must still be acyclic. **Client side** (this):
- **Negatives end-to-end** — `timeline_model.validate()` no longer blocks a negative step offset;
  `step_drop_offset` returns the (possibly negative) gap unclamped so a drag-to-anchor drop keeps a
  source that sits BEFORE the edge in place; the StepEditor (`_resolve_step_anchor`) and ramp editor
  (`_accept`) accept a negative offset (offset spinboxes already ranged ±100000); the offset-row labels
  are direction-neutral ("Offset — from the step"). New gate `step_anchor_negative_supported(client)`
  (cap + agent ≥ 1.25.0); the **save gate** (`sequence_editor._step_anchor_block`) and **arm gate**
  (`sequences_panel._on_start` + `_step_anchor_negative_ok`) block a negative-offset sequence on a
  < 1.25.0 unit (the library holds only a definition, never blocked; the agent stays the backstop).
- **Arrow placement** (`ui/timeline_editor.py`) — `_paint_connectors` is now two-pass: compute every
  connector's waypoints + draw all PATHS first (lines under everything), then draw each arrowhead + offset
  chip via `_draw_connector_head`, backed off along its own entry run to the spot CLOSEST to the dependent
  that keeps the whole annotation clear of the OTHER connectors' polylines (`_span_clear`/`_pt_seg_dist`)
  — so at a junction each arrow (and its label) unambiguously belongs to its line, and the arrow is no
  longer hidden under the pin. The arrowhead is an open V whose point sits TOWARD the dependent.
- **Right-entry routing** — a dependent placed LEFT of its anchor (a negative offset) is entered from the
  RIGHT (arrow points left): `_connector_points` gained `entry_from_right` (drop column just right of the
  dependent, clear of obstacles); byte-identical for the common x2 ≥ x1 case (the wrap/duck/obstacle tests
  unchanged). Offset chip text is signed (`_offset_chip_text`: `+` for ≥ 0, the `−` that `_mmss` already
  prints for negatives, never `+−`).
- **Caption side from geometry** (`_pin_caption_side`/`_pin_conn_sides`) — a pin's readout flips to the
  side its connectors DON'T occupy: a connector on the RIGHT with a clear LEFT (an anchor target whose
  dependents are to the right, OR a negative-offset dependent entered from the right) → caption LEFT;
  both sides busy → the two-sided right-duck (option C, unchanged); else RIGHT. This fixes the owner's
  case (a pin that is BOTH a negative dependent AND a target — connectors on both right-sides — flips its
  caption to the clear left, so the incoming left-pointing arrow + `−M:SS` chip are visible).
Client-only beyond the agent gate; drift-guarded files untouched. Tests: `tests/test_timeline_step_anchor.py`
(negative resolves before the edge; `step_drop_offset` keeps a source negative; `step_anchor_negative_supported`
gate), `tests/test_timeline_step_anchor_ui.py` (dialog accepts a negative; right-entry routing; signed
chip; `_span_clear`; a 4-step negative layout paints), `tests/test_ramp_view_fold.py` (ramp `_accept`
takes a negative step offset), `tests/test_step_editor_carried_bw.py` (the two-sided negative dependent
flips its caption left). Suite 900 → 945 offscreen. **Verified LIVE cross-repo** on the owner's exact
4-step layout (Step1@0:30, Step2 −0:30→0:00, Step3&4 +0:30→0:30): resolves and paints exactly as sketched.

## Current state — sequence editor REDESIGN, Phase 1 (visual + Gantt layout): IN PROGRESS (branch `claude/step-to-step-anchoring`, client-only)
Owner ask: redesign the sequence timeline editor to look **exactly** like the approved mockup
(`docs/sequence-editor-mockup.html`, published Artifact) — compact/modern, per-task colour, real-time
axis, connectors. Phased build: **visual redesign → drag/drop → connectors (interactive) → context menu
→ time axis**. **Phase 1 (this) = the visual + layout overhaul**, verified offscreen against the mockup
(dev harness `tools/_seqshot.py`, headless Chromium/Qt grab). Shipped:
- **`ui/timeline_model.py`** (pure, tested): `TASK_HUES` + `task_hue_map(items)` (each duration task a
  stable hue distinct from the reserved green/amber/red; tunes/ramps inherit their parent task's hue via
  shared `task_name`); `display_order(items)` → (rows, holds) grouping a task's tunes/ramps directly
  under it, groups ordered by earliest fire time. Tests: `tests/test_timeline_redesign_model.py`.
- **`ui/timeline_editor.py`** — the canvas (`_TimelineCanvas`) is now **one row per item** (Gantt),
  panels/captions dropped. New paint language: capsule bars with a hue rail + vertical gradient + edge
  connection dots; ramps with a rising/falling slope motif + parent-task badge + duration; **tunes/one-
  shots as PINS** (filled circle / diamond) + borderless caption (never a capsule → no false duration);
  the **Hold** divider; on-air/off-air **pills**; a **REAL-TIME axis** — concrete mm:ss ticks across the
  anchored/defined span (`_def_x`), then a **hatched "relative" region** to off-air (length set at arm);
  step-to-step **connectors** drawn as orthogonal rounded SVG-style paths with an always-on `+M:SS`
  offset chip (`_paint_connectors`/`_draw_connector`). Layout LEFT-anchors (small pre-roll gutter, no dead
  warm-up), the axis rides under the rows, and the editor **auto-fits on open** (`_fit`, `showEvent`).
  New **`_RowHeader`** column ("TASKS & STEPS"): task swatch / indented kin-line for children, name + sub
  + type badge, in the task hue — the parent-task encoding shown three ways (position, colour, badge).
  Toolbar restyled to pill chips WITH line icons (`_tool_icon`) + a segmented `− %  +` zoom + Fit, and a
  task-colour **legend** row (`_Legend`). The hosting **`ui/sequence_editor.py` header** was restyled to
  match the mockup: a SEQUENCE NAME label + large borderless name field, a unit/scope chip, ghost Cancel +
  primary Save-sequence buttons, and a **Ready / Needs-a-fix validity pill** (`_set_ready`). Kept the
  tested surface (`_run_label`, `_place`, `render`,
  `_geom`, `_hit`, `_lane_of`, `_default_hold_offset`, `has_hold`, `set_items`) so the whole suite (904)
  stays green. `docs/sequence-editor-mockup.html` is the visual spec.
- **Ramp pill (option B, `docs/ramp-pill-mockup.html`):** a ramp capsule's rising/falling slope moved off
  the text into a dedicated right **end-cap** (`RAMP_CAP_W`; faint tinted zone + hairline divider + a
  slope mark ending in a dot at the destination level); the badge · from→to range · duration sit
  flush-left, elided to the space before the divider. `_paint_ramp` + `_paint_slope`.
- **Phase 2 — interactive selection + drag-to-anchor + drag readout (COMPLETE):** anchoring is now
  100% on-canvas, no forms.
  - **Click-to-select + highlight** — the canvas holds `_selected` (a uid); a single click selects (accent
    ring around the bar/ramp capsule or the pin dot), a click on empty deselects, and **double-click**
    opens the editor (was single-click; `mouseDoubleClickEvent`). A newly-added item is auto-selected.
  - **Remove anchor (UI)** — a selected step-anchored item paints a clickable red **"✕ Remove anchor"**
    pill on the lower leg of its (accent-highlighted) connector; a press detaches via `_detach_anchor`
    (reverts to a plain on-air `start` anchor at the offset it currently resolves to, so it stays put) —
    no dialog. The chip's hit rect is `_rmchip` (recomputed each paint, position recorded in
    `_paint_connectors`).
  - **Drag-to-anchor** — every ramp edge dot + pin dot is a connection handle (`_edge_at`; bars/Hold are
    not Phase-1 participants). Pressing a source handle (a ramp/point's START) begins a connect-drag
    (`_connect`); `mouseMove` rubber-bands to the cursor and snaps onto an eligible target edge
    (`_drop_target` → `tlm.eligible_step_targets`), `mouseRelease` creates the anchor via `_make_anchor`
    (`tlm.step_drop_offset` → offset = the current gap, clamped ≥ 0 so the source stays in place +
    honours the ordering invariant; `tlm.ensure_step_id` stamps the target). `_paint_connect_drag`.
  - **Live time readout while dragging** — `_paint_drag_readout` floats an accent tag (`_paint_tag`) at
    the dragged edge showing the resolved time (`_timing_text`, e.g. `+45 s · on-air`, `off-air −0:30`,
    `Hold · on-resume`); the connect-drag readout reads `+M:SS after <task> · <edge>`.
  - **Right-click context menu** (`contextMenuEvent` → `_open_context_menu`) — **Edit…** (`edit_item`),
    **Duplicate** (`_duplicate_item`: deep-copy + fresh `tlm._ids` uid + cleared `step_id` so the copy
    isn't a reference target, nudged +15 s, then selected; a Hold is unique so it's excluded), **Remove
    anchor** (only when `anchor=="step"` → `_detach_anchor`), and **Delete** (`_delete_with_reanchor`).
    Menu contents come from the pure `_context_menu_spec(it)` (tested) + `_run_context_action`.
  - **Delete re-anchors dependents** — `_delete_with_reanchor` deletes the item, but first, for every
    DIRECT dependent (anchor="step", anchor_step_id == the deleted item's step_id), re-anchors it to a
    plain on-air `start` at the offset it currently resolves to (`_step_bases[dep.uid]`, captured before
    removal) — so a dependent stays at the instant it was meant to fire instead of orphaning. Chained
    dependents (anchored to a direct dependent) are unaffected: the direct dependent keeps its `step_id`
    and its position, so they still resolve.
  - **Undo / redo** — the canvas keeps a stack of item-list snapshots (deepcopies): `_record()` pushes the
    CURRENT state before each mutation (add/replace/remove/clear/anchor/detach/delete) and clears redo; a
    move-drag stashes its pre-drag snapshot at press (`_drag["undo0"]`) and commits it on a moved release;
    `undo()`/`redo()` swap between the stacks via `_restore` (prunes a stale selection). `set_items` (a
    fresh load) resets both stacks. Keyboard: **Ctrl+Z** undo, **Ctrl+Y** / **Ctrl+Shift+Z** redo,
    **Ctrl+D** duplicate the selection, **Delete/Backspace** delete it (`keyPressEvent`; the canvas takes
    focus on press, `StrongFocus`). Toolbar **Undo/Redo** chips (`_sync_undo_buttons` on every `changed`).
  - **Hover tooltips** — `_update_tooltip`/`_tooltip_text` (via `QToolTip`, throttled on the hovered uid;
    `leaveEvent` clears) show a rich multi-line description: task + kind, what it does (ramp from→to·dur,
    tune param changes, bar starts/stops timing), and, if step-anchored, `⚓ after <task>'s <edge> +M:SS`.
  - **Connector routing (owner-reported, obstacle-aware)** — connectors always **exit and enter steps
    HORIZONTALLY** and route AROUND intervening third-party steps, with the offset chip **inline** on the
    entry run (the line passes through it). `_draw_connector` → `_connector_points` → `_ortho_path`
    (rounded-orthogonal through axis-aligned waypoints):
    - **Drop column** is chosen LEFT of the dependent by `chip_w + 24` (room for the inline chip), then
      pushed LEFT of any **intervening obstacle** it lands in (`_intervening_obstacles(anchor_uid,
      dep_uid)` = x-intervals of steps whose ROW is strictly between the two) — so a `+3:00` line no
      longer drops THROUGH a `+0s` step, it runs along the anchor's row and drops clear of it.
    - **Exit** leaves the anchor AWAY from its body (`exit_dir`): END edge / point exits right, START
      edge exits left. **Entry** is always a horizontal run into the dependent's start from the left.
    - **Wrap** when the drop column falls left of the anchor edge (a ~zero offset): exit the stub, run a
      channel just outside the dependent's row, back to the drop column, drop, and run in — entry stays
      horizontal (the owner's "wrap down, back, and into the step start").
    - The offset chip is centred inline on the entry run just left of the step (`chip_cx = x2 − chip_w/2
      − 10`, at `y2`); the selected-connector "Remove anchor" chip sits directly below it (`y2 + 15`).
    Tests: `_connector_points` avoids an intervening obstacle's column and wraps at zero offset
    (`tests/test_timeline_step_anchor_ui.py`).
  - Pure model: **`timeline_model.step_drop_offset(items, src, tgt, edge, h_off, step_bases)`** (+ helpers
    `_by_uid`/`_item_edge_offset`). Tests: `tests/test_timeline_step_anchor.py` (step_drop_offset keeps in
    place / clamps to 0 / rejects self·bar·cycle) + `tests/test_timeline_step_anchor_ui.py` (canvas
    `_make_anchor`/`_detach_anchor`, the remove chip appears only when anchored, `_edge_at`/`_drop_target`
    eligibility; context-menu spec varies by item, duplicate clones with a fresh uid + no step_id, delete
    re-anchors dependents to on-air, delete of a plain item just removes it; undo/redo of add·anchor·
    delete-with-reanchor, set_items resets history; tooltip text names the anchor + bar timing). Suite
    904 → 921 offscreen. Drift-guarded files untouched.
  - **Drag snapping** — while dragging a bar handle / bar body / pin / Hold, the moved edge snaps
    (within `SNAP_PX = 7`) to a meaningful x: the on-air / off-air anchors, the Hold divider, every OTHER
    step's edges (a bar/ramp's start+stop, a pin's centre), and the major axis ticks (`_snap_targets`
    excludes the dragged item; `_snap_cursor` picks the nearest). A snap sets the offset EXACTLY on the
    target (else the 1 s grid) and shows a dashed accent **guide line** (`_snap_guide` / `_paint_snap_guide`,
    cleared on release + hover). Bar-body snaps its START edge. Tests: `_snap_targets` includes other
    edges + anchors and excludes self; a near-edge cursor snaps within `SNAP_PX`
    (`tests/test_timeline_step_anchor_ui.py`). Suite 923 → 924.
  - **Multi-select + group move** — the canvas holds a `_selection` SET (with `_selected` as the PRIMARY
    for the connector chip / context menu / tooltip). **Shift/Ctrl-click** toggles an item
    (`_toggle_select`); a plain click on a member keeps the set (so a body-drag moves the group) and
    collapses to just it on release-without-move; **Ctrl+A** selects all; an empty-canvas **marquee**
    drag (`_marquee` / `_apply_marquee`, a translucent accent rect) selects intersecting items
    (additive with a modifier). `_paint_selection` rings every selected item. **Group move**: dragging
    any selected item's BODY (`bar_body`/`run_body`) shifts every selected item by one on-air delta
    (`_group_move` / `_group_bases`), snapping the primary's leading edge (excluding the whole moving
    group from snap targets); the gap between them is preserved. **Delete/Backspace** deletes the whole
    selection in one undo step (`_delete_selection`, reusing `_reanchor_deps`, which re-anchors each
    deleted target's dependents unless they're also being deleted); the right-click menu shows
    **"Delete selected"** for a multi-selection. Live-drag already previews the move, so no separate
    ghost. Tests (`tests/test_timeline_step_anchor_ui.py`): toggle/select-only, delete-selection +
    one-undo, marquee intersect, group move preserves the gap. Suite 924 → 928.
  - **Overview minimap** (`_Minimap`, an "OVERVIEW" strip under the stage) — the whole sequence scaled
    to fit (`scale = track_w / canvas.width()`): on-air (green) / off-air (red) guides, one task-hued
    segment per row (`_rows`/`_geom`, bars/ramps span start→stop, pins a small mark; tunes/ramps dimmed),
    and a draggable accent **viewport rectangle** (the scroll area's value + viewport width, scaled).
    Click/drag the strip → `_scroll_to` centres the canvas there. Repaints on the canvas `changed` and on
    the horizontal scrollbar's `valueChanged`/`rangeChanged` (covers edits, scroll, and zoom). Smoke
    test: `_minimap` present, paints, `_scroll_to` safe (`tests/test_timeline_step_anchor_ui.py`).
    Suite 928 → 929. **The redesign's interactive plan (visual → drag/drop → connectors → context menu →
    time axis → snapping → multi-select → minimap) is now COMPLETE.**
  - **Axis off-air anchor labelled "0"** — `_paint_axis` labels the off-air anchor **0** (was "arm",
    owner found it confusing); warm-up/cool-down ticks already read ±M:SS around it, so it reads
    balanced. Muted tick-label colour. Paint-only. Suite still 929.
  - **Ramp / tune power shows the CONTROLLED quantity in the row header + canvas** (owner ask) — the
    left-side `_RowHeader` sub-line showed a calibrated `--power` in the raw BASE quantity for both a
    ramp's from→to and a tune step's value. Now both read the quantity the operator SET (a chirp's live
    density, `power_view`) — the tune canvas pill already did this via `_pill_power_display`; extended to
    the ramp canvas pill (`_paint_ramp`) and the row header for both. `_pill_power_display`'s view-delta
    math was extracted into **`_view_delta_for(item, info, pv)`** (bw-keyed view → delta at the CARRIED
    bw via `sequence_effective_values`; constant-offset view → the law's rep delta) and reused by a new
    **`_ramp_power_display(item)`** → `(from_str, to_str)` in the view's unit (None for a non-power ramp
    or no view → raw base). `_RowHeader._meta` routes tune→`_pill_power_display`, ramp→`_ramp_power_display`.
    Best-effort (any gap falls back to raw base). Client-only; drift-guarded files untouched. Tests:
    `tests/test_step_editor_carried_bw.py` (`_ramp_power_display` shows density at carried bw / None
    without a view / None for a `--bw` ramp; the row header shows the controlled quantity for a ramp AND
    a tune). Suite 929 → 932.
  - **Tune-step readout chips on the canvas — option B** (owner ask: the pin caption looked "cheap" +
    the task-name badge was redundant with the pin colour). Mockup `docs/tune-pin-mockup.html` (published
    Artifact); owner picked **B (recessed readout chip)**. `_paint_pin`'s tune branch now drops the
    task-name badge (the pin hue + row header already name the task) and renders **one recessed inset
    chip per changed param** (`_paint_tune_chips`): a hue rail in the task colour, an UPPERCASE param
    label (`TEXT_FAINT`), the mono value, and a **family-tinted unit chip** (teal density / slate dBm,
    colours from `param_form._family_chip` via `_unit_chip_colors`); an on/off param renders a small
    green "on" / muted "off" state pill. The controlled `--power` shows its view quantity + unit
    (split from `_pill_power_display`). Layout is measured once in `_tune_chip_defs` and shared by
    `_paint_tune_chips` + `_run_width` (footprint/hit stay in sync); `_tune_parts` splits each param
    into `(name, value, unit, is_flag)`. A ONE-SHOT keeps its task-name caption (the name is its
    identity). Chip metrics: `TCHIP_*`/`TUCHIP_PAD` constants. Client-only; drift-guarded files
    untouched. Tests: `tests/test_step_editor_carried_bw.py` (`_tune_parts` split + flags; one chip per
    param with widths + separator; family colours differ + the pin paints). Suite 932 → 935.
    - **Anchor-target pins caption LEFT** (owner follow-up): a pin that some step is anchored TO has its
      connector exit to the RIGHT (`_paint_connectors`: a point exits right), which would run straight
      through a right-hand caption. `_paint_pin` now checks `_is_anchor_target(it)` (any row with
      `anchor=="step"` referencing this item's `step_id`) and, when true, places the caption on the LEFT
      of the pin (chips right-aligned ending at `cx − 13`; one-shot name right-aligned) so it clears the
      line; a plain pin keeps the right side. `_paint_tune_chips` now takes pre-measured `(defs, fonts)`
      + a start x so the caller can left- or right-anchor without resolving twice. (A pin that is BOTH a
      target and a dependent is rare — it flips left, favouring the more prominent right-exit line.)
      Test: `tests/test_step_editor_carried_bw.py::test_anchor_target_pin_flips_its_caption_to_the_left`.
      Suite 935 → 936.
    - **Two-sided pins duck the exit UNDER the caption** (owner follow-up; mockup
      `docs/tune-pin-both-sides-mockup.html`, option C): a pin that is BOTH a target AND a dependent
      (a chain `A → this → C`) has a connector off both sides, so the left-flip has no free side.
      `_paint_pin` keeps its caption on the RIGHT with a wider gap (`PIN_CAP_GAP2` = 24 vs
      `PIN_CAP_GAP` = 13), and the connector router routes that pin's EXIT line horizontally out, then
      into a channel just below/above the readout (toward the dependent) and UNDER it — so the line
      never runs through the text (connectors paint under the pins, so a beside caption otherwise reads
      as line-through-text). `_pin_right_caption(it)` returns the right caption's `(left_x, width)`
      (None when flipped left); `_paint_connectors` passes it as `anchor_cap` to `_draw_connector` →
      `_connector_points`, which prepends a duck (`exit stub → drop just before the caption → under-
      channel`) and resumes the normal drop-to-dependent routing past the caption. Byte-identical for a
      one-sided pin / bar / ramp anchor (`anchor_cap` None). Tests: `tests/test_step_editor_carried_bw.py`
      (two-sided pin keeps a right span at the wide gap + target-only stays None; `_connector_points`
      ducks below y1 and drops before the caption with `anchor_cap`, no duck without). Verified live on
      an `A → mid → C` tune chain. Suite 936 → 938.

## Current state — step-to-step anchoring Phase 1 (client authoring + geometry): COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo; stacked on `run-task-conflict-guards`)
Owner ask: anchor a step not only to on-air/off-air/Hold but to ANOTHER step's start/end edge — e.g. a
ramp right after another ramp's end — so editing the target moves everything downstream (a DAG).
Decisions: full DAG (any step → any step's start/end + offset); PHASED (Phase 1 = tunes/ramps; Phase 2
makes the Hold itself step-anchorable). Plus the owner ordering rule: a dependent may never fire before
its anchor (offset ≥ 0), and moving an anchor mustn't invalidate a dependent. **Agent side** (1.24.0,
capability `sequence-step-anchor`; see `sdr-agent/CLAUDE.md`): two-pass topological `_resolve_steps`,
`_validate_steps` rejects unknown target / self / bad edge / cycle / step-in-a-Hold-sequence / negative
offset. **Client Phase 1** (this):
- **`api/models.py`** — `SequenceStep` gains `id` / `anchor_step_id` / `anchor_edge` (additive; a plain
  sequence's wire is byte-identical — the fields are emitted only when a step is a target or is anchored).
- **`ui/timeline_model.py`** — `RunItem` carries `step_id`/`anchor_step_id`/`anchor_edge`.
  **`resolve_step_offsets(items, h_off)`** places every `anchor="step"` item topologically on the on-air
  clock (a point's start==end==offset; a ramp's end==start+duration; chains resolve; a cycle/unknown/
  off-air target drops → the item falls back to its own offset, `validate()` blocks it). `effective_
  anchor_offset`/`ramp_span`/`_carry_order_key` gained an optional `step_bases`; both temporal power walks,
  `sequence_effective_values`, `compute_anchors`, and `min_on_air_duration` (pre-resolves to start-anchored
  offsets before delegating to the drift-guarded `api.ramp`) thread it. `validate()` mirrors the agent.
  `eligible_step_targets(items, source_uid)` (cycle-safe, on-air-resolvable points+ramps only — NOT bars/
  Hold in Phase 1), `ensure_step_id`, `step_edge_offset`, `step_anchor_supported(client)` (`≥ 1.24.0`).
- **`ui/timeline_editor.py`** (StepEditorDialog) + **`ui/ramp_editor.py`** — an **"after another step…"**
  anchor option (shown when an eligible target exists and there's no Hold), with **Anchor to** (target) +
  **Relative to** (its start/end) pickers; saving assigns the target a stable id and records the anchor;
  a negative offset is refused. The canvas resolves `step_bases` alongside `_hold_off`, so a dependent
  draws off its target and moving the target moves it.
- **`ui/sequence_editor.py`** (save) + **`ui/sequences_panel.py`** (arm) — a safety gate `step_anchor_
  supported`: block saving/arming a step-anchored sequence to a unit whose agent is < 1.24.0 (the agent
  stays the hard backstop; the library holds only a definition, never blocked).
**Owner-testing fix (round-trip drop):** `TimelineEditor.steps()` (deploy) and `set_steps()` (load)
rebuild the wire step dicts BY HAND and were dropping `id`/`anchor_step_id`/`anchor_edge` — so a saved
step-anchored sequence armed as `anchor="step"` with an EMPTY `anchor_step_id` (agent 400 "needs
anchor_step_id"), and on re-edit the item reloaded `anchor="step"` with no target → the dialog fell back
to the first eligible step (the "wrong target" symptom). Both now carry the three fields. The pure
`items_to_steps`/`steps_to_items` were always correct; the gap was these two hand-built converters — now
covered by `test_editor_steps_roundtrip_preserves_step_anchor`.
Tests: `tests/test_timeline_step_anchor.py` (resolver / round-trip / validate / min-duration / eligible
targets / gate / stable id) + `tests/test_timeline_step_anchor_ui.py` (both dialogs offer the anchor,
assign the id, hide it with a Hold, refuse a negative offset, editor steps()/set_steps() round-trip).
Suite 875 → 900 offscreen. Drift-guarded
files untouched. **KNOWN Phase-1 LIMITATIONS**: a step-anchored target is limited to points/ramps (a bar's
off-air end and the Hold aren't offered as targets — the Hold becomes step-anchorable in Phase 2);
dragging a step-anchored pill on the canvas doesn't live-track during the drag (it snaps into place on
drop — the dialog is the precise authoring path). **NEXT — Phase 2**: the Hold itself step-anchorable.

## Current state — run/task conflict guards (arm-over-running · stop-task-in-run): COMPLETE (branch `claude/run-task-conflict-guards`, client-only)
Owner ask: (1) arming a sequence/plan whose task is already running should INFORM + offer to stop it;
(2) stopping a task (Tasks tab) that's part of a running sequence/plan should INFORM + offer to stop
just the task OR the whole run. Client-only — no agent change; uses existing endpoints (`list_tasks`,
`stop_task`, `cancel_sequence_run`, arm), and the agent's existing arm guard ("cannot arm: task(s)
already running") stays the backstop. Pieces:
- **`ui/run_conflict.py`** (new, pure/no-Qt) — `sequence_task_names(steps)`, `running_task_names(
  statuses, wanted)` (∩ RUNNING/STARTING), `active_runs_using_task(runs, task)` (ARMED/RUNNING/HOLDING
  runs whose steps use the task), `run_label(run)` (`plan “X”`/`sequence “Y”`).
- **Arm pre-check (Feature 1)** — `sequences_panel._on_start` now fires `seq_precheck` (fetch
  `list_tasks`, intersect with the sequence's tasks) → `_on_task_done`: clear/none → `_arm_flow` (the
  old `_on_start` body, extracted: ArmDialog + arm); conflicts → `_offer_stop_and_arm` ("Stop & arm" /
  Cancel) → `seq_stoptasks` (stop each) → `_arm_flow`. Plans: `_on_arm`'s preflight also fetches
  `tasks_all`; `_finish_arm_preflight` computes per-unit conflicts across items → `_offer_stop_and_arm_plan`
  → `plan_stoptasks` (`_stop_tasks_on_hosts`, per-task result) → re-run `_on_arm` (preflight now clear).
- **Stop-in-run (Feature 2)** — `unit_detail`: `UnitDetail.on_fast_update` feeds `snap.runs` →
  `_TasksPanel.update_runs`; each `_TaskRow` gets a `runs_provider`. `_TaskRow._on_stop` → if an active
  run owns the task, `_confirm_stop_owned` (3-way: **Stop task only** / **Stop sequence/plan** /
  Cancel); "Stop run" aborts the owning run(s) via `cancel_sequence_run` (`task_abortrun` label);
  else the plain stop. Multiple owning runs → "Stop all".
No agent/scripts/capability change; drift-guarded files untouched. Tests: `tests/test_run_conflict.py`
(pure helpers) + `tests/test_run_conflict_ui.py` (row stop routes to the dialog only when a run owns
the task; panel feeds runs to rows; sequence pre-check arms/offers/stops-then-arms; the plan batch-stop
helper reports per-task). Suite 870 → 875 offscreen.

## Current state — spreadsheet run-log export (client): COMPLETE (branch `claude/hold-step-phase-0-wwwxf7-lty0i5`, cross-repo)
Export a ran sequence/plan's log as an **.xlsx** — one ROW PER STATE CHANGE (a tune that changes
nothing adds no row), every power quantity + realized SDR gain/attenuation + each live/derived param in
its own column (fixed params like PRN/Frequency at the end; RF as 0/1), and (for a multi-unit plan) one
SHEET PER UNIT. The **agent** builds each run's per-change tables (`GET /sequence-runs/{id}/log-table`,
capability `sequence-log-table`, agent `1.21.0`; see `sdr-agent/CLAUDE.md`); the **client** turns them
into the workbook. Shipped, client-only beyond the agent endpoint; drift-guarded files untouched:
- **`api/client.py`** — `sequence_run_log_table(run_id)` (`GET …/log-table` → the
  `{run_id,sequence_name,state,on_air_at,tables:[{task,columns,rows}]}` payload).
- **`ui/run_export.py`** — pure helpers + the dialog. `run_has_data` (a run with ≥1 fired step) /
  `recent_runs(runs, sequence_id, plan_id="", limit=10)` (exportable runs, newest first, capped 10,
  scoped to the sequence and optionally the plan); `_safe_sheet` (≤31 chars, strip `[]:*?/\`,
  uniquify `… (2)`); `build_workbook`/`save_workbook` (openpyxl; a styled header row, freeze panes,
  column widths; never an empty workbook); `tables_to_sheets(unit_label, payload)` (1 task → a
  unit-named sheet, multi → `"<unit> — <task>"` per task). **`RunExportDialog`** (the row entry point):
  lists the last ≤10 runs, gathers one `/log-table` per target unit, writes one .xlsx.
- **Row buttons** — an **"Export…"** button on `ui/sequences_panel._SequenceRow` (gated
  `can_run and export_ok`) and `ui/plans_tab._PlanRow` (gated `export_ok`), wired to `_on_export` →
  `RunExportDialog`. `export_ok` = the unit advertises `SEQUENCE_LOG_TABLE_CAPABILITY`
  (`ui/timeline_model`; plan row: ANY target unit supports it).
- **Packaging** — `openpyxl` added to `requirements.txt` + `sdr_client.spec`.
- **KNOWN LIMITATION / TODO** — a multi-unit plan export targets the FIRST unit's run only: v1 plans
  are single-unit, and `RunExportDialog` gathers one `/log-table` per target but keys them all by the
  SAME `run_id` (which only holds for one unit). Per-unit run-id resolution across a plan arm is a TODO
  (the run row would need to map each unit → its own run for that arm).
Tests: `tests/test_run_export.py` (pure: `recent_runs` scoping/order/cap, `run_has_data`, `_safe_sheet`,
`build_workbook`/`save_workbook` round-trip, `tables_to_sheets` single/multi/empty; rows: `_SequenceRow`
+ `_PlanRow` show/hide the Export button on `export_ok` and a click calls `on_export`). Suite 833 → 847
offscreen. Verified live end-to-end against the agent: a PRN run (RF on → `--sidelobes` 5→10 → a no-op
tune → RF off) exports 4 rows for 6 fired steps — the no-op tune adds no row.

## Current state — Hold step runnable in PLANS (single-unit, operator-present): COMPLETE (branch `claude/hold-step-phase-0-wwwxf7`, client-first)
A deliberate scope expansion of the plan/schedule no-op (docs/sequence-hold-step.md §7/§12): a Hold is
now **authorable in the plan editor** and **actually pauses when a plan is armed directly for a SINGLE
unit** (operator-present). Multi-unit plans (cross-unit Hold sync deferred — §10) and the unattended
schedule STILL compile the Hold out, with the up-front notice. Client-first — the agent already runs the
runtime (1.19.0) and needs no code change (`arm` accepts any `hold_aware` arm, plan-stamped or not, and
refuses only a Hold armed WITHOUT `hold_aware`). Pieces:
- **Authoring** (`ui/plan_editor.py`): dropped `set_hold_authoring(False)` in `PlanItemDialog` — `+ Hold`
  is available in the plan-local sequence editor (it round-trips via the shared `TimelineEditor`). The
  ARM path, not the editor, decides whether a Hold pauses.
- **Runtime gate** (`ui/timeline_model.py`): `hold_runtime_supported(client)` = advertises `sequence-hold`
  AND agent ≥ **1.17.0** (`SEQUENCE_HOLD_RUNTIME_MIN_VERSION`, `_agent_version_tuple`). The cap is
  advertised from 1.16.0 (data model only — a 1.16.0 agent REFUSES a hold-aware arm), so the version
  check turns a would-be agent-side refusal into a clean client-side block. Unknown/blank version →
  capability is authoritative (never blocks a real capable unit). The Library gate
  (`sequences_panel._arm_hold_aware` → `_hold_runtime_ok`) now uses it too.
- **Direct plan arm** (`ui/plans_tab.py`): `_hold_aware_plan_item(plan, resolved, supports_runtime)` is
  eligible iff EXACTLY ONE item, its resolved steps hold, and the unit runs the runtime → `_arm_plan(…,
  hold_aware=True, max_hold_s)` sends the Hold VERBATIM (open-ended, no collapse), via a Library-style
  ArmDialog (`_arm_hold_aware_plan`: body note, `show_stop=False`, `max_hold_default_s`). A steps-less
  item's legacy overrides are baked into inline steps (the agent refuses `step_overrides` with a Hold).
  Any other Hold-bearing plan collapses + the (reason-tailored) notice. `_arm_scheduled`
  (`ui/timeline_tab.py`) is untouched — still collapses.
- **Holding plan run row** (`_PlanRow` + `PlansTab`): `_ACTIVE` now includes HOLDING; while HOLDING the
  Arm button becomes **Proceed**, a RUNNING hold-aware not-yet-held run offers **Hold now**, and a
  HOLDING run offers **Edit…** (gated per-unit on `sequence-hold-now`/`sequence-hold-edit`).
  `_on_proceed`/`_on_hold_now`/`_on_edit_wb`/`_hold_status_text` mirror `sequences_panel`; `_wb_edits`
  holds per-run window-B edits sent as `ProceedRequest.steps` on the next Proceed.
Tests: `tests/test_plan_hold_arm.py` (runtime gate, plan-local + PlanItem round-trip, `+ Hold` enabled in
the plan-item dialog, eligibility 4 cases, hold-aware arm sends the Hold verbatim + override-baking,
multi-unit still collapses, Proceed/Hold-now/Edit row controls). Suite 818 → 833. Agent added one
regression test only (see sdr-agent CLAUDE.md).

## Current state — Hold-step UI fixes: ramp/duration Hold-anchoring + window-B timing labels: COMPLETE (branch `claude/hold-step-phase-0-wwwxf7`, client-only)
Two owner-reported gaps in the Hold authoring UI (the runtime + canvas already resolved `anchor="hold"`;
these were the missing authoring surfaces + a wrong label). Client-only; drift-guarded files untouched.
- **Window-B timing labels** — `ui/timeline_editor.py::_timing_text` now handles `side="hold"`: a
  step anchored to the Hold reads **on-resume** (offset 0 or after) or **pre-hold** (before), never the
  old fallthrough "off-air". Drives the run/tune canvas chips (which pass the item's real anchor) and a
  bar's START chip. A **ramp's** canvas chips are special: `ramp_span` maps a Hold ramp onto the START
  axis at `hold_offset + offset` for geometry, so `_paint_ramp` remaps each end back via the new
  `_ramp_end_side_off` (side `hold`, offset `end − hold_offset`) before labelling — else the chip read
  "+X s · on-air".
- **Ramp → Hold anchor** — `ui/ramp_editor.py` offers **"Hold (after Hold)"** in the anchor dropdown
  when the timeline has a Hold (or the ramp already uses it — `findData`-based selection); `_sync_anchor`
  treats it as a single forward-from-resume anchor (relabels the offset row "Offset from Hold (resume)",
  hides the end-offset row), `_ft_sublabels`/the preview name it "on-resume"/"Hold (resume)", and the
  on-air window-fit check (`step_within_task_error`) is skipped (window B is timed at proceed — mirrors
  the step editor's hold-anchored tune). The down-ramp is the motivating case; canvas geometry
  (`ramp_span`/`effective_anchor_offset`) + round-trip + the agent's `_resolve_ramp(hold_at=…)` already
  handled it.
- **Duration task → Hold anchor** — a bar can now be a **window-B duration task**: new
  `BarItem.start_anchor` ("start" | "hold") hangs its START off the Hold (its STOP stays off-air).
  `item_to_steps`/`steps_to_items` round-trip it; `bar_start_placement`/`_carry_order_key` place +
  order the start at the hold divider; canvas geometry + the start-handle drag are hold-relative
  (clamped ≥ resume); the step editor grows a **"Start anchor"** picker (ON-AIR / at Hold) shown for a
  bar only when a Hold exists, relabelling the start-offset row; `api/models.collapse_hold` re-anchors
  it for the schedule (generic — no change needed). The agent already resolves a `START(anchor="hold")`
  in window B (no agent code change; the `_split_hold_windows` routing is covered by a new agent test).
Tests: `tests/test_hold_ui_anchors.py` (hold timing labels; ramp Hold anchor option/rows/sublabels/
round-trip; window-B bar round-trip/geometry/collapse/step-editor picker + save). Suite 807 → 817
offscreen. Agent: `tests/test_sequence_hold_model.py::test_split_hold_windows_routes_window_b_start_and_ramp`
(425 → 426).

## Current state — Windows high-DPI (125% scaling) robustness: COMPLETE (branch `claude/hidpi-windows-scaling`, client-only)
A coworker's INSTALLED build at Windows 125% scaling looked "weird" (blurry/misaligned) while the owner's
from-source run at 100% was crisp. Root causes + fixes (from a `/code-review` audit — Qt6 already
auto-scales and paints in LOGICAL px, so hardcoded pixel geometry is mostly safe; the real culprits are
DPI-awareness, font conventions, and fixed rasters):
- **The frozen EXE wasn't DPI-aware.** New **`packaging/app.manifest`** declares **PerMonitorV2**
  (`<dpiAwareness>` + legacy `<dpiAware>`, `asInvoker`), embedded via **`sdr_client.spec`** `EXE(manifest=…)`.
  Without it Windows bitmap-scales (blurs) the whole window at 125%. This is the primary fix.
- **Deterministic scaling** (`main.py`, before `QApplication`): `setHighDpiScaleFactorRoundingPolicy(
  PassThrough)` (keep the real 1.25, don't snap) + `AA_Use96Dpi` (pin point-size fonts to the same
  96-DPI baseline the theme's pixel-size fonts use, so the two conventions can't diverge — chosen over a
  risky wholesale `setPointSize`→`setPixelSize` sweep of the canvases/logs).
- **`QFont("monospace", …)` → `mono_font(px)`** (`agent_update_dialog`, `provision_dialog`,
  `calibration_panel` ×2): "monospace" is not a real Windows family and was point-sized, so log/JSON panes
  fell back to a proportional font and lost column alignment. `theme.mono_font()` = IBM Plex Mono +
  Monospace style hint + pixel size. (Dead `QFont` imports removed.)
- **Crisp file-tree icons** (`scripts_panel._svg_icon`): render the SVG at `32×dpr` and
  `setDevicePixelRatio`, cached per ratio, instead of one DPR-1.0 32px raster that upscales blurry.
- **No-truncate action buttons** (`sequences_panel._SequenceRow`): `setFixedWidth` → `setMinimumWidth`
  so "Proceed"/"Hold now"/"Edit…" grow to fit a wider fallback font instead of clipping.
Deferred (low-value/higher-risk, noted not done): the `ToggleSwitch`/`Dropdown` 0.5-px crisp-line insets
(`param_widgets.py`) soften slightly at fractional DPR — cosmetic, custom-paint, left alone. No
agent/scripts change; drift-guarded files untouched. Tests: `tests/test_hidpi_ui.py` (buttons size-to-
content; SVG icon renders + caches per DPR); full suite 802 → 804 offscreen.

**Follow-up — vertical overflow on a shorter viewport (same branch).** Making the app genuinely
DPI-aware means Qt now reports the TRUE (shorter) logical height at 125% (a 1366×768 panel is only
~614 logical px tall), which exposed every dialog that hardcoded a tall height without capping to the
screen — the footer buttons (Save/Cancel, outside the scroll) dropped off-screen and unreachable.
Only `ramp_editor` had the cap. Fix: extract that cap into a shared **`widgets.fit_dialog_to_screen(
dialog, w, h, *, cap_max=True)`** — resize to `(w, h)` but never past ~0.92×/0.94× of
`availableGeometry()`, relax any forced minimum above the cap so a dialog can still shrink into view,
and pin `setMaximumHeight` (skip it via `cap_max=False` for the resizable `main_window`, which must
stay maximizable). Applied to `run_task_dialog`, `timeline_editor` StepEditor, `live_tune_dialog`,
`plan_editor` ×2, `hold_edit_dialog`, `calibration_panel` (source-bias + JSON dialogs), and
`main_window`. `arm_dialog` had NO scroll area (capping alone would clip), so its stacked sections were
wrapped in a `QScrollArea` with the Arm/Cancel footer PINNED outside it, then capped. Tests added to
`tests/test_hidpi_ui.py` (cap + minimum-relax; `cap_max=False` stays maximizable; arm footer outside
the scroll); suite 804 → 807 offscreen.

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
780 → 788 offscreen.

## Current state — Hold step Phase 3b (Fast-Forward-to-Hold): COMPLETE (branch `claude/hold-step-phase-0-wwwxf7`, cross-repo)
Design §5.4. A **"Hold now"** button jumps a RUNNING hold-aware run straight to its Hold, skipping the
rest of the run-up (the up-ramp stops emitting; the signal holds its current value) — so the test
reaches the interesting state without waiting out a ramp whose outcome is already known. Client
(`sdr-client`): `api/client.py::hold_now_sequence_run` (`POST /sequence-runs/{id}/hold-now`, no body);
`SEQUENCE_HOLD_NOW_CAPABILITY = "sequence-hold-now"` (`ui/timeline_model.py`); a **"Hold now"** button
on the sequence row (`ui/sequences_panel.py::_SequenceRow`) shown only for a RUNNING hold-aware
not-yet-held run (`held_actual` None) when the agent advertises the capability, wired to `_on_hold_now`
(a confirm, then `client.hold_now_sequence_run` → the run flips to HOLDING and the row's Proceed button
takes over). Agent (`sdr-agent` 1.18.0, capability `sequence-hold-now`): `SequenceRunner.hold_now`
marks un-fired window-A steps skipped, `RUNNING → HOLDING`, stamps `held_actual` (409 if not a running
hold-aware run with a pending Hold). Client-only files touched here; no drift-guarded change. Tests:
`tests/test_hold_arm_proceed.py` (`hold_now_sequence_run` posts to `/hold-now`; the row shows "Hold
now" only for a running hold-aware run with the capability, hidden once held or when unsupported); suite
788 → 790 offscreen.

## Current state — Hold step Phase 3c (edit-while-holding): COMPLETE — the Hold step is feature-complete (branch `claude/hold-step-phase-0-wwwxf7`, cross-repo)
Design §6.4. While a run is HOLDING, the operator can retarget the **post-hold (window-B) steps** — e.g.
aim the down-ramp at the −50 dBm where the receiver actually lost lock — then Proceed with the revision.
Client: **`ui/hold_edit_dialog.py::HoldEditDialog`** hosts the same `TimelineEditor` used to author a
sequence, loaded with the running sequence and wired to the unit (tasks/yaml/calibration); a banner
states only post-hold steps take effect (window A has run), and OK returns the edited **full** step list
(`result_steps`, requiring the Hold to survive). An **"Edit…"** row button (`ui/sequences_panel.py::
_SequenceRow`) shows only on a HOLDING run when the agent advertises `sequence-hold-edit`, wired to
`_on_edit_wb` → the dialog; the edit is held per-run in `SequencesPanel._wb_edits` and passed to
`_on_proceed` → `_proceed_run(..., steps=edited)` → `ProceedRequest.steps` (`_rebuild` prunes it once
the run leaves HOLDING — applied or aborted). New constant `SEQUENCE_HOLD_EDIT_CAPABILITY`. Agent
(`sdr-agent` 1.19.0, capability `sequence-hold-edit`): `proceed` validates `req.steps` and re-extracts
window B from it (window A ignored — already fired); a ≤1.18 agent ignores `req.steps`, hence the gate.
Client-only files here; drift-guarded files untouched. Tests: `tests/test_hold_arm_proceed.py` (the
"Edit…" button shows only while holding + supported; `_proceed_run` carries/omits edited steps;
`HoldEditDialog` returns the edited full list with the Hold + window B intact); suite 790 → 793
offscreen. Phases 0–3 of the Hold step (design `docs/sequence-hold-step.md`) are now all shipped.

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
