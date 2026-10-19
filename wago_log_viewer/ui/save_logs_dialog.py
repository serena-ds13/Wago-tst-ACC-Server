"""
Boîte de dialogue « Enregistrer les fichiers » du mode temps réel.

Liste les fichiers présents dans le répertoire de logs de l'automate
(``/media/6289-794F/stib/``), permet de cocher/décocher ceux à récupérer, puis
de les télécharger dans un dossier choisi sur l'ordinateur.

Le dépôt source est abstrait par ``data.live_source.LogRepository`` :
- ``SftpLogRepository``  en fonctionnement réel (carte mémoire de l'automate) ;
- ``LocalLogRepository`` pour tester sans automate (un dossier local fait office
  de carte mémoire — voir ``tools/test_save_logs_dialog.py``).

Les opérations potentiellement longues (listing réseau, téléchargement)
s'exécutent dans des threads pour ne jamais figer l'interface.
"""

import os
from typing import List, Optional, Tuple

from PySide6 import QtCore, QtWidgets

from ..data.live_source import LogRepository


def _human_size(n: int) -> str:
    """Taille lisible (o, Ko, Mo, Go)."""
    size = float(n)
    for unit in ("o", "Ko", "Mo"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} Go"


class _ListWorker(QtCore.QThread):
    """Récupère la liste des fichiers du dépôt (peut nécessiter une connexion)."""

    done = QtCore.Signal(list)     # List[(name, size)]
    failed = QtCore.Signal(str)

    def __init__(self, repo: LogRepository):
        super().__init__()
        self.repo = repo

    def run(self):
        try:
            self.done.emit(self.repo.list_files())
        except Exception as e:
            self.failed.emit(str(e))


class _DownloadWorker(QtCore.QThread):
    """Télécharge les fichiers choisis, un par un, en signalant la progression."""

    progress = QtCore.Signal(int, int, str)   # index (1-based), total, nom courant
    finished_all = QtCore.Signal(int, list)   # n_ok, [(nom, message_erreur)]

    def __init__(self, repo: LogRepository, names: List[str], dest_dir: str):
        super().__init__()
        self.repo = repo
        self.names = names
        self.dest_dir = dest_dir

    def run(self):
        n_ok = 0
        errors: List[Tuple[str, str]] = []
        total = len(self.names)
        for i, name in enumerate(self.names, start=1):
            self.progress.emit(i, total, name)
            try:
                self.repo.download(name, os.path.join(self.dest_dir, name))
                n_ok += 1
            except Exception as e:
                errors.append((name, str(e)))
        self.finished_all.emit(n_ok, errors)


