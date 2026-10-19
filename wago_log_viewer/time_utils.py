"""
Conversions entre horodatages pandas et coordonnées X des graphiques.

Pourquoi ce module ? Les horodatages des logs sont « naïfs » (heure locale,
sans fuseau). pyqtgraph (``DateAxisItem``) affiche quant à lui les secondes
Unix en les convertissant en heure LOCALE. Pour que l'axe affiche l'heure
des logs, on retire le décalage heure locale / UTC (Greenwich) avant de
tracer — et on le ré-ajoute pour retrouver l'horodatage sous le curseur.

Le décalage est évalué au moment présent (comportement historique de
l'application). Toutes les conversions passent par ces fonctions afin de
garantir la cohérence : axe, curseurs et bandeau d'information utilisent
exactement le même décalage, dans les deux sens.
"""

import time

import numpy as np
import pandas as pd


def local_utc_offset_seconds() -> float:
    """
    Décalage heure locale − UTC (Greenwich) en secondes, au moment présent
    (ex. +3600 en hiver à Bruxelles, +7200 en été).
    """
    return float(-(time.altzone if time.localtime().tm_isdst else time.timezone))


def index_to_axis_seconds(index: pd.DatetimeIndex) -> np.ndarray:
    """
    Convertit un index de dates en abscisses (secondes) pour pyqtgraph.

    IMPORTANT : l'index est d'abord normalisé en datetime64[ns]. Selon la
    version de pandas, un index peut être stocké en microsecondes (us) ou
    millisecondes (ms) — ``view("int64")`` renverrait alors des valeurs
    1000× ou 10⁶× trop petites, et l'axe temporel serait complètement faux
    (échelle compressée, scroll incohérent).
    """
    idx = pd.DatetimeIndex(index)
    if idx.dtype != "datetime64[ns]":
        idx = idx.astype("datetime64[ns]")
    return idx.view("int64") / 1e9 - local_utc_offset_seconds()


def timestamp_to_axis_seconds(ts: pd.Timestamp) -> float:
    """Convertit un horodatage unique en abscisse (secondes) pour pyqtgraph."""
    # Timestamp.value est TOUJOURS en nanosecondes, quelle que soit la
    # résolution d'origine — pas de normalisation nécessaire ici.
    return pd.Timestamp(ts).value / 1e9 - local_utc_offset_seconds()


def axis_seconds_to_timestamp(x_sec: float) -> pd.Timestamp:
    """Conversion inverse : abscisse graphique → horodatage des logs."""
    return pd.to_datetime(int((x_sec + local_utc_offset_seconds()) * 1e9))
