"""
Onglet « Temps réel » du Wago Log Viewer.

Widget autonome : choix de la source (simulation / automate SFTP / fichier
local), sélection des canaux, et graphe à fenêtre glissante. Toute la logique
temps réel est confinée ici — la fenêtre principale se contente d'ajouter cet
onglet et d'appeler ``stop_live()`` à la fermeture.

Flux :
    LiveWorker (thread)  --newData(lignes brutes)-->  LiveView
        LiveView accumule un tampon glissant, applique la logique métier
        (AID3 → vitesse, ffill) puis pousse le tampon dans LivePlotPane.
"""

from typing import List, Optional

import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from ..data import live_source, log_processing, plc_config
from . import palette
from .channel_selector import ChannelSelector
from .live_plot_pane import LivePlotPane
from .live_worker import LiveWorker
from .plot_pane import ChannelMeta
from .save_logs_dialog import SaveLogsDialog

#: Hôte de l'automate Wago par défaut, issu de la configuration (variable
#: d'environnement WAGO_PLC_HOST ou fichier wago_plc.ini). Vide si non
#: configuré : l'utilisateur saisit alors l'adresse dans l'interface. Jamais
#: codé en dur dans le source (recommandation SonarQube).
DEFAULT_PLC_HOST = plc_config.host()


