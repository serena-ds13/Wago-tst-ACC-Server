"""
Bandeau d'information des curseurs (en haut de chaque onglet).

Affiche les valeurs de tous les canaux sélectionnés à la position du curseur
C1, et — si le mode double curseur est actif — celles de C2 ainsi que les
écarts Δ (temps et valeurs analogiques).
"""

from PySide6 import QtCore, QtWidgets

_SCROLLBAR_STYLE = """
    QScrollBar:horizontal { border: none; background: #1e1e1e; height: 10px; margin: 0px; }
    QScrollBar::handle:horizontal { background: #5c5c5c; min-width: 30px; border-radius: 4px; margin: 2px; }
    QScrollBar::handle:horizontal:hover { background: #0078d7; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }
"""

_LABEL_STYLE = (
    "padding:4px; background:#2b2f33; border:1px solid #3b3f45; "
    "border-radius:4px; font-family: 'Consolas', monospace;"
)


class CursorInfoPane(QtWidgets.QWidget):
    """Trois lignes (C1, C2, Δ) dans une zone défilante horizontale compacte."""

    def __init__(self):
        super().__init__()

        self.main_layout = QtWidgets.QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)

        # Zone défilante horizontale : les valeurs peuvent dépasser la largeur
        # de la fenêtre quand beaucoup de canaux sont sélectionnés.
        self.scroll_area = QtWidgets.QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet(_SCROLLBAR_STYLE)

        self.container = QtWidgets.QWidget()
        self.container_layout = QtWidgets.QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(0, 0, 0, 0)
        self.container_layout.setSpacing(4)

        self.c1_label = QtWidgets.QLabel("C1 : —")
        self.c2_label = QtWidgets.QLabel("C2 : —")
        self.delta_label = QtWidgets.QLabel("Δ (C2 - C1) : —")

        for lbl in (self.c1_label, self.c2_label, self.delta_label):
            lbl.setAlignment(QtCore.Qt.AlignLeft)
            lbl.setStyleSheet(_LABEL_STYLE)
            self.container_layout.addWidget(lbl)

        # La ligne Δ est mise en évidence (vert clair, gras).
        self.delta_label.setStyleSheet(_LABEL_STYLE + "color: #c6fcc6; font-weight: bold;")

        # C2 et Δ ne sont visibles qu'en mode double curseur.
        self.c2_label.setVisible(False)
        self.delta_label.setVisible(False)

        self.scroll_area.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                                       QtWidgets.QSizePolicy.Maximum)
        self.scroll_area.setWidget(self.container)
        self.main_layout.addWidget(self.scroll_area)

        # Premier appel pour fixer la hauteur initiale (mode simple curseur).
        self.activate_dual_mode(False)

    def update_info(self, c1_text: str, c2_text: str, delta_text: str, dual_mode: bool):
        """Met à jour les trois lignes ; bascule le mode si nécessaire."""
        self.c1_label.setText(c1_text)
        self.c2_label.setText(c2_text)
        self.delta_label.setText(delta_text)
        if self.c2_label.isVisible() != dual_mode:
            self.activate_dual_mode(dual_mode)

    def activate_dual_mode(self, enabled: bool):
        """Affiche/masque C2 et Δ, puis recalcule la hauteur fixe du bandeau."""
        self.c2_label.setVisible(enabled)
        self.delta_label.setVisible(enabled)

        # Recalcul de la hauteur idéale du contenu…
        self.container.adjustSize()
        hint = self.container_layout.sizeHint()

        # … + 12 px pour que la barre de défilement horizontale ne masque pas
        # le texte du dernier label.
        final_height = hint.height() + 12
        self.scroll_area.setFixedHeight(final_height)
        self.setFixedHeight(final_height)
        self.updateGeometry()
