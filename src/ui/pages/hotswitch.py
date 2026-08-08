"""无感换号页面 - CodeBuddy / WorkBuddy 透明中转

独立的本地中转服务（默认端口 8003）：
- 原样转发到官方服务器，仅在对话计费请求经过时用池里的账号 token 替换
- 上游 Key 池只放账号 token（JWT），禁用/恢复状态独立于 API 代理页
- 使用日志独立，只记录经过本中转的请求
"""

import secrets
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QLineEdit, QSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QTabWidget, QTextEdit, QMessageBox, QApplication,
    QScrollArea, QDialog, QToolButton, QMenu, QComboBox,
    QFormLayout, QDialogButtonBox
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QBrush, QColor

from ...utils.store import save_setting, load_setting
from ...modules.proxy_server import ProxyDatabase
from ...modules.codebuddy_relay import (
    CodeBuddyRelayServer, apply_client_config, restore_client_config,
    get_client_config_state, is_dev_mode_enabled, enable_dev_mode,
    is_codebuddy_running,
    apply_workbuddy_config, restore_workbuddy_config,
    get_workbuddy_config_state,
)
from .api_proxy import (
    ImportFromAccountsDialog, _style_popup_menu, _fmt_tokens, ApiProxyPage,
    _get_account_concurrency_setting,
)


def _set_item(table, row, col, text, tooltip=None):
    """设置表格单元格，自动加 tooltip 显示完整内容"""
    item = QTableWidgetItem(text)
    item.setToolTip(tooltip if tooltip else text)
    table.setItem(row, col, item)
    return item


def _set_multiline_text(label: QLabel, text: str):
    """给 wordWrap QLabel 写多行文本并按行数锁定最小高度。

    实测：macOS 上 QLabel sizeHint 不随 setText 的行数更新，
    布局仍按 1 行分高，多行文字被裁掉一半。按行数 setMinimumHeight 兜底。
    """
    label.setText(text)
    fm = label.fontMetrics()
    lines = text.count("\n") + 1
    label.setMinimumHeight(fm.lineSpacing() * lines + 8)


