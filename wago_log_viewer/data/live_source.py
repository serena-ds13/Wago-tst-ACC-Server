"""
Sources de données TEMPS RÉEL pour le Wago Log Viewer.

L'automate Wago enregistre en continu dans un fichier texte (même format que
les logs ouverts en lecture statique). En mode temps réel, on relit ce fichier
à intervalle régulier (200 ms par défaut) et on ne traite QUE les lignes
ajoutées depuis la dernière lecture (lecture incrémentale), pour rester léger
même si le fichier grossit pendant des heures.

Trois sources interchangeables (même interface ``LiveSource``) :

- ``SftpLiveSource``      : lit le fichier directement sur l'automate via SFTP
  (cas réel). L'hôte et les identifiants proviennent de la configuration
  (variables d'environnement ou ``wago_plc.ini`` — voir ``plc_config``), jamais
  codés en dur.
- ``FileTailLiveSource``  : lit un fichier local qui grossit en continu (utile
  pour tester tout le pipeline incrémental sans automate, en pointant sur un
  fichier alimenté par ``tools/fake_plc_writer.py``).
- ``SimulatedLiveSource`` : génère des signaux synthétiques à la cadence réelle,
  SANS aucun fichier ni matériel — c'est la source à utiliser pour visualiser
  immédiatement le rendu du mode temps réel.

Toutes les sources renvoient, à chaque appel de ``read_new()``, un DataFrame
des lignes nouvelles : colonne ``Date`` (Timestamp) + colonnes AI*/DI* brutes.
La conversion métier (AID3 → vitesse, ffill) est appliquée plus tard sur le
tampon glissant, côté affichage (voir ``ui/live_view.py``).
"""

import datetime
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from . import log_parser, plc_config

# --- Emplacement du fichier de log sur l'automate Wago ---
# L'automate crée un fichier par jour, nommé « logs_AAAA-MM-JJ.txt », sur la
# carte SD montée sous /media/<UUID>/stib (UUID = identifiant du volume SD ;
# il change si la carte est reformatée/remplacée → chemin modifiable).
# ATTENTION : les chemins SFTP sont sensibles à la casse.
PLC_LOG_DIR = "/media/6289-794F/stib"
PLC_LOG_PREFIX = "logs_"
PLC_LOG_DATE_FMT = "%Y-%m-%d"
PLC_LOG_SUFFIX = ".txt"


def plc_log_path(day: Optional[datetime.date] = None,
                 directory: str = PLC_LOG_DIR,
                 prefix: str = PLC_LOG_PREFIX,
                 date_fmt: str = PLC_LOG_DATE_FMT,
                 suffix: str = PLC_LOG_SUFFIX) -> str:
    """Chemin du fichier de log de l'automate pour un jour donné (aujourd'hui
    par défaut), ex. ``/media/6289-794F/stib/logs_2026-06-19.txt``."""
    day = day or datetime.date.today()
    return f"{directory}/{prefix}{day.strftime(date_fmt)}{suffix}"


class LiveSource:
    """Interface commune des sources temps réel."""

    def open(self) -> List[str]:
        """
        Établit la connexion / ouvre le fichier et retourne la liste des
        colonnes (en-tête). Lève une exception explicite en cas d'échec.
        """
        raise NotImplementedError

    def read_new(self) -> pd.DataFrame:
        """
        Retourne les lignes apparues depuis le dernier appel (DataFrame avec
        colonne ``Date``). DataFrame vide s'il n'y a rien de nouveau.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Libère les ressources (connexion, fichier). Idempotent."""
        pass


# ======================================================================
# Lecture incrémentale d'un flux texte (logique partagée FTP / fichier)
# ======================================================================

