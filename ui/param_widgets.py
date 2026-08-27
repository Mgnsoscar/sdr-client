"""
Styled building blocks for the parameter form — the visual layer that makes the
Run-task panel match the design mockup (ui/param_form.py composes these around the
value-carrying widgets it already used, so all form logic is unchanged).

  SegmentedControl  the Absolute / Relative power-mode chooser, with a sliding thumb
  ToggleSwitch      an On/Off toggle that IS a QCheckBox (drop-in for a flag field)
  RangeRail         a track + fill + thumb showing a value inside its [min,max]
  LimitChip         the always-visible "min … → max …" range beside a bounded input
  UnitChip          the small unit tag next to a field name
  LiveBadge         the pulsing "LIVE" marker for a run-tunable parameter

All colours come from ui.theme.Palette so light-surface styling stays in one place.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PyQt6.QtCore import (
    QEasingCurve, QPropertyAnimation, QRectF, Qt, pyqtProperty, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen,
)
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from .theme import Palette, mono_font


def _c(hex_str: str) -> QColor:
    return QColor(hex_str)


# ── Segmented control (power mode) ──────────────────────────────────────────────

class SegmentedControl(QWidget):
    """A row of mutually exclusive segments with a sliding thumb behind the active
    one. Each segment has a bold main label and a lighter sub-label (e.g. Absolute /
    "dBm · this unit"). Emits ``changed(index)`` when the selection changes by click.

    API mirrors the essentials of a QComboBox so the form can swap it in:
        currentIndex(), setCurrentIndex(i[, animate]), changed(int)
    """
    changed = pyqtSignal(int)

    def __init__(self, items: List[Tuple[str, str]], parent=None):
        super().__init__(parent)
        self._items = list(items)                    # [(main, sub), ...]
        self._index = 0
        self._thumb_pos = 0.0                        # animated float in [0, n-1]
        self.setMinimumHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"thumbPos", self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # animated property ------------------------------------------------------------
    def _get_thumb(self) -> float:
        return self._thumb_pos

    def _set_thumb(self, v: float) -> None:
        self._thumb_pos = v
        self.update()

    thumbPos = pyqtProperty(float, fget=_get_thumb, fset=_set_thumb)

    # api --------------------------------------------------------------------------
    def currentIndex(self) -> int:
        return self._index

    def setCurrentIndex(self, i: int, animate: bool = True) -> None:
        if not (0 <= i < len(self._items)) or i == self._index:
            return
        self._index = i
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._thumb_pos)
            self._anim.setEndValue(float(i))
            self._anim.start()
        else:
            self._set_thumb(float(i))
        self.changed.emit(i)

    # geometry ---------------------------------------------------------------------
    def _seg_rect(self, i: float) -> QRectF:
        pad = 3.0
        n = max(1, len(self._items))
        w = (self.width() - 2 * pad) / n
        return QRectF(pad + i * w, pad, w, self.height() - 2 * pad)

    def mousePressEvent(self, ev) -> None:
        if not self._items:
            return
        pad = 3.0
        n = len(self._items)
        w = (self.width() - 2 * pad) / n
        idx = int((ev.position().x() - pad) // w) if w > 0 else 0
        self.setCurrentIndex(max(0, min(n - 1, idx)))

    # paint ------------------------------------------------------------------------
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        # track
        track = QPainterPath()
        track.addRoundedRect(r, 11, 11)
        p.fillPath(track, _c(Palette.INSET))
        p.setPen(QPen(_c(Palette.BORDER), 1))
        p.drawPath(track)

        # thumb (behind the active segment)
        tr = self._seg_rect(self._thumb_pos)
        thumb = QPainterPath()
        thumb.addRoundedRect(tr, 8, 8)
        p.fillPath(thumb, _c(Palette.SURFACE))
        p.setPen(QPen(_c(Palette.BORDER_STRONG), 1))
        p.drawPath(thumb)

        # labels
        for i, (main, sub) in enumerate(self._items):
            rect = self._seg_rect(i)
            active = (i == self._index)
            main_col = _c(Palette.TEXT) if active else _c(Palette.TEXT_MUTED)
            sub_col = _c(Palette.ACCENT_INK) if active else _c(Palette.TEXT_FAINT)
            fm = QFont("IBM Plex Sans")
            fm.setPixelSize(13)
            fm.setWeight(QFont.Weight.DemiBold)
            p.setFont(fm)
            p.setPen(main_col)
            if sub:
                top = rect.adjusted(0, 6, 0, -rect.height() / 2 + 4)
                p.drawText(top, Qt.AlignmentFlag.AlignCenter, main)
                fs = QFont("IBM Plex Sans")
                fs.setPixelSize(10)
                p.setFont(fs)
                p.setPen(sub_col)
                bot = rect.adjusted(0, rect.height() / 2 - 3, 0, -5)
                p.drawText(bot, Qt.AlignmentFlag.AlignCenter, sub)
            else:
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, main)
        p.end()


# ── On/Off toggle (a QCheckBox in disguise, so form logic is unchanged) ──────────

class ToggleSwitch(QCheckBox):
    """A two-state On/Off pill. Subclasses QCheckBox so ParamForm's flag handling
    (isinstance(w, QCheckBox), isChecked/setChecked, stateChanged) works untouched —
    only the appearance and hit target change."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedSize(112, 34)

    def sizeHint(self):
        from PyQt6.QtCore import QSize
        return QSize(112, 34)

    def hitButton(self, pos) -> bool:                # whole widget toggles
        return self.rect().contains(pos)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        base = QPainterPath()
        base.addRoundedRect(r, 9, 9)
        p.fillPath(base, _c(Palette.INSET))
        p.setPen(QPen(_c(Palette.BORDER), 1))
        p.drawPath(base)

        on = self.isChecked()
        pad = 3.0
        half = (r.width() - 2 * pad) / 2
        sel = QRectF(r.left() + pad + (0 if on else half), r.top() + pad,
                     half, r.height() - 2 * pad)
        knob = QPainterPath()
        knob.addRoundedRect(sel, 6, 6)
        p.fillPath(knob, _c(Palette.SURFACE))
        p.setPen(QPen(_c(Palette.BORDER_STRONG), 1))
        p.drawPath(knob)

        f = QFont("IBM Plex Sans")
        f.setPixelSize(12)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        left = QRectF(r.left() + pad, r.top(), half, r.height())
        right = QRectF(r.left() + pad + half, r.top(), half, r.height())
        p.setPen(_c(Palette.ONLINE) if on else _c(Palette.TEXT_FAINT))
        p.drawText(left, Qt.AlignmentFlag.AlignCenter, "On")
        p.setPen(_c(Palette.TEXT) if not on else _c(Palette.TEXT_FAINT))
        p.drawText(right, Qt.AlignmentFlag.AlignCenter, "Off")
        p.end()


