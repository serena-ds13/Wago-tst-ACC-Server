"""
Zone de tracé des signaux (pyqtgraph).

Contient :
- ``ChannelMeta``     : métadonnées d'un canal à tracer (nom, type, couleur, plage Y) ;
- ``downsample_data`` : réduction du nombre de points avant tracé (performance) ;
- ``PlotPane``        : le widget de tracé proprement dit, utilisé en deux
  exemplaires dans la fenêtre principale :
    * onglet « Signaux »        → un graphique par canal (``add_timeseries``) ;
    * onglet « Superposition »  → tous les canaux sur un graphique (``add_overlay_plot``).

Les deux panes partagent leurs curseurs et leur plage X via les signaux
``requestSyncX`` / ``sync_from_external`` (câblés dans ``main_window``).
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .. import time_utils

logger = logging.getLogger(__name__)

#: Nombre de points visé après downsampling (compromis fluidité / fidélité).
DEFAULT_TARGET_POINTS = 10000


def _stepify(x: np.ndarray, y: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    Transforme une suite de points en escalier (« step-after ») : la valeur est
    maintenue jusqu'au point suivant, puis saute. Indispensable pour les signaux
    digitaux : l'automate n'écrit une valeur que lorsqu'elle change, donc deux
    échantillons consécutifs peuvent être très éloignés dans le temps. Sans
    escalier, pyqtgraph relierait alors les états 0 et 1 par une diagonale.
    """
    if len(x) < 2:
        return x, y
    x2 = np.repeat(x, 2)[1:]
    y2 = np.repeat(y, 2)[:-1]
    return x2, y2


@dataclass
class ChannelMeta:
    """Description d'un canal à tracer."""
    name: str                                       # nom de colonne, ex. "AICS" / "DI44P"
    is_digital: bool                                # True pour les DI (tracé 0/1)
    color: Optional[QtGui.QColor] = None            # couleur de la courbe
    y_range: Optional[Tuple[float, float]] = None   # plage Y imposée (sinon auto)


