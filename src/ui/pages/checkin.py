"""签到页面"""

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar, QComboBox,
    QSpinBox, QTextEdit, QTimeEdit
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QTime

from ...i18n import t
from ...models import Account, Platform, AccountStatus
from ...utils.store import load_accounts, save_account, save_setting, load_setting
from ...modules import CheckinManager
from ...modules.api_client import ApiClient

PAGE_SIZE = 100  # 每页显示条数


class StatusRefreshWorker(QThread):
    """后台刷新签到状态 - 通过 daily-checkin API 检测是否已签到

    说明：checkin-status API 当前返回全空数据（可能已废弃），
    所以改用 daily-checkin 返回的 code=10001 来判断今日已签到。
    刷新后会查询积分包数据推算本月签到积分。
    """
    progress = Signal(str, bool, int, int)   # uid, checked_today, daily_credit, monthly_credits
    finished_all = Signal()

    def __init__(self, accounts: list, proxy: str = None, max_workers: int = 5):
        super().__init__()
        self.accounts = accounts
        self.proxy = proxy
        self.max_workers = max_workers
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def _process_account(self, account):
        """处理单个账号，返回 (uid, checked_today, daily_credit, monthly_credits)"""
        try:
            # 优先使用 API Key 模式
            if account.api_key and account.api_key.startswith("ck_"):
                client = ApiClient.from_api_key(account.api_key, proxy=self.proxy)
            else:
                client = ApiClient.from_account(account, proxy=self.proxy)
            result = client.daily_checkin()
            if result["success"]:
                checked = True
                credit = 0 if result.get("already") else result.get("credit", 0)
                account.checkin.mark_checked_today(credit)

                # 从积分包推算本月签到积分
                try:
                    quota_result = client.get_user_resource()
                    if quota_result["success"]:
                        packages = quota_result.get("packages", [])
                        monthly = self._calc_monthly_credits(packages)
                        if monthly > 0:
                            account.checkin.total_credits = monthly
                        # 联动更新上游 Key 池（用 API Key 或 auth_token 匹配）
                        try:
                            from ...modules.proxy_server import ProxyDatabase
                            db = ProxyDatabase.get_instance()
                            match_key = account.api_key if (account.api_key and account.api_key.startswith("ck_")) else account.auth_token
                            db.sync_quota_to_key(
                                api_key_or_token=match_key,
                                remaining_credits=quota_result.get("remaining_credits", 0),
                                total_credits=quota_result.get("total_credits", 0),
                                packages=packages,
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

                save_account(account)
                return (account.uid, checked, account.checkin.daily_credit,
                        account.checkin.total_credits)
            else:
                return (account.uid, account.checkin.checked_today, 0,
                        account.checkin.total_credits)
        except Exception:
            return (account.uid, account.checkin.checked_today, 0,
                    account.checkin.total_credits)

    @staticmethod
    def _calc_monthly_credits(packages) -> int:
        """只统计本月运营裂变包的积分"""
        now = datetime.now()
        month_prefix = now.strftime("%Y-%m")
        total = 0
        for pkg in packages:
            name = pkg.package_name or ""
            if "运营裂变包" in name:
                if pkg.cycle_start and pkg.cycle_start.startswith(month_prefix):
                    total += int(pkg.capacity_size)
        return total

    def run(self):
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self._process_account, acc): acc
                       for acc in self.accounts}
            for future in as_completed(futures):
                if self._stop_flag:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    self.progress.emit(*future.result())
                except Exception:
                    pass
        self.finished_all.emit()


