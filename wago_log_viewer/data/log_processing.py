"""
Transformation des données après lecture des fichiers de logs.

Pipeline complet (dans l'ordre d'application, voir ``ui/workers.py``) :

0. ``split_at_clock_rewind``  : découpe un fichier en tronçons chronologiques à
   chaque recul d'horloge (changement d'heure sur l'automate). L'appelant
   (``ui/workers.py``) demande alors à l'utilisateur quel tronçon conserver.
1. ``unify_columns``          : fusionne plusieurs DataFrames (un par fichier
   ou tronçon) en alignant les colonnes, trie par date et supprime les doublons.
2. ``inject_zeros_in_gaps_1hz`` : insère des lignes à zéro (1 Hz) dans les
   trous d'acquisition, pour matérialiser visuellement l'absence de mesure.
3. ``apply_ai_di_logic``      : convertit AID3 en vitesse (AIVitesse), propage
   les dernières valeurs connues (ffill) et force à 0 les lignes sans mesure.
"""

from typing import List, Tuple

import numpy as np
import pandas as pd

# --- Calibration du capteur de vitesse (dynamo tachymétrique D3) ---
# Conversion tension mesurée → vitesse : v [km/h] = GAIN * (U [V] - OFFSET)
# Valeurs déterminées empiriquement lors des essais (mission RAP EP MX).
D3_SPEED_GAIN = 0.316
D3_VOLTAGE_OFFSET = 0.2197534


# ============================================================
# 1. FUSION DE PLUSIEURS FICHIERS
# ============================================================

def unify_columns(dfs: List[pd.DataFrame]) -> pd.DataFrame:
    """
    Fusionne plusieurs DataFrames en un seul.

    Les fichiers peuvent avoir des colonnes différentes (configurations Wago
    différentes selon les jours) : on prend l'union des colonnes en préservant
    l'ordre d'apparition. Le résultat est trié par date, sans doublons.
    """
    if not dfs:
        return pd.DataFrame()

    all_ordered_cols = dfs[0].columns
    for df in dfs[1:]:
        all_ordered_cols = all_ordered_cols.union(df.columns, sort=False)

    aligned = [df.reindex(columns=all_ordered_cols) for df in dfs]
    merged = pd.concat(aligned, ignore_index=True)
    merged = merged.dropna(subset=["Date"]).sort_values("Date").drop_duplicates()
    merged["Date"] = _make_dates_unique(merged["Date"])
    return merged


def _make_dates_unique(dates: pd.Series) -> pd.Series:
    """
    Garantit des horodatages STRICTEMENT uniques, sans perte de ligne.

    Toute l'application suppose un index temporel unique (recherche du point le
    plus proche sous le curseur via ``Index.get_indexer``, découpe ``.loc``,
    axe temporel). Or un recul d'horloge conservé (``split_at_clock_rewind``)
    peut faire coïncider les horodatages de deux tronçons qui se recouvrent.
    On décale alors chaque doublon d'un nombre entier de nanosecondes (0, 1,
    2, … dans chaque groupe d'horodatages identiques) : l'ordre est préservé,
    l'écart est imperceptible (bien en-deçà du pas d'échantillonnage de 20 ms),
    et l'index redevient unique. ``dates`` doit être déjà trié.
    """
    if not dates.duplicated().any():
        return dates
    offsets = dates.groupby(dates).cumcount()
    return dates + pd.to_timedelta(offsets.to_numpy(), unit="ns")


