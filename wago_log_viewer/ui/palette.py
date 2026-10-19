"""
Palette de couleurs des signaux, PARTAGÉE par tous les panes (Signaux,
Superposition, Temps réel).

Garanties :
- **Même couleur partout** : ``color_for_channel`` est une fonction PURE du nom
  du canal — un signal donné a toujours la même couleur, quel que soit le pane,
  la sélection ou le fichier chargé (aucune dépendance à l'ordre ou au nombre).
- **Adaptée au daltonisme (CVD)** : couleurs dérivées de la palette Okabe-Ito
  (référence pour le daltonisme), déclinées en deux niveaux de LUMINOSITÉ. Deux
  signaux partageant une teinte se distinguent alors par la clarté — repère
  fiable pour les daltoniens, qui s'appuient sur la luminosité. Avec 16 signaux,
  une séparation parfaite pour tous les types de CVD reste impossible : le NOM
  du signal est donc toujours affiché (étiquette/légende) comme repère sûr.
- **Lisibles sur fond noir** : aucune couleur sombre ni proche du noir.
"""

import hashlib

from PySide6 import QtGui

#: Signaux connus du data-recorder STIB → couleur dédiée (stable).
#: Noms normalisés par ``_base`` (préfixe AI/DI retiré, majuscules).
KNOWN_ORDER = [
    "CS", "IMOT1", "IMOT2", "VLIGNE1", "VITESSE",
    "44P-LMV", "T/1", "T/2", "F/1", "F/2",
    "SD/1", "SD/2", "LO/1", "LO/2", "SH/1", "SH/2",
]
_KNOWN_INDEX = {name: i for i, name in enumerate(KNOWN_ORDER)}

#: Palette CVD : 8 teintes Okabe-Ito (adaptées au fond noir) déclinées en deux
#: niveaux de luminosité (teinte « foncée » puis « claire »). L'ordre alterne
#: les teintes pour que deux signaux successifs diffèrent par la teinte, et que
#: les deux signaux d'une même teinte diffèrent par la clarté.
PALETTE = [
    # --- niveau 1 (teintes Okabe-Ito) ---
    "#E69F00",  # orange
    "#56B4E9",  # bleu ciel
    "#009E73",  # vert
    "#E8C84D",  # or
    "#3D85C6",  # bleu
    "#D55E00",  # vermillon
    "#C879A7",  # violet
    "#9AA0A6",  # gris
    # --- niveau 2 (mêmes teintes, plus claires) ---
    "#FFC862",  # orange clair
    "#A5D8F3",  # bleu ciel clair
    "#5CCBA7",  # vert clair
    "#F5EE9E",  # jaune clair
    "#8FBEE8",  # bleu clair
    "#F08A4B",  # vermillon clair
    "#E3B3CE",  # violet clair
    "#D9DCE0",  # gris clair
]


def _base(name: str) -> str:
    """Nom normalisé : retire le préfixe AI/DI et met en majuscules."""
    n = name.strip()
    if n[:2] in ("AI", "DI"):
        n = n[2:]
    return n.upper()


def _slot(name: str) -> int:
    """Index de couleur stable (connu → fixe ; sinon hachage stable du nom)."""
    base = _base(name)
    if base in _KNOWN_INDEX:
        return _KNOWN_INDEX[base]
    # SHA-256 : simple hachage stable du nom pour choisir un index de couleur
    # (usage non cryptographique), conforme à la reco SonarQube.
    digest = int(hashlib.sha256(base.encode("utf-8")).hexdigest(), 16)
    return digest % len(PALETTE)


def color_for_channel(name: str) -> QtGui.QColor:
    """Couleur (QColor) d'un canal — STABLE et identique dans tous les panes."""
    return QtGui.QColor(PALETTE[_slot(name) % len(PALETTE)])


def color_map_for(channels) -> dict:
    """Dictionnaire ``{nom_canal: QColor}`` pour une liste de noms de canaux."""
    return {c: color_for_channel(c) for c in channels}