class CheckinWorker(QThread):
    """并发签到后台线程"""
    progress = Signal(str, str, str)  # account_name, status, message
    finished_all = Signal(dict)       # 结果汇总

    def __init__(self, accounts: list, proxy: str = None, max_workers: int = 5):
        super().__init__()
        self.accounts = accounts
        self.proxy = proxy
        self.max_workers = max_workers
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def _checkin_one(self, account):
        """签到单个账号，返回 (name, status, message)"""
        if self._stop_flag:
            return (account.display_name, "stopped", "已停止")
        try:
            manager = CheckinManager()
            result = manager.checkin_account(account, proxy=self.proxy)
            if result["success"]:
                if result.get("already"):
                    return (account.display_name, "already",
                            f"今日已签到 (连续{account.checkin.streak_days}天)")
                else:
                    credit = result.get("credit", 0)
                    return (account.display_name, "success",
                            f"签到成功！+{credit}积分 (连续{account.checkin.streak_days}天)")
            else:
                return (account.display_name, "failed", result.get("error", "失败"))
        except Exception as e:
            return (account.display_name, "failed", str(e))

    def run(self):
        results = {"success": 0, "failed": 0, "already": 0, "stopped": 0}

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self._checkin_one, acc): acc
                       for acc in self.accounts}
            for future in as_completed(futures):
                if self._stop_flag:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    name, status, message = future.result()
                    self.progress.emit(name, status, message)
                    if status in results:
                        results[status] += 1
                except Exception:
                    results["failed"] += 1

        self.finished_all.emit(results)


