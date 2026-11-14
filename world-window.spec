from pathlib import Path

root = Path(SPECPATH)
a = Analysis([str(root / 'world_window.py')], pathex=[str(root)],
             binaries=[], datas=[], hiddenimports=[], hookspath=[],
             hooksconfig={}, runtime_hooks=[],
             excludes=['tkinter', 'PySide6.QtQml', 'PySide6.QtQuick'],
             noarchive=False, optimize=0)
# Qt 6.12 uses Windows ICU. Do not bundle a similarly named, incompatible
# ICU from another application found on PATH.
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() not in ('icuuc.dll', 'icudt78.dll')]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='WorldWindow',
          debug=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='WorldWindow')
