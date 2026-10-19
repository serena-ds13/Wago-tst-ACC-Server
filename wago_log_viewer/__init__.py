"""
Wago Log Viewer — Visualiseur de logs d'automate Wago (STIB-MIVB).

Le package est organisé en deux sous-packages :

- ``wago_log_viewer.data`` : lecture, fusion et transformation des fichiers
  de logs (aucune dépendance à l'interface graphique → testable isolément).
- ``wago_log_viewer.ui``   : interface graphique PySide6 / pyqtgraph.

Point d'entrée de l'application : ``main.py`` à la racine du projet.
"""

__version__ = "5.1"
