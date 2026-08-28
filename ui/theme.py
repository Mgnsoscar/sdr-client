"""
Central theme for the SDR broadcaster control GUI.

Calm light surface, soft elevation, restrained accent. Status colors
(green/amber/red/grey) are reserved for actual state — connection, on-air, armed,
crashed, clock sync — so color always means something. Everything else stays
neutral so the operator's eye is drawn only to what needs attention.

Usage:
    from ui.theme import apply_theme, Palette, status_color
    apply_theme(app)                       # app-wide stylesheet + palette
    pill = StatusPill("ON AIR", Palette.ONAIR)
"""
from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PyQt6.QtWidgets import QApplication

logger = logging.getLogger("sdr-client.theme")

_FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
# Filled in by _load_fonts(): the real family names Qt registered, or the
# fallback stacks when the bundled IBM Plex faces aren't available.
_FONTS_LOADED = False


class Palette:
    # ── Surfaces (light, softly shaded) ──────────────────────────────────────
    BG          = "#F2F3F5"   # app background — soft cool grey, not stark white
    SURFACE     = "#FFFFFF"   # cards / panels sit above the background
    SURFACE_ALT = "#FAFBFC"   # subtle alternate (table stripes, insets)
    INSET       = "#F1F4F7"   # recessed field background (inputs, chips)
    BORDER      = "#E2E5EA"   # hairline separators / card borders
    BORDER_STRONG = "#CDD2DA" # slightly stronger divider when needed

    # ── Text ──────────────────────────────────────────────────────────────────
    TEXT        = "#1E2530"   # primary text — near-black, soft
    TEXT_MUTED  = "#5C6675"   # secondary / labels
    TEXT_FAINT  = "#8A93A2"   # captions, timestamps, hints

    # ── Accent (used sparingly — selected tab, focus, primary action) ─────────
    ACCENT      = "#2C6E9B"   # muted slate-blue; calm, not loud
    ACCENT_SOFT = "#E8F0F6"   # accent tint for selected/hover backgrounds
    ACCENT_INK  = "#1F5476"   # darker accent for text on an accent-soft ground

    # ── Status (RESERVED for real state — never decorative) ───────────────────
    ONLINE      = "#1D9E75"   # connected / on-air / healthy (green)
    ONLINE_SOFT = "#E1F5EE"
    ARMED       = "#BA7517"   # armed / pending / warning (amber)
    ARMED_SOFT  = "#FAEEDA"
    CRASH       = "#C23B3B"   # crashed / offline / error (red)
    CRASH_SOFT  = "#FBE9E9"
    IDLE        = "#8A93A2"   # stopped / queued / unknown (grey)
    IDLE_SOFT   = "#EDEFF2"

    # ── Panic (deliberately the loudest thing in the app) ─────────────────────
    PANIC       = "#C8102E"
    PANIC_HOVER = "#A50D26"
    PANIC_TEXT  = "#FFFFFF"


class Fonts:
    """Font-family stacks used across the UI. The bundled IBM Plex faces
    (ui/assets/fonts) are registered by ``_load_fonts`` at theme-apply time; the
    trailing families are the graceful fallback when they aren't available, so the
    layout is identical even where IBM Plex can't load."""
    SANS = '"IBM Plex Sans", "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif'
    MONO = '"IBM Plex Mono", "DejaVu Sans Mono", "SF Mono", "Consolas", ui-monospace, monospace'


def _load_fonts() -> None:
    """Register the bundled IBM Plex faces with Qt so the app renders in the same
    typography as the design mockups regardless of what's installed on the host. The
    faces ship as TTF: Qt's font loader handles TTF/OTF but NOT woff2 (a web-only
    format Qt rejects with 'Failed to create DirectWrite face'). Best-effort: a
    missing/failed file just leaves the fallback stack in Fonts."""
    global _FONTS_LOADED
    if _FONTS_LOADED:
        return
    _FONTS_LOADED = True
    if not _FONT_DIR.is_dir():
        logger.info("Bundled fonts not found at %s — using fallback families", _FONT_DIR)
        return
    loaded = 0
    for path in sorted(p for ext in ("*.ttf", "*.otf") for p in _FONT_DIR.glob(ext)):
        if QFontDatabase.addApplicationFont(str(path)) == -1:
            logger.warning("Could not load bundled font %s", path.name)
        else:
            loaded += 1
    logger.info("Loaded %d bundled IBM Plex font file(s)", loaded)


def mono_font(size: int = 13, weight: int = 400) -> QFont:
    """A QFont in the app's monospace family at a given px size/weight — for tabular
    numerics (field values, limit chips, command previews)."""
    f = QFont("IBM Plex Mono")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPixelSize(size)
    f.setWeight(QFont.Weight(weight))
    return f


# Semantic status name → (fg, bg) for pills/badges
_STATUS_MAP = {
    # connection
    "online":    (Palette.ONLINE, Palette.ONLINE_SOFT),
    "offline":   (Palette.CRASH,  Palette.CRASH_SOFT),
    "error":     (Palette.CRASH,  Palette.CRASH_SOFT),
    "unknown":   (Palette.IDLE,   Palette.IDLE_SOFT),
    # process / run / event states
    "running":   (Palette.ONLINE, Palette.ONLINE_SOFT),
    "on air":    (Palette.ONLINE, Palette.ONLINE_SOFT),
    "on_air":    (Palette.ONLINE, Palette.ONLINE_SOFT),
    "armed":     (Palette.ARMED,  Palette.ARMED_SOFT),
    "starting":  (Palette.ARMED,  Palette.ARMED_SOFT),
    "stopping":  (Palette.ARMED,  Palette.ARMED_SOFT),
    "queued":    (Palette.IDLE,   Palette.IDLE_SOFT),
    "stopped":   (Palette.IDLE,   Palette.IDLE_SOFT),
    "completed": (Palette.IDLE,   Palette.IDLE_SOFT),
    "cancelled": (Palette.IDLE,   Palette.IDLE_SOFT),
    "crashed":   (Palette.CRASH,  Palette.CRASH_SOFT),
    "aborted":   (Palette.CRASH,  Palette.CRASH_SOFT),
}