class _IncrementalTextSource(LiveSource):
    """
    Base des sources qui lisent un flux texte qui grossit : conserve l'offset
    en octets déjà lus, l'en-tête, et la dernière ligne incomplète (un bloc
    peut se terminer au milieu d'une ligne, à compléter au prochain appel).

    Gère aussi la BASCULE de flux : si le fichier sous-jacent change (ex.
    nouveau fichier du jour à minuit), ``_current_source_key()`` renvoie une
    nouvelle valeur ; on repart alors du début du nouveau fichier afin de ne
    perdre aucune donnée.
    """

    def __init__(self):
        self._offset = 0          # octets déjà consommés
        self._header: Optional[List[str]] = None
        self._pending = ""        # fin de ligne incomplète conservée
        self._stream_key = object()  # identifiant du flux courant (sentinelle)

    # --- À implémenter par les sous-classes ---

    def _read_bytes_from(self, offset: int) -> bytes:
        """Lit les octets du flux à partir de ``offset``."""
        raise NotImplementedError

    def _byte_size(self) -> int:
        """Taille courante du flux en octets."""
        raise NotImplementedError

    def _head_text(self) -> str:
        """Renvoie le DÉBUT du flux (assez pour la ligne d'en-tête)."""
        raise NotImplementedError

    def _current_source_key(self):
        """
        Identifiant du flux courant. Quand il change, la base bascule sur le
        nouveau flux (lu depuis son début). Par défaut : flux unique et stable.
        """
        return "stream"

    # --- Logique commune ---

    def _read_header(self) -> Optional[List[str]]:
        text = self._head_text()
        lines = text.splitlines() if text else []
        if not lines:
            return None
        cols = log_parser.normalize_header(lines[0])
        return cols if "Date" in cols else None

    def open(self) -> List[str]:
        # On récupère l'en-tête puis on se positionne à la FIN du fichier : le
        # mode temps réel n'affiche que ce qui arrive à partir de maintenant,
        # pas l'historique déjà enregistré du jour.
        self._stream_key = self._current_source_key()
        header = self._read_header()
        if header is None:
            raise ValueError(
                "En-tête introuvable : la première ligne ne contient pas « Date ».")
        self._header = header
        self._offset = self._byte_size()
        self._pending = ""
        return self._header

    def read_new(self) -> pd.DataFrame:
        # Détection d'une bascule de fichier (ex. nouveau jour) : on relit le
        # nouveau fichier depuis son tout début (offset 0) pour tout capter.
        key = self._current_source_key()
        if key != self._stream_key:
            self._stream_key = key
            self._offset = 0
            self._pending = ""
            self._header = None

        # Détection d'une TRONCATURE / RÉÉCRITURE du fichier : si la taille a
        # diminué sous l'offset déjà lu, c'est que le fichier a été remis à zéro
        # (nouvelle session d'enregistrement, rotation, redémarrage). Sans cela,
        # la source attendrait indéfiniment un offset qui n'existe plus et
        # l'affichage ne reprendrait jamais. On repart donc du début du fichier.
        if self._byte_size() < self._offset:
            self._offset = 0
            self._pending = ""
            self._header = None

        # (Re)lecture paresseuse de l'en-tête : utile après une bascule, une
        # troncature, ou si le fichier du jour n'existait pas encore à l'ouverture.
        if self._header is None:
            header = self._read_header()
            if header is None:
                return pd.DataFrame()  # flux pas encore disponible
            self._header = header

        data = self._read_bytes_from(self._offset)
        if not data:
            return pd.DataFrame()
        self._offset += len(data)

        text = self._pending + data.decode("utf-8", errors="ignore")
        # Conserver une éventuelle dernière ligne incomplète pour la prochaine
        # lecture (l'automate peut écrire une ligne en deux fois).
        if not text.endswith("\n"):
            text, _, self._pending = text.rpartition("\n")
        else:
            self._pending = ""

        if not text.strip():
            return pd.DataFrame()
        # La ligne d'en-tête éventuellement relue après une bascule est
        # naturellement ignorée par parse_block (horodatage non valide).
        return log_parser.parse_block(text, self._header)


