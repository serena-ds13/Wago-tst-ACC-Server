"""
Configuration de la connexion à l'automate Wago (hôte, identifiants).

AUCUN identifiant ni adresse IP n'est stocké en dur dans le code source
(recommandation SonarQube : sortir les secrets du dépôt de code). Les valeurs
sont résolues, par ordre de priorité :

1. Variables d'environnement :
   ``WAGO_PLC_HOST``, ``WAGO_PLC_USER``, ``WAGO_PLC_PASSWORD``.
2. Fichier de configuration local ``wago_plc.ini`` (section ``[plc]``), cherché
   à côté de l'exécutable (build PyInstaller), dans le répertoire courant, puis
   à la racine du projet. Ce fichier N'EST PAS versionné (voir ``.gitignore``) :
   il contient les paramètres propres au site. Un modèle est fourni :
   ``wago_plc.ini.example``.
3. Chaîne vide si rien n'est défini : l'utilisateur saisit alors les valeurs
   directement dans l'interface (onglet « Temps réel »).
"""

import configparser
import os
import sys
from pathlib import Path
from typing import Optional

#: Clé de config → nom de la variable d'environnement associée.
_ENV_NAMES = {
    "host": "WAGO_PLC_HOST",
    "user": "WAGO_PLC_USER",
    "password": "WAGO_PLC_PASSWORD",
}

_CONFIG_FILENAME = "wago_plc.ini"
_SECTION = "plc"


def _config_file() -> Optional[Path]:
    """Premier ``wago_plc.ini`` trouvé parmi les emplacements standard, ou None."""
    candidates = []
    # Exécutable gelé (PyInstaller) : fichier posé à côté du .exe.
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / _CONFIG_FILENAME)
    # Répertoire de lancement.
    candidates.append(Path.cwd() / _CONFIG_FILENAME)
    # Racine du projet (deux niveaux au-dessus de wago_log_viewer/data/).
    candidates.append(Path(__file__).resolve().parents[2] / _CONFIG_FILENAME)
    for path in candidates:
        if path.is_file():
            return path
    return None


def _from_file(key: str) -> str:
    path = _config_file()
    if path is None:
        return ""
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
        return parser.get(_SECTION, key, fallback="").strip()
    except (configparser.Error, OSError):
        return ""


def get(key: str, default: str = "") -> str:
    """Valeur de config : variable d'environnement, sinon fichier, sinon ``default``."""
    env_name = _ENV_NAMES.get(key)
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return value.strip()
    value = _from_file(key)
    return value if value else default


def host() -> str:
    """Hôte (IP ou nom) de l'automate. Vide si non configuré."""
    return get("host")


def user() -> str:
    """Nom d'utilisateur SFTP. Vide si non configuré."""
    return get("user")


def password() -> str:
    """Mot de passe SFTP. Vide si non configuré."""
    return get("password")