# ── Range rail (track + fill + thumb) ───────────────────────────────────────────

class RailTrack(QWidget):
    """The painted part of a range rail: a slim track with an accent fill and a thumb
    at ``fraction`` (0..1). Turns amber in the ``over`` (clamp) state. Purely a
    read-out — it doesn't accept input (the field's input widget owns the value)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fraction = 0.0
        self._over = False
        self.setFixedHeight(16)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def setFraction(self, f: float) -> None:
        f = max(0.0, min(1.0, f))
        if f != self._fraction:
            self._fraction = f
            self.update()

    def setOver(self, over: bool) -> None:
        if over != self._over:
            self._over = over
            self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = 5.0
        y = (self.height() - h) / 2
        w = float(self.width())
        track = QRectF(0.5, y, w - 1, h)
        tp = QPainterPath()
        tp.addRoundedRect(track, h / 2, h / 2)
        p.fillPath(tp, _c(Palette.INSET))
        p.setPen(QPen(_c(Palette.BORDER), 1))
        p.drawPath(tp)

        accent = _c(Palette.ARMED if self._over else Palette.ACCENT)
        fill_w = max(0.0, min(w, w * self._fraction))
        if fill_w > 1:
            fp = QPainterPath()
            fp.addRoundedRect(QRectF(0.5, y, fill_w, h), h / 2, h / 2)
            if self._over:
                p.fillPath(fp, accent)
            else:
                grad = QLinearGradient(0, 0, w, 0)
                soft = _c(Palette.ACCENT)
                soft.setAlpha(150)
                grad.setColorAt(0.0, soft)
                grad.setColorAt(1.0, _c(Palette.ACCENT))
                p.fillPath(fp, grad)

        cx = max(6.0, min(w - 6.0, w * self._fraction))
        cy = self.height() / 2
        p.setPen(QPen(accent, 2.5))
        p.setBrush(_c(Palette.SURFACE))
        p.drawEllipse(QRectF(cx - 6, cy - 6, 12, 12))
        p.end()


class RangeRail(QWidget):
    """A [lo] ── track ── [hi] row that reflects a value inside its bounds, with an
    optional note beneath it (e.g. the frequency a freq-dependent range was folded at —
    kept inside the rail so it travels with it). Call ``set_bounds`` once, ``set_value``
    as the field changes, and ``set_note`` for the caption."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lo = 0.0
        self._hi = 1.0
        self.track = RailTrack()
        self._lo_lbl = self._end_label()
        self._hi_lbl = self._end_label()
        self._note = None
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 3, 0, 0)
        self._outer.setSpacing(3)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self._lo_lbl)
        row.addWidget(self.track, 1)
        row.addWidget(self._hi_lbl)
        self._outer.addLayout(row)

    def _end_label(self) -> QLabel:
        lbl = QLabel()
        lbl.setFont(mono_font(10))
        lbl.setStyleSheet(f"color: {Palette.TEXT_FAINT};")
        return lbl

    def set_bounds(self, lo: float, hi: float, fmt=None) -> None:
        self._lo, self._hi = float(lo), float(hi)
        f = fmt or (lambda v: f"{v:g}")
        self._lo_lbl.setText(f(lo))
        self._hi_lbl.setText(f(hi))

    def set_value(self, v: Optional[float]) -> None:
        if v is None or self._hi <= self._lo:
            self.track.setFraction(0.0)
            self.track.setOver(False)
            return
        frac = (min(max(v, self._lo), self._hi) - self._lo) / (self._hi - self._lo)
        self.track.setFraction(frac)
        self.track.setOver(v > self._hi + 1e-9)

    def set_note(self, text: str) -> None:
        if not text:
            if self._note is not None:
                self._note.setVisible(False)
            return
        if self._note is None:
            self._note = QLabel()
            self._note.setWordWrap(True)
            self._note.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            self._outer.addWidget(self._note)
        self._note.setText(text)
        self._note.setVisible(True)


