"""
CalibrationPanel — the Calibration sub-tab of the unit detail view.

Shows this unit's power calibration (whether it's calibrated + a resolved per-signal
summary) and lets you edit `calibration.json` two ways:

  • Editor  — the "chain builder" (calibration v2, docs/calibration-v2.md §8): the RF
              chain reads left-to-right as a flow of STAGES. Source/measured stages show
              their gain→power minicurve; PASSIVE stages (cable/antenna/pad) are pickers
              onto the fleet-wide component library, their loss evaluated at each
              signal's frequency. Selecting a stage opens a detail pane — a frequency-
              response plot + component picker for a passive stage, or the per-signal
              measured curve grids for a measured stage. Alongside: the resolved Signals
              table, the Limits/ceiling, the Component library grid, and the chain
              settings (gains, operating plane, defaults).
  • JSON    — the raw document (source of truth for the plane topology and anything
              the editor doesn't cover).

Both views drive one document model (self._doc); switching tabs syncs it. Upload or
Save sends the document to the agent, which VALIDATES it (the full resolver checks)
before storing — so a bad curve is rejected with the agent's exact reason, never at
transmit. Passive planes reference components by id; the catalog (components.yaml) is
uploaded to the unit first so those refs resolve.

Network calls go through the DataHub run_async / task_done pattern, filtered to this
host + ops:
    cal_get:<host>   GET /calibration → {unit_type, document, valid, signals|error}
    cal_save:<host>  POST /files (calibration.json) → {saved, calibration:{…}} | raises
"""
from __future__ import annotations

import copy
import json
from typing import Optional

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemDelegate, QAbstractItemView, QAbstractScrollArea, QApplication,
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from api.client import AgentHTTPError
from api.models import UNIT_TYPES, UNIT_TYPE_LABELS
from .theme import Palette

CAL_NAME = "calibration.json"
CAL_CAPABILITY = "calibration"
CAL_VALIDATE_CAPABILITY = "cal-validate"   # agent >= 1.1.9 dry-run endpoint
CAL_COMPONENTS_CAPABILITY = "calibration-components"   # agent >= 1.2.0 (v2 component refs)
_COMPONENTS_NEEDS_NEWER = (
    "this unit's agent is too old for component-based calibration (needs 1.2.0+). "
    "Update the agent, or use a constant Δ dB on the passive planes.")
CAL_PARTIAL_STAGES_CAPABILITY = "calibration-partial-stages"  # agent >= 1.3.0
_PARTIAL_STAGES_NEEDS_NEWER = (
    "this unit's agent is too old to leave a measured stage unmeasured for some signals "
    "(needs 1.3.0+). Update the agent, or measure every signal on each measured stage.")
CAL_NO_SIGNALS_CAPABILITY = "calibration-no-signals"  # agent >= 1.4.0
_NO_SIGNALS_NEEDS_NEWER = (
    "this unit's agent is too old to save a calibration with no signals yet (needs 1.4.0+). "
    "Update the agent, or add at least one measured signal before saving.")
CAL_LIMIT_SIDE_CAPABILITY = "calibration-limit-side"  # agent >= 1.5.0
_LIMIT_SIDE_NEEDS_NEWER = (
    "this unit's agent is too old for input/output-side limits (needs 1.5.0+). It would "
    "ignore 'side' and apply the cap at the plane's output — a different, unsafe limit. "
    "Update the agent, or set every limit's side to Output (the plane itself).")
CAL_PLANE_ROLES_CAPABILITY = "calibration-plane-roles"  # agent >= 1.6.0
_PLANE_ROLES_NEEDS_NEWER = (
    "this unit's agent is too old for reported (report-only) measured stages (needs 1.6.0+). "
    "It would treat a reported stage as an ordinary limiting one and mis-gauge the ceiling. "
    "Update the agent, or make every measured stage Limiting.")
CAL_GAIN_STEP_CAPABILITY = "calibration-gain-step"  # agent >= 1.7.0
_GAIN_STEP_NEEDS_NEWER = (
    "this unit's agent is too old to snap the gain to a step (needs 1.7.0+). It would ignore "
    "the gain step and command an off-grid gain the SDR silently rounds. Update the agent, or "
    "clear the Gain step field.")
CAL_FREQ_OPTIONAL_CENTER_CAPABILITY = "calibration-freq-optional-center"  # agent >= 1.7.1
_FREQ_OPTIONAL_CENTER_NEEDS_NEWER = (
    "this unit's agent is too old to leave the centre frequency blank on a frequency-dependent "
    "chain (needs 1.7.1+). It requires a centre frequency to fold the operating point. Update "
    "the agent, or fill in each signal's centre frequency.")

CAL_ACTIVE_COMPONENTS_CAPABILITY = "calibration-active-components"  # agent >= 1.8.0
_ACTIVE_COMPONENTS_NEEDS_NEWER = (
    "this unit's agent is too old for active components (a task-controlled attenuator/gain "
    "stage — needs 1.8.0+). Update the agent, or remove the active stage before saving.")
CAL_SOURCE_BIAS_CAPABILITY = "calibration-source-bias"  # agent >= 1.9.0 (per-unit source bias)
_SOURCE_BIAS_NEEDS_NEWER = (
    "this unit's agent is too old for a source bias (the SDR's power-vs-frequency flatness — "
    "needs 1.9.0+). Update the agent, or remove the source-bias stage before saving.")
CAL_STAGE_BYPASS_CAPABILITY = "calibration-stage-bypass"  # agent >= 1.9.0 (bypass a stage)
_STAGE_BYPASS_NEEDS_NEWER = (
    "this unit's agent is too old to bypass a stage (needs 1.9.0+). Update the agent, or "
    "un-bypass every stage before saving.")
CAL_POWER_BRIDGES_CAPABILITY = "calibration-power-bridges"  # agent >= 1.10.0
_POWER_BRIDGES_NEEDS_NEWER = (
    "this unit's agent is too old for reported/limiting power-quantity bridges (needs "
    "1.10.0+). It would ignore them and report --power in the measured quantity (wrong power) "
    "and skip the limiting cap. Update the agent, or set every reading to “Same as measured” "
    "before saving.")
CAL_MEASUREMENT_DEEMBED_CAPABILITY = "calibration-measurement-deembed"  # agent >= 1.11.0
_DEEMBED_NEEDS_NEWER = (
    "this unit's agent is too old to de-embed a measurement cable (needs 1.11.0+). It would "
    "leave the cable loss baked into the measurement — wrong absolute power and a mis-placed "
    "ceiling. Update the agent, or clear the measurement cable before saving.")

CAL_MEASUREMENT_QUANTITY_CAPABILITY = "calibration-measurement-quantity"  # agent >= 1.12.0
_MEASUREMENT_QUANTITY_NEEDS_NEWER = (
    "this unit's agent is too old for a per-signal measurement quantity/unit (needs 1.12.0+). "
    "It would ignore the signal's declared unit and show --power in the wrong quantity. Update "
    "the agent, or clear the per-signal measurement unit before saving.")

CAL_LIMIT_THROUGH_READING_CAPABILITY = "calibration-limit-through-reading"  # agent >= 1.13.0
_LIMIT_THROUGH_READING_NEEDS_NEWER = (
    "this unit's agent is too old to gauge a stage limit through the signal's limiting reading "
    "(needs 1.13.0+). It would compare the dBm ceiling against the measured quantity instead — "
    "under-applying the limit and transmitting over the ceiling. Update the agent, or set every "
    "signal's limiting to “Same as measured” before saving.")

CAL_EXTRAPOLATE_CAPABILITY = "calibration-extrapolate"  # agent >= 1.14.0
_EXTRAPOLATE_NEEDS_NEWER = (
    "this unit's agent is too old to extrapolate a measured curve past its endpoints (needs "
    "1.14.0+). It would clamp instead, so the unit would deliver a different power than the "
    "range shown here for commands in the extrapolated region. Update the agent, or set every "
    "signal's measured-curve extrapolation back to “None” before saving.")

# The measured-curve extrapolation modes, and their operator-facing labels for the picker.
_EXTRAPOLATE_LABELS = [("none", "None (clamp at measured)"), ("down", "Extend down"),
                       ("up", "Extend up"), ("both", "Extend both ways")]

# The baseband amplitude every broadcaster script transmits at is a FIXED constant (the
# scripts' baked AMPLITUDE), not an operator control — so calibration is always measured at
# this amplitude and the editor does not expose it as an editable field. It is recorded on
# the document so the script's calkit amplitude gate can validate against it. A calibration
# whose stored amplitude differs (measured with an older, differently-amplitude'd script) is
# NOT silently relabelled: it is flagged here and rejected at runtime until re-measured.
FIXED_BASEBAND_AMPLITUDE = 0.5
_AMPLITUDE_TOL = 1e-6


def _amp_conflicts(value) -> bool:
    """True when a stored amplitude is present and differs from the fixed fleet amplitude —
    i.e. a legacy calibration measured with a differently-amplitude'd script. Such a value is
    preserved (never relabelled) so the runtime gate rejects it until it is re-measured."""
    if value is None:
        return False
    try:
        return abs(float(value) - FIXED_BASEBAND_AMPLITUDE) > _AMPLITUDE_TOL
    except (TypeError, ValueError):
        return False

# When the unit is simply uncalibrated, the /calibration route answers 404 with this
# detail. A generic "Not Found" 404 instead means the route itself is missing — i.e.
# the agent deployed on the unit predates the calibration endpoints and must be updated.
_NO_CAL_DETAIL = "no calibration document"
_OUTDATED_AGENT_MSG = (
    "this unit's agent is out of date — it has no calibration endpoint. "
    "Open the unit's ••• menu → “Update agent…”, then Refresh here.")


def _is_outdated_agent(err) -> bool:
    """Fallback heuristic (used only before /info capabilities are known): a 404 that
    is NOT the agent's own 'not calibrated' answer ⇒ the route is absent ⇒ the deployed
    agent predates the calibration/files endpoints."""
    return (isinstance(err, AgentHTTPError) and err.status_code == 404
            and _NO_CAL_DETAIL not in (err.detail or "").lower())


def _fmt_range(lo, hi, unit: str) -> str:
    if lo is None or hi is None:
        return "—"
    return f"{lo:g} – {hi:g} {unit}"


def _numstr(x) -> str:
    """Format a JSON number for a text field: drop the trailing .0 on integers."""
    if isinstance(x, bool) or x is None:
        return ""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def _to_float(s: str, field: str) -> float:
    s = (s or "").strip()
    if s == "":
        raise ValueError(f"{field} is empty")
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"{field}: '{s}' is not a number")


def _numeric(v, default: float) -> float:
    """A JSON value coerced to float, falling back to ``default`` for None/bad values."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _curve_issues(sid: str, plane: str, pts) -> list:
    """Cheap monotonicity checks on one signal/plane curve, mirroring the resolver:
    points sorted by gain must have strictly increasing gain AND power (invertible)."""
    vals = []
    for pt in pts or []:
        try:
            vals.append((float(pt["gain_db"]), float(pt["power_dbm"])))
        except (KeyError, TypeError, ValueError):
            return [f"signal '{sid}' · {plane}: a point isn't numeric"]
    if len(vals) < 2:
        return []                              # 0 pts = latent (legal); 1 pt = slope-1 ok
    vals.sort(key=lambda gp: gp[0])
    out = []
    if any(vals[i][0] <= vals[i - 1][0] for i in range(1, len(vals))):
        out.append(f"signal '{sid}' · {plane}: two points share a gain")
    if any(vals[i][1] <= vals[i - 1][1] for i in range(1, len(vals))):
        out.append(f"signal '{sid}' · {plane}: power must increase with gain (not invertible)")
    return out


def _control_issues(name: str, control) -> list:
    """Structural check of an active component's ``control`` block, mirroring the agent's
    _parse_control (agent/calibration.py): task/param set, a positive step, a valid range,
    a sense, and a 0..100 engage percentage."""
    if not isinstance(control, dict):
        return [f"active plane '{name}': control must be an object"]
    out = []
    if not str(control.get("task") or "").strip():
        out.append(f"active plane '{name}': set the task that controls it")
    if not str(control.get("param") or "").strip():
        out.append(f"active plane '{name}': set the task parameter it drives")
    if control.get("sense", "attenuation") not in ("attenuation", "gain"):
        out.append(f"active plane '{name}': sense must be attenuation or gain")
    try:
        lo, hi, step = (float(control["min_db"]), float(control["max_db"]),
                        float(control["step_db"]))
        if not hi > lo:
            out.append(f"active plane '{name}': max must exceed min")
        if not step > 0:
            out.append(f"active plane '{name}': step must be greater than 0")
    except (KeyError, TypeError, ValueError):
        out.append(f"active plane '{name}': needs numeric min, max and step")
    try:
        eng = float(control.get("engage_pct", 0.0))
        if not 0.0 <= eng <= 100.0:
            out.append(f"active plane '{name}': engage % must be between 0 and 100")
    except (TypeError, ValueError):
        out.append(f"active plane '{name}': engage % must be numeric")
    consts = control.get("consts")
    if consts is not None:
        if not isinstance(consts, dict):
            out.append(f"active plane '{name}': constant params must be an object")
        elif str(control.get("param") or "").strip() in consts:
            out.append(f"active plane '{name}': the driving param can't also be a constant")
    return out


def _reading_block(sub) -> Optional[dict]:
    """Normalize a reported/limiting reading's editor state into a clean doc block, or None
    when it is the trivial default (Same as measured with nothing else). Mirrors the shapes
    state/power_law.parse_bridge accepts (docs/calibration-v2.md §13); a `law` keeps its
    embedded law dict so the document stays self-contained."""
    if not isinstance(sub, dict):
        return None
    kind = sub.get("kind", "same")
    if kind not in ("same", "own", "law"):
        return None
    out = {"kind": kind}
    if kind == "law":
        law = sub.get("law")
        if not isinstance(law, dict):
            return None
        out["law"] = law
    else:
        k = sub.get("k")
        if isinstance(k, (int, float)) and not isinstance(k, bool) and k:
            out["k"] = float(k)
    unit = str(sub.get("unit", "") or "").strip()
    if unit:
        out["unit"] = unit
    q = str(sub.get("quantity", "") or "").strip()
    if q:
        out["quantity"] = q
    mx = sub.get("max_dbm")
    if isinstance(mx, (int, float)) and not isinstance(mx, bool):
        out["max_dbm"] = float(mx)
    if kind == "own":
        # An `own` reading is a SEPARATELY measured curve (docs/calibration-v2 §13/§15):
        # {kind: own, curve: {points: […]}}. The curve is the whole point — an own reading
        # without one is meaningless, so drop it. (Kept out of the trivial-default check
        # below, which only concerns `same`.)
        curve = sub.get("curve")
        if isinstance(curve, dict) and curve.get("points"):
            out["curve"] = curve
        else:
            return None
    if kind == "same" and set(out) == {"kind"}:
        return None                     # nothing to say — a plain "same" is the default
    return out


# ── per-signal MEASUREMENT (quantity + unit) ─────────────────────────────────────
# The unit the operator measured the signal in. `dBm` is absolute power; the rest are
# spectral densities (a per-Hz/kHz/MHz denominator). The family drives which conversion
# laws apply and whether the limiting reading can be "the same" (a density can't be a dBm
# limit). The agent reads signals.<id>.measurement from >= the Phase-2 capability; today it
# ignores the key, so a dBm measurement is byte-for-byte today's behaviour.
_MEASUREMENT_UNITS = ("dBm", "dBm/Hz", "dBm/kHz", "dBm/MHz")
_UNIT_FAMILY = {"dBm": "abs", "dBm/Hz": "density", "dBm/kHz": "density", "dBm/MHz": "density"}


def _unit_family(unit: str) -> str:
    """The unit family (``abs`` or ``density``) a display unit belongs to; unknown ⇒ abs."""
    return _UNIT_FAMILY.get((unit or "").strip(), "abs")


def _measurement_block(sub) -> Optional[dict]:
    """Normalize a per-signal measurement editor dict into a clean doc block, or None when
    it's the trivial default (absolute dBm, no quantity label). Mirrors _reading_block's
    drop-the-default philosophy so a plain dBm signal stays byte-identical to today."""
    if not isinstance(sub, dict):
        return None
    out = {}
    q = str(sub.get("quantity", "") or "").strip()
    if q:
        out["quantity"] = q
    unit = str(sub.get("unit", "") or "").strip()
    if unit and unit != "dBm":              # dBm is the default — omit it to keep docs clean
        out["unit"] = unit
    return out or None


def _upstream_plane_name(planes: dict, name: str):
    """The plane feeding INTO ``name``'s stage — one hop upstream in the cascade, mirroring
    the agent (agent/calibration.py:_upstream_plane): a derived plane's parent is its
    ``from``; a measured plane's is the plane before it in cascade (dict) order. Returns
    None when ``name`` is the first plane (nothing upstream)."""
    p = planes.get(name)
    if isinstance(p, dict) and p.get("type") == "derived":
        return p.get("from")
    keys = list(planes)
    i = keys.index(name) if name in keys else -1
    return keys[i - 1] if i > 0 else None


def local_calibration_issues(doc) -> list:
    """A fast, best-effort structural check of a working document, for instant editor
    feedback BEFORE the authoritative agent validate/save. Catches the common mistakes
    (non-monotonic curve, no safety ceiling, unset/dangling operating plane, a derived
    plane missing its parent/Δ, a curve on a non-measured plane). Not exhaustive — the
    agent's resolver remains the source of truth."""
    if not isinstance(doc, dict):
        return ["document is not an object"]
    issues: list = []
    if doc.get("schema_version") != 1:
        issues.append(f"schema_version should be 1 (is {doc.get('schema_version')!r})")
    chain = doc.get("chain") or {}
    planes = chain.get("planes") or {}
    if not isinstance(planes, dict) or not planes:
        return issues + ["no planes defined — add at least one measured plane"]

    measured = {n for n, p in planes.items()
                if isinstance(p, dict) and p.get("type") == "measured"}
    for name, p in planes.items():
        if not isinstance(p, dict):
            issues.append(f"plane '{name}' is malformed"); continue
        t = p.get("type")
        if t == "derived":
            frm = p.get("from")
            if not frm:
                issues.append(f"derived plane '{name}' has no parent plane")
            elif frm not in planes:
                issues.append(f"derived plane '{name}' points at unknown plane '{frm}'")
            # A bypassed stage is transparent (0 dB, limits dropped), so it needs no Δ /
            # component / control — skip those checks (it still needs a valid parent above).
            if not p.get("bypass"):
                # A hop's Δ dB comes from an inline constant (delta_db), a library component
                # (component, possibly frequency-dependent), OR — for an active component — its
                # own inline Δ dB(f) table (delta_db_by_freq). Only flag when it has NONE.
                if (p.get("delta_db") is None and not p.get("component")
                        and not p.get("delta_db_by_freq")):
                    issues.append(f"derived plane '{name}' has no Δ dB or component")
                # An ACTIVE component adds a `control` block on top of that baseline.
                if p.get("control") is not None:
                    issues.extend(_control_issues(name, p.get("control")))
        elif t != "measured":
            issues.append(f"plane '{name}' has an unknown type")

    op = chain.get("operating_plane")
    if not op:
        issues.append("no operating plane set")
    elif op not in planes:
        issues.append(f"operating plane '{op}' is not one of the planes")
    else:
        seen, cur = set(), op                  # walk derived hops to a measured anchor
        while isinstance(planes.get(cur), dict) and planes[cur].get("type") == "derived":
            if cur in seen:
                issues.append(f"derived plane cycle through '{cur}'"); break
            seen.add(cur)
            cur = planes[cur].get("from")
            if cur not in planes:
                break

    gl = chain.get("gain_limits") or {}
    if gl.get("max_gain_db") is None and not chain.get("limits"):
        issues.append("no safety ceiling — set a max gain or add at least one limit")
    for lim in (chain.get("limits") or []):
        if not isinstance(lim, dict):
            continue
        if lim.get("plane") not in planes:
            issues.append(f"limit references unknown plane '{lim.get('plane')}'")
            continue
        side = lim.get("side", "output")
        if side not in ("input", "output"):
            issues.append(f"limit on '{lim.get('plane')}': side must be input or output")
        elif side == "input" and _upstream_plane_name(planes, lim["plane"]) is None:
            issues.append(f"limit on '{lim.get('plane')}' is input-side but that stage "
                          "is first in the chain — nothing upstream to cap")

    # An empty signal set is a valid onboarding state — the chain + ceiling can be
    # saved before any signal is measured (the agent accepts a signal-less document;
    # nothing can transmit until a signal is added). So it is not flagged as an issue.
    signals = doc.get("signals") or {}
    default_amp = ((doc.get("defaults") or {}).get("amplitude"))
    for sid, sig in signals.items():
        for pname, curve in ((sig or {}).get("curves") or {}).items():
            if pname not in planes:
                issues.append(f"signal '{sid}': curve for unknown plane '{pname}'")
            elif pname not in measured:
                issues.append(f"signal '{sid}': curve given for derived plane '{pname}'")
            issues.extend(_curve_issues(sid, pname, (curve or {}).get("points")))
        # Amplitude is fixed at FIXED_BASEBAND_AMPLITUDE. A curve measured at a different
        # amplitude describes a different power scale, so the script runs uncalibrated until
        # it is re-measured — surface that here (the runtime enforces it too).
        eff = (sig or {}).get("amplitude", default_amp)
        if eff is not None and abs(float(eff) - FIXED_BASEBAND_AMPLITUDE) > _AMPLITUDE_TOL:
            issues.append(
                f"signal '{sid}': calibrated at amplitude {float(eff):g}, but scripts "
                f"transmit at {FIXED_BASEBAND_AMPLITUDE:g} — re-measure at "
                f"{FIXED_BASEBAND_AMPLITUDE:g} (runs uncalibrated until then)")
    return issues


class _Sparkline(QWidget):
    """A tiny plot of a curve's points, so a fat-fingered point (a dip, a duplicate) is
    obvious at a glance next to the grid.

    ``mode`` sets both the axes hint and how the line is COLOURED:
      • "curve" (default) — a measured gain→power curve. It MUST be invertible (power
        strictly increasing with gain), so a non-monotonic sequence is drawn red as a
        warning.
      • "delta" — a component's Δ dB vs frequency. Monotonicity is meaningless here (an
        antenna/cable can roll off with frequency), so it's coloured by SIGN instead:
        accent for a net gain (positive dB), red for a net loss (negative) — matching
        the gain=blue / loss=red intuition."""
    def __init__(self, mode: str = "curve"):
        super().__init__()
        self._pts: list = []
        self._mode = mode
        self.setFixedHeight(46)
        self.setMinimumWidth(120)
        self.setToolTip("Δ dB (y) across frequency (x)" if mode == "delta"
                        else "gain (x) → power (y) for the points above")

    def set_points(self, pts) -> None:
        vals = []
        for g, p in pts or []:
            try:
                vals.append((float(g), float(p)))
            except (TypeError, ValueError):
                continue
        vals.sort(key=lambda gp: gp[0])
        self._pts = vals
        self.update()

    def _line_color(self) -> QColor:
        """The plot colour. In "delta" mode: accent for a net gain, red for a net loss.
        In "curve" mode: red when the power sequence isn't strictly increasing (a
        non-invertible measured curve), else accent."""
        ps = [p for _, p in self._pts]
        if not ps:
            return QColor(Palette.ACCENT)
        if self._mode == "delta":
            return QColor(Palette.ACCENT if sum(ps) >= 0 else Palette.CRASH)
        bad = any(ps[i] <= ps[i - 1] for i in range(1, len(ps)))
        return QColor(Palette.CRASH if bad else Palette.ACCENT)

    def paintEvent(self, _evt) -> None:
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, pad = self.width(), self.height(), 6
        if len(self._pts) < 1:
            qp.setPen(QColor(Palette.TEXT_FAINT))
            qp.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "no points")
            return
        gs = [g for g, _ in self._pts]; ps = [p for _, p in self._pts]
        g0, g1 = min(gs), max(gs); p0, p1 = min(ps), max(ps)
        gspan = (g1 - g0) or 1.0; pspan = (p1 - p0) or 1.0

        def xy(g, p):
            x = pad + (g - g0) / gspan * (w - 2 * pad)
            y = h - pad - (p - p0) / pspan * (h - 2 * pad)   # y grows downward
            return x, y

        line = self._line_color()
        qp.setPen(QPen(line, 1.5))
        for i in range(1, len(self._pts)):
            x0, y0 = xy(*self._pts[i - 1]); x1, y1 = xy(*self._pts[i])
            qp.drawLine(int(x0), int(y0), int(x1), int(y1))
        qp.setPen(QPen(line, 1))
        qp.setBrush(line)
        for g, p in self._pts:
            x, y = xy(g, p)
            qp.drawEllipse(int(x) - 2, int(y) - 2, 4, 4)


# ── calibration v2 "chain builder" visual pieces (the mockup) ────────────────────

def _interp_db(table, f: float) -> float:
    """Linear interpolation of a [[freq_hz, delta_db], …] table at frequency f, with
    endpoint clamping (mirrors the agent/calkit interp). Empty table → 0.0."""
    pts = sorted(((float(a), float(b)) for a, b in (table or [])), key=lambda p: p[0])
    if not pts:
        return 0.0
    if len(pts) == 1 or f <= pts[0][0]:
        return pts[0][1]
    if f >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        if f <= pts[i][0]:
            (x0, y0), (x1, y1) = pts[i - 1], pts[i]
            return y0 + (y1 - y0) * (f - x0) / (x1 - x0)
    return pts[-1][1]


def _freq_span(table):
    """(min_freq_hz, max_freq_hz) of a delta table, or None if empty."""
    fs = [float(a) for a, _ in (table or [])]
    return (min(fs), max(fs)) if fs else None


def _fmt_ghz_span(table) -> str:
    span = _freq_span(table)
    if not span:
        return "—"
    lo, hi = span
    if lo == hi:
        return "flat · constant"
    return f"{lo/1e9:.2f}–{hi/1e9:.2f} GHz"


