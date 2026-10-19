# -*- mode: python ; coding: utf-8 -*-
# Configuration PyInstaller — génère un exécutable Windows autonome.
# Usage : pyinstaller WagoLogViewer.spec
# Résultat : dist/WagoLogViewer_v<version>.exe

from PyInstaller.utils.hooks import collect_submodules

from wago_log_viewer import __version__

# paramiko (SFTP temps réel) embarque des modules chargés dynamiquement ;
# on les collecte explicitement pour qu'ils soient présents dans l'exécutable.
hiddenimports = collect_submodules('paramiko')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Écran de démarrage affiché pendant le chargement de l'application.
splash = Splash(
    'assets\\LogViewerBackground.PNG',
    binaries=a.binaries,
    datas=a.datas,
    text_pos=None,
    text_size=12,
    minify_script=True,
    always_on_top=True,
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    splash,
    splash.binaries,
    [],
    name=f'WagoLogViewer_v{__version__}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\STIB_1.ico'],
)