class CheckinPage(QWidget):
    """每日签到页面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content_area")
        self._worker = None
        self._status_worker = None
        self._accounts = []       # 全量账号（当前筛选后）
        self._is_checking = False
        self._current_page = 0    # 当前页码（0-based）
        self._timer_active = False  # 定时签到是否开启
        self._sort_column = None
        self._sort_order = Qt.AscendingOrder
        self._setup_ui()

        # 定时签到计时器 — 每秒检查一次是否到达设定时间
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_timer_tick)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题
        title = QLabel(t("checkin.title"))
        title.setObjectName("page_title")
        layout.addWidget(title)

        subtitle = QLabel("每日签到领取积分奖励，支持批量并发签到 · 双击行签到单个账号")
        subtitle.setObjectName("page_subtitle")
        layout.addWidget(subtitle)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(32, 0, 32, 32)
        content_layout.setSpacing(16)

        # 统计行
        stats_row = QHBoxLayout()
        stats_row.setSpacing(16)

        self._stat_total = QLabel("📋 总计: 0")
        self._stat_total.setStyleSheet("font-size: 14px; font-weight: 600;")
        stats_row.addWidget(self._stat_total)

        self._stat_checked = QLabel("✅ 已签到: 0")
        self._stat_checked.setStyleSheet("font-size: 14px; font-weight: 600; color: #38A169;")
        stats_row.addWidget(self._stat_checked)

        self._stat_unchecked = QLabel("⏳ 未签到: 0")
        self._stat_unchecked.setStyleSheet("font-size: 14px; font-weight: 600; color: #D69E2E;")
        stats_row.addWidget(self._stat_unchecked)

        self._stat_monthly_credits = QLabel("💰 本月积分: 0")
        self._stat_monthly_credits.setStyleSheet("font-size: 14px; font-weight: 600; color: #805AD5;")
        stats_row.addWidget(self._stat_monthly_credits)

        stats_row.addStretch()

        # 刷新状态按钮
        self._btn_refresh_status = QPushButton("🔄 刷新状态")
        self._btn_refresh_status.setObjectName("secondary_btn")
        self._btn_refresh_status.setCursor(Qt.PointingHandCursor)
        self._btn_refresh_status.clicked.connect(self._refresh_status_online)
        stats_row.addWidget(self._btn_refresh_status)

        content_layout.addLayout(stats_row)

        # 筛选和操作
        toolbar = QHBoxLayout()

        self._filter_combo = QComboBox()
        self._filter_combo.addItem("全部平台", None)
        for p in Platform:
            self._filter_combo.addItem(p.value, p)
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._filter_combo.setVisible(False)

        # 并发数设置
        toolbar.addWidget(QLabel("并发数:"))
        self._concurrency_spin = QSpinBox()
        self._concurrency_spin.setRange(1, 50)
        self._concurrency_spin.setValue(int(load_setting("checkin_concurrency", "5")))
        self._concurrency_spin.setToolTip("同时请求线程数，范围 1-50")
        self._concurrency_spin.valueChanged.connect(
            lambda value: save_setting("checkin_concurrency", str(value))
        )
        self._concurrency_spin.setFixedWidth(60)
        toolbar.addWidget(self._concurrency_spin)

        toolbar.addStretch()

        # 全部签到按钮
        self._btn_checkin_all = QPushButton(f"✅ {t('checkin.checkin_all')}")
        self._btn_checkin_all.setObjectName("primary_btn")
        self._btn_checkin_all.setCursor(Qt.PointingHandCursor)
        self._btn_checkin_all.clicked.connect(self._checkin_all)
        toolbar.addWidget(self._btn_checkin_all)

        # 停止按钮
        self._btn_stop = QPushButton("⏹ 停止")
        self._btn_stop.setObjectName("secondary_btn")
        self._btn_stop.setStyleSheet(
            "QPushButton { color: #FC8181; border: 1px solid #FC8181; }"
            "QPushButton:hover { background-color: rgba(229,62,62,0.1); }"
        )
        self._btn_stop.setCursor(Qt.PointingHandCursor)
        self._btn_stop.setVisible(False)
        self._btn_stop.clicked.connect(self._stop_checkin)
        toolbar.addWidget(self._btn_stop)

        # 分隔符
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("color: #4A5568;")
        toolbar.addWidget(sep)

        # 定时签到
        toolbar.addWidget(QLabel("⏰ 定时:"))
        self._time_edit = QTimeEdit()
        self._time_edit.setDisplayFormat("HH:mm")
        now = QTime.currentTime()
        # 默认设为明天早上8点
        self._time_edit.setTime(QTime(8, 0))
        self._time_edit.setFixedWidth(80)
        self._time_edit.setToolTip("设置每天自动签到的时间")
        toolbar.addWidget(self._time_edit)

        self._btn_timer = QPushButton("启动定时")
        self._btn_timer.setObjectName("secondary_btn")
        self._btn_timer.setCursor(Qt.PointingHandCursor)
        self._btn_timer.clicked.connect(self._toggle_timer)
        toolbar.addWidget(self._btn_timer)

        self._timer_status = QLabel("")
        self._timer_status.setStyleSheet("font-size: 12px; color: #805AD5;")
        toolbar.addWidget(self._timer_status)

        content_layout.addLayout(toolbar)

        # 签到表格 — 不再用 setCellWidget 放按钮，双击行签到
        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels([
            "账号", "平台", "签到状态", "今日积分", "本月积分"
        ])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_header_sort)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.doubleClicked.connect(self._on_table_double_click)
        content_layout.addWidget(self._table, 1)

        # 翻页栏
        pager_row = QHBoxLayout()
        self._btn_prev = QPushButton("◀ 上一页")
        self._btn_prev.setObjectName("secondary_btn")
        self._btn_prev.clicked.connect(self._prev_page)
        pager_row.addWidget(self._btn_prev)

        self._page_label = QLabel("0 / 0")
        self._page_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        self._page_label.setAlignment(Qt.AlignCenter)
        pager_row.addWidget(self._page_label)

        self._btn_next = QPushButton("下一页 ▶")
        self._btn_next.setObjectName("secondary_btn")
        self._btn_next.clicked.connect(self._next_page)
        pager_row.addWidget(self._btn_next)

        pager_row.addStretch()

        # 跳页
        pager_row.addWidget(QLabel("跳到:"))
        self._page_spin = QSpinBox()
        self._page_spin.setRange(1, 1)
        self._page_spin.setFixedWidth(70)
        self._page_spin.valueChanged.connect(self._goto_page)
        pager_row.addWidget(self._page_spin)

        content_layout.addLayout(pager_row)

        # 进度
        self._progress_bar = QProgressBar()
        self._progress_bar.setVisible(False)
        content_layout.addWidget(self._progress_bar)

        # 结果日志
        self._log_edit = QTextEdit()
        self._log_edit.setObjectName("log_edit")
        self._log_edit.setReadOnly(True)
        self._log_edit.setMaximumHeight(150)
        self._log_edit.setVisible(False)
        content_layout.addWidget(self._log_edit)

        layout.addWidget(content)

    # === 分页逻辑 ===

    @property
    def _total_pages(self) -> int:
        return max(1, (len(self._accounts) + PAGE_SIZE - 1) // PAGE_SIZE)

    def _get_page_accounts(self) -> list:
        start = self._current_page * PAGE_SIZE
        end = start + PAGE_SIZE
        return self._accounts[start:end]

    def _update_pager(self):
        total = self._total_pages
        self._page_label.setText(f"{self._current_page + 1} / {total}")
        self._btn_prev.setEnabled(self._current_page > 0)
        self._btn_next.setEnabled(self._current_page < total - 1)
        self._page_spin.setRange(1, total)
        self._page_spin.blockSignals(True)
        self._page_spin.setValue(self._current_page + 1)
        self._page_spin.blockSignals(False)

    def _prev_page(self):
        if self._current_page > 0:
            self._current_page -= 1
            self._render_page()

    def _next_page(self):
        if self._current_page < self._total_pages - 1:
            self._current_page += 1
            self._render_page()

    def _goto_page(self, page: int):
        if page >= 1 and page <= self._total_pages:
            self._current_page = page - 1
            self._render_page()

    # === 数据加载 & 渲染 ===

    def _load_accounts(self):
        """加载全量账号"""
        self._accounts = load_accounts()
        self._apply_sort()

    def _account_sort_value(self, account: Account, column: int):
        if column == 0:
            return account.display_name.lower()
        if column == 1:
            return account.platform.value
        if column == 2:
            return 0 if account.checkin.checked_today else 1
        if column == 3:
            return account.checkin.daily_credit if account.checkin.checked_today else 0
        if column == 4:
            return account.checkin.total_credits
        return ""

    def _apply_sort(self):
        if self._sort_column is None:
            return
        reverse = self._sort_order == Qt.DescendingOrder
        self._accounts.sort(
            key=lambda account: self._account_sort_value(account, self._sort_column),
            reverse=reverse,
        )

    def _on_header_sort(self, section: int):
        if self._sort_column == section:
            self._sort_order = Qt.DescendingOrder if self._sort_order == Qt.AscendingOrder else Qt.AscendingOrder
        else:
            self._sort_column = section
            self._sort_order = Qt.AscendingOrder
        self._table.horizontalHeader().setSortIndicator(section, self._sort_order)
        self._apply_sort()
        self._current_page = 0
        self._render_page()

    def _update_stats(self):
        """更新统计栏"""
        total = len(self._accounts)
        checked = len([a for a in self._accounts if a.checkin.checked_today])
        monthly_credits = sum(a.checkin.total_credits for a in self._accounts)
        self._stat_total.setText(f"📋 总计: {total}")
        self._stat_checked.setText(f"✅ 已签到: {checked}")
        self._stat_unchecked.setText(f"⏳ 未签到: {total - checked}")
        self._stat_monthly_credits.setText(f"💰 本月积分: {monthly_credits}")

    def _render_page(self):
        """只渲染当前页的表格（轻量）"""
        page_accounts = self._get_page_accounts()
        self._table.setRowCount(len(page_accounts))

        for row, account in enumerate(page_accounts):
            self._table.setItem(row, 0, QTableWidgetItem(account.display_name))
            self._table.setItem(row, 1, QTableWidgetItem(account.platform.value))

            status_text = "✅ 已签到" if account.checkin.checked_today else "⏳ 未签到"
            status_item = QTableWidgetItem(status_text)
            if account.checkin.checked_today:
                status_item.setForeground(Qt.darkGreen)
            self._table.setItem(row, 2, status_item)

            # 今日积分
            daily = account.checkin.daily_credit if account.checkin.checked_today else 0
            daily_item = QTableWidgetItem(f"+{daily}" if daily > 0 else "-")
            if daily > 0:
                daily_item.setForeground(Qt.darkGreen)
            self._table.setItem(row, 3, daily_item)

            # 本月积分
            monthly = account.checkin.total_credits
            monthly_item = QTableWidgetItem(str(monthly) if monthly > 0 else "-")
            if monthly > 0:
                monthly_item.setForeground(Qt.blue)
            self._table.setItem(row, 4, monthly_item)

        self._update_pager()

    def _refresh_table(self):
        """全量刷新（重新加载+渲染当前页）"""
        self._load_accounts()
        self._update_stats()
        self._current_page = 0
        self._render_page()

    def _on_filter_changed(self):
        """筛选变化时重置到第一页"""
        self._load_accounts()
        self._update_stats()
        self._current_page = 0
        self._render_page()

    # === 双击签到 ===

    def _on_table_double_click(self, index):
        """双击表格行签到单个账号"""
        page_accounts = self._get_page_accounts()
        row = index.row()
        if row >= len(page_accounts):
            return
        account = page_accounts[row]
        if account.checkin.checked_today:
            return
        manager = CheckinManager()
        result = manager.checkin_account(account)
        # 只刷新当前页
        self._load_accounts()
        self._update_stats()
        self._render_page()

    # === 在线刷新状态 ===

    def _refresh_status_online(self):
        """在线刷新所有账号的签到状态"""
        self._load_accounts()
        if not self._accounts:
            return

        max_workers = self._concurrency_spin.value()
        self._btn_refresh_status.setEnabled(False)
        self._btn_refresh_status.setText("🔄 刷新中...")
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, len(self._accounts))
        self._progress_bar.setValue(0)

        self._status_worker = StatusRefreshWorker(self._accounts, max_workers=max_workers)
        self._status_worker.progress.connect(self._on_status_refresh)
        self._status_worker.finished_all.connect(self._on_status_refresh_done)
        self._status_worker.start()

    def _on_status_refresh(self, uid: str, checked_today: bool, daily_credit: int, monthly_credits: int):
        """单个账号状态刷新完成"""
        current = self._progress_bar.value() + 1
        self._progress_bar.setValue(current)

        # 更新缓存中对应账号的状态
        for account in self._accounts:
            if account.uid == uid:
                if checked_today:
                    account.checkin.mark_checked_today(daily_credit)
                account.checkin.total_credits = monthly_credits
                break

        # 每50个刷新一次当前页（避免频繁重建表格）
        if current % 50 == 0 or current == self._progress_bar.maximum():
            self._update_stats()
            self._render_page()

    def _on_status_refresh_done(self):
        """所有账号状态刷新完成"""
        self._progress_bar.setVisible(False)
        self._btn_refresh_status.setEnabled(True)
        self._btn_refresh_status.setText("🔄 刷新状态")
        self._update_stats()
        self._render_page()

    # === 批量签到 ===

    def _checkin_all(self):
        """全部签到 — 只签未签到的账号，并发执行"""
        self._load_accounts()
        # 过滤掉已签到的
        unchecked = [a for a in self._accounts if not a.checkin.checked_today]
        if not unchecked:
            self._log_edit.setVisible(True)
            self._append_log("📋 所有账号今日已签到，无需操作")
            return

        max_workers = self._concurrency_spin.value()

        self._btn_checkin_all.setVisible(False)
        self._btn_stop.setVisible(True)
        self._is_checking = True

        self._worker = CheckinWorker(unchecked, max_workers=max_workers)
        self._worker.progress.connect(self._on_checkin_progress)
        self._worker.finished_all.connect(self._on_checkin_finished)
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, len(unchecked))
        self._progress_bar.setValue(0)

        self._log_edit.clear()
        self._log_edit.setVisible(True)
        skipped = len(self._accounts) - len(unchecked)
        if skipped:
            self._append_log(f"⏭️ 跳过{skipped}个已签到账号")
        self._append_log(f"🚀 开始签到 {len(unchecked)} 个账号，并发数: {max_workers}")

        self._worker.start()

    def _stop_checkin(self):
        """停止签到"""
        if self._worker:
            self._worker.stop()
            self._append_log("⏹ 正在停止签到...")
        if self._status_worker:
            self._status_worker.stop()
        self._btn_stop.setEnabled(False)

    def _append_log(self, text: str):
        """追加日志并自动滚到底部"""
        self._log_edit.append(text)
        scrollbar = self._log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_checkin_progress(self, name: str, status: str, message: str):
        """签到进度回调"""
        current = self._progress_bar.value() + 1
        self._progress_bar.setValue(current)
        icon = "✅" if status == "success" else "⏭️" if status == "already" else "❌"
        self._append_log(f"{icon} {name}: {message}")

    def _on_checkin_finished(self, results: dict):
        """签到完成"""
        self._progress_bar.setVisible(False)
        self._is_checking = False
        self._btn_checkin_all.setVisible(True)
        self._btn_stop.setVisible(False)
        self._btn_stop.setEnabled(True)

        self._append_log(
            f"📊 完成！成功{results.get('success', 0)}，"
            f"已签到{results.get('already', 0)}，"
            f"失败{results.get('failed', 0)}，"
            f"停止{results.get('stopped', 0)}"
        )
        # 签到完成后重新加载刷新
        self._load_accounts()
        self._update_stats()
        self._render_page()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_table()

    # === 定时签到 ===

    def _toggle_timer(self):
        """开启/关闭定时签到"""
        if self._timer_active:
            self._stop_timer()
        else:
            self._start_timer()

    def _start_timer(self):
        """启动定时签到"""
        target = self._time_edit.time()
        self._timer_active = True
        self._btn_timer.setText("关闭定时")
        self._btn_timer.setStyleSheet(
            "QPushButton { color: #FC8181; border: 1px solid #FC8181; }"
            "QPushButton:hover { background-color: rgba(229,62,62,0.1); }"
        )
        self._time_edit.setEnabled(False)
        self._timer.start()
        self._update_timer_status()

    def _stop_timer(self):
        """关闭定时签到"""
        self._timer_active = False
        self._timer.stop()
        self._btn_timer.setText("启动定时")
        self._btn_timer.setStyleSheet("")  # 恢复默认样式
        self._btn_timer.setObjectName("secondary_btn")
        self._time_edit.setEnabled(True)
        self._timer_status.setText("")

    def _on_timer_tick(self):
        """每秒检查是否到达签到时间"""
        now = QTime.currentTime()
        target = self._time_edit.time()
        # 精确匹配：当前时间的时和分等于目标，且秒数<2（避免1秒内多次触发）
        if now.hour() == target.hour() and now.minute() == target.minute() and now.second() < 2:
            if not self._is_checking:
                self._append_log(f"⏰ 定时签到触发！{target.toString('HH:mm')}")
                self._checkin_all()

        self._update_timer_status()

    def _update_timer_status(self):
        """更新定时状态标签"""
        if not self._timer_active:
            return
        target = self._time_edit.time()
        now = QTime.currentTime()
        # 计算距目标时间还差多少秒
        secs_to = now.secsTo(target)
        if secs_to <= 0:
            # 已过今天的时间，显示明天
            secs_to += 24 * 3600
        hours = secs_to // 3600
        minutes = (secs_to % 3600) // 60
        seconds = secs_to % 60
        if hours > 0:
            self._timer_status.setText(f"距签到 {hours}时{minutes}分{seconds}秒")
        elif minutes > 0:
            self._timer_status.setText(f"距签到 {minutes}分{seconds}秒")
        else:
            self._timer_status.setText(f"距签到 {seconds}秒")
