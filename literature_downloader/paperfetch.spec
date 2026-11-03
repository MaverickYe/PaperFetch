# -*- mode: python ; coding: utf-8 -*-
"""One-file build used by the Unix packaging scripts."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all

HERE = Path(SPECPATH)
project_root = HERE.parent
runtime_tmpdir = str(project_root / '.paperfetch_runtime')

pymupdf_datas, pymupdf_binaries, pymupdf_hidden = collect_all('pymupdf')
playwright_datas, playwright_binaries, playwright_hidden = collect_all('playwright')
datas = [(str(HERE / 'index.html'), '.'), (str(HERE / 'assets'), 'assets'), *pymupdf_datas, *playwright_datas]
binaries = [*pymupdf_binaries, *playwright_binaries]
hiddenimports = ['tkinter', '_tkinter', *pymupdf_hidden, *playwright_hidden]

a = Analysis([str(HERE / 'server.py')], pathex=[str(project_root)], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='PaperFetch', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, runtime_tmpdir=runtime_tmpdir)