# ── Limit chip (always-visible min → max beside a bounded input) ─────────────────

class LimitChip(QFrame):
    """The persistent min/max range shown next to a bounded field's input — the
    calibrated limits that stay in view instead of hiding once a value is typed.
    Turns amber in the clamp (``over``) state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("limitChip")
        self.setFixedHeight(42)
        self._over = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(11, 0, 11, 0)
        lay.setSpacing(6)
        self._cap_lo = self._cap("MIN")
        self._lo = self._val()
        self._arrow = QLabel("→")
        self._arrow.setStyleSheet(f"color: {Palette.TEXT_FAINT};")
        self._cap_hi = self._cap("MAX")
        self._hi = self._val()
        for w in (self._cap_lo, self._lo, self._arrow, self._cap_hi, self._hi):
            lay.addWidget(w)
        self._apply_style()

    def _cap(self, text: str) -> QLabel:
        lbl = QLabel(text)
        f = QFont("IBM Plex Sans")
        f.setPixelSize(9)
        f.setWeight(QFont.Weight.DemiBold)
        lbl.setFont(f)
        lbl.setStyleSheet(f"color: {Palette.TEXT_FAINT}; letter-spacing: 0.5px;")
        return lbl

    def _val(self) -> QLabel:
        lbl = QLabel()
        lbl.setFont(mono_font(12, 500))
        return lbl

    def set_range(self, lo_text: str, hi_text: str) -> None:
        self._lo.setText(lo_text)
        self._hi.setText(hi_text)

    def set_over(self, over: bool) -> None:
        if over != self._over:
            self._over = over
            self._apply_style()

    def _apply_style(self) -> None:
        if self._over:
            self.setStyleSheet(
                f"#limitChip {{ background: {Palette.ARMED_SOFT}; "
                f"border: 1px solid {Palette.ARMED}; border-radius: 9px; }}")
            self._hi.setStyleSheet(f"color: {Palette.ARMED}; font-weight: 600;")
            self._lo.setStyleSheet(f"color: {Palette.ARMED};")
        else:
            self.setStyleSheet(
                f"#limitChip {{ background: {Palette.SURFACE}; "
                f"border: 1px solid {Palette.BORDER}; border-radius: 9px; }}")
            self._hi.setStyleSheet(f"color: {Palette.TEXT}; font-weight: 500;")
            self._lo.setStyleSheet(f"color: {Palette.TEXT}; font-weight: 500;")


# ── Small label helpers ─────────────────────────────────────────────────────────

def unit_chip(text: str) -> QLabel:
    """The unit tag beside a field name (e.g. Hz, dBm · EIRP, MHz)."""
    lbl = QLabel(text)
    f = QFont("IBM Plex Sans")
    f.setPixelSize(10)
    f.setWeight(QFont.Weight.DemiBold)
    lbl.setFont(f)
    lbl.setStyleSheet(
        f"color: {Palette.TEXT_FAINT}; background: {Palette.INSET}; "
        f"border: 1px solid {Palette.BORDER}; border-radius: 5px; padding: 1px 6px;")
    return lbl


def field_name_label(text: str) -> QLabel:
    """The uppercase, tracked field name."""
    lbl = QLabel(text.upper())
    f = QFont("IBM Plex Sans")
    f.setPixelSize(11)
    f.setWeight(QFont.Weight.DemiBold)
    lbl.setFont(f)
    lbl.setStyleSheet(f"color: {Palette.TEXT_MUTED}; letter-spacing: 0.7px;")
    return lbl


class LiveBadge(QWidget):
    """A pulsing dot + LIVE, marking a parameter the script can retune while running."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._op = 1.0
        self.setFixedSize(46, 14)
        self._anim = QPropertyAnimation(self, b"dotOpacity", self)
        self._anim.setDuration(2600)
        self._anim.setStartValue(1.0)
        self._anim.setKeyValueAt(0.5, 0.35)
        self._anim.setEndValue(1.0)
        self._anim.setLoopCount(-1)
        self._anim.start()

    def _get_op(self) -> float:
        return self._op

    def _set_op(self, v: float) -> None:
        self._op = v
        self.update()

    dotOpacity = pyqtProperty(float, fget=_get_op, fset=_set_op)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cy = self.height() / 2
        dot = _c(Palette.ACCENT)
        halo = _c(Palette.ACCENT)
        halo.setAlpha(46)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QRectF(0, cy - 5, 10, 10))
        dot.setAlphaF(self._op)
        p.setBrush(dot)
        p.drawEllipse(QRectF(2, cy - 2.5, 5, 5))
        f = QFont("IBM Plex Sans")
        f.setPixelSize(9)
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        p.setPen(_c(Palette.ACCENT))
        p.drawText(QRectF(12, 0, self.width() - 12, self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "LIVE")
        p.end()
