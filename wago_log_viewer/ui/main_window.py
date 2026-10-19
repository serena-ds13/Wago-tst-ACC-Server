"""
Fenêtre principale du Wago Log Viewer.

Organisation de l'écran :

    ┌────────────┬──────────────────────────────────────┐
    │  Barre     │  Onglet « Signaux »                  │
    │  latérale  │  (un graphique par canal, empilés)   │
    │            ├──────────────────────────────────────┤
    │  fichiers  │  Onglet « Superposition »            │
    │  fenêtre   │  (tous les canaux sur un graphique)  │
    │  canaux    │                                      │
    │  options   │                                      │
    └────────────┴──────────────────────────────────────┘

Cette classe orchestre tous les composants : chargement des fichiers (thread
séparé), sélection des canaux, fenêtre temporelle, curseurs synchronisés
entre les deux onglets, navigation sur les fronts montants du signal 44P,
et re-downsampling adaptatif lors du zoom.
"""

import math
import re
from collections import defaultdict
from typing import List, Optional

import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets
import colorsys as cs

from .. import __version__, time_utils
from ..data import signal_analysis as sa
from . import plot_pane as pp
from . import theme
from . import palette
from .channel_selector import ChannelSelector
from .clock_rewind_dialog import ClockRewindDialog
from .cursor_info_pane import CursorInfoPane
from .live_view import LiveView
from .range_dialog import RangeDialog
from .workers import FileLoadWorker

#: Hauteur minimale d'un graphique individuel (px) dans l'onglet « Signaux ».
MIN_PLOT_HEIGHT = 150

#: Résolution interne des barres de défilement horizontales (sans unité).
SCROLLBAR_RESOLUTION = 100000


