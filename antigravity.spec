# -*- mode: python ; coding: utf-8 -*-
"""Antigravity Tools - PyInstaller 打包配置

用法:
    cd F:\自制工具\antigravity-tools
    venv\Scripts\pyinstaller.exe antigravity.spec

输出:
    dist/Antigravity Tools/Antigravity Tools.exe  (目录模式，可分发)
"""

import os
import sys
from PySide6 import QtCore

block_cipher = None

# 项目根目录
ROOT = os.path.abspath(SPECPATH)

# ─── PySide6 运行时路径 ───
pyside6_dir = os.path.dirname(QtCore.__file__)
plugins_dir = os.path.join(pyside6_dir, 'plugins')
translations_dir = os.path.join(pyside6_dir, 'translations')

# ─── 只收集我们实际需要的 Qt 模块 ───
# 我们只用了 QtWidgets, QtCore, QtGui, QtNetwork, QtSvg — 不需要 WebEngine/Quick/QML 等
needed_qt_dlls = {
    'Qt6Core', 'Qt6Gui', 'Qt6Widgets', 'Qt6Network', 'Qt6Svg', 'Qt6OpenGL',
    'pyside6', 'shiboken6',
}
needed_qt_pyds = {
    'QtCore', 'QtGui', 'QtWidgets', 'QtNetwork', 'QtSvg', 'QtOpenGL',
}

# 只收集需要的 DLL 和 PYD
pyside6_bins = []
for root, dirs, files in os.walk(pyside6_dir):
    for f in files:
        if not f.endswith(('.dll', '.pyd')):
            continue
        # DLL: 只保留需要的
        base = f.replace('.dll', '').replace('.pyd', '')
        if f.endswith('.dll'):
            # 保留 Qt6Core/Gui/Widgets/Network/Svg/OpenGL + shiboken6/pyside6 + python相关
            keep = any(k.lower() in base.lower() for k in needed_qt_dlls)
            keep |= base.lower() in ('libpyside6', 'shiboken6')
            keep |= 'python' in base.lower()  # python311.dll等
            keep |= base.lower().startswith('icu')  # ICU是Qt6Core的依赖
            keep |= base.lower() in ('libcrypto-3-x64', 'libssl-3-x64')  # QtNetwork依赖
            # MSVC 运行时 DLL — 必须打包到根目录，否则客户机缺 VC++ Redist 就崩
            keep |= base.lower() in ('vcruntime140', 'vcruntime140_1',
                                     'msvcp140', 'msvcp140_1', 'msvcp140_2')
            # OpenGL 软件渲染兜底 — 客户机没显卡驱动时用这个
            keep |= base.lower() == 'opengl32sw'
            if not keep:
                continue
        elif f.endswith('.pyd'):
            if not any(k in base for k in needed_qt_pyds):
                continue
        # 所有 DLL/PYD 都放到 _internal/ 根目录（不要放到 PySide6/ 子目录）
        # 这样 PyInstaller 加载时能正确找到依赖链
        pyside6_bins.append((os.path.join(root, f), '.'))

# ─── 数据文件 ───
datas = [
    # src 包（所有源码）
    (os.path.join(ROOT, 'src'), 'src'),
    # VERSION 文件
    (os.path.join(ROOT, 'src', 'VERSION'), 'src'),
    # 应用图标
    (os.path.join(ROOT, 'assets', 'icons'), 'assets/icons'),
]

# ─── Playwright driver（内置到软件包中，无需运行时下载）───
# Python 模块由 hiddenimports 处理，这里只打包 driver（node.exe + playwright-core JS）
playwright_dir = None
_site_packages = os.path.join(os.path.dirname(os.path.dirname(pyside6_dir)), 'Lib', 'site-packages')
if os.path.isdir(os.path.join(_site_packages, 'playwright')):
    playwright_dir = os.path.join(_site_packages, 'playwright')
else:
    try:
        import importlib
        pw = importlib.import_module('playwright')
        playwright_dir = os.path.dirname(pw.__file__)
    except ImportError:
        pass

if playwright_dir and os.path.isdir(os.path.join(playwright_dir, 'driver')):
    # 只打包 driver 目录（node.exe 88MB + package 15MB = ~103MB）
    # Python API 模块由 hiddenimports 自动收集
    datas.append((os.path.join(playwright_dir, 'driver'), 'playwright/driver'))

# 只收集必要的 Qt 插子目录
# 插件放到 PySide6/plugins/ 下，与 qt.conf 中的 Prefix 路径对应
needed_plugin_dirs = ['platforms', 'imageformats', 'styles', 'tls']
if os.path.isdir(plugins_dir):
    for d in needed_plugin_dirs:
        sub = os.path.join(plugins_dir, d)
        if os.path.isdir(sub):
            datas.append((sub, os.path.join('PySide6', 'plugins', d)))

# ─── qt.conf — 告诉 Qt 运行时去哪里找 plugins 和 translations ───
# 没有这个文件，PyInstaller 打包后 Qt 找不到 platforms 插件 → "找不到指定的模块"
qt_conf_dir = os.path.join(ROOT, 'build', 'qt_conf')
os.makedirs(qt_conf_dir, exist_ok=True)
with open(os.path.join(qt_conf_dir, 'qt.conf'), 'w', encoding='utf-8') as f:
    f.write('[Paths]\n')
    f.write('Prefix = PySide6\n')
    f.write('Plugins = plugins\n')
    f.write('Translations = translations\n')
datas.append((qt_conf_dir, '.'))

# 只保留中文翻译 + Qt基础翻译
zh_trans_dir = os.path.join(ROOT, 'build', 'zh_translations')
os.makedirs(zh_trans_dir, exist_ok=True)
if os.path.isdir(translations_dir):
    for f in os.listdir(translations_dir):
        # 只保留 qt 中文翻译 + pyside6 翻译
        if 'zh_CN' in f or 'qtbase_' in f.lower() or f.startswith('pyside6'):
            import shutil
            shutil.copy2(os.path.join(translations_dir, f), os.path.join(zh_trans_dir, f))
    datas.append((zh_trans_dir, 'PySide6/translations'))

# ─── 隐式导入（PyInstaller 可能分析不到的）───
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
    'winreg',
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
    # Playwright 全系列（已内置 driver，无需运行时安装）
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
        # 不需要的 Qt 模块（大幅减小体积）
        'PySide6.QtWebEngine', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtQml',
        'PySide6.QtDesigner', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
        'PySide6.Qt3D', 'PySide6.QtMultimedia', 'PySide6.QtTextToSpeech',
        'PySide6.QtBluetooth', 'PySide6.QtPositioning', 'PySide6.QtLocation',
        'PySide6.QtSensors', 'PySide6.QtSerialPort', 'PySide6.QtSql',
        'PySide6.QtXml', 'PySide6.QtTest', 'PySide6.QtHelp',
        'PySide6.QtPrintSupport', 'PySide6.QtCharts',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ─── PYZ ───
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ─── EXE (目录模式，便于分发和调试) ───
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
    console=False,  # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, 'assets', 'icons', 'app.ico'),  # 应用图标
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