class SftpLiveSource(_IncrementalTextSource):
    """
    Lit le fichier de log sur l'automate Wago via SFTP (SSH, port 22).

    C'est le protocole réellement exposé par l'automate (le même que celui
    utilisé par WinSCP : SFTP-3). Nécessite le module ``paramiko``.

    Lecture incrémentale native : on ouvre le fichier distant, on se positionne
    à l'offset déjà lu (``seek``) et on lit le reste — seuls les octets ajoutés
    depuis le dernier cycle transitent.

    Deux modes, comme la source FTP :
    - ``remote_path`` fixe ;
    - ``daily=True`` : suit le « fichier du jour » et bascule à minuit.
    """

    def __init__(self, host: str, remote_path: Optional[str] = None,
                 user: Optional[str] = None, password: Optional[str] = None,
                 port: int = 22, timeout: float = 10.0,
                 daily: bool = False,
                 directory: str = PLC_LOG_DIR,
                 prefix: str = PLC_LOG_PREFIX,
                 date_fmt: str = PLC_LOG_DATE_FMT,
                 suffix: str = PLC_LOG_SUFFIX):
        super().__init__()
        if not remote_path and not daily:
            raise ValueError("Indiquer remote_path, ou activer daily=True.")
        self.host = host
        self._fixed_path = remote_path
        # Identifiants : ceux fournis, sinon issus de la configuration
        # (variables d'environnement / wago_plc.ini), jamais codés en dur.
        self.user = user if user is not None else plc_config.user()
        self.password = password if password is not None else plc_config.password()
        self.port = port
        self.timeout = timeout
        self.daily = daily
        self.directory = directory
        self.prefix = prefix
        self.date_fmt = date_fmt
        self.suffix = suffix
        self._ssh = None
        self._sftp = None

    def _current_source_key(self) -> str:
        if self.daily:
            return plc_log_path(directory=self.directory, prefix=self.prefix,
                                date_fmt=self.date_fmt, suffix=self.suffix)
        return self._fixed_path

    def open(self) -> List[str]:
        try:
            import paramiko
        except ImportError as e:
            raise RuntimeError(
                "Le module « paramiko » est requis pour le SFTP. "
                "Installez-le : pip install paramiko") from e

        # SSHClient : accepte automatiquement la clé d'hôte (comme WinSCP au
        # premier contact) et force l'authentification par mot de passe.
        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ssh.connect(
            self.host, port=self.port,
            username=self.user, password=self.password,
            timeout=self.timeout, look_for_keys=False, allow_agent=False)
        self._sftp = self._ssh.open_sftp()
        self._sftp.get_channel().settimeout(self.timeout)
        return super().open()

    def _byte_size(self) -> int:
        try:
            return int(self._sftp.stat(self._current_source_key()).st_size)
        except Exception:
            return self._offset  # fichier absent (ex. avant création du jour)

    def _read_bytes_from(self, offset: int) -> bytes:
        size = self._byte_size()
        if size <= offset:
            return b""
        try:
            with self._sftp.open(self._current_source_key(), "rb") as f:
                f.seek(offset)
                return f.read()
        except Exception:
            return b""

    def _head_text(self, nbytes: int = 8192) -> str:
        try:
            with self._sftp.open(self._current_source_key(), "rb") as f:
                return f.read(nbytes).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def close(self) -> None:
        for obj in (self._sftp, self._ssh):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._sftp = None
        self._ssh = None


class FileTailLiveSource(_IncrementalTextSource):
    """
    Lit un fichier LOCAL qui grossit en continu (« tail »).

    Sert à tester l'ensemble du pipeline temps réel (lecture incrémentale,
    parsing, fenêtre glissante) sans automate : il suffit d'alimenter le
    fichier avec ``tools/fake_plc_writer.py``.
    """

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def _current_source_key(self) -> str:
        return self.path

    def _byte_size(self) -> int:
        import os
        try:
            return os.path.getsize(self.path)
        except OSError:
            return self._offset

    def _read_bytes_from(self, offset: int) -> bytes:
        try:
            with open(self.path, "rb") as f:
                f.seek(offset)
                return f.read()
        except OSError:
            return b""

    def _head_text(self, nbytes: int = 8192) -> str:
        try:
            with open(self.path, "rb") as f:
                return f.read(nbytes).decode("utf-8", errors="ignore")
        except OSError:
            return ""


# ======================================================================
# Source simulée : signaux synthétiques à la cadence réelle (aucun fichier)
# ======================================================================

class SimulatedLiveSource(LiveSource):
    """
    Génère des signaux synthétiques à ``sample_hz`` (50 Hz par défaut, comme
    l'automate), à la cadence réelle : à chaque ``read_new()``, on produit
    exactement les échantillons correspondant au temps écoulé depuis l'ouverture.

    Signaux générés (mêmes noms que l'automate, pour exercer toute la chaîne) :
    - ``AICS``  : sinusoïde lente 0–10 V ;
    - ``AID3``  : tension dynamo → convertie en vitesse (≈ 2–38 km/h) par le
      pipeline d'affichage ;
    - ``DI44P`` : créneau 0,25 Hz ;
    - ``DIT/1`` : créneau 1 Hz ;
    - ``DIF/1`` : créneau rapide.
    """

    COLUMNS = ["AICS", "AID3", "DI44P", "DIT/1", "DIF/1"]

    # Calibration inverse vitesse → tension (cf. data.log_processing).
    _SPEED_GAIN = 0.316
    _VOLTAGE_OFFSET = 0.2197534

    def __init__(self, sample_hz: float = 50.0):
        self.sample_hz = float(sample_hz)
        self._start: Optional[pd.Timestamp] = None
        self._n_emitted = 0   # nombre d'échantillons déjà produits

    def open(self) -> List[str]:
        self._start = pd.Timestamp.now()
        self._n_emitted = 0
        return ["Date"] + self.COLUMNS

    def read_new(self) -> pd.DataFrame:
        if self._start is None:
            raise RuntimeError("Source non ouverte (appeler open() d'abord).")

        elapsed = (pd.Timestamp.now() - self._start).total_seconds()
        target = int(elapsed * self.sample_hz)
        if target <= self._n_emitted:
            return pd.DataFrame()

        idx = np.arange(self._n_emitted, target)
        self._n_emitted = target
        t = idx / self.sample_hz  # secondes depuis le début

        dates = self._start + pd.to_timedelta(t, unit="s")

        aics = 5.0 + 4.0 * np.sin(2 * np.pi * 0.2 * t)

        # Vitesse cible (km/h) → tension AID3 attendue par le pipeline.
        speed = 20.0 + 18.0 * np.sin(2 * np.pi * 0.05 * t)
        aid3 = speed / self._SPEED_GAIN + self._VOLTAGE_OFFSET

        di44p = ((t % 4.0) < 2.0).astype(float)
        dit1 = ((t % 1.0) < 0.5).astype(float)
        dif1 = ((t % 0.4) < 0.2).astype(float)

        return pd.DataFrame({
            "Date": dates,
            "AICS": aics,
            "AID3": aid3,
            "DI44P": di44p,
            "DIT/1": dit1,
            "DIF/1": dif1,
        })


