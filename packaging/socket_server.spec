# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

block_cipher = None

# 从 version.py 读取版本号
version_file = os.path.join(os.path.dirname(SPECPATH), '..', 'socket_server', 'version.py')
version_vars = {}
with open(version_file) as f:
    exec(f.read(), version_vars)
VERSION = version_vars.get('VERSION', '0.0.0')

a = Analysis(
    ['socket_server/__main__.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'socket_server',
        'socket_server.cli',
        'socket_server.version',
        'socket_server.server',
        'socket_server.protocol',
        'socket_server.handlers',
        'socket_server.netutils',
        'socket_server.replayer',
        'socket_server.capture',
        'socket_server.boce',
        'socket_server.socket_listen',
        'socket_server.pcap_flow',
        'socket_server.upgrader',
        'socket_server.supervisor',
        'socket_server.autoupgrade',
        'scapy.all',
        'scapy.layers.l2',
        'scapy.layers.inet',
        'scapy.layers.inet6',
        'scapy.layers.dhcp',
        'scapy.layers.dns',
        'scapy.layers.http',
        'scapy.layers.tls',
        'scapy.layers.vxlan',
        'scapy.layers.vrrp',
        'pyppeteer',
        'packaging',
        'requests',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='socket_server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
