"""Тёмная нео-бруталистичная тема DropWave.

Признаки стиля: жирные контрастные границы (3px), жёсткие тени без размытия,
кислотный акцент, крупная рубленая типографика, прямые углы.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget

# Палитра
BG = "#0b0b0e"
PANEL = "#15151b"
PANEL_2 = "#1d1d25"
INK = "#f4f4f0"
MUTED = "#9a9aa4"
ACCENT = "#d4ff3f"      # кислотный лайм
ACCENT_INK = "#0b0b0e"
VIOLET = "#8f7bff"
DANGER = "#ff5470"
OK = "#3fdf8f"
BORDER = "#f4f4f0"

STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {INK};
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {BG}; }}

/* Карточки */
QFrame[card="true"] {{
    background: {PANEL};
    border: 3px solid {BORDER};
}}
QFrame[card="true"] QLabel {{ background: transparent; border: none; }}
QFrame[card="true"] QListWidget {{ background: {BG}; border: 2px solid #33333d; }}

/* Типографика */
QLabel[role="title"] {{
    font-size: 30px; font-weight: 900; letter-spacing: 2px;
    color: {INK}; background: transparent;
}}
QLabel[role="subtitle"] {{
    color: {MUTED}; font-size: 12px; font-weight: 600;
    letter-spacing: 1px; background: transparent;
}}
QLabel[role="cardTitle"] {{
    font-size: 13px; font-weight: 900; color: {ACCENT};
    letter-spacing: 1.5px; background: transparent;
}}
QLabel[role="muted"] {{ color: {MUTED}; background: transparent; }}
QLabel[role="big"] {{ font-size: 20px; font-weight: 900; background: transparent; }}
QLabel[role="url"] {{
    font-family: "Consolas", "Courier New", monospace;
    font-size: 15px; font-weight: 700; color: {ACCENT};
    background: {BG}; border: 2px dashed #4a4a55; padding: 8px 10px;
}}
QLabel[role="chip"] {{
    font-weight: 900; font-size: 11px; padding: 5px 12px;
    border: 2px solid {BORDER}; background: {PANEL_2}; color: {MUTED};
}}
QLabel[chipState="online"] {{ background: {ACCENT}; color: {ACCENT_INK}; border-color: {ACCENT}; }}
QLabel[chipState="error"] {{ background: {DANGER}; color: {INK}; border-color: {DANGER}; }}

/* Кнопки */
QPushButton {{
    background: {ACCENT}; color: {ACCENT_INK};
    border: 3px solid {ACCENT};
    padding: 9px 16px; font-weight: 900; font-size: 12px;
    letter-spacing: 0.8px;
}}
QPushButton:hover {{ background: #e4ff7a; border-color: #e4ff7a; }}
QPushButton:pressed {{ background: #b8dd2e; border-color: #b8dd2e; }}
QPushButton:disabled {{ background: {PANEL_2}; color: {MUTED}; border-color: #33333d; }}
QPushButton[variant="ghost"] {{
    background: {PANEL_2}; color: {INK}; border: 3px solid {BORDER};
}}
QPushButton[variant="ghost"]:hover {{ background: #2a2a35; }}
QPushButton[variant="danger"] {{
    background: {DANGER}; color: {INK}; border: 3px solid {DANGER};
}}

/* Списки */
QListWidget {{
    background: {BG}; border: 2px solid #33333d;
    padding: 4px; outline: none; font-size: 12.5px;
}}
QListWidget::item {{
    padding: 7px 8px; border-bottom: 1px solid #23232b; color: {INK};
}}
QListWidget::item:selected {{
    background: {ACCENT}; color: {ACCENT_INK}; font-weight: 700;
}}
QListWidget::item:hover {{ background: {PANEL_2}; }}
QListWidget::item:selected:hover {{ background: {ACCENT}; }}

/* Прогресс */
QProgressBar {{
    background: {BG}; border: 3px solid {BORDER};
    height: 22px; text-align: center; font-weight: 900; color: {INK};
}}
QProgressBar::chunk {{ background: {ACCENT}; }}

/* Чекбокс-переключатель */
QCheckBox {{ spacing: 10px; font-weight: 700; background: transparent; }}
QCheckBox::indicator {{
    width: 42px; height: 22px; border: 3px solid {BORDER}; background: {BG};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:unchecked {{ background: {PANEL_2}; }}

/* Прокрутка */
QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #3a3a45; min-height: 24px; border: 2px solid {BG}; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}

/* Меню трея */
QMenu {{ background: {PANEL}; border: 3px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 8px 24px; font-weight: 700; }}
QMenu::item:selected {{ background: {ACCENT}; color: {ACCENT_INK}; }}

QToolTip {{
    background: {ACCENT}; color: {ACCENT_INK}; border: 2px solid {ACCENT};
    padding: 6px; font-weight: 700;
}}
QStatusBar {{ background: {PANEL}; color: {MUTED}; border-top: 2px solid #33333d; }}
QStatusBar::item {{ border: none; }}
"""


def hard_shadow(widget: QWidget, offset: int = 6, color: str = "#000000") -> None:
    """Жёсткая смещённая тень без размытия — визитная карточка нео-брутализма."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(0)
    effect.setOffset(offset, offset)
    effect.setColor(QColor(color))
    widget.setGraphicsEffect(effect)


def make_qr_pixmap(url: str, size: int = 232) -> QPixmap:
    """Чёткий QR-код: модули масштабируются без интерполяции (NearestNeighbor)."""
    import qrcode

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0b0b0e", back_color="#ffffff").convert("RGBA")
    img = img.resize((size, size), resample=0)  # NEAREST — модули остаются чёткими
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.width, img.height, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


def make_app_icon() -> QIcon:
    """Иконка приложения/трея: кислотный квадрат с чёрной волной."""
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(s, s)
        pm.fill(QColor(ACCENT))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(ACCENT_INK))
        p.drawRect(0, 0, s, max(1, s // 16))
        p.drawRect(0, s - max(1, s // 16), s, max(1, s // 16))
        from PySide6.QtGui import QPainterPath, QPen
        pen = QPen(QColor(ACCENT_INK), max(1.6, s * 0.09))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        path.moveTo(s * 0.12, s * 0.58)
        path.cubicTo(s * 0.30, s * 0.18, s * 0.42, s * 0.90, s * 0.58, s * 0.50)
        path.cubicTo(s * 0.70, s * 0.22, s * 0.80, s * 0.62, s * 0.90, s * 0.44)
        p.drawPath(path)
        p.end()
        icon.addPixmap(pm)
    return icon


def mono_font(point_size: int = 12, bold: bool = True) -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.Monospace)
    font.setPointSize(point_size)
    font.setBold(bold)
    return font