# -*- mode: python ; coding: utf-8 -*-
"""
Agent 项目 PyInstaller 打包配置
使用方式:
    pyinstaller --clean main.spec
"""

import os
import sys

block_cipher = None

# 项目根目录
PROJECT_ROOT = os.path.abspath('.')

# 需要收集的隐式导入
hiddenimports = [
    # FastAPI / Uvicorn 相关
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.http.httptools_impl',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.protocols.websockets.websockets_impl',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'uvicorn.lifespan.off',
    'anyio',
    'anyio._backends',
    'anyio._backends._asyncio',
    'h11',
    'httptools',
    'multipart',

    # Pydantic
    'pydantic',
    'pydantic_core',

    # Redis
    'redis',
    'redis.connection',
    'redis.client',

    # psutil
    'psutil',

    # YAML
    'yaml',

    # requests
    'requests',
    'urllib3',
    'urllib3.util',
    'charset_normalizer',
    'certifi',
    'idna',

    # 项目自身模块
    'core',
    'core.heartbeat',
    'core.manager',
    'core.register',
    'core.task',
    'api',
    'api.app',
    'api.routes',
    'api.routes.agent',
    'api.routes.system',
    'api.routes.task',
    'utils',
    'utils.config_loader',
    'utils.redis_client',
    'utils.redis_store',
    'utils.util',

    # GPUtil（心跳模块依赖）
    'GPUtil',

    # 标准库
    'multiprocessing',
    'multiprocessing.spawn',
    'encodings',
    'encodings.idna',
]

# 需要包含的数据文件 (config.yaml)
datas = [
    (os.path.join(PROJECT_ROOT, 'config.yaml'), '.'),
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, 'main.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'IPython',
        'jupyter',
        'notebook',
        'pytest',
        'black',
        'flake8',
        'pyinstaller',
    ],
    # 关键：打包时优先使用自带的 DLL，不依赖系统环境
    win_no_prefer_redirects=True,
    win_private_assemblies=True,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # 关闭 UPX 压缩，减少在其他系统上的兼容性问题
    upx=False,
    console=True,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Agent',
)
