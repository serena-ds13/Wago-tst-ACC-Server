"""
Sélecteur de canaux : liste à cases à cocher, réordonnable par glisser-déposer.

Deux instances dans la fenêtre principale : une pour les signaux analogiques
(AI), une pour les signaux digitaux (DI). L'ordre des éléments dans la liste
détermine l'ordre des graphiques à l'écran.
"""

from typing import List

from PySide6 import QtCore, QtWidgets


class ChannelSelector(QtWidgets.QGroupBox):
    """Groupe « liste de canaux » avec boutons Tout / Aucun."""

    #: Émis (avec un léger différé) à chaque changement de sélection ou d'ordre.
    changed = QtCore.Signal()

    def __init__(self, title: str):
        super().__init__(title)
        self.setLayout(QtWidgets.QVBoxLayout())

        self.list = QtWidgets.QListWidget()
        self.list.setMaximumHeight(75)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)

        # Glisser-déposer interne : permet de réordonner les graphiques.
        self.list.setDragEnabled(True)
        self.list.setAcceptDrops(True)
        self.list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(QtCore.Qt.MoveAction)

        self.layout().addWidget(self.list)

        btns = QtWidgets.QHBoxLayout()
        self.btn_all = QtWidgets.QPushButton("Tout")
        self.btn_none = QtWidgets.QPushButton("Aucun")
        btns.addWidget(self.btn_all)
        btns.addWidget(self.btn_none)
        self.layout().addLayout(btns)

        self.btn_all.clicked.connect(self.check_all)
        self.btn_none.clicked.connect(self.uncheck_all)

        # Changement d'état d'une case.
        self.list.itemChanged.connect(lambda *_: self._request_update())
        # Fin d'un glisser-déposer (réordonnancement).
        self.list.model().rowsMoved.connect(lambda *_: self._request_update())

    def _request_update(self):
        # Léger différé pour laisser l'UI se stabiliser avant d'émettre.
        QtCore.QTimer.singleShot(10, self.changed.emit)

    def set_channels(self, names: List[str]):
        """Remplit la liste (toutes les cases cochées par défaut)."""
        self.list.blockSignals(True)
        self.list.clear()
        for name in names:
            # Le préfixe AI/DI est retiré à l'affichage (il est ré-ajouté
            # par get_selected pour retrouver le nom de colonne).
            display_name = name.replace("AI", "").replace("DI", "")
            it = QtWidgets.QListWidgetItem(display_name)
            it.setFlags(
                it.flags()
                | QtCore.Qt.ItemIsUserCheckable
                | QtCore.Qt.ItemIsDragEnabled
                | QtCore.Qt.ItemIsEnabled
                | QtCore.Qt.ItemIsSelectable
            )
            it.setCheckState(QtCore.Qt.Checked)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self._request_update()

    def get_selected(self, prefix: str = "") -> List[str]:
        """
        Retourne les noms de colonnes cochés, dans l'ordre actuel de la liste.
        ``prefix`` (« AI » ou « DI ») est ré-accolé au nom affiché.
        """
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == QtCore.Qt.Checked:
                out.append(prefix + it.text())
        return out

    def check_all(self):
        self._set_all(QtCore.Qt.Checked)

    def uncheck_all(self):
        self._set_all(QtCore.Qt.Unchecked)

    def _set_all(self, state):
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)
        self.list.blockSignals(False)
        self._request_update()