class SaveLogsDialog(QtWidgets.QDialog):
    """Sélection et téléchargement des fichiers de log de l'automate."""

    def __init__(self, repository: LogRepository,
                 parent: Optional[QtWidgets.QWidget] = None,
                 default_dir: str = ""):
        super().__init__(parent)
        self.repo = repository
        self._list_worker: Optional[_ListWorker] = None
        self._dl_worker: Optional[_DownloadWorker] = None

        self.setWindowTitle("Enregistrer les fichiers de log")
        self.resize(540, 500)
        self._build_ui()
        if default_dir:
            self.ed_dest.setText(default_dir)

        self.finished.connect(self._cleanup)
        # Listing démarré après l'affichage de la fenêtre (UI réactive d'emblée).
        QtCore.QTimer.singleShot(0, self._start_listing)

    # ------------------------------------------------------------------

    def _build_ui(self):
        lay = QtWidgets.QVBoxLayout(self)

        self.lbl_status = QtWidgets.QLabel("Connexion…")
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)

        # --- Liste des fichiers (cases à cocher) ---
        self.list = QtWidgets.QListWidget()
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        lay.addWidget(self.list, 1)

        sel_row = QtWidgets.QHBoxLayout()
        self.btn_all = QtWidgets.QPushButton("Tout sélectionner")
        self.btn_none = QtWidgets.QPushButton("Tout désélectionner")
        self.btn_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_none.clicked.connect(lambda: self._set_all_checked(False))
        sel_row.addWidget(self.btn_all)
        sel_row.addWidget(self.btn_none)
        sel_row.addStretch()
        lay.addLayout(sel_row)

        # --- Dossier de destination ---
        dest_row = QtWidgets.QHBoxLayout()
        dest_row.addWidget(QtWidgets.QLabel("Enregistrer dans :"))
        self.ed_dest = QtWidgets.QLineEdit()
        self.ed_dest.setPlaceholderText("Choisir un dossier sur l'ordinateur…")
        self.btn_browse = QtWidgets.QPushButton("Parcourir…")
        self.btn_browse.clicked.connect(self._browse_dest)
        dest_row.addWidget(self.ed_dest, 1)
        dest_row.addWidget(self.btn_browse)
        lay.addLayout(dest_row)

        # --- Barre de progression (masquée tant qu'aucun téléchargement) ---
        self.progress = QtWidgets.QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)

        # --- Boutons ---
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        self.btn_download = QtWidgets.QPushButton("Télécharger")
        self.btn_download.setDefault(True)
        self.btn_download.setEnabled(False)   # activé après le listing
        self.btn_download.clicked.connect(self._start_download)
        self.btn_cancel = QtWidgets.QPushButton("Fermer")
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_download)
        btn_row.addWidget(self.btn_cancel)
        lay.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def _start_listing(self):
        self.lbl_status.setText("Lecture du dossier de l'automate…")
        self._list_worker = _ListWorker(self.repo)
        self._list_worker.done.connect(self._on_listed)
        self._list_worker.failed.connect(self._on_list_failed)
        self._list_worker.start()

    def _on_listed(self, files: List[Tuple[str, int]]):
        self.list.clear()
        for name, size in files:
            item = QtWidgets.QListWidgetItem(f"{name}    ({_human_size(size)})")
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Checked)
            item.setData(QtCore.Qt.UserRole, name)
            self.list.addItem(item)

        if files:
            self.lbl_status.setText(f"{len(files)} fichier(s) trouvé(s).")
            self.btn_download.setEnabled(True)
        else:
            self.lbl_status.setText("Aucun fichier dans le dossier.")

    def _on_list_failed(self, message: str):
        self.lbl_status.setText(f"<span style='color:#ff7b72'>Échec : {message}</span>")
        QtWidgets.QMessageBox.critical(
            self, "Connexion impossible",
            f"Impossible de lister les fichiers :\n{message}")

    # ------------------------------------------------------------------
    # Sélection
    # ------------------------------------------------------------------

    def _set_all_checked(self, checked: bool):
        state = QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def _checked_names(self) -> List[str]:
        names = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() == QtCore.Qt.Checked:
                names.append(item.data(QtCore.Qt.UserRole))
        return names

    def _browse_dest(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Dossier de destination", self.ed_dest.text() or "")
        if path:
            self.ed_dest.setText(path)

    # ------------------------------------------------------------------
    # Téléchargement
    # ------------------------------------------------------------------

    def _start_download(self):
        names = self._checked_names()
        if not names:
            QtWidgets.QMessageBox.information(
                self, "Aucun fichier", "Cochez au moins un fichier à enregistrer.")
            return

        dest = self.ed_dest.text().strip()
        if not dest or not os.path.isdir(dest):
            QtWidgets.QMessageBox.information(
                self, "Dossier invalide",
                "Choisissez un dossier de destination valide.")
            return

        self._set_controls_enabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(names))
        self.progress.setValue(0)

        self._dl_worker = _DownloadWorker(self.repo, names, dest)
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.finished_all.connect(self._on_dl_finished)
        self._dl_worker.start()

    def _on_dl_progress(self, index: int, total: int, name: str):
        self.progress.setValue(index)
        self.lbl_status.setText(f"Téléchargement {index}/{total} : {name}")

    def _on_dl_finished(self, n_ok: int, errors: List[Tuple[str, str]]):
        self.progress.setValue(self.progress.maximum())
        if errors:
            detail = "\n".join(f"• {n} : {msg}" for n, msg in errors)
            QtWidgets.QMessageBox.warning(
                self, "Téléchargement terminé (avec erreurs)",
                f"{n_ok} fichier(s) enregistré(s).\n"
                f"{len(errors)} échec(s) :\n{detail}")
            self._set_controls_enabled(True)
            self.lbl_status.setText("Terminé avec des erreurs.")
        else:
            QtWidgets.QMessageBox.information(
                self, "Téléchargement terminé",
                f"{n_ok} fichier(s) enregistré(s) dans :\n{self.ed_dest.text()}")
            self.accept()

    # ------------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool):
        for w in (self.list, self.btn_all, self.btn_none, self.ed_dest,
                  self.btn_browse, self.btn_download, self.btn_cancel):
            w.setEnabled(enabled)

    def _cleanup(self, *_):
        """Attend la fin des threads et ferme la connexion à la fermeture."""
        for w in (self._list_worker, self._dl_worker):
            if w is not None and w.isRunning():
                w.wait(5000)
        try:
            self.repo.close()
        except Exception:
            pass

    def reject(self):
        # Pendant un téléchargement actif, la fermeture est neutralisée
        # (les boutons sont déjà désactivés ; ceci protège la touche Échap).
        if self._dl_worker is not None and self._dl_worker.isRunning():
            return
        super().reject()
