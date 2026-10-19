"""
Graphe TEMPS RÉEL à fenêtre glissante.

Affiche les ``window_seconds`` dernières secondes (10 s par défaut) : un
graphique par canal sélectionné, empilés et liés en X, avec un axe temporel
commun en bas. À chaque arrivée de données, la fenêtre se décale pour suivre
le dernier échantillon — le tracé « avance » en continu.

Contrairement à ``PlotPane`` (affichage statique de millions de points avec
downsampling adaptatif), ce pane garde un faible nombre de points (quelques
centaines : 10 s à 50 Hz = 500 points) et n'a donc pas besoin de downsampling.
"""

from typing import Dict, List

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtWidgets

from .. import time_utils
from . import palette
from .plot_pane import ChannelMeta

#: Hauteur minimale d'un graphique (px) — identique à l'onglet « Signaux ».
#: Au-delà de la hauteur de l'écran, la zone défilante verticale prend le relais.
MIN_LIVE_PLOT_HEIGHT = 150

#: Échelles Y FIXES des signaux analogiques en temps réel, par mot-clé du nom
#: de canal (recherché en majuscules). Le premier mot-clé contenu dans le nom
#: gagne ; un canal non listé garde une échelle automatique.
FIXED_ANALOG_Y_RANGES = [
    ("CS", (-1.0, 12.0)),
    ("IMOT", (-5.0, 300.0)),
    ("VLIGNE", (-5.0, 900.0)),
    ("VIT", (-5.0, 80.0)),
]


def fixed_analog_range(name: str):
    """Échelle Y fixe imposée pour un canal analogique, ou None (auto)."""
    up = name.upper()
    for key, rng in FIXED_ANALOG_Y_RANGES:
        if key in up:
            return rng
    return None


def _stepify(x: np.ndarray, y: np.ndarray):
    """
    Transforme une suite de points en escalier (« step-after ») : la valeur est
    maintenue jusqu'au point suivant, puis saute. Indispensable pour les signaux
    digitaux, sinon pyqtgraph relie les états par des diagonales.
    """
    if len(x) < 2:
        return x, y
    x2 = np.repeat(x, 2)[1:]
    y2 = np.repeat(y, 2)[:-1]
    return x2, y2


class LivePlotPane(QtWidgets.QWidget):
    """Pile de graphiques à fenêtre temporelle glissante."""

    def __init__(self, window_seconds: float = 10.0):
        super().__init__()
        self.window_seconds = float(window_seconds)

        self.setLayout(QtWidgets.QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().setSpacing(2)

        self.view = pg.GraphicsLayoutWidget()
        self.layout().addWidget(self.view)

        self.plots: Dict[str, pg.PlotItem] = {}
        self.curves: Dict[str, pg.PlotDataItem] = {}
        self._channels: List[ChannelMeta] = []
        self.master = None

        pg.setConfigOptions(antialias=False, background=(28, 31, 34))

    # ------------------------------------------------------------------

    def set_channels(self, channels: List[ChannelMeta]):
        """(Re)construit la pile de graphiques pour la sélection donnée."""
        self.view.clear()
        self.plots = {}
        self.curves = {}
        self._channels = list(channels)
        self.master = None

        # Hauteur minimale proportionnelle au nombre de graphiques : au-delà de
        # l'écran, la zone défilante verticale (LiveView) prend le relais, comme
        # dans l'onglet « Signaux ».
        self.setMinimumHeight(len(channels) * MIN_LIVE_PLOT_HEIGHT)
        if not channels:
            return

        for i, ch in enumerate(channels):
            plt: pg.PlotItem = self.view.addPlot(row=i, col=0)
            plt.showGrid(x=True, y=True, alpha=0.2)
            plt.setMouseEnabled(x=False, y=False)   # fenêtre pilotée par le flux
            plt.getAxis("left").setWidth(40)
            plt.getAxis("bottom").setStyle(showValues=False)
            plt.getAxis("bottom").hide()

            color = ch.color or palette.color_for_channel(ch.name)
            curve = pg.PlotDataItem(pen=pg.mkPen(color, width=1.5), connect="finite")
            plt.addItem(curve)

            if ch.is_digital:
                plt.setYRange(-0.1, 1.1, padding=0)
                plt.getAxis("left").setTicks([[(0, "0"), (1, "1")]])
                inner = f'{ch.name.replace("DI", "")} [-]'
            else:
                # Échelle Y : imposée par ChannelMeta, sinon fixée par nom
                # (CS, Imot, Vligne…), sinon automatique.
                yr = ch.y_range or fixed_analog_range(ch.name)
                if yr:
                    plt.setYRange(yr[0], yr[1], padding=0)
                    plt.enableAutoRange(y=False)
                else:
                    plt.enableAutoRange(y=True)
                if "Vit" in ch.name:
                    unit = " (D3) [km/h]"
                elif "IMOT" in ch.name.upper():
                    unit = " [A]"
                else:
                    unit = " [V]"
                inner = ch.name.replace("AI", "") + unit

            # Étiquette identique à l'onglet « Signaux » (PlotPane.add_timeseries).
            plt.setLabel(
                "left",
                f'<span style="border-radius:4px; padding:2px 10px; '
                f'color:{pg.mkColor(color).name()}">{inner}</span>')

            if i == 0:
                self.master = plt
            else:
                plt.setXLink(self.master)

            self.plots[ch.name] = plt
            self.curves[ch.name] = curve

        # Axe temporel commun en bas de la pile.
        axis_x = pg.DateAxisItem(orientation="bottom")
        x_axis_plot = self.view.addPlot(
            row=len(channels), col=0, axisItems={"bottom": axis_x})
        x_axis_plot.setXLink(self.master)
        x_axis_plot.hideAxis("left")
        x_axis_plot.setFixedHeight(40)
        x_axis_plot.setMouseEnabled(x=False, y=False)

    # ------------------------------------------------------------------

    def update_data(self, df: pd.DataFrame):
        """
        Met à jour les courbes à partir du tampon glissant ``df`` (indexé par
        ``Date``) et décale la fenêtre pour afficher les ``window_seconds``
        dernières secondes.
        """
        if self.master is None or df is None or df.empty:
            return

        x_all = time_utils.index_to_axis_seconds(df.index)
        if len(x_all) == 0:
            return
        x_max = float(x_all[-1])
        x_min = x_max - self.window_seconds

        for ch in self._channels:
            curve = self.curves.get(ch.name)
            if curve is None or ch.name not in df.columns:
                continue
            y = np.nan_to_num(df[ch.name].to_numpy(dtype=float), nan=0.0)
            if ch.is_digital:
                xs, ys = _stepify(x_all, y)
                curve.setData(xs, ys)
            else:
                curve.setData(x_all, y)

        # Décalage de la fenêtre : toujours les 10 dernières secondes.
        self.master.setXRange(x_min, x_max, padding=0)

    def clear_data(self):
        """Vide les courbes sans détruire les graphiques."""
        for curve in self.curves.values():
            curve.clear()