def _badge(text: str, fg: str, bg: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet(
        f"color: {fg}; background: {bg}; font-size: 10px; font-weight: 700; "
        f"letter-spacing: .06em; padding: 2px 7px; border-radius: 5px;")
    return lab


# kind → (foreground, background) for badges, matching the mockup
_KIND_COLORS = {
    "source":   (Palette.TEXT_MUTED, Palette.IDLE_SOFT),
    "measured": (Palette.ACCENT, Palette.ACCENT_SOFT),
    "passive":  (Palette.ARMED, Palette.ARMED_SOFT),
    "active":   (Palette.ONLINE, Palette.ONLINE_SOFT),
    "cable":    (Palette.ACCENT, Palette.ACCENT_SOFT),
    "antenna":  (Palette.ONLINE, Palette.ONLINE_SOFT),
    "pad":      (Palette.TEXT_MUTED, Palette.IDLE_SOFT),
}


class _FreqSparkline(QWidget):
    """A tiny ΔdB-vs-frequency curve for a component (loss/gain sweep). A single point
    draws as a flat line (a constant hop)."""
    def __init__(self, height: int = 40):
        super().__init__()
        self._pts: list = []
        self._color = Palette.ACCENT
        self.setFixedHeight(height)
        self.setMinimumWidth(80)

    def set_table(self, table, color: str = Palette.ACCENT) -> None:
        self._pts = sorted(((float(a), float(b)) for a, b in (table or [])),
                           key=lambda p: p[0])
        self._color = color
        self.update()

    def paintEvent(self, _evt) -> None:
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, pad = self.width(), self.height(), 5
        if not self._pts:
            return
        col = QColor(self._color)
        if len(self._pts) == 1:
            qp.setPen(QPen(QColor(Palette.IDLE), 2))
            y = h // 2
            qp.drawLine(pad, y, w - pad, y)
            return
        fs = [f for f, _ in self._pts]; ds = [d for _, d in self._pts]
        f0, f1 = min(fs), max(fs); d0, d1 = min(ds), max(ds)
        fspan = (f1 - f0) or 1.0; dspan = (d1 - d0) or 1.0

        def xy(f, d):
            x = pad + (f - f0) / fspan * (w - 2 * pad)
            y = h - pad - (d - d0) / dspan * (h - 2 * pad)
            return int(x), int(y)

        qp.setPen(QPen(col, 2))
        for i in range(1, len(self._pts)):
            x0, y0 = xy(*self._pts[i - 1]); x1, y1 = xy(*self._pts[i])
            qp.drawLine(x0, y0, x1, y1)


class _FreqResponsePlot(QWidget):
    """The big per-component frequency-response plot: the ΔdB(f) sweep with its measured
    points, vertical band markers at the signals' frequencies, and an evaluated dot on
    the curve at each. Δ negative = loss, positive = gain."""
    def __init__(self):
        super().__init__()
        self._table: list = []
        self._markers: list = []      # [(label, freq_hz, color), …]
        self.setMinimumHeight(190)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(),
                           self.sizePolicy().verticalPolicy())

    def set_data(self, table, markers) -> None:
        self._table = sorted(((float(a), float(b)) for a, b in (table or [])),
                             key=lambda p: p[0])
        self._markers = list(markers or [])
        self.update()

    def paintEvent(self, _evt) -> None:
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        L, R, T, B = 44, 12, 16, 26
        x0, x1, y0, y1 = L, W - R, T, H - B
        qp.fillRect(self.rect(), QColor(Palette.SURFACE))
        # axes
        qp.setPen(QPen(QColor(Palette.BORDER), 1))
        qp.drawLine(x0, y0, x0, y1)
        qp.drawLine(x0, y1, x1, y1)
        if not self._table:
            qp.setPen(QColor(Palette.TEXT_FAINT))
            qp.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                        "select a passive stage to see its frequency response")
            return
        fs = [f for f, _ in self._table]; ds = [d for _, d in self._table]
        # include marker freqs in the x-range so their lines land on the plot
        mfs = [m[1] for m in self._markers]
        fmin = min(fs + mfs); fmax = max(fs + mfs)
        if fmax == fmin:
            fmax = fmin + 1.0
        dmin = min(ds); dmax = max(ds)
        if dmax == dmin:
            dmin -= 0.5; dmax += 0.5
        dpad = (dmax - dmin) * 0.15
        dmin -= dpad; dmax += dpad

        def X(f):
            return x0 + (f - fmin) / (fmax - fmin) * (x1 - x0)

        def Y(d):
            return y1 - (d - dmin) / (dmax - dmin) * (y1 - y0)

        mono = QFont("monospace"); mono.setPointSize(8)
        qp.setFont(mono)
        # y grid + labels (dB)
        qp.setPen(QColor(Palette.TEXT_FAINT))
        for frac in (0.0, 0.5, 1.0):
            d = dmax - frac * (dmax - dmin)
            yy = int(Y(d))
            qp.setPen(QPen(QColor(Palette.SURFACE_ALT), 1))
            qp.drawLine(x0, yy, x1, yy)
            qp.setPen(QColor(Palette.TEXT_FAINT))
            qp.drawText(0, yy - 6, L - 6, 12,
                        int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                        f"{d:+.2f}")
        # x labels (GHz) at the ends + middle
        for frac in (0.0, 0.5, 1.0):
            f = fmin + frac * (fmax - fmin)
            xx = int(X(f))
            al = (Qt.AlignmentFlag.AlignHCenter if frac == 0.5 else
                  (Qt.AlignmentFlag.AlignLeft if frac == 0.0 else Qt.AlignmentFlag.AlignRight))
            qp.drawText(xx - 24, y1 + 4, 48, 14,
                        int(al | Qt.AlignmentFlag.AlignTop), f"{f/1e9:.2f}")
        # band markers (vertical dashed) + evaluated dots
        for label, freq, color in self._markers:
            xx = int(X(freq))
            pen = QPen(QColor(color), 1.5); pen.setDashPattern([3, 3])
            qp.setPen(pen)
            qp.drawLine(xx, y0, xx, y1)
            qp.setPen(QColor(color))
            fm = qp.fontMetrics()
            tw = min(fm.horizontalAdvance(label) + 6, x1 - x0)
            tx = max(x0, min(xx - tw // 2, x1 - tw))     # keep the label inside the axes
            qp.drawText(tx, y0 - 2, tw, 12,
                        int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom),
                        label)
        # the measured Δ(f) curve
        qp.setPen(QPen(QColor(Palette.ACCENT), 2.5))
        prev = None
        for f, d in self._table:
            pt = (int(X(f)), int(Y(d)))
            if prev is not None:
                qp.drawLine(prev[0], prev[1], pt[0], pt[1])
            prev = pt
        qp.setBrush(QColor(Palette.ACCENT)); qp.setPen(QColor(Palette.ACCENT))
        for f, d in self._table:
            qp.drawEllipse(int(X(f)) - 3, int(Y(d)) - 3, 6, 6)
        # evaluated dots on the curve at each marker freq
        for label, freq, color in self._markers:
            d = _interp_db(self._table, freq)
            qp.setBrush(QColor(color))
            qp.setPen(QPen(QColor(Palette.SURFACE), 1.5))
            qp.drawEllipse(int(X(freq)) - 4, int(Y(d)) - 4, 8, 8)


class _ClickCard(QFrame):
    """A QFrame that runs a callback when clicked — used for the chain stages and the
    component-library cards."""
    def __init__(self, on_click=None):
        super().__init__()
        self._on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._on_click is not None and event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
        super().mousePressEvent(event)


class _DragHandle(QLabel):
    """The grip on a chain stage. Dragging it runs a LIVE reorder: the real card follows
    the cursor and its neighbours slide aside as it crosses them (via the panel's
    ``on_start`` / ``on_move`` / ``on_end`` callbacks), so you see where it lands before
    letting go. Releasing finalises. Kept separate from the card's click-to-select so the
    two don't fight (accepting the press makes this widget the mouse grabber)."""
    def __init__(self, name: str, on_start, on_move, on_end):
        super().__init__("⠿")
        self._name = name
        self._on_start, self._on_move, self._on_end = on_start, on_move, on_end
        self._press = None                    # press-point, set once we own the press
        self._dragging = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to reorder this stage")
        self.setStyleSheet(f"color:{Palette.TEXT_FAINT};font-size:13px;")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        gp = event.globalPosition().toPoint()
        if not self._dragging:
            if (gp - self._press).manhattanLength() < QApplication.startDragDistance():
                return                        # below the threshold — a click, not a drag
            self._dragging = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self._on_start(self._name, gp)
        else:
            self._on_move(gp)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was = self._dragging
        self._press = None
        self._dragging = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if was:
            self._on_end()
        event.accept()


def _rename_plane_in_doc(doc: dict, old: str, new: str) -> dict:
    """Rename a plane throughout a calibration document: the chain.planes key (order
    preserved), operating_plane, every limit's plane, every derived plane's 'from', and
    each signal's curve keyed by this plane. Keeps the document internally consistent so
    a rename never leaves a dangling reference. Mutates and returns `doc`."""
    if old == new or not new:
        return doc
    chain = doc.get("chain") or {}
    planes = chain.get("planes")
    if isinstance(planes, dict) and old in planes:
        # rebuild preserving insertion order with the one key swapped
        chain["planes"] = {(new if k == old else k): v for k, v in planes.items()}
    if chain.get("operating_plane") == old:
        chain["operating_plane"] = new
    for lim in (chain.get("limits") or []):
        if isinstance(lim, dict) and lim.get("plane") == old:
            lim["plane"] = new
    for spec in (chain.get("planes") or {}).values():
        if isinstance(spec, dict) and spec.get("from") == old:
            spec["from"] = new
        if isinstance(spec, dict) and spec.get("of") == old:   # reported plane's basis
            spec["of"] = new
    for sig in (doc.get("signals") or {}).values():
        curves = (sig or {}).get("curves")
        if isinstance(curves, dict) and old in curves:
            sig["curves"] = {(new if k == old else k): v for k, v in curves.items()}
    return doc


def _template() -> dict:
    """A minimal, valid starting document (broadcaster, one measured plane)."""
    return {
        "schema_version": 1, "unit_id": "", "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "sdr_output",
            "limits": [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp P1dB input"}],
            "planes": {
                "sdr_output": {"type": "measured", "quantity": "total in-band power"},
            },
        },
        "defaults": {"amplitude": FIXED_BASEBAND_AMPLITUDE},
        "signals": {
            "mock": {"curves": {
                "sdr_output": {"points": [
                    {"gain_db": 40, "power_dbm": -36}, {"gain_db": 74, "power_dbm": -2.5}]}}},
        },
    }


# ── A small editable (gain, power) grid ─────────────────────────────────────────

class _CurveTable(QTableWidget):
    def __init__(self, on_changed=None, headers=("gain (dB)", "power (dBm)")):
        super().__init__(0, 2)
        self._on_changed = on_changed          # called after any edit (live feedback)
        self.setHorizontalHeaderLabels(list(headers))
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        # No persistent selection fill: clicking a cell (or arrow-keying to it) makes
        # it the CURRENT cell, and that outline is the only in-focus visual — it shows
        # only while the grid has focus, so nothing stays highlighted after you click
        # away. (NoSelection still supports a current cell + arrow-key navigation.)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        # Edit on a double-click or F2, or just by typing on the current cell.
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.AnyKeyPressed
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        # The current (focused) cell gets a clear accent outline — the only in-focus
        # visual, and it disappears on its own when the grid loses focus.
        self.setStyleSheet(
            f"QTableWidget::item:focus {{ background: {Palette.SURFACE}; "
            f"border: 1px solid {Palette.ACCENT}; }}")
        # Grow with the rows (up to a cap) so added points are always visible, rather
        # than hiding them behind an inner scrollbar in a fixed-height box.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.setToolTip("Each row is one measured point: the SDR gain you set and the "
                        "power you measured on this plane. Enter at least two points, "
                        "with gain AND power both strictly increasing.\n\n"
                        "Double-click a cell (or just type) to edit · Del clears the "
                        "current cell · Ctrl+Z / Ctrl+Y undo/redo · Esc or click away "
                        "to deselect · paste (Ctrl+V) a \"gain, power\" block from a "
                        "spreadsheet — it lands at the selected cell (or appends), and a "
                        "single column fills just that column · right-click to paste or "
                        "clear the whole table.")
        # Right-click menu: paste a measured sweep, or clear the whole table.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        # Undo/redo history of row snapshots (see _record_history / undo / redo).
        self._history: list = [[]]
        self._hist_idx = 0
        self._restoring = False
        self.cellChanged.connect(lambda *_: self._changed())
        # Clicking anywhere outside the grid (including out of its own cell editor)
        # drops the current cell, so no highlight lingers after clicking away.
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)
        self._fit_height()

    def _changed(self) -> None:
        self._record_history()
        if self._on_changed:
            self._on_changed()

    def numeric_points(self) -> list:
        """(gain, power) tuples for the sparkline — skips blank/non-numeric rows."""
        out = []
        for r in range(self.rowCount()):
            g = self.item(r, 0).text().strip() if self.item(r, 0) else ""
            p = self.item(r, 1).text().strip() if self.item(r, 1) else ""
            try:
                out.append((float(g), float(p)))
            except ValueError:
                continue
        return out

    def keyPressEvent(self, event) -> None:
        # Ctrl+V pastes spreadsheet rows: lines of "gain, power" (comma, tab, or
        # whitespace separated) become new points, so an operator can copy a measured
        # table straight in instead of retyping it cell by cell.
        if event.matches(QKeySequence.StandardKey.Paste) and self._paste_csv():
            return
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undo()
            return
        # Redo: the platform's standard chord, plus an explicit Ctrl+Y so it works the
        # same everywhere (StandardKey.Redo is Ctrl+Y on Windows but Ctrl+Shift+Z on
        # some Linux setups).
        if event.matches(QKeySequence.StandardKey.Redo) or (
                event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_Y):
            self.redo()
            return
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._deselect()               # Esc drops the current cell
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) \
                and self._clear_current_contents():
            return                         # Del/Backspace empties the current cell
        super().keyPressEvent(event)

    # ── Focus / current-cell ergonomics ──────────────────────────────────────

    def _on_focus_changed(self, _old, new) -> None:
        # When focus leaves the grid entirely (including its own cell editor), drop
        # the current cell so no highlight lingers after clicking away.
        if new is self or (new is not None and self.isAncestorOf(new)):
            return
        self.setCurrentCell(-1, -1)

    def _deselect(self) -> None:
        self.clearSelection()
        self.setCurrentCell(-1, -1)

    def _finish_edit(self) -> None:
        """Commit and close any open cell editor, so its value is saved before rows
        are added/removed underneath it (the +/− buttons are focus-less, so a click
        on them doesn't itself end the edit)."""
        if self.state() != QAbstractItemView.State.EditingState:
            return
        editor = self.focusWidget()
        if editor is not None and editor is not self:
            self.commitData(editor)
            self.closeEditor(editor, QAbstractItemDelegate.EndEditHint.NoHint)

    def _clear_current_contents(self) -> bool:
        it = self.currentItem()
        if it is None or it.text() == "":
            return False
        it.setText("")                     # fires cellChanged → history + sparkline
        return True

    # ── Undo / redo ──────────────────────────────────────────────────────────

    def _snapshot(self) -> list:
        return [((self.item(r, 0).text() if self.item(r, 0) else ""),
                 (self.item(r, 1).text() if self.item(r, 1) else ""))
                for r in range(self.rowCount())]

    def _reset_history(self) -> None:
        """Make the current grid the baseline (called after a programmatic load), so
        undo never reaches back past it."""
        self._history = [self._snapshot()]
        self._hist_idx = 0

    def _record_history(self) -> None:
        if self._restoring:
            return
        snap = self._snapshot()
        if snap == self._history[self._hist_idx]:
            return                         # nothing actually changed
        del self._history[self._hist_idx + 1:]     # a fresh edit drops the redo branch
        self._history.append(snap)
        self._hist_idx += 1
        cap = 200
        if len(self._history) > cap:
            drop = len(self._history) - cap
            del self._history[:drop]
            self._hist_idx -= drop

    def _restore(self, snap) -> None:
        self._restoring = True
        self.blockSignals(True)
        self.setRowCount(0)
        for g, p in snap:
            self._append(g, p)
        self.blockSignals(False)
        self._restoring = False
        self._fit_height()
        if self._on_changed:
            self._on_changed()             # refresh the sparkline, but don't re-record

    def undo(self) -> None:
        if self._hist_idx > 0:
            self._hist_idx -= 1
            self._restore(self._history[self._hist_idx])

    def redo(self) -> None:
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
            self._restore(self._history[self._hist_idx])

    @staticmethod
    def _is_num(s: str) -> bool:
        try:
            float(s)
            return True
        except (TypeError, ValueError):
            return False

    def _strip_trailing_blank_rows(self) -> int:
        """Drop fully-empty rows at the bottom (from an unfilled “+ point”), returning the
        resulting row count — the anchor for an append-style paste, so pasted data lands
        against the real points instead of below stray blanks."""
        r = self.rowCount() - 1
        while r >= 0:
            g = self.item(r, 0).text().strip() if self.item(r, 0) else ""
            p = self.item(r, 1).text().strip() if self.item(r, 1) else ""
            if g == "" and p == "":
                self.removeRow(r)
                r -= 1
            else:
                break
        return self.rowCount()

    def _paste_csv(self) -> bool:
        """Paste a spreadsheet block of gain/power values. Cells are comma-, tab-, or
        whitespace-separated; a leading header row ("gain … power …") is skipped. The block
        lands at the current cell (overwriting downward and extending rows, spreadsheet
        style); with no current cell it appends after the existing points. A single-column
        paste fills just the focused column, so you can drop in gains or powers on their own."""
        text = QApplication.clipboard().text()
        if not text or not text.strip():
            return False
        grid = []
        for line in text.splitlines():
            parts = [p for p in line.strip().replace(",", " ").replace("\t", " ").split()
                     if p != ""]
            if parts:
                grid.append(parts)
        if grid and not self._is_num(grid[0][0]):
            grid = grid[1:]                              # drop a header line
        if not grid:
            return False
        ncols = min(2, max(len(r) for r in grid))
        cur_r, cur_c = self.currentRow(), self.currentColumn()
        # A 2-column block always starts in the gain column; a 1-column paste fills the
        # focused column (gain or power), so you can paste a single measured column.
        start_col = 0 if ncols >= 2 else (cur_c if cur_c in (0, 1) else 0)
        start_row = cur_r if cur_r is not None and cur_r >= 0 \
            else self._strip_trailing_blank_rows()
        self.blockSignals(True)
        for i, fields in enumerate(grid):
            r = start_row + i
            while r >= self.rowCount():
                self._append()
            for j in range(min(ncols, len(fields))):
                c = start_col + j
                if c > 1:
                    break
                self.setItem(r, c, QTableWidgetItem(fields[j]))
        self.blockSignals(False)
        self._fit_height()
        self._changed()
        return True

    def clear_rows(self) -> None:
        """Empty the whole table (undoable). Fires the change hook so the sparkline and the
        owning form update. Used by the right-click 'Clear table' action, cross-program."""
        if self.rowCount() == 0:
            return
        self.blockSignals(True)
        self.setRowCount(0)
        self.blockSignals(False)
        self._fit_height()
        self._changed()

    def _context_menu(self, pos) -> None:
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        paste = menu.addAction("Paste (Ctrl+V)")
        clear = menu.addAction("Clear table")
        clear.setEnabled(self.rowCount() > 0)
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is paste:
            self._paste_csv()
        elif chosen is clear:
            self.clear_rows()

    def _fit_height(self) -> None:
        """Size the table to its rows (with a sensible min and max), so it expands as
        points are added instead of scrolling inside a squat box."""
        header = self.horizontalHeader().height()
        row_h = self.verticalHeader().defaultSectionSize()
        rows = max(self.rowCount(), 1)
        wanted = header + rows * row_h + 2 * self.frameWidth() + 2
        self.setMinimumHeight(min(wanted, header + 3 * row_h))   # show ~3 rows before scrolling
        self.setMaximumHeight(min(wanted, header + 12 * row_h))  # cap tall grids

    def set_points(self, points) -> None:
        # Display sorted by gain (the resolver sorts internally anyway) so the grid
        # reads in the order the curve is actually interpolated.
        def _key(pt):
            try:
                return (0, float(pt.get("gain_db")))
            except (TypeError, ValueError):
                return (1, 0.0)                # unparseable rows sink to the bottom
        self.blockSignals(True)
        self.setRowCount(0)
        for pt in sorted(points or [], key=_key):
            self._append(_numstr(pt.get("gain_db")), _numstr(pt.get("power_dbm")))
        self.blockSignals(False)
        self._fit_height()
        self._reset_history()   # the loaded points are the undo baseline

    def _append(self, g="", p="") -> None:
        r = self.rowCount()
        self.insertRow(r)
        self.setItem(r, 0, QTableWidgetItem(g))
        self.setItem(r, 1, QTableWidgetItem(p))

    def add_blank_row(self) -> None:
        self._finish_edit()
        self._append()
        self._fit_height()
        self._changed()
        # Land on the new row's first cell, ready to type straight away.
        r = self.rowCount() - 1
        if r >= 0:
            self.setCurrentCell(r, 0)

    def remove_selected(self) -> None:
        # Remove the selected rows; if nothing is selected, fall back to the current
        # row, then the last row — so "− point" always removes something rather than
        # silently doing nothing when the user hasn't clicked to select a whole row.
        self._finish_edit()
        rows = {i.row() for i in self.selectedItems()}
        if not rows and self.currentRow() >= 0:
            rows = {self.currentRow()}
        if not rows and self.rowCount() > 0:
            rows = {self.rowCount() - 1}
        for r in sorted(rows, reverse=True):
            self.removeRow(r)
        self._fit_height()
        self._changed()

    def points(self, strict: bool):
        """Return [{gain_db, power_dbm}], skipping fully-blank rows. strict=True raises
        on a partially-filled or non-numeric row; strict=False skips it."""
        out = []
        for r in range(self.rowCount()):
            g = self.item(r, 0).text().strip() if self.item(r, 0) else ""
            p = self.item(r, 1).text().strip() if self.item(r, 1) else ""
            if not g and not p:
                continue
            try:
                out.append({"gain_db": _to_float(g, f"row {r+1} gain"),
                            "power_dbm": _to_float(p, f"row {r+1} power")})
            except ValueError:
                if strict:
                    raise
        return out

    # Generic two-column accessors (used when this grid holds a freq→Δ dB table for a
    # component, not a gain→power curve).
    def set_rows(self, pairs) -> None:
        self.blockSignals(True)
        self.setRowCount(0)
        for x, y in (pairs or []):
            self._append(_numstr(x), _numstr(y))
        self.blockSignals(False)
        self._fit_height()
        self._reset_history()

    def rows(self, strict: bool) -> list:
        out = []
        for r in range(self.rowCount()):
            a = self.item(r, 0).text().strip() if self.item(r, 0) else ""
            b = self.item(r, 1).text().strip() if self.item(r, 1) else ""
            if not a and not b:
                continue
            try:
                out.append([_to_float(a, f"row {r+1} col 1"), _to_float(b, f"row {r+1} col 2")])
            except ValueError:
                if strict:
                    raise
        return out


