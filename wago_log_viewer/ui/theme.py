"""
Thème sombre de l'application (palette Qt + feuille de style).

Appelé une seule fois au démarrage, depuis ``MainWindow.__init__``.
"""

from PySide6 import QtGui, QtWidgets

# Feuille de style globale : panneaux arrondis, boutons et barres de
# défilement fines assorties au thème sombre.
_STYLESHEET = """
    QFrame[frameShape="5"] { background:#2b2f33; border:1px solid #3b3f45; border-radius:8px; }
    QPushButton { background:#3a3f44; border:1px solid #4a4f55; border-radius:6px; padding:7px 10px; }
    QPushButton:hover { background:#454b52; }
    QListWidget, QLabel, QCheckBox, QGroupBox { color:#e6e6e6; font-size:12.5px; }
    QScrollBar:vertical {
        border: none;
        background: #1e1e1e;
        width: 10px;
        margin: 0px;
    }
    QScrollBar::handle:vertical {
        background: #5c5c5c;
        min-height: 30px;
        border-radius: 7px;
        margin: 2px;
    }
    QScrollBar::handle:vertical:hover { background: #0078d7; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
    QScrollBar:horizontal {
        border: none;
        background: #1e1e1e;
        height: 16px;
        margin: 0px;
    }
    QScrollBar::handle:horizontal {
        background: #5c5c5c;
        min-width: 30px;
        border-radius: 7px;
        margin: 2px;
    }
    QScrollBar::handle:horizontal:hover { background: #0078d7; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }
"""


def apply_dark_theme(app: QtWidgets.QApplication) -> None:
    """Applique le style « Fusion » sombre à toute l'application."""
    app.setStyle("Fusion")

    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor(28, 31, 34))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor(230, 230, 230))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor(22, 25, 28))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor(230, 230, 230))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor(45, 48, 53))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(230, 230, 230))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(0, 120, 215))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(255, 255, 255))
    app.setPalette(pal)

    app.setStyleSheet(_STYLESHEET)