class MainWindow(QtWidgets.QMainWindow):
    """Fenêtre principale de l'application."""

    def __init__(self):
        super().__init__()
        # Version affichée dans le titre : permet de vérifier d'un coup d'œil
        # quelle version du code tourne réellement.
        self.setWindowTitle(f"Wago Log Viewer {__version__}")
        self.setMinimumSize(800, 600)

        theme.apply_dark_theme(QtWidgets.QApplication.instance())

        # Construction de l'interface, puis de l'état, puis câblage.
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root_layout = QtWidgets.QHBoxLayout(central)
        root_layout.setContentsMargins(10, 6, 6, 6)
        root_layout.setSpacing(10)

        self.sidebar = self._build_sidebar()
        root_layout.addWidget(self.sidebar)
        self.tabs = self._build_tabs()
        root_layout.addWidget(self.tabs, 1)

        self._init_state()
        self._connect_signals()

    def showEvent(self, event):
        super().showEvent(event)
        # Démarrage en plein écran ; Qt gère ensuite le redimensionnement.
        self.showMaximized()
        self.setWindowState(QtCore.Qt.WindowMaximized)

    # ==================================================================
    # CONSTRUCTION DE L'INTERFACE
    # ==================================================================

    def _build_sidebar(self) -> QtWidgets.QScrollArea:
        """Barre latérale : fichiers, fenêtre temporelle, canaux, options."""
        side = QtWidgets.QFrame()
        side.setFrameShape(QtWidgets.QFrame.StyledPanel)
        side.setMinimumWidth(300)
        layout = QtWidgets.QVBoxLayout(side)
        layout.setSpacing(8)

        # --- Ouverture de fichiers ---
        self.btn_open = QtWidgets.QPushButton("Ouvrir fichiers .txt")
        self.btn_open.clicked.connect(self.open_files)
        layout.addWidget(self.btn_open)

        self.files = QtWidgets.QListWidget()
        self.files.setMaximumHeight(50)
        layout.addWidget(self.files)

        # --- Fenêtre temporelle ---
        layout.addWidget(self._build_time_group())

        # --- Sélecteurs de canaux ---
        self.sel_ai = ChannelSelector("Signaux analogiques (AI)")
        self.sel_ai.setMaximumHeight(250)
        self.sel_di = ChannelSelector("Signaux digitaux (DI)")
        layout.addWidget(self.sel_ai, 1)
        layout.addWidget(self.sel_di, 1)

        # --- Options d'affichage ---
        layout.addWidget(self._build_options_group())

        self.btn_ranges = QtWidgets.QPushButton("Réglage des axes Y")
        self.btn_ranges.clicked.connect(self._open_range_dialog)
        layout.addWidget(self.btn_ranges)

        # Label d'information (période chargée).
        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        layout.addStretch()

        # La barre latérale est encapsulée dans une zone défilante de largeur
        # fixe : son contenu peut dépasser la hauteur de l'écran.
        side_scroll = QtWidgets.QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setWidget(side)
        side_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        side_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        side_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        side_scroll.setFixedWidth(350)
        return side_scroll

    def _build_time_group(self) -> QtWidgets.QGroupBox:
        """Groupe « Fenêtre temporelle » : préréglages + dates de début/fin."""
        grp = QtWidgets.QGroupBox("Fenêtre temporelle")
        grid = QtWidgets.QGridLayout(grp)

        self.cmb_preset = QtWidgets.QComboBox()
        self.cmb_preset.addItems([
            "Tout",
            "Dernières 10 s",
            "Dernières 30 s",
            "Dernière 1 min",
            "Dernières 5 min",
            "Dernières 15 min",
            "Dernière 1 h",
            "Manuel",
        ])

        self.dt_start = QtWidgets.QDateTimeEdit()
        self.dt_end = QtWidgets.QDateTimeEdit()
        for w in (self.dt_start, self.dt_end):
            w.setDisplayFormat("dd/MM/yyyy HH:mm:ss")
            w.setCalendarPopup(True)

        self.btn_apply_time = QtWidgets.QPushButton("Appliquer")
        self.btn_full = QtWidgets.QPushButton("Toute la période")

        grid.addWidget(QtWidgets.QLabel("Préréglage :"), 0, 0)
        grid.addWidget(self.cmb_preset, 0, 1, 1, 2)
        grid.addWidget(QtWidgets.QLabel("Début :"), 1, 0)
        grid.addWidget(self.dt_start, 1, 1, 1, 2)
        grid.addWidget(QtWidgets.QLabel("Fin :"), 2, 0)
        grid.addWidget(self.dt_end, 2, 1, 1, 2)
        grid.addWidget(self.btn_apply_time, 3, 1)
        grid.addWidget(self.btn_full, 3, 2)
        return grp

    def _build_options_group(self) -> QtWidgets.QGroupBox:
        """Groupe « Options » : double curseur + navigation fronts 44P."""
        grp = QtWidgets.QGroupBox("Options d'affichage")
        layout = QtWidgets.QVBoxLayout(grp)

        self.chk_dual_cursor = QtWidgets.QCheckBox("Activer le double curseur (Mesure)")
        self.chk_dual_cursor.setToolTip("Affiche un second curseur pour mesurer les écarts (Delta)")
        self.chk_dual_cursor.toggled.connect(self._on_dual_cursor_toggled)
        layout.addWidget(self.chk_dual_cursor)

        # --- Navigation sur les fronts montants du signal 44P ---
        edge_grid = QtWidgets.QGridLayout()
        edge_grid.setSpacing(6)
        edge_grid.setContentsMargins(0, 0, 0, 0)

        self.chk_track_44p = QtWidgets.QCheckBox("Verrouiller sur montées 44P")
        self.chk_track_44p.setToolTip("Suit les fronts montants du signal 44P")
        self.chk_track_44p.toggled.connect(self._on_track_44p_toggled)
        self.chk_track_44p.setEnabled(False)  # activé après chargement si 44P présent
        edge_grid.addWidget(self.chk_track_44p, 0, 0, 1, 2)

        self.lbl_edge_info = QtWidgets.QLabel("-- / --")
        self.lbl_edge_info.setStyleSheet("font-size: 10px; color: #aaa;")
        edge_grid.addWidget(QtWidgets.QLabel("Montée:"), 1, 0, alignment=QtCore.Qt.AlignRight)
        edge_grid.addWidget(self.lbl_edge_info, 1, 1)

        self.btn_prev_edge = QtWidgets.QPushButton("◄")
        self.btn_prev_edge.setMaximumWidth(40)
        self.btn_prev_edge.setMinimumHeight(26)
        self.btn_prev_edge.clicked.connect(self._go_to_prev_edge)
        self.btn_prev_edge.setEnabled(False)

        self.btn_next_edge = QtWidgets.QPushButton("►")
        self.btn_next_edge.setMaximumWidth(40)
        self.btn_next_edge.setMinimumHeight(26)
        self.btn_next_edge.clicked.connect(self._go_to_next_edge)
        self.btn_next_edge.setEnabled(False)

        self.lbl_edge_timestamp = QtWidgets.QLabel("--:--:--")
        self.lbl_edge_timestamp.setStyleSheet(
            "font-size: 9px; color: #888; font-family: monospace;")

        edge_nav = QtWidgets.QHBoxLayout()
        edge_nav.setSpacing(6)
        edge_nav.setContentsMargins(0, 0, 0, 0)
        edge_nav.addWidget(self.btn_prev_edge)
        edge_nav.addWidget(self.lbl_edge_timestamp, 1, alignment=QtCore.Qt.AlignCenter)
        edge_nav.addWidget(self.btn_next_edge)
        edge_grid.addLayout(edge_nav, 2, 0, 1, 2)

        layout.addLayout(edge_grid)
        return grp

    def _build_tabs(self) -> QtWidgets.QTabWidget:
        """Zone centrale : onglets « Signaux » et « Superposition »."""
        tabs = QtWidgets.QTabWidget()

        self.cursor_pane_tab1 = CursorInfoPane()
        self.cursor_pane_tab2 = CursorInfoPane()

        # ----- Onglet 1 : graphiques séparés -----
        self.pane_separate = pp.PlotPane("Affichage séparé des canaux")

        tab_separate = QtWidgets.QWidget()
        layout_sep = QtWidgets.QVBoxLayout(tab_separate)
        layout_sep.setContentsMargins(0, 0, 0, 0)
        layout_sep.setSpacing(0)
        layout_sep.addWidget(self.cursor_pane_tab1)

        # Les graphiques empilés vivent dans une zone défilante verticale.
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setContentsMargins(0, 0, 0, 0)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.v_scrollbar = scroll.verticalScrollBar()
        self.v_scrollbar.rangeChanged.connect(self._toggle_footer_visibility)

        container = QtWidgets.QWidget()
        container_layout = QtWidgets.QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(10)
        container_layout.addWidget(self.pane_separate)
        scroll.setWidget(container)
        layout_sep.addWidget(scroll, 1)

        # Axe temporel « footer » fixe : affiché uniquement quand la liste de
        # graphiques dépasse l'écran (l'axe du bas est alors hors champ).
        self.footer_axis_widget = pg.PlotWidget()
        self.footer_axis_widget.getAxis("left").setWidth(80)
        self.footer_axis_widget.setFixedHeight(40)
        self.footer_axis_widget.setAxisItems({"bottom": pg.DateAxisItem(orientation="bottom")})
        self.footer_axis_widget.showAxis("left", False)
        self.footer_axis_widget.getViewBox().setMouseEnabled(x=True, y=False)
        self.footer_axis_widget.setContentsMargins(0, 0, 0, 0)
        self.footer_axis_widget.getAxis("bottom").setHeight(40)
        self.footer_axis_widget.setVisible(False)
        layout_sep.addWidget(self.footer_axis_widget)

        # Barre de navigation temporelle (active uniquement quand zoomé).
        self.h_scrollbar_sep = self._make_time_scrollbar()
        layout_sep.addWidget(self.h_scrollbar_sep)

        tabs.addTab(tab_separate, "Signaux")

        # ----- Onglet 2 : superposition -----
        self.pane_summary = pp.PlotPane("Superposition de tous les canaux")

        tab_summary = QtWidgets.QWidget()
        layout_sum = QtWidgets.QVBoxLayout(tab_summary)
        layout_sum.setContentsMargins(0, 0, 0, 0)
        layout_sum.setSpacing(0)
        layout_sum.addWidget(self.cursor_pane_tab2)

        scroll_summary = QtWidgets.QScrollArea()
        scroll_summary.setWidgetResizable(True)
        scroll_summary.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll_summary.setContentsMargins(0, 0, 0, 0)
        scroll_summary.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)

        container_summary = QtWidgets.QWidget()
        sum_layout = QtWidgets.QVBoxLayout(container_summary)
        sum_layout.setContentsMargins(0, 0, 0, 0)
        sum_layout.setSpacing(10)
        sum_layout.addWidget(self.pane_summary)
        scroll_summary.setWidget(container_summary)
        layout_sum.addWidget(scroll_summary, 1)

        self.h_scrollbar_sum = self._make_time_scrollbar()
        layout_sum.addWidget(self.h_scrollbar_sum)

        tabs.addTab(tab_summary, "Superposition")

        # ----- Onglet 3 : visualisation temps réel -----
        # Autonome : lit le fichier en cours d'écriture sur l'automate (SFTP) et
        # le rafraîchit en continu. Indépendant des fichiers chargés en local.
        self.live_view = LiveView()
        tabs.addTab(self.live_view, "Temps réel ▶")

        return tabs

    def closeEvent(self, event):
        """Arrête proprement le thread temps réel avant de fermer la fenêtre."""
        try:
            self.live_view.stop_live()
        except Exception:
            pass
        super().closeEvent(event)

    @staticmethod
    def _make_time_scrollbar() -> QtWidgets.QScrollBar:
        """
        Crée la barre de navigation temporelle (désactivée tant que toute la
        période est visible). Style dédié, volontairement TRÈS visible : la
        poignée bleue indique où l'on se trouve dans la période totale et sa
        largeur représente la fraction visible.
        """
        sb = QtWidgets.QScrollBar(QtCore.Qt.Horizontal)
        sb.setMinimum(0)
        sb.setMaximum(0)
        sb.setEnabled(False)
        sb.setToolTip("Navigation temporelle : actif quand la vue est zoomée.\n"
                      "La poignée montre la position dans la période totale.")
        sb.setFixedHeight(18)
        # Stylesheet posé sur le widget : prioritaire sur le thème global, et
        # la hauteur CSS correspond exactement au setFixedHeight (un écart
        # entre les deux fausse la géométrie de la poignée).
        sb.setStyleSheet("""
            QScrollBar:horizontal {
                border: 1px solid #3b3f45;
                background: #1a1d20;
                height: 18px;
                border-radius: 4px;
                margin: 0px;
            }
            QScrollBar::handle:horizontal {
                background: #0078d7;
                min-width: 14px;
                border-radius: 3px;
                margin: 2px;
            }
            QScrollBar::handle:horizontal:hover { background: #2b95ff; }
            QScrollBar::handle:horizontal:pressed { background: #54a8ff; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: transparent;
            }
            QScrollBar:horizontal:disabled {
                border: 1px solid #2b2f33;
                background: #1a1d20;
            }
            QScrollBar::handle:horizontal:disabled { background: #3a3f44; }
        """)
        return sb

    def _init_state(self):
        """Initialise les attributs d'état (données, caches, timers)."""
        self.df = pd.DataFrame()        # dataset complet (index Date)
        self.view_df = pd.DataFrame()   # fenêtre temporelle courante
        self.ai_all: List[str] = []     # colonnes AI disponibles
        self.di_all: List[str] = []     # colonnes DI disponibles
        self.c1_data = None             # (timestamp, ligne) sous le curseur C1
        self.c2_data = None             # idem pour C2
        self.color_map = {}             # couleur de chaque canal
        self.custom_ranges = {}         # plages Y définies via RangeDialog

        # Dernière sélection connue : évite un re-tracé si rien n'a changé.
        self._last_ai_sel = []
        self._last_di_sel = []

        # Positions des curseurs, ré-appliquées après un changement de sélection.
        self._last_cursor_x1 = None
        self._last_cursor_x2 = None

        # Navigation sur les fronts montants 44P.
        self._current_edges: List[pd.Timestamp] = []
        self._current_edge_index = -1
        self.rising_edge_cache = sa.RisingEdgeCache()
        self._di44p_col = None          # nom réel de la colonne 44P (détecté au chargement)

        # Debounce zoom : un seul re-downsampling 50 ms après le dernier
        # événement, pour éviter la boucle de signaux sur les plots liés en X.
        self._zoom_timer = QtCore.QTimer()
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(50)
        self._zoom_timer.timeout.connect(self._do_deferred_zoom)
        self._zoom_in_progress = False

        # Debounce sélection : évite un re-tracé par case cochée.
        self._sel_timer = QtCore.QTimer()
        self._sel_timer.setSingleShot(True)
        self._sel_timer.setInterval(150)
        self._sel_timer.timeout.connect(self._do_selection_changed)

    def _connect_signals(self):
        """Câble les signaux entre composants (curseurs, zoom, sélection…)."""
        # Curseurs et plage X synchronisés entre les deux onglets.
        self.pane_separate.requestSyncX.connect(
            lambda x1, x2: self.pane_summary.sync_from_external(x1, x2))
        self.pane_summary.requestSyncX.connect(
            lambda x1, x2: self.pane_separate.sync_from_external(x1, x2))

        # Mise à jour du bandeau d'info quand un curseur bouge.
        self.pane_separate.requestSyncX.connect(self._sync_cursor)
        self.pane_summary.requestSyncX.connect(self._sync_cursor)

        # Zoom/pan → re-downsampling différé.
        self.pane_separate.viewChanged.connect(lambda _obj: self._on_view_zoom())
        self.pane_summary.viewChanged.connect(lambda _obj: self._on_view_zoom())

        # Menu contextuel « Couleur » des graphiques.
        self.pane_separate.requestColorChange.connect(self._change_channel_color)

        # Sélection de canaux et fenêtre temporelle.
        self.sel_ai.changed.connect(self._on_selection_changed)
        self.sel_di.changed.connect(self._on_selection_changed)
        self.cmb_preset.currentIndexChanged.connect(self._on_preset_changed)
        self.btn_apply_time.clicked.connect(self._apply_time_controls)
        self.btn_full.clicked.connect(self._set_full_range)

        # Barres de défilement temporelles.
        # On branche actionTriggered (et NON valueChanged) : ce signal n'est
        # émis QUE par une action de l'utilisateur (glisser le curseur, cliquer
        # la piste ou les flèches), jamais par un setValue programmatique. La
        # boucle de rétroaction barre↔vue est donc impossible — aucun verrou.
        self.h_scrollbar_sep.actionTriggered.connect(
            lambda _a: self._on_scrollbar_action(self.pane_separate, self.h_scrollbar_sep))
        self.h_scrollbar_sum.actionTriggered.connect(
            lambda _a: self._on_scrollbar_action(self.pane_summary, self.h_scrollbar_sum))

        # Onglet « Temps réel » : masquer la barre latérale (inutile ici, elle
        # ne sert qu'aux fichiers chargés en local) pour maximiser la place
        # disponible pour le graphe.
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _on_tab_changed(self, index: int):
        """Masque la barre latérale sur l'onglet temps réel, la rétablit ailleurs."""
        is_live = self.tabs.widget(index) is self.live_view
        self.sidebar.setVisible(not is_live)

    # ==================================================================
    # CHARGEMENT DES FICHIERS
    # ==================================================================

    def open_files(self):
        """Ouvre le sélecteur de fichiers puis lance le chargement en arrière-plan."""
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Sélectionner fichiers .txt", "", "Text (*.txt)"
        )
        if not paths:
            return

        self.files.clear()
        for p in paths:
            self.files.addItem(p)

        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.btn_open.setEnabled(False)

        # Le worker garde une référence sur self.load_worker pour ne pas être
        # détruit par le garbage collector pendant son exécution.
        self.load_worker = FileLoadWorker(paths)
        self.load_worker.progress.connect(self.progress_bar.setValue)
        self.load_worker.finished.connect(self._on_files_loaded)
        self.load_worker.needRewindChoice.connect(self._on_need_rewind_choice)
        self.load_worker.warning.connect(self._on_load_warning)
        self.load_worker.error.connect(self._on_load_error)
        self.load_worker.start()

    def _on_load_warning(self, message: str):
        """Avertissement non bloquant pendant le chargement (heure changée…)."""
        QtWidgets.QMessageBox.warning(self, "Heure changée sur l'automate", message)

    def _on_need_rewind_choice(self, affected: list):
        """
        Un changement d'heure (recul) a été détecté : demander à l'utilisateur,
        pour chaque fichier concerné, quel tronçon conserver. Exécuté dans le
        thread UI ; le worker attend la réponse via ``provide_choices``.
        """
        dlg = ClockRewindDialog(affected, parent=self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.load_worker.provide_choices(dlg.get_choices())
        else:
            # Annulation : le worker s'arrête, on réinitialise l'interface.
            self.load_worker.provide_choices(None)
            self.progress_bar.setVisible(False)
            self.btn_open.setEnabled(True)

    def generate_ultra_distinct_colors(self, keys):
        """
        Génère des couleurs extrêmement distinctes (perceptuelles) en utilisant HCL.
        Conversion finale en QColor compatible PyQtGraph.
        """
        if not isinstance(keys, (list, tuple)):
            raise ValueError("keys doit être une liste ou un tuple.")

        n = len(keys)
        if n == 0:
            return {}

        colors = {}

        # Réglages perceptuels
        L = 75              # Luminosité stable (70–80 excellent pour contrastes)
        C = 55              # Chroma (50–65 très bon pour couleurs vives)
        
        for i, key in enumerate(keys):
            # Teinte espacée autant que possible (0–360°)
            H = (360 * i / n) % 360

            # Convert HCL → RGB → QColor
            rgb = self.hcl_to_rgb(H, C, L)
            qcolor = QtGui.QColor(int(rgb[0]), int(rgb[1]), int(rgb[2]))
            colors[key] = qcolor

        return colors


    def hcl_to_rgb(self, H, C, L):
        """
        Conversion HCL → RGB (0–255).
        Implémentation sûre, adaptée pour Python/PyQt.
        """

        # Convert HCL to radians
        H_rad = math.radians(H)

        # HCL (CIELUV) → LUV
        u = math.cos(H_rad) * C
        v = math.sin(H_rad) * C

        # LUV → XYZ
        Y = (L + 16) / 116
        Y = Y ** 3 if Y > 0.206893 else (Y - 16/116) / 7.787
        ref_X, ref_Y, ref_Z = 95.047, 100.000, 108.883
        X = ref_X * (Y * 9 / (4 * u + 15 * Y + 3 * v))
        Z = ref_Z * (Y * 3 / (4 * u + 15 * Y + 3 * v))
        
        # XYZ → sRGB
        R =  3.2406*X/100 - 1.5372*Y + -0.4986*Z/100
        G = -0.9689*X/100 + 1.8758*Y +  0.0415*Z/100
        B =  0.0557*X/100 + -0.2040*Y + 1.0570*Z/100

        # Gamma correction
        def gamma(c):
            if c <= 0.0031308:
                return 12.92 * c
            return 1.055 * (c ** (1/2.4)) - 0.055

        R = max(0, min(255, int(gamma(R) * 255)))
        G = max(0, min(255, int(gamma(G) * 255)))
        B = max(0, min(255, int(gamma(B) * 255)))

        return R, G, B

    def _on_files_loaded(self, df: pd.DataFrame, ai_all: List[str], di_all: List[str]):
        """Reçoit les données préparées par le worker et initialise l'affichage."""
        try:
            self._reset_state()
            self.df = df
            self.ai_all = ai_all
            self.di_all = di_all
            # Couleur stable par signal, partagée par tous les panes
            # (Signaux, Superposition, Temps réel) — voir ui/palette.py.
            self.color_map = palette.color_map_for(self.ai_all + self.di_all)

            start = self.df.index.min()
            end = self.df.index.max()
            self.info.setText(
                f"<b>Période :</b> {start.strftime('%d/%m/%Y %H:%M:%S')}<br>"
                f"<b>→</b> {end.strftime('%d/%m/%Y %H:%M:%S')}"
            )

            self.sel_ai.set_channels(self.ai_all)
            self.sel_di.set_channels(self.di_all)
            self._init_time_controls(start, end)

            # Détection souple du signal 44P (insensible aux espaces / à la casse).
            self._di44p_col = next(
                (c for c in di_all if "44P" in c.upper().replace(" ", "")),
                None,
            )
            if self._di44p_col:
                self.chk_track_44p.setEnabled(True)
                self.chk_track_44p.setToolTip(
                    f"Suit les fronts montants du signal {self._di44p_col}")
            else:
                self.chk_track_44p.setEnabled(False)
                self.chk_track_44p.setChecked(False)
                di_names = ", ".join(di_all) if di_all else "aucun"
                self.chk_track_44p.setToolTip(
                    f"Signal 44P introuvable. Colonnes DI disponibles : {di_names}")

            self._set_full_range()

            self.progress_bar.setVisible(False)
            self.btn_open.setEnabled(True)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Erreur", str(e))
            self.btn_open.setEnabled(True)

    def _on_load_error(self, error_msg: str):
        QtWidgets.QMessageBox.critical(self, "Erreur", error_msg)
        self.progress_bar.setVisible(False)
        self.btn_open.setEnabled(True)

    def _reset_state(self):
        """Réinitialise les données et caches avant un nouveau chargement."""
        self.df = pd.DataFrame()
        self.view_df = pd.DataFrame()
        self.ai_all = []
        self.di_all = []
        self.c1_data = None
        self.c2_data = None

        self.rising_edge_cache.invalidate()
        self._current_edges = []
        self._current_edge_index = -1
        self.chk_track_44p.setChecked(False)

        self.pane_separate.clear()

    # ==================================================================
    # FENÊTRE TEMPORELLE
    # ==================================================================

    def _init_time_controls(self, start: pd.Timestamp, end: pd.Timestamp):
        """Cale les sélecteurs de dates sur la période réellement chargée."""
        qstart, qend = pd.Timestamp(start), pd.Timestamp(end)
        for w in (self.dt_start, self.dt_end):
            w.setMinimumDateTime(qstart)
            w.setMaximumDateTime(qend)

        self.dt_start.setDateTime(qstart)
        self.dt_end.setDateTime(qend)
        self.cmb_preset.setCurrentText("Tout")
        self.pane_separate.saved_x_range = None

    def _on_preset_changed(self):
        """Préréglage choisi → met à jour les champs début/fin (sans appliquer)."""
        if self.df.empty:
            return
        choice = self.cmb_preset.currentText()
        start_all, end_all = self.df.index.min(), self.df.index.max()

        if choice == "Tout":
            self.dt_start.setDateTime(start_all.to_pydatetime())
            self.dt_end.setDateTime(end_all.to_pydatetime())
        elif choice.startswith("Dernière"):
            durations = {
                "10 s": pd.Timedelta(seconds=10),
                "30 s": pd.Timedelta(seconds=30),
                "1 min": pd.Timedelta(minutes=1),
                "5 min": pd.Timedelta(minutes=5),
                "15 min": pd.Timedelta(minutes=15),
                "1 h": pd.Timedelta(hours=1),
            }
            delta = next((d for key, d in durations.items() if key in choice),
                         end_all - start_all)
            start = max(start_all, end_all - delta)
            self.dt_start.setDateTime(start.to_pydatetime())
            self.dt_end.setDateTime(end_all.to_pydatetime())
        # « Manuel » : ne rien faire, l'utilisateur saisit lui-même.

    def _apply_time_controls(self):
        """Bouton « Appliquer » : applique la fenêtre saisie dans les champs."""
        if self.df.empty:
            return
        start = self.dt_start.dateTime().toPython()
        end = self.dt_end.dateTime().toPython()
        if end < start:
            start, end = end, start  # tolérance : on permute si inversé
        self._apply_time_window(start, end)

    def _set_full_range(self):
        """Bouton « Toute la période » : affiche l'intégralité des données."""
        if self.df.empty:
            return
        self._apply_time_window(self.df.index.min(), self.df.index.max())

    def _apply_time_window(self, start: pd.Timestamp, end: pd.Timestamp):
        """
        Applique une fenêtre temporelle : construit les graphiques au premier
        appel, puis se contente d'un zoom/pan (pas de re-tracé) ensuite.
        """
        view = self.df.loc[start:end]
        if view.empty:
            QtWidgets.QMessageBox.warning(
                self, "Fenêtre vide", "Aucune donnée dans l'intervalle choisi.")
            return
        self.view_df = view

        self.dt_start.setDateTime(pd.Timestamp(start).to_pydatetime())
        self.dt_end.setDateTime(pd.Timestamp(end).to_pydatetime())

        # Premier affichage (ou après reset) : construire les graphiques.
        if self.pane_separate.master is None:
            self._replot_selected()

        # Zoomer sur la fenêtre demandée — simple pan/zoom, pas de re-tracé.
        x0 = time_utils.timestamp_to_axis_seconds(view.index[0])
        x1 = time_utils.timestamp_to_axis_seconds(view.index[-1])
        self.pane_separate.set_x_limits(x0, x1)

    # ==================================================================
    # TRACÉ
    # ==================================================================

    def _replot_selected(self):
        """(Re)construit tous les graphiques à partir de la sélection courante."""
        if self.df.empty:
            return

        # Annuler tout zoom différé en attente : sa référence de vue serait
        # périmée après le re-tracé.
        self._zoom_timer.stop()

        ai_sel = self.sel_ai.get_selected("AI")
        di_sel = self.sel_di.get_selected("DI")

        # Hauteur minimale proportionnelle au nombre de graphiques, pour que
        # la zone défilante verticale prenne le relais au-delà de l'écran.
        nb_plots = len(ai_sel) + len(di_sel)
        self.pane_separate.setMinimumHeight(nb_plots * MIN_PLOT_HEIGHT)

        # Plages Y partagées : les canaux d'une même famille (ex. Imot1/Imot2)
        # reçoivent la même échelle pour être comparables visuellement.
        shared = self._compute_shared_y_ranges(ai_sel)
        ai_meta = [pp.ChannelMeta(c, False, self.color_map.get(c),
                                  self.custom_ranges.get(c) or shared.get(c))
                   for c in ai_sel]
        di_meta = [pp.ChannelMeta(c, True, self.color_map.get(c)) for c in di_sel]

        # Premier tracé : positionner le curseur au début des données.
        if not self.pane_separate.vlines:
            self._sync_cursor(x1=time_utils.timestamp_to_axis_seconds(self.df.index[0]))

        # Toujours tracer depuis le dataset complet (downsamplé) : la fenêtre
        # temporelle n'est qu'un zoom, pas un filtre.
        self.pane_separate.add_timeseries(self.df, ai_meta + di_meta)
        self.pane_summary.add_overlay_plot(self.df, ai_meta + di_meta,
                                           v_lines_source=self.pane_separate.vlines)

        # Synchronisation des axes X : l'onglet « Signaux » est la référence,
        # la superposition et l'axe footer le suivent.
        view_sep = self.pane_separate.master
        self.pane_summary.master.setXLink(view_sep)
        self.footer_axis_widget.getPlotItem().setXLink(view_sep)

        # Le footer est interactif lui aussi : il doit être borné comme les
        # autres graphiques pour ne pas pouvoir les désynchroniser.
        x_data_min, x_data_max = self._full_x_bounds()
        self.footer_axis_widget.getPlotItem().getViewBox().setLimits(
            xMin=x_data_min, xMax=x_data_max)

        # Initialiser les barres de défilement d'après la vue courante.
        self._sync_time_scrollbar(self.pane_separate, self.h_scrollbar_sep)
        self._sync_time_scrollbar(self.pane_summary, self.h_scrollbar_sum)

    def _compute_shared_y_ranges(self, channel_names: List[str]) -> dict:
        """
        Groupe les canaux dont le nom de base est identique (ex. « Imot » pour
        Imot1/Imot2) et calcule une plage Y commune au groupe. Les canaux ayant
        déjà une plage personnalisée (RangeDialog) ne sont pas touchés.
        """
        groups: dict = defaultdict(list)
        for c in channel_names:
            # Nom de base = lettres uniquement, sans préfixe AI/DI ni chiffres.
            family = re.sub(r"[^A-Za-z]", "", c.replace("AI", "").replace("DI", ""))
            groups[family].append(c)

        shared: dict = {}
        for _family, cols in groups.items():
            if len(cols) < 2:
                continue
            y_min, y_max = float("inf"), float("-inf")
            for c in cols:
                if c in self.custom_ranges or c not in self.df.columns:
                    continue
                vals = self.df[c].dropna()
                if len(vals):
                    y_min = min(y_min, float(vals.min()))
                    y_max = max(y_max, float(vals.max()))
            if y_min < y_max:
                margin = max(1.0, (y_max - y_min) * 0.05)
                for c in cols:
                    if c not in self.custom_ranges:
                        shared[c] = (y_min - margin, y_max + margin)
        return shared

    # ==================================================================
    # ZOOM ET DOWNSAMPLING ADAPTATIF
    # ==================================================================

    def _on_view_zoom(self):
        """
        Tout changement de vue (zoom, pan, molette, défilement) passe par ici,
        via le signal stable ``PlotPane.viewChanged``.

        1. Met à jour les barres de défilement IMMÉDIATEMENT (suivi temps réel
           de la taille et de la position du curseur). C'est volontairement
           branché sur ``viewChanged`` plutôt que sur le ``sigXRangeChanged``
           d'un ViewBox : ce dernier est recréé à chaque re-tracé et la
           connexion s'y perdait. ``viewChanged`` appartient au PlotPane, objet
           stable, donc la barre reste toujours synchronisée.
        2. Planifie le re-downsampling adaptatif (throttle 50 ms) : si le timer
           court déjà, on ne le réarme pas, donc les courbes se rafraîchissent
           pendant un défilement continu au lieu d'attendre l'arrêt du geste.
        """
        self._sync_time_scrollbar(self.pane_separate, self.h_scrollbar_sep)
        self._sync_time_scrollbar(self.pane_summary, self.h_scrollbar_sum)

        if self._zoom_in_progress:
            return
        if not self._zoom_timer.isActive():
            self._zoom_timer.start()

    def _do_deferred_zoom(self):
        """
        Ré-échantillonne chaque courbe sur la plage X visible, élargie d'un
        écran de chaque côté (bornée à la période des données) : les zones
        voisines sont déjà tracées quand l'utilisateur fait défiler la vue, les
        signaux restent donc visibles pendant le scroll. Plus on zoome, plus la
        résolution affichée augmente — le tout sans reconstruire les graphiques.
        """
        if self.df.empty:
            return
        self._zoom_in_progress = True
        try:
            x_data_min, x_data_max = self._full_x_bounds()
            for pane in (self.pane_separate, self.pane_summary):
                if not pane.plots or pane.master is None:
                    continue
                view_range = pane.master.getViewBox().viewRange()[0]
                # Marge d'un écran de part et d'autre, bornée aux données :
                # aucun point ne doit être créé en dehors de la période réelle.
                span = view_range[1] - view_range[0]
                x_min = max(view_range[0] - span, x_data_min)
                x_max = min(view_range[1] + span, x_data_max)
                if x_max <= x_min:
                    continue
                for plt in pane.plots:
                    for item in plt.listDataItems():
                        col_name = getattr(item, "column_id", None)
                        if not col_name or col_name not in self.df.columns:
                            continue
                        x_ds, y_ds = pane.get_downsampled_data(
                            df=self.df,
                            column=col_name,
                            x_min=x_min,
                            x_max=x_max,
                            is_digital=col_name.startswith("DI"),
                            # 3 écrans chargés → 3× le budget de points pour
                            # conserver la même densité dans la zone visible.
                            target_pts=3 * pp.DEFAULT_TARGET_POINTS,
                        )
                        if len(x_ds) > 0:
                            item.setData(x_ds, y_ds, connect="finite", stepMode=False)
        except Exception:
            pass  # un échec de downsampling ne doit jamais faire planter l'UI
        finally:
            self._zoom_in_progress = False

    # ==================================================================
    # BARRE DE DÉFILEMENT TEMPORELLE
    # ==================================================================
    #
    # La barre représente TOUTE la période chargée [t_start, t_end] :
    #   - taille du curseur  = fraction de la période actuellement visible ;
    #   - position du curseur = décalage de la fenêtre visible dans la période.
    # Quand tout est visible (pas de zoom), la barre est désactivée.
    #
    # Deux sens, sans boucle de rétroaction :
    #   vue  → barre : _sync_time_scrollbar, appelé sur chaque changement de vue
    #                  (_on_view_zoom). Utilise setValue/setPageStep, qui
    #                  n'émettent JAMAIS actionTriggered.
    #   barre → vue  : _on_scrollbar_action, branché sur actionTriggered, signal
    #                  émis UNIQUEMENT par l'utilisateur (glisser, clic piste /
    #                  flèche). setValue ne le déclenche pas → aucune boucle,
    #                  aucun verrou nécessaire.

    def _full_x_bounds(self):
        """Bornes X (secondes) de TOUTE la période chargée."""
        return (
            time_utils.timestamp_to_axis_seconds(self.df.index[0]),
            time_utils.timestamp_to_axis_seconds(self.df.index[-1]),
        )

    def _sync_time_scrollbar(self, pane, scrollbar):
        """Vue → barre : ajuste taille et position du curseur d'après la vue."""
        if pane.master is None or self.df.empty:
            scrollbar.setEnabled(False)
            return

        x_data_min, x_data_max = self._full_x_bounds()
        data_span = x_data_max - x_data_min
        view_range = pane.master.getViewBox().viewRange()[0]
        view_span = view_range[1] - view_range[0]
        if data_span <= 0 or view_span <= 0:
            scrollbar.setEnabled(False)
            return

        ratio = view_span / data_span

        # blockSignals : ces appels ne doivent émettre aucun signal.
        scrollbar.blockSignals(True)
        try:
            if ratio >= 0.999:
                # Toute la période tient dans la vue → rien à faire défiler.
                scrollbar.setRange(0, 0)
                scrollbar.setEnabled(False)
                return

            page = max(1, int(ratio * SCROLLBAR_RESOLUTION))
            scrollbar.setRange(0, SCROLLBAR_RESOLUTION - page)
            scrollbar.setPageStep(page)
            scrollbar.setSingleStep(max(1, page // 4))
            scrollbar.setEnabled(True)

            # Ne pas repositionner le curseur pendant que l'utilisateur le
            # tient (sinon il « saute » sous la souris). Sa taille, elle, peut
            # toujours être mise à jour ci-dessus.
            if not scrollbar.isSliderDown():
                pos = (view_range[0] - x_data_min) / data_span * SCROLLBAR_RESOLUTION
                pos = int(round(max(0, min(pos, SCROLLBAR_RESOLUTION - page))))
                scrollbar.setValue(pos)
        finally:
            scrollbar.blockSignals(False)

    def _on_scrollbar_action(self, pane, scrollbar):
        """Barre → vue : déplace la fenêtre visible selon la position demandée."""
        if pane.master is None or self.df.empty:
            return
        x_data_min, x_data_max = self._full_x_bounds()
        data_span = x_data_max - x_data_min
        if data_span <= 0:
            return

        view_range = pane.master.getViewBox().viewRange()[0]
        view_span = view_range[1] - view_range[0]

        # sliderPosition() = valeur cible APRÈS l'action de l'utilisateur
        # (déjà mise à jour quand actionTriggered est émis).
        value = scrollbar.sliderPosition()
        new_x_min = x_data_min + (value / SCROLLBAR_RESOLUTION) * data_span
        new_x_max = new_x_min + view_span
        if new_x_max > x_data_max:
            new_x_max = x_data_max
            new_x_min = max(x_data_min, new_x_max - view_span)
        pane.master.setXRange(new_x_min, new_x_max, padding=0)

    # ==================================================================
    # SÉLECTION DE CANAUX
    # ==================================================================

    def _on_selection_changed(self):
        """Sélection modifiée → planifie un re-tracé différé (debounce 150 ms)."""
        self._sel_timer.start()

    def _do_selection_changed(self):
        """Re-trace uniquement si la sélection a réellement changé."""
        ai_sel = self.sel_ai.get_selected("AI")
        di_sel = self.sel_di.get_selected("DI")
        if ai_sel != self._last_ai_sel or di_sel != self._last_di_sel:
            self._last_ai_sel = ai_sel
            self._last_di_sel = di_sel
            self._replot_selected()
            # Ré-afficher les valeurs sous les curseurs avec la nouvelle sélection.
            if self._last_cursor_x1 is not None:
                self._sync_cursor(self._last_cursor_x1, self._last_cursor_x2)

    # ==================================================================
    # CURSEURS ET BANDEAU D'INFORMATION
    # ==================================================================

    def _on_dual_cursor_toggled(self, enabled: bool):
        """Active/désactive le second curseur (C2) et l'affichage des deltas."""
        self.cursor_pane_tab1.activate_dual_mode(enabled)
        self.cursor_pane_tab2.activate_dual_mode(enabled)

        self.pane_separate.toggle_second_cursor(enabled)
        self.pane_summary.toggle_second_cursor(enabled)

        if not enabled:
            self.c2_data = None

    def _sync_cursor(self, x1: float, x2: Optional[float] = None):
        """
        Met à jour le bandeau d'information à partir des positions de curseurs.
        Optimisé pour être appelé en continu pendant le drag d'un curseur.
        """
        if self.df.empty:
            return

        self._last_cursor_x1 = x1
        self._last_cursor_x2 = x2

        # Cache de l'index : évite de le re-créer à chaque mouvement de souris.
        if not hasattr(self, "_idx_cache") or id(self.df) != self._idx_cache_id:
            self._idx_cache = self.df.index
            self._idx_cache_id = id(self.df)

        def get_data_at(x_sec):
            """Ligne de données la plus proche de l'abscisse x_sec."""
            ts_point = time_utils.axis_seconds_to_timestamp(x_sec)
            idx = self._idx_cache.get_indexer([ts_point], method="nearest")[0]
            idx = min(max(0, idx), len(self.df) - 1)
            return self._idx_cache[idx], self.df.iloc[idx]

        def build_html_row(label, timestamp, data_str):
            """Une ligne du bandeau : étiquette + horodatage + valeurs."""
            ts_formatted = timestamp.strftime("%d-%m-%Y %H:%M:%S.%f")[:-4]
            return f"""
            <table border="0" style="border-collapse: collapse;">
                <tr>
                    <td style="vertical-align: top; white-space: nowrap; padding-right: 10px;">
                        <b>{label}</b> [{ts_formatted}] :
                    </td>
                    <td style="font-family: monospace;">{data_str}</td>
                </tr>
            </table>
            """

        # --- Curseur C1 ---
        ts1, row1 = get_data_at(x1)
        self.c1_data = (ts1, row1)
        txt_c1 = build_html_row("C1", ts1, self._format_row_compact(row1))

        # --- Curseur C2 + deltas (si mode double curseur actif) ---
        dual_enabled = self.chk_dual_cursor.isChecked()
        txt_c2 = "<b>C2 :</b> —"
        txt_delta = "<b>Δ (C2 - C1) :</b> —"
        self.c2_data = None

        if dual_enabled and x2 is not None:
            ts2, row2 = get_data_at(x2)
            self.c2_data = (ts2, row2)
            txt_c2 = build_html_row("C2", ts2, self._format_row_compact(row2))

            dt = (ts2 - ts1).total_seconds()
            deltas = [f"Δt: {dt:+.3f}s"]
            for c in self.sel_ai.get_selected("AI"):
                if c in row1 and c in row2:
                    diff = float(row2[c]) - float(row1[c])
                    display_name = c.replace("AI", "").replace("DI", "")
                    deltas.append(f"Δ{display_name}: {diff:+.4g}")
            txt_delta = "<b>Δ :</b> " + " | ".join(deltas)

        for pane in (self.cursor_pane_tab1, self.cursor_pane_tab2):
            pane.update_info(txt_c1, txt_c2, txt_delta, dual_enabled)

    def _format_row_compact(self, row) -> str:
        """Formate les valeurs des canaux sélectionnés : « CS:1.25 | 44P:1 | … »."""
        active_cols = self.sel_ai.get_selected("AI") + self.sel_di.get_selected("DI")
        parts = []
        for c in active_cols:
            if c in row:
                val = row[c]
                fmt = f"{val}" if c.startswith("DI") else f"{val:.4g}"
                display_name = c.replace("AI", "").replace("DI", "")
                parts.append(f"{display_name}:{fmt}")
        return " | ".join(parts)

    # ==================================================================
    # NAVIGATION SUR LES FRONTS MONTANTS 44P
    # ==================================================================

    def _on_track_44p_toggled(self, checked: bool):
        """Case « Verrouiller sur montées 44P » cochée/décochée."""
        if checked:
            self._detect_44p_edges()
            if self._current_edges:
                self._current_edge_index = 0
                self._apply_edge_zoom_and_sync()
        else:
            self._current_edges = []
            self._current_edge_index = -1
            self.btn_prev_edge.setEnabled(False)
            self.btn_next_edge.setEnabled(False)
            self.lbl_edge_info.setText("-- / --")
            self.lbl_edge_timestamp.setText("--:--:--")

    def _detect_44p_edges(self):
        """Détecte (ou récupère du cache) les fronts montants du signal 44P."""
        col = self._di44p_col
        if self.df.empty or not col or col not in self.df.columns:
            di_names = ", ".join(self.di_all) if self.di_all else "aucun"
            QtWidgets.QMessageBox.warning(
                self, "Erreur",
                f"Signal 44P introuvable dans les données.\n"
                f"Colonnes DI disponibles : {di_names}")
            self.chk_track_44p.setChecked(False)
            return

        edges = self.rising_edge_cache.get_edges(col, self.df)
        if edges is None:
            edges = sa.find_rising_edges_for_di(self.df, col)
            self.rising_edge_cache.store_edges(col, self.df, edges)

        self._current_edges = edges

        if len(edges) == 0:
            QtWidgets.QMessageBox.information(
                self, "Info", f"Aucune montée détectée sur le signal {col}.")
            self.chk_track_44p.setChecked(False)

    def _go_to_prev_edge(self):
        if not self._current_edges or self._current_edge_index <= 0:
            return
        self._current_edge_index -= 1
        self._apply_edge_zoom_and_sync()

    def _go_to_next_edge(self):
        if not self._current_edges or self._current_edge_index >= len(self._current_edges) - 1:
            return
        self._current_edge_index += 1
        self._apply_edge_zoom_and_sync()

    def _apply_edge_zoom_and_sync(self):
        """
        Centre la vue sur le front courant (fenêtre de 1 min, ±30 s),
        place le curseur C1 exactement sur le front et met à jour les labels.
        """
        if (not self._current_edges
                or self._current_edge_index < 0
                or self._current_edge_index >= len(self._current_edges)):
            return

        edge_timestamp = self._current_edges[self._current_edge_index]

        delta = pd.Timedelta(seconds=30)
        start = max(edge_timestamp - delta, self.df.index.min())
        end = min(edge_timestamp + delta, self.df.index.max())
        self._apply_time_window(start, end)

        # Curseur C1 exactement sur le front montant, dans les deux onglets.
        x_edge = time_utils.timestamp_to_axis_seconds(edge_timestamp)
        self.pane_separate._broadcast_fast(x1=x_edge)
        self.pane_summary._broadcast_fast(x1=x_edge)
        self._sync_time_scrollbar(self.pane_separate, self.h_scrollbar_sep)
        self._sync_time_scrollbar(self.pane_summary, self.h_scrollbar_sum)
        self._update_edge_display_labels()

    def _update_edge_display_labels(self):
        """Met à jour le compteur « n / total », l'horodatage et les boutons ◄ ►."""
        if not self._current_edges or self._current_edge_index < 0:
            self.lbl_edge_info.setText("-- / --")
            self.lbl_edge_timestamp.setText("--:--:--")
            self.btn_prev_edge.setEnabled(False)
            self.btn_next_edge.setEnabled(False)
            return

        total = len(self._current_edges)
        edge_ts = self._current_edges[self._current_edge_index]

        self.lbl_edge_info.setText(f"{self._current_edge_index + 1} / {total}")
        self.lbl_edge_timestamp.setText(edge_ts.strftime("%H:%M:%S.%f")[:-3])

        self.btn_prev_edge.setEnabled(self._current_edge_index > 0)
        self.btn_next_edge.setEnabled(self._current_edge_index < total - 1)

    # ==================================================================
    # DIVERS
    # ==================================================================

    def _change_channel_color(self, channel: pp.ChannelMeta):
        """Menu contextuel « Couleur » d'un graphique → sélecteur de couleur."""
        current_color = channel.color or QtGui.QColor(255, 255, 255)
        color = QtWidgets.QColorDialog.getColor(
            current_color, self, f"Couleur pour {channel.name}")
        if color.isValid():
            self.color_map[channel.name] = color
            channel.color = color
            self._replot_selected()

    def _toggle_footer_visibility(self):
        """Affiche l'axe temporel « footer » uniquement si un scroll vertical existe."""
        has_scroll = self.v_scrollbar.maximum() > 0
        self.footer_axis_widget.setVisible(has_scroll)

    def _open_range_dialog(self):
        """Bouton « Réglage des axes Y » → dialogue des plages personnalisées."""
        ai_sel = self.sel_ai.get_selected("AI")
        if not ai_sel:
            QtWidgets.QMessageBox.information(
                self, "Info", "Sélectionnez des signaux analogiques d'abord.")
            return

        dlg = RangeDialog(ai_sel, self.custom_ranges, self.df, self)
        if dlg.exec():
            self.custom_ranges = dlg.get_ranges()
            self._replot_selected()  # re-tracer pour appliquer les nouvelles plages
