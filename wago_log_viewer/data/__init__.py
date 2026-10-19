"""
Sous-package « ui » : interface graphique PySide6 / pyqtgraph.

Modules :
- ``main_window``      : fenêtre principale (assemble tous les composants).
- ``plot_pane``        : zone de tracé (graphiques empilés ou superposés),
  downsampling et curseurs verticaux.
- ``channel_selector`` : liste de canaux à cocher, réordonnables par glisser-déposer.
- ``cursor_info_pane`` : bandeau affichant les valeurs sous les curseurs C1/C2.
- ``range_dialog``     : boîte de dialogue de réglage des plages Y.
- ``workers``          : chargement des fichiers dans un thread séparé.
- ``theme``            : thème sombre de l'application.
"""
