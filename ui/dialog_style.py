"""Shared styling so the editor dialogs (task / sequence step / ramp step / …) match the
Run dialog's polished look: a clean white surface, rounded inset inputs with an accent
focus ring, card-like group boxes, and primary/secondary buttons.

Apply with ``self.setStyleSheet(editor_qss())`` on the dialog. It's scoped by widget type
and only affects that dialog's own widget tree; the ParamForm inside keeps its own more
specific ``#paramForm`` styling."""
from __future__ import annotations

from .theme import Palette


def scrollbar_qss() -> str:
    """A slim, subtle scrollbar held a few px off the right/bottom edge so it doesn't sit
    flush against the form (a flush bar read as claustrophobic). Shared by the editor
    dialogs and the Run dialog so every scrollable form matches."""
    return f"""
QScrollBar:vertical {{
    background: transparent; width: 12px; margin: 2px 3px 2px 0;
}}
QScrollBar::handle:vertical {{
    background: {Palette.BORDER_STRONG}; border-radius: 4px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {Palette.TEXT_FAINT}; }}
QScrollBar:horizontal {{
    background: transparent; height: 12px; margin: 0 2px 3px 2px;
}}
QScrollBar::handle:horizontal {{
    background: {Palette.BORDER_STRONG}; border-radius: 4px; min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: {Palette.TEXT_FAINT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def editor_qss() -> str:
    return f"""
QDialog {{ background: {Palette.SURFACE}; }}
QLabel {{ color: {Palette.TEXT}; }}

QLineEdit, QComboBox, QPlainTextEdit, QAbstractSpinBox {{
    background: {Palette.INSET};
    border: 1px solid {Palette.BORDER};
    border-radius: 9px;
    min-height: 30px;
    padding: 4px 10px;
    color: {Palette.TEXT};
    selection-background-color: {Palette.ACCENT_SOFT};
    selection-color: {Palette.TEXT};
}}
QPlainTextEdit {{ padding: 6px 10px; }}
QLineEdit:focus, QComboBox:focus, QComboBox:on,
QPlainTextEdit:focus, QAbstractSpinBox:focus {{
    border: 1px solid {Palette.ACCENT};
    background: {Palette.SURFACE};
}}
/* The Dropdown widget paints its own chevron/chip (matching the parameter form), so
   hide Qt's native arrow and reserve its width — scoped to Dropdown so a plain QComboBox,
   if any, keeps a visible arrow. */
Dropdown::drop-down {{
    border: none; width: 26px; subcontrol-origin: padding; subcontrol-position: center right;
}}
Dropdown::down-arrow {{ image: none; width: 0; height: 0; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    width: 0; height: 0; border: none; margin: 0;
}}
/* DurationSpinBox paints its own up/down chevron chip on the right (matching the
   Dropdown widget), so reserve room for it and keep the value text clear of it. */
DurationSpinBox {{ padding-right: 30px; }}
QComboBox QAbstractItemView {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 8px; padding: 4px; outline: 0;
    selection-background-color: {Palette.ACCENT_SOFT};
    selection-color: {Palette.ACCENT_INK};
}}

QGroupBox {{
    border: 1px solid {Palette.BORDER};
    border-radius: 12px;
    margin-top: 12px;
    padding: 8px 10px 10px;
    font-weight: 600;
    color: {Palette.TEXT};
    background: transparent;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px; padding: 0 5px;
    color: {Palette.TEXT_MUTED};
}}

/* Both the scroll area AND its viewport child must be transparent, or the body shows
   the grey default viewport instead of the dialog's white SURFACE. */
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
{scrollbar_qss()}

QDialogButtonBox QPushButton {{
    background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 10px; padding: 8px 16px; font-weight: 600;
    color: {Palette.TEXT}; min-width: 76px;
}}
QDialogButtonBox QPushButton:hover {{ background: {Palette.SURFACE_ALT}; }}
QDialogButtonBox QPushButton:default, QDialogButtonBox QPushButton:focus {{
    background: {Palette.ACCENT}; border: 1px solid {Palette.ACCENT}; color: #FFFFFF;
}}
QDialogButtonBox QPushButton:default:hover, QDialogButtonBox QPushButton:focus:hover {{
    background: {Palette.ACCENT_INK}; border-color: {Palette.ACCENT_INK};
}}
"""