def status_color(name: str) -> tuple[str, str]:
    """Return (foreground, background) hex for a status name. Falls back to idle."""
    return _STATUS_MAP.get(name.lower().strip(), (Palette.IDLE, Palette.IDLE_SOFT))


# ── Application stylesheet ──────────────────────────────────────────────────────

QSS = f"""
* {{
    font-family: {Fonts.SANS};
    font-size: 13px;
    color: {Palette.TEXT};
}}

QMainWindow, QWidget#root {{
    background: {Palette.BG};
}}

/* ── Top bar ──────────────────────────────────────────────────────────────── */
QWidget#topbar {{
    background: {Palette.SURFACE};
    border-bottom: 1px solid {Palette.BORDER};
}}

/* Tab buttons in the top bar (we use QPushButton with checkable state) */
QPushButton#tab {{
    background: transparent;
    border: none;
    padding: 9px 16px;
    color: {Palette.TEXT_MUTED};
    font-weight: 500;
    border-radius: 6px;
}}
QPushButton#tab:hover {{
    background: {Palette.SURFACE_ALT};
    color: {Palette.TEXT};
}}
QPushButton#tab:checked {{
    background: {Palette.ACCENT_SOFT};
    color: {Palette.ACCENT};
}}

/* ── Panic button ─────────────────────────────────────────────────────────── */
QPushButton#panic {{
    background: {Palette.PANIC};
    color: {Palette.PANIC_TEXT};
    border: none;
    border-radius: 6px;
    padding: 8px 18px;
    font-weight: 700;
    letter-spacing: 0.4px;
}}
QPushButton#panic:hover {{ background: {Palette.PANIC_HOVER}; }}
QPushButton#panic:pressed {{ background: {Palette.PANIC_HOVER}; }}

/* ── Generic cards/panels ─────────────────────────────────────────────────── */
QFrame#card {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER};
    border-radius: 10px;
}}

/* ── Buttons (default) ────────────────────────────────────────────────────── */
QPushButton {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 6px;
    padding: 6px 12px;
    color: {Palette.TEXT};
}}
QPushButton:hover {{ background: {Palette.SURFACE_ALT}; }}
QPushButton:pressed {{ background: {Palette.ACCENT_SOFT}; }}
QPushButton:disabled {{ color: {Palette.TEXT_FAINT}; background: {Palette.SURFACE_ALT}; }}

/* Primary action button variant */
QPushButton#primary {{
    background: {Palette.ACCENT};
    color: #FFFFFF;
    border: none;
}}
QPushButton#primary:hover {{ background: #25597E; }}

/* ── Clock-sync indicator label ───────────────────────────────────────────── */
QLabel#clockOk    {{ color: {Palette.ONLINE};  font-weight: 600; }}
QLabel#clockWarn  {{ color: {Palette.CRASH};   font-weight: 600; }}
QLabel#clockUnknown {{ color: {Palette.TEXT_FAINT}; }}

/* ── Alert feed strip ─────────────────────────────────────────────────────── */
QWidget#alertstrip {{
    background: {Palette.SURFACE};
    border-top: 1px solid {Palette.BORDER};
}}
QLabel#alertHeader {{
    color: {Palette.TEXT_FAINT};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.6px;
}}
QListWidget#alertList {{
    background: transparent;
    border: none;
}}
QListWidget#alertList::item {{
    padding: 3px 2px;
    border-bottom: 1px solid {Palette.SURFACE_ALT};
}}

/* ── Tables / lists ───────────────────────────────────────────────────────── */
QTableWidget, QTableView {{
    background: {Palette.SURFACE};
    gridline-color: {Palette.BORDER};
    border: 1px solid {Palette.BORDER};
    border-radius: 8px;
}}
QHeaderView::section {{
    background: {Palette.SURFACE_ALT};
    color: {Palette.TEXT_MUTED};
    border: none;
    border-bottom: 1px solid {Palette.BORDER};
    padding: 6px 8px;
    font-weight: 600;
}}

/* ── Scrollbars (slim, unobtrusive) ───────────────────────────────────────── */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {Palette.BORDER_STRONG}; border-radius: 5px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {Palette.TEXT_FAINT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

/* ── Tooltips ─────────────────────────────────────────────────────────────── */
QToolTip {{
    background: {Palette.TEXT};
    color: #FFFFFF;
    border: none;
    padding: 5px 8px;
    border-radius: 4px;
}}
"""


def apply_theme(app: QApplication) -> None:
    """Apply the palette + global stylesheet to the whole application."""
    app.setStyle("Fusion")   # consistent base across platforms
    _load_fonts()            # register bundled IBM Plex faces before anything renders

    # Make IBM Plex Sans the application default so even unstyled widgets match.
    base = QFont("IBM Plex Sans")
    base.setPixelSize(13)
    app.setFont(base)

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(Palette.BG))
    pal.setColor(QPalette.ColorRole.Base, QColor(Palette.SURFACE))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(Palette.SURFACE_ALT))
    pal.setColor(QPalette.ColorRole.Text, QColor(Palette.TEXT))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(Palette.TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(Palette.SURFACE))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(Palette.TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(Palette.ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(Palette.TEXT))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#FFFFFF"))
    app.setPalette(pal)

    app.setStyleSheet(QSS)