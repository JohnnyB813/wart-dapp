# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from pathlib import Path
from PyInstaller.utils.win32.versioninfo import VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct

version = Path('version.txt').read_text(encoding='utf-8').strip()
version_numbers = tuple(map(int, version.split('-')[0].split('.'))) + (0,)
version_resource = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_numbers, prodvers=version_numbers, mask=0x3f,
                      flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable('040904B0', [
        StringStruct('CompanyName', 'Warthog Wallet contributors'),
        StringStruct('FileDescription', 'Warthog mainnet and DeFi testnet desktop wallet'),
        StringStruct('FileVersion', version), StringStruct('ProductVersion', version),
        StringStruct('ProductName', 'Warthog Wallet'),
    ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])],
)

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=collect_data_files('mnemonic') + [('version.txt', '.'), ('build/generated/build-info.json', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PIL', 'zxingcpp'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f'wart-dapp-{version}-windows-x64',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='wart_icon.ico',
    version=version_resource,
)
