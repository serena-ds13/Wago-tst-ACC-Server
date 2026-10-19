"""
Point d'entrée du Wago Log Viewer.

Lancement en développement :
    python main.py

Génération de l'exécutable Windows (voir README) :
    pyinstaller WagoLogViewer.spec
"""

import sys

from PySide6 import QtWidgets

from wago_log_viewer.ui.main_window import MainWindow


def main():
    # pyi_splash n'existe que dans l'exécutable PyInstaller (écran de
    # démarrage) : en développement, l'import échoue silencieusement.
    try:
        import pyi_splash
        pyi_splash.update_text("Chargement de l'interface...")
        pyi_splash.close()
    except (ImportError, AttributeError):
        pass

    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