class CalibrationPanel(QWidget):
    def __init__(self, hostname: str, hub, parent=None):
        super().__init__(parent)
        self.hostname = hostname
        self.hub = hub
        self._doc: Optional[dict] = None       # the working document model
        self._f: dict = {}                      # references to editor widgets
        self._expanded_signals: set = set()    # measured-detail signals shown expanded
        self._task_signal_ids: list = []       # signal ids this unit's tasks reference (hints)
        self._task_signals: dict = {}          # task name → SDR_CAL_SIGNAL_ID it references
        self._tasks_yaml: str = ""             # last-fetched tasks.yaml (for task renames)
        # Active-component editor: a script's numeric params, fetched per task on demand so the
        # control's "set param" picker can offer the linked task's parameters.
        self._task_params: dict = {}           # script basename → list[param spec]
        self._task_laws: dict = {}             # script basename → CAL_POWER_LAWS (declared laws)
        self._task_params_inflight: set = set()
        self._saved_doc: Optional[dict] = None  # the unit's last-persisted calibration doc
        # Canonical form snapshot taken whenever a whole document is loaded into the editor
        # (get/template/upload/save-refresh). has_unsaved_changes() compares the live form
        # against it, so the host can warn before the user leaves with unsaved edits.
        self._baseline: Optional[str] = None
        # The resolved per-signal --power ranges from the last Validate/Get, and a snapshot
        # of the document they were resolved from. While the document is unchanged, a form
        # rebuild keeps showing these ranges instead of reverting the table to "validate to
        # resolve" — that placeholder should appear only after a value is actually edited.
        self._resolved_signals: dict = {}
        self._resolved_key: Optional[str] = None
        # plane id → set of signal ids opened on a NON-source measured stage that don't
        # yet have data there (so the user can enter points). Reset on a fresh document.
        self._stage_extra: dict = {}
        self._drag: Optional[dict] = None       # in-flight chain drag state (see below)
        # The one fleet-wide component library, shared with the Library tab's Components
        # sub-tab so a part characterized anywhere is immediately available here. Falls
        # back to a private catalog only when the fleet can't supply one (test fakes).
        fleet = getattr(self.hub, "fleet", None)
        getter = getattr(fleet, "component_catalog", None)
        if callable(getter):
            self._catalog = getter()
        else:
            from state import ComponentCatalog
            self._catalog = ComponentCatalog()
        self._components_synced = False          # merged this unit's catalog on first load
        self._build()
        self.hub.task_done.connect(self._on_task_done)

    # ── layout ──────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._refresh_btn = QPushButton("Refresh"); self._refresh_btn.clicked.connect(self._refresh)
        self._validate_btn = QPushButton("Validate"); self._validate_btn.clicked.connect(self._on_validate)
        self._validate_btn.setToolTip("Check this document against the unit WITHOUT saving "
                                      "it — preview what each signal resolves to, or why "
                                      "it's rejected.")
        self._upload_btn = QPushButton("Upload…"); self._upload_btn.setObjectName("primary")
        self._upload_btn.clicked.connect(self._on_upload)
        self._save_btn = QPushButton("Save"); self._save_btn.clicked.connect(self._on_save)
        self._download_btn = QPushButton("Download…"); self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._on_download)
        self._json_btn = QPushButton("JSON…")
        self._json_btn.setToolTip("View or edit the raw calibration document — for anything "
                                  "the form doesn't cover. Applying it reloads the editor.")
        self._json_btn.clicked.connect(self._open_json)
        self._components_btn = QPushButton("Components…")
        self._components_btn.setToolTip("Open the component library — characterize cables "
                                        "and antennas once, then pick them in the chain.")
        self._components_btn.clicked.connect(self._open_components)
        for b in (self._refresh_btn, self._validate_btn, self._upload_btn,
                  self._save_btn, self._download_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self._json_btn)
        row.addWidget(self._components_btn)
        outer.addLayout(row)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(self._status)

        # Live local check — instant, structural, complementing the authoritative
        # agent Validate/Save. Hidden when the working document has no issues.
        self._issues = QLabel("")
        self._issues.setWordWrap(True)
        self._issues.setVisible(False)
        self._issues.setStyleSheet(f"font-size: 11px; color: {Palette.ARMED};")
        outer.addWidget(self._issues)

        # The resolved per-signal summary table now lives inside the editor's Signals
        # card (built in _build_editor_tab); _populate_table fills it after a get/validate.

        outer.addWidget(self._build_editor_tab(), stretch=1)

    # ── card scaffolding (matches the mockup's .card + header) ───────────────────
    def _make_card(self, *, number=None, title=None, desc=None, lbl=None, sub=None,
                   trailing: Optional[QWidget] = None):
        """A surface card with a header, returning (frame, body_layout). The header is
        either a numbered eyebrow (number ● title — desc) or an uppercase lbl · sub."""
        frame = QFrame(); frame.setObjectName("calcard")
        frame.setStyleSheet(
            f"QFrame#calcard {{ background: {Palette.SURFACE}; border: 1px solid "
            f"{Palette.BORDER}; border-radius: 10px; }}")
        outer = QVBoxLayout(frame); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)

        header = QWidget(); header.setObjectName("calhdr")
        header.setStyleSheet(f"#calhdr {{ border-bottom: 1px solid {Palette.BORDER}; }}")
        hb = QHBoxLayout(header); hb.setContentsMargins(14, 11, 14, 11); hb.setSpacing(10)
        if number is not None:
            num = QLabel(str(number)); num.setFixedSize(20, 20)
            num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            num.setStyleSheet(
                f"background: {Palette.ACCENT}; color: #fff; border-radius: 10px; "
                f"font-size: 11px; font-weight: 700;")
            hb.addWidget(num)
            txt = QLabel(f"<span style='color:{Palette.TEXT};font-weight:600;'>{title}</span>"
                         f" <span style='color:{Palette.TEXT_FAINT};'>{desc or ''}</span>")
            txt.setObjectName("cardtitle")
            txt.setTextFormat(Qt.TextFormat.RichText)
            hb.addWidget(txt)
        else:
            l = QLabel((lbl or "").upper())
            l.setStyleSheet(f"font-size: 11px; font-weight: 700; letter-spacing: .09em; "
                            f"color: {Palette.TEXT_FAINT};")
            hb.addWidget(l)
            if sub:
                s = QLabel(sub); s.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
                hb.addWidget(s)
        hb.addStretch(1)
        if trailing is not None:
            hb.addWidget(trailing)
        outer.addWidget(header)

        content = QWidget(); body = QVBoxLayout(content)
        body.setContentsMargins(14, 12, 14, 12); body.setSpacing(10)
        outer.addWidget(content)
        return frame, body

    def _build_editor_tab(self) -> QWidget:
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget(); self._editor_layout = QVBoxLayout(inner)
        self._editor_layout.setContentsMargins(2, 2, 2, 2)
        self._editor_layout.setSpacing(14)
        self._selected_plane: Optional[str] = None

        intro = QLabel(
            "Build this unit's RF chain from parts you characterized once. Passive stages "
            "become pickers — choose the cable and antenna you actually wired — and their "
            "loss is evaluated at each signal's frequency.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
        self._editor_layout.addWidget(intro)

        # ── Section 2: hardware chain (a left-to-right flow of stages) ───────────
        chain_card, chain_body = self._make_card(
            number="2", title="Hardware chain",
            desc="— drop in the parts you wired this unit with")
        chain_scroll = QScrollArea(); chain_scroll.setWidgetResizable(True)
        chain_scroll.setFrameShape(QFrame.Shape.NoFrame)
        chain_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        chain_scroll.setMinimumHeight(190)
        holder = QWidget(); self._chain_holder = holder
        self._chain_row = QHBoxLayout(holder)
        self._chain_row.setContentsMargins(0, 2, 0, 2); self._chain_row.setSpacing(0)
        chain_scroll.setWidget(holder)
        chain_body.addWidget(chain_scroll)
        self._editor_layout.addWidget(chain_card)

        # ── Section 3 + signals/limits: two columns ─────────────────────────────
        cols = QHBoxLayout(); cols.setSpacing(14)
        self._detail_card, self._detail_body = self._make_card(
            number="3", title="Stage detail", desc="— pick a stage above")
        self._detail_hdr = self._detail_card.findChild(QLabel, "cardtitle")  # title, not the "3"
        cols.addWidget(self._detail_card, 3)

        rightw = QWidget(); rcol = QVBoxLayout(rightw)
        rcol.setContentsMargins(0, 0, 0, 0); rcol.setSpacing(14)
        add_sig = QPushButton("+ Add signal…"); add_sig.clicked.connect(self._on_add_signal)
        add_sig.setStyleSheet("font-size: 11px;")
        sig_card, sig_body = self._make_card(
            lbl="Signals", sub="resolved --power at each frequency", trailing=add_sig)
        sig_body.setContentsMargins(0, 0, 0, 0)
        self._table = QTableWidget(0, 4)
        # "Range" is shown in the quantity chosen by the per-row "Shown in" dropdown — the
        # measured quantity the operator dials --power in, or (when the signal declares a
        # non-trivial limiting reading) the dBm quantity the safety ceiling is gauged in.
        self._table.setHorizontalHeaderLabels(["Signal", "Freq MHz", "Range", "Shown in"])
        self._table.verticalHeader().setVisible(False)
        # Only the Signal cell is editable — double-click it to rename the signal id (the
        # other columns are read-outs). See _populate_table for the per-cell flags.
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                    QAbstractItemView.EditTrigger.EditKeyPressed)
        self._table.itemChanged.connect(self._on_signal_item_changed)
        # No built-in (blue, persistent) selection: clicking a row still fires
        # cellClicked, but the highlight is painted by hand in _populate_table — with the
        # app's accent tint, and only while that signal's editor is actually open (see
        # _active_signal_ids). So nothing stays highlighted after you leave it.
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setMaximumHeight(180)
        self._table.setToolTip("Click a signal to open its measured curve for editing; "
                               "double-click its name to rename it.")
        self._table.cellClicked.connect(self._on_signal_row_clicked)
        sig_body.addWidget(self._table)
        rcol.addWidget(sig_card)

        add_lim = QPushButton("+ Add"); add_lim.clicked.connect(lambda: (self._add_limit_row(), None))
        add_lim.setStyleSheet("font-size: 11px;")
        lim_card, lim_body = self._make_card(lbl="Limits · ceiling", trailing=add_lim)
        lim_body.setContentsMargins(0, 0, 0, 4)
        self._limits_box = QVBoxLayout(); self._limits_box.setContentsMargins(0, 0, 0, 0)
        self._limits_box.setSpacing(0)
        lim_body.addLayout(self._limits_box)
        rcol.addWidget(lim_card)
        rcol.addStretch(1)
        cols.addWidget(rightw, 2)
        self._editor_layout.addLayout(cols)

        # ── Section 1: component library ────────────────────────────────────────
        fleet = QLabel("fleet-wide · deployed to units")
        fleet.setStyleSheet(f"font-size: 10px; font-weight: 700; letter-spacing: .06em; "
                            f"color: {Palette.ACCENT}; background: {Palette.ACCENT_SOFT}; "
                            f"padding: 3px 9px; border-radius: 5px;")
        lib_card, lib_body = self._make_card(
            number="1", title="Component library",
            desc="— characterize a part once; every unit reuses it", trailing=fleet)
        self._lib_grid = QGridLayout(); self._lib_grid.setSpacing(12)
        lib_body.addLayout(self._lib_grid)
        self._editor_layout.addWidget(lib_card)

        # ── Chain settings (gains / operating / defaults — needed by the resolver) ─
        self._f["unit_type"] = QComboBox()
        for t in UNIT_TYPES:
            self._f["unit_type"].addItem(UNIT_TYPE_LABELS.get(t, t), t)
        self._f["unit_type"].setToolTip(
            "This unit's hardware type. It selects the shared type-defaults chain that's "
            "merged in, so it must match the real unit — a wrong type silently mis-resolves.")
        self._f["min_gain"] = QLineEdit(); self._f["max_gain"] = QLineEdit()
        self._f["gain_step"] = QLineEdit()
        self._f["gain_step"].setPlaceholderText("optional, e.g. 0.25")
        self._f["min_gain"].setToolTip("Lowest usable SDR internal gain (dB).")
        self._f["max_gain"].setToolTip(
            "Highest SDR gain the safety ceilings allow (usually the amp's P1dB gain).")
        self._f["gain_step"].setToolTip(
            "SDR gain step (dB), optional. The radio only settles on a discrete gain grid "
            "(e.g. 0.25 dB); set it and calibration snaps the commanded gain to the nearest "
            "step — never above the ceiling — so delivered power matches. Blank = continuous.")
        # Baseband amplitude is fixed fleet-wide (FIXED_BASEBAND_AMPLITUDE) and owned by the
        # scripts, so it is NOT an editable field here — it is recorded on save and any
        # legacy mismatch is flagged by local_calibration_issues.
        set_card, set_body = self._make_card(
            lbl="Chain settings", sub="gain range · defaults")
        form = QFormLayout(); form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Unit type", self._f["unit_type"])
        form.addRow("Min gain (dB)", self._f["min_gain"])
        form.addRow("Max gain (dB)", self._f["max_gain"])
        form.addRow("Gain step (dB)", self._f["gain_step"])
        set_body.addLayout(form)
        self._editor_layout.addWidget(set_card)

        # Empty-state hint / template button (shown when there's no document).
        self._empty_hint = QPushButton("New from template")
        self._empty_hint.clicked.connect(self._on_new_template)
        self._editor_layout.addWidget(self._empty_hint, alignment=Qt.AlignmentFlag.AlignLeft)
        self._editor_layout.addStretch(1)

        scroll.setWidget(inner)
        return scroll

    def _open_json(self) -> None:
        """View / edit the raw calibration document in a dialog. The editor form is the
        primary surface; this is the escape hatch for anything the form doesn't cover.
        Applying valid JSON replaces the working document and rebuilds the editor."""
        try:                                     # fold current form edits in first
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox
        dlg = QDialog(self.window())
        dlg.setWindowTitle("Calibration document · JSON")
        dlg.setMinimumSize(700, 560)
        v = QVBoxLayout(dlg); v.setSpacing(8)
        intro = QLabel("The raw calibration document. Edit here for anything the form "
                       "doesn't surface — “Apply” parses it back into the editor.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        v.addWidget(intro)
        view = QPlainTextEdit(); view.setFont(QFont("monospace"))
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        view.setPlainText(json.dumps(self._doc, indent=2) if self._doc is not None else "")
        v.addWidget(view, 1)
        err = QLabel(""); err.setWordWrap(True)
        err.setStyleSheet(f"font-size:11px;color:{Palette.CRASH};")
        v.addWidget(err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        v.addWidget(bb)

        def _apply():
            msg = self._apply_json_text(view.toPlainText())
            if msg:
                err.setText(msg)
            else:
                dlg.accept()
        bb.accepted.connect(_apply)
        bb.rejected.connect(dlg.reject)
        dlg.exec()

    def _apply_json_text(self, text: str) -> Optional[str]:
        """Parse raw JSON into the working document and rebuild the editor. Returns an
        error message on failure (document left unchanged), or None on success."""
        text = (text or "").strip()
        if not text:
            return "the document is empty"
        try:
            doc = json.loads(text)
        except ValueError as exc:
            return f"not valid JSON: {exc}"
        self._doc = doc
        self._download_btn.setEnabled(doc is not None)
        self._doc_to_form()
        return None

    # ── model → views ────────────────────────────────────────────────────────────
    def _set_doc(self, doc: Optional[dict]) -> None:
        self._doc = doc
        self._stage_extra = {}                  # a fresh document → forget transient adds
        self._download_btn.setEnabled(doc is not None)
        self._doc_to_form()
        # A whole document was just loaded into the editor → this is the clean baseline
        # against which later edits count as unsaved (see has_unsaved_changes).
        self._baseline = self._canonical_form()

    def _canonical_form(self) -> Optional[str]:
        """A stable JSON snapshot of the live form, or None if it can't be read. Insertion
        order is preserved (not sort_keys) so a stage reorder — which is a real edit — is
        detected, not normalised away."""
        try:
            return json.dumps(self._read_form(strict=False), default=str)
        except Exception:  # noqa: BLE001 — a mid-edit form that won't read is treated as dirty
            return None

    def _remember_resolved(self, signals: dict) -> None:
        """Cache the resolved --power ranges against a snapshot of the current form (the
        same read_form basis as has_unsaved_changes), so a later rebuild that leaves the
        form unchanged keeps showing them instead of reverting to 'validate to resolve'."""
        self._resolved_signals = dict(signals or {})
        self._resolved_key = self._canonical_form()

    def has_unsaved_changes(self) -> bool:
        """True when the editor holds edits not yet saved to the unit. Compares the live
        form to the baseline captured at the last load/save. Used by the host to warn
        before the user leaves the Calibration tab."""
        if self._baseline is None:
            return False
        cur = self._canonical_form()
        return cur is None or cur != self._baseline

    def _plane_names(self):
        planes = ((self._doc or {}).get("chain") or {}).get("planes") or {}
        return list(planes.keys())

    def _measured_planes(self):
        planes = ((self._doc or {}).get("chain") or {}).get("planes") or {}
        return [n for n, p in planes.items() if isinstance(p, dict) and p.get("type") == "measured"]

    def _doc_to_form(self) -> None:
        """Rebuild every editor widget from the model, then render the chain flow, the
        selected-stage detail, the resolved signals table, the limits, and the component
        library. A full rebuild each time keeps widget lifetimes simple (see
        _select_plane)."""
        self._syncing = True
        doc = self._doc
        have = doc is not None
        self._empty_hint.setVisible(not have)
        if not have:
            utype, _ = self._unit_meta()
            self._empty_hint.setText(f"New from {utype} template")
        chain = (doc or {}).get("chain") or {}
        gl = chain.get("gain_limits") or {}
        self._f["min_gain"].setText(_numstr(gl.get("min_gain_db")))
        self._f["max_gain"].setText(_numstr(gl.get("max_gain_db")))
        self._f["gain_step"].setText(_numstr(gl.get("gain_step_db")))

        ut = (doc or {}).get("unit_type", "")
        i = self._f["unit_type"].findData(ut)
        if i >= 0:
            self._f["unit_type"].setCurrentIndex(i)
        elif ut:                                    # a type we don't have a label for
            self._f["unit_type"].addItem(ut, ut)
            self._f["unit_type"].setCurrentIndex(self._f["unit_type"].count() - 1)

        # plane rows + signal entries: create the editable widgets (the chain/detail
        # render decides where the selected ones are shown).
        self._f["planes"] = [self._make_plane_row(n, s or {})
                             for n, s in (chain.get("planes") or {}).items()]
        self._f["signals"] = {}
        self._spark_src = {}                # sparkline → its source curve table
        measured = self._measured_planes()
        for sid, sig in ((doc or {}).get("signals") or {}).items():
            self._build_signal_entry(sid, sig or {}, measured)

        # MIGRATE a legacy operating-plane shared `limiting` default into each signal that
        # lacks its own (docs/calibration-ui-redesign §5: no stage-level shared defaults).
        # The resolver read per-signal first with the plane as fallback, so this is
        # semantics-preserving; on save the per-signal blocks are written and the plane
        # default is dropped (_read_planes no longer emits plane readings).
        plane_specs = (chain.get("planes") or {})
        if plane_specs:
            op_name = list(plane_specs)[-1]
            op_lim = (plane_specs.get(op_name) or {}).get("limiting")
            if isinstance(op_lim, dict) and op_lim:
                seed = copy.deepcopy(op_lim)
                seed.pop("max_dbm", None)      # the per-signal ceiling is gone (§5/§6.6);
                for entry in self._f["signals"].values():   # the stage limits list caps now
                    if not entry["reading"]["limiting"]:
                        entry["reading"]["limiting"] = copy.deepcopy(seed)

        names = self._plane_names()
        op = names[-1] if names else None       # operating plane = the last stage, always

        # limits
        self._clear_layout(self._limits_box)
        self._f["limits"] = []
        for lim in (chain.get("limits") or []):
            self._add_limit_row(lim)
        if not (chain.get("limits") or []):
            hint = QLabel("no ceiling yet — add one (the unit refuses to transmit "
                          "without a safety ceiling)")
            hint.setWordWrap(True)
            hint.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};padding:10px 14px;")
            self._limits_box.addWidget(hint)

        self._syncing = False

        # keep the current selection if the plane still exists, else operating / first
        if self._selected_plane not in names:
            self._selected_plane = op if op in names else (names[0] if names else None)

        self._render_chain()
        self._render_detail()
        self._render_library()

        # Show the document's signals so they're always clickable. Keep the resolved --power
        # ranges from the last Validate/Get as long as the form is unchanged from when they
        # were resolved (compared on the same read_form basis as has_unsaved_changes, so
        # navigation that re-reads the form doesn't count) — only a real edit reverts the
        # column to "validate to resolve". Populated last, once the whole form is built.
        cur_sigs = (doc or {}).get("signals") or {}
        key = self._canonical_form()
        if (self._resolved_key is not None and key is not None and key == self._resolved_key
                and set(self._resolved_signals) >= set(cur_sigs)):
            self._populate_table(self._resolved_signals, resolved=True)
        else:
            self._populate_table({sid: {} for sid in cur_sigs}, resolved=False)
        self._update_issues()
        self._sync_validate_button()

    # ── representative frequency (for stage values / plots) ──────────────────────
    def _rep_freq(self) -> float:
        """A representative transmit frequency for previewing passive-hop dB: the first
        signal that declares a centre frequency, else 1.5 GHz (mid GNSS band)."""
        for sig in ((self._doc or {}).get("signals") or {}).values():
            f = (sig or {}).get("center_freq_hz")
            if f:
                try:
                    return float(f)
                except (TypeError, ValueError):
                    pass
        return 1.5e9

    def _signal_markers(self):
        """Band markers for the frequency plot: (label, freq_hz, colour), one per distinct
        centre frequency. Signals that share a frequency are merged into a single dashed
        line with their labels combined, so overlapping signals don't stack invisibly.
        Each signal's label is its chosen plot_label, else a short form of its id."""
        cols = [Palette.ACCENT, Palette.ONLINE, Palette.ARMED, Palette.TEXT_MUTED]
        groups: dict = {}                         # rounded freq → {"freq", "labels"}
        order: list = []
        for sid, sig in sorted(((self._doc or {}).get("signals") or {}).items()):
            f = (sig or {}).get("center_freq_hz")
            if not f:
                continue
            try:
                fval = float(f)
            except (TypeError, ValueError):
                continue
            label = ((sig or {}).get("plot_label") or "").strip() \
                or (sid.split("_")[-1][:6] or sid[:6])
            key = round(fval, 3)                   # merge near-identical frequencies
            if key not in groups:
                groups[key] = {"freq": fval, "labels": []}
                order.append(key)
            groups[key]["labels"].append(label)
        out = []
        for i, key in enumerate(order):
            g = groups[key]
            labels = g["labels"]
            lab = " · ".join(labels) if len(labels) <= 2 else f"{labels[0]} +{len(labels) - 1}"
            out.append((lab, g["freq"], cols[i % len(cols)]))
        return out

    def _comp_table(self, comp_id: str):
        spec = self._catalog.get(comp_id) if comp_id else None
        return (spec or {}).get("delta_db_by_freq") or []

    def _update_issues(self) -> None:
        """Recompute the instant local structural check from the current widgets and
        show the top few problems (or hide the panel when the document is clean)."""
        try:
            doc = self._read_form(strict=False)
        except ValueError:
            return
        issues = local_calibration_issues(doc)
        if not issues:
            self._issues.setVisible(False)
            self._issues.clear()
            return
        shown = issues[:6]
        more = f"  (+{len(issues) - len(shown)} more)" if len(issues) > len(shown) else ""
        self._issues.setText("⚠ " + " · ".join(shown) + more)
        self._issues.setVisible(True)

    def _add_limit_row(self, lim: Optional[dict] = None) -> None:
        lim = lim or {}
        w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        plane = QComboBox(); plane.addItems(self._plane_names())
        plane.setToolTip("The stage this ceiling protects.")
        if lim.get("plane") in self._plane_names():
            plane.setCurrentText(lim["plane"])
        side = QComboBox(); side.addItems(["output", "input"])
        side.setToolTip(
            "Which boundary of that stage the cap applies at.\n"
            "• output — the plane itself (e.g. a licence EIRP cap on the antenna).\n"
            "• input — the plane feeding the stage (e.g. an amp's max input power). An\n"
            "  input limit follows its stage: insert a component upstream and it stays put,\n"
            "  no need to restate it on the new part.")
        side.setCurrentText(lim.get("side", "output") if lim.get("side") in ("input", "output")
                            else "output")
        max_dbm = QLineEdit(_numstr(lim.get("max_dbm"))); max_dbm.setPlaceholderText("max dBm")
        max_dbm.setToolTip("Maximum power (dBm) permitted at that stage boundary.")
        reason = QLineEdit(lim.get("reason", "")); reason.setPlaceholderText("reason (optional)")
        reason.setToolTip("Why this ceiling exists — e.g. “amp P1dB input”, "
                          "“licence EIRP cap”. Shown for context only.")
        rm = QPushButton("✕"); rm.setFixedWidth(28)
        for wdg, s in ((plane, 2), (side, 1), (max_dbm, 1), (reason, 3)):
            h.addWidget(wdg, s)
        h.addWidget(rm)
        row = {"w": w, "plane": plane, "side": side, "max": max_dbm, "reason": reason}
        rm.clicked.connect(lambda: self._remove_row(self._limits_box, self._f["limits"], row))
        self._limits_box.addWidget(w)
        self._f["limits"].append(row)

    def _on_add_plane(self) -> None:
        try:
            self._sync_from(strict=False)
        except ValueError:
            pass
        if self._doc is None:
            self._doc = self._blank_doc()
        planes = self._doc.setdefault("chain", {}).setdefault("planes", {})
        nm, i = "plane", 1
        while nm in planes:
            i += 1; nm = f"plane{i}"
        planes[nm] = {"type": "measured"}
        self._download_btn.setEnabled(True)
        self._doc_to_form()

    def _make_plane_row(self, name: str = "", spec: Optional[dict] = None) -> dict:
        """Create (but do not place) the editable widgets for one chain stage, returning
        the row dict _read_planes reads. The chain is an ordered LINEAR sequence: a
        stage's parent is the stage before it and the operating plane is always the last
        stage, so there is no parent picker and no operating control. A stage is one of:
          • measured  — a gain→power curve measured on this box (SDR, amp),
          • component  — a library component (its Δ dB(f) fixed at add-time), or
          • constant   — an inline constant Δ dB (editable).
        The role is fixed when the stage is added; to change a stage's part, add a new
        stage and remove the old one, then drag/move it into place."""
        spec = spec or {}
        if spec.get("type") == "derived":
            if spec.get("control") is not None:
                role = "active"
            elif spec.get("component"):
                role = "component"
            else:
                role = "constant"
        else:
            role = "measured"
        name_e = QLineEdit(name); name_e.setPlaceholderText("plane id (e.g. antenna_eirp)")
        name_e.setToolTip("A short id for this stage, e.g. sdr_output, amplifier_output, "
                          "antenna_eirp. Renaming it re-points everything that references it.")
        delta_e = QLineEdit(_numstr(spec.get("delta_db"))); delta_e.setPlaceholderText("Δ dB")
        delta_e.setToolTip("Constant offset from the previous stage, in dB. Negative = "
                           "loss (cable/pad), positive = gain (antenna).")
        delta_e.editingFinished.connect(self._refresh_form_from_widgets)
        # "orig" is the plane's last-committed name, so a rename propagates to everything
        # that references it instead of silently dangling.
        # cal_role: a MEASURED stage is "limiting" (default — safety limits gauge on it) or
        # "reported" (report-only; limits punch through it to the nearest limiting stage
        # upstream, derived automatically at save — see _auto_of / docs/calibration.md §4.1).
        cal_role = spec.get("role", "limiting") if role == "measured" else "limiting"
        row = {"name": name_e, "role": role, "comp_id": spec.get("component") or "",
               "delta": delta_e, "orig": name,
               "cal_role": cal_role if cal_role == "reported" else "limiting",
               # Bypass: a stage physically pulled without deleting it — resolves as a
               # transparent 0-dB hop with its limits dropped. Every stage but the source.
               "bypass": bool(spec.get("bypass"))}
        if role == "active":
            c = spec.get("control") or {}
            row["control"] = {
                "task": str(c.get("task", "")), "param": str(c.get("param", "")),
                "sense": c.get("sense", "attenuation"),
                "min_db": _numeric(c.get("min_db"), 0.0), "max_db": _numeric(c.get("max_db"), 0.0),
                "step_db": _numeric(c.get("step_db"), 0.0),
                "engage_pct": _numeric(c.get("engage_pct"), 0.0),
                # Other params of the set-task sent unchanged on every set (e.g. an attenuator's
                # serial port): {dest: value_string}. The driving param is not among them.
                "consts": {str(k): str(v) for k, v in (c.get("consts") or {}).items()}}
            # The active component's OWN baseline insertion loss: a frequency table it owns
            # inline (not a shared library part). Empty ⇒ a flat constant (row["delta"]).
            row["baseline_table"] = [list(p) for p in (spec.get("delta_db_by_freq") or [])]
        # Measurement DE-EMBED (docs/calibration-v2.md §14) on a measured stage: the cable/pad
        # between it and the analyzer, removed from the reading. A catalog component id is
        # picker-editable; a non-string inline table is preserved (JSON-only, advanced).
        dm = spec.get("measurement_deembed")
        row["deembed"] = dm if isinstance(dm, str) else ""
        row["deembed_custom"] = dm if (dm is not None and not isinstance(dm, str)) else None
        name_e.editingFinished.connect(lambda r=row: self._on_plane_name_changed(r))
        return row

    # ── chain flow (mockup section 2) ────────────────────────────────────────────
    def _render_chain(self) -> None:
        self._clear_layout(self._chain_row)
        self._drag = None                        # any in-flight drag is stale after a rebuild
        rows = self._f.get("planes", [])
        rep = self._rep_freq()
        n = len(rows)
        # Each stage is a "slot" = its card plus a trailing "→", so a live reorder moves
        # the card and its arrow as one unit and the flow always reads left-to-right.
        self._chain_slots = []                   # [(plane_name, slot_widget)], chain order
        # Source-bias stage (unit-owned, BEFORE the source) when the agent supports it and a
        # chain exists. It's not a plane — it edits doc['source_bias'] directly.
        if n and self._supports(CAL_SOURCE_BIAS_CAPABILITY):
            bslot = QWidget()
            bsl = QHBoxLayout(bslot); bsl.setContentsMargins(0, 0, 0, 0); bsl.setSpacing(0)
            bsl.addWidget(self._source_bias_card())
            barrow = QLabel("→")
            barrow.setStyleSheet(f"color:{Palette.BORDER_STRONG};font-size:18px;")
            barrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
            barrow.setFixedWidth(26)
            bsl.addWidget(barrow)
            self._chain_row.addWidget(bslot)
        for i, row in enumerate(rows):
            slot = QWidget()
            sl = QHBoxLayout(slot); sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(0)
            sl.addWidget(self._stage_card(row, i, n, rep))
            arrow = QLabel("→")
            arrow.setStyleSheet(f"color:{Palette.BORDER_STRONG};font-size:18px;")
            arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
            arrow.setFixedWidth(26)
            sl.addWidget(arrow)
            self._chain_row.addWidget(slot)
            self._chain_slots.append((row["name"].text().strip(), slot))
        if n:                                    # mark the operating plane beside the end
            self._chain_row.addWidget(self._operating_marker())
        self._chain_row.addWidget(self._add_stage_card())
        self._chain_row.addStretch(1)

    def _operating_marker(self) -> QWidget:
        """The “operating plane” callout, placed to the RIGHT of the last stage (not on it):
        the last stage's output is where an absolute --power value is read. Kept as its own
        fixed element so it doesn't travel with a card during a drag reorder."""
        marker = QWidget()
        marker.setToolTip("Operating plane — where an absolute --power value is read "
                          "(the output of the last stage). It's always the final stage.")
        col = QVBoxLayout(marker); col.setContentsMargins(2, 0, 10, 0); col.setSpacing(0)
        col.addStretch(1)
        inner = QHBoxLayout(); inner.setSpacing(6)
        arrow = QLabel("◀")
        arrow.setStyleSheet(f"color:{Palette.ONLINE};font-size:16px;font-weight:700;")
        pill = QLabel("--power\nreads here")
        pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pill.setStyleSheet(f"color:#fff;background:{Palette.ONLINE};font-size:10px;"
                           f"font-weight:700;padding:4px 9px;border-radius:8px;")
        inner.addWidget(arrow); inner.addWidget(pill)
        col.addLayout(inner)
        col.addStretch(1)
        return marker

    def _add_stage_card(self) -> QWidget:
        """The dashed “+ Add stage” tile at the end of the chain."""
        card = _ClickCard(on_click=self._add_stage)
        card.setObjectName("addstage")
        card.setStyleSheet(f"#addstage {{ border:1px dashed {Palette.BORDER_STRONG}; "
                           f"border-radius:10px; }}")
        card.setMinimumWidth(150); card.setMaximumWidth(180)
        v = QVBoxLayout(card); v.setContentsMargins(12, 12, 12, 12)
        plus = QLabel("+"); plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus.setStyleSheet(f"font-size:22px;color:{Palette.ACCENT};font-weight:600;")
        t = QLabel("Add stage"); t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet(f"font-size:13px;font-weight:600;color:{Palette.ACCENT};")
        h = QLabel("component, measured\nor constant"); h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        v.addStretch(1); v.addWidget(plus); v.addWidget(t); v.addWidget(h); v.addStretch(1)
        return card

    def _stage_card(self, row, index: int, total: int, rep_freq: float) -> QWidget:
        name = row["name"].text().strip()
        role = row.get("role", "measured")
        kind = ("source" if index == 0 else "active" if role == "active"
                else "passive" if role != "measured" else "measured")
        selected = (name == self._selected_plane)
        # The operating plane (always the last stage) is marked by a callout to the RIGHT
        # of the chain, not by styling this card — so a card doesn't flash "operating"
        # while being dragged past the end. Cards are visually uniform bar the selection.
        border = Palette.ACCENT if selected else Palette.BORDER
        bg = Palette.SURFACE if selected else Palette.SURFACE_ALT
        card = _ClickCard(on_click=lambda n=name: self._select_plane(n))
        card.setObjectName("stage")
        card.setStyleSheet(f"#stage {{ background:{bg}; border:1px solid {border}; "
                           f"border-radius:10px; }}")
        card.setMinimumWidth(178); card.setMaximumWidth(230)
        v = QVBoxLayout(card); v.setContentsMargins(12, 10, 12, 12); v.setSpacing(7)

        # top row: drag grip + move ◀▶ handles (none on the source)
        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0)
        if index > 0:
            top.addWidget(_DragHandle(name, self._chain_drag_start,
                                      self._chain_drag_move, self._chain_drag_end))
        top.addStretch(1)
        # Bypass: every stage but the source (index 0) can be pulled from the chain without
        # deleting it. Gated on the agent capability (an older agent would reject bypass).
        if index > 0 and self._supports(CAL_STAGE_BYPASS_CAPABILITY):
            byp = QCheckBox("bypass")
            byp.setChecked(bool(row.get("bypass")))
            byp.setToolTip("Bypass this stage — treat it as if it weren't there (0 dB, its "
                           "safety limits don't apply), without deleting it.")
            byp.setStyleSheet("font-size:10px;")
            byp.toggled.connect(lambda ck, r=row: self._toggle_bypass(r, ck))
            top.addWidget(byp)
        if index > 0:                             # the source stage stays first
            for glyph, delta, en in (("◀", -1, index > 1), ("▶", +1, index < total - 1)):
                mv = QPushButton(glyph); mv.setFixedSize(20, 20)
                mv.setFocusPolicy(Qt.FocusPolicy.NoFocus); mv.setEnabled(en)
                mv.setStyleSheet("font-size:10px;padding:0;")
                mv.setToolTip("Move this stage earlier" if delta < 0 else "Move this stage later")
                mv.clicked.connect(lambda _=False, r=row, d=delta: self._move_stage(r, d))
                top.addWidget(mv)
        v.addLayout(top)

        fg, kbg = _KIND_COLORS[kind]
        badge_text = {"source": "SOURCE", "measured": "MEASURED",
                      "passive": "Passive · from library", "active": "ACTIVE"}[kind]
        if kind == "measured" and row.get("cal_role") == "reported":
            badge_text = "REPORTED"           # report-only: invisible to safety limits
        bypassed = index > 0 and bool(row.get("bypass"))
        if bypassed:                          # translucent, transparent 0-dB, limits dropped
            badge_text = f"{badge_text} · BYPASSED"
            fg, kbg = Palette.TEXT_FAINT, Palette.SURFACE_ALT
        v.addWidget(_badge(badge_text, fg, kbg), alignment=Qt.AlignmentFlag.AlignLeft)

        title = QLabel(name or "(unnamed)")
        title.setStyleSheet(f"font-size:13px;font-weight:600;color:{Palette.TEXT};")
        v.addWidget(title)

        if role == "component":
            comp = row.get("comp_id", "")
            cn = QLabel(comp or "(missing component)")
            cn.setStyleSheet(f"font-size:12px;font-weight:500;color:{Palette.TEXT};")
            cn.setWordWrap(True)
            v.addWidget(cn)
            db = _interp_db(self._comp_table(comp), rep_freq)
            val = QLabel(f"{db:+.2f} dB  @ {rep_freq/1e6:.0f} MHz")
            val.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
            v.addWidget(val)
        elif role == "constant":
            d = row["delta"].text().strip()
            val = QLabel(f"{d or '0'} dB · constant")
            val.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
            v.addWidget(val)
        elif role == "active":
            c = row.get("control") or {}
            task, param = c.get("task", ""), c.get("param", "")
            cn = QLabel(f"{task}·{param}" if task or param else "(set task · param)")
            cn.setStyleSheet(f"font-size:12px;font-weight:500;color:{Palette.TEXT};")
            cn.setWordWrap(True)
            v.addWidget(cn)
            lo, hi, st = (_numeric(c.get("min_db"), 0.0), _numeric(c.get("max_db"), 0.0),
                          _numeric(c.get("step_db"), 0.0))
            val = QLabel(f"{lo:g}…{hi:g} dB · {st:g} step")
            val.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
            v.addWidget(val)
        else:
            spark = _Sparkline(); spark.setFixedHeight(30)
            spark.set_points(self._first_curve_points(name))
            v.addWidget(spark)
            hint = QLabel("gain → power · this unit")
            hint.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
            v.addWidget(hint)
        v.addStretch(1)
        if bypassed:                          # dim the whole card so it reads as "not there"
            from PyQt6.QtWidgets import QGraphicsOpacityEffect
            eff = QGraphicsOpacityEffect(card)
            eff.setOpacity(0.42)
            card.setGraphicsEffect(eff)
        return card

    def _toggle_bypass(self, row, checked: bool) -> None:
        """Bypass / un-bypass a stage. The row model carries the flag (serialized by
        _read_planes); the chain re-render + re-validate is deferred to the next event-loop
        turn so the toggled checkbox isn't destroyed mid-signal."""
        if self._syncing:
            return
        row["bypass"] = bool(checked)
        self._download_btn.setEnabled(True)          # a real edit — Save/Download now live
        from PyQt6.QtCore import QTimer

        def _after():
            self._render_chain()
            self._render_detail()
            self._update_issues()
        QTimer.singleShot(0, _after)

    # ── source-bias stage (unit-owned, before the source) ────────────────────────
    def _source_bias_card(self) -> QWidget:
        """The leading 'Source bias' card: the SDR's power-vs-frequency flatness, edited as a
        freq→dBm table on doc['source_bias']. Not a plane — one per unit."""
        sb = (self._doc or {}).get("source_bias") or {}
        pts = sb.get("power_by_freq") or []
        bypassed = bool(sb.get("bypass")) and bool(pts)
        card = _ClickCard(on_click=self._edit_source_bias)
        card.setObjectName("stage")
        card.setStyleSheet(f"#stage {{ background:{Palette.SURFACE_ALT}; "
                           f"border:1px dashed {Palette.BORDER_STRONG}; border-radius:10px; }}")
        card.setMinimumWidth(170); card.setMaximumWidth(215)
        v = QVBoxLayout(card); v.setContentsMargins(12, 10, 12, 12); v.setSpacing(7)
        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0); top.addStretch(1)
        if pts and self._supports(CAL_STAGE_BYPASS_CAPABILITY):
            byp = QCheckBox("bypass"); byp.setChecked(bypassed); byp.setStyleSheet("font-size:10px;")
            byp.setToolTip("Bypass the source bias — apply no frequency correction.")
            byp.toggled.connect(self._toggle_bias_bypass)
            top.addWidget(byp)
        v.addLayout(top)
        text = "SOURCE BIAS" + (" · BYPASSED" if bypassed else "")
        fg, kbg = ((Palette.TEXT_FAINT, Palette.SURFACE_ALT) if bypassed
                   else (Palette.ACCENT, Palette.ACCENT_SOFT))
        v.addWidget(_badge(text, fg, kbg), alignment=Qt.AlignmentFlag.AlignLeft)
        title = QLabel("SDR flatness")
        title.setStyleSheet(f"font-size:13px;font-weight:600;color:{Palette.TEXT};")
        v.addWidget(title)
        summ = QLabel(f"{len(pts)} point(s) · dBm(f)" if pts else "not set — click to add")
        summ.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
        v.addWidget(summ)
        edit = QPushButton("Edit table…"); edit.setStyleSheet("font-size:11px;")
        edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        edit.clicked.connect(self._edit_source_bias)
        v.addWidget(edit)
        v.addStretch(1)
        if bypassed:
            from PyQt6.QtWidgets import QGraphicsOpacityEffect
            eff = QGraphicsOpacityEffect(card); eff.setOpacity(0.42); card.setGraphicsEffect(eff)
        return card

    def _toggle_bias_bypass(self, checked: bool) -> None:
        if self._syncing:
            return
        sb = (self._doc or {}).get("source_bias")
        if not isinstance(sb, dict):
            return
        if checked:
            sb["bypass"] = True
        else:
            sb.pop("bypass", None)
        self._download_btn.setEnabled(True)
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._render_chain)

    def _edit_source_bias(self) -> None:
        """Modal freq→dBm table editor for the per-unit source bias. Frequencies are entered
        in MHz; stored as Hz in doc['source_bias']['power_by_freq']."""
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox
        if self._doc is None:
            self._doc = self._blank_doc()
        sb = dict((self._doc.get("source_bias") or {}))
        dlg = QDialog(self)
        dlg.setWindowTitle("Source bias — SDR power vs frequency")
        lay = QVBoxLayout(dlg)
        info = QLabel(
            "Transmit a fixed-gain CW and read the delivered power at each frequency, then "
            "enter frequency (MHz) + measured power (dBm). The bias is normalized to each "
            "signal's centre frequency and corrects the delivered power AND the safety ceiling.")
        info.setWordWrap(True)
        info.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_MUTED};")
        lay.addWidget(info)
        tbl = _CurveTable(headers=("frequency (MHz)", "power (dBm)"))
        for f, p in (sb.get("power_by_freq") or []):
            r = tbl.rowCount(); tbl.insertRow(r)
            tbl.setItem(r, 0, QTableWidgetItem(_numstr(float(f) / 1e6)))
            tbl.setItem(r, 1, QTableWidgetItem(_numstr(float(p))))
        if not sb.get("power_by_freq"):
            tbl.add_blank_row()
        lay.addWidget(tbl)
        grow = QHBoxLayout()
        grow.addWidget(QLabel("measured at gain (dB), optional:"))
        gain_e = QLineEdit(_numstr(sb.get("gain_db"))); gain_e.setPlaceholderText("e.g. 60")
        grow.addWidget(gain_e); lay.addLayout(grow)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        pts = tbl.numeric_points()                    # (MHz, dBm) tuples, blanks skipped
        new_sb = dict(sb)
        if pts:
            new_sb["power_by_freq"] = [[round(f * 1e6, 3), p] for f, p in sorted(pts)]
        else:
            new_sb.pop("power_by_freq", None)
        g = gain_e.text().strip()
        if g:
            try:
                new_sb["gain_db"] = float(g)
            except ValueError:
                pass
        else:
            new_sb.pop("gain_db", None)
        if new_sb.get("power_by_freq"):
            self._doc["source_bias"] = new_sb
        else:
            self._doc.pop("source_bias", None)        # no points ⇒ no bias
        self._download_btn.setEnabled(True)
        self._render_chain()
        self._update_issues()

    # ── add / reorder stages ─────────────────────────────────────────────────────
    def _add_stage(self) -> None:
        """Add a stage to the end of the chain — a library component, a fresh measured
        plane, or a constant Δ dB. (Reorder with the ◀▶ handles.)"""
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction("Component from library…", lambda: self._add_component_stage())
        menu.addAction("Measured plane…", lambda: self._add_measured_stage())
        menu.addAction("Constant Δ dB…", lambda: self._add_constant_stage())
        menu.addAction("Active component (attenuator…)…", lambda: self._add_active_stage())
        menu.exec(self.cursor().pos())

    def _prepare_add(self):
        """Fold current edits into the model and return the planes dict to append to."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        if self._doc is None:
            self._doc = self._blank_doc()
        self._download_btn.setEnabled(True)
        return self._doc.setdefault("chain", {}).setdefault("planes", {})

    def _unique_plane_id(self, base: str, planes) -> str:
        base = base or "stage"
        nm, i = base, 1
        while nm in planes:
            i += 1; nm = f"{base}_{i}"
        return nm

    def _add_component_stage(self) -> None:
        ids = self._catalog.ids()
        if not ids:
            QMessageBox.information(self, "Add component stage",
                                   "No components yet — characterize a cable/antenna in the "
                                   "Component library first.")
            self._open_components()
            return
        cid, ok = QInputDialog.getItem(self, "Add component stage",
                                       "Component (from the library):", ids, 0, False)
        if not ok or not cid:
            return
        planes = self._prepare_add()
        name = self._unique_plane_id(cid, planes)
        planes[name] = {"type": "derived", "from": "", "component": cid}
        self._selected_plane = name
        self._doc_to_form()

    def _add_measured_stage(self) -> None:
        name, ok = QInputDialog.getText(self, "Add measured plane",
                                        "Plane id (e.g. amplifier_output):")
        name = (name or "").strip()
        if not ok or not name:
            return
        planes = self._prepare_add()
        name = self._unique_plane_id(name, planes)
        planes[name] = {"type": "measured"}
        self._selected_plane = name
        self._doc_to_form()

    def _add_constant_stage(self) -> None:
        name, ok = QInputDialog.getText(self, "Add constant Δ dB stage",
                                        "Plane id (e.g. cable_loss):")
        name = (name or "").strip()
        if not ok or not name:
            return
        planes = self._prepare_add()
        name = self._unique_plane_id(name, planes)
        planes[name] = {"type": "derived", "from": "", "delta_db": 0.0}
        self._selected_plane = name
        self._doc_to_form()

    def _add_active_stage(self) -> None:
        """Add an ACTIVE component — a derived stage whose baseline Δ dB is dynamically
        adjusted by a controllable parameter of a task (e.g. a step attenuator). It extends
        the achievable power range and participates automatically in power calculations."""
        name, ok = QInputDialog.getText(self, "Add active component",
                                        "Plane id (e.g. attenuator_out):")
        name = (name or "").strip()
        if not ok or not name:
            return
        planes = self._prepare_add()
        name = self._unique_plane_id(name, planes)
        planes[name] = {"type": "derived", "from": "", "delta_db": 0.0,
                        "control": {"task": "", "param": "", "sense": "attenuation",
                                    "min_db": 0.0, "max_db": 0.0, "step_db": 0.0,
                                    "engage_pct": 0.0}}
        self._selected_plane = name
        self._doc_to_form()

    def _move_stage(self, row, delta: int) -> None:
        """Swap a stage one place earlier/later. The source (index 0) is fixed and nothing
        can move into its slot. `from`/operating are recomputed from the new order."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        planes = (self._doc or {}).get("chain", {}).get("planes") or {}
        names = list(planes.keys())
        name = row["name"].text().strip()
        if name not in names:
            return
        i = names.index(name); j = i + delta
        if i == 0 or j < 1 or j >= len(names):    # keep the source first; stay in range
            return
        names[i], names[j] = names[j], names[i]
        self._doc["chain"]["planes"] = {nm: planes[nm] for nm in names}
        self._selected_plane = name
        self._doc_to_form()

    def _reorder_stage(self, src: str, dst: str) -> None:
        """Drag-and-drop reorder: move stage `src` to `dst`'s slot. The source stage
        (index 0) is pinned and nothing may take slot 0, so the linear chain always keeps
        its measured source first; the operating plane stays the last stage."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        planes = (self._doc or {}).get("chain", {}).get("planes") or {}
        names = list(planes.keys())
        if src not in names or dst not in names or src == dst:
            return
        i, j = names.index(src), names.index(dst)
        if i == 0 or j == 0:                       # never move the source, never displace it
            return
        names.pop(i)
        j = names.index(dst)                       # dst's index after removing src
        names.insert(j if i > j else j + 1, src)   # before dst moving left, after moving right
        self._doc["chain"]["planes"] = {nm: planes[nm] for nm in names}
        self._selected_plane = src
        self._doc_to_form()

    def _reorder_planes_to(self, plane: str, index: int) -> None:
        """Move ``plane`` so it lands at position ``index`` in the chain, clamped to keep
        the source pinned first and stay in range. Commits to the model, then rebuilds."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        planes = (self._doc or {}).get("chain", {}).get("planes") or {}
        names = list(planes.keys())
        if plane not in names or names.index(plane) == 0:
            return                                 # the source stays pinned first
        names.remove(plane)
        index = max(1, min(index, len(names)))     # never before the source, never off the end
        names.insert(index, plane)
        self._doc["chain"]["planes"] = {nm: planes[nm] for nm in names}
        self._selected_plane = plane
        self._doc_to_form()

    # ── live drag-reorder of chain stages ────────────────────────────────────────
    # The real stage card is lifted out of the flow and follows the cursor; a same-size
    # placeholder holds its spot, and as the card crosses a neighbour the placeholder
    # hops past it (the neighbours slide over with a short animation). Releasing drops the
    # card where the placeholder sits and commits the new order. The source stays pinned.
    def _stage_positions(self):
        """(slot, centre_x) for every stage slot except the one being dragged, in the
        holder's coordinates and current visual order."""
        out = []
        for name, slot in self._chain_slots:
            if self._drag and name == self._drag["plane"]:
                continue
            out.append((slot, slot.x() + slot.width() / 2))
        return out

    def _chain_drag_start(self, plane: str, global_pos) -> None:
        slot = next((s for n, s in self._chain_slots if n == plane), None)
        if slot is None:
            return
        holder = self._chain_holder
        idx = self._chain_row.indexOf(slot)
        placeholder = QWidget(); placeholder.setFixedSize(slot.size())
        self._chain_row.insertWidget(idx, placeholder)   # holds the gap at the live index
        self._chain_row.removeWidget(slot)
        geo = slot.geometry()
        slot.setParent(holder)                            # float free of the layout
        slot.setGeometry(geo); slot.show(); slot.raise_()
        card = slot.findChild(QFrame, "stage")
        if card is not None:                              # a lifted look while dragging
            card.setStyleSheet(f"#stage {{ background:{Palette.SURFACE}; "
                               f"border:1px solid {Palette.ACCENT}; border-radius:10px; }}")
        local = holder.mapFromGlobal(global_pos)
        self._drag = {"plane": plane, "slot": slot, "placeholder": placeholder,
                      "holder": holder, "grab_dx": local.x() - geo.x(), "y": geo.y(),
                      "anims": []}

    def _chain_drag_move(self, global_pos) -> None:
        d = self._drag
        if not d:
            return
        holder, slot = d["holder"], d["slot"]
        x = holder.mapFromGlobal(global_pos).x() - d["grab_dx"]
        x = max(0, min(x, holder.width() - slot.width()))
        slot.move(int(x), d["y"])
        # The placeholder goes after every stage whose centre is left of the dragged card;
        # clamp so the source (index 0) stays pinned and the index stays in range.
        centre = x + slot.width() / 2
        k = sum(1 for _, cx in self._stage_positions() if cx < centre)
        target = max(1, min(k, len(self._chain_slots) - 1))
        if self._chain_row.indexOf(d["placeholder"]) != target:
            self._move_placeholder(target)

    def _move_placeholder(self, target: int) -> None:
        """Reposition the gap to ``target`` and slide the displaced stage slots over."""
        d = self._drag
        before = {s: s.geometry() for _, s in self._chain_slots if s is not d["slot"]}
        self._chain_row.removeWidget(d["placeholder"])
        self._chain_row.insertWidget(target, d["placeholder"])
        self._chain_row.activate()                        # recompute the new geometry now
        for anim in d["anims"]:
            anim.stop()
        d["anims"] = []
        for _, s in self._chain_slots:
            if s is d["slot"] or s not in before:
                continue
            new = s.geometry()
            if new == before[s]:
                continue
            a = QPropertyAnimation(s, b"geometry", self)
            a.setDuration(140); a.setEasingCurve(QEasingCurve.Type.OutCubic)
            a.setStartValue(before[s]); a.setEndValue(new)
            a.start()
            d["anims"].append(a)

    def _chain_drag_end(self) -> None:
        d = self._drag
        if not d:
            return
        target = self._chain_row.indexOf(d["placeholder"])
        for anim in d["anims"]:
            anim.stop()
        d["slot"].setParent(None); d["slot"].deleteLater()
        d["placeholder"].setParent(None); d["placeholder"].deleteLater()
        plane = d["plane"]
        self._drag = None
        self._reorder_planes_to(plane, target)            # commit + full rebuild

    def _first_curve_points(self, plane: str):
        """The first signal's measured points on `plane`, for a stage minicurve."""
        for sig in ((self._doc or {}).get("signals") or {}).values():
            pts = ((sig or {}).get("curves") or {}).get(plane, {}).get("points")
            if pts:
                return [(p.get("gain_db"), p.get("power_dbm")) for p in pts]
        return []

    def _select_plane(self, name: str) -> None:
        """Select a chain stage: fold the current edits into the model, then rebuild so
        the detail pane and stage borders follow the selection."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        self._selected_plane = name or None
        self._doc_to_form()

    def _on_signal_row_clicked(self, r: int, _c: int) -> None:
        item = self._table.item(r, 0)
        if item is not None:
            self._select_signal(item.text().strip())

    def _active_signal_ids(self) -> set:
        """Signals whose editor is currently on screen: a MEASURED stage is selected and
        the signal is both shown there and expanded. These get the accent row-highlight in
        the Signals table — and only these, so the highlight tracks the visible editor and
        clears when you collapse it or move to a passive/other stage."""
        sel = self._selected_plane
        if not self._is_measured_plane(sel):
            return set()
        return {sid for sid in self._expanded_signals
                if sid in self._f.get("signals", {}) and self._signal_shown_on(sid, sel)}

    # ── stage / signal membership helpers ────────────────────────────────────────
    def _ordered_plane_names(self) -> list:
        return [r["name"].text().strip() for r in self._f.get("planes", [])]

    def _measured_plane_names(self) -> list:
        return [r["name"].text().strip() for r in self._f.get("planes", [])
                if r.get("role", "measured") == "measured"]

    def _source_plane(self) -> Optional[str]:
        """The chain's first stage — always the measured origin every signal is shown on."""
        names = self._ordered_plane_names()
        return names[0] if names else None

    def _is_measured_plane(self, name: Optional[str]) -> bool:
        row = next((r for r in self._f.get("planes", [])
                    if r["name"].text().strip() == name), None)
        return row is not None and row.get("role", "measured") == "measured"

    def _signal_has_data_on(self, sid: str, plane: str) -> bool:
        """True when this signal's grid on ``plane`` holds at least one numeric point."""
        entry = self._f.get("signals", {}).get(sid)
        tbl = entry and entry["curves"].get(plane)
        return bool(tbl and tbl.numeric_points())

    def _signal_shown_on(self, sid: str, plane: str) -> bool:
        """Whether ``sid`` appears in ``plane``'s measured detail. The SOURCE shows every
        signal; a downstream measured stage shows only those measured there (or just
        opened for measuring via '+ Measure a signal here')."""
        if plane == self._source_plane():
            return True
        return (self._signal_has_data_on(sid, plane)
                or sid in self._stage_extra.get(plane, set()))

    def _select_signal(self, sid: str) -> None:
        """Clicking a signal opens its measured curve and expands just that signal. Stay
        on the currently-open measured stage when it already shows this signal (don't yank
        the view back to Source); otherwise fall back to the Source, where every signal is
        shown."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        cur = self._selected_plane
        if not (self._is_measured_plane(cur) and self._signal_shown_on(sid, cur)):
            self._selected_plane = self._source_plane() or cur
        self._expanded_signals = {sid}
        self._doc_to_form()

    # ── stage detail (mockup section 3) ──────────────────────────────────────────
    def _render_detail(self) -> None:
        self._clear_layout(self._detail_body)
        name = self._selected_plane
        rows = self._f.get("planes", [])
        row = next((r for r in rows if r["name"].text().strip() == name), None)
        if self._detail_hdr is not None:
            if row is None:
                self._detail_hdr.setText(
                    f"<span style='color:{Palette.TEXT};font-weight:600;'>Stage detail</span>"
                    f" <span style='color:{Palette.TEXT_FAINT};'>— pick a stage above</span>")
            else:
                idx = rows.index(row)
                role = row.get("role", "measured")
                if idx == 0 or role == "measured":
                    knd = "Source" if idx == 0 else "Measured"
                    desc = "measured gain→power on this box"
                elif role == "active":
                    knd, desc = "Active", "a task-controlled gain/attenuation stage"
                else:
                    knd, desc = "Passive", "loss evaluated at each signal's frequency"
                self._detail_hdr.setText(
                    f"<span style='color:{Palette.TEXT};font-weight:600;'>{knd} · {name}</span>"
                    f" <span style='color:{Palette.TEXT_FAINT};'>— {desc}</span>")
        if row is None:
            ph = QLabel("Select a stage in the chain above to edit it.")
            ph.setStyleSheet(f"color:{Palette.TEXT_FAINT};font-size:12px;padding:16px 0;")
            self._detail_body.addWidget(ph)
            return
        role = row.get("role", "measured")
        if role == "measured":
            self._detail_measured(row)
        elif role == "active":
            self._detail_active(row)
        else:
            self._detail_passive(row)
        # Reported/limiting readings are per-SIGNAL now (docs/calibration-ui-redesign §5),
        # rendered inside each signal's card on the source stage — not a shared block on the
        # operating plane.
        self._detail_body.addWidget(self._stage_advanced(row))

    def _detail_passive(self, row) -> None:
        """Detail for a passive stage. A component stage shows its library part read-only —
        its Δ dB(f) sweep plotted, with an “edit in library” shortcut — because to change a
        stage's part you add a new stage and drop the old one. A constant stage shows an
        editable Δ dB field."""
        if row.get("role") == "component":
            comp = row.get("comp_id", "")
            table = self._comp_table(comp)
            markers = self._signal_markers()
            if comp and table:
                plot = _FreqResponsePlot()
                plot.set_data(table, markers)
                self._detail_body.addWidget(plot)
                leg = QHBoxLayout(); leg.setSpacing(16)
                sw = QLabel("● VNA sweep (measured points)")
                sw.setStyleSheet(f"font-size:11px;color:{Palette.ACCENT};")
                leg.addWidget(sw)
                for label, freq, color in markers:
                    db = _interp_db(table, freq)
                    l = QLabel(f"{label} → {db:+.2f} dB")
                    l.setStyleSheet(f"font-size:11px;color:{color};")
                    leg.addWidget(l)
                leg.addStretch(1)
                self._detail_body.addLayout(leg)
            pr = QHBoxLayout()
            lab = QLabel("Component")
            lab.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
            val = QLabel(comp or "(missing component)")
            val.setStyleSheet(f"font-size:12px;font-weight:600;color:{Palette.TEXT};")
            edit = QPushButton("Edit in library…")
            edit.setStyleSheet("font-size:11px;")
            edit.clicked.connect(lambda _=False, c=comp: self._open_components(c or None))
            pr.addWidget(lab); pr.addWidget(val); pr.addStretch(1); pr.addWidget(edit)
            self._detail_body.addLayout(pr)
            note = QLabel("Characterized once in the Component library · shared across the "
                          "fleet. To swap the part, add a new stage and remove this one — "
                          "then drag it into place.")
        else:                                          # constant Δ dB stage
            # Wrap the persistent row["delta"] in a container widget, not a bare sub-layout, so a
            # re-render can't deleteLater() it out from under _read_planes — see _detail_active.
            holder = QWidget()
            dr = QHBoxLayout(holder); dr.setContentsMargins(0, 0, 0, 0)
            dl = QLabel("Constant Δ dB")
            dl.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
            dr.addWidget(dl); row["delta"].setFixedWidth(90); dr.addWidget(row["delta"])
            dr.addStretch(1)
            self._detail_body.addWidget(holder)
            note = QLabel("A fixed, frequency-independent offset from the previous stage "
                          "(negative = loss, positive = gain).")
        note.setWordWrap(True)
        note.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        self._detail_body.addWidget(note)

    # ── active component (task-controlled gain/attenuation) ──────────────────────
    def _all_task_names(self) -> list:
        """Every task deployed on this unit (from the last-fetched tasks.yaml), so the
        control's set-task picker can offer them. Empty until the tasks are fetched."""
        import yaml
        try:
            doc = yaml.safe_load(self._tasks_yaml) or {}
        except yaml.YAMLError:
            return []
        return [t["name"] for t in doc.get("tasks", [])
                if isinstance(t, dict) and t.get("name")]

    def _task_script(self, task_name: str) -> str:
        """The script basename a task runs (for fetching its params), or ''."""
        import yaml
        try:
            doc = yaml.safe_load(self._tasks_yaml) or {}
        except yaml.YAMLError:
            return ""
        entry = next((t for t in doc.get("tasks", [])
                      if isinstance(t, dict) and t.get("name") == task_name), None)
        if not entry:
            return ""
        for a in entry.get("command", []):
            if isinstance(a, str) and a.endswith(".py"):
                return a.rsplit("/", 1)[-1]
        return ""

    def _fetch_task_params(self, task_name: str) -> None:
        """Fetch a task's script params (numeric ones feed the set-param picker), once per
        script. Best-effort — the param field stays free-text until they arrive."""
        script = self._task_script(task_name)
        if not script or script in self._task_params or script in self._task_params_inflight:
            return
        self._task_params_inflight.add(script)
        self.hub.run_async(
            f"cal_taskparams:{self.hostname}:{script}",
            lambda: self.hub.fleet.get(self.hostname).get_script_params(script))

    def _numeric_params_for(self, task_name: str) -> list:
        """The numeric parameter names of a task's script (once fetched) — the candidates
        for a gain/attenuation control param."""
        return [p["dest"] for p in self._params_for(task_name)
                if p["type"] in ("int", "float")]

    def _params_for(self, task_name: str) -> list:
        """Every parameter of a task's script (once fetched) as ``{dest, type, numeric}`` —
        the set-param form lists them all: a numeric one can DRIVE the attenuation, the rest
        can be given constant values (e.g. a serial ``port``). Empty until the fetch returns."""
        script = self._task_script(task_name)
        out = []
        for s in self._task_params.get(script, []):
            dest = s.get("dest") or (s.get("flags") or [""])[0].lstrip("-")
            if not dest:
                continue
            typ = s.get("type") or "str"
            out.append({"dest": dest, "type": typ, "numeric": typ in ("int", "float")})
        return out

    def _build_active_param_form(self, c: dict, v, commit) -> None:
        """The set-task's parameter form for an active component. One numeric param is the
        DRIVER (set automatically from the requested power → control.param); the rest can be
        given constant values sent on every set (control.consts), e.g. a serial ``port``.
        Falls back to a free-text driver picker until the task's params are fetched."""
        params = self._params_for(c.get("task", ""))
        if not params:                                   # not fetched yet / offline
            prow = QHBoxLayout()
            pl = QLabel("param"); pl.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
            combo = QComboBox(); combo.setEditable(True)
            for pn in self._numeric_params_for(c.get("task", "")):
                combo.addItem(pn)
            combo.setCurrentText(c.get("param", ""))
            combo.currentTextChanged.connect(
                lambda _=0, cb=combo: (c.__setitem__("param", cb.currentText().strip()), commit()))
            prow.addWidget(pl); prow.addWidget(combo, 1)
            v.addLayout(prow)
            hint = QLabel("Connect to this unit to load the task's parameters — then you can "
                          "pick the driving param and set constants (e.g. a serial port).")
            hint.setWordWrap(True)
            hint.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
            v.addWidget(hint)
            return

        numeric = [p["dest"] for p in params if p["numeric"]]
        driver = (c.get("param") or "").strip()
        if driver not in numeric:                        # default to the first numeric param
            driver = numeric[0] if numeric else ""
            c["param"] = driver
        consts = c.setdefault("consts", {})
        consts.pop(driver, None)                         # the driver is never also a constant

        hdr = QLabel("Parameters — ● drives the attenuation from the requested power; the "
                     "others are sent as constants on every set.")
        hdr.setWordWrap(True)
        hdr.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        v.addWidget(hdr)

        group = QButtonGroup(v.parentWidget() or self)   # exclusive driver selection
        val_fields: dict = {}
        form = QFormLayout(); form.setContentsMargins(0, 0, 0, 0); form.setSpacing(6)
        for p in params:
            dest = p["dest"]
            cell = QHBoxLayout(); cell.setContentsMargins(0, 0, 0, 0)
            radio = QRadioButton("drives")
            radio.setEnabled(p["numeric"])
            radio.setChecked(p["numeric"] and dest == driver)
            radio.setToolTip("Drive this parameter automatically from the requested power."
                             if p["numeric"] else
                             "Only a numeric parameter can drive the power — set a constant.")
            group.addButton(radio)
            field = QLineEdit(consts.get(dest, ""))
            field.setEnabled(dest != driver)
            field.setPlaceholderText("set at runtime" if dest == driver else "constant value")
            val_fields[dest] = field

            def _on_drive(checked, d=dest):
                if not checked:
                    return
                c["param"] = d
                (c.get("consts") or {}).pop(d, None)     # a driver can't also be a constant
                for dd, f in val_fields.items():
                    f.setEnabled(dd != d)
                    if dd == d:
                        f.blockSignals(True); f.clear(); f.blockSignals(False)
                        f.setPlaceholderText("set at runtime")
                    else:
                        f.setPlaceholderText("constant value")
                commit()
            radio.toggled.connect(_on_drive)

            def _on_const(text, d=dest):
                cc = c.setdefault("consts", {})
                if text.strip():
                    cc[d] = text
                else:
                    cc.pop(d, None)
            field.textChanged.connect(_on_const)
            field.editingFinished.connect(commit)

            cell.addWidget(radio); cell.addWidget(field, 1)
            holder = QWidget(); holder.setLayout(cell)
            form.addRow(dest, holder)
        v.addLayout(form)

    def _handle_taskparams(self, script: str, result) -> None:
        """Cache a script's params and, if the active stage that asked is on screen, re-render
        so its set-param picker populates."""
        self._task_params_inflight.discard(script)
        if isinstance(result, dict):
            self._task_params[script] = result.get("params", []) or []
            # A script may also declare power-quantity laws (CAL_POWER_LAWS) for the reported/
            # limiting bridge picker (docs/calibration-v2.md §13).
            self._task_laws[script] = result.get("calibration_power_laws", []) or []
            self._render_detail()

    def _control_from_row(self, row) -> dict:
        """The control block for an active stage, read from its live editor state."""
        c = row.get("control") or {}
        sense = c.get("sense", "attenuation")
        param = str(c.get("param", "")).strip()
        # Constant params sent on every set: {dest: value}. Drop empties and never let the
        # driving param double as a constant.
        consts = {str(k).strip(): str(v) for k, v in (c.get("consts") or {}).items()
                  if str(k).strip() and str(k).strip() != param and str(v).strip() != ""}
        out = {"task": str(c.get("task", "")).strip(),
               "param": param,
               "sense": sense if sense in ("attenuation", "gain") else "attenuation",
               "min_db": _numeric(c.get("min_db"), 0.0),
               "max_db": _numeric(c.get("max_db"), 0.0),
               "step_db": _numeric(c.get("step_db"), 0.0),
               "engage_pct": _numeric(c.get("engage_pct"), 0.0)}
        if consts:
            out["consts"] = consts
        return out

    # ── power-quantity conversion laws (declared by signals' scripts) ─────────────
    def _declared_laws(self) -> dict:
        """Every power-quantity law any of this unit's signals' scripts declares
        (CAL_POWER_LAWS), keyed by id. Triggers the per-task param fetch (cached) so they
        populate as they arrive. Prefer _declared_laws_for_signal for a per-signal picker —
        this unscoped set mixes every signal's laws together."""
        laws: dict = {}
        for tname in self._all_task_names():
            self._fetch_task_params(tname)
            for lw in self._task_laws.get(self._task_script(tname), []) or []:
                lid = lw.get("id") or lw.get("name")
                if lid and lid not in laws:
                    laws[lid] = lw
        return laws

    def _declared_laws_for_signal(self, sid: str) -> dict:
        """The power-quantity laws THIS signal's transmit script declares (CAL_POWER_LAWS),
        keyed by id — scoped by SDR_CAL_SIGNAL_ID so one signal's laws never leak into
        another's picker (a chirp's law must not appear for a GPS signal). Empty until this
        unit's tasks (and their params) are fetched."""
        laws: dict = {}
        for tname, tsid in self._task_signals.items():
            if tsid != sid:
                continue
            self._fetch_task_params(tname)
            for lw in self._task_laws.get(self._task_script(tname), []) or []:
                lid = lw.get("id") or lw.get("name")
                if lid and lid not in laws:
                    laws[lid] = lw
        return laws

    def _law_caption(self, lw: dict, role: str) -> QLabel:
        terms = lw.get("terms") or ([{"param": lw["param"], "coeff": lw.get("coeff", 10.0),
                                      "ref": lw.get("ref", 1.0)}] if lw.get("param") else [])
        parts = []
        if lw.get("k"):
            parts.append(f"{lw['k']:+g} dB")
        for t in terms:
            parts.append(f"{t.get('coeff', 10.0):+g}·log₁₀({t['param']}/{t.get('ref', 1.0):g})")
        expr = (" " + " ".join(parts)) if parts else " (unchanged)"
        cap = QLabel(f"{role} = measured{expr}  ·  declared by the signal (read-only)")
        cap.setWordWrap(True)
        cap.setStyleSheet(f"font-size:10px;color:{Palette.ONLINE};font-family:monospace;")
        return cap

    def _mark_reading_dirty(self) -> None:
        """Persist an in-place reading/measurement edit (a quantity keystroke) into the
        working doc without a full detail rebuild (which would steal focus mid-edit) —
        _read_form reads the blocks from each signal entry's live dicts."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            return
        self._download_btn.setEnabled(True)

    def _detail_active(self, row) -> None:
        """Detail editor for an ACTIVE component: a passive baseline Δ dB plus a control block
        that links a task's parameter (e.g. a step attenuator's ``attenuation``) so the engine
        can dynamically apply gain/attenuation — extending the achievable power range and
        participating automatically in every power calculation."""
        c = row.setdefault("control", {})

        def _commit():
            self._sync_from(strict=False)

        # Baseline — the component's behaviour at 0 dB applied, i.e. its fixed INSERTION LOSS.
        # It belongs to THIS active component: a flat constant Δ dB, or its own inline Δ dB(f)
        # table (frequency-dependent). The programmable range below is layered on top.
        base_tbl = row.get("baseline_table") or []
        has_tbl = bool(base_tbl)
        brow = QHBoxLayout()
        bl = QLabel("Baseline"); bl.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
        kind = QComboBox()
        kind.addItem("Constant Δ dB (flat insertion loss)", "constant")
        kind.addItem("Δ dB(f) table (frequency-dependent insertion loss)", "table")
        kind.setCurrentIndex(1 if has_tbl else 0)
        brow.addWidget(bl); brow.addWidget(kind, 1)
        self._detail_body.addLayout(brow)

        def _base_kind_changed(_=0):
            if kind.currentData() == "table":
                if not row.get("baseline_table"):
                    # Seed a one-row table from the current constant so the switch is lossless.
                    try:
                        d = float(row["delta"].text().strip())
                    except (TypeError, ValueError):
                        d = 0.0
                    row["baseline_table"] = [[1.0e9, d]]
            else:
                row["baseline_table"] = []
            _commit()
            self._render_detail()
        kind.currentIndexChanged.connect(_base_kind_changed)

        # row["delta"] is a PERSISTENT model widget (created once in _make_plane_row) that
        # _read_planes reads for the constant baseline AND for the empty-table fallback. Place
        # it on EVERY render — even in table mode, where it's hidden — inside a WRAPPER widget:
        #   • _clear_layout recurses into bare sub-layouts and would deleteLater() a model widget
        #     placed directly in one (a direct re-render fires e.g. from _handle_taskparams);
        #     a wrapper widget is not recursed into, so a re-render just reparents it (as
        #     _stage_advanced does for row["name"]).
        #   • If table mode SKIPPED placing it, a constant→table switch would delete it with its
        #     old wrapper, and then emptying the table (Del) would read a dead QLineEdit in
        #     _read_planes and abort. Always re-homing it keeps it alive regardless of the kind.
        holder = QWidget()
        dr = QHBoxLayout(holder); dr.setContentsMargins(0, 0, 0, 0)
        dl = QLabel("Δ dB (at 0 applied)")
        dl.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
        row["delta"].setFixedWidth(90)
        dr.addWidget(dl); dr.addWidget(row["delta"]); dr.addStretch(1)
        holder.setVisible(not has_tbl)                # shown for a constant baseline, else hidden
        self._detail_body.addWidget(holder)

        if has_tbl:
            hint = QLabel("This component's insertion loss vs frequency (signed dB, negative = "
                          "loss). Folded into the range at each signal's frequency; the "
                          "programmable range below adds on top.")
            hint.setWordWrap(True)
            hint.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
            self._detail_body.addWidget(hint)
            spark = _FreqSparkline()
            spark.set_table(base_tbl)
            base_grid = _CurveTable(on_changed=None, headers=("freq (Hz)", "Δ dB"))
            base_grid.set_rows(base_tbl)

            def _table_changed(_grid=base_grid, _spark=spark):
                row["baseline_table"] = _grid.rows(strict=False)
                _spark.set_table(row["baseline_table"])
                _commit()
            base_grid._on_changed = _table_changed        # _CurveTable calls this on edit
            self._detail_body.addWidget(spark)
            self._detail_body.addWidget(base_grid)

        box = QFrame(); box.setObjectName("ctrlbox")
        box.setStyleSheet(f"#ctrlbox {{ border:1px solid {Palette.BORDER}; border-radius:8px; }}")
        v = QVBoxLayout(box); v.setContentsMargins(10, 8, 10, 8); v.setSpacing(8)

        # Set task.
        self._fetch_task_params(c.get("task", ""))
        trow = QHBoxLayout()
        tl = QLabel("Set task"); tl.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
        task_combo = QComboBox(); task_combo.setEditable(True)
        for t in self._all_task_names():
            task_combo.addItem(t)
        task_combo.setCurrentText(c.get("task", ""))
        trow.addWidget(tl); trow.addWidget(task_combo, 1)
        v.addLayout(trow)

        def _task_changed(_=0):
            c["task"] = task_combo.currentText().strip()
            self._fetch_task_params(c["task"])
            _commit()
            # Re-render so the parameter form matches the new task. Deferred: rebuilding the
            # detail from inside the combo's own signal would delete the live combo mid-signal
            # (a Qt slot touching a freed widget aborts the process).
            QTimer.singleShot(0, self._render_detail)
        task_combo.currentTextChanged.connect(_task_changed)

        # Parameters. Every param of the set-task is listed: pick the ONE numeric param that
        # drives the attenuation from the requested power, and give the others (e.g. a serial
        # ``port``) constant values that ride along on every set. Until the task's params are
        # fetched (offline), fall back to a free-text driver picker so the field still works.
        self._build_active_param_form(c, v, _commit)

        # Sense.
        srow = QHBoxLayout()
        sl = QLabel("Sense"); sl.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
        sense = QComboBox()
        sense.addItem("Attenuation — the param subtracts (0 = full power)", "attenuation")
        sense.addItem("Gain — the param adds", "gain")
        sense.setCurrentIndex(1 if c.get("sense") == "gain" else 0)
        sense.currentIndexChanged.connect(
            lambda _=0: (c.__setitem__("sense", sense.currentData()), _commit()))
        srow.addWidget(sl); srow.addWidget(sense, 1)
        v.addLayout(srow)

        # min / max / step / engage.
        def _spin(key, lo, hi, step, decimals, default):
            sp = QDoubleSpinBox(); sp.setRange(lo, hi); sp.setSingleStep(step)
            sp.setDecimals(decimals); sp.setValue(_numeric(c.get(key), default))
            sp.valueChanged.connect(lambda val, k=key: (c.__setitem__(k, float(val)), _commit()))
            return sp

        grid = QHBoxLayout()
        for label, key, lo, hi, step, dec, dflt in [
                ("min dB", "min_db", -200.0, 200.0, 0.25, 3, 0.0),
                ("max dB", "max_db", -200.0, 200.0, 0.25, 3, 0.0),
                ("step dB", "step_db", 0.0, 200.0, 0.05, 3, 0.0),
                ("engage %", "engage_pct", 0.0, 100.0, 1.0, 1, 0.0)]:
            lab = QLabel(label); lab.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_MUTED};")
            cell = QVBoxLayout(); cell.setSpacing(2); cell.addWidget(lab); cell.addWidget(_spin(
                key, lo, hi, step, dec, dflt))
            grid.addLayout(cell)
        v.addLayout(grid)
        self._detail_body.addWidget(box)

        note = QLabel(
            "The task's parameter dynamically applies gain/attenuation on top of this stage's "
            "baseline — so requesting a calibrated power drives both the SDR and this "
            "component. min/max/step are the parameter's own range and resolution; engage % "
            "is where, as a fraction of the SDR's dynamic range, it starts contributing "
            "(0 = only below the SDR's own floor).")
        note.setWordWrap(True)
        note.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        self._detail_body.addWidget(note)
        for msg in _control_issues(row["name"].text().strip() or "stage", self._control_from_row(row)):
            warn = QLabel("⚠ " + msg.split(": ", 1)[-1])
            warn.setWordWrap(True)
            warn.setStyleSheet(f"font-size:11px;color:{Palette.ARMED};")
            self._detail_body.addWidget(warn)

    def _auto_of(self, plane: str) -> Optional[str]:
        """The limiting curve a reported stage at ``plane`` gauges its limits through,
        derived automatically (there's no picker): the nearest LIMITING measured stage
        upstream. Reported stages in between are skipped (several re-measurements of one
        node share its limiting basis). A PASSIVE stage in between means ``plane`` is a
        different physical node than any upstream limiting curve, so there is no valid
        basis — returns None, and the toggle refuses Reported there."""
        rows = self._f.get("planes", [])
        idx = next((i for i, r in enumerate(rows)
                    if r["name"].text().strip() == plane), None)
        if idx is None:
            return None
        for j in range(idx - 1, -1, -1):
            r = rows[j]
            if r.get("role", "measured") != "measured":     # a passive stage → different node
                return None
            if r.get("cal_role") != "reported":             # the nearest limiting curve
                return r["name"].text().strip()
        return None                                          # only reported/none upstream

    def _measured_role_controls(self, row, plane: str) -> QWidget:
        """Limiting/Reported gauge-role control for a non-source measured stage. Reported
        (report-only) means safety limits ignore this curve and punch through to the nearest
        limiting stage upstream (chosen automatically) — so --power can show a
        region-of-interest quantity (e.g. main-lobe power) while limits stay gauged on the
        full-band curve (§4.1)."""
        box = QFrame(); box.setObjectName("rolebox")
        box.setStyleSheet(f"#rolebox {{ border:1px solid {Palette.BORDER}; border-radius:8px; }}")
        v = QVBoxLayout(box); v.setContentsMargins(10, 8, 10, 8); v.setSpacing(6)
        top = QHBoxLayout()
        lab = QLabel("Gauge role")
        lab.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
        auto_of = self._auto_of(plane)
        combo = QComboBox()
        combo.addItem("Limiting — safety limits gauge on this curve", "limiting")
        combo.addItem("Reported — report-only (limits gauge upstream)", "reported")
        combo.setCurrentIndex(1 if row.get("cal_role") == "reported" else 0)

        def _apply():
            role = combo.currentData()
            if role == "reported" and auto_of is None:
                # No limiting stage is reachable upstream at this node — a reported stage
                # would have nothing to gauge through, so keep it limiting and say why.
                combo.setCurrentIndex(0)
                self._set_status("a reported stage needs a limiting stage directly upstream "
                                 "to gauge limits through — none is reachable here", kind="warn")
                return
            row["cal_role"] = role
            self._sync_from(strict=False)
            self._render_chain()
            self._render_detail()

        combo.currentIndexChanged.connect(lambda _=0: _apply())
        top.addWidget(lab); top.addWidget(combo, 1)
        v.addLayout(top)
        if row.get("cal_role") == "reported" and auto_of:
            gv = QLabel(f"Safety limits gauge on <b>{auto_of}</b> (upstream limiting curve).")
            gv.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_MUTED};")
            v.addWidget(gv)
        note = QLabel(
            "Reported shows a different quantity than the limits use — e.g. main-lobe power "
            "for the operator, while an amplifier's input limit stays gauged on the full-band "
            "curve upstream. The source stage is always limiting.")
        note.setWordWrap(True); note.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        v.addWidget(note)
        return box

    def _detail_measured(self, row) -> None:
        plane = row["name"].text().strip()
        is_source = (plane == self._source_plane())
        if not is_source:
            self._detail_body.addWidget(self._measured_role_controls(row, plane))
        all_sigs = self._f.get("signals", {})
        # The source shows every signal; a downstream measured stage shows only the ones
        # actually measured there (or just opened for measuring) — so it reads as an
        # override list, not a duplicate of the whole signal set.
        sigs = (dict(all_sigs) if is_source else
                {sid: e for sid, e in all_sigs.items() if self._signal_shown_on(sid, plane)})

        head = QHBoxLayout()
        intro = QLabel(
            "Enter the gain→power points you measured on this plane, per signal. "
            "Click a signal to expand it." if is_source else
            "Only signals you measured on this stage are shown — each overrides the "
            "upstream curve here. Add one to measure it on this stage.")
        intro.setWordWrap(True); intro.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        head.addWidget(intro, 1)
        if not is_source:
            addb = QPushButton("+ Measure a signal here")
            addb.setFocusPolicy(Qt.FocusPolicy.NoFocus); addb.setStyleSheet("font-size:11px;")
            addb.clicked.connect(lambda _=False, p=plane: self._add_signal_to_stage(p))
            head.addWidget(addb)
        # expand/collapse-all when there are enough signals to be worth it
        if len(sigs) > 1:
            all_open = self._expanded_signals.issuperset(sigs)
            toggle_all = QPushButton("Collapse all" if all_open else "Expand all")
            toggle_all.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            toggle_all.setStyleSheet("font-size:11px;")
            toggle_all.clicked.connect(
                lambda _=False, s=set(sigs), o=all_open: self._toggle_all_signals(s, o))
            head.addWidget(toggle_all)
        self._detail_body.addLayout(head)
        if not sigs:
            l = QLabel("No signals yet — “+ Add signal…” (right), then enter its measured "
                       "gain→power points here." if is_source else
                       "No signals measured on this stage yet — use “+ Measure a signal "
                       "here”. Signals you don't measure here inherit the upstream curve.")
            l.setWordWrap(True); l.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_FAINT};")
            self._detail_body.addWidget(l)
            return
        for sid, entry in sigs.items():
            self._detail_body.addWidget(self._signal_section(sid, entry, plane, is_source))

    def _signal_section(self, sid: str, entry: dict, plane: str,
                        is_source: bool = True) -> QWidget:
        """One collapsible signal card. On the SOURCE stage the expanded card owns the
        signal's full config (docs/calibration-ui-redesign §5): a Measurement section
        (quantity, unit, its frequency, and the measured curve behind a dialog) and a
        Limiting section (how the dBm safety reading is obtained). On a downstream measured
        stage the card is just the per-stage curve override — otherwise it inherits the
        upstream curve. The remove action deletes the whole signal on the source, or clears
        it from this stage downstream."""
        tbl = entry["curves"][plane]
        expanded = sid in self._expanded_signals
        npts = len(tbl.numeric_points())
        # A non-source measured stage with no points for THIS signal isn't broken — the
        # unit inherits the previous stage's curve (see the agent resolver's partial
        # measured-stage fallback). Name that behaviour instead of just "no points".
        names = [r["name"].text().strip() for r in self._f.get("planes", [])]
        prev_stage = names[names.index(plane) - 1] if plane in names and names.index(plane) > 0 else None
        inherits = npts == 0 and prev_stage is not None
        box = QFrame(); box.setObjectName("sigbox")
        box.setStyleSheet(f"#sigbox {{ border:1px solid "
                          f"{Palette.ACCENT if expanded else Palette.BORDER}; border-radius:8px; }}")
        bv = QVBoxLayout(box); bv.setContentsMargins(10, 8, 10, 8); bv.setSpacing(6)
        header = _ClickCard(on_click=lambda s=sid: self._toggle_signal(s))
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        hh = QHBoxLayout(header); hh.setContentsMargins(0, 0, 0, 0)
        chev = QLabel("▾" if expanded else "▸")
        chev.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
        nm = QLabel(sid); nm.setStyleSheet(f"font-weight:600;color:{Palette.TEXT};")
        hh.addWidget(chev); hh.addWidget(nm)
        if is_source:                                    # a unit chip beside the name
            unit = entry["measurement"].get("unit", "dBm")
            fg, bg = ((Palette.ONLINE, Palette.ONLINE_SOFT) if _unit_family(unit) == "density"
                      else (Palette.TEXT_MUTED, Palette.IDLE_SOFT))
            hh.addWidget(_badge(unit, fg, bg))
        hh.addStretch(1)
        if not expanded:                                 # a compact summary while collapsed
            summ = QLabel(f"{npts} point(s)" if npts else
                          (f"inherits “{prev_stage}”" if inherits else "no points yet"))
            summ.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
            hh.addWidget(summ)
        bv.addWidget(header)
        if not expanded:
            return box
        if inherits:
            note = QLabel(f"No points here — this signal inherits the “{prev_stage}” "
                          f"curve. Add points only if you measured this stage for it.")
            note.setWordWrap(True)
            note.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
            bv.addWidget(note)
        if is_source:
            bv.addWidget(self._measurement_section(sid, entry, plane))
            bv.addWidget(self._limiting_section(sid, entry))
        else:
            self._inline_curve_editor(bv, entry, plane)
        foot = QHBoxLayout(); foot.addStretch(1)
        rmsig = QPushButton("Remove signal" if is_source else "Remove from this stage")
        rmsig.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rmsig.setStyleSheet(f"color:{Palette.CRASH};font-size:11px;")
        if is_source:
            rmsig.setToolTip("Delete this signal and all its measured curves from the "
                             "calibration (every stage).")
            rmsig.clicked.connect(lambda _=False, s=sid: self._on_remove_signal(s))
        else:
            rmsig.setToolTip("Remove this signal's measured points from this stage (and "
                             "any downstream measured stage). It stays measured upstream "
                             "and inherits that curve here.")
            rmsig.clicked.connect(
                lambda _=False, s=sid, p=plane: self._on_remove_signal_from_stage(s, p))
        foot.addWidget(rmsig)
        bv.addLayout(foot)
        return box

    def _inline_curve_editor(self, bv, entry: dict, plane: str) -> None:
        """The downstream measured-stage curve editor: the signal's frequency, the gain→power
        grid with its sparkline, and add/remove-point buttons. Only shown on non-source
        measured stages, where the card is a per-stage curve override rather than the
        signal's own config. (The plot-label field was removed — see _measurement_section.)"""
        tbl = entry["curves"][plane]
        sub = QHBoxLayout()
        sub.addWidget(QLabel("freq Hz")); entry["cfreq"].setFixedWidth(104)
        sub.addWidget(entry["cfreq"])
        sub.addStretch(1)
        bv.addLayout(sub)
        grid = QHBoxLayout()
        grid.addWidget(tbl, 3); grid.addWidget(entry["sparks"][plane], 2)
        bv.addLayout(grid)
        btns = QHBoxLayout()
        addp = QPushButton("+ point"); addp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        addp.clicked.connect(tbl.add_blank_row)
        rmp = QPushButton("− point"); rmp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rmp.clicked.connect(tbl.remove_selected)
        btns.addWidget(addp); btns.addWidget(rmp); btns.addStretch(1)
        bv.addLayout(btns)

    # ── per-signal Measurement + Limiting (source card) ──────────────────────────
    def _section_head(self, marker: str, tag_bg: str, title: str, tag: str) -> QHBoxLayout:
        """A section header row: a small colour marker, a bold title, and a faint tag chip
        (matching docs/calibration-signal-editor-mockup.html)."""
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(8)
        m = QLabel(); m.setFixedSize(10, 10)
        m.setStyleSheet(f"background:{marker};border-radius:3px;")
        t = QLabel(title); t.setStyleSheet(f"font-size:12px;font-weight:600;color:{Palette.TEXT};")
        tg = QLabel(tag)
        tg.setStyleSheet(f"font-size:9px;font-weight:700;letter-spacing:.06em;"
                         f"color:{Palette.TEXT_FAINT};background:{tag_bg};"
                         f"border-radius:4px;padding:2px 7px;")
        h.addWidget(m); h.addWidget(t); h.addWidget(tg); h.addStretch(1)
        return h

    def _measurement_section(self, sid: str, entry: dict, plane: str) -> QWidget:
        """The per-signal Measurement block: the free-text quantity label, the unit it was
        measured in, a live "shows as" preview, the signal's frequency, and the measured
        curve behind a dialog. quantity+unit persist to signals.<id>.measurement (the agent
        reads them from the Phase-2 capability; today it ignores the key)."""
        meas = entry["measurement"]
        tbl = entry["curves"][plane]
        npts = len(tbl.numeric_points())
        sec = QFrame(); v = QVBoxLayout(sec); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(6)
        v.addLayout(self._section_head(Palette.TEXT_MUTED, Palette.IDLE_SOFT, "Measurement",
                                       "WHAT YOU TOOK ON THE ANALYZER"))
        form = QFormLayout(); form.setContentsMargins(0, 0, 0, 0); form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        q = QLineEdit(meas.get("quantity", "")); q.setPlaceholderText("e.g. Full-band power")
        q.setToolTip("The label the operator sees above --power (e.g. “Full-band power”).")
        u = QComboBox()
        for uu in _MEASUREMENT_UNITS:
            u.addItem(uu, uu)
        j = u.findData(meas.get("unit", "dBm")); u.setCurrentIndex(j if j >= 0 else 0)
        u.setToolTip("The unit you measured the signal in. dBm is absolute power; the "
                     "dBm/… units are spectral densities. The unit fixes which limiting "
                     "conversions are offered below.")
        u.setFixedWidth(120)
        preview = QLabel()

        def _set_preview():
            qn = q.text().strip() or "—"
            preview.setText(
                f"<b>{qn}</b>&nbsp;&nbsp;<span style='color:{Palette.TEXT_MUTED};"
                f"font-family:monospace;font-size:11px;'>[{u.currentData()}]</span>")
        preview.setStyleSheet(
            f"font-size:11.5px;background:{Palette.SURFACE_ALT};border:1px solid "
            f"{Palette.BORDER};border-radius:5px;padding:4px 9px;")
        _set_preview()
        q.textChanged.connect(lambda t, m=meas: (m.__setitem__("quantity", t.strip()),
                                                  _set_preview()))
        q.editingFinished.connect(self._mark_reading_dirty)
        u.currentIndexChanged.connect(
            lambda _=0, cb=u, m=meas: (m.__setitem__("unit", cb.currentData()),
                                       self._refresh_form_from_widgets()))
        fam = "absolute · no denominator" if _unit_family(meas.get("unit", "dBm")) == "abs" \
            else "spectral density"
        uw = QWidget(); ur = QHBoxLayout(uw); ur.setContentsMargins(0, 0, 0, 0)
        fam_hint = QLabel(fam); fam_hint.setStyleSheet(f"font-size:10.5px;color:{Palette.TEXT_FAINT};")
        ur.addWidget(u); ur.addWidget(fam_hint); ur.addStretch(1)
        # Frequency, with a note that it's the frequency the curve was measured at. (The
        # plot-label field was removed — it will become signal-independent later; the stored
        # value still round-trips via _read_form, just no longer shown here.)
        entry["cfreq"].setFixedWidth(150)
        fw = QWidget(); fr = QHBoxLayout(fw); fr.setContentsMargins(0, 0, 0, 0)
        freq_hint = QLabel("the frequency the signal was measured at")
        freq_hint.setStyleSheet(f"font-size:10.5px;color:{Palette.TEXT_FAINT};")
        fr.addWidget(entry["cfreq"]); fr.addWidget(freq_hint); fr.addStretch(1)
        pts_btn = QPushButton(f"Measured points…   ({npts} point(s))")
        pts_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        pts_btn.setToolTip("The measured SDR gain → measured value points, in a dialog.")
        pts_btn.clicked.connect(lambda _=False, s=sid, p=plane: self._open_points_dialog(s, p))
        form.addRow("quantity", q)
        form.addRow("unit", uw)
        form.addRow("shows as", preview)
        form.addRow("frequency (Hz)", fw)
        form.addRow("curve", pts_btn)
        v.addLayout(form)
        return sec

    def _limiting_laws_for(self, sid: str, unit: str) -> dict:
        """The laws usable as a LIMITING conversion for signal ``sid`` measured in ``unit``:
        scoped to the signal's OWN declared laws (not the whole unit's), and among those the
        ones that RETURN dBm (out == abs) and accept the measurement's family (in == its
        family). These are the only "Derived" options — a limiting reading is always dBm."""
        fam = _unit_family(unit)
        return {lid: lw for lid, lw in self._declared_laws_for_signal(sid).items()
                if str(lw.get("out", "abs")) == "abs" and str(lw.get("in", "abs")) == fam}

    def _limiting_section(self, sid: str, entry: dict) -> QWidget:
        """The per-signal Limiting block — how the dBm safety reading (what the stage
        ceiling gauges against) is obtained from the measurement. Always resolves to dBm:
        Same as measurement (only if measured in dBm) · Derived via a dBm-returning law ·
        Separate measurement (an own dBm curve). Bound to signals.<id>.limiting."""
        meas = entry["measurement"]; unit = meas.get("unit", "dBm")
        sub = entry["reading"]["limiting"]
        lim_laws = self._limiting_laws_for(sid, unit)
        is_abs = _unit_family(unit) == "abs"
        # Coerce a stored kind this measurement can't offer (a density can't be a dBm
        # "same"; "derived" needs a dBm-returning law), preserving an own curve. A saved
        # "law" carries its embedded law dict, so it stays valid even before the unit's
        # declared laws have loaded (they arrive asynchronously) — otherwise a Derived reading
        # would be silently flipped to "Separate measurement" every time the signal is opened.
        kind = sub.get("kind", "same")
        has_law = isinstance(sub.get("law"), dict)

        def _ok(k):
            if k == "same":
                return is_abs
            if k == "law":
                return bool(lim_laws) or has_law
            return k == "own"
        if not _ok(kind):
            kind = "law" if lim_laws else ("same" if is_abs else "own")
            keep = sub.get("curve")
            sub.clear(); sub["kind"] = kind
            if kind == "own" and isinstance(keep, dict):
                sub["curve"] = keep
        sec = QFrame(); v = QVBoxLayout(sec); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(6)
        v.addLayout(self._section_head(Palette.ARMED, Palette.ARMED_SOFT, "Limiting",
                                       "GAUGED IN dBm"))
        combo = QComboBox()

        def _add(value, label, enabled):
            combo.addItem(label, value)
            if not enabled:
                combo.model().item(combo.count() - 1).setEnabled(False)
        _add("same", "Same as measurement" if is_abs else "Same as measurement (needs dBm)",
             is_abs)
        law_ok = bool(lim_laws) or has_law           # a declared law, or the embedded one
        _add("law", "Derived (convert → dBm)" if law_ok else "Derived (no dBm law)", law_ok)
        _add("own", "Separate measurement (dBm)", True)
        i = combo.findData(kind); combo.setCurrentIndex(i if i >= 0 else 0)
        combo.currentIndexChanged.connect(
            lambda _=0, c=combo, s=sub, L=lim_laws: self._on_limiting_kind(c, s, L))
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
        lab = QLabel("follows by"); lab.setFixedWidth(80)
        lab.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_MUTED};")
        row.addWidget(lab); row.addWidget(combo, 1)
        v.addLayout(row)
        read_q = ""
        if kind == "same":
            read_q = meas.get("quantity", "").strip() or "measured quantity"
        elif kind == "law":
            law = sub.get("law") or {}
            # Offer the declared laws PLUS the signal's embedded one — so while the unit's
            # laws are still loading the picker still shows the saved law (selected), rather
            # than emptying and re-picking a different one.
            options = dict(lim_laws)
            if has_law and law.get("id") and law["id"] not in options:
                options[law["id"]] = law
            lid = law.get("id") if law.get("id") in options else next(iter(options), None)
            if lid and law.get("id") != lid:
                sub["kind"] = "law"; sub["law"] = dict(options[lid]); law = sub["law"]
            picker = QComboBox()
            for llid, lw in options.items():
                picker.addItem(lw.get("name", llid), llid)
            k = picker.findData(law.get("id")); picker.setCurrentIndex(k if k >= 0 else 0)
            picker.currentIndexChanged.connect(
                lambda _=0, c=picker, s=sub, L=options: self._on_limiting_law(c, s, L))
            lr = QHBoxLayout(); lr.setContentsMargins(0, 0, 0, 0)
            ll = QLabel("law"); ll.setFixedWidth(80)
            ll.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_MUTED};")
            lr.addWidget(ll); lr.addWidget(picker, 1)
            v.addLayout(lr)
            v.addWidget(self._law_caption(law, "limiting"))
            read_q = sub.get("quantity", "").strip() or law.get("name", "derived power")
        elif kind == "own":
            npts = len((sub.get("curve") or {}).get("points") or [])
            ob = QPushButton(f"Separate points (dBm)…   ({npts} point(s))")
            ob.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            ob.clicked.connect(lambda _=False, s=sid: self._open_own_points_dialog(s))
            orow = QHBoxLayout(); orow.setContentsMargins(0, 0, 0, 0)
            ol = QLabel("its curve"); ol.setFixedWidth(80)
            ol.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_MUTED};")
            hint = QLabel("shares the signal frequency")
            hint.setStyleSheet(f"font-size:10.5px;color:{Palette.TEXT_FAINT};")
            orow.addWidget(ol); orow.addWidget(ob); orow.addWidget(hint); orow.addStretch(1)
            v.addLayout(orow)
            read_q = sub.get("quantity", "").strip() or "Separate dBm measurement"
        reads = QLabel(f"limit reading: <b>{read_q}</b> "
                       f"<span style='color:{Palette.TEXT_MUTED};font-family:monospace;"
                       f"font-size:10.5px;'>[dBm]</span>")
        reads.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_MUTED};")
        v.addWidget(reads)
        return sec

    def _on_limiting_kind(self, combo, sub, lim_laws) -> None:
        data = combo.currentData() or "same"
        if data == "law":
            cur = (sub.get("law") or {}).get("id")
            lid = cur if cur in lim_laws else next(iter(lim_laws), None)
            sub.clear(); sub["kind"] = "law"
            if lid:
                sub["law"] = dict(lim_laws[lid])
        elif data == "own":
            curve = sub.get("curve")
            sub.clear(); sub["kind"] = "own"
            if isinstance(curve, dict):
                sub["curve"] = curve
        else:
            sub.clear(); sub["kind"] = "same"
        self._refresh_form_from_widgets()

    def _on_limiting_law(self, combo, sub, lim_laws) -> None:
        lid = combo.currentData()
        if lid in lim_laws:
            sub["kind"] = "law"; sub["law"] = dict(lim_laws[lid])
            sub.pop("quantity", None)          # follow the law's own name
        self._refresh_form_from_widgets()

    def _open_points_dialog(self, sid: str, plane: str) -> None:
        """The signal's measured gain→power points in a modal dialog (the persistent curve
        table + its sparkline are re-parented in, then detached so closing the dialog can't
        destroy them). A rebuild on close refreshes the button's point count."""
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox
        entry = self._f["signals"].get(sid)
        if not entry or plane not in entry.get("curves", {}):
            return
        tbl = entry["curves"][plane]; spark = entry["sparks"][plane]
        dlg = QDialog(self.window())
        dlg.setWindowTitle(f"Measured points — {sid}")
        v = QVBoxLayout(dlg); v.setSpacing(8)
        cap = QLabel(f"SDR gain → measured value, for “{sid}”. At least two points, "
                     f"gain and value both strictly increasing.")
        cap.setWordWrap(True); cap.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        v.addWidget(cap)
        grid = QHBoxLayout(); grid.addWidget(tbl, 3); grid.addWidget(spark, 2)
        v.addLayout(grid)
        pts = QHBoxLayout()
        addp = QPushButton("+ point"); addp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        addp.clicked.connect(tbl.add_blank_row)
        rmp = QPushButton("− point"); rmp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rmp.clicked.connect(tbl.remove_selected)
        pts.addWidget(addp); pts.addWidget(rmp); pts.addStretch(1)
        v.addLayout(pts)
        # Extrapolation past the measured endpoints. Default None clamps flat (safe, unchanged);
        # Down/Up/Both continue the end-segment slope so --power can reach a gain that wasn't
        # measured (e.g. below a noise-floor-limited low-gain point). Needs agent 1.14.0+.
        ex = QHBoxLayout()
        exlbl = QLabel("Extrapolate:")
        exlbl.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_MUTED};")
        excombo = QComboBox()
        for val, label in _EXTRAPOLATE_LABELS:
            excombo.addItem(label, val)
        cur_mode = str(getattr(tbl, "_extrapolate", "none") or "none").strip().lower()
        idx = excombo.findData(cur_mode)
        excombo.setCurrentIndex(idx if idx >= 0 else 0)
        excombo.setToolTip("How --power behaves past the measured gain range. None clamps at the "
                           "measured endpoints (the safe default). Down/Up/Both continue the end "
                           "slope so you can command power at a gain you didn't measure — the "
                           "commanded gain is still capped by the safety ceiling. Needs agent 1.14.0+.")

        def _set_extrap(_i, t=tbl, c=excombo):
            t._extrapolate = c.currentData()
        excombo.currentIndexChanged.connect(_set_extrap)
        ex.addWidget(exlbl); ex.addWidget(excombo); ex.addStretch(1)
        v.addLayout(ex)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject); bb.accepted.connect(dlg.accept)
        v.addWidget(bb)
        dlg.exec()
        tbl.setParent(None); spark.setParent(None)   # keep the persistent widgets alive
        self._refresh_form_from_widgets()

    def _open_own_points_dialog(self, sid: str) -> None:
        """The Limiting `own` reading's separately-measured dBm curve, in a modal dialog.
        Data-backed (a fresh table seeded from signals.<id>.limiting.curve.points, committed
        on Done) since the own curve only exists while the reading is `own`."""
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox
        entry = self._f["signals"].get(sid)
        if not entry:
            return
        sub = entry["reading"]["limiting"]
        if sub.get("kind") != "own":
            return
        spark = _Sparkline()
        tbl = _CurveTable(headers=("gain (dB)", "power (dBm)"))
        tbl._on_changed = lambda t=tbl, s=spark: s.set_points(t.numeric_points())
        tbl.set_points((sub.get("curve") or {}).get("points"))
        spark.set_points(tbl.numeric_points())
        dlg = QDialog(self.window())
        dlg.setWindowTitle(f"Separate limiting measurement (dBm) — {sid}")
        v = QVBoxLayout(dlg); v.setSpacing(8)
        cap = QLabel("A separately-measured dBm curve backing the limit (e.g. a main-lobe "
                     "measurement). Shares the signal's frequency.")
        cap.setWordWrap(True); cap.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        v.addWidget(cap)
        grid = QHBoxLayout(); grid.addWidget(tbl, 3); grid.addWidget(spark, 2)
        v.addLayout(grid)
        pts = QHBoxLayout()
        addp = QPushButton("+ point"); addp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        addp.clicked.connect(tbl.add_blank_row)
        rmp = QPushButton("− point"); rmp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rmp.clicked.connect(tbl.remove_selected)
        pts.addWidget(addp); pts.addWidget(rmp); pts.addStretch(1)
        v.addLayout(pts)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec():
            pts_out = tbl.points(strict=False)
            if pts_out:
                sub["curve"] = {"points": pts_out}
            else:
                sub.pop("curve", None)
            self._refresh_form_from_widgets()

    def _toggle_signal(self, sid: str) -> None:
        self._expanded_signals ^= {sid}          # flip membership
        self._refresh_form_from_widgets()        # rebuild, preserving committed edits

    def _toggle_all_signals(self, sids: set, currently_all_open: bool) -> None:
        if currently_all_open:
            self._expanded_signals -= sids
        else:
            self._expanded_signals |= sids
        self._refresh_form_from_widgets()

    def _add_signal_to_stage(self, plane: str) -> None:
        """On a downstream measured stage, pick an existing signal to ALSO measure here.
        It's shown (empty, ready for points) until you enter data; a signal you never
        measure here just inherits the upstream curve."""
        candidates = [sid for sid in self._f.get("signals", {})
                      if not self._signal_shown_on(sid, plane)]
        if not candidates:
            QMessageBox.information(
                self, "Measure a signal here",
                "Every signal is already shown on this stage. Add a brand-new signal with "
                "“+ Add signal…” first.")
            return
        sid, ok = QInputDialog.getItem(
            self, "Measure a signal here",
            f"Which signal did you measure on “{plane}”?", candidates, 0, False)
        sid = (sid or "").strip()
        if not ok or not sid:
            return
        self._stage_extra.setdefault(plane, set()).add(sid)
        self._expanded_signals = {sid}
        self._refresh_form_from_widgets()        # keeps the current stage selected

    def _stage_advanced(self, row) -> QWidget:
        """The bits the chain flow doesn't show inline: the stage's id (renaming it
        re-points everything that references it) and a remove action. The stage's role,
        parent and operating status are all implied by its position in the linear chain,
        so there's nothing else to set here."""
        frame = QFrame(); frame.setObjectName("adv")
        frame.setStyleSheet(f"#adv {{ border-top:1px solid {Palette.BORDER}; }}")
        v = QVBoxLayout(frame); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(6)
        cap = QLabel("STAGE SETTINGS")
        cap.setStyleSheet(f"font-size:10px;font-weight:700;letter-spacing:.08em;"
                          f"color:{Palette.TEXT_FAINT};")
        v.addWidget(cap)
        form = QFormLayout(); form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Plane id", row["name"])
        v.addLayout(form)
        actions = QHBoxLayout(); actions.addStretch(1)
        rows = self._f.get("planes", [])
        is_source = bool(rows) and rows[0] is row
        if is_source:
            # The source is the chain's measured origin — everything derives from it and
            # there's no way to re-create it once gone, so it can't be removed. Say so
            # instead of offering a button that would strand the chain.
            note = QLabel("The source stage can't be removed — it's the chain's origin.")
            note.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
            actions.insertWidget(0, note)
        else:
            rm = QPushButton("Remove stage")
            # NB: clicked emits a `checked` bool — absorb it with a leading throwaway
            # parameter, or it clobbers the r=row default (r would become False).
            rm.clicked.connect(lambda _=False, r=row: self._remove_plane(r))
            actions.addWidget(rm)
        v.addLayout(actions)
        return frame

    # ── component library (mockup section 1) ─────────────────────────────────────
    def _render_library(self) -> None:
        while self._lib_grid.count():
            it = self._lib_grid.takeAt(0); w = it.widget()
            if w is not None:
                w.setParent(None); w.deleteLater()
        used = {p.get("component") for p in
                (((self._doc or {}).get("chain") or {}).get("planes") or {}).values()
                if isinstance(p, dict)}
        comps = self._catalog.components()
        cols = 4
        idx = 0
        for cid in self._catalog.ids():
            self._lib_grid.addWidget(
                self._component_card(cid, comps.get(cid) or {}, cid in used), idx // cols, idx % cols)
            idx += 1
        add = _ClickCard(on_click=self._open_components)
        add.setObjectName("addcomp")
        add.setStyleSheet(f"#addcomp {{ border:1px dashed {Palette.BORDER_STRONG}; "
                          f"border-radius:9px; }}")
        av = QVBoxLayout(add); av.setContentsMargins(11, 16, 11, 16); av.setSpacing(4)
        plus = QLabel("+"); plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus.setStyleSheet(f"font-size:22px;color:{Palette.ACCENT};font-weight:600;")
        t = QLabel("Characterize component"); t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet(f"font-size:13px;font-weight:600;color:{Palette.ACCENT};")
        h = QLabel("paste a VNA sweep"); h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        av.addStretch(1); av.addWidget(plus); av.addWidget(t); av.addWidget(h); av.addStretch(1)
        self._lib_grid.addWidget(add, idx // cols, idx % cols)

    def _component_card(self, cid: str, spec: dict, in_chain: bool) -> QWidget:
        kind = (spec.get("kind") or "cable").lower()
        table = spec.get("delta_db_by_freq") or []
        desc = spec.get("description") or ""
        card = _ClickCard(on_click=lambda c=cid: self._open_components(c))
        card.setObjectName("comp")
        card.setStyleSheet(f"#comp {{ background:{Palette.SURFACE_ALT}; "
                           f"border:1px solid {Palette.BORDER}; border-radius:9px; }}")
        v = QVBoxLayout(card); v.setContentsMargins(11, 11, 11, 11); v.setSpacing(8)
        top = QHBoxLayout()
        nm = QLabel(desc or cid); nm.setStyleSheet(f"font-size:13px;font-weight:600;color:{Palette.TEXT};")
        nm.setWordWrap(True)
        top.addWidget(nm, 1)
        fg, bg = _KIND_COLORS.get(kind, _KIND_COLORS["pad"])
        top.addWidget(_badge(kind.capitalize(), fg, bg))
        v.addLayout(top)
        if desc:
            sub = QLabel(cid); sub.setStyleSheet(
                f"font-family:monospace;font-size:10px;color:{Palette.TEXT_FAINT};")
            v.addWidget(sub)
        spark = _FreqSparkline(40); spark.set_table(table, _KIND_COLORS.get(kind, (Palette.ACCENT,))[0])
        v.addWidget(spark)
        foot = QHBoxLayout()
        span = QLabel(_fmt_ghz_span(table))
        span.setStyleSheet(f"font-family:monospace;font-size:10px;color:{Palette.TEXT_MUTED};")
        foot.addWidget(span); foot.addStretch(1)
        tag = QLabel("in this chain" if in_chain else "in library")
        tag.setStyleSheet(f"font-size:10px;color:{Palette.TEXT_FAINT};")
        foot.addWidget(tag)
        v.addLayout(foot)
        return card

    def _read_planes(self, strict: bool) -> dict:
        """Build the planes dict from the ordered stage rows. The chain is LINEAR: each
        derived stage's parent (`from`) is the stage immediately before it, so two stages
        can never share a parent and there's no dangling reference. Preserves any stored
        description/quantity (the editor no longer surfaces quantity, but doesn't drop
        it either)."""
        prev = ((self._doc or {}).get("chain") or {}).get("planes") or {}
        planes: dict = {}
        prev_name: Optional[str] = None
        for idx, row in enumerate(self._f.get("planes", [])):
            name = row["name"].text().strip()
            if not name:
                continue
            role = row.get("role", "measured")
            if role == "measured":
                p = {"type": "measured"}
                # Measurement de-embed (the analyzer-cable loss to remove): a picked catalog
                # component id, or a preserved inline table (JSON-authored).
                if row.get("deembed_custom") is not None:
                    p["measurement_deembed"] = row["deembed_custom"]
                elif row.get("deembed"):
                    p["measurement_deembed"] = row["deembed"]
            elif role == "component":
                p = {"type": "derived", "from": prev_name or "", "component": row.get("comp_id", "")}
            elif role == "active":                    # baseline (constant Δ dB or Δ dB(f)) + control
                p = {"type": "derived", "from": prev_name or ""}
                tbl = [pt for pt in (row.get("baseline_table") or [])
                       if len(pt) == 2 and all(isinstance(x, (int, float)) for x in pt)]
                if tbl:                               # the component's own frequency table
                    p["delta_db_by_freq"] = [[float(f), float(db)] for f, db in tbl]
                else:                                 # flat constant insertion loss
                    d = row["delta"].text().strip()
                    p["delta_db"] = _to_float(d, f"stage '{name}' Δ dB") if d else 0.0
                p["control"] = self._control_from_row(row)
            else:                                     # constant Δ dB
                p = {"type": "derived", "from": prev_name or ""}
                d = row["delta"].text().strip()
                if d:
                    p["delta_db"] = _to_float(d, f"stage '{name}' Δ dB")
                elif strict:
                    raise ValueError(f"stage '{name}' has no Δ dB")
                else:
                    p["delta_db"] = 0.0
            old = prev.get(name)
            if isinstance(old, dict):
                for k in ("description", "quantity"):
                    if old.get(k):
                        p[k] = old[k]
            # A measured stage may be marked reported (report-only). `of` — the limiting
            # curve its limits gauge through — is derived automatically from chain position
            # (nearest limiting stage upstream), never picked by the user. If none is
            # reachable (a passive stage intervenes, or nothing limiting upstream), the
            # reported mark can't be honoured, so the stage stays limiting.
            if p.get("type") == "measured" and row.get("cal_role") == "reported":
                of = self._auto_of(name)
                if of:
                    p["role"] = "reported"
                    p["of"] = of
            # Bypass (never on the source/first stage): a transparent, limits-dropped stage.
            if idx > 0 and row.get("bypass"):
                p["bypass"] = True
            planes[name] = p
            prev_name = name
        # Reported/limiting readings are per-SIGNAL now (docs/calibration-ui-redesign §5) —
        # written to signals.<id> by _read_form. No plane carries a shared reading block, so
        # a legacy operating-plane default (already migrated into each signal on load) is not
        # re-emitted here and drops out of the document on save.
        return planes

    def _remove_plane(self, row) -> None:
        rows = self._f.get("planes", [])
        if rows and rows[0] is row:
            # The source (first) stage is the chain's measured origin and can't be
            # rebuilt from the editor, so removing it would strand the unit. Refuse.
            self._set_status("the source stage can't be removed — it's the chain's origin",
                             kind="warn")
            return
        try:
            self._sync_from(strict=False)
        except ValueError:
            pass
        name = row["name"].text().strip()
        # Removing a plane cascades — it drops that plane's measured points from every
        # signal that has them, plus its limits, and clears the operating pointer if it
        # pointed here. Confirm first, since that's easy to do by a mis-click and not
        # obviously reversible.
        affected = sorted(
            sid for sid, sig in ((self._doc or {}).get("signals") or {}).items()
            if isinstance((sig or {}).get("curves"), dict) and name in sig["curves"])
        if name:
            detail = (f"\n\nThis also removes its measured points from "
                      f"{len(affected)} signal(s): {', '.join(affected)}." if affected else "")
            resp = QMessageBox.question(
                self, "Remove plane",
                f"Remove plane '{name}' from the chain?{detail}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if resp != QMessageBox.StandardButton.Yes:
                self._doc_to_form()               # repaint (undo the widget-level edit)
                return
        chain = (self._doc or {}).get("chain", {})
        planes = chain.get("planes") or {}
        planes.pop(name, None)
        # Drop references to the removed plane so the document stays consistent: its
        # safety limits, its per-signal curves, and the operating-plane pointer if it
        # pointed here. (A derived plane whose parent this was is left for the agent to
        # flag clearly — silently rewiring the chain would be worse than a plain error.)
        chain["limits"] = [l for l in (chain.get("limits") or [])
                           if not (isinstance(l, dict) and l.get("plane") == name)]
        if chain.get("operating_plane") == name:
            chain["operating_plane"] = ""
        for sig in ((self._doc or {}).get("signals") or {}).values():
            curves = (sig or {}).get("curves")
            if isinstance(curves, dict):
                curves.pop(name, None)
        self._doc_to_form()

    def _refresh_form_from_widgets(self) -> None:
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            return
        self._doc_to_form()

    def _on_plane_name_changed(self, row: dict) -> None:
        """A plane was renamed in the form. Read the form back with the OLD name (so
        curves/operating/limits stay consistent), then rename the plane everywhere it's
        referenced, so nothing dangles. Falls back to a plain rebuild when there's no
        real rename or the new name would collide with another plane."""
        new = row["name"].text().strip()
        old = row.get("orig", "")
        planes_now = {r["name"].text().strip() for r in self._f.get("planes", []) if r is not row}
        if not new or new == old or new in planes_now:
            # nothing to propagate (or a name clash — let the generic rebuild/agent
            # surface it); just resync so the rest of the form stays current.
            self._refresh_form_from_widgets()
            return
        row["name"].setText(old)                      # read the form under the old name…
        try:
            doc = self._read_form(strict=False)
        except ValueError:
            row["name"].setText(new)
            self._refresh_form_from_widgets()
            return
        row["name"].setText(new)
        self._doc = _rename_plane_in_doc(doc, old, new)
        row["orig"] = new
        self._doc_to_form()

    def _build_signal_entry(self, sid: str, sig: dict, measured) -> None:
        """Create (but do not place) a signal's editable widgets: amplitude, occupied
        BW, centre frequency, and a curve grid + sparkline per measured plane. The
        measured-stage detail places the ones for the selected plane; _read_form reads
        them all regardless of placement."""
        bw = QLineEdit(_numstr(sig.get("occupied_bw_hz")))
        bw.setToolTip("Occupied bandwidth (Hz), optional.")
        cfreq = QLineEdit(_numstr(sig.get("center_freq_hz")))
        cfreq.setPlaceholderText("Hz — optional; blank folds at a representative frequency")
        cfreq.setToolTip("Centre frequency (Hz) at which this signal's chain is evaluated "
                         "for the --power bounds. Optional even on a frequency-dependent "
                         "chain — the transmit frequency is set at runtime by the task's "
                         "--freq, and left blank the agent folds the bounds at a "
                         "representative (worst-case) frequency. Set it to pin the preview "
                         "to one frequency (needs agent 1.7.1+ to leave blank when a "
                         "cable/antenna is frequency-dependent).")
        cfreq.editingFinished.connect(self._refresh_form_from_widgets)
        plabel = QLineEdit(sig.get("plot_label", ""))
        plabel.setPlaceholderText(sid)
        plabel.setToolTip("The label drawn on the frequency-response plot's dashed line "
                          "for this signal. Blank = a short form of the signal id.")
        plabel.editingFinished.connect(self._refresh_form_from_widgets)

        curves = {}; sparks = {}
        for plane in measured:
            spark = _Sparkline()
            tbl = _CurveTable(on_changed=lambda t=None, s=spark: self._on_curve_changed(s))
            _cur = (sig.get("curves") or {}).get(plane) or {}
            tbl.set_points(_cur.get("points"))
            # Per-curve extrapolation mode (persists on the table widget across rebuilds, edited
            # in the measured-points dialog, serialized by _read_form). Default "none".
            tbl._extrapolate = str(_cur.get("extrapolate") or "none").strip().lower()
            spark.set_points(tbl.numeric_points())
            self._spark_src[spark] = tbl
            curves[plane] = tbl; sparks[plane] = spark
        # Per-signal MEASUREMENT (quantity + unit) and the LIMITING reading, both owned by
        # the signal now (docs/calibration-ui-redesign §5). Stored as mutable dicts the
        # source card's editors mutate in place (like the old plane reading blocks); the
        # widgets are rebuilt each render, the dicts persist. Reported is gone entirely.
        meas = sig.get("measurement") if isinstance(sig.get("measurement"), dict) else {}
        measurement = {"quantity": str(meas.get("quantity") or "").strip(),
                       "unit": str(meas.get("unit") or "dBm").strip() or "dBm"}
        reading = {"limiting": dict(sig.get("limiting") or {})}
        self._f["signals"][sid] = {"bw": bw, "cfreq": cfreq, "plabel": plabel,
                                   "curves": curves, "sparks": sparks,
                                   "measurement": measurement, "reading": reading}

    def _on_curve_changed(self, spark: "_Sparkline") -> None:
        """A curve grid was edited: repaint its sparkline from its source table and
        refresh the live local-issues panel."""
        src = getattr(self, "_spark_src", {}).get(spark)
        if src is not None:
            spark.set_points(src.numeric_points())
        self._update_issues()

    # ── views → model ─────────────────────────────────────────────────────────────
    def _read_form(self, strict: bool) -> dict:
        """Rebuild the document from the editor widgets, preserving fields the form
        doesn't model (schema_version, unit_id, meta, chain.planes, interp/offset_db).
        strict=True raises ValueError on bad numeric input."""
        doc = copy.deepcopy(self._doc) if self._doc else _template()
        ut = self._f["unit_type"].currentData()
        if ut:
            doc["unit_type"] = ut
        # Amplitude is fixed at FIXED_BASEBAND_AMPLITUDE, not an editable field. Normalise an
        # absent or already-matching chain default to exactly that value (so the artifact
        # records it for the runtime gate), but PRESERVE a conflicting legacy value rather
        # than silently relabelling curves measured at a different amplitude.
        defaults = doc.setdefault("defaults", {})
        cur = defaults.get("amplitude")
        if not _amp_conflicts(cur):
            defaults["amplitude"] = FIXED_BASEBAND_AMPLITUDE
        if not defaults:
            doc.pop("defaults", None)
        chain = doc.setdefault("chain", {})
        gl = chain.setdefault("gain_limits", {})
        self._set_num(gl, "min_gain_db", self._f["min_gain"].text(), "min gain", strict)
        self._set_num(gl, "max_gain_db", self._f["max_gain"].text(), "max gain", strict)
        gl.pop("gain_step_db", None)                  # rewritten below only if provided
        self._set_num(gl, "gain_step_db", self._f["gain_step"].text(), "gain step", strict)
        chain["planes"] = self._read_planes(strict)
        # The operating plane is ALWAYS the last stage in the chain (that's where --power
        # is delivered), so it's derived from the order, never set by hand.
        names = list(chain["planes"].keys())
        if names:
            chain["operating_plane"] = names[-1]

        limits = []
        for row in self._f.get("limits", []):
            mx = row["max"].text().strip()
            if not mx:
                if strict:
                    raise ValueError(f"limit on '{row['plane'].currentText()}' has no max dBm")
                continue
            lim = {"plane": row["plane"].currentText(),
                   "max_dbm": _to_float(mx, "limit max dBm")}
            side = row["side"].currentText()
            if side == "input":                 # omit the default 'output' to keep docs clean
                lim["side"] = side
            if row["reason"].text().strip():
                lim["reason"] = row["reason"].text().strip()
            limits.append(lim)
        chain["limits"] = limits

        signals = {}
        prev_sigs = (self._doc or {}).get("signals") or {}
        for sid, w in self._f.get("signals", {}).items():
            # Start from the stored signal so fields the form doesn't model (the JSON
            # tab is the source of truth for those) survive a form round-trip; then
            # overwrite only what the form edits.
            sig = dict(prev_sigs.get(sid) or {})
            # Amplitude is not editable per signal. Drop a stored per-signal amplitude that
            # matches the fixed fleet value (let it inherit the chain default); PRESERVE a
            # conflicting legacy value so it stays flagged and is rejected at runtime.
            if not _amp_conflicts(sig.get("amplitude")):
                sig.pop("amplitude", None)
            if w["bw"].text().strip():
                sig["occupied_bw_hz"] = _to_float(w["bw"].text(), f"{sid} occupied BW")
            else:
                sig.pop("occupied_bw_hz", None)
            if w["cfreq"].text().strip():
                sig["center_freq_hz"] = _to_float(w["cfreq"].text(), f"{sid} centre freq")
            else:
                sig.pop("center_freq_hz", None)
            if w["plabel"].text().strip():
                sig["plot_label"] = w["plabel"].text().strip()
            else:
                sig.pop("plot_label", None)
            curves = {}
            for plane, tbl in w["curves"].items():
                pts = tbl.points(strict)
                if not pts:
                    continue
                prev = ((prev_sigs.get(sid) or {}).get("curves") or {}).get(plane) or {}
                entry = dict(prev)           # preserve unmodeled curve fields too
                entry["points"] = pts
                mode = str(getattr(tbl, "_extrapolate", "none") or "none").strip().lower()
                if mode and mode != "none":  # keep the doc clean when unused (default)
                    entry["extrapolate"] = mode
                else:
                    entry.pop("extrapolate", None)
                curves[plane] = entry
            sig["curves"] = curves
            # Per-signal MEASUREMENT (quantity/unit) and LIMITING reading, from the source
            # card's editor state (docs/calibration-ui-redesign §5). Reported is removed
            # entirely — drop any stored bridge so "gone from the UI" means gone from the doc.
            sig.pop("reported", None)
            lim = _reading_block((w.get("reading") or {}).get("limiting"))
            if lim is not None:
                lim.pop("max_dbm", None)     # per-signal ceiling removed (§5/§6.6): the
                sig["limiting"] = lim         # stage limits list is the dBm cap for all
            else:
                sig.pop("limiting", None)
            mb = _measurement_block(w.get("measurement"))
            if mb is not None:
                sig["measurement"] = mb
            else:
                sig.pop("measurement", None)
            signals[sid] = sig
        doc["signals"] = signals
        return doc

    @staticmethod
    def _set_num(d: dict, key: str, text: str, field: str, strict: bool) -> None:
        text = (text or "").strip()
        if not text:
            return
        try:
            d[key] = _to_float(text, field)
        except ValueError:
            if strict:
                raise

    def _sync_from(self, strict: bool) -> None:
        """Pull the editor form's contents into self._doc. (The JSON view is a separate
        apply-on-close dialog — see _open_json — so the form is always the live model.)"""
        self._doc = self._read_form(strict)

    # ── refresh / load ──────────────────────────────────────────────────────────
    def on_shown(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self._set_status("loading…")
        # Drop cached script params/laws so re-opening the tab reflects task edits made
        # elsewhere (e.g. a changed CAL_POWER_LAWS or a newly-deployed task); they re-fetch
        # once tasks.yaml lands (_handle_tasks). A saved signal keeps its embedded law shown
        # in the meantime, so the Limiting picker never blanks or flips (see _limiting_section).
        self._task_params = {}
        self._task_laws = {}
        self._task_params_inflight = set()
        self.hub.run_async(
            f"cal_get:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_calibration(),
        )
        # Learn this unit's component catalog once, so a fresh client sees the parts
        # already deployed and the chain pickers can resolve existing references.
        if not self._components_synced:
            self._components_synced = True
            self.hub.run_async(
                f"cal_components:{self.hostname}",
                lambda: self.hub.fleet.get(self.hostname).get_components())
        # Learn which calibration signals this unit's tasks reference, to suggest them
        # when adding a signal (best-effort — the picker still works without them).
        self.hub.run_async(
            f"cal_tasks:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_tasks_yaml())

    def _unit_meta(self) -> tuple[str, str]:
        """This unit's type + id, read from its client, to seed a new document with
        the RIGHT unit_type (it selects the shared type-defaults layer, so a wrong one
        silently mis-resolves) instead of the template's hardcoded 'broadcaster'."""
        try:
            c = self.hub.fleet.get(self.hostname)
        except Exception:  # noqa: BLE001
            return "broadcaster", self.hostname
        return (getattr(c, "unit_type", "") or "broadcaster",
                getattr(c, "unit_id", "") or self.hostname)

    def _blank_doc(self) -> dict:
        """A fresh template stamped with this unit's real type + id."""
        doc = _template()
        utype, uid = self._unit_meta()
        doc["unit_type"] = utype
        doc["unit_id"] = uid
        return doc

    def _on_new_template(self) -> None:
        self._set_doc(self._blank_doc())
        utype, _ = self._unit_meta()
        self._set_status(f"template loaded (unit type: {utype}) — edit, then Save", kind="warn")

    # ── actions ───────────────────────────────────────────────────────────────
    def _on_upload(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Upload calibration.json", "", "JSON (*.json);;All files (*)")
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                content = fh.read()
        except OSError as exc:
            self._set_status(f"could not read file: {exc}", kind="error")
            return
        self._send(content)

    def _on_save(self) -> bool:
        """Validate + push the calibration. Returns True when a save was dispatched, False
        when it was blocked (invalid form, nothing to save, or a capability guard) — the
        host uses that to decide whether it's safe to leave the tab."""
        try:
            self._sync_from(strict=True)
        except ValueError as exc:
            self._set_status(f"cannot save: {exc}", kind="error")
            return False
        if self._doc is None:
            self._set_status("nothing to save", kind="error")
            return False
        if (self._blocks_on_components() or self._blocks_on_partial_stages()
                or self._blocks_on_no_signals() or self._blocks_on_limit_side()
                or self._blocks_on_plane_roles() or self._blocks_on_gain_step()
                or self._blocks_on_freq_optional_center() or self._blocks_on_active_components()
                or self._blocks_on_source_bias() or self._blocks_on_stage_bypass()
                or self._blocks_on_power_bridges() or self._blocks_on_deembed()
                or self._blocks_on_measurement_quantity()
                or self._blocks_on_limit_through_reading()
                or self._blocks_on_extrapolate()):
            return False
        self._send(json.dumps(self._doc).encode("utf-8"))
        return True

    def request_save(self) -> bool:
        """Public entry the host calls to save on the user's behalf (e.g. from the
        leave-with-unsaved-changes prompt). Returns whether a save was dispatched."""
        return self._on_save()

    @staticmethod
    def _doc_uses_components(doc) -> bool:
        return any(isinstance(p, dict) and p.get("component")
                   for p in (((doc or {}).get("chain") or {}).get("planes") or {}).values())

    @staticmethod
    def _doc_uses_active_components(doc) -> bool:
        return any(isinstance(p, dict) and p.get("control") is not None
                   for p in (((doc or {}).get("chain") or {}).get("planes") or {}).values())

    def _blocks_on_active_components(self) -> bool:
        """Guard: don't push an active-component document to an agent that predates the
        feature (it would reject the control block). Returns True (and says why) when blocked."""
        if (self._doc_uses_active_components(self._doc)
                and not self._supports(CAL_ACTIVE_COMPONENTS_CAPABILITY)):
            self._set_status(_ACTIVE_COMPONENTS_NEEDS_NEWER, kind="error")
            return True
        return False

    @staticmethod
    def _doc_uses_source_bias(doc) -> bool:
        sb = (doc or {}).get("source_bias")
        return isinstance(sb, dict) and bool(sb.get("power_by_freq"))

    def _blocks_on_source_bias(self) -> bool:
        """Guard: don't push a source-bias document to an agent that predates it (it would
        ignore the bias and mis-report power vs frequency)."""
        if (self._doc_uses_source_bias(self._doc)
                and not self._supports(CAL_SOURCE_BIAS_CAPABILITY)):
            self._set_status(_SOURCE_BIAS_NEEDS_NEWER, kind="error")
            return True
        return False

    @staticmethod
    def _doc_uses_bypass(doc) -> bool:
        planes = ((doc or {}).get("chain") or {}).get("planes") or {}
        if any(isinstance(p, dict) and p.get("bypass") for p in planes.values()):
            return True
        sb = (doc or {}).get("source_bias")
        return isinstance(sb, dict) and bool(sb.get("bypass"))

    def _blocks_on_stage_bypass(self) -> bool:
        """Guard: don't push a bypassed stage to an agent that predates it (it would reject
        the bypass field)."""
        if self._doc_uses_bypass(self._doc) and not self._supports(CAL_STAGE_BYPASS_CAPABILITY):
            self._set_status(_STAGE_BYPASS_NEEDS_NEWER, kind="error")
            return True
        return False

    @staticmethod
    def _doc_uses_power_bridges(doc) -> bool:
        """True when any plane OR signal carries a non-trivial reported/limiting bridge —
        anything a ≤1.9.0 agent would silently ignore (mis-reporting the power quantity)."""
        def has(holder):
            return any(_reading_block((holder or {}).get(k)) is not None
                       for k in ("reported", "limiting"))
        planes = ((doc or {}).get("chain") or {}).get("planes") or {}
        if any(isinstance(p, dict) and has(p) for p in planes.values()):
            return True
        return any(isinstance(s, dict) and has(s)
                   for s in ((doc or {}).get("signals") or {}).values())

    @staticmethod
    def _doc_uses_deembed(doc) -> bool:
        return any(isinstance(p, dict) and p.get("measurement_deembed")
                   for p in (((doc or {}).get("chain") or {}).get("planes") or {}).values())

    def _blocks_on_deembed(self) -> bool:
        """Guard (safety): don't push a measurement de-embed to an agent that predates it —
        it would leave the analyzer-cable loss baked into the measurement."""
        if (self._doc_uses_deembed(self._doc)
                and not self._supports(CAL_MEASUREMENT_DEEMBED_CAPABILITY)):
            self._set_status(_DEEMBED_NEEDS_NEWER, kind="error")
            return True
        return False

    def _blocks_on_power_bridges(self) -> bool:
        """Guard (safety): don't push a reported/limiting bridge to an agent that predates it —
        it would ignore `readings`, report --power in the measured quantity, and skip the
        limiting cap."""
        if (self._doc_uses_power_bridges(self._doc)
                and not self._supports(CAL_POWER_BRIDGES_CAPABILITY)):
            self._set_status(_POWER_BRIDGES_NEEDS_NEWER, kind="error")
            return True
        return False

    @staticmethod
    def _doc_uses_measurement_quantity(doc) -> bool:
        """True when any signal declares a per-signal ``measurement`` block (quantity/unit) —
        a ≤1.11.x agent ignores it and shows --power in the plane quantity / dBm."""
        return any(isinstance(s, dict) and isinstance(s.get("measurement"), dict)
                   and s["measurement"]
                   for s in ((doc or {}).get("signals") or {}).values())

    def _blocks_on_measurement_quantity(self) -> bool:
        """Guard: don't push a per-signal measurement quantity/unit to an agent that predates
        it — it would ignore the declared unit and mislabel the operator's --power axis."""
        if (self._doc_uses_measurement_quantity(self._doc)
                and not self._supports(CAL_MEASUREMENT_QUANTITY_CAPABILITY)):
            self._set_status(_MEASUREMENT_QUANTITY_NEEDS_NEWER, kind="error")
            return True
        return False

    @staticmethod
    def _doc_uses_limit_through_reading(doc) -> bool:
        """True when the chain has a stage limit AND some signal (or the operating-plane spec)
        declares a NON-TRIVIAL limiting reading (a law / own / same+k). Then a stage limit is
        gauged THROUGH that reading (agent >= 1.13.0); a ≤1.12.0 agent gauges it in the measured
        quantity instead — under-applying the ceiling (over-power). A trivial "same as measured"
        limiting doesn't change the gauging, so it doesn't need the newer agent."""
        chain = (doc or {}).get("chain") or {}
        if not (chain.get("limits") or []):
            return False

        def has_lim(holder):
            return _reading_block((holder or {}).get("limiting")) is not None

        planes = chain.get("planes") or {}
        if any(isinstance(p, dict) and has_lim(p) for p in planes.values()):
            return True
        return any(isinstance(s, dict) and has_lim(s)
                   for s in ((doc or {}).get("signals") or {}).values())

    def _blocks_on_limit_through_reading(self) -> bool:
        """Guard (safety): don't push a document whose stage limit is gauged through a limiting
        reading to an agent that predates it — it would compare the dBm ceiling against the
        measured quantity and resolve a ceiling that is too high (over-power)."""
        if (self._doc_uses_limit_through_reading(self._doc)
                and not self._supports(CAL_LIMIT_THROUGH_READING_CAPABILITY)):
            self._set_status(_LIMIT_THROUGH_READING_NEEDS_NEWER, kind="error")
            return True
        return False

    @staticmethod
    def _doc_uses_extrapolate(doc) -> bool:
        """True when any signal's measured curve sets extrapolate to a non-``none`` mode — a
        ≤1.13.1 agent ignores it and CLAMPS, so the range the unit delivers would be narrower
        than the client shows (a command in the extrapolated region maps to a different power)."""
        for s in ((doc or {}).get("signals") or {}).values():
            if not isinstance(s, dict):
                continue
            for c in (s.get("curves") or {}).values():
                v = c.get("extrapolate") if isinstance(c, dict) else None
                if v and str(v).strip().lower() not in ("", "none"):
                    return True
        return False

    def _blocks_on_extrapolate(self) -> bool:
        """Guard (safety): don't push an extrapolating curve to an agent that predates it — it
        would clamp instead, so a --power the operator authored in the extrapolated region would
        be delivered at a different (clamped) power than the client showed."""
        if (self._doc_uses_extrapolate(self._doc)
                and not self._supports(CAL_EXTRAPOLATE_CAPABILITY)):
            self._set_status(_EXTRAPOLATE_NEEDS_NEWER, kind="error")
            return True
        return False

    def _blocks_on_components(self) -> bool:
        """Guard: don't push a component-referencing document to an agent that can't
        resolve it (it would reject the derived plane confusingly). Returns True (and
        shows why) when blocked."""
        if self._doc_uses_components(self._doc) and not self._supports(CAL_COMPONENTS_CAPABILITY):
            self._set_status(_COMPONENTS_NEEDS_NEWER, kind="error")
            return True
        return False

    @staticmethod
    def _doc_uses_partial_stages(doc) -> bool:
        """True when a signal omits the curve for a non-first MEASURED stage — it would
        rely on the agent's inherit-the-upstream-curve fallback (agent >= 1.3.0)."""
        planes = ((doc or {}).get("chain") or {}).get("planes") or {}
        names = list(planes.keys())
        downstream_measured = [n for i, n in enumerate(names)
                               if i > 0 and isinstance(planes.get(n), dict)
                               and planes[n].get("type") == "measured"]
        if not downstream_measured:
            return False
        for sig in ((doc or {}).get("signals") or {}).values():
            curves = (sig or {}).get("curves") or {}
            if any(dm not in curves for dm in downstream_measured):
                return True
        return False

    def _blocks_on_partial_stages(self) -> bool:
        """Guard: an agent older than 1.3.0 rejects a signal that skips a downstream
        measured stage, so warn rather than let it fail confusingly on the unit."""
        if self._doc_uses_partial_stages(self._doc) and not self._supports(
                CAL_PARTIAL_STAGES_CAPABILITY):
            self._set_status(_PARTIAL_STAGES_NEEDS_NEWER, kind="error")
            return True
        return False

    def _blocks_on_no_signals(self) -> bool:
        """Guard: an agent older than 1.4.0 rejects a signal-less document ("document has
        no signals"), so warn rather than let a first onboarding Save fail confusingly."""
        if not ((self._doc or {}).get("signals") or {}) and not self._supports(
                CAL_NO_SIGNALS_CAPABILITY):
            self._set_status(_NO_SIGNALS_NEEDS_NEWER, kind="error")
            return True
        return False

    @staticmethod
    def _doc_uses_limit_side(doc) -> bool:
        """True when a limit sets side: input. (An explicit 'output' is identical to the
        default, so an older agent resolves it correctly — only 'input' needs the newer
        agent.)"""
        return any(isinstance(l, dict) and l.get("side") == "input"
                   for l in (((doc or {}).get("chain") or {}).get("limits") or []))

    def _blocks_on_limit_side(self) -> bool:
        """Guard: an agent older than 1.5.0 IGNORES a limit's side and would apply an
        input-side cap at the plane's output — a different, unsafe limit — so refuse to
        push rather than silently mis-protect the hardware."""
        if self._doc_uses_limit_side(self._doc) and not self._supports(
                CAL_LIMIT_SIDE_CAPABILITY):
            self._set_status(_LIMIT_SIDE_NEEDS_NEWER, kind="error")
            return True
        return False

    @staticmethod
    def _doc_uses_plane_roles(doc) -> bool:
        """True when any measured plane is marked role: reported. (An explicit 'limiting' is
        identical to the default, so only 'reported' needs the newer agent.)"""
        return any(isinstance(p, dict) and p.get("role") == "reported"
                   for p in (((doc or {}).get("chain") or {}).get("planes") or {}).values())

    def _blocks_on_plane_roles(self) -> bool:
        """Guard: an agent older than 1.6.0 doesn't understand role/of and would treat a
        reported (report-only) stage as an ordinary limiting one — inverting a limit through
        the wrong-quantity curve and mis-gauging the ceiling — so refuse to push."""
        if self._doc_uses_plane_roles(self._doc) and not self._supports(
                CAL_PLANE_ROLES_CAPABILITY):
            self._set_status(_PLANE_ROLES_NEEDS_NEWER, kind="error")
            return True
        return False

    @staticmethod
    def _doc_uses_gain_step(doc) -> bool:
        return ((((doc or {}).get("chain") or {}).get("gain_limits") or {})
                .get("gain_step_db") is not None)

    def _blocks_on_gain_step(self) -> bool:
        """Guard: an agent older than 1.7.0 ignores gain_step_db and would command an
        off-grid gain the SDR silently rounds, so the delivered power wouldn't match the
        calibration — refuse to push rather than mislead."""
        if self._doc_uses_gain_step(self._doc) and not self._supports(
                CAL_GAIN_STEP_CAPABILITY):
            self._set_status(_GAIN_STEP_NEEDS_NEWER, kind="error")
            return True
        return False

    def _doc_uses_freq_optional_center(self, doc) -> bool:
        """True when the chain is frequency-dependent (a derived plane references a
        multi-point component table) AND at least one signal declares no center_freq_hz.
        Only then does an agent older than 1.7.1 reject the document (a flat / single-point
        component needs no frequency, and a signal that supplies its own centre frequency
        resolves on any agent)."""
        planes = ((doc or {}).get("chain") or {}).get("planes") or {}
        freq_dep = any(isinstance(p, dict) and p.get("component")
                       and len(self._comp_table(p.get("component"))) > 1
                       for p in planes.values())
        if not freq_dep:
            return False
        return any(not (sig or {}).get("center_freq_hz")
                   for sig in ((doc or {}).get("signals") or {}).values())

    def _blocks_on_freq_optional_center(self) -> bool:
        """Guard: an agent older than 1.7.1 rejects a frequency-dependent chain whose signal
        has no center_freq_hz ("uses a frequency-dependent component but has no
        'center_freq_hz'"), so warn rather than let Save fail confusingly on the unit."""
        if self._doc_uses_freq_optional_center(self._doc) and not self._supports(
                CAL_FREQ_OPTIONAL_CENTER_CAPABILITY):
            self._set_status(_FREQ_OPTIONAL_CENTER_NEEDS_NEWER, kind="error")
            return True
        return False

    def _supports(self, capability: str) -> bool:
        try:
            client = self.hub.fleet.get(self.hostname)
        except Exception:  # noqa: BLE001
            return False
        return bool(getattr(client, "supports", lambda _c: False)(capability))

    def _sync_validate_button(self) -> None:
        ok = self._supports(CAL_VALIDATE_CAPABILITY)
        self._validate_btn.setEnabled(ok)
        self._validate_btn.setToolTip(
            "Check this document against the unit WITHOUT saving it — preview what each "
            "signal resolves to, or why it's rejected." if ok else
            "This unit's agent is too old for dry-run validate (needs 1.1.9+). "
            "Local checks still run above; use Save to validate on the unit.")

    def _on_validate(self) -> None:
        # Parse the form the SAME way Save does (strict). A non-strict read silently
        # drops malformed input — e.g. text typed into a numeric cell — so the doc
        # would validate "clean" and then fail on Save. Surface those errors here
        # instead, before any dry-run against the unit.
        try:
            self._sync_from(strict=True)
        except ValueError as exc:
            self._update_issues()          # reflect what local checks can see too
            self._set_status(f"invalid — would fail to save: {exc}", kind="error")
            return
        if self._doc is None:
            self._set_status("nothing to validate", kind="faint")
            return
        self._update_issues()                       # instant local pass
        issues = local_calibration_issues(self._doc)
        if not self._supports(CAL_VALIDATE_CAPABILITY):
            self._set_status(
                f"{len(issues)} local issue(s) found — see above" if issues else
                "no local issues found (agent too old to dry-run on the unit)",
                kind="error" if issues else "warn")
            return
        if (self._blocks_on_components() or self._blocks_on_partial_stages()
                or self._blocks_on_no_signals() or self._blocks_on_limit_side()
                or self._blocks_on_plane_roles() or self._blocks_on_gain_step()
                or self._blocks_on_freq_optional_center()
                or self._blocks_on_source_bias() or self._blocks_on_stage_bypass()):
            return
        self._set_status("validating (dry run — not saving)…")
        doc = self._doc
        wire = self._catalog.to_wire()
        host = self.hostname

        def _do():
            client = self.hub.fleet.get(host)
            client.upload_components(wire)       # so component refs resolve in the dry-run
            return client.validate_calibration(doc)
        self.hub.run_async(f"cal_validate:{host}", _do)

    def _send(self, content: bytes) -> None:
        self._set_status("validating + saving…")
        wire = self._catalog.to_wire()
        host = self.hostname

        def _do():
            client = self.hub.fleet.get(host)
            client.upload_components(wire)       # push the catalog first so refs resolve
            return client.upload_file(CAL_NAME, content)
        self.hub.run_async(f"cal_save:{host}", _do)

    # ── Component library ────────────────────────────────────────────────────────
    def _open_components(self, select: Optional[str] = None) -> None:
        """Open the shared component library, optionally on a specific component. A
        rename inside the dialog is applied to this unit's chain references too, so a
        renamed part doesn't dangle. Refresh the chain afterward."""
        try:
            self._sync_from(strict=False)
        except ValueError:
            pass
        from .component_library_dialog import ComponentLibraryDialog
        dlg = ComponentLibraryDialog(self._catalog, parent=self.window(),
                                     select=select if isinstance(select, str) else None)
        dlg.exec()
        for old, new in dlg.renames.items():          # re-point chain references
            for p in (((self._doc or {}).get("chain") or {}).get("planes") or {}).values():
                if isinstance(p, dict) and p.get("component") == old:
                    p["component"] = new
        self._doc_to_form()                  # rebuild the chain with the updated catalog

    def _on_download(self) -> None:
        try:
            self._sync_from(strict=False)
        except ValueError:
            pass
        if self._doc is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save calibration.json", CAL_NAME, "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._doc, fh, indent=2)
        except OSError as exc:
            self._set_status(f"could not save: {exc}", kind="error")
            return
        self._set_status(f"downloaded to {path}")

    # ── results ─────────────────────────────────────────────────────────────────
    def _on_task_done(self, label: str, result) -> None:
        if not label.startswith("cal_"):
            return
        parts = label.split(":")
        if len(parts) < 2 or parts[1] != self.hostname:
            return
        if parts[0] == "cal_get":
            self._handle_get(result)
        elif parts[0] == "cal_save":
            self._handle_save(result)
        elif parts[0] == "cal_validate":
            self._handle_validate(result)
        elif parts[0] == "cal_components":
            self._handle_components(result)
        elif parts[0] == "cal_tasks":
            self._handle_tasks(result)
        elif parts[0] == "cal_taskparams":
            self._handle_taskparams(parts[2] if len(parts) > 2 else "", result)
        elif parts[0] == "cal_taskrename":
            self._handle_task_rename(parts[2] if len(parts) > 2 else "", result)
        elif parts[0] == "cal_rename_save":
            self._handle_rename_save(result)
        elif parts[0] == "cal_fleetrename":
            self._handle_fleet_rename(result)

    def _handle_tasks(self, result) -> None:
        """Record which calibration signal each of this unit's tasks references (via its
        SDR_CAL_SIGNAL_ID) — used to suggest ids when adding a signal, and to offer to
        rename a task's signal when the signal is renamed here. Best-effort."""
        if isinstance(result, Exception) or not isinstance(result, str) or not result.strip():
            return
        try:
            from .timeline_editor import task_signals_from_yaml
            self._tasks_yaml = result
            self._task_signals = task_signals_from_yaml(result)
            self._task_signal_ids = sorted(set(self._task_signals.values()))
        except Exception:  # noqa: BLE001 — a broken tasks file shouldn't break the panel
            return
        # Now that we know which task references which signal, fetch each task's script params
        # (which carry CAL_POWER_LAWS) up front, and re-render — so a signal's Derived law
        # picker populates automatically once the unit answers, without the user navigating
        # around to trigger a render. _fetch_task_params de-dupes per script (cached/inflight).
        for tname in self._task_signals:
            self._fetch_task_params(tname)
        self._render_detail()

    def _handle_components(self, result) -> None:
        """Merge the unit's stored catalog into the local one (additive — never clobbers
        a locally-authored component), then refresh the chain pickers."""
        if isinstance(result, Exception) or not isinstance(result, str) or not result.strip():
            return
        try:
            from state import ComponentCatalog
            added = self._catalog.merge(ComponentCatalog.parse_wire(result))
        except Exception:  # noqa: BLE001 — a broken unit catalog shouldn't break the panel
            return
        if added:
            self._doc_to_form()                  # so the new components appear in pickers

    def _handle_validate(self, result) -> None:
        if isinstance(result, Exception):
            self._set_status(f"validate failed: {result}", kind="error")
            return
        if not isinstance(result, dict):
            self._set_status("unexpected response", kind="error")
            return
        if result.get("valid"):
            self._populate_table(result.get("signals") or {})
            self._remember_resolved(result.get("signals") or {})
            n = len(result.get("signals") or {})
            self._set_status(
                f"valid ✓ (dry run) · {n} signal(s) resolve — NOT saved yet", kind="ok")
        else:
            self._set_status(f"would be REJECTED: {result.get('error', '')}", kind="error")

    def _agent_lacks_calibration(self) -> bool:
        """Definitive when the unit's /info has been read (agent_version is set): the
        agent is reachable but advertises no calibration capability ⇒ it's too old.
        When /info hasn't been read yet, returns False and the caller falls back to the
        404 heuristic on the actual response."""
        try:
            client = self.hub.fleet.get(self.hostname)
        except Exception:  # noqa: BLE001
            return False
        return bool(getattr(client, "agent_version", "")) and not (
            getattr(client, "supports", lambda _c: False)(CAL_CAPABILITY))

    def _is_outdated(self, result) -> bool:
        """Prefer the explicit capability flag; fall back to the 404 heuristic."""
        return self._agent_lacks_calibration() or _is_outdated_agent(result)

    def _handle_get(self, result) -> None:
        if self._is_outdated(result):
            self._set_doc(None)
            self._table.setRowCount(0)
            self._set_status(_OUTDATED_AGENT_MSG, kind="error")
            return
        if isinstance(result, AgentHTTPError) and result.status_code == 404:
            self._set_doc(None)
            self._table.setRowCount(0)
            self._set_status("not calibrated — start from a template or Upload…", kind="faint")
            return
        if isinstance(result, Exception):
            self._set_status(f"error: {result}", kind="error")
            return
        if not isinstance(result, dict):
            self._set_status("unexpected response", kind="error")
            return
        self._set_doc(result.get("document"))
        # Remember the persisted document so a signal rename can be pushed on its own
        # (without saving the working doc's other in-progress edits).
        self._saved_doc = copy.deepcopy(result.get("document"))
        utype = result.get("unit_type") or "—"
        if result.get("valid"):
            from state.calibration_cache import get_calibration_cache
            get_calibration_cache().put(self.hostname, result)   # remember for offline
            self._populate_table(result.get("signals") or {})
            self._remember_resolved(result.get("signals") or {})
            n = len(result.get("signals") or {})
            self._set_status(f"calibrated ✓  ·  type {utype}  ·  {n} signal(s) resolve", kind="ok")
        else:
            self._table.setRowCount(0)
            self._set_status(f"stored document is INVALID: {result.get('error', '')}", kind="error")

    def _handle_save(self, result) -> None:
        if self._is_outdated(result):
            self._set_status(_OUTDATED_AGENT_MSG, kind="error")
            QMessageBox.warning(self, "Agent out of date",
                                "This unit's agent has no file-upload endpoint, so the "
                                "calibration could not be saved.\n\nUpdate the agent "
                                "(unit ••• menu → “Update agent…”), then try again.")
            return
        if isinstance(result, AgentHTTPError) and result.status_code == 400:
            self._set_status("rejected — not saved", kind="error")
            QMessageBox.warning(self, "Calibration rejected",
                                f"The unit rejected this calibration and did not store it:"
                                f"\n\n{result.detail}")
            return
        if isinstance(result, Exception):
            self._set_status(f"error: {result}", kind="error")
            return
        summary = result.get("calibration") if isinstance(result, dict) else None
        n = len(summary) if isinstance(summary, dict) else 0
        self._set_status(f"saved ✓  ·  {n} signal(s) valid", kind="ok")
        self._refresh()

    # ── helpers ─────────────────────────────────────────────────────────────────
    def _suggested_signal_ids(self) -> list:
        """Signal ids to offer when adding a signal: those this unit's tasks reference plus
        any the fleet's calibration cache has seen, minus ids already in this document
        (no point re-adding). Task-referenced ids come first — those are the ones a task
        actually expects this document to define."""
        from state.calibration_cache import get_calibration_cache
        have = set(((self._doc or {}).get("signals") or {}).keys())
        out: list = []
        for sid in list(self._task_signal_ids) + get_calibration_cache().known_signal_ids():
            if sid and sid not in have and sid not in out:
                out.append(sid)
        return out

    def _on_add_signal(self) -> None:
        suggestions = self._suggested_signal_ids()
        if suggestions:
            # Editable combo: pick an id a task/the fleet already uses, or type a new one.
            sid, ok = QInputDialog.getItem(
                self, "Add signal", "Signal id (pick one your tasks use, or type a new one):",
                suggestions, 0, True)
        else:
            sid, ok = QInputDialog.getText(
                self, "Add signal", "Signal id (e.g. gps_l1_mcode):")
        sid = (sid or "").strip()
        if not ok or not sid:
            return
        # sync current edits, then add an empty signal for each measured plane
        try:
            self._sync_from(strict=False)
        except ValueError:
            pass
        if self._doc is None:
            self._doc = self._blank_doc()
        self._doc.setdefault("signals", {})[sid] = {"curves": {}}
        self._download_btn.setEnabled(True)
        self._doc_to_form()

    def _on_remove_signal(self, sid: str) -> None:
        """Confirm, then delete a signal (it drops all its measured curves), and drop it
        from the expanded set so nothing dangles on the rebuild."""
        if QMessageBox.question(
                self, "Remove signal",
                f"Remove signal '{sid}' and all its measured curves from this "
                f"calibration?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        self._expanded_signals.discard(sid)
        self._remove_signal(sid)

    def _remove_signal(self, sid: str) -> None:
        try:
            self._sync_from(strict=False)
        except ValueError:
            pass
        if self._doc and sid in (self._doc.get("signals") or {}):
            del self._doc["signals"][sid]
        for shown in self._stage_extra.values():
            shown.discard(sid)
        self._doc_to_form()

    def _rename_signal(self, old: str, new: str) -> None:
        """Rename a signal id in place, keeping its curves/amplitude/plot label — so a task
        rename can be mirrored here without deleting and re-entering the signal. The id is
        only a dict key in the calibration document (curves nest under it), so a key rename
        that preserves order is enough."""
        new = (new or "").strip()
        if not new or new == old:
            return
        # Fold current widget edits into the model first, so nothing typed is lost.
        try:
            self._sync_from(strict=False)
        except ValueError:
            pass
        signals = (self._doc or {}).get("signals") or {}
        if old not in signals:
            return                                    # already renamed (re-entrant blur)
        if new in signals:
            QMessageBox.warning(self, "Rename signal",
                                f"A signal named “{new}” already exists — pick another id.")
            self._doc_to_form()                       # repaint the field back to the old id
            return
        # A calibration signal id is a fleet-wide contract name: the shared task library
        # carries one SDR_CAL_SIGNAL_ID deployed to every unit. So if any task references
        # the old id (here or in the library), renaming is a fleet-wide change — confirm it
        # up front. Yes propagates everywhere; Cancel aborts (the signal stays as it was).
        local_tasks = sorted(n for n, s in self._task_signals.items() if s == old)
        lib_tasks = self._library_tasks_referencing(old)
        go_fleet = False
        if local_tasks or lib_tasks:
            others = self._other_unit_hosts()
            scope = ["the shared task library", "this unit’s calibration and tasks"]
            if others:
                scope.append(f"{len(others)} other unit(s) — online ones now, "
                             f"offline ones reported so you can finish them later")
            resp = QMessageBox.question(
                self, "Rename signal across the fleet",
                f"Calibration signal ids are shared across the fleet, and “{old}” is used "
                f"by task(s). Renaming it to “{new}” is a fleet-wide change.\n\n"
                f"This updates:\n• " + "\n• ".join(scope) + f"\n\n"
                f"Rename “{old}” → “{new}” across the fleet?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes)
            if resp != QMessageBox.StandardButton.Yes:
                self._doc_to_form()                   # abort — repaint the name back
                return
            go_fleet = True
        self._doc["signals"] = {(new if k == old else k): v for k, v in signals.items()}
        if old in self._expanded_signals:             # keep it expanded under the new id
            self._expanded_signals.discard(old)
            self._expanded_signals.add(new)
        for shown in self._stage_extra.values():      # keep it shown on any downstream stage
            if old in shown:
                shown.discard(old)
                shown.add(new)
        self._set_status(f"renamed signal “{old}” → “{new}”", kind="ok")
        self._doc_to_form()
        self._persist_signal_rename(old, new)         # persist just the rename (not a full save)
        if go_fleet:
            self._rename_library_tasks(old, new)      # the deploy source
            if local_tasks:
                self._rename_tasks_signal(local_tasks, new)   # this unit's live tasks
            self._rename_across_fleet(old, new)       # every other unit's cal + tasks

    def _persist_signal_rename(self, old: str, new: str) -> None:
        """Push ONLY the signal rename to the unit — not the working doc's other unsaved
        edits. Renames the key in a copy of the last-persisted calibration and uploads
        that, so the rename sticks (matching the tasks we just updated) without a full Save
        and without discarding the user's in-progress edits on refresh. No-op until the
        unit has a stored calibration that actually contains the old id."""
        saved = self._saved_doc
        if not isinstance(saved, dict) or old not in (saved.get("signals") or {}):
            return
        renamed = copy.deepcopy(saved)
        renamed["signals"] = {(new if k == old else k): v
                              for k, v in (renamed.get("signals") or {}).items()}
        self._saved_doc = renamed                      # our view of what's now persisted
        host = self.hostname
        content = json.dumps(renamed).encode("utf-8")
        wire = self._catalog.to_wire()

        def _do():
            client = self.hub.fleet.get(host)
            client.upload_components(wire)             # keep component refs resolvable
            return client.upload_file(CAL_NAME, content)
        self.hub.run_async(f"cal_rename_save:{host}", _do)

    def _handle_rename_save(self, result) -> None:
        # Success is silent — the working doc already shows the rename and we must NOT
        # refresh (that would replace it and drop the user's other unsaved edits). Only a
        # failure is worth surfacing.
        if isinstance(result, Exception):
            self._set_status(f"couldn't persist the signal rename: {result}", kind="error")

    # ── Fleet-wide signal rename (library + every other unit) ─────────────────────
    def _library_store(self):
        getter = getattr(getattr(self.hub, "fleet", None), "library_store", None)
        return getter() if callable(getter) else None

    def _other_unit_hosts(self) -> list:
        fleet = getattr(self.hub, "fleet", None)
        names = getattr(fleet, "hostnames", None)
        if not callable(names):
            return []
        return [h for h in names() if h != self.hostname]

    def _library_tasks_referencing(self, sid: str) -> list:
        """Names of canonical-library tasks whose SDR_CAL_SIGNAL_ID is `sid`."""
        store = self._library_store()
        if store is None:
            return []
        return [t.name for t in store.tasks()
                if (getattr(t, "env", None) or {}).get("SDR_CAL_SIGNAL_ID") == sid]

    def _rename_library_tasks(self, old: str, new: str) -> int:
        """Repoint the shared library's task(s) at the new id — the deploy source, so a
        later library→unit deploy carries the rename instead of reinstating the old id."""
        store = self._library_store()
        if store is None:
            return 0
        n = 0
        for t in store.tasks():
            if (getattr(t, "env", None) or {}).get("SDR_CAL_SIGNAL_ID") == old:
                t.env["SDR_CAL_SIGNAL_ID"] = new
                store.upsert_task(t)
                n += 1
        return n

    def _rename_across_fleet(self, old: str, new: str) -> None:
        """Rename the signal on every OTHER unit — its calibration and its tasks — off the
        GUI thread. Units we can't reach (offline) are reported, not silently skipped."""
        hosts = self._other_unit_hosts()
        if not hosts:
            return
        wire = self._catalog.to_wire()

        def _do():
            fleet = self.hub.fleet
            renamed, no_signal, unreachable = [], [], []
            for h in hosts:
                try:
                    client = fleet.get(h)
                    touched = self._rename_signal_on_client(client, old, new, wire)
                except Exception:  # noqa: BLE001 — offline / transport error → report it
                    unreachable.append(h)
                    continue
                (renamed if touched else no_signal).append(h)
            return {"renamed": renamed, "no_signal": no_signal, "unreachable": unreachable}
        self._set_status(f"renaming “{old}” → “{new}” across the fleet…")
        self.hub.run_async(f"cal_fleetrename:{self.hostname}", _do)

    def _rename_signal_on_client(self, client, old: str, new: str, wire: str) -> bool:
        """Rename the signal on ONE unit: the key in its calibration.json (if present) and
        any task env that references it. Runs on the worker thread (agent I/O). Returns
        True if anything was changed. A 404 calibration (unit not calibrated) is not an
        error — its tasks may still reference the id."""
        touched = False
        try:
            res = client.get_calibration()
            doc = res.get("document") if isinstance(res, dict) else None
            if isinstance(doc, dict) and old in (doc.get("signals") or {}):
                doc = copy.deepcopy(doc)
                doc["signals"] = {(new if k == old else k): v
                                  for k, v in doc["signals"].items()}
                client.upload_components(wire)
                client.upload_file(CAL_NAME, json.dumps(doc).encode("utf-8"))
                touched = True
        except AgentHTTPError as exc:
            if exc.status_code != 404:                # 404 = not calibrated (fine); else real
                raise
        import yaml as _yaml
        tdoc = _yaml.safe_load(client.get_tasks_yaml()) or {}
        for entry in (tdoc.get("tasks") or []):
            if isinstance(entry, dict) and (entry.get("env") or {}).get("SDR_CAL_SIGNAL_ID") == old:
                spec = dict(entry)
                spec["env"] = {**(spec.get("env") or {}), "SDR_CAL_SIGNAL_ID": new}
                client.update_task(entry.get("name"), spec)
                touched = True
        return touched

    def _handle_fleet_rename(self, result) -> None:
        if isinstance(result, Exception):
            self._set_status(f"fleet rename error: {result}", kind="error")
            return
        renamed = result.get("renamed", [])
        unreachable = result.get("unreachable", [])
        if unreachable:
            QMessageBox.warning(
                self, "Some units weren’t updated",
                f"Renamed the signal on {len(renamed)} other unit(s).\n\n"
                f"These units are offline / unreachable and still use the old id — reopen "
                f"their calibration when they’re back to finish the rename:\n\n"
                f"{', '.join(unreachable)}")
            self._set_status(f"fleet rename: {len(renamed)} updated · "
                             f"{len(unreachable)} offline", kind="warn")
        else:
            self._set_status(f"fleet rename applied on {len(renamed)} other unit(s)", kind="ok")

    def _rename_tasks_signal(self, names: list, new: str) -> None:
        """Point each named task's SDR_CAL_SIGNAL_ID at `new`, preserving every other field
        (the full stored entry is sent back). Each update is a live PUT the agent reloads."""
        import yaml as _yaml
        try:
            doc = _yaml.safe_load(self._tasks_yaml) or {}
        except _yaml.YAMLError:
            self._set_status("couldn't parse this unit's tasks to update them", kind="error")
            return
        entries = {e.get("name"): e for e in (doc.get("tasks") or []) if isinstance(e, dict)}
        host = self.hostname
        self._task_rename_total = 0
        self._task_rename_errors = []
        for name in names:
            entry = entries.get(name)
            if not isinstance(entry, dict):
                continue
            spec = dict(entry)
            spec["env"] = {**(spec.get("env") or {}), "SDR_CAL_SIGNAL_ID": new}
            self._task_signals[name] = new           # optimistic; re-fetched on completion
            self._task_rename_total += 1
            self.hub.run_async(
                f"cal_taskrename:{host}:{name}",
                lambda n=name, s=spec: self.hub.fleet.get(host).update_task(n, s))
        if self._task_rename_total:
            self._set_status(f"updating {self._task_rename_total} task(s) to “{new}”…")

    def _handle_task_rename(self, name: str, result) -> None:
        if isinstance(result, Exception):
            self._task_rename_errors.append(name)
        self._task_rename_total = max(0, getattr(self, "_task_rename_total", 1) - 1)
        if self._task_rename_total > 0:
            return                                    # wait for the rest to land
        errs = getattr(self, "_task_rename_errors", [])
        if errs:
            self._set_status(f"couldn't update {len(errs)} task(s): {', '.join(errs)}",
                             kind="error")
        else:
            self._set_status("tasks updated to the new signal id", kind="ok")
        # Re-sync our view of the unit's tasks so a later rename sees the fresh state.
        self.hub.run_async(f"cal_tasks:{self.hostname}",
                           lambda: self.hub.fleet.get(self.hostname).get_tasks_yaml())

    def _on_remove_signal_from_stage(self, sid: str, plane: str) -> None:
        """Downstream-stage remove: clear this signal's measured points from ``plane`` and
        every measured stage after it (data flows downstream, so a later stage that was
        measured for it goes too). The signal stays measured upstream and keeps
        transmitting — it just inherits the upstream curve here. The user is told exactly
        which stages are affected before it happens."""
        measured = self._measured_plane_names()
        if plane not in measured:
            return
        downstream = measured[measured.index(plane):]        # this stage + all later ones
        affected = [p for p in downstream if self._signal_has_data_on(sid, p)]
        if not affected:
            # nothing measured here yet (a just-opened, empty row) — just stop showing it
            self._stage_extra.get(plane, set()).discard(sid)
            self._expanded_signals.discard(sid)
            self._refresh_form_from_widgets()
            return
        later = [p for p in affected if p != plane]
        detail = (f"\n\nThis also removes its points from the downstream measured "
                  f"stage(s): {', '.join(later)}." if later else "")
        if QMessageBox.question(
                self, "Remove signal from stage",
                f"Remove signal '{sid}'’s measured points from stage '{plane}'?{detail}"
                f"\n\nThe signal stays measured upstream and keeps transmitting — it "
                f"inherits the upstream curve on {'these stages' if later else 'this stage'}.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        sig = ((self._doc or {}).get("signals") or {}).get(sid)
        if isinstance(sig, dict):
            curves = sig.get("curves")
            if isinstance(curves, dict):
                for p in affected:
                    curves.pop(p, None)
        for p in affected:
            self._stage_extra.get(p, set()).discard(sid)
        self._doc_to_form()

    def _remove_row(self, layout, registry: list, row: dict) -> None:
        registry.remove(row)
        row["w"].setParent(None)
        row["w"].deleteLater()

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()               # so it can't briefly paint as an orphan before…
                w.setParent(None)
                w.deleteLater()        # …deleteLater runs on the event loop
            else:
                child = item.layout()
                if child is not None:
                    CalibrationPanel._clear_layout(child)

    def _populate_table(self, signals: dict, resolved: bool = True) -> None:
        """Fill the resolved Signals table (Signal | Freq | Range | Shown in). Freq comes from
        the document; the Range from the resolver, shown in the quantity picked by the per-row
        "Shown in" dropdown (the measured quantity, or the dBm limiting quantity when the signal
        declares a non-trivial limiting reading). `resolved=False` (the plain editor view, before
        a Validate/Save) shows a "validate to resolve" placeholder instead. (Amplitude is fixed
        fleet-wide, so it is not shown.)"""
        doc_sigs = (self._doc or {}).get("signals") or {}
        active = self._active_signal_ids()               # rows to tint (editor on screen)
        hi = QColor(Palette.ACCENT_SOFT)
        # Programmatic (re)fill fires itemChanged; mute it so it isn't mistaken for a rename.
        self._table.blockSignals(True)
        self._table.setRowCount(len(signals))
        for r, (sid, info) in enumerate(sorted(signals.items())):
            dsig = doc_sigs.get(sid) or {}
            f = dsig.get("center_freq_hz")
            try:
                freq = f"{float(f)/1e6:.2f}" if f else "at run"
            except (TypeError, ValueError):
                freq = "at run"
            views = self._quantity_views(info, resolved=resolved, has_freq=bool(f))
            for c, text in enumerate([sid, freq, views[0][1]]):
                item = QTableWidgetItem(str(text))
                if c == 0:                               # the Signal cell: editable → rename
                    item.setData(Qt.ItemDataRole.UserRole, sid)   # its current id, for rename
                    item.setToolTip("Click to edit this signal's measured curve; "
                                    "double-click to rename it.")
                else:                                    # read-out columns aren't editable
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if sid in active:                        # accent tint while its editor is open
                    item.setBackground(hi)
                self._table.setItem(r, c, item)
            # "Shown in": a dropdown of the quantities this signal's range can be read in. Its
            # per-item data is the pre-formatted range string, so switching it just re-labels the
            # Range cell (no recompute needed). Single-view rows (measured only, or pre-validate)
            # get a disabled combo — the picker is only meaningful with a second quantity.
            combo = QComboBox()
            combo.setStyleSheet("font-size: 11px;")
            for label, range_text in views:
                combo.addItem(label, range_text)
            combo.setEnabled(resolved and len(views) > 1)
            combo.setToolTip("Read this signal's range in its measured quantity, or in the dBm "
                             "quantity its safety limit is gauged in.")
            combo.currentIndexChanged.connect(lambda _i, row=r: self._on_view_changed(row))
            self._table.setCellWidget(r, 3, combo)
        self._table.blockSignals(False)

    def _on_view_changed(self, row: int) -> None:
        """The row's 'Shown in' dropdown changed → relabel its Range cell in the chosen
        quantity (the range strings are pre-computed and stored as the combo items' data)."""
        combo = self._table.cellWidget(row, 3)
        item = self._table.item(row, 2)
        if combo is not None and item is not None:
            item.setText(str(combo.currentData() or "—"))

    def _quantity_views(self, info: dict, *, resolved: bool, has_freq: bool) -> list:
        """The quantities a signal's range can be shown in, as ``[(label, range_text), …]`` —
        the first is the default (the measured/operating quantity). A non-trivial LIMITING
        reading adds a second view in dBm (the quantity the safety ceiling is gauged in): a
        law/same shifts the measured range by the reading's representative delta; an own reading
        reads its separate dBm curve at the gain bounds. Ranges are indicative (folded at the
        representative frequency/parameter), enough to see what to expect."""
        if not resolved:
            return [("—", "validate to resolve")]
        art = info.get("artifact") or {}
        op_unit = (art.get("operating_unit") or "").strip() or "dBm"
        op_q = (info.get("quantity") or art.get("quantity") or "").strip()
        meas_label = f"{op_q} [{op_unit}]" if op_q and op_q.lower() != "power" else op_unit
        lo, hi = info.get("min_power_dbm"), info.get("max_power_dbm")
        if lo is None or hi is None:
            return [(meas_label, "per frequency" if not has_freq else "—")]
        views = [(meas_label, _fmt_range(lo, hi, op_unit).strip() or "—")]
        lim_view = self._limiting_view((art.get("readings") or {}).get("limiting"), lo, hi, info)
        if lim_view is not None:
            views.append(lim_view)
        return views

    @staticmethod
    def _limiting_view(lim, lo: float, hi: float, info: dict):
        """A ``(label, range_text)`` for the LIMITING (dBm) quantity, or None when the limiting
        reading is trivial (same as measured). A law/same shifts the measured range by the
        reading's representative delta; an own reading reads its published dBm curve at the
        signal's gain bounds."""
        block = _reading_block(lim)                      # None ⇒ trivial (same, no k)
        if block is None:
            return None
        try:
            from state.power_law import parse_bridge
            from state.power_fold import _interp
            bridge = parse_bridge(block)
        except Exception:                                # noqa: BLE001 — never break the table
            return None
        if bridge.is_own:
            curve = (lim or {}).get("anchor_curve") or []
            gmin, gmax = info.get("min_gain_db"), info.get("max_gain_db")
            if not curve or gmin is None or gmax is None:
                return None
            gs = [pt[0] for pt in curve]
            ps = [pt[1] for pt in curve]
            llo, lhi = _interp(gmin, gs, ps), _interp(gmax, gs, ps)
        else:
            d = bridge.rep_delta_db()
            llo, lhi = lo + d, hi + d
        return ("limiting [dBm]", _fmt_range(llo, lhi, "dBm").strip() or "—")

    def _on_signal_item_changed(self, item) -> None:
        """The Signal cell was edited in place → rename the signal. The item's stored id is
        the old name; its new text is the target. Other columns are read-only, so this only
        fires for a rename."""
        if item.column() != 0:
            return
        old = item.data(Qt.ItemDataRole.UserRole)
        new = (item.text() or "").strip()
        if not old or new == old:
            return
        self._rename_signal(old, new)

    def _set_status(self, text: str, kind: str = "muted") -> None:
        color = {"ok": Palette.ONLINE, "warn": Palette.ARMED, "error": Palette.CRASH,
                 "faint": Palette.TEXT_FAINT, "muted": Palette.TEXT_MUTED}.get(kind, Palette.TEXT_MUTED)
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 12px; color: {color};")
