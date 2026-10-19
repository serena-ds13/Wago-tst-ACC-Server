"""
Analyse des signaux digitaux : détection de fronts et cache de résultats.

Utilisé par la fonctionnalité « Verrouiller sur montées 44P » de l'interface :
on détecte tous les fronts montants du signal DI44P pour pouvoir naviguer
d'un front à l'autre avec les boutons ◄ / ►.
"""

import hashlib
from typing import List, Optional

import numpy as np
import pandas as pd


def find_rising_edges_for_di(df: pd.DataFrame, di_column: str) -> List[pd.Timestamp]:
    """
    Détecte tous les fronts montants (transitions 0 → 1) d'un signal digital.

    Paramètres
    ----------
    df : DataFrame indexé par date, contenant les colonnes DI*.
    di_column : nom de la colonne, ex. « DI44P ».

    Retour
    ------
    Liste des horodatages de chaque front montant.
    """
    if di_column not in df.columns:
        return []

    signal = df[di_column].fillna(0).values.astype(float)

    # diff = +1 sur un front montant, -1 sur un front descendant.
    diffs = np.diff(signal, prepend=signal[0] if len(signal) > 0 else 0)
    rising_indices = np.where(diffs == 1)[0]

    return [df.index[i] for i in rising_indices if i < len(df)]


def find_falling_edges_for_di(df: pd.DataFrame, di_column: str) -> List[pd.Timestamp]:
    """Détecte tous les fronts descendants (transitions 1 → 0). Voir ci-dessus."""
    if di_column not in df.columns:
        return []

    signal = df[di_column].fillna(0).values.astype(float)
    diffs = np.diff(signal, prepend=signal[0] if len(signal) > 0 else 0)
    falling_indices = np.where(diffs == -1)[0]

    return [df.index[i] for i in falling_indices if i < len(df)]


class RisingEdgeCache:
    """
    Cache (singleton) des fronts détectés.

    La détection sur un fichier de 24 h (~4 millions de lignes) prend quelques
    secondes : on mémorise le résultat par nom de signal. Un hash du DataFrame
    sert de clé d'invalidation — si les données changent (nouveaux fichiers
    chargés), le cache est automatiquement considéré comme périmé.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.cache = {}
        return cls._instance

    @staticmethod
    def _compute_dataframe_hash(df: pd.DataFrame) -> str:
        """Hash du contenu du DataFrame, pour détecter un changement de données."""
        if df.empty:
            return "empty"
        df_bytes = pd.util.hash_pandas_object(df, index=True).values.tobytes()
        # SHA-256 : simple empreinte de contenu servant de clé d'invalidation de
        # cache (usage non cryptographique), conforme à la reco SonarQube.
        return hashlib.sha256(df_bytes).hexdigest()

    def store_edges(self, signal_name: str, df: pd.DataFrame,
                    edges: List[pd.Timestamp]) -> None:
        """Mémorise les fronts détectés, associés au hash des données courantes."""
        self.cache[signal_name] = {
            "edges": edges,
            "hash": self._compute_dataframe_hash(df),
        }

    def get_edges(self, signal_name: str, df: pd.DataFrame) -> Optional[List[pd.Timestamp]]:
        """
        Retourne les fronts en cache s'ils sont encore valides,
        sinon None (cache absent ou données modifiées).
        """
        if signal_name not in self.cache:
            return None

        cached = self.cache[signal_name]
        if cached["hash"] == self._compute_dataframe_hash(df):
            return cached["edges"]

        # Les données ont changé : on invalide l'entrée.
        del self.cache[signal_name]
        return None

    def invalidate(self) -> None:
        """Vide entièrement le cache (appelé à chaque nouveau chargement)."""
        self.cache.clear()
