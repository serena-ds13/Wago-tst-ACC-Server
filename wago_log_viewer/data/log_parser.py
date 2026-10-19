"""
Lecture des fichiers de logs Wago (.txt).

Format attendu (CSV avec en-tête, séparateur « , ») :

    Date, AI1, AI2, DI1, DI2, ...
    18/03/2026 14:30:40:779, 0.0659, 0.0, FALSE, FALSE, ...

Conventions de nommage des colonnes :
- ``Date`` : horodatage au format ``jj/mm/aaaa HH:MM:SS:mmm`` ;
- ``AI*``  : entrées analogiques (valeurs flottantes, en volts) ;
- ``DI*``  : entrées digitales (TRUE/FALSE ou 1.0/0.0).

Les lignes peuvent être incomplètes (moins de valeurs que de colonnes) :
les valeurs manquantes deviennent NaN et seront traitées plus tard par
``log_processing.apply_ai_di_logic`` (propagation de la dernière valeur).
"""

import re
from typing import List, Optional, Tuple

import pandas as pd

# Horodatage : jj/mm/aaaa HH:MM:SS suivi des millisecondes. Selon la version
# du programme Wago qui a généré le fichier, les millisecondes sont séparées
# par « : » (14:30:40:779) ou par « . » (14:30:40.779) — les deux existent
# dans les fichiers réels, le parseur accepte donc les deux.
DATE_RE = re.compile(
    r"^(?P<d>\d{2})/(?P<m>\d{2})/(?P<y>\d{4}) "
    r"(?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2})(?:[:.](?P<ms>\d{1,6}))?$"
)

# Formats essayés dans l'ordre par le parsing rapide (pandas).
DATE_FORMATS = [
    "%d/%m/%Y %H:%M:%S:%f",   # millisecondes après « : » (format Wago courant)
    "%d/%m/%Y %H:%M:%S.%f",   # millisecondes après « . » (variante rencontrée)
    "%d/%m/%Y %H:%M:%S",      # sans millisecondes
]


def parse_timestamp(raw: str) -> Optional[pd.Timestamp]:
    """
    Convertit « 18/03/2026 14:30:40:779 » (ou la variante avec un point :
    « …40.779 ») en Timestamp. Retourne None si le format est invalide.
    """
    m = DATE_RE.match(raw.strip())
    if not m:
        return None
    g = m.groupdict()
    # Millisecondes optionnelles ; complétées/tronquées à 3 chiffres.
    ms = int((g["ms"] or "0").ljust(3, "0")[:3])
    return pd.Timestamp(
        year=int(g["y"]), month=int(g["m"]), day=int(g["d"]),
        hour=int(g["H"]), minute=int(g["M"]), second=int(g["S"]),
    ) + pd.Timedelta(milliseconds=ms)


def normalize_header(header_line: str) -> List[str]:
    """Découpe la ligne d'en-tête et supprime les espaces parasites."""
    return [c.strip() for c in header_line.split(",") if c.strip()]


def parse_value(col: str, val: Optional[str]) -> Optional[float]:
    """
    Convertit une valeur texte en float selon le type de colonne.

    - DI* : TRUE → 1.0, FALSE → 0.0, sinon conversion numérique directe.
    - AI* : conversion numérique directe.
    - Valeur vide ou colonne inconnue → None (sera traité comme NaN).
    """
    if val is None or val.strip() == "":
        return None
    s = val.strip()

    if col.startswith("DI"):
        if s.upper() == "TRUE":
            return 1.0
        if s.upper() == "FALSE":
            return 0.0
        try:
            return float(s)
        except ValueError:
            return None

    if col.startswith("AI"):
        try:
            return float(s)
        except ValueError:
            return None

    return None


def read_wago_txt(path: str) -> Tuple[pd.DataFrame, List[str]]:
    """
    Lit un fichier de log Wago et retourne ``(DataFrame, liste des colonnes)``.

    Deux stratégies :
    1. Lecture rapide via ``pandas.read_csv`` (moteur C) — utilisée dans
       l'immense majorité des cas.
    2. En cas d'échec (fichier mal formé), repli sur un parsing manuel
       ligne par ligne, plus tolérant mais plus lent.

    Le DataFrame retourné contient une colonne ``Date`` (Timestamp) et des
    colonnes numériques AI*/DI*. Il n'est PAS indexé par la date : c'est
    ``log_processing`` qui s'en charge après la fusion des fichiers.
    """
    try:
        return _read_fast(path)
    except Exception as fast_error:
        df, header = _read_fallback(path)
        if df.empty:
            # Les deux stratégies ont échoué : remonter la cause précise
            # (souvent un format de date inattendu) plutôt qu'un échec muet.
            raise ValueError(f"{path} : {fast_error}") from fast_error
        return df, header


