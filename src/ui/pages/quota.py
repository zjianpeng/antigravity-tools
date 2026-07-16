"""配额/积分监控页面

展示所有账号的资源包积分、签到状态、付费类型等信息。
数据来源：copilot.tencent.com/v2/billing/meter/* API
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QScrollArea, QSizePolicy, QComboBox, QSpinBox, QProgressBar,
    QGridLayout, QGroupBox,
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal

from ...i18n import t
from ...models import Account, Platform, AccountStatus, ResourcePackage, CheckinStatus
from ...utils.store import load_accounts, save_account
from ...modules.api_client import ApiClient
from ...modules.proxy_server import ProxyDatabase


class QuotaQueryThread(QThread):
    """后台查询积分的线程"""
    result_ready = Signal(dict)  # 查询结果
    status_update = Signal(str)  # 状态更新

    def __init__(self, access_token: str, uid: str, domain: str = "www.codebuddy.cn", api_key: str = ""):
        super().__init__()
        self.access_token = access_token
        self.uid = uid
        self.domain = domain
        self.api_key = api_key  # API Key (ck_xxx)，优先使用

    def run(self):
        # 优先使用 API Key 模式
        if self.api_key and self.api_key.startswith("ck_"):
            client = ApiClient.from_api_key(self.api_key)
        else:
            client = ApiClient(
                access_token=self.access_token,
                uid=self.uid,
                domain=self.domain,
            )

        # 查询积分
        self.status_update.emit("正在查询积分...")
        resource_result = client.get_user_resource()

        # 查询付费类型
        payment_result = client.get_payment_type()

        # 查询签到状态
        self.status_update.emit("正在查询签到状态...")
        checkin_result = client.get_checkin_status()

        self.result_ready.emit({
            "resource": resource_result,
            "payment": payment_result,
            "checkin": checkin_result,
        })


class ResourcePackageCard(QFrame):
    """单个资源包卡片"""

    def __init__(self, pkg: ResourcePackage, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._pkg = pkg
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(12, 10, 12, 10)

        # 资源包名称 + 类型标签
        header = QHBoxLayout()
        name_label = QLabel(self._pkg.package_name or self._pkg.sub_product_name or "未知资源包")
        name_label.setStyleSheet("font-weight: 600; font-size: 13px;")
        name_label.setWordWrap(True)
        header.addWidget(name_label)

        type_badge = QLabel(self._pkg.type_label)
        type_color = "#2B6CB0" if self._pkg.package_type == "2" else "#718096"
        type_badge.setStyleSheet(
            f"background-color: {type_color}; color: white; padding: 1px 6px; border-radius: 3px; font-size: 10px;"
        )
        header.addWidget(type_badge)

        if self._pkg.is_exhausted:
            exhausted_badge = QLabel("已耗尽")
            exhausted_badge.setStyleSheet(
                "background-color: #E53E3E; color: white; padding: 1px 6px; border-radius: 3px; font-size: 10px;"
            )
            header.addWidget(exhausted_badge)

        header.addStretch()
        layout.addLayout(header)

        # 积分数值
        credits_layout = QHBoxLayout()

        remain_label = QLabel(f"{self._pkg.cycle_remain:.0f}")
        remain_color = "#E53E3E" if self._pkg.is_exhausted else ("#D69E2E" if self._pkg.remain_percentage < 20 else "#38A169")
        remain_label.setStyleSheet(f"font-size: 20px; font-weight: 700; color: {remain_color};")
        credits_layout.addWidget(remain_label)

        total_label = QLabel(f" / {self._pkg.cycle_size:.0f} {self._pkg.capacity_unit}")
        total_label.setStyleSheet("font-size: 12px; color: #9BA4B0;")
        credits_layout.addWidget(total_label)
        credits_layout.addStretch()
        layout.addLayout(credits_layout)

        # 进度条
        pct = self._pkg.remain_percentage
        progress = QProgressBar()
        progress.setValue(int(pct))
        progress.setFormat(f"{pct:.1f}%")
        progress.setMaximumHeight(8)
        if pct < 10:
            progress.setStyleSheet("QProgressBar::chunk { background-color: #E53E3E; border-radius: 4px; }")
        elif pct < 30:
            progress.setStyleSheet("QProgressBar::chunk { background-color: #D69E2E; border-radius: 4px; }")
        else:
            progress.setStyleSheet("QProgressBar::chunk { background-color: #38A169; border-radius: 4px; }")
        layout.addWidget(progress)

        # 周期信息
        if self._pkg.cycle_start and self._pkg.cycle_end:
            cycle_label = QLabel(f"周期: {self._pkg.cycle_start[:10]} ~ {self._pkg.cycle_end[:10]}")
            cycle_label.setStyleSheet("font-size: 10px; color: #9BA4B0;")
            layout.addWidget(cycle_label)


class AccountQuotaCard(QFrame):
    """单个账号的完整配额卡片"""

    def __init__(self, account: Account, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._account = account
        self._query_thread = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 14, 16, 14)

        # 顶部：账号名 + 平台 + 操作按钮
        header = QHBoxLayout()

        name_label = QLabel(self._account.display_name)
        name_label.setStyleSheet("font-weight: 600; font-size: 15px;")
        header.addWidget(name_label)

        platform_badge = QLabel(self._account.platform.value)
        platform_badge.setStyleSheet(
            "background-color: #2B6CB0; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px;"
        )
        header.addWidget(platform_badge)

        header.addStretch()

        # 签到状态
        self._checkin_label = QLabel("⏳ 未查询")
        self._checkin_label.setStyleSheet("font-size: 12px; color: #9BA4B0;")
        header.addWidget(self._checkin_label)

        # 签到按钮
        self._checkin_btn = QPushButton("签到")
        self._checkin_btn.setObjectName("secondary_btn")
        self._checkin_btn.setCursor(Qt.PointingHandCursor)
        self._checkin_btn.setMaximumWidth(60)
        self._checkin_btn.clicked.connect(self._do_checkin)
        header.addWidget(self._checkin_btn)

        # 刷新按钮
        btn_refresh = QPushButton("刷新")
        btn_refresh.setObjectName("secondary_btn")
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.setMaximumWidth(60)
        btn_refresh.clicked.connect(self._query_quota)
        header.addWidget(btn_refresh)

        layout.addLayout(header)

        # 总积分
        self._total_label = QLabel("💎 点击「刷新」查询积分")
        self._total_label.setStyleSheet("font-size: 13px; color: #9BA4B0;")
        layout.addWidget(self._total_label)

        # 付费类型
        self._payment_label = QLabel("")
        self._payment_label.setStyleSheet("font-size: 11px; color: #9BA4B0;")
        layout.addWidget(self._payment_label)

        # 资源包容器
        self._packages_layout = QVBoxLayout()
        self._packages_layout.setSpacing(8)
        layout.addLayout(self._packages_layout)

        # 状态标签
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("font-size: 11px; color: #9BA4B0;")
        layout.addWidget(self._status_label)

    def _query_quota(self):
        """查询积分"""
        if not self._account.auth_token and not self._account.api_key:
            self._status_label.setText("❌ 无 token，请先添加账号")
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 11px;")
            return

        self._status_label.setText("⏳ 正在查询...")
        self._status_label.setStyleSheet("color: #D69E2E; font-size: 11px;")

        self._query_thread = QuotaQueryThread(
            access_token=self._account.auth_token,
            uid=self._account.uid,
            domain=self._account.domain or "www.codebuddy.cn",
            api_key=self._account.api_key or "",
        )
        self._query_thread.result_ready.connect(self._on_quota_result)
        self._query_thread.status_update.connect(self._on_status_update)
        self._query_thread.start()

    def _on_status_update(self, text: str):
        self._status_label.setText(f"⏳ {text}")

    def _on_quota_result(self, result: dict):
        """积分查询结果回调"""
        # 解析资源包
        resource_result = result.get("resource", {})
        payment_result = result.get("payment", {})
        checkin_result = result.get("checkin", {})

        if resource_result.get("success"):
            packages = resource_result.get("packages", [])
            total_credits = resource_result.get("total_credits", 0)
            remaining_credits = resource_result.get("remaining_credits", 0)

            # 更新总积分
            self._total_label.setText(
                f"💎 总积分: {remaining_credits:.0f} / {total_credits:.0f}"
            )
            self._total_label.setStyleSheet("font-size: 14px; font-weight: 600;")

            # 清空旧的资源包卡片
            while self._packages_layout.count():
                item = self._packages_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

            # 添加新的资源包卡片
            for pkg in packages:
                card = ResourcePackageCard(pkg)
                self._packages_layout.addWidget(card)

            self._status_label.setText(f"✅ 已更新（{len(packages)} 个资源包）")
            self._status_label.setStyleSheet("color: #38A169; font-size: 11px;")

            # 更新 Account 的 QuotaInfo
            self._account.quota.credits_remaining = remaining_credits
            self._account.quota.credits_total = total_credits
            self._account.quota.packages = packages
            self._account.quota.last_updated = __import__('datetime').datetime.now()
            save_account(self._account)

            # 联动更新上游 Key 池：同步积分、自动禁用/恢复
            try:
                db = ProxyDatabase.get_instance()
                # 优先用 API Key 匹配，其次用 auth_token
                match_key = self._account.api_key if (self._account.api_key and self._account.api_key.startswith("ck_")) else self._account.auth_token
                db.sync_quota_to_key(
                    api_key_or_token=match_key,
                    remaining_credits=remaining_credits,
                    total_credits=total_credits,
                    packages=packages,
                )
            except Exception:
                pass
        else:
            error = resource_result.get("error", "未知错误")
            self._total_label.setText(f"❌ 查询失败: {error}")
            self._total_label.setStyleSheet("color: #E53E3E; font-size: 13px;")
            self._status_label.setText("")

        # 付费类型
        if payment_result.get("success"):
            pt = payment_result.get("payment_type", "unknown")
            pt_map = {"free": "免费版", "pro": "专业版", "team": "团队版", "enterprise": "企业版"}
            self._payment_label.setText(f"📋 套餐: {pt_map.get(pt, pt)}")
            self._account.quota.payment_type = pt
        else:
            self._payment_label.setText("")

        # 签到状态
        if checkin_result.get("success"):
            cs = checkin_result.get("data")
            if isinstance(cs, CheckinStatus):
                self._account.quota.checkin_status = cs
                if cs.today_checked_in:
                    self._checkin_label.setText(f"✅ 已签到（连续 {cs.streak_days} 天）")
                    self._checkin_label.setStyleSheet("color: #38A169; font-size: 12px;")
                    self._checkin_btn.setEnabled(False)
                    self._checkin_btn.setText("已签")
                else:
                    self._checkin_label.setText(f"⏳ 未签到（连续 {cs.streak_days} 天）")
                    self._checkin_label.setStyleSheet("color: #D69E2E; font-size: 12px;")
                    self._checkin_btn.setEnabled(True)
        else:
            self._checkin_label.setText("⏳ 签到状态未知")
            self._checkin_label.setStyleSheet("color: #9BA4B0; font-size: 12px;")

    def _do_checkin(self):
        """执行签到"""
        if not self._account.auth_token and not self._account.api_key:
            return

        self._checkin_btn.setEnabled(False)
        self._checkin_btn.setText("签到中...")
        self._checkin_label.setText("⏳ 签到中...")

        # 优先使用 API Key 模式
        if self._account.api_key and self._account.api_key.startswith("ck_"):
            client = ApiClient.from_api_key(self._account.api_key)
        else:
            client = ApiClient(
                access_token=self._account.auth_token,
                uid=self._account.uid,
                domain=self._account.domain or "www.codebuddy.cn",
            )
        result = client.daily_checkin()

        if result.get("success"):
            credit = result.get("credit", 0)
            streak = result.get("streak_days", 0)
            self._checkin_label.setText(f"✅ 签到成功！+{credit} 积分（连续 {streak} 天）")
            self._checkin_label.setStyleSheet("color: #38A169; font-size: 12px;")
            self._checkin_btn.setText("已签")

            # 签到后刷新积分
            self._query_quota()
        else:
            error = result.get("error", "未知错误")
            self._checkin_label.setText(f"❌ 签到失败: {error}")
            self._checkin_label.setStyleSheet("color: #E53E3E; font-size: 12px;")
            self._checkin_btn.setEnabled(True)
            self._checkin_btn.setText("签到")


class QuotaPage(QWidget):
    """配额/积分监控页面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content_area")
        self._first_show = True  # 标记是否首次显示
        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self._refresh_cards)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel(t("quota.title"))
        title.setObjectName("page_title")
        layout.addWidget(title)

        subtitle = QLabel("实时监控各平台账号的积分使用情况，一键签到")
        subtitle.setObjectName("page_subtitle")
        layout.addWidget(subtitle)

        content = QWidget()
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(32, 0, 32, 32)
        self._content_layout.setSpacing(16)

        # 工具栏
        toolbar = QHBoxLayout()

        self._filter_combo = QComboBox()
        self._filter_combo.addItem("全部平台", None)
        for p in Platform:
            self._filter_combo.addItem(p.value, p)
        self._filter_combo.currentIndexChanged.connect(self._refresh_cards)
        toolbar.addWidget(self._filter_combo)

        toolbar.addStretch()

        # 自动刷新
        auto_label = QLabel("自动刷新:")
        toolbar.addWidget(auto_label)

        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(5, 120)
        self._interval_spin.setValue(30)
        self._interval_spin.setSuffix(" 分钟")
        toolbar.addWidget(self._interval_spin)

        btn_toggle_auto = QPushButton("启用")
        btn_toggle_auto.setObjectName("secondary_btn")
        btn_toggle_auto.setCheckable(True)
        btn_toggle_auto.toggled.connect(self._toggle_auto_refresh)
        toolbar.addWidget(btn_toggle_auto)

        self._btn_refresh_all = QPushButton("刷新全部")
        self._btn_refresh_all.setObjectName("primary_btn")
        self._btn_refresh_all.setCursor(Qt.PointingHandCursor)
        self._btn_refresh_all.clicked.connect(self._refresh_all_quotas)
        toolbar.addWidget(self._btn_refresh_all)

        self._btn_checkin_all = QPushButton("全部签到")
        self._btn_checkin_all.setObjectName("primary_btn")
        self._btn_checkin_all.setCursor(Qt.PointingHandCursor)
        self._btn_checkin_all.clicked.connect(self._checkin_all)
        toolbar.addWidget(self._btn_checkin_all)

        self._content_layout.addLayout(toolbar)

        # 查询状态
        self._query_status = QLabel("")
        self._query_status.setStyleSheet("font-size: 11px; color: #9BA4B0;")
        self._content_layout.addWidget(self._query_status)

        # 卡片容器（可滚动）
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)

        self._cards_container = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setSpacing(12)
        scroll_area.setWidget(self._cards_container)

        self._content_layout.addWidget(scroll_area)
        self._content_layout.addStretch()
        layout.addWidget(content)

    def _refresh_cards(self):
        """刷新配额卡片（只重建 UI，不自动查询）"""
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        accounts = load_accounts()
        platform = self._filter_combo.currentData()
        if platform:
            accounts = [a for a in accounts if a.platform == platform]

        for account in accounts:
            card = AccountQuotaCard(account)
            self._cards_layout.addWidget(card)

        if not accounts:
            empty_label = QLabel("暂无账号，请先添加账号")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("color: #9BA4B0; font-size: 14px; padding: 40px;")
            self._cards_layout.addWidget(empty_label)

    def _refresh_all_quotas(self):
        """刷新所有账号的积分"""
        accounts = load_accounts()
        platform = self._filter_combo.currentData()
        if platform:
            accounts = [a for a in accounts if a.platform == platform]

        for i in range(self._cards_layout.count()):
            item = self._cards_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), AccountQuotaCard):
                item.widget()._query_quota()

    def _checkin_all(self):
        """全部签到"""
        for i in range(self._cards_layout.count()):
            item = self._cards_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), AccountQuotaCard):
                card = item.widget()
                if card._checkin_btn.isEnabled():
                    card._do_checkin()

    def _toggle_auto_refresh(self, enabled: bool):
        """切换自动刷新"""
        if enabled:
            interval = self._interval_spin.value() * 60 * 1000
            self._refresh_timer.start(interval)
        else:
            self._refresh_timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_cards()
        # 首次进入时自动查询所有账号积分
        if self._first_show:
            self._first_show = False
            accounts = load_accounts()
            has_token = [a for a in accounts if a.auth_token]
            if has_token:
                # 延迟 200ms 自动查询，避免 UI 还没完全加载
                QTimer.singleShot(200, self._refresh_all_quotas)