def downsample_data(
    x: np.ndarray,
    y: np.ndarray,
    target_points: int = DEFAULT_TARGET_POINTS,
    is_digital: bool = False,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Réduit le nombre de points à tracer tout en préservant la forme du signal.

    - Signaux digitaux : on ne garde que les transitions (plus les deux bornes),
      puis on reconstruit un escalier « step-after » (cf. ``_stepify``). L'échelon
      explicite est nécessaire car l'automate n'écrit une valeur que lorsqu'elle
      change : deux échantillons consécutifs peuvent être très espacés et seraient
      sinon reliés par une diagonale.
    - Signaux analogiques : « bucketing min-max » — pour chaque tranche, on
      conserve le minimum et le maximum, ce qui préserve les pics même très brefs.

    Garantit un alignement strict des axes X entre digital et analogique
    (premier et dernier point toujours conservés).
    """
    n = len(x)

    if is_digital:
        if n > target_points:
            # Indices où la valeur change réellement (+1 : index de l'état neuf).
            transitions = np.nonzero(np.diff(y))[0] + 1
            indices = np.unique(np.concatenate(([0], transitions, [n - 1])))
            x, y = x[indices], y[indices]
        return _stepify(x, y)

    if n <= target_points:
        return x, y

    # --- Bucketing min-max pour les signaux analogiques ---
    bucket_size = float(n) / target_points
    indices = [0]
    for i in range(1, target_points - 1):
        start = int(np.floor((i - 1) * bucket_size)) + 1
        end = int(np.floor(i * bucket_size)) + 1
        if start >= n:
            break
        if end >= n:
            end = n - 1
        segment = y[start:end]
        if segment.size == 0:
            continue
        min_idx = start + int(np.argmin(segment))
        max_idx = start + int(np.argmax(segment))
        indices.extend(sorted({min_idx, max_idx}))

    indices.append(n - 1)
    indices = np.unique(np.array(indices, dtype=int))

    # Sécurité : si le bucketing a produit trop de points, répartition uniforme.
    if len(indices) > target_points:
        indices = np.unique(np.linspace(0, n - 1, target_points, dtype=int))
    return x[indices], y[indices]


class PlotPane(QtWidgets.QWidget):
    """
    Widget de tracé multi-graphiques avec curseurs verticaux synchronisés.

    Signaux :
    - ``requestSyncX(float, object)`` : positions des curseurs C1/C2 (pour
      synchroniser l'autre pane et le bandeau d'info) ;
    - ``requestColorChange(ChannelMeta)`` : demande de changement de couleur
      (menu contextuel d'un graphique) ;
    - ``viewChanged(object)`` : la plage visible a changé (zoom/pan) →
      déclenche le re-downsampling adaptatif dans la fenêtre principale.
    """

    requestSyncX = QtCore.Signal(float, object)
    requestColorChange = QtCore.Signal(ChannelMeta)
    viewChanged = QtCore.Signal(object)

    def __init__(self, title: str):
        super().__init__()
        self.setLayout(QtWidgets.QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().setSpacing(2)

        self.title_label = QtWidgets.QLabel(f"<b>{title}</b>")
        self.title_label.setStyleSheet("padding-left: 10px;")
        self.layout().addWidget(self.title_label)

        self.view = pg.GraphicsLayoutWidget()
        self.layout().addWidget(self.view)

        # Plage X à restaurer après un re-tracé (None = toute la période).
        self.saved_x_range: Optional[Tuple[float, float]] = None

        # --- État ---
        self.plots: List[pg.PlotItem] = []
        #: Une paire de curseurs (C1, C2) par graphique.
        self.vlines: List[Tuple[pg.InfiniteLine, pg.InfiniteLine]] = []
        self.h_lines: List[pg.InfiniteLine] = []
        self.master: Optional[pg.PlotItem] = None   # graphique de référence (lien X)
        self.is_dual_mode = False                   # second curseur actif ?
        self._scene_click_connected = False

        # Antialiasing désactivé : gain de performance notable sur les
        # gros fichiers, sans perte de lisibilité.
        pg.setConfigOptions(
            enableExperimental=True,
            antialias=False,
            background=(30, 33, 36),
        )

    # ------------------------------------------------------------------
    # Gestion du cycle de vie
    # ------------------------------------------------------------------

    def clear(self):
        """Détruit tous les graphiques et déconnecte proprement les signaux."""
        for c1, c2 in self.vlines:
            for cursor in (c1, c2):
                try:
                    cursor.sigPositionChanged.disconnect()
                except Exception:
                    pass  # déjà déconnecté

        self._disconnect_scene_click()
        self.view.clear()
        self.plots = []
        self.vlines = []
        self.h_lines = []
        self.master = None

    def _disconnect_scene_click(self):
        try:
            if self._scene_click_connected:
                self.view.scene().sigMouseClicked.disconnect(self._on_click)
                self._scene_click_connected = False
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Construction des graphiques
    # ------------------------------------------------------------------

    def add_timeseries(self, data: pd.DataFrame, channels: List[ChannelMeta]):
        """
        Trace chaque canal dans son propre graphique, empilés verticalement.
        Tous les graphiques sont liés en X au premier (« master »").
        La plage X visible et les positions des curseurs sont préservées
        d'un re-tracé à l'autre.
        """
        x_raw = time_utils.index_to_axis_seconds(data.index)

        # Positions par défaut des curseurs : milieu de la période.
        saved_c1_pos = min(x_raw) + (max(x_raw) - min(x_raw)) / 2
        saved_c2_pos = saved_c1_pos + 0.5

        # Sauvegarde de l'état actuel (plage X + curseurs) avant destruction.
        if self.master is not None:
            try:
                self.saved_x_range = self.master.getViewBox().viewRange()[0]
                if self.vlines:
                    saved_c1_pos = self.vlines[0][0].value()
                    saved_c2_pos = self.vlines[0][1].value()
            except Exception:
                pass

        self.clear()
        if data.empty or len(channels) == 0:
            return

        # Suspendre le rendu pendant la construction (évite les repaints inutiles).
        self.view.setUpdatesEnabled(False)

        for i, ch in enumerate(channels):
            plt: pg.PlotItem = self.view.addPlot(row=i, col=0)
            plt.setContentsMargins(0, 0, 0, 0)
            plt.showGrid(x=True, y=True, alpha=0.2)
            plt.getAxis("bottom").setStyle(showValues=False)
            plt.setMouseEnabled(x=True, y=False)   # zoom/pan horizontal uniquement
            plt.getAxis("left").setWidth(40)
            # Auto-range désactivé : la plage visible n'est pilotée que par
            # l'utilisateur. Sans cela, le re-downsampling avec marge
            # (MainWindow._do_deferred_zoom) déclencherait un auto-zoom sur la
            # marge à chaque rafraîchissement → dézoom exponentiel.
            plt.getViewBox().enableAutoRange(x=False, y=False)

            # Entrée « Couleur » dans le menu contextuel du graphique.
            color_action = QtGui.QAction("Couleur", plt)
            color_action.triggered.connect(lambda _, c=ch: self.requestColorChange.emit(c))
            plt.getMenu().addAction(color_action)

            # Données Y (zéros si la colonne n'existe pas dans ce jeu de données).
            if ch.name in data.columns:
                y = np.nan_to_num(data[ch.name].to_numpy(dtype=float), nan=0.0)
            else:
                y = np.zeros(len(x_raw), dtype=float)

            x_plot, y_plot = downsample_data(x_raw, y, is_digital=ch.is_digital)

            curve = pg.PlotDataItem(
                x_plot, y_plot,
                pen=pg.mkPen(ch.color, width=1.5),
                name=ch.name,
                connect="finite",
            )
            # column_id permet de retrouver la colonne source lors du
            # re-downsampling adaptatif (voir MainWindow._do_deferred_zoom).
            curve.column_id = ch.name
            plt.addItem(curve)
            plt.sigRangeChanged.connect(lambda obj: self.viewChanged.emit(obj))

            if ch.is_digital:
                # Échelle fixe 0/1 pour les signaux digitaux.
                plt.setYRange(-0.1, 1.1, padding=0)
                plt.getAxis("left").setTicks([[(0, "0"), (1, "1")]])
                label_html = (
                    f'<span style="border-radius:4px; padding:2px 10px; '
                    f'color:{pg.mkColor(ch.color).name()}">'
                    f'{ch.name.replace("DI", "")} [-]</span>'
                )
                plt.setLabel("left", label_html)
            else:
                if ch.y_range:
                    plt.setYRange(ch.y_range[0], ch.y_range[1], padding=0)
                else:
                    plt.setYRange(-2, max(y_plot) + 2, padding=0)
                # La vitesse (issue de D3) s'affiche en km/h, les courants moteurs
                # en ampères, le reste en volts.
                if "Vit" in ch.name:
                    unit = " (D3) [km/h]"
                elif "IMOT" in ch.name.upper():
                    unit = " [A]"
                else:
                    unit = " [V]"
                label_html = (
                    f'<span style="border-radius:4px; padding:2px 10px; '
                    f'color:{pg.mkColor(ch.color).name()}">'
                    f'{ch.name.replace("AI", "") + unit}</span>'
                )
                plt.setLabel("left", label_html)
                # Curseur horizontal de mesure (ligne pointillée jaune déplaçable).
                self._add_horizontal_cursor(plt, hval=np.mean(y_plot))

            # Lien X : le premier graphique sert de référence aux suivants.
            if i == 0:
                self.master = plt
            else:
                plt.setXLink(self.master)
            plt.getAxis("bottom").hide()

            c1, c2 = self._setup_cursors_for_plot(plt, saved_c1_pos, saved_c2_pos)
            self.vlines.append((c1, c2))
            self.plots.append(plt)
            c1.sigPositionChanged.connect(lambda line, idx=0: self._on_vline_dragged(line, idx))
            c2.sigPositionChanged.connect(lambda line, idx=1: self._on_vline_dragged(line, idx))

        # Axe temporel commun, en bas de la pile.
        axis_x = pg.DateAxisItem(orientation="bottom")
        x_axis_plot = self.view.addPlot(row=len(channels), col=0, axisItems={"bottom": axis_x})
        x_axis_plot.setXLink(self.master)
        x_axis_plot.hideAxis("left")
        x_axis_plot.setFixedHeight(40)
        x_axis_plot.getViewBox().setMouseEnabled(y=False)

        # Clic dans un graphique → positionnement du curseur C1.
        self._disconnect_scene_click()
        self.view.scene().sigMouseClicked.connect(self._on_click)
        self._scene_click_connected = True

        # Bornes de navigation : impossible de sortir de la période des données.
        # Les limites sont posées sur CHAQUE ViewBox, pas seulement le maître :
        # si un seul graphique clampait, un pan/zoom à la souris sur un autre
        # graphique pourrait dépasser la borne pendant que le maître reste
        # bloqué, ce qui désynchroniserait les axes X (« signal dézoomé seul »).
        x_min_data, x_max_data = float(min(x_raw)), float(max(x_raw))
        for p in self.plots + [x_axis_plot]:
            p.getViewBox().setLimits(xMin=x_min_data, xMax=x_max_data)

        # Restauration de la plage X (ou affichage de toute la période).
        if self.saved_x_range is not None:
            self.set_x_limits(self.saved_x_range[0], self.saved_x_range[1])
            self.master.setXRange(self.saved_x_range[0], self.saved_x_range[1], padding=0)
        else:
            self.set_x_limits(min(x_raw), max(x_raw))
            self.master.setXRange(x_raw[0], x_raw[-1], padding=0)

        self.view.setUpdatesEnabled(True)

    def add_overlay_plot(self, data: pd.DataFrame, channels: List[ChannelMeta],
                         v_lines_source=None):
        """
        Trace tous les canaux superposés sur UN seul graphique, avec légende
        (onglet « Superposition »). ``v_lines_source`` permet de reprendre les
        positions des curseurs de l'autre pane.
        """
        if data.empty or len(channels) == 0:
            return

        x_raw = time_utils.index_to_axis_seconds(data.index)

        if self.master is not None:
            try:
                self.saved_x_range = self.master.getViewBox().viewRange()[0]
            except Exception:
                pass

        self.clear()
        layout = self.view.ci

        # Légende dans une colonne fixe à gauche du graphique.
        legend = pg.LegendItem(offset=(5, 5), brush=(40, 44, 48, 200))
        layout.addItem(legend, row=0, col=0)
        layout.layout.setColumnFixedWidth(0, 80)
        legend.setFixedHeight(28 * len(channels))

        axis_x = pg.DateAxisItem(orientation="bottom")
        plt = pg.PlotItem(axisItems={"bottom": axis_x})
        layout.addItem(plt, row=0, col=1)
        layout.layout.setColumnStretchFactor(1, 1)

        plt.showGrid(x=True, y=True, alpha=0.2)
        plt.setMouseEnabled(x=True, y=False)
        plt.getViewBox().setMenuEnabled(True)

        # Plage Y globale calculée sur TOUTES les données avant le tracé,
        # puis verrouillée : l'axe Y ne bouge plus pendant le zoom/pan X.
        y_min_global, y_max_global = float("inf"), float("-inf")
        for ch in channels:
            if ch.name in data.columns:
                vals = np.nan_to_num(data[ch.name].to_numpy(dtype=float), nan=0.0)
                if len(vals):
                    y_min_global = min(y_min_global, float(vals.min()))
                    y_max_global = max(y_max_global, float(vals.max()))
        if y_min_global == float("inf"):
            y_min_global, y_max_global = 0.0, 1.0
        y_margin = max(0.5, (y_max_global - y_min_global) * 0.05)

        for ch in channels:
            if ch.name not in data.columns:
                continue
            y = np.nan_to_num(data[ch.name].to_numpy(dtype=float), nan=0.0)
            x_plot, y_plot = downsample_data(x_raw, y, is_digital=ch.is_digital)
            curve = pg.PlotDataItem(
                x_plot, y_plot,
                pen=pg.mkPen(ch.color, width=1.5),
                name=ch.name,
                antialias=False,
            )
            curve.column_id = ch.name
            plt.addItem(curve)
            plt.sigRangeChanged.connect(lambda obj: self.viewChanged.emit(obj))

            prefix = "[D]" if ch.is_digital else "[A]"
            legend.addItem(curve, f"{prefix} {ch.name.replace('DI', '').replace('AI', '')}")

        plt.setYRange(y_min_global - y_margin, y_max_global + y_margin, padding=0)
        # Auto-range désactivé sur les deux axes (cf. add_timeseries : évite le
        # dézoom exponentiel provoqué par la marge de re-downsampling).
        plt.getViewBox().enableAutoRange(x=False, y=False)
        plt.getViewBox().setAutoVisible(y=False)

        self.master = plt
        self.plots = [plt]

        # Reprise des positions de curseurs de l'autre pane, si fournies.
        saved_c1_pos = v_lines_source[0][0].value() if v_lines_source and v_lines_source[0][0] else None
        saved_c2_pos = v_lines_source[0][1].value() if v_lines_source and v_lines_source[0][1] else None
        c1, c2 = self._setup_cursors_for_plot(plt, saved_c1_pos, saved_c2_pos)
        self.vlines = [(c1, c2)]

        c1.sigPositionChanged.connect(lambda line, idx=0: self._on_vline_dragged(line, idx))
        c2.sigPositionChanged.connect(lambda line, idx=1: self._on_vline_dragged(line, idx))

        self._disconnect_scene_click()
        self.view.scene().sigMouseClicked.connect(self._on_click)
        self._scene_click_connected = True
        plt.getViewBox().setLimits(xMin=min(x_raw), xMax=max(x_raw))

        if self.saved_x_range is not None:
            plt.setXRange(self.saved_x_range[0], self.saved_x_range[1], padding=0)
        else:
            plt.setXRange(x_raw[0], x_raw[-1], padding=0)
        self._add_horizontal_cursor(plt, hval=(y_min_global + y_max_global) / 2)

    # ------------------------------------------------------------------
    # Downsampling adaptatif au zoom
    # ------------------------------------------------------------------

    def get_downsampled_data(self, df, column, x_min, x_max,
                             is_digital=False, target_pts=DEFAULT_TARGET_POINTS):
        """
        Ré-échantillonne une colonne sur la fenêtre visible [x_min, x_max].

        Appelé à chaque zoom/pan (avec debounce) par la fenêtre principale :
        plus on zoome, plus la densité de points réels affichés augmente.

        Pour les signaux digitaux, la courbe est ancrée aux deux bords de la
        fenêtre (valeur du dernier état connu) afin qu'une ligne soit toujours
        visible même sans transition dans la fenêtre.
        """
        try:
            if not df.index.is_monotonic_increasing:
                df = df.sort_index()

            t_start = time_utils.axis_seconds_to_timestamp(x_min)
            t_end = time_utils.axis_seconds_to_timestamp(x_max)

            # Découpe rapide sur la fenêtre visible.
            view = df.loc[t_start:t_end, column].dropna()

            if is_digital:
                # État en entrée de fenêtre = dernier point connu avant t_start.
                before = df.loc[:t_start, column].dropna()
                start_val = float(before.iloc[-1]) if not before.empty else 0.0

                if view.empty:
                    # Aucune donnée dans la fenêtre → ligne plate à l'état courant.
                    return np.array([x_min, x_max]), np.array([start_val, start_val])

                # Ne garder que les transitions (l'escalier est reconstruit après).
                if len(view) > target_pts:
                    view_ds = view[view.diff().fillna(1).ne(0)]
                else:
                    view_ds = view

                # Conversion centralisée (décalage évalué à la date des données).
                x_out = time_utils.index_to_axis_seconds(view_ds.index)
                y_out = view_ds.values.astype(np.float64)

                # Ancrage aux bords de la fenêtre visible.
                if x_out[0] > x_min:
                    x_out = np.concatenate([[x_min], x_out])
                    y_out = np.concatenate([[start_val], y_out])
                if x_out[-1] < x_max:
                    x_out = np.concatenate([x_out, [x_max]])
                    y_out = np.concatenate([y_out, [y_out[-1]]])

                # Escalier explicite : valeur tenue jusqu'au changement suivant.
                return _stepify(x_out, y_out)

            # --- Signaux analogiques ---
            if view.empty:
                return np.array([]), np.array([])

            if len(view) <= target_pts:
                view_ds = view
            else:
                step = max(1, len(view) // target_pts)
                view_ds = view.iloc[::step]

            # Conversion centralisée (décalage évalué à la date des données).
            x = time_utils.index_to_axis_seconds(view_ds.index)
            y = view_ds.values.astype(np.float64)
            return x, y

        except Exception:
            logger.exception("Échec du downsampling adaptatif (colonne %s)", column)
            return np.array([]), np.array([])

    # ------------------------------------------------------------------
    # Curseurs
    # ------------------------------------------------------------------

    def _setup_cursors_for_plot(self, plt, saved_c1_pos, saved_c2_pos):
        """Crée la paire de curseurs verticaux (C1 blanc, C2 bleu) d'un graphique."""
        c1 = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("#ebe7e7", width=2))
        c2 = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("#adbbfa", width=2))
        c1.setZValue(1000)  # toujours au-dessus des courbes
        c2.setZValue(1000)

        if not self.is_dual_mode:
            c2.hide()

        if saved_c1_pos is not None:
            c1.setValue(saved_c1_pos)
        if saved_c2_pos is not None:
            c2.setValue(saved_c2_pos)

        plt.addItem(c1)
        plt.addItem(c2)
        return c1, c2

    def _add_horizontal_cursor(self, plt: pg.PlotItem, hval=0.5):
        """Ajoute une ligne horizontale de mesure déplaçable (valeur affichée)."""
        h_line = pg.InfiniteLine(
            angle=0,
            movable=True,
            pen=pg.mkPen(color="#ffcf33", width=1, style=QtCore.Qt.DashLine),
            label="{value:.2f}",
            labelOpts={
                "color": "#ffcf33",
                "position": 0.02,
                # Deux ancres identiques requises pour éviter un IndexError pyqtgraph.
                "anchors": [(0, 1), (0, 1)],
                "movable": True,
            },
        )
        h_line.setZValue(1001)
        h_line.setValue(hval)
        plt.addItem(h_line)
        self.h_lines.append(h_line)

    def toggle_second_cursor(self, enabled: bool):
        """Affiche/masque le second curseur (C2) sur tous les graphiques."""
        self.is_dual_mode = enabled
        if not self.vlines:
            return

        # C2 est placé juste à côté de C1 lors de l'activation.
        pos_c1 = self.vlines[0][0].value()
        for _c1, c2 in self.vlines:
            if enabled:
                c2.setValue(pos_c1 + 2)
                c2.show()
            else:
                c2.hide()

        self._emit_sync_all()

    def _on_vline_dragged(self, line, cursor_index):
        """L'utilisateur déplace un curseur à la souris → propager à tous les graphiques."""
        new_pos = line.value()
        if cursor_index == 0:
            self._broadcast_fast(x1=new_pos)
        else:
            self._broadcast_fast(x2=new_pos)

    def _broadcast_fast(self, x1=None, x2=None):
        """
        Positionne les curseurs sur tous les graphiques en bloquant leurs
        signaux (évite une boucle infinie de notifications), puis émet une
        seule synchronisation.
        """
        if not self.vlines:
            return

        for c1, c2 in self.vlines:
            c1.blockSignals(True)
            c2.blockSignals(True)
            if x1 is not None:
                c1.setValue(x1)
            if x2 is not None and self.is_dual_mode:
                c2.setValue(x2)
            c1.blockSignals(False)
            c2.blockSignals(False)
        self._emit_sync_all()

    def _on_click(self, ev):
        """Clic gauche dans un graphique → positionne le curseur C1."""
        pos = ev.scenePos()
        for p in self.plots:
            vb = p.getViewBox()
            if vb.sceneBoundingRect().contains(pos):
                mouse_x = vb.mapSceneToView(pos).x()
                if ev.button() == QtCore.Qt.LeftButton:
                    self._broadcast_fast(x1=mouse_x)
                # Clic droit réservé au menu contextuel pyqtgraph.
                break

    def _emit_sync_all(self):
        """Publie les positions courantes des curseurs (signal requestSyncX)."""
        if self.vlines:
            p1 = self.vlines[0][0].value()
            p2 = self.vlines[0][1].value() if self.is_dual_mode else None
            self.requestSyncX.emit(p1, p2)

    def sync_from_external(self, x1, x2=None):
        """Reçoit une position de curseur de l'autre pane, SANS ré-émettre de signal."""
        if not self.vlines:
            return
        for c1, c2 in self.vlines:
            c1.blockSignals(True)
            c2.blockSignals(True)
            if x1 is not None:
                c1.setValue(x1)
            if x2 is not None and self.is_dual_mode:
                c2.setValue(x2)
            c1.blockSignals(False)
            c2.blockSignals(False)

    # ------------------------------------------------------------------
    # Divers
    # ------------------------------------------------------------------

    def set_x_limits(self, xmin, xmax):
        """Cale la plage X visible du graphique de référence."""
        if self.master:
            self.master.setXRange(xmin, xmax)
