# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path

block_cipher = None

# spec 文件在 packaging/ 子目录，项目根目录是其父目录
ROOTDIR = os.path.abspath(os.path.join(SPECPATH, '..'))

# 从 version.py 读取版本号
version_file = os.path.join(ROOTDIR, 'socket_server', 'version.py')
version_vars = {}
with open(version_file) as f:
    exec(f.read(), version_vars)
VERSION = version_vars.get('VERSION', '0.0.0')

# 打包前从 CHANGELOG.md 生成 release_notes.py（内置 notes，不联网）
_notes_gen = os.path.join(ROOTDIR, 'packaging', 'generate_release_notes.py')
if os.path.isfile(_notes_gen):
    import subprocess
    subprocess.run([sys.executable, _notes_gen], cwd=ROOTDIR, check=False)

a = Analysis(
    [os.path.join(ROOTDIR, 'packaging', 'entry.py')],
    pathex=[ROOTDIR],
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
        'socket_server.release_notes',
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
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