class LiveView(QtWidgets.QWidget):
    """Onglet de visualisation temps réel."""

    def __init__(self):
        super().__init__()
        self._worker: Optional[LiveWorker] = None
        self._raw_buf = pd.DataFrame()      # tampon brut (index Date)
        self._processed = pd.DataFrame()    # tampon après logique métier
        self._channels_built = False
        self.color_map = {}
        # Dernière valeur connue de chaque colonne JUSTE AVANT le tampon : permet
        # de propager l'état d'un signal digital qui n'a pas changé depuis plus
        # longtemps que la fenêtre (sinon sa valeur, sortie du tampon, serait
        # perdue et le signal s'afficherait à tort à 0).
        self._prefill: Optional[pd.Series] = None

        self._build_ui()
        self._update_buffer_seconds()

    # ==================================================================
    # CONSTRUCTION DE L'INTERFACE
    # ==================================================================

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        root.addWidget(self._build_control_bar())

        # Corps : sélecteurs de canaux à gauche, graphe glissant à droite.
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(8)

        selectors = QtWidgets.QVBoxLayout()
        self.sel_ai = ChannelSelector("Signaux analogiques (AI)")
        self.sel_di = ChannelSelector("Signaux digitaux (DI)")
        self.sel_ai.changed.connect(self._on_selection_changed)
        self.sel_di.changed.connect(self._on_selection_changed)
        selectors.addWidget(self.sel_ai)
        selectors.addWidget(self.sel_di)
        selectors.addStretch()
        sel_container = QtWidgets.QWidget()
        sel_container.setLayout(selectors)
        sel_container.setFixedWidth(240)
        body.addWidget(sel_container)

        # Graphe glissant dans une zone défilante verticale + axe temporel fixe
        # en bas (même disposition que l'onglet « Signaux »).
        self.live_pane = LivePlotPane(window_seconds=10.0)

        right = QtWidgets.QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        scroll = QtWidgets.QScrollArea()
        self._scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self._v_scrollbar = scroll.verticalScrollBar()
        #self._v_scrollbar.rangeChanged.connect(self._toggle_footer_visibility)

        # Le graphe est directement le widget défilant : avec widgetResizable,
        # la zone s'adapte en largeur tout en respectant la hauteur minimale du
        # graphe (nb_canaux × MIN_LIVE_PLOT_HEIGHT) → scroll vertical dès que la
        # pile dépasse la hauteur visible.
        scroll.setWidget(self.live_pane)
        right.addWidget(scroll, 1)

        # Axe temporel « footer » fixe : visible uniquement quand la pile de
        # graphiques dépasse l'écran (l'axe du bas est alors hors champ).
        #self.footer_axis_widget = pg.PlotWidget()
        #self.footer_axis_widget.setAxisItems(
        #    {"bottom": pg.DateAxisItem(orientation="bottom")})
        #self.footer_axis_widget.showAxis("left", False)
        #self.footer_axis_widget.setFixedHeight(40)
        #self.footer_axis_widget.getViewBox().setMouseEnabled(x=False, y=False)
        #self.footer_axis_widget.setVisible(False)
        #right.addWidget(self.footer_axis_widget)

        body.addLayout(right, 1)

        root.addLayout(body, 1)

    #def _toggle_footer_visibility(self):
    #    """Affiche l'axe temporel fixe seulement si un scroll vertical existe."""
    #    self.footer_axis_widget.setVisible(self._v_scrollbar.maximum() > 0)

    def _build_control_bar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QFrame()
        bar.setFrameShape(QtWidgets.QFrame.StyledPanel)
        layout = QtWidgets.QGridLayout(bar)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setHorizontalSpacing(10)

        # --- Source (fixée : automate Wago en SFTP) ---
        layout.addWidget(QtWidgets.QLabel("Source :"), 0, 0)
        self.lbl_source = QtWidgets.QLabel("Automate Wago (SFTP)")
        layout.addWidget(self.lbl_source, 0, 1)

        # --- Panneau de configuration SFTP ---
        self.config_ftp = self._build_ftp_config()
        layout.addWidget(self.config_ftp, 0, 2, 2, 1)

        # --- Cadence et fenêtre ---
        layout.addWidget(QtWidgets.QLabel("Rafraîchissement (ms) :"), 0, 3)
        self.spin_interval = QtWidgets.QSpinBox()
        self.spin_interval.setRange(50, 5000)
        self.spin_interval.setSingleStep(50)
        self.spin_interval.setValue(200)
        layout.addWidget(self.spin_interval, 0, 4)

        layout.addWidget(QtWidgets.QLabel("Fenêtre (s) :"), 1, 3)
        self.spin_window = QtWidgets.QSpinBox()
        self.spin_window.setRange(2, 120)
        self.spin_window.setValue(10)
        self.spin_window.valueChanged.connect(self._on_window_changed)
        layout.addWidget(self.spin_window, 1, 4)

        # --- Démarrer / Arrêter ---
        self.btn_start = QtWidgets.QPushButton("▶ Démarrer")
        self.btn_start.clicked.connect(self.start_live)
        self.btn_stop = QtWidgets.QPushButton("■ Arrêter")
        self.btn_stop.clicked.connect(self.stop_live)
        self.btn_stop.setEnabled(False)
        layout.addWidget(self.btn_start, 0, 5)
        layout.addWidget(self.btn_stop, 1, 5)

        # --- Enregistrer les fichiers de log de la carte mémoire ---
        self.btn_save = QtWidgets.QPushButton("Enregistrer fichiers")
        self.btn_save.setToolTip(
            "Parcourir les fichiers de log présents sur la carte mémoire de "
            "l'automate et en télécharger une sélection sur l'ordinateur.")
        self.btn_save.clicked.connect(self._open_save_dialog)
        layout.addWidget(self.btn_save, 2, 5, 2, 1)
    

        self.lbl_status = QtWidgets.QLabel("Prêt.")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status, 2, 0, 1, 7)

        layout.setColumnStretch(2, 1)
        return bar

    def _open_save_dialog(self):
        """Ouvre la boîte « Enregistrer les fichiers » sur la carte de l'automate."""
        host = self.ed_host.text().strip()
        if not host:
            self._set_status("Indiquez l'hôte de l'automate.", error=True)
            return
        repo = live_source.SftpLogRepository(
            host=host,
            user=self.ed_user.text().strip(),
            password=self.ed_pass.text())
        SaveLogsDialog(repo, parent=self).exec()


    def _build_ftp_config(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QGridLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setHorizontalSpacing(8)

        # Pré-remplissage depuis la configuration (env / wago_plc.ini) ; champs
        # vides si rien n'est configuré. Aucun identifiant en dur dans le source.
        self.ed_host = QtWidgets.QLineEdit(DEFAULT_PLC_HOST)
        self.ed_user = QtWidgets.QLineEdit(plc_config.user())
        self.ed_pass = QtWidgets.QLineEdit(plc_config.password())
        self.ed_pass.setEchoMode(QtWidgets.QLineEdit.Password)

        # Fichier du jour : date insérée automatiquement, bascule à minuit.
        self.chk_today = QtWidgets.QCheckBox("Fichier du jour (date automatique)")
        self.chk_today.setChecked(True)
        self.chk_today.setToolTip(
            "Suit « logs_<date> » et bascule automatiquement sur le fichier "
            "du jour suivant à minuit.")
        self.chk_today.toggled.connect(self._on_today_toggled)

        self.ed_path = QtWidgets.QLineEdit(live_source.plc_log_path())
        self.ed_path.setPlaceholderText("/media/6289-794F/stib/logs_AAAA-MM-JJ.txt")
        self.ed_path.setEnabled(False)  # piloté par la case « Fichier du jour »

        lay.addWidget(QtWidgets.QLabel("Hôte :"), 0, 0)
        lay.addWidget(self.ed_host, 0, 1)
        lay.addWidget(QtWidgets.QLabel("Utilisateur :"), 0, 2)
        lay.addWidget(self.ed_user, 0, 3)
        lay.addWidget(QtWidgets.QLabel("Mot de passe :"), 0, 4)
        lay.addWidget(self.ed_pass, 0, 5)
        lay.addWidget(self.chk_today, 1, 0, 1, 2)
        lay.addWidget(QtWidgets.QLabel("Fichier distant :"), 1, 2)
        lay.addWidget(self.ed_path, 1, 3, 1, 3)
        return w

    def _on_today_toggled(self, checked: bool):
        self.ed_path.setEnabled(not checked)
        if checked:
            self.ed_path.setText(live_source.plc_log_path())


    # ==================================================================
    # DÉMARRAGE / ARRÊT
    # ==================================================================

    def _make_source(self) -> live_source.LiveSource:
        """Construit la source SFTP à partir des champs de configuration."""
        host = self.ed_host.text().strip()
        user = self.ed_user.text().strip()
        password = self.ed_pass.text()
        if self.chk_today.isChecked():
            # Fichier du jour avec bascule automatique à minuit.
            return live_source.SftpLiveSource(
                host=host, user=user, password=password, daily=True)
        path = self.ed_path.text().strip()
        if not path:
            raise ValueError("Indiquez le chemin du fichier sur l'automate.")
        return live_source.SftpLiveSource(
            host=host, remote_path=path, user=user, password=password)

    def start_live(self):
        if self._worker is not None:
            return
        try:
            source = self._make_source()
        except Exception as e:
            self._set_status(str(e), error=True)
            return

        # Réinitialisation de l'état d'affichage.
        self._raw_buf = pd.DataFrame()
        self._processed = pd.DataFrame()
        self._prefill = None
        self._channels_built = False
        self.live_pane.set_channels([])
        # Délier l'axe fixe de l'ancien graphe maître (détruit) et le masquer.
    #    self.footer_axis_widget.getPlotItem().setXLink(None)
    #    self.footer_axis_widget.setVisible(False)
        self._update_buffer_seconds()

        self._worker = LiveWorker(source, interval_ms=self.spin_interval.value())
        self._worker.newData.connect(self._on_new_data)
        self._worker.status.connect(lambda m: self._set_status(m))
        self._worker.error.connect(lambda m: self._set_status(m, error=True))
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._set_config_enabled(False)
        self._set_status("Démarrage…")

    def stop_live(self):
        if self._worker is not None:
            self._worker.stop()  # _on_worker_finished réactive l'interface

    def _on_worker_finished(self):
        self._worker = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_config_enabled(True)

    def _set_config_enabled(self, enabled: bool):
        self.config_ftp.setEnabled(enabled)
        self.spin_interval.setEnabled(enabled)

    # ==================================================================
    # RÉCEPTION DES DONNÉES
    # ==================================================================

    def _on_new_data(self, block: pd.DataFrame):
        """Ajoute un bloc de lignes au tampon glissant et rafraîchit le graphe."""
        if block is None or block.empty:
            return

        block = block.dropna(subset=["Date"]).set_index("Date")
        self._raw_buf = (
            block if self._raw_buf.empty
            else pd.concat([self._raw_buf, block])
        )
        self._raw_buf = self._raw_buf[~self._raw_buf.index.duplicated(keep="last")]
        self._raw_buf.sort_index(inplace=True)

        # --- Élagage du tampon, en préservant l'état des signaux ---
        # L'automate n'écrit une colonne (souvent un digital) que lorsqu'elle
        # change : entre deux changements la valeur est absente (NaN). On propage
        # donc d'abord la dernière valeur connue (ffill), AVANT d'élaguer, et on
        # mémorise l'état des lignes supprimées dans ``_prefill``. Ainsi un signal
        # constant depuis plus longtemps que le tampon n'est jamais perdu.
        cutoff = self._raw_buf.index[-1] - pd.Timedelta(seconds=self._buffer_seconds)

        filled = self._raw_buf.ffill()
        if self._prefill is not None:
            # Compléter les NaN de tête avec l'état connu avant le tampon.
            filled = filled.fillna(self._prefill)

        dropped = filled.loc[filled.index < cutoff]
        if not dropped.empty:
            self._prefill = dropped.iloc[-1]

        self._raw_buf = self._raw_buf.loc[cutoff:]

        # Tampon complété pour l'affichage : ffill interne + état d'avant-tampon.
        view = self._raw_buf.ffill()
        if self._prefill is not None:
            view = view.fillna(self._prefill)

        # Logique métier (AID3 → vitesse, propagation, etc.).
        self._processed = log_processing.apply_ai_di_logic(view)

        if not self._channels_built:
            self._build_channels_from(self._processed)

        self.live_pane.update_data(self._processed)

    def _build_channels_from(self, df: pd.DataFrame):
        """Première arrivée de données : remplit les sélecteurs de canaux."""
        ai = [c for c in df.columns if c.startswith("AI")]
        di = [c for c in df.columns if c.startswith("DI")]
        # Couleur stable par signal, identique aux autres panes (ui/palette.py).
        self.color_map = palette.color_map_for(ai + di)
        self.sel_ai.set_channels(ai)
        self.sel_di.set_channels(di)
        self._channels_built = True
        # set_channels déclenche `changed` (différé) → _rebuild_live_channels.
        self._rebuild_live_channels()

    def _on_selection_changed(self):
        if self._channels_built:
            self._rebuild_live_channels()

    def _rebuild_live_channels(self):
        """Reconstruit les graphiques selon les canaux cochés."""
        ai_sel = self.sel_ai.get_selected("AI")
        di_sel = self.sel_di.get_selected("DI")
        channels: List[ChannelMeta] = (
            [ChannelMeta(c, False, self.color_map.get(c)) for c in ai_sel]
            + [ChannelMeta(c, True, self.color_map.get(c)) for c in di_sel]
        )
        self.live_pane.set_channels(channels)

        # Lier l'axe temporel fixe au graphe maître (recréé à chaque
        # reconstruction) pour qu'il suive la même fenêtre glissante.
    #    if self.live_pane.master is not None:
    #        self.footer_axis_widget.getPlotItem().setXLink(self.live_pane.master)

        if not self._processed.empty:
            self.live_pane.update_data(self._processed)

    # ==================================================================
    # DIVERS
    # ==================================================================

    def _on_window_changed(self, seconds: int):
        self.live_pane.window_seconds = float(seconds)
        self._update_buffer_seconds()
        if not self._processed.empty:
            self.live_pane.update_data(self._processed)

    def _update_buffer_seconds(self):
        # Marge de contexte : on conserve plus que la fenêtre affichée pour que
        # le ffill dispose d'un état antérieur même après défilement.
        self._buffer_seconds = max(self.live_pane.window_seconds * 2.0, 20.0)

    def _set_status(self, message: str, error: bool = False):
        color = "#ff7b72" if error else "#9fdf9f"
        self.lbl_status.setStyleSheet(f"color: {color};")
        self.lbl_status.setText(message)
