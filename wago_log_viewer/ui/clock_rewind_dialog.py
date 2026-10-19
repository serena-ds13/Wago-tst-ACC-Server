"""
Dialogue : quel tronçon conserver lors d'un changement d'heure sur l'automate.

Quand le temps recule dans un fichier (l'heure a été changée en cours
d'acquisition), les deux côtés du changement se recouvrent et ne peuvent pas
coexister sur un axe horaire unique. On demande donc à l'utilisateur, POUR
CHAQUE fichier concerné, s'il faut garder la partie AVANT ou APRÈS le
changement. Un choix groupé permet d'appliquer la même décision à tous.

Renvoie, via ``get_choices()``, un dictionnaire ``{index_fichier: "before"|"after"}``.
"""

from typing import Dict, List

from PySide6 import QtWidgets

#: Libellés des deux choix (l'ordre fixe l'index du combo : 0 = avant, 1 = après).
_BEFORE, _AFTER = "Avant le changement", "Après le changement"


class ClockRewindDialog(QtWidgets.QDialog):
    """Choix « avant / après » par fichier, avec application groupée optionnelle."""

    def __init__(self, affected: List[dict], parent=None):
        """
        ``affected`` : liste de dicts ``{"index": int, "name": str,
        "rewinds": [Timestamp, …]}`` — un par fichier où un recul a été détecté.
        """
        super().__init__(parent)
        self.setWindowTitle("Changement d'heure détecté")
        self._combos: Dict[int, QtWidgets.QComboBox] = {}

        layout = QtWidgets.QVBoxLayout(self)

        intro = QtWidgets.QLabel(
            "L'heure a été changée sur l'automate (le temps recule) dans le(s) "
            "fichier(s) ci-dessous. Les deux côtés du changement se recouvrent : "
            "choisissez, pour chacun, le tronçon à conserver."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- Choix groupé (facultatif) ---
        group_row = QtWidgets.QHBoxLayout()
        group_row.addWidget(QtWidgets.QLabel("Appliquer à tous :"))
        self.cmb_all = QtWidgets.QComboBox()
        self.cmb_all.addItems(["— choisir —", _BEFORE, _AFTER])
        self.cmb_all.currentIndexChanged.connect(self._apply_to_all)
        group_row.addWidget(self.cmb_all)
        group_row.addStretch()
        layout.addLayout(group_row)

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.HLine)
        layout.addWidget(sep)

        # --- Une ligne par fichier concerné (zone défilante si nombreux) ---
        host = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(host)
        grid.setColumnStretch(0, 1)
        for row, info in enumerate(affected):
            lbl = QtWidgets.QLabel(self._describe(info))
            lbl.setWordWrap(True)
            cmb = QtWidgets.QComboBox()
            cmb.addItems([_BEFORE, _AFTER])   # défaut : « avant » (index 0)
            grid.addWidget(lbl, row, 0)
            grid.addWidget(cmb, row, 1)
            self._combos[info["index"]] = cmb

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.resize(580, min(220 + 44 * len(affected), 520))

    @staticmethod
    def _describe(info: dict) -> str:
        name, rewinds = info["name"], info["rewinds"]
        if len(rewinds) == 1:
            return (f"<b>{name}</b><br>recul le "
                    f"{rewinds[0].strftime('%d/%m/%Y %H:%M:%S')}")
        return (f"<b>{name}</b><br>{len(rewinds)} changements d'heure "
                f"(« Avant » = jusqu'au 1er ; « Après » = après le dernier)")

    def _apply_to_all(self, combo_index: int):
        """Le choix groupé (1 = avant, 2 = après) recopie sur toutes les lignes."""
        if combo_index == 0:
            return  # « — choisir — » : ne touche à rien
        target = 0 if combo_index == 1 else 1
        for cmb in self._combos.values():
            cmb.setCurrentIndex(target)

    def get_choices(self) -> Dict[int, str]:
        """``{index_fichier: "before"|"after"}`` selon les combos."""
        return {idx: ("before" if cmb.currentIndex() == 0 else "after")
                for idx, cmb in self._combos.items()}