def split_at_clock_rewind(df: pd.DataFrame) -> Tuple[List[pd.DataFrame], List[pd.Timestamp]]:
    """
    Découpe un fichier en tronçons chronologiques à chaque RECUL du temps
    (horloge reculée sur l'automate en cours d'acquisition).

    Les horodatages doivent croître ligne après ligne. Un recul (le temps qui
    revient en arrière) signale un changement d'heure : la ligne suivante
    ouvre un nouveau tronçon. Cette fonction se contente de DÉCOUPER — elle ne
    jette rien. C'est l'appelant qui décide quel tronçon conserver (les deux
    côtés d'un recul se recouvrent dans le temps et ne peuvent pas coexister
    sur un axe horaire unique) : ``ui/workers.py`` demande le choix (garder la
    partie « avant » = premier tronçon, ou « après » = dernier tronçon) à
    l'utilisateur via ``ui/clock_rewind_dialog.py``.

    Un simple saut vers l'AVANT (trou d'acquisition normal) n'est PAS un recul
    et ne provoque aucun découpage : il reste géré par ``inject_zeros_in_gaps_1hz``.

    Paramètres
    ----------
    df : DataFrame d'UN fichier, dans l'ordre des lignes (avant tri).

    Retour
    ------
    ``(tronçons, horodatages_de_recul)`` : la liste des tronçons chronologiques
    (un seul élément = ``[df]`` si aucun recul), et la liste des derniers
    horodatages précédant chaque recul (vide si aucun recul).
    """
    if df.empty or "Date" not in df.columns or len(df) < 2:
        return [df], []

    dates = df["Date"].values.astype("datetime64[ns]").astype("int64")
    backward = np.nonzero(np.diff(dates) < 0)[0]  # index juste AVANT chaque recul
    if len(backward) == 0:
        return [df], []  # entièrement chronologique

    # Points de coupe : juste APRÈS chaque recul (début du nouveau tronçon).
    cuts = [int(b) + 1 for b in backward]
    bounds = [0] + cuts + [len(df)]
    segments = [df.iloc[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
    segments = [s for s in segments if not s.empty]
    # Dernier instant chronologique de chaque tronçon précédant un recul.
    rewind_times = [df["Date"].iloc[c - 1] for c in cuts]
    return segments, rewind_times


# ============================================================
# 2. INJECTION DE ZÉROS DANS LES TROUS D'ACQUISITION
# ============================================================

def _ceil_to_next_whole_second_strict(ts: pd.Timestamp) -> pd.Timestamp:
    """Prochaine seconde entière STRICTEMENT > ts (10.000 → 11.000 ; 10.123 → 11.000)."""
    return ts.floor("s") + pd.Timedelta(seconds=1)


def _floor_to_prev_whole_second_strict(ts: pd.Timestamp) -> pd.Timestamp:
    """
    Seconde entière STRICTEMENT < ts si ts tombe pile sur une seconde,
    sinon simple arrondi inférieur (14:30:45.000 → 14:30:44.000 ;
    14:30:45.789 → 14:30:45.000).
    """
    flo = ts.floor("s")
    if ts == flo:
        return flo - pd.Timedelta(seconds=1)
    return flo


def inject_zeros_in_gaps_1hz(
    df: pd.DataFrame,
    *,
    min_gap_seconds: float = 1.0,
) -> pd.DataFrame:
    """
    Insère des lignes « tout à zéro » à 1 Hz dans les trous d'acquisition.

    L'automate Wago n'enregistre que lorsqu'il y a de l'activité : entre deux
    sessions de mesure, il peut y avoir des heures sans aucune ligne. Sans ces
    zéros, les graphiques relieraient le dernier point d'une session au premier
    de la suivante par une droite trompeuse.

    Les lignes injectées sont placées sur des secondes entières (XX:XX:SS.000)
    strictement comprises entre deux échantillons réels. Aucune ligne ni valeur
    d'origine n'est modifiée.

    Paramètres
    ----------
    df : DataFrame avec une colonne ``Date`` (Timestamp).
    min_gap_seconds : durée minimale d'un trou pour déclencher l'injection.

    Retour
    ------
    DataFrame indexé par ``Date`` : lignes d'origine + lignes injectées, triées.
    """
    out = df.dropna(subset=["Date"]).sort_values("Date")
    dfi = out.set_index("Date")

    ai_cols = [c for c in dfi.columns if c.startswith("AI")]
    di_cols = [c for c in dfi.columns if c.startswith("DI")]
    if not ai_cols and not di_cols:
        raise ValueError("Aucune colonne AI*/DI* trouvée.")

    min_gap = pd.to_timedelta(min_gap_seconds, unit="s")

    injected_chunks: List[pd.DataFrame] = []
    idx = dfi.index

    # Parcours des paires d'échantillons consécutifs à la recherche de trous.
    for prev_ts, next_ts in zip(idx[:-1], idx[1:]):
        if next_ts - prev_ts < min_gap:
            continue

        start = _ceil_to_next_whole_second_strict(prev_ts)
        end = _floor_to_prev_whole_second_strict(next_ts)
        if start > end:
            continue

        inject_times = pd.date_range(start=start, end=end, freq="1s")
        if len(inject_times) == 0:
            continue

        data = {c: np.zeros(len(inject_times), dtype=np.float64)
                for c in ai_cols + di_cols}
        injected_chunks.append(pd.DataFrame(data, index=inject_times))

    if not injected_chunks:
        # Aucun trou détecté : rien à injecter.
        result = dfi.reset_index().rename(columns={"index": "Date"})
        return result.set_index("Date")

    injected_all = pd.concat(injected_chunks, axis=0)
    result = pd.concat([dfi, injected_all], axis=0).sort_index()
    result = result.reset_index().rename(columns={"index": "Date"})
    return result.set_index("Date")


# ============================================================
# 3. LOGIQUE AI / DI ET CONVERSION VITESSE
# ============================================================

def apply_ai_di_logic(df: pd.DataFrame) -> pd.DataFrame:
    """
    Applique la logique métier sur le DataFrame fusionné :

    1. Si la colonne ``AID3`` (tension dynamo) existe, elle est convertie en
       vitesse et renommée ``AIVitesse`` (voir constantes de calibration).
    2. Les valeurs AI et DI manquantes sont remplacées par la dernière valeur
       connue (forward fill) : l'automate n'écrit une valeur que lorsqu'elle
       change ou lors d'une mesure.
    3. Les lignes sans AUCUNE mesure (ni AI ni DI, c.-à-d. avant la première
       mesure réelle) sont mises à 0.
    """
    out = df.copy()

    # --- Conversion tension D3 → vitesse ---
    if "AID3" in out.columns:
        out["AIVitesse"] = D3_SPEED_GAIN * (out["AID3"] - D3_VOLTAGE_OFFSET)
        out = out.drop(columns=["AID3"])

    ai_cols = [c for c in out.columns if c.startswith("AI")]
    di_cols = [c for c in out.columns if c.startswith("DI")]

    # --- Propagation de la dernière valeur connue ---
    if ai_cols:
        out[ai_cols] = out[ai_cols].ffill()
    if di_cols:
        out[di_cols] = out[di_cols].ffill()

    # --- Lignes sans aucune mesure → tout à zéro ---
    has_ai = out[ai_cols].notna().any(axis=1) if ai_cols else pd.Series(False, index=out.index)
    has_di = out[di_cols].notna().any(axis=1) if di_cols else pd.Series(False, index=out.index)
    no_measure = ~(has_ai | has_di)

    if ai_cols or di_cols:
        out.loc[no_measure, ai_cols + di_cols] = 0.0

    return out