# ======================================================================
# Dépôt de fichiers de log : liste + téléchargement (≠ lecture temps réel)
# ======================================================================
#
# Sert à la fonction « Enregistrer les fichiers » : on parcourt le répertoire
# de logs de l'automate, l'utilisateur choisit les fichiers à récupérer, puis
# on les copie sur son ordinateur. C'est une opération ponctuelle (fichiers
# complets), distincte du suivi incrémental ``LiveSource`` ci-dessus.

class LogRepository:
    """Accès en lecture seule à un dépôt de fichiers de log."""

    def list_files(self) -> List[Tuple[str, int]]:
        """Liste des fichiers disponibles : couples ``(nom, taille_octets)``."""
        raise NotImplementedError

    def download(self, name: str, dest_path: str) -> None:
        """Copie le fichier ``name`` du dépôt vers ``dest_path`` (local)."""
        raise NotImplementedError

    def close(self) -> None:
        """Libère les ressources (connexion). Idempotent."""
        pass


class SftpLogRepository(LogRepository):
    """
    Dépôt distant : le répertoire de logs de l'automate Wago, via SFTP.

    La connexion (paramiko) est ouverte paresseusement au premier accès et
    réutilisée pour le listing puis les téléchargements successifs.
    """

    def __init__(self, host: str, user: Optional[str] = None,
                 password: Optional[str] = None,
                 directory: str = PLC_LOG_DIR, port: int = 22,
                 timeout: float = 10.0):
        self.host = host
        # Identifiants : ceux fournis, sinon issus de la configuration
        # (variables d'environnement / wago_plc.ini), jamais codés en dur.
        self.user = user if user is not None else plc_config.user()
        self.password = password if password is not None else plc_config.password()
        self.directory = directory
        self.port = port
        self.timeout = timeout
        self._ssh = None
        self._sftp = None

    def _connect(self) -> None:
        if self._sftp is not None:
            return
        try:
            import paramiko
        except ImportError as e:
            raise RuntimeError(
                "Le module « paramiko » est requis pour le SFTP. "
                "Installez-le : pip install paramiko") from e
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            self.host, port=self.port,
            username=self.user, password=self.password,
            timeout=self.timeout, look_for_keys=False, allow_agent=False)
        sftp = ssh.open_sftp()
        sftp.get_channel().settimeout(self.timeout)
        self._ssh, self._sftp = ssh, sftp

    def list_files(self) -> List[Tuple[str, int]]:
        import stat
        self._connect()
        files: List[Tuple[str, int]] = []
        for attr in self._sftp.listdir_attr(self.directory):
            # On ignore les sous-répertoires : seuls les fichiers nous intéressent.
            if stat.S_ISDIR(attr.st_mode):
                continue
            files.append((attr.filename, int(attr.st_size)))
        files.sort(key=lambda f: f[0])
        return files

    def download(self, name: str, dest_path: str) -> None:
        self._connect()
        self._sftp.get(f"{self.directory}/{name}", dest_path)

    def close(self) -> None:
        for obj in (self._sftp, self._ssh):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._sftp = None
        self._ssh = None


class LocalLogRepository(LogRepository):
    """
    Dépôt LOCAL : un répertoire de l'ordinateur tient lieu de carte mémoire de
    l'automate. Sert à tester la fonction « Enregistrer les fichiers » SANS
    automate (cf. ``tools/test_save_logs_dialog.py``).
    """

    def __init__(self, directory: str):
        self.directory = directory

    def list_files(self) -> List[Tuple[str, int]]:
        import os
        files: List[Tuple[str, int]] = []
        for name in os.listdir(self.directory):
            path = os.path.join(self.directory, name)
            if os.path.isfile(path):
                files.append((name, os.path.getsize(path)))
        files.sort(key=lambda f: f[0])
        return files

    def download(self, name: str, dest_path: str) -> None:
        import os
        import shutil
        shutil.copy2(os.path.join(self.directory, name), dest_path)