class HotSwitchPage(QWidget):
    """无感换号页面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._db = ProxyDatabase.get_instance()
        self._relay_server: CodeBuddyRelayServer = None
        self._setup_ui()

        # 中转状态/日志定时刷新
        self._relay_timer = QTimer(self)
        self._relay_timer.timeout.connect(self._on_timer)
        self._relay_timer.start(2000)

        # 上次开启过中转的话，启动后自动拉起（静默，不弹窗）
        QTimer.singleShot(800, self._autostart_relay)

    # ═══════════ UI 构建 ═══════════

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("无感换号")
        title.setObjectName("page_title")
        layout.addWidget(title)

        subtitle = QLabel("CodeBuddy / WorkBuddy 透明中转 · 客户端无感知 · 只消耗池里 token 号的额度")
        subtitle.setObjectName("page_subtitle")
        layout.addWidget(subtitle)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(32, 0, 32, 32)
        content_layout.setSpacing(16)

        # ─── 服务控制区（单行，跟 API 代理页同款紧凑布局）───
        control_card = QFrame()
        control_card.setObjectName("card")
        control_layout = QVBoxLayout(control_card)
        control_layout.setSpacing(10)

        row = QHBoxLayout()
        row.addWidget(QLabel("端口:"))
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1024, 65535)
        self._port_spin.setValue(int(load_setting("codebuddy_relay_port", "8003")))
        row.addWidget(self._port_spin)

        row.addWidget(QLabel("  "))
        self._listen_mode_combo = QComboBox()
        self._listen_mode_combo.addItem("🔒 本地模式", "local")
        self._listen_mode_combo.addItem("🌐 开放模式", "open")
        self._listen_mode_combo.setCurrentIndex(
            0 if load_setting("hotswitch_listen_mode", "local") == "local" else 1)
        self._listen_mode_combo.setToolTip("本地模式只有本机能连；开放模式监听 0.0.0.0，局域网设备也能连")
        self._listen_mode_combo.currentIndexChanged.connect(self._on_listen_mode_changed)
        row.addWidget(self._listen_mode_combo)

        row.addWidget(QLabel("    "))
        row.addWidget(QLabel("中转地址:"))
        self._url_label = QLabel(f"http://127.0.0.1:{self._port_spin.value()}")
        self._url_label.setStyleSheet("color: #2B6CB0; font-weight: 600; font-size: 13px;")
        self._url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self._url_label)

        btn_copy_url = QPushButton("📋 复制")
        btn_copy_url.setObjectName("secondary_btn")
        btn_copy_url.setCursor(Qt.PointingHandCursor)
        btn_copy_url.setFixedWidth(60)
        btn_copy_url.clicked.connect(self._copy_url)
        row.addWidget(btn_copy_url)

        row.addStretch()

        self._status_label = QLabel("⏹ 已停止")
        self._status_label.setStyleSheet("font-weight: 600; color: #9BA4B0;")
        row.addWidget(self._status_label)

        self._toggle_btn = QPushButton("▶ 启动服务")
        self._toggle_btn.setObjectName("primary_btn")
        self._toggle_btn.setCursor(Qt.PointingHandCursor)
        self._toggle_btn.clicked.connect(self._toggle_service)
        row.addWidget(self._toggle_btn)

        control_layout.addLayout(row)

        # 第二行：积分阈值 + 限流冷却（照 API 代理页，阈值只作用于本页侧 relay_* 状态）
        thresh_row = QHBoxLayout()
        thresh_row.addWidget(QLabel("最低积分:"))
        self._min_credits_spin = QSpinBox()
        self._min_credits_spin.setRange(0, 100000)
        self._min_credits_spin.setValue(int(load_setting("hotswitch_min_credits", "0")))
        self._min_credits_spin.setSuffix(" 分")
        self._min_credits_spin.setMinimumWidth(100)
        self._min_credits_spin.setToolTip("低于此积分自动禁用 Key（仅本页侧，0=不限制）")
        thresh_row.addWidget(self._min_credits_spin)

        thresh_row.addWidget(QLabel("  "))
        thresh_row.addWidget(QLabel("自动启用:"))
        self._auto_enable_spin = QSpinBox()
        self._auto_enable_spin.setRange(0, 100000)
        self._auto_enable_spin.setValue(int(load_setting("hotswitch_auto_enable", "100")))
        self._auto_enable_spin.setSuffix(" 分")
        self._auto_enable_spin.setMinimumWidth(100)
        self._auto_enable_spin.setToolTip("查分高于此值自动恢复「积分不足自动禁用」的 Key（仅本页侧）")
        thresh_row.addWidget(self._auto_enable_spin)

        thresh_row.addWidget(QLabel("  "))
        thresh_row.addWidget(QLabel("限流冷却:"))
        self._cooldown_spin = QSpinBox()
        self._cooldown_spin.setRange(1, 3600)
        self._cooldown_spin.setValue(int(load_setting("cooldown_seconds", "10")))
        self._cooldown_spin.setSuffix(" 秒")
        self._cooldown_spin.setMinimumWidth(90)
        self._cooldown_spin.setToolTip("Key 被限流(429)后冷却多少秒再自动恢复")
        thresh_row.addWidget(self._cooldown_spin)
        self._cooldown_spin.valueChanged.connect(
            lambda v: save_setting("cooldown_seconds", str(v))
        )

        self._open_mode_hint = QLabel("")
        self._open_mode_hint.setStyleSheet("color: #E53E3E; font-size: 12px; font-weight: 600;")
        self._open_mode_hint.setVisible(False)
        thresh_row.addWidget(self._open_mode_hint)
        thresh_row.addStretch()

        # 积分阈值变更自动保存并立即按存量积分同步状态
        self._min_credits_spin.valueChanged.connect(self._apply_thresholds_now)
        self._auto_enable_spin.valueChanged.connect(self._apply_thresholds_now)

        control_layout.addLayout(thresh_row)
        content_layout.addWidget(control_card)

        # ─── Tab 区：上游 Key 池 / 使用日志 / 客户端接入 ───
        self._tab_widget = QTabWidget()
        self._build_pool_tab()
        self._build_log_tab()
        self._build_client_tab()
        content_layout.addWidget(self._tab_widget, 1)

        layout.addWidget(content, 1)

        # 构建期 setCurrentIndex 触发的信号被 guard 跳过，这里补一次同步（开放模式提示/URL）
        self._on_listen_mode_changed(self._listen_mode_combo.currentIndex())

    def _build_pool_tab(self):
        """Tab 1: 上游 Key 池（只放账号 token / JWT）"""
        pool_tab = QWidget()
        pool_layout = QVBoxLayout(pool_tab)
        pool_layout.setSpacing(10)

        # 统计行
        stats_row = QHBoxLayout()
        self._stat_total = QLabel("📋 总 Key: 0")
        self._stat_total.setStyleSheet("font-size: 13px; font-weight: 600;")
        stats_row.addWidget(self._stat_total)
        self._stat_active = QLabel("✅ 活跃: 0")
        self._stat_active.setStyleSheet("font-size: 13px; font-weight: 600; color: #38A169;")
        stats_row.addWidget(self._stat_active)
        self._stat_disabled = QLabel("🚫 禁用: 0")
        self._stat_disabled.setStyleSheet("font-size: 13px; font-weight: 600; color: #E53E3E;")
        stats_row.addWidget(self._stat_disabled)
        self._stat_used = QLabel("📊 总调用: 0")
        self._stat_used.setStyleSheet("font-size: 13px; font-weight: 600; color: #805AD5;")
        stats_row.addWidget(self._stat_used)
        stats_row.addStretch()

        btn_refresh = QPushButton("🔄 刷新")
        btn_refresh.setObjectName("secondary_btn")
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.clicked.connect(self._refresh_pool)
        stats_row.addWidget(btn_refresh)
        pool_layout.addLayout(stats_row)

        # 工具栏 — 从账号导入 + 查积分 + 检测 + 批量操作（照 API 代理页）
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.setContentsMargins(0, 0, 0, 0)

        btn_import = QPushButton("📥 导入")
        btn_import.setObjectName("primary_btn")
        btn_import.setCursor(Qt.PointingHandCursor)
        btn_import.setToolTip("从已获取的账号中导入 token（JWT）到上游 Key 池，ck_ 卡密不导入")
        btn_import.clicked.connect(self._import_from_accounts)
        toolbar.addWidget(btn_import)

        btn_points = QPushButton("🔄 积分")
        btn_points.setObjectName("secondary_btn")
        btn_points.setCursor(Qt.PointingHandCursor)
        btn_points.setToolTip("异步查询所有 token Key 的剩余积分（每个 Key 5 分钟限频一次）")
        btn_points.clicked.connect(self._refresh_all_points)
        toolbar.addWidget(btn_points)

        btn_check = QPushButton("🔍 检测")
        btn_check.setObjectName("secondary_btn")
        btn_check.setCursor(Qt.PointingHandCursor)
        btn_check.setToolTip("批量检测所有 token Key 是否被风控（11140），异常的自动禁用（仅本页侧）")
        btn_check.clicked.connect(self._check_all_key_status)
        toolbar.addWidget(btn_check)

        btn_perm_disable = QPushButton("🚫 永禁")
        btn_perm_disable.setObjectName("danger_btn")
        btn_perm_disable.setCursor(Qt.PointingHandCursor)
        btn_perm_disable.setToolTip("按积分范围批量永久禁用 Key（仅本页侧，不会自动恢复）")
        btn_perm_disable.clicked.connect(
            lambda: self._open_batch_status_dialog(enable=False, permanent=True))
        toolbar.addWidget(btn_perm_disable)

        btn_disable = QPushButton("禁用")
        btn_disable.setObjectName("secondary_btn")
        btn_disable.setCursor(Qt.PointingHandCursor)
        btn_disable.setToolTip("按积分范围批量临时禁用 Key（仅本页侧，不影响 API 代理页）")
        btn_disable.clicked.connect(
            lambda: self._open_batch_status_dialog(enable=False, permanent=False))
        toolbar.addWidget(btn_disable)

        btn_enable = QPushButton("✅ 解禁")
        btn_enable.setObjectName("secondary_btn")
        btn_enable.setCursor(Qt.PointingHandCursor)
        btn_enable.setToolTip("按积分范围批量恢复 Key 为可用（仅本页侧）")
        btn_enable.clicked.connect(lambda: self._open_batch_status_dialog(enable=True))
        toolbar.addWidget(btn_enable)

        # 当天/总计切换
        self._today_only = False
        self._chk_today = QPushButton("📅 当天")
        self._chk_today.setObjectName("secondary_btn")
        self._chk_today.setCheckable(True)
        self._chk_today.setCursor(Qt.PointingHandCursor)
        self._chk_today.setToolTip("开启后只显示当天统计，关闭显示总计")
        self._chk_today.clicked.connect(self._toggle_today)
        toolbar.addWidget(self._chk_today)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍 搜索 Key...")
        self._search_input.textChanged.connect(lambda _t: self._refresh_pool())
        toolbar.addWidget(self._search_input)

        toolbar.addStretch()
        pool_layout.addLayout(toolbar)

        # Key 表格
        self._pool_table = QTableWidget()
        self._pool_table.setColumnCount(9)
        self._pool_table.setHorizontalHeaderLabels([
            "Key ID", "标签", "状态", "调用次数", "积分", "Token", "缓存命中", "备注", "操作"
        ])
        self._pool_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._pool_table.setAlternatingRowColors(True)
        self._pool_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pool_table.setSelectionBehavior(QTableWidget.SelectRows)
        pool_layout.addWidget(self._pool_table)

        self._tab_widget.addTab(pool_tab, "🔑 上游 Key 池")

    def _build_log_tab(self):
        """Tab 2: 使用日志（中转自己的事件流）"""
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)

        self._log_edit = QTextEdit()
        self._log_edit.setObjectName("log_edit")
        self._log_edit.setReadOnly(True)
        self._log_edit.setFont(QFont("Consolas"))
        log_layout.addWidget(self._log_edit)

        log_toolbar = QHBoxLayout()
        btn_refresh_log = QPushButton("🔄 刷新日志")
        btn_refresh_log.setObjectName("secondary_btn")
        btn_refresh_log.setCursor(Qt.PointingHandCursor)
        btn_refresh_log.clicked.connect(self._refresh_log)
        log_toolbar.addWidget(btn_refresh_log)

        btn_clear_log = QPushButton("🗑️ 清空")
        btn_clear_log.setObjectName("secondary_btn")
        btn_clear_log.setCursor(Qt.PointingHandCursor)
        btn_clear_log.clicked.connect(self._clear_log)
        log_toolbar.addWidget(btn_clear_log)

        log_toolbar.addStretch()
        log_layout.addLayout(log_toolbar)

        self._tab_widget.addTab(log_tab, "📊 使用日志")

    def _build_client_tab(self):
        """Tab 3: 客户端接入（CodeBuddy / WorkBuddy）"""
        client_tab = QWidget()
        outer = QVBoxLayout(client_tab)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 内容超出时用滚动区承载（照 settings.py 标准模式），
        # 否则窗口不够高时 QVBoxLayout 会把标题/按钮压到 0 高
        scroll = QScrollArea()
        scroll.setObjectName("settings_scroll_area")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        client_layout = QVBoxLayout(content)
        client_layout.setContentsMargins(8, 8, 8, 8)
        client_layout.setSpacing(12)

        # CodeBuddy 客户端配置区
        cb_card = QFrame()
        cb_card.setObjectName("card")
        cb_layout = QVBoxLayout(cb_card)
        cb_layout.setSpacing(8)

        cb_layout.addWidget(QLabel("CodeBuddy 客户端配置:"))
        self._client_label = QLabel("检测中…")
        self._client_label.setStyleSheet("font-size: 12px;")
        self._client_label.setWordWrap(True)
        cb_layout.addWidget(self._client_label)

        cb_btn_row = QHBoxLayout()
        self._devmode_btn = QPushButton("🔧 开启开发者模式")
        self._devmode_btn.setObjectName("secondary_btn")
        self._devmode_btn.setCursor(Qt.PointingHandCursor)
        self._devmode_btn.setToolTip(
            "自定义端点的前置条件，一次性操作。需先完全退出 CodeBuddy。")
        self._devmode_btn.clicked.connect(self._enable_devmode)
        cb_btn_row.addWidget(self._devmode_btn)

        btn_recheck = QPushButton("🔄 重新检测")
        btn_recheck.setObjectName("secondary_btn")
        btn_recheck.setCursor(Qt.PointingHandCursor)
        btn_recheck.clicked.connect(self._refresh_client_status)
        cb_btn_row.addWidget(btn_recheck)
        cb_btn_row.addStretch()
        cb_layout.addLayout(cb_btn_row)

        client_layout.addWidget(cb_card)

        # WorkBuddy 客户端配置区
        wb_card = QFrame()
        wb_card.setObjectName("card")
        wb_layout = QVBoxLayout(wb_card)
        wb_layout.setSpacing(8)

        wb_layout.addWidget(QLabel("WorkBuddy 客户端配置:"))
        self._wb_label = QLabel("检测中…")
        self._wb_label.setStyleSheet("font-size: 12px;")
        self._wb_label.setWordWrap(True)
        wb_layout.addWidget(self._wb_label)

        wb_btn_row = QHBoxLayout()
        self._wb_btn = QPushButton("🔗 接入 WorkBuddy")
        self._wb_btn.setObjectName("secondary_btn")
        self._wb_btn.setCursor(Qt.PointingHandCursor)
        self._wb_btn.setToolTip(
            "写入 ~/.workbuddy/settings.json 的 env.CODEBUDDY_BASE_URL 指向本地中转。\n"
            "WorkBuddy 新会话即走中转（无需重启，无需开发者模式）。")
        self._wb_btn.clicked.connect(self._toggle_workbuddy)
        wb_btn_row.addWidget(self._wb_btn)
        wb_btn_row.addStretch()
        wb_layout.addLayout(wb_btn_row)

        client_layout.addWidget(wb_card)
        client_layout.addStretch()

        scroll.setWidget(content)
        outer.addWidget(scroll)
        self._tab_widget.addTab(client_tab, "🤖 客户端接入")

    # ═══════════ 服务控制 ═══════════

    def _relay_host(self) -> str:
        """按监听模式返回绑定地址"""
        return "127.0.0.1" if self._listen_mode_combo.currentData() == "local" else "0.0.0.0"

    def _display_url(self) -> str:
        """当前应展示/复制的中转地址（开放模式显示局域网 IP）"""
        port = self._port_spin.value()
        if self._listen_mode_combo.currentData() == "open":
            ips = self._get_local_ips()
            return f"http://{ips[0] if ips else '0.0.0.0'}:{port}"
        return f"http://127.0.0.1:{port}"

    def _on_listen_mode_changed(self, _index: int):
        """监听模式切换：持久化 + 更新提示和 URL"""
        if not hasattr(self, "_url_label"):
            return  # UI 构建期间触发的信号，忽略
        mode = self._listen_mode_combo.currentData()
        save_setting("hotswitch_listen_mode", mode)
        if mode == "open":
            ips = self._get_local_ips()
            ip_list = "、".join(ips) if ips else "未检测到"
            self._open_mode_hint.setText(f"⚠️ 开放模式：局域网设备均可访问（本机IP: {ip_list}）")
            self._open_mode_hint.setVisible(True)
        else:
            self._open_mode_hint.setVisible(False)
        if not (self._relay_server and self._relay_server.is_running):
            self._url_label.setText(self._display_url())

    @staticmethod
    def _get_local_ips() -> list:
        """获取本机所有非回环 IP 地址（照 API 代理页）"""
        import socket
        ips = []
        try:
            hostname = socket.gethostname()
            for ip in socket.getaddrinfo(hostname, None):
                addr = ip[4][0]
                if isinstance(addr, str) and addr != "127.0.0.1" and not addr.startswith("169.254.") and ":" not in addr:
                    if addr not in ips:
                        ips.append(addr)
        except Exception:
            pass
        if not ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                s.close()
                if ip != "127.0.0.1":
                    ips.append(ip)
            except Exception:
                pass
        return ips

    def _autostart_relay(self):
        """按持久化标记自动恢复中转服务与客户端配置（静默）"""
        if load_setting("codebuddy_relay_enabled", "0") != "1":
            return
        if self._relay_server and self._relay_server.is_running:
            return
        port = int(load_setting("codebuddy_relay_port", "8003") or "8003")
        server = CodeBuddyRelayServer(host=self._relay_host(), port=port)
        if not server.start():
            return
        self._relay_server = server
        apply_client_config(port)
        if load_setting("codebuddy_relay_wb_enabled", "0") == "1":
            apply_workbuddy_config(port)
        self._refresh_status()

    def _toggle_service(self):
        """启动/停止无感换号中转"""
        if self._relay_server and self._relay_server.is_running:
            self._relay_server.stop()
            self._relay_server = None
            save_setting("codebuddy_relay_enabled", "0")
            save_setting("codebuddy_relay_wb_enabled", "0")
            ok, msg = restore_client_config()
            if not ok:
                QMessageBox.warning(self, "还原配置失败", msg)
            # WorkBuddy 如指向本地中转也一并还原，避免打到已关闭的端口
            ok, msg = restore_workbuddy_config()
            if not ok:
                QMessageBox.warning(self, "还原 WorkBuddy 配置失败", msg)
            self._refresh_status()
            return

        port = self._port_spin.value()
        self._relay_server = CodeBuddyRelayServer(host=self._relay_host(), port=port)
        if not self._relay_server.start():
            self._relay_server = None
            QMessageBox.warning(self, "启动失败", f"无法在端口 {port} 启动中转服务，可能端口已被占用")
            return

        save_setting("codebuddy_relay_port", str(port))
        save_setting("codebuddy_relay_enabled", "1")
        # 把 CodeBuddy 的 API 端点指向本地中转（settings.json 热加载，即时生效）
        ok, msg = apply_client_config(port)
        if not ok:
            QMessageBox.warning(self, "配置客户端失败", msg)

        # 开发者模式是自定义端点的前置条件（一次性）
        if not is_dev_mode_enabled():
            if is_codebuddy_running():
                QMessageBox.information(
                    self, "还需一步（一次性）",
                    "中转已开启，端点已指向本地。\n\n"
                    "但 CodeBuddy 的「开发者模式」还没开，自定义端点不会生效。\n"
                    "请完全退出 CodeBuddy（Cmd+Q），然后回来点一下\n"
                    "「🔧 开启开发者模式」，再启动 CodeBuddy 即可。\n"
                    "此操作只需一次，永久生效。"
                )
            else:
                ok, msg = enable_dev_mode()
                if not ok:
                    QMessageBox.warning(self, "开启开发者模式失败", msg)
        self._refresh_status()

    def _enable_devmode(self):
        """手动开启 CodeBuddy 开发者模式"""
        if is_dev_mode_enabled():
            QMessageBox.information(self, "开发者模式", "开发者模式已开启，无需重复操作。")
            return
        ok, msg = enable_dev_mode()
        if ok:
            QMessageBox.information(self, "开发者模式", msg + "\n现在可以启动 CodeBuddy 了。")
        else:
            QMessageBox.warning(self, "开启开发者模式失败", msg)
        self._refresh_client_status()

    def _toggle_workbuddy(self):
        """接入/还原 WorkBuddy 的 CLI 端点配置（静默，不弹窗）"""
        port = self._port_spin.value()
        running = self._relay_server and self._relay_server.is_running
        state = get_workbuddy_config_state(port)
        if state["pointed_to_us"]:
            ok, msg = restore_workbuddy_config()
            if not ok:
                QMessageBox.warning(self, "还原失败", msg)
            else:
                save_setting("codebuddy_relay_wb_enabled", "0")
        else:
            if not running:
                QMessageBox.warning(self, "中转未开启", "请先点上方「▶ 启动服务」启动中转服务。")
                return
            ok, msg = apply_workbuddy_config(port)
            if ok:
                save_setting("codebuddy_relay_wb_enabled", "1")
            else:
                QMessageBox.warning(self, "接入失败", msg)
        self._refresh_client_status()

    def _copy_url(self):
        QApplication.clipboard().setText(self._display_url())

    # ═══════════ 状态刷新 ═══════════

    def _on_timer(self):
        """2 秒定时：状态 + Key 池 + 日志（可见时）"""
        self._refresh_status()
        self._refresh_client_status()
        idx = self._tab_widget.currentIndex()
        if idx == 0:
            self._refresh_pool()
        elif idx == 1:
            self._refresh_log()

    def _refresh_status(self):
        """刷新中转运行状态"""
        running = self._relay_server and self._relay_server.is_running

        if running:
            st = self._relay_server.get_status()
            cur = st.get("current_key") or {}
            self._status_label.setText(f"▶ 运行中 :{st['port']}")
            self._status_label.setStyleSheet("font-weight: 600; color: #38A169;")
            self._toggle_btn.setText("⏹ 停止服务")
            self._toggle_btn.setObjectName("danger_btn")
            self._port_spin.setEnabled(False)
            self._listen_mode_combo.setEnabled(False)
            if cur:
                points = cur.get("points") or "?"
                consume = f"当前消耗: {cur.get('label', '-')}（剩余 {points} 分）"
            else:
                consume = "当前消耗: -（等待客户端发起对话）"
            self._stat_used.setToolTip(
                f"{consume}｜累计请求 {st['total_requests']} 次"
                f"（换号 {st['swapped_requests']} 次）｜最近: {st['last_event'] or '-'}")
        else:
            self._status_label.setText("⏹ 已停止")
            self._status_label.setStyleSheet("font-weight: 600; color: #9BA4B0;")
            self._toggle_btn.setText("▶ 启动服务")
            self._toggle_btn.setObjectName("primary_btn")
            self._port_spin.setEnabled(True)
            self._listen_mode_combo.setEnabled(True)
            self._url_label.setText(self._display_url())

        # 控件样式重载（objectName 切换后需要）
        self._toggle_btn.style().unpolish(self._toggle_btn)
        self._toggle_btn.style().polish(self._toggle_btn)

    def _refresh_client_status(self):
        """刷新两个客户端的配置状态"""
        port = self._port_spin.value()
        try:
            cfg = get_client_config_state(port)
            endpoint_txt = cfg["endpoint"] or "官方默认"
            endpoint_ok = "✅" if cfg["pointed_to_us"] else "⚠️"
            devmode_txt = "✅ 已开启" if cfg["dev_mode"] else "❌ 未开启（点下方按钮，需先退出 CodeBuddy）"
            _set_multiline_text(self._client_label,
                f"{endpoint_ok} 当前端点: {endpoint_txt}\n"
                f"开发者模式: {devmode_txt}"
            )
            self._devmode_btn.setVisible(not cfg["dev_mode"])
        except Exception:
            self._client_label.setText("配置状态检测失败")

        try:
            wb = get_workbuddy_config_state(port)
            # WorkBuddy 桌面端保存自身设置时会丢掉它不认识的 env 键（实测被覆盖过），
            # 中转运行中且标记为已接入时发现漂移就静默补回
            if not wb["pointed_to_us"] and \
                    self._relay_server and self._relay_server.is_running and \
                    load_setting("codebuddy_relay_wb_enabled", "0") == "1":
                apply_workbuddy_config(port)
                wb = get_workbuddy_config_state(port)
            wb_url = wb["base_url"] or "官方默认"
            wb_ok = "✅" if wb["pointed_to_us"] else "⚠️"
            _set_multiline_text(self._wb_label, f"{wb_ok} 当前端点: {wb_url}")
            self._wb_btn.setText(
                "🔌 断开 WorkBuddy" if wb["pointed_to_us"] else "🔗 接入 WorkBuddy")
        except Exception:
            self._wb_label.setText("配置状态检测失败")

    # ═══════════ 上游 Key 池（仅 JWT，状态独立）═══════════

    def _jwt_keys(self) -> list:
        """池子里所有账号 token Key（JWT）"""
        return [
            k for k in self._db.get_upstream_keys()
            if k.get("api_key", "").startswith("eyJ")
        ]

    @staticmethod
    def _relay_state_of(key: dict) -> tuple:
        """(状态文本, 状态码)：active / cooldown / disabled / permanent_disabled"""
        relay_status = key.get("relay_status", "active")
        if relay_status == "permanent_disabled":
            return "⛔ 永久禁用", "permanent_disabled"
        if relay_status != "active":
            return "🚫 已禁用", "disabled"
        remain = float(key.get("relay_cooldown_until") or 0) - time.time()
        if remain > 0:
            return f"🧊 冷却中({int(remain)}s)", "cooldown"
        return "✅ 活跃", "active"

    def _toggle_today(self):
        """切换 Key 池表格的当天/总计显示"""
        self._today_only = self._chk_today.isChecked()
        self._chk_today.setText("📅 当天✓" if self._today_only else "📅 当天")
        self._refresh_pool()

    def _apply_thresholds_now(self, *_args):
        """最低积分/自动启用变更：持久化 + 下次 _refresh_pool 生效（那里只在状态需要变迁时才写库）"""
        if not hasattr(self, "_pool_table"):
            return  # UI 构建期间 setValue 触发的信号，忽略
        save_setting("hotswitch_min_credits", str(self._min_credits_spin.value()))
        save_setting("hotswitch_auto_enable", str(self._auto_enable_spin.value()))
        self._refresh_pool()

    def _apply_point_thresholds(self, k: dict):
        """按存量积分对本页侧 relay_* 状态执行 最低积分/自动启用 规则。

        只在状态需要变迁时写库（_refresh_pool 每 2s 跑一次，不能无脑写）。
        永禁 / 检测禁用 / 无积分数据的一律不动；自动恢复的仅限「积分不足自动禁用」的。
        """
        relay_status = k.get("relay_status", "active")
        if relay_status == "permanent_disabled":
            return
        note = str(k.get("relay_note", ""))
        if note.startswith("检测:"):
            return  # 风控禁用交给「检测」按钮管理
        pts = ApiProxyPage._points_remaining(k.get("points", ""))
        if pts < 0:
            return  # 无积分数据

        min_val = self._min_credits_spin.value()
        auto_val = self._auto_enable_spin.value()
        key_id = k.get("key_id", "")
        in_cooldown = float(k.get("relay_cooldown_until") or 0) > time.time()

        # 积分 <= min → 自动禁用（冷却中的跳过，等冷却结束后再判）
        if min_val > 0 and pts <= min_val and relay_status == "active" and not in_cooldown:
            self._db.update_upstream_key(key_id, {
                "relay_status": "disabled",
                "relay_note": f"积分不足({pts:.0f}<={min_val})，中转侧自动禁用",
            })
            k["relay_status"] = "disabled"
            k["relay_note"] = f"积分不足({pts:.0f}<={min_val})，中转侧自动禁用"
        # 积分 > auto → 只恢复「积分不足自动禁用」的，手动/风控禁用的不动
        elif auto_val > 0 and pts > auto_val and relay_status == "disabled" \
                and note.startswith("积分不足"):
            self._db.update_upstream_key(key_id, {
                "relay_status": "active",
                "relay_note": "",
            })
            k["relay_status"] = "active"
            k["relay_note"] = ""

    def _refresh_pool(self):
        keys = self._jwt_keys()

        # 最低积分/自动启用规则（只在状态需要变迁时写库）
        if hasattr(self, "_min_credits_spin"):
            for k in keys:
                try:
                    self._apply_point_thresholds(k)
                except Exception:
                    pass

        search = self._search_input.text().strip().lower()
        if search:
            keys = [
                k for k in keys
                if search in k.get("key_id", "").lower()
                or search in k.get("label", "").lower()
                or search in str(k.get("points", "")).lower()
            ]

        # 正在中转请求里使用的 Key（relay 侧 inflight 跟踪）
        inflight = {}
        if self._relay_server and self._relay_server.is_running:
            try:
                inflight = self._relay_server.get_status().get("inflight", {}) or {}
            except Exception:
                inflight = {}

        keys.sort(key=lambda k: k.get("last_used_at", ""), reverse=True)
        # 使用中的 Key 置顶
        if inflight:
            keys.sort(key=lambda k: 0 if k.get("key_id", "") in inflight else 1)

        self._pool_table.setRowCount(len(keys))
        active = 0
        disabled = 0
        total_used = 0

        for row, k in enumerate(keys):
            key_id = k.get("key_id", "")
            label = k.get("label", "") or "-"
            state_text, state = self._relay_state_of(k)
            points = k.get("points", "-")
            note = k.get("relay_note", "") or "-"

            # 统计数据（当天或总计）— 调用次数/Token 都跟随切换
            if self._today_only:
                today = self._db.get_today_stats("relay", key_id)
                used = today.get("count", 0)
                total_prompt = today.get("prompt_tokens", 0)
                total_completion = today.get("completion_tokens", 0)
                total_t = today.get("total_tokens", 0)
                total_cached = today.get("cached_tokens", 0)
            else:
                used = int(k.get("relay_used", 0) or 0)
                total_prompt = int(k.get("relay_prompt_tokens", 0) or 0)
                total_completion = int(k.get("relay_completion_tokens", 0) or 0)
                total_t = int(k.get("relay_total_tokens", 0) or 0)
                total_cached = int(k.get("relay_cached_tokens", 0) or 0)

            if state in ("active", "cooldown"):
                active += 1
            else:
                disabled += 1
            total_used += used

            _set_item(self._pool_table, row, 0, key_id, tooltip=f"Key ID: {key_id}")
            _set_item(self._pool_table, row, 1, label, tooltip=label)

            # 正在使用的 Key 状态文字加并发数标记 + 整行绿色背景（照 API 代理页）
            if key_id in inflight:
                state_text = f"🟢 使用中({inflight[key_id]})"
            state_item = _set_item(self._pool_table, row, 2, state_text,
                                   tooltip=f"中转侧状态: {state}" + (
                                       f"，并发: {inflight[key_id]}" if key_id in inflight else ""))
            if key_id in inflight:
                green_bg = QBrush(QColor(200, 255, 200))  # 浅绿色
                for col in range(self._pool_table.columnCount()):
                    item = self._pool_table.item(row, col)
                    if item:
                        item.setBackground(green_bg)
            elif state == "active":
                state_item.setForeground(Qt.darkGreen)
            elif state in ("disabled", "permanent_disabled"):
                state_item.setForeground(Qt.red)

            _set_item(self._pool_table, row, 3, str(used), tooltip=f"调用次数: {used:,}")
            _set_item(self._pool_table, row, 4, str(points), tooltip=f"剩余积分: {points}")

            # Token 统计（智能单位转换）
            if total_t > 0:
                token_display = f"{_fmt_tokens(total_prompt)}+{_fmt_tokens(total_completion)}"
                token_tip = f"输入: {total_prompt:,}  输出: {total_completion:,}  总计: {total_t:,}"
            else:
                token_display = "-"
                token_tip = "暂无数据"
            _set_item(self._pool_table, row, 5, token_display, tooltip=token_tip)

            # 缓存命中率
            if total_t > 0 and total_cached > 0:
                cache_rate = total_cached / total_t * 100
                cache_text = f"{cache_rate:.1f}%"
                cache_tip = f"缓存命中: {total_cached:,} / 总计: {total_t:,} = {cache_rate:.1f}%"
            else:
                cache_text = "-"
                cache_tip = "暂无数据"
            _set_item(self._pool_table, row, 6, cache_text, tooltip=cache_tip)

            _set_item(self._pool_table, row, 7, note, tooltip=note)

            # 操作栏：一个按钮触发下拉菜单（照 API 代理页）
            ops_widget = QWidget()
            ops_widget.setAttribute(Qt.WA_TranslucentBackground, True)
            ops_layout = QHBoxLayout(ops_widget)
            ops_layout.setContentsMargins(4, 0, 4, 0)
            ops_layout.setSpacing(0)

            btn_ops = QToolButton()
            btn_ops.setObjectName("ops_btn")
            btn_ops.setText("操作 ▾")
            btn_ops.setCursor(Qt.PointingHandCursor)
            btn_ops.setToolButtonStyle(Qt.ToolButtonTextOnly)
            btn_ops.setPopupMode(QToolButton.InstantPopup)

            ops_menu = QMenu(btn_ops)
            _style_popup_menu(ops_menu)
            if state != "active":
                act = ops_menu.addAction("✅ 恢复")
                act.triggered.connect(
                    lambda checked, kid=key_id: self._set_key_relay_status(kid, "active"))
            else:
                act = ops_menu.addAction("🚫 禁用")
                act.triggered.connect(
                    lambda checked, kid=key_id: self._set_key_relay_status(kid, "disabled"))
            if state != "permanent_disabled":
                act = ops_menu.addAction("⛔ 永久禁用")
                act.triggered.connect(
                    lambda checked, kid=key_id: self._permanent_disable_key(kid))
            ops_menu.addSeparator()
            act = ops_menu.addAction("📋 复制 token")
            act.triggered.connect(
                lambda checked, kid=key_id: self._copy_key_token(kid))

            btn_ops.setMenu(ops_menu)
            ops_layout.addWidget(btn_ops)
            ops_layout.addStretch()
            self._pool_table.setCellWidget(row, 8, ops_widget)

        self._stat_total.setText(f"📋 总 Key: {len(keys)}")
        self._stat_active.setText(f"✅ 活跃: {active}")
        self._stat_disabled.setText(f"🚫 禁用: {disabled}")
        self._stat_used.setText(f"📊 总调用: {total_used}")

    def _set_key_relay_status(self, key_id: str, status: str):
        """单 Key 禁用/恢复（只写 relay_* 字段，不动主池 status）"""
        updates = {"relay_status": status}
        if status == "active":
            updates["relay_note"] = ""
            updates["relay_cooldown_until"] = 0
        self._db.update_upstream_key(key_id, updates)
        self._refresh_pool()

    def _permanent_disable_key(self, key_id: str):
        """永久禁用（不会被检测等自动恢复，只能手动恢复）"""
        ret = QMessageBox.question(
            self, "确认永久禁用",
            f"确定永久禁用 Key {key_id}？\n\n永久禁用后不会被检测等操作自动恢复，\n只能手动点击「恢复」来重新启用。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret == QMessageBox.Yes:
            self._set_key_relay_status(key_id, "permanent_disabled")

    def _copy_key_token(self, key_id: str):
        for k in self._jwt_keys():
            if k.get("key_id") == key_id:
                QApplication.clipboard().setText(k.get("api_key", ""))
                return

    def _open_batch_status_dialog(self, enable: bool, permanent: bool = False):
        """批量禁/解禁弹框（照 API 代理页：按积分范围筛选 + 实时预览 + 二次确认）

        只写 relay_* 字段，不动 API 代理页的 status。
        """
        keys = self._jwt_keys()
        if enable:
            action_text = "批量解禁"
            status_to = "active"
            done_verb = "解禁"
            confirm_template = "确定将上述 {n} 个 Key 恢复为可用吗？"
        elif permanent:
            action_text = "批量永久禁用"
            status_to = "permanent_disabled"
            done_verb = "永久禁用"
            confirm_template = "确定永久禁用上述 {n} 个 Key 吗？\n（永久禁用后只能手动解禁）"
        else:
            action_text = "批量临时禁用"
            status_to = "disabled"
            done_verb = "临时禁用"
            confirm_template = "确定临时禁用上述 {n} 个 Key 吗？"

        dlg = QDialog(self)
        dlg.setWindowTitle(action_text)
        dlg.setMinimumWidth(420)

        v = QVBoxLayout(dlg)

        # 输入区
        form = QFormLayout()
        min_spin = QSpinBox()
        min_spin.setRange(0, 9_999_999)
        min_spin.setValue(0 if not enable else 500)
        form.addRow("最小积分:", min_spin)

        max_spin = QSpinBox()
        max_spin.setRange(0, 9_999_999)
        max_spin.setValue(500 if not enable else 100_000)
        form.addRow("最大积分:", max_spin)

        v.addLayout(form)
        v.addWidget(QLabel(f"筛选条件：积分（剩余）在上述范围内的 Key\n操作类型：{action_text}（仅本页侧）"))

        # 预览（实时刷新）
        preview_label = QLabel("")
        preview_label.setWordWrap(True)
        preview_label.setObjectName("preview_label")
        v.addWidget(preview_label)

        def _refresh_preview():
            lo, hi = min_spin.value(), max_spin.value()
            matched = self._filter_keys_by_points(keys, lo, hi, status_to)
            in_range = [k for k in keys
                        if lo <= ApiProxyPage._points_remaining(k.get("points", "")) <= hi]
            already_done = len([k for k in in_range
                                if k.get("relay_status", "active") == status_to])
            preview_label.setText(
                f"范围匹配 {len(in_range)} 个，将实际生效 <b>{len(matched)}</b> 个"
                + (f"（{already_done} 个已{'启用' if enable else '禁用'}，跳过）" if already_done else "")
            )

        min_spin.valueChanged.connect(_refresh_preview)
        max_spin.valueChanged.connect(_refresh_preview)
        _refresh_preview()

        # 按钮
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        v.addWidget(btn_box)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setText(action_text)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        lo, hi = min_spin.value(), max_spin.value()
        matched = self._filter_keys_by_points(keys, lo, hi, status_to)
        if not matched:
            QMessageBox.information(self, "提示", "范围内没有可操作的 Key")
            return

        # 二次确认
        reply = QMessageBox.question(
            self,
            action_text,
            confirm_template.format(n=len(matched)) +
            f"\n积分范围：{lo} ~ {hi}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for k in matched:
            updates = {"relay_status": status_to}
            if status_to == "active":
                updates["relay_note"] = ""
                updates["relay_cooldown_until"] = 0
            self._db.update_upstream_key(k["key_id"], updates)

        self._refresh_pool()
        QMessageBox.information(self, "完成", f"已{done_verb} {len(matched)} 个 Key")

    @staticmethod
    def _filter_keys_by_points(keys: list, lo: int, hi: int, target_status: str = None) -> list:
        """按 points 剩余积分在 [lo, hi] 范围内筛选 Key，排除已处于目标状态的（看 relay_status）"""
        result = []
        for k in keys:
            if target_status and k.get("relay_status", "active") == target_status:
                continue  # 已经是目标状态，跳过
            pts = ApiProxyPage._points_remaining(k.get("points", ""))
            if pts < 0:
                continue  # 解析失败的跳过（没有积分数据）
            if lo <= pts <= hi:
                result.append(k)
        return result

    def _import_from_accounts(self):
        """从已获取账号导入 token（JWT）到池子，ck_ 卡密不导入"""
        existing_keys = self._db.get_upstream_keys()
        existing_api_keys = {k.get("api_key", "") for k in existing_keys}
        dialog = ImportFromAccountsDialog(self, existing_api_keys=existing_api_keys)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            accounts = dialog.get_selected_accounts()
            if not accounts:
                QMessageBox.warning(self, "提示", "请选择要导入的账号")
                return

            count = 0
            skipped = 0
            for acc in accounts:
                # 无感换号只收账号 token（JWT），ck_ 卡密走 API 代理那边
                import_key = acc.auth_token or ""
                if not import_key.startswith("eyJ"):
                    skipped += 1
                    continue

                existing_keys = self._db.get_upstream_keys()
                existing_api_keys = {k.get("api_key", "") for k in existing_keys}
                if import_key in existing_api_keys:
                    continue

                key_data = {
                    "key_id": f"ck_{secrets.token_hex(4)}",
                    "api_key": import_key,
                    "label": acc.display_name or acc.uid,
                    "status": "active",
                    "used_count": 0,
                    "points": f"{acc.quota.credits_remaining:.0f}/{acc.quota.credits_total:.0f}" if acc.quota and acc.quota.credits_total > 0 else "",
                    "points_updated_at": "imported" if acc.quota and acc.quota.credits_total > 0 else "",
                    "created_at": __import__('datetime').datetime.now().isoformat(),
                }
                self._db.add_upstream_key(key_data)
                count += 1

            self._refresh_pool()
            msg = f"成功导入 {count} 个 token Key"
            if skipped:
                msg += f"，跳过 {skipped} 个非 token 账号（卡密请去 API 代理页导入）"
            if count == 0 and not skipped:
                msg = "没有新的 Key 需要导入（可能已存在）"
            QMessageBox.information(self, "导入完成", msg)

    def _refresh_all_points(self):
        """查询所有 token Key 的积分并同步（照 API 代理页：后台 worker + 进度上状态行，不弹窗）"""
        from PySide6.QtCore import QThread, Signal as QSignal
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ...modules.api_client import ApiClient

        keys = [k for k in self._jwt_keys() if k.get("api_key", "")]
        if not keys:
            QMessageBox.information(self, "提示", "上游 Key 池为空，无需查询")
            return

        class PointsRefreshWorker(QThread):
            """Background worker for refreshing upstream key quota."""
            progress = QSignal(str)
            done = QSignal(int, int)  # success, failed

            def __init__(self, keys, db, max_workers=5):
                super().__init__()
                self._keys = keys
                self._db = db
                self.max_workers = max_workers

            def _query_one(self, k):
                api_key = k.get("api_key", "")
                label = k.get("label", api_key[:12])
                self.progress.emit(f"正在查询 {label}...")
                if api_key.startswith("ck_"):
                    client = ApiClient.from_api_key(api_key)
                else:
                    from ...utils.store import load_accounts
                    accounts = load_accounts()
                    acc = None
                    for a in accounts:
                        if a.auth_token == api_key or a.api_key == api_key:
                            acc = a
                            break
                    if acc and acc.api_key and acc.api_key.startswith("ck_"):
                        client = ApiClient.from_api_key(acc.api_key)
                    elif acc:
                        client = ApiClient(
                            access_token=acc.auth_token,
                            uid=acc.uid,
                            domain=acc.domain or "www.codebuddy.cn",
                        )
                    else:
                        client = ApiClient.from_api_key(api_key)
                return k, client.get_user_resource()

            def run(self):
                success = 0
                failed = 0
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {executor.submit(self._query_one, k): k for k in self._keys}
                    for future in as_completed(futures):
                        try:
                            k, result = future.result()
                            api_key = k.get("api_key", "")
                            if result.get("success"):
                                remaining = result.get("remaining_credits", 0)
                                total = result.get("total_credits", 0)
                                packages = result.get("packages", [])
                                self._db.sync_quota_to_key(
                                    api_key_or_token=api_key,
                                    remaining_credits=remaining,
                                    total_credits=total,
                                    packages=packages,
                                )
                                try:
                                    from ...utils.store import load_accounts, save_account
                                    accounts = load_accounts()
                                    for acc in accounts:
                                        if acc.auth_token == api_key or acc.api_key == api_key:
                                            acc.quota.credits_remaining = remaining
                                            acc.quota.credits_total = total
                                            save_account(acc)
                                            break
                                except Exception:
                                    pass
                                success += 1
                            else:
                                failed += 1
                        except Exception:
                            failed += 1
                self.done.emit(success, failed)

        max_workers = _get_account_concurrency_setting()
        self._points_worker = PointsRefreshWorker(keys, self._db, max_workers=max_workers)
        self._points_worker.progress.connect(
            lambda msg: self._stat_total.setText(f"⏳ {msg}")
        )
        self._points_worker.done.connect(self._on_points_refresh_done)
        self._points_worker.start()

    def _on_points_refresh_done(self, success: int, failed: int):
        """积分刷新完成回调（照 API 代理页：结果上状态行，不弹窗）"""
        self._refresh_pool()
        msg = f"积分刷新完成：✅ {success} 个成功"
        if failed > 0:
            msg += f"，❌ {failed} 个失败"
        self._stat_total.setText(f"📋 {msg}")

    def _check_all_key_status(self):
        """一键检测所有 token Key 是否被风控（403 code:11140），异常的本页侧禁用"""
        from PySide6.QtCore import QThread, Signal as QSignal
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ...modules.api_client import check_api_key_chat_status

        keys = [
            k for k in self._jwt_keys()
            if k.get("relay_status", "active") == "active"
        ]
        if not keys:
            QMessageBox.information(self, "提示", "没有需要检测的 Key（活跃的 token Key 为空）")
            return

        class KeyStatusCheckWorker(QThread):
            progress = QSignal(str)
            done = QSignal(int, int, int)  # normal, abnormal, failed

            def __init__(self, keys, db, max_workers=5):
                super().__init__()
                self._keys = keys
                self._db = db
                self.max_workers = max_workers

            def _check_one(self, k):
                api_key = k.get("api_key", "")
                label = k.get("label", api_key[:12])
                # JWT 临期/过期先续期再检测，避免可续期的 Key 被 401 误判
                if api_key.startswith("eyJ"):
                    from ...modules.proxy_server import refresh_pool_jwt_key
                    api_key = refresh_pool_jwt_key(self._db, k)
                self.progress.emit(f"检测 {label}...")
                result = check_api_key_chat_status(api_key, attempts=3)
                return k, label, result

            def run(self):
                normal = 0
                abnormal = 0
                failed = 0
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {executor.submit(self._check_one, k): k for k in self._keys}
                    for future in as_completed(futures):
                        try:
                            k, label, result = future.result()
                            key_id = k.get("key_id", "")
                            status_text = result.get("status_text", "check_failed")
                            self.progress.emit(f"{label}: {status_text}")
                            if result.get("flag") == "abnormal":
                                # 只动本页侧状态，不动 API 代理页的 status
                                self._db.update_upstream_key(key_id, {
                                    "relay_status": "disabled",
                                    "relay_note": "检测: 被上游风控(11140)",
                                })
                                abnormal += 1
                            elif result.get("flag") == "rate_limited":
                                self._db.update_upstream_key(key_id, {
                                    "relay_status": "disabled",
                                    "relay_note": "检测: 系统限流",
                                })
                                abnormal += 1
                            elif result.get("success"):
                                # 检测通过：自动禁用的（备注带「检测:」）恢复，手动禁用的不动
                                if k.get("relay_status") == "disabled" and \
                                        str(k.get("relay_note", "")).startswith("检测:"):
                                    self._db.update_upstream_key(key_id, {
                                        "relay_status": "active",
                                        "relay_note": "",
                                    })
                                normal += 1
                            else:
                                failed += 1
                        except Exception as e:
                            self.progress.emit(f"检测失败: {e}")
                            failed += 1
                self.done.emit(normal, abnormal, failed)

        max_workers = _get_account_concurrency_setting()
        self._status_check_worker = KeyStatusCheckWorker(keys, self._db, max_workers=max_workers)
        self._status_check_worker.progress.connect(
            lambda msg: self._stat_total.setText(f"🔍 {msg}")
        )
        self._status_check_worker.done.connect(self._on_status_check_done)
        self._status_check_worker.start()

    def _on_status_check_done(self, normal: int, abnormal: int, failed: int):
        """检测完成回调（照 API 代理页：结果上状态行，有异常才弹窗）"""
        self._refresh_pool()
        msg = f"检测完成：✅ 正常 {normal} 个"
        if abnormal > 0:
            msg += f"，⚠️ 异常 {abnormal} 个（已在本页侧禁用）"
        if failed > 0:
            msg += f"，❓ 失败 {failed} 个"
        self._stat_total.setText(f"📋 {msg}")
        if abnormal > 0:
            QMessageBox.warning(
                self, "检测完成",
                f"发现 {abnormal} 个 Key 被风控/限流，已在本页侧禁用。\n"
                f"禁用的 Key 不会再被中转调用。\n\n"
                f"正常: {normal}  异常: {abnormal}  失败: {failed}",
            )

    # ═══════════ 使用日志 ═══════════

    def _refresh_log(self):
        if not (self._relay_server and self._relay_server.is_running):
            self._log_edit.setPlainText("中转服务未运行。")
            return
        events = self._relay_server.get_events()
        st = self._relay_server.get_status()
        cur = st.get("current_key") or {}
        header = ""
        if cur:
            header = f"当前消耗: {cur.get('label', '-')}（剩余 {cur.get('points') or '?'} 分）\n"
        header += f"累计请求 {st['total_requests']} 次（换号 {st['swapped_requests']} 次）\n"
        header += "─" * 40
        self._log_edit.setPlainText(
            header + "\n" + ("\n".join(events) if events else "（暂无请求）"))

    def _clear_log(self):
        if self._relay_server and self._relay_server.is_running:
            self._relay_server.clear_events()
        self._log_edit.clear()
