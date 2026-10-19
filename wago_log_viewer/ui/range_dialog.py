"""
Boîte de dialogue de réglage manuel des plages Y des signaux analogiques.

Ouverte via le bouton « Réglage des axes Y » de la fenêtre principale.
Les plages choisies sont conservées dans ``MainWindow.custom_ranges`` et
prennent le pas sur l'échelle automatique.
"""

from PySide6 import QtWidgets


class RangeDialog(QtWidgets.QDialog):
    """Une ligne (Min, Max) par signal analogique sélectionné."""

    def __init__(self, channels, current_ranges, df, parent=None):
        """
        Paramètres
        ----------
        channels : noms des colonnes AI sélectionnées.
        current_ranges : dict {colonne: (min, max)} des plages déjà définies.
        df : DataFrame complet, utilisé pour proposer des valeurs par défaut.
        """
        super().__init__(parent)
        self.setWindowTitle("Définir les plages des signaux")
        layout = QtWidgets.QVBoxLayout(self)

        self.inputs = {}
        grid = QtWidgets.QGridLayout()
        grid.addWidget(QtWidgets.QLabel("Signal"), 0, 0)
        grid.addWidget(QtWidgets.QLabel("Min (Y)"), 0, 1)
        grid.addWidget(QtWidgets.QLabel("Max (Y)"), 0, 2)

        for i, ch_name in enumerate(channels):
            lbl = QtWidgets.QLabel(ch_name.replace("AI", ""))

            # Plage par défaut : min/max des données ± 2, sinon plage déjà définie.
            if ch_name in df.columns:
                default_range = (float(df[ch_name].min()) - 2,
                                 float(df[ch_name].max()) + 2)
            else:
                default_range = (0.0, 100.0)
            curr_min, curr_max = current_ranges.get(ch_name, default_range)

            edit_min = QtWidgets.QDoubleSpinBox()
            edit_min.setRange(-999999, 999999)
            edit_min.setValue(curr_min)

            edit_max = QtWidgets.QDoubleSpinBox()
            edit_max.setRange(-999999, 999999)
            edit_max.setValue(curr_max)

            grid.addWidget(lbl, i + 1, 0)
            grid.addWidget(edit_min, i + 1, 1)
            grid.addWidget(edit_max, i + 1, 2)

            self.inputs[ch_name] = (edit_min, edit_max)

        layout.addLayout(grid)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_ranges(self) -> dict:
        """Retourne {nom de colonne: (min, max)} pour tous les signaux du dialogue."""
        return {name: (min_w.value(), max_w.value())
                for name, (min_w, max_w) in self.inputs.items()}
