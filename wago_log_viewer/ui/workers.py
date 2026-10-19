"""
Workers Qt : opérations longues exécutées hors du thread d'interface.

Le chargement d'un fichier de 24 h (~4 millions de lignes) prend plusieurs
secondes : il est exécuté dans un ``QThread`` pour que l'interface reste
réactive, avec une barre de progression alimentée par le signal ``progress``.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from typing import List

import pandas as pd
from PySide6 import QtCore

from ..data import log_parser, log_processing


class FileLoadWorker(QtCore.QThread):
    """
    Charge et prépare un ou plusieurs fichiers de logs en arrière-plan.

    Signaux :
    - ``progress(int)``                      : avancement 0–100 %.
    - ``finished(DataFrame, list, list)``    : données fusionnées + noms des
      colonnes AI et DI détectées.
    - ``needRewindChoice(list)``             : un changement d'heure (recul) a
      été détecté ; demande au thread UI quel tronçon garder pour chaque
      fichier concerné. Le worker se met en pause jusqu'à ``provide_choices``.
    - ``warning(str)``                       : avertissement non bloquant.
    - ``error(str)``                         : message d'erreur le cas échéant.
    """

    progress = QtCore.Signal(int)
    finished = QtCore.Signal(pd.DataFrame, list, list)
    needRewindChoice = QtCore.Signal(list)
    warning = QtCore.Signal(str)
    error = QtCore.Signal(str)

    def __init__(self, paths: List[str]):
        super().__init__()
        self.paths = paths
        # Rendez-vous worker ↔ thread UI pour le choix de tronçon sur recul.
        self._mutex = QtCore.QMutex()
        self._cond = QtCore.QWaitCondition()
        self._answered = False
        self._answer = None   # dict {index: "before"|"after"}, ou None si annulé

    def provide_choices(self, answer):
        """
        Appelé depuis le thread UI pour transmettre la réponse de l'utilisateur
        (dict de choix, ou ``None`` si le chargement est annulé) et réveiller le
        worker en attente dans ``run()``.
        """
        self._mutex.lock()
        try:
            self._answer = answer
            self._answered = True
            self._cond.wakeAll()
        finally:
            self._mutex.unlock()

    def run(self):
        try:
            self.progress.emit(10)

            # Lecture des fichiers en parallèle (4 threads max : la lecture
            # est limitée par le disque, inutile d'en lancer davantage).
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(log_parser.read_wago_txt, self.paths))

            # Association fichier ↔ données (l'ordre de executor.map est
            # préservé) pour un traitement et un message PAR FICHIER.
            named = [(path, df) for path, (df, _) in zip(self.paths, results)
                     if not df.empty]
            if not named:
                raise ValueError("Aucune donnée exploitable.")

            # Détection d'un changement d'heure sur l'automate (recul du temps
            # dans l'ordre des lignes). Chaque fichier concerné est découpé en
            # tronçons chronologiques ; comme les deux côtés d'un recul se
            # recouvrent (mêmes heures), on ne peut en garder qu'UN. Le choix
            # (« avant » = 1er tronçon, « après » = dernier) est demandé à
            # l'utilisateur, fichier par fichier.
            per_file = []   # (segments, rewind_times) dans l'ordre de `named`
            affected = []   # infos pour le dialogue (fichiers avec recul)
            for i, (path, df) in enumerate(named):
                segments, rewind_times = log_processing.split_at_clock_rewind(df)
                per_file.append((segments, rewind_times))
                if rewind_times:
                    affected.append({
                        "index": i,
                        "name": os.path.basename(path),
                        "rewinds": rewind_times,
                    })

            choices = {}
            if affected:
                # Demander le choix au thread UI puis attendre la réponse.
                self._answered = False
                self._answer = None
                self.needRewindChoice.emit(affected)
                self._mutex.lock()
                try:
                    while not self._answered:
                        self._cond.wait(self._mutex)
                    choices = self._answer
                finally:
                    self._mutex.unlock()
                if choices is None:
                    return  # chargement annulé par l'utilisateur

            # Ne conserver, pour chaque fichier, que le tronçon retenu.
            dfs = []
            for i, (segments, rewind_times) in enumerate(per_file):
                if rewind_times:
                    side = choices.get(i, "before")
                    dfs.append(segments[0] if side == "before" else segments[-1])
                else:
                    dfs.append(segments[0])   # un seul tronçon (aucun recul)

            # Pipeline de préparation — voir wago_log_viewer/data/log_processing.py
            self.progress.emit(30)
            merged = log_processing.unify_columns(dfs)

            self.progress.emit(50)
            merged = log_processing.inject_zeros_in_gaps_1hz(merged, min_gap_seconds=1.0)

            self.progress.emit(70)
            merged = log_processing.apply_ai_di_logic(merged)

            self.progress.emit(90)
            cols = list(merged.columns)
            ai_all = [c for c in cols if c.startswith("AI")]
            di_all = [c for c in cols if c.startswith("DI")]

            self.progress.emit(100)
            self.finished.emit(merged, ai_all, di_all)
        except Exception as e:
            self.error.emit(str(e))
