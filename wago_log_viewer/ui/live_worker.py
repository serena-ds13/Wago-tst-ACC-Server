"""
Worker Qt du mode TEMPS RÉEL : interroge une ``LiveSource`` à intervalle
régulier (200 ms par défaut) dans un thread séparé, pour que l'interface
reste fluide pendant que les données arrivent.

Le worker ne touche JAMAIS aux widgets : il se contente d'émettre des signaux
(nouvelles lignes, erreurs, statut) que la fenêtre consomme dans le thread GUI.
"""

import pandas as pd
from PySide6 import QtCore

from ..data.live_source import LiveSource


class LiveWorker(QtCore.QThread):
    """
    Boucle de lecture temps réel.

    Signaux :
    - ``newData(DataFrame)`` : lignes nouvelles depuis le dernier cycle ;
    - ``status(str)``        : message d'état (« Connecté à … ») ;
    - ``error(str)``         : erreur (la boucle s'arrête sur erreur d'ouverture,
      continue sur erreur de lecture ponctuelle).
    """

    newData = QtCore.Signal(pd.DataFrame)
    status = QtCore.Signal(str)
    error = QtCore.Signal(str)

    def __init__(self, source: LiveSource, interval_ms: int = 200):
        super().__init__()
        self.source = source
        self.interval_ms = max(20, int(interval_ms))
        self._running = True

    def run(self):
        # --- Ouverture (connexion FTP, ouverture fichier, init simulation) ---
        try:
            columns = self.source.open()
            self.status.emit("Connecté — " + ", ".join(columns))
        except Exception as e:
            self.error.emit(f"Connexion impossible : {e}")
            return

        # --- Boucle de polling ---
        while self._running:
            try:
                block = self.source.read_new()
                if block is not None and not block.empty:
                    self.newData.emit(block)
            except Exception as e:
                # Une erreur de lecture ponctuelle (paquet FTP perdu, fichier
                # momentanément verrouillé) ne doit pas tuer la session.
                self.error.emit(f"Lecture : {e}")
            self.msleep(self.interval_ms)

        try:
            self.source.close()
        except Exception:
            pass

    def stop(self):
        """Demande l'arrêt de la boucle et attend la fin propre du thread."""
        self._running = False
        self.wait(2000)