def _parse_date_series(raw: pd.Series) -> pd.Series:
    """
    Convertit la colonne Date (texte) en datetimes, en essayant chaque format
    connu. Lève une ValueError EXPLICITE si aucun format ne convient :
    surtout ne jamais laisser passer des dates mal interprétées en silence
    (elles produiraient un axe temporel absurde dans les graphiques).
    """
    s = raw.astype(str).str.strip()

    for fmt in DATE_FORMATS:
        parsed = pd.to_datetime(s, format=fmt, errors="coerce")
        # Format accepté si la quasi-totalité des lignes est convertie
        # (tolérance pour quelques lignes corrompues en fin de fichier).
        if parsed.notna().mean() >= 0.9:
            # Normalisation en nanosecondes : les pandas récents peuvent
            # retourner du datetime64[us], ce qui fausserait l'axe temporel
            # (voir time_utils.index_to_axis_seconds).
            return parsed.astype("datetime64[ns]")

    sample = s.iloc[0] if len(s) else "(vide)"
    raise ValueError(
        f"Format de date non reconnu : « {sample} ». "
        f"Formats acceptés : jj/mm/aaaa HH:MM:SS:mmm ou jj/mm/aaaa HH:MM:SS.mmm"
    )


def _read_fast(path: str) -> Tuple[pd.DataFrame, List[str]]:
    """Lecture rapide via pandas (moteur C)."""
    # Les dates ne sont PAS parsées par read_csv : en cas de format inattendu,
    # read_csv laisserait la colonne en texte sans prévenir. Le parsing est
    # fait explicitement ci-dessous, avec validation.
    df = pd.read_csv(path, sep=",", header=0, engine="c")
    df.columns = df.columns.str.strip()

    if "Date" not in df.columns:
        raise ValueError("Colonne « Date » introuvable dans le fichier.")

    df["Date"] = _parse_date_series(df["Date"])
    df = df.dropna(subset=["Date"])

    # Garde-fou : des dates aberrantes (avant l'an 2000) signalent un parsing
    # incorrect — mieux vaut échouer clairement qu'afficher un axe faux.
    if not df.empty and df["Date"].min().year < 2000:
        raise ValueError(
            f"Dates aberrantes après parsing (min = {df['Date'].min()}) : "
            f"format de fichier non supporté."
        )

    # Conversion en numérique : les valeurs non convertibles deviennent NaN.
    for col in df.columns:
        if col == "Date":
            continue
        if col.startswith("DI"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        elif col.startswith("AI"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float)

    return df, list(df.columns)


def _rows_from_lines(lines: List[str], header: List[str]) -> List[dict]:
    """
    Convertit une liste de lignes CSV (sans en-tête) en lignes de DataFrame,
    selon un ``header`` déjà connu. Les lignes incomplètes sont tolérées
    (valeurs manquantes → None) ; les lignes sans horodatage sont ignorées.

    Factorisé pour être partagé entre le parsing de repli (``_read_fallback``)
    et la lecture incrémentale temps réel (``parse_block``).
    """
    rows = []
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        # Supprimer les champs vides en fin de ligne (virgule terminale).
        while parts and parts[-1] == "":
            parts.pop()

        # Associer chaque colonne à sa valeur ; None si la ligne est trop courte.
        mapping = {col: (parts[i] if i < len(parts) else None)
                   for i, col in enumerate(header)}

        ts = parse_timestamp(mapping.get("Date") or "")
        if ts is None:
            continue  # ligne sans horodatage exploitable → ignorée

        row = {"Date": ts}
        for col in header:
            if col != "Date":
                row[col] = parse_value(col, mapping[col])
        rows.append(row)
    return rows


def parse_block(text: str, header: List[str]) -> pd.DataFrame:
    """
    Parse un bloc de texte CSV (SANS ligne d'en-tête) en DataFrame, selon un
    ``header`` déjà connu. Utilisé en mode TEMPS RÉEL : seules les lignes
    ajoutées depuis la dernière lecture sont transmises, l'en-tête ayant été
    lu une seule fois à l'ouverture du fichier.

    Le DataFrame retourné contient une colonne ``Date`` (Timestamp ns) et les
    colonnes AI*/DI* numériques. Vide si aucune ligne exploitable.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rows = _rows_from_lines(lines, header)
    df = pd.DataFrame(rows)
    if not df.empty:
        # Même normalisation que les autres chemins (voir _parse_date_series).
        df["Date"] = pd.to_datetime(df["Date"]).astype("datetime64[ns]")
    return df


def _read_fallback(path: str) -> Tuple[pd.DataFrame, List[str]]:
    """Parsing manuel ligne par ligne (tolérant aux lignes incomplètes)."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        return pd.DataFrame(), []

    header = normalize_header(lines[0])
    if "Date" not in header:
        return pd.DataFrame(), []

    rows = _rows_from_lines(lines[1:], header)

    df = pd.DataFrame(rows)
    if not df.empty:
        # Même normalisation que le chemin rapide (voir _parse_date_series).
        df["Date"] = pd.to_datetime(df["Date"]).astype("datetime64[ns]")
    return df, header
