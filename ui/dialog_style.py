"""Shared styling so the editor dialogs (task / sequence step / ramp step / …) match the
Run dialog's polished look: a clean white surface, rounded inset inputs with an accent
focus ring, card-like group boxes, and primary/secondary buttons.

Apply with ``self.setStyleSheet(editor_qss())`` on the dialog. It's scoped by widget type
and only affects that dialog's own widget tree; the ParamForm inside keeps its own more
specific ``#paramForm`` styling."""
from __future__ import annotations

from .theme import Palette


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
QComboBox::drop-down {{ border: none; width: 22px; }}
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

QScrollArea {{ border: none; background: transparent; }}

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
