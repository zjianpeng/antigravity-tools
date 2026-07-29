# -*- mode: python ; coding: utf-8 -*-
"""Antigravity Tools - macOS PyInstaller 打包配置

用法:
    cd /path/to/antigravity-tools-mac
    python3 -m PyInstaller antigravity-mac.spec

输出:
    dist/Antigravity Tools.app  (macOS .app bundle)
"""

import os
import sys
import platform
from PySide6 import QtCore

block_cipher = None

# 项目根目录
ROOT = os.path.abspath(SPECPATH)

# ─── PySide6 运行时路径 ───
pyside6_dir = os.path.dirname(QtCore.__file__)
plugins_dir = os.path.join(pyside6_dir, 'plugins')
translations_dir = os.path.join(pyside6_dir, 'translations')

# ─── 只收集我们实际需要的 Qt 模块 ───
needed_qt_dylibs = {
    'Qt6Core', 'Qt6Gui', 'Qt6Widgets', 'Qt6Network', 'Qt6Svg', 'Qt6OpenGL',
    'pyside6', 'shiboken6',
}
needed_qt_pyds = {
    'QtCore', 'QtGui', 'QtWidgets', 'QtNetwork', 'QtSvg', 'QtOpenGL',
}

# 只收集需要的 dylib 和 so
pyside6_bins = []
for root, dirs, files in os.walk(pyside6_dir):
    for f in files:
        if not f.endswith(('.dylib', '.so', '.pyd')):
            continue
        base = f.replace('.dylib', '').replace('.so', '').replace('.pyd', '')
        if f.endswith('.dylib'):
            keep = any(k.lower() in base.lower() for k in needed_qt_dylibs)
            keep |= base.lower() in ('libpyside6', 'shiboken6')
            keep |= 'python' in base.lower()
            keep |= base.lower().startswith('libicu')
            keep |= base.lower().startswith('ssl')
            keep |= base.lower().startswith('crypto')
            if not keep:
                continue
        elif f.endswith(('.so', '.pyd')):
            if not any(k in base for k in needed_qt_pyds):
                continue
        rel = os.path.relpath(os.path.join(root, f), pyside6_dir)
        pyside6_bins.append((os.path.join(root, f), os.path.dirname(rel) if os.path.dirname(rel) else '.'))

# ─── 数据文件 ───
datas = [
    # src 包（所有源码）
    (os.path.join(ROOT, 'src'), 'src'),
    # VERSION 文件
    (os.path.join(ROOT, 'src', 'VERSION'), 'src'),
]

# ─── Playwright driver ───
playwright_dir = None
try:
    import importlib
    pw = importlib.import_module('playwright')
    playwright_dir = os.path.dirname(pw.__file__)
except ImportError:
    pass

if playwright_dir and os.path.isdir(os.path.join(playwright_dir, 'driver')):
    datas.append((os.path.join(playwright_dir, 'driver'), 'playwright/driver'))

# 只收集必要的 Qt 插子目录
needed_plugin_dirs = ['platforms', 'imageformats', 'styles', 'tls']
if os.path.isdir(plugins_dir):
    for d in needed_plugin_dirs:
        sub = os.path.join(plugins_dir, d)
        if os.path.isdir(sub):
            datas.append((sub, os.path.join('PySide6', 'plugins', d)))

# 中文翻译
zh_trans_dir = os.path.join(ROOT, 'build', 'zh_translations')
os.makedirs(zh_trans_dir, exist_ok=True)
if os.path.isdir(translations_dir):
    for f in os.listdir(translations_dir):
        if 'zh_CN' in f or 'qtbase_' in f.lower() or f.startswith('pyside6'):
            import shutil
            shutil.copy2(os.path.join(translations_dir, f), os.path.join(zh_trans_dir, f))
    datas.append((zh_trans_dir, 'PySide6/translations'))

# ─── 隐式导入 ───
hiddenimports = [
    # 标准库（src 以 datas 源码形式打包，import 图分析不到，必须显式列出）
    'sqlite3',
    'http.server',
    'socketserver',
    'plistlib',
    'webbrowser',
    'secrets',
    'select',
    'atexit',
    'ssl',
    'hashlib',
    'concurrent.futures',
    'urllib.parse',
    'logging.handlers',
    'PySide6.QtWidgets',
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtNetwork',
    'PySide6.QtSvg',
    'PySide6.QtOpenGL',
    'shiboken6',
    'requests',
    'urllib3',
    'certifi',
    'charset_normalizer',
    'idna',
    'aiohttp',
    'aiohappyeyeballs',
    'aiosignal',
    'frozenlist',
    'multidict',
    'yarl',
    'propcache',
    'cryptography',
    'cffi',
    'pycparser',
    'playwright',
    'playwright.async_api',
    'playwright.sync_api',
    'playwright._impl',
    'playwright._impl._api_structures',
    'playwright._impl._api_types',
    'playwright._impl._browser',
    'playwright._impl._browser_context',
    'playwright._impl._browser_type',
    'playwright._impl._cdp_session',
    'playwright._impl._connection',
    'playwright._impl._element_handle',
    'playwright._impl._frame',
    'playwright._impl._helper',
    'playwright._impl._network',
    'playwright._impl._page',
    'playwright._impl._playwright',
    'playwright._impl._transport',
    'greenlet',
    'pyee',
    'pyee._base',
]

# ─── 二进制文件 ───
binaries = pyside6_bins

# ─── Analysis ───
a = Analysis(
    [os.path.join(ROOT, 'app.py')],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy',
        'PIL', 'cv2', 'sqlalchemy', 'django', 'flask',
        'unittest', 'xmlrunner', 'pytest',
        'PySide6.QtWebEngine', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtQml',
        'PySide6.QtDesigner', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
        'PySide6.Qt3D', 'PySide6.QtMultimedia', 'PySide6.QtTextToSpeech',
        'PySide6.QtBluetooth', 'PySide6.QtPositioning', 'PySide6.QtLocation',
        'PySide6.QtSensors', 'PySide6.QtSerialPort', 'PySide6.QtSql',
        'PySide6.QtXml', 'PySide6.QtTest', 'PySide6.QtHelp',
        'PySide6.QtPrintSupport', 'PySide6.QtCharts',
        # Windows-only
        'win32crypt', 'win32api', 'win32con', 'pywintypes', 'pythoncom', 'win32com',
    ],
    cipher=block_cipher,
    noarchive=False,
)

# ─── PYZ ───
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ─── EXE (macOS .app bundle) ───
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Antigravity Tools',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=os.environ.get('PYINSTALLER_TARGET_ARCH', None),  # Intel构建传 x86_64，ARM构建不传
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# ─── COLLECT (目录模式) ───
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Antigravity Tools',
)

# ─── macOS .app bundle ───
app = BUNDLE(
    coll,
    name='Antigravity Tools.app',
    icon=None,
    bundle_identifier='com.antigravity.tools',
    info_plist={
        'CFBundleName': 'Antigravity Tools',
        'CFBundleDisplayName': 'Antigravity Tools',
        'CFBundleIdentifier': 'com.antigravity.tools',
        'CFBundleVersion': '2.0.0',
        'CFBundleShortVersionString': '2.0.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '10.15',
        'NSRequiresAquaSystemAppearance': False,
    },
)
