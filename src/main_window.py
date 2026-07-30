"""主窗口 - Antigravity Tools 桌面应用"""

import logging
import os
import sys
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QStackedWidget, QSystemTrayIcon,
    QMenu, QApplication,
)
from PySide6.QtGui import QIcon, QAction
from PySide6.QtCore import Qt, QSize

from .ui import Sidebar, get_stylesheet
from .ui.pages import (
    DashboardPage, AccountsPage, CheckinPage,
    SettingsPage, ApiProxyPage, ChangelogPage,
)
from .i18n import t
from .utils.store import init_db, load_setting

logger = logging.getLogger(__name__)


def get_current_version() -> str:
    """读取当前版本号（src/VERSION）"""
    version_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
    if os.path.exists(version_file):
        try:
            with open(version_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return "2.0.1"


class MainWindow(QMainWindow):
    """主窗口"""

    def __init__(self):
        super().__init__()
        self._update_version_suffix()
        self.setWindowTitle("⚡ Antigravity Tools")
        self.setMinimumSize(QSize(1100, 700))
        self.resize(1200, 800)

        # 初始化数据库
        init_db()

        # 加载设置
        self._current_theme = load_setting("theme", "system")

        # 构建UI
        self._setup_ui()
        self._setup_tray()
        self.apply_theme(self._current_theme)

    def _update_version_suffix(self):
        """更新窗口标题中的版本号"""
        ver = get_current_version()
        self.setWindowTitle(f"⚡ Antigravity Tools v{ver}")

    def _setup_ui(self):
        """构建主界面"""
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 侧边栏
        self._sidebar = Sidebar()
        self._sidebar.page_changed.connect(self._switch_page)
        layout.addWidget(self._sidebar)

        # 页面堆栈
        self._stack = QStackedWidget()
        self._pages = {
            "dashboard": DashboardPage(),
            "accounts": AccountsPage(),
            "checkin": CheckinPage(),
            "api_proxy": ApiProxyPage(),
            "changelog": ChangelogPage(),
            "settings": SettingsPage(),
        }

        for page_id, page in self._pages.items():
            self._stack.addWidget(page)

        # 设置页面需要引用主窗口来切换主题
        self._pages["settings"].set_main_window(self)

        # 跨页面信号：积分更新互相同步
        self._pages["accounts"].quota_updated.connect(self._on_accounts_quota_updated)
        self._pages["api_proxy"].quota_updated.connect(self._on_proxy_quota_updated)

        layout.addWidget(self._stack, 1)

        # 默认显示仪表盘
        self._stack.setCurrentWidget(self._pages["dashboard"])

    def _setup_tray(self):
        """设置系统托盘"""
        self._tray = QSystemTrayIcon(self)
        self._tray.setToolTip("Antigravity Tools")

        # 加载应用图标（优先 .ico 文件，降级为程序化生成）
        app_icon = self._load_app_icon()
        self._tray.setIcon(app_icon)
        self.setWindowIcon(app_icon)

        tray_menu = QMenu()
        show_action = tray_menu.addAction("显示主窗口")
        show_action.triggered.connect(self._show_window)

        quit_action = tray_menu.addAction("退出")
        quit_action.triggered.connect(self._quit_app)

        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)

        # 如果关闭行为设为最小化到托盘，则初始化时就显示托盘图标
        close_behavior = load_setting("close_behavior", "minimize")
        if close_behavior == "minimize":
            self._tray.show()

    def _load_app_icon(self) -> QIcon:
        """加载应用图标 — 优先 .ico 文件，降级为程序化生成"""
        # 1. 尝试从打包路径加载
        icon_paths = []
        if getattr(sys, 'frozen', False):
            # PyInstaller 打包模式
            base = sys._MEIPASS
            icon_paths.append(os.path.join(base, 'assets', 'icons', 'app.ico'))
        # 2. 开发模式
        src_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(src_dir)
        icon_paths.append(os.path.join(project_root, 'assets', 'icons', 'app.ico'))

        for icon_path in icon_paths:
            if os.path.isfile(icon_path):
                icon = QIcon(icon_path)
                if not icon.isNull():
                    logger.info(f"加载应用图标: {icon_path}")
                    return icon

        # 3. 降级：程序化生成闪电图标
        logger.info("未找到 .ico 图标文件，使用程序化生成图标")
        from PySide6.QtGui import QPixmap, QPainter, QColor, QFont
        from PySide6.QtCore import QRect
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#6C5CE7"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(2, 2, 60, 60)
        painter.setPen(QColor("#FFFFFF"))
        font = QFont("Segoe UI Emoji", 32)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRect(0, 0, 64, 64), Qt.AlignmentFlag.AlignCenter, "⚡")
        painter.end()
        return QIcon(pixmap)

    def _on_accounts_quota_updated(self):
        """账号页积分更新 → 从磁盘重新加载代理池数据并刷新

        账号页面查分使用独立的 ProxyDatabase() 实例写盘，
        代理页面的 db 实例内存可能还是旧数据，需要 reload_from_disk。
        """
        proxy_page = self._pages.get("api_proxy")
        if proxy_page:
            try:
                proxy_page._refresh_upstream_keys(reload_from_disk=True)
                proxy_page._refresh_sub_keys()
            except Exception:
                pass

    def _on_proxy_quota_updated(self):
        """代理池页积分更新 → 刷新账号页"""
        accounts_page = self._pages.get("accounts")
        if accounts_page:
            try:
                accounts_page._refresh_table()
            except Exception:
                pass

    def _switch_page(self, page_id: str):
        """切换页面"""
        page = self._pages.get(page_id)
        if page:
            self._stack.setCurrentWidget(page)

    def apply_theme(self, theme: str):
        """应用主题"""
        self._current_theme = theme
        stylesheet = get_stylesheet(theme)
        app = QApplication.instance()
        if app:
            app.setStyleSheet(stylesheet)
        self.setStyleSheet(stylesheet)
        # 通知各页面刷新动态颜色（硬编码样式的控件需要手动更新）
        for page in self._pages.values():
            if hasattr(page, "apply_theme"):
                try:
                    page.apply_theme()
                except Exception:
                    pass

    def _show_window(self):
        """显示窗口"""
        self.showNormal()
        self.activateWindow()
        self.raise_()
        self.setFocus()

    def _on_tray_activated(self, reason):
        """托盘图标激活"""
        if reason == QSystemTrayIcon.DoubleClick:
            self._show_window()

    def _quit_app(self):
        """退出应用 — 清理所有子进程和资源，确保进程真正退出"""
        # 1. 停止 API 代理服务器（刷盘 + 关闭 socket + 关闭连接池）
        api_proxy_page = self._pages.get("api_proxy")
        if api_proxy_page:
            try:
                api_proxy_page._cleanup()
            except Exception:
                pass

        # 2. 注意：不关闭 WorkBuddy 进程！
        #    WorkBuddy 是独立应用，只有用户在登录流程中主动确认时才会关闭（oauth.py）
        #    关闭本软件不应影响用户正在使用的 WorkBuddy

        # 3. 关闭所有 QThread（签到、查询等后台任务）
        for page in self._pages.values():
            try:
                if hasattr(page, '_worker') and page._worker:
                    page._worker.stop()
                if hasattr(page, '_status_worker') and page._status_worker:
                    page._status_worker.stop()
                if hasattr(page, '_batch_worker') and page._batch_worker:
                    page._batch_worker.stop()
            except Exception:
                pass

        # 5. 隐藏托盘图标
        try:
            self._tray.hide()
        except Exception:
            pass

        # 6. 先尝试优雅退出 Qt 事件循环
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.instance().quit()
        except Exception:
            pass

        # 7. 兜底：如果 Qt 退出后线程还没结束，强制杀掉进程
        # 这是必要的，因为 HTTPServer 的 serve_forever() 线程可能阻塞
        # 即使调了 shutdown()，如果 socket 正在 accept() 等待，也可能卡住
        import threading
        # 给 1 秒让优雅退出生效
        for t in threading.enumerate():
            if t is not threading.main_thread() and t.is_alive():
                try:
                    t.join(timeout=1.0)
                except Exception:
                    pass
        # 如果还有非 daemon 线程活着，强制退出
        still_alive = [t for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive()]
        if still_alive:
            logger.warning(f"还有 {len(still_alive)} 个线程未退出，强制终止进程")
            os._exit(0)

    def _kill_workbuddy_process(self):
        """已弃用 — 不再在软件关闭时杀 WorkBuddy
        
        WorkBuddy 是独立应用，只有用户在登录流程（oauth.py）中主动确认后才会关闭。
        保留此方法仅为向后兼容，实际不再执行任何操作。
        """
        logger.debug("_kill_workbuddy_process 被调用但已弃用，不再杀 WorkBuddy 进程")

    def closeEvent(self, event):
        """关闭事件 - 退出或最小化到托盘"""
        close_behavior = load_setting("close_behavior", "minimize")
        if close_behavior == "minimize":
            event.ignore()
            self.hide()
            self._tray.show()
            self._tray.showMessage(
                "Antigravity Tools",
                "已最小化到系统托盘，双击图标恢复",
            )
        else:
            event.accept()
            self._quit_app()
