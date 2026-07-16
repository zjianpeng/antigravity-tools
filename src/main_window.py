"""主窗口 - Antigravity Tools 桌面应用"""

import logging
import os
import subprocess
import sys
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QStackedWidget, QSystemTrayIcon,
    QMenu, QMessageBox, QDialog, QVBoxLayout, QLabel, QProgressBar, QPushButton,
    QApplication,
)
from PySide6.QtGui import QIcon, QAction
from PySide6.QtCore import Qt, QSize, Slot

from .ui import Sidebar, get_stylesheet
from .ui.pages import (
    DashboardPage, AccountsPage, CheckinPage,
    SettingsPage, ApiProxyPage,
)
from .i18n import t
from .utils.store import init_db, load_setting
from .modules.updater import UpdateChecker, get_current_version

logger = logging.getLogger(__name__)


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

        # 自动更新
        self._setup_updater()

    def _update_version_suffix(self):
        """更新窗口标题中的版本号"""
        ver = get_current_version()
        self.setWindowTitle(f"⚡ Antigravity Tools v{ver}")

    def _setup_updater(self):
        """初始化自动更新检查器"""
        self._updater = UpdateChecker(self)
        self._updater.update_available.connect(self._on_update_available)
        self._updater.download_progress.connect(self._on_download_progress)
        self._updater.update_finished.connect(self._on_update_finished)
        self._updater.no_update.connect(self._on_no_update)

        # 启动定期检查：首次5秒后检查，之后每1小时检查
        self._updater.start_periodic_check(3600_000)

        # 更新对话框引用
        self._update_dialog = None
        self._update_progress_bar = None
        self._pending_download_url = ""
        self._pending_sha256 = ""

    def _on_update_available(self, version: str, changelog: str, download_url: str, sha256: str):
        """发现新版本 — 弹窗提示"""
        current = get_current_version()

        msg = QMessageBox(self)
        msg.setWindowTitle("发现新版本")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(f"发现新版本 v{version}！（当前 v{current}）")
        msg.setInformativeText(
            f"更新内容：\n{changelog}\n\n"
            f"是否立即更新？"
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Yes)

        # 以后不再提醒选项
        skip_btn = msg.addButton("跳过此版本", QMessageBox.ButtonRole.RejectRole)

        result = msg.exec()

        if msg.clickedButton() == skip_btn:
            # 记录跳过的版本
            from .utils.store import save_setting
            save_setting("skip_version", version)
            return

        if result == QMessageBox.StandardButton.Yes:
            self._pending_download_url = download_url
            self._pending_sha256 = sha256
            self._start_download_update(download_url, sha256)

    def _start_download_update(self, download_url: str, sha256: str):
        """显示下载进度对话框并开始下载"""
        self._update_dialog = QDialog(self)
        self._update_dialog.setWindowTitle("正在更新")
        self._update_dialog.setFixedSize(420, 150)
        self._update_dialog.setWindowFlags(
            self._update_dialog.windowFlags() & ~Qt.WindowCloseButtonHint
        )

        layout = QVBoxLayout(self._update_dialog)
        self._update_status_label = QLabel("正在下载更新包…")
        self._update_status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._update_status_label)

        self._update_progress_bar = QProgressBar()
        self._update_progress_bar.setMinimum(0)
        self._update_progress_bar.setMaximum(100)
        self._update_progress_bar.setValue(0)
        layout.addWidget(self._update_progress_bar)

        self._update_dialog.show()

        # 开始下载
        self._updater.download_and_apply(download_url, sha256)

    @Slot(int, int)
    def _on_download_progress(self, downloaded: int, total: int):
        """下载进度回调"""
        if self._update_progress_bar and total > 0:
            percent = int(downloaded * 100 / total)
            self._update_progress_bar.setValue(percent)
            mb_down = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            self._update_status_label.setText(
                f"正在下载更新包… {mb_down:.1f}/{mb_total:.1f} MB"
            )

    @Slot(bool, str)
    def _on_update_finished(self, success: bool, message: str):
        """更新完成回调"""
        # 关闭进度对话框
        if self._update_dialog:
            self._update_dialog.close()
            self._update_dialog = None

        if success:
            if message == "UPDATE_NEED_RESTART":
                # 打包模式：批处理已启动，直接退出让批处理接管
                msg = QMessageBox(self)
                msg.setWindowTitle("更新就绪")
                msg.setIcon(QMessageBox.Icon.Information)
                msg.setText("✅ 更新已下载完成！")
                msg.setInformativeText("点击「确定」后将自动关闭并完成更新，请稍候片刻自动重启。")
                msg.setStandardButtons(QMessageBox.StandardButton.Ok)
                msg.exec()
                # 批处理已经在等待进程退出，直接退出即可
                QApplication.quit()
                os._exit(0)
            else:
                # 源码模式：更新成功 — 提示重启
                msg = QMessageBox(self)
                msg.setWindowTitle("更新成功")
                msg.setIcon(QMessageBox.Icon.Information)
                msg.setText("✅ 更新已下载并安装完成！")
                msg.setInformativeText("需要重启应用才能生效，是否立即重启？")
                msg.setStandardButtons(
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                msg.setDefaultButton(QMessageBox.StandardButton.Yes)

                if msg.exec() == QMessageBox.StandardButton.Yes:
                    self._restart_app()
        else:
            QMessageBox.warning(self, "更新失败", f"❌ {message}")

    def _restart_app(self):
        """重启应用"""
        try:
            # 获取当前项目根目录
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

            # 确定启动命令
            python_exe = sys.executable
            if getattr(sys, 'frozen', False):
                # 打包模式 — 直接重启 exe
                subprocess.Popen([python_exe])
            else:
                # 开发模式 — 用 pythonw 启动
                pythonw = python_exe.replace("python.exe", "pythonw.exe")
                if not os.path.isfile(pythonw):
                    pythonw = python_exe
                subprocess.Popen(
                    [pythonw, "-m", "src.main"],
                    cwd=project_root,
                    creationflags=subprocess.DETACHED_PROCESS if os.name == "nt" else 0,
                )

            # 退出当前进程
            from PySide6.QtWidgets import QApplication
            QApplication.instance().quit()
        except Exception as e:
            logger.error(f"重启应用失败: {e}")
            QMessageBox.warning(self, "重启失败", f"请手动重启应用。\n错误: {e}")

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

        # 手动检查更新
        check_update_action = tray_menu.addAction("🔄 检查更新")
        check_update_action.triggered.connect(self._manual_check_update)

        quit_action = tray_menu.addAction("退出")
        quit_action.triggered.connect(self._quit_app)

        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)

        # 如果关闭行为设为最小化到托盘，则初始化时就显示托盘图标
        close_behavior = load_setting("close_behavior", "minimize")
        if close_behavior == "minimize":
            self._tray.show()

    def _manual_check_update(self):
        """手动检查更新"""
        self._updater._manual_check = True
        self._updater.check_update()

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

    @Slot(bool)
    def _on_no_update(self, is_manual: bool):
        """无更新"""
        if is_manual:
            # 手动检查才弹提示，自动检查不打扰
            QMessageBox.information(self, "检查更新", "✅ 当前已是最新版本！")

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
