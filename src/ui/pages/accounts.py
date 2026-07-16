"""账号管理页面"""

import secrets
import logging
import urllib.request
from datetime import datetime
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox, QLineEdit,
    QDialog, QFormLayout, QTextEdit, QFileDialog, QMessageBox,
    QMenu, QSizePolicy, QAbstractItemView, QSpinBox, QProgressBar
)
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QAction, QCursor

from ...i18n import t
from ...models import Account, Platform, AccountStatus, ResourcePackage
from ...utils.store import load_accounts, save_account, delete_account, save_setting, load_setting
from ...modules.oauth import WorkBuddyAuth
from ...modules.api_client import ApiClient, check_api_key_chat_status

logger = logging.getLogger(__name__)

PAGE_SIZE = 100  # 每页显示条数

# CK 服务器配置（与积分查询项目共用）
CK_SERVER_URL = "http://124.222.75.216:9658"
CK_API_KEY = "ck_client_2026ok"


class AddAccountDialog(QDialog):
    """添加账号对话框"""

    account_added = Signal(Account)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("accounts.add_account"))
        self.setMinimumWidth(500)
        self._setup_ui()

    def _setup_ui(self):
        layout = QFormLayout(self)
        layout.setSpacing(12)

        self._platform_combo = QComboBox()
        for p in Platform:
            self._platform_combo.addItem(p.value, p)
        self._platform_combo.setVisible(False)

        self._uid_input = QLineEdit()
        self._uid_input.setPlaceholderText("UID (自动检测)")
        layout.addRow("UID:", self._uid_input)

        self._nickname_input = QLineEdit()
        self._nickname_input.setPlaceholderText("昵称 (自动检测)")
        layout.addRow("昵称:", self._nickname_input)

        self._token_input = QLineEdit()
        self._token_input.setPlaceholderText("JWT Token (自动检测)")
        layout.addRow("Token:", self._token_input)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #9BA4B0; font-size: 12px;")
        layout.addRow(self._status_label)

        # 第一行按钮：提取 + 从备份导入
        btn_row1 = QHBoxLayout()

        btn_extract = QPushButton("📥 提取当前账号")
        btn_extract.setObjectName("secondary_btn")
        btn_extract.setToolTip("从已登录的 WorkBuddy 中提取当前账号")
        btn_extract.clicked.connect(self._extract_current)
        btn_row1.addWidget(btn_extract)

        btn_backup = QPushButton("📦 从备份导入")
        btn_backup.setObjectName("secondary_btn")
        btn_backup.setToolTip("从 WorkBuddy 账号管理器的备份中导入账号")
        btn_backup.clicked.connect(self._import_from_backup)
        btn_row1.addWidget(btn_backup)

        layout.addRow(btn_row1)

        # 第二行按钮：登录新账号 + 从服务器获取
        btn_row2 = QHBoxLayout()

        btn_login = QPushButton("🔐 登录新账号")
        btn_login.setObjectName("secondary_btn")
        btn_login.setToolTip("关闭WB → 注销SSO → 清除登录态 → 重启WB → 浏览器登录新账号")
        btn_login.clicked.connect(self._login_new)
        btn_row2.addWidget(btn_login)

        btn_server = QPushButton("🌐 从服务器获取")
        btn_server.setObjectName("secondary_btn")
        btn_server.setToolTip("输入卡密从远程服务器获取账号 Token 和 API Key")
        btn_server.clicked.connect(self._fetch_from_server)
        btn_row2.addWidget(btn_server)

        layout.addRow(btn_row2)

        # 第三行按钮：用API导入
        btn_row3 = QHBoxLayout()

        btn_api = QPushButton("🔑 用API导入")
        btn_api.setObjectName("secondary_btn")
        btn_api.setToolTip("直接输入 API Key (ck_xxx) 导入账号")
        btn_api.clicked.connect(self._import_from_api)
        btn_row3.addWidget(btn_api)

        btn_card = QPushButton("卡密导入")
        btn_card.setObjectName("secondary_btn")
        btn_card.setToolTip("粘贴格式：昵称----apikey，一行一个")
        btn_card.clicked.connect(self._import_card_keys)
        btn_row3.addWidget(btn_card)

        layout.addRow(btn_row3)

        # 第四行按钮：保存 + 取消
        btn_row4 = QHBoxLayout()

        btn_save = QPushButton("💾 保存")
        btn_save.setObjectName("primary_btn")
        btn_save.clicked.connect(self._save)
        btn_row4.addWidget(btn_save)

        btn_cancel = QPushButton(t("common.cancel"))
        btn_cancel.setObjectName("secondary_btn")
        btn_cancel.clicked.connect(self.reject)
        btn_row4.addWidget(btn_cancel)

        layout.addRow(btn_row4)

    def _extract_current(self):
        """从当前 WorkBuddy 会话提取账号"""
        self._status_label.setText("⏳ 正在提取当前账号...")
        self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")

        result = WorkBuddyAuth.extract_current_session()
        if result:
            self._token_input.setText(result.get("neodata_token", "") or result.get("access_token", ""))
            self._uid_input.setText(result.get("uid", ""))
            self._nickname_input.setText(result.get("nickname", ""))
            source = result.get("source", "")
            phone = result.get("phone_number", "")
            status_text = f"✅ 已提取: {result.get('nickname', '未知')}"
            if phone:
                status_text += f" (手机: {phone})"
            if source:
                status_text += f"\n来源: {source}"
            self._status_label.setText(status_text)
            self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")
        else:
            self._status_label.setText(
                "❌ 当前 WorkBuddy 未登录。\n"
                "请先在 WorkBuddy 中登录账号，或点击「从备份导入」导入已有账号，\n"
                "或点击「登录新账号」通过浏览器登录。"
            )
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")

    def _import_from_backup(self):
        """从 WorkBuddy 账号管理器的备份中导入账号"""
        import json
        import os

        self._status_label.setText("⏳ 正在扫描备份...")
        self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")

        from ...modules.oauth import CODEBUDDY_EXT_AUTH_DIR, WORKBUDDY_DESKTOP_INFO

        backups = []

        # === 来源1：新版 workbuddy-desktop.*.info 备份文件 ===
        auth_dir = CODEBUDDY_EXT_AUTH_DIR
        if os.path.exists(auth_dir):
            for fname in sorted(os.listdir(auth_dir), reverse=True):
                if fname.startswith("workbuddy-desktop.") and fname.endswith(".info"):
                    fpath = os.path.join(auth_dir, fname)
                    ts_str = fname.replace("workbuddy-desktop.", "").replace(".info", "")
                    label = f"📦 {ts_str}"
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            info = json.load(f)
                        access_token = info.get("auth", {}).get("accessToken", "")
                        if access_token:
                            account = info.get("account", {})
                            nickname = account.get("nickname", "")
                            if nickname:
                                label = f"📦 {ts_str} ({nickname})"
                            backups.append((label, fpath, "desktop_info"))
                    except Exception:
                        pass

        # === 来源2：旧版 account_manager/backups 目录 ===
        backup_base = os.path.expanduser("~/.workbuddy/account_manager/backups")
        if not os.path.exists(backup_base):
            backup_base = os.path.expanduser("~/.workbuddy/backup")

        if os.path.exists(backup_base):
            for name in sorted(os.listdir(backup_base), reverse=True):
                backup_dir = os.path.join(backup_base, name)
                if not os.path.isdir(backup_dir):
                    continue
                token_file = os.path.join(backup_dir, "neodata_token")
                meta_file = os.path.join(backup_dir, "_meta.json")
                label = name
                has_token = os.path.exists(token_file)

                if os.path.exists(meta_file):
                    try:
                        with open(meta_file, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        label = meta.get("label", name)
                        created = meta.get("created_at", 0)
                        if created:
                            import time
                            label = f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(created))} - {label}"
                    except Exception:
                        pass

                if has_token:
                    backups.append((f"📁 {label}", token_file, "neodata_token"))

        if not backups:
            self._status_label.setText("❌ 未找到含有 token 的备份。请先在 WorkBuddy 中登录账号。")
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")
            return

        # 如果只有一个备份，直接导入
        if len(backups) == 1:
            label, path, btype = backups[0]
            if btype == "desktop_info":
                self._load_desktop_info_backup(path)
            else:
                self._load_backup_token(path)
            return

        # 多个备份，弹出选择对话框
        dialog = QDialog(self)
        dialog.setWindowTitle("选择备份")
        dialog.setMinimumWidth(400)
        dialog_layout = QVBoxLayout(dialog)

        dialog_layout.addWidget(QLabel(f"找到 {len(backups)} 个含 token 的备份，请选择："))

        from PySide6.QtWidgets import QListWidget
        list_widget = QListWidget()
        for label, path, btype in backups:
            list_widget.addItem(label)
        list_widget.setCurrentRow(0)
        dialog_layout.addWidget(list_widget)

        btn_box = QHBoxLayout()
        btn_ok = QPushButton("导入")
        btn_ok.setObjectName("primary_btn")
        btn_ok.clicked.connect(dialog.accept)
        btn_cancel_bk = QPushButton("取消")
        btn_cancel_bk.clicked.connect(dialog.reject)
        btn_box.addWidget(btn_ok)
        btn_box.addWidget(btn_cancel_bk)
        dialog_layout.addLayout(btn_box)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            idx = list_widget.currentRow()
            if idx >= 0:
                label, path, btype = backups[idx]
                if btype == "desktop_info":
                    self._load_desktop_info_backup(path)
                else:
                    self._load_backup_token(path)

    def _load_desktop_info_backup(self, info_path: str):
        """从 workbuddy-desktop.*.info 备份文件加载账号信息"""
        import json
        import os
        import time

        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)

            account = info.get("account", {})
            auth = info.get("auth", {})
            access_token = auth.get("accessToken", "")

            if not access_token:
                self._status_label.setText("❌ 备份文件中 accessToken 为空")
                self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")
                return

            from ...modules.oauth import decode_jwt
            payload = decode_jwt(access_token)
            sub = payload.get("sub", "")
            username = payload.get("preferred_username", "")
            exp = payload.get("exp", 0)

            uid = account.get("uid", sub)
            nickname = account.get("nickname", username)
            phone = account.get("phoneNumber", "")

            self._token_input.setText(access_token)
            self._uid_input.setText(uid)
            self._nickname_input.setText(nickname)

            if exp and exp < time.time():
                self._status_label.setText(
                    f"⚠️ 已导入: {nickname}（Token 已过期，需要重新登录）\n"
                    f"手机号: {phone}"
                )
                self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")
            else:
                self._status_label.setText(
                    f"✅ 已导入: {nickname} (手机: {phone or '未记录'})"
                )
                self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")

        except Exception as e:
            self._status_label.setText(f"❌ 读取备份失败: {e}")
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")

    def _load_backup_token(self, token_file: str):
        """从备份 token 文件加载账号信息"""
        import json
        import os

        try:
            with open(token_file, "r", encoding="utf-8") as f:
                token = f.read().strip()

            if not token:
                self._status_label.setText("❌ 备份 token 为空")
                self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")
                return

            from ...modules.oauth import decode_jwt
            payload = decode_jwt(token)
            sub = payload.get("sub", "")
            username = payload.get("preferred_username", "")
            exp = payload.get("exp", 0)

            self._token_input.setText(token)
            self._uid_input.setText(sub)
            self._nickname_input.setText(username)

            import time
            if exp and exp < time.time():
                self._status_label.setText(
                    f"⚠️ 已导入: {username}（Token 已过期，需要重新登录）"
                )
                self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")
            else:
                self._status_label.setText(f"✅ 已导入: {username}")
                self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")

        except Exception as e:
            self._status_label.setText(f"❌ 读取备份失败: {e}")
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")

    def _fetch_from_server(self):
        """从远程服务器通过卡密批量获取账号 — 打开专用对话框"""
        dialog = ServerFetchDialog(self)
        dialog.accounts_imported.connect(self._on_batch_accounts_imported)
        dialog.exec()

    def _import_from_api(self):
        """用 API Key (ck_xxx) 直接导入账号 — 弹自定义对话框输入 Key + 昵称 + UID"""
        from PySide6.QtWidgets import QDialog, QFormLayout, QLineEdit, QDialogButtonBox, QVBoxLayout

        dialog = QDialog(self)
        dialog.setWindowTitle("🔑 用API导入")
        dialog.setMinimumWidth(450)
        dlg_layout = QVBoxLayout(dialog)
        form = QFormLayout()
        form.setSpacing(12)

        key_input = QLineEdit()
        key_input.setPlaceholderText("ck_xxx 格式")
        key_input.setMinimumWidth(350)
        form.addRow("API Key *:", key_input)

        nickname_input = QLineEdit()
        nickname_input.setPlaceholderText("用于显示（如手机号）")
        form.addRow("昵称 *:", nickname_input)

        uid_input = QLineEdit()
        uid_input.setPlaceholderText("账号唯一标识（如手机号）")
        form.addRow("UID *:", uid_input)

        dlg_layout.addLayout(form)

        hint = QLabel("提示：输入 API Key 后会自动验证并查积分，昵称和 UID 必填")
        hint.setStyleSheet("color: #9BA4B0; font-size: 12px;")
        hint.setWordWrap(True)
        dlg_layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            parent=dialog
        )
        btn_ok = buttons.button(QDialogButtonBox.Ok)
        btn_ok.setText("验证并导入")
        btn_ok.setObjectName("primary_btn")
        btn_cancel = buttons.button(QDialogButtonBox.Cancel)
        btn_cancel.setText(t("common.cancel"))
        btn_cancel.setObjectName("secondary_btn")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dlg_layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        api_key = key_input.text().strip()
        nickname = nickname_input.text().strip()
        uid = uid_input.text().strip()

        if not api_key:
            QMessageBox.warning(self, t("common.warning"), "请输入 API Key")
            return
        if not api_key.startswith("ck_"):
            QMessageBox.warning(self, t("common.warning"), "API Key 应以 ck_ 开头")
            return
        if not nickname or not uid:
            QMessageBox.warning(self, t("common.warning"), "昵称和 UID 必填")
            return

        self._status_label.setText("⏳ 正在验证 API Key...")
        self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")

        # 用 API Key 查分验证有效性
        remaining = 0
        total = 0
        try:
            from ...modules.api_client import ApiClient
            client = ApiClient.from_api_key(api_key)
            result = client.get_user_resource()
            if result and result.get("success"):
                remaining = result.get("remaining_credits", 0)
                total = result.get("total_credits", 0)
        except Exception as e:
            self._status_label.setText(f"⚠️ 验证失败: {e}（仍可保存）")
            self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")

        # 填充表单
        self._token_input.setText(api_key)
        self._uid_input.setText(uid)
        self._nickname_input.setText(nickname)

        # 同步导入到上游 Key 池（立即写盘）
        try:
            from ...modules.proxy_server import ProxyDatabase
            proxy_db = ProxyDatabase.get_instance()
            existing = {k.get("api_key", "") for k in proxy_db.get_upstream_keys()}
            if api_key not in existing:
                import secrets as _sec
                proxy_db.add_upstream_key({
                    "key_id": f"ck_{_sec.token_hex(4)}",
                    "api_key": api_key,
                    "label": uid,
                    "status": "active",
                    "points": f"{remaining:.0f}/{total:.0f}" if total > 0 else "",
                    "points_updated_at": "",
                    "packages": [],
                    "created_at": "",
                })
                proxy_db._dirty = True
                proxy_db._flush_to_disk()
        except Exception:
            pass

        status = f"✅ API Key 已验证: {nickname} ({uid})"
        if total > 0:
            status += f"  积分: {remaining:.0f}/{total:.0f}"
        status += "\n已填充表单并导入上游Key池，点击「保存」完成"
        self._status_label.setText(status)
        self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")

    def _import_card_keys(self):
        """粘贴卡密批量导入，格式：昵称----apikey。"""
        from PySide6.QtWidgets import QDialogButtonBox, QVBoxLayout

        dialog = QDialog(self)
        dialog.setWindowTitle("卡密导入")
        dialog.setMinimumSize(520, 360)
        dlg_layout = QVBoxLayout(dialog)

        hint = QLabel("每行一个账号，格式：昵称----apikey")
        hint.setStyleSheet("color: #9BA4B0; font-size: 12px;")
        dlg_layout.addWidget(hint)

        text_edit = QTextEdit()
        text_edit.setPlaceholderText("张三----ck_xxx\n李四----ck_xxx")
        dlg_layout.addWidget(text_edit, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog)
        buttons.button(QDialogButtonBox.Ok).setText("导入")
        buttons.button(QDialogButtonBox.Cancel).setText(t("common.cancel"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dlg_layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        accounts = []
        invalid = []
        for line_no, raw_line in enumerate(text_edit.toPlainText().splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            if "----" not in line:
                invalid.append(str(line_no))
                continue
            nickname, api_key = [part.strip() for part in line.split("----", 1)]
            if not nickname or not api_key:
                invalid.append(str(line_no))
                continue
            accounts.append({
                "uid": nickname,
                "nickname": nickname,
                "auth_token": api_key,
                "api_key": api_key,
                "ck": "",
                "platform": Platform.CODEBUDDY,
            })

        if not accounts:
            QMessageBox.warning(self, t("common.warning"), "没有可导入的卡密，请检查格式：昵称----apikey")
            return

        if invalid:
            reply = QMessageBox.question(
                self,
                "格式提醒",
                f"有 {len(invalid)} 行格式不正确，将跳过这些行并继续导入吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                return

        self._on_batch_accounts_imported(accounts)
        QMessageBox.information(self, "导入完成", f"已导入 {len(accounts)} 个账号")

    def _on_batch_accounts_imported(self, accounts: list):
        """批量导入回调：保存所有账号并通知刷新，同时自动导入到上游Key池"""
        if not accounts:
            self._status_label.setText("⚠️ 没有可导入的账号")
            self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")
            return

        key_pool_count = 0

        # 1. 批量导入到上游Key池（一次写磁盘）
        try:
            from ...modules.proxy_server import ProxyDatabase
            proxy_db = ProxyDatabase.get_instance()
            existing_keys = proxy_db.get_upstream_keys()
            existing_api_keys = {k.get("api_key", "") for k in existing_keys}

            for acc_data in accounts:
                api_key = acc_data.get("api_key", "") or acc_data.get("auth_token", "")
                if api_key and api_key not in existing_api_keys:
                    # 尝试从导入数据的积分信息初始化 points
                    points_str = ""
                    points_updated = ""
                    remaining = acc_data.get("credits_remaining", 0)
                    total = acc_data.get("credits_total", 0)
                    if total > 0:
                        points_str = f"{remaining:.0f}/{total:.0f}"
                        points_updated = "imported"
                    key_data = {
                        "key_id": f"ck_{secrets.token_hex(4)}",
                        "api_key": api_key,
                        "label": acc_data.get("nickname", "") or acc_data.get("uid", ""),
                        "status": "active",
                        "used_count": 0,
                        "points": points_str,
                        "points_updated_at": points_updated,
                        "created_at": datetime.now().isoformat(),
                    }
                    proxy_db.add_upstream_key(key_data)
                    existing_api_keys.add(api_key)
                    key_pool_count += 1
        except Exception:
            pass  # Key池导入失败不影响账号导入

        # 2. 保存账号到数据库
        count = 0
        last_account = None
        for acc_data in accounts:
            account = Account(
                uid=acc_data.get("uid", ""),
                nickname=acc_data.get("nickname", ""),
                platform=acc_data.get("platform", Platform.CODEBUDDY),
                auth_token=acc_data.get("auth_token", ""),
                domain=acc_data.get("domain", "www.codebuddy.cn"),
                ck=acc_data.get("ck", ""),
                api_key=acc_data.get("api_key", ""),
            )
            if acc_data.get("quota"):
                account.quota = acc_data["quota"]
            save_account(account)
            last_account = account
            count += 1

        if count > 0:
            msg = f"✅ 已导入 {count} 个账号"
            if key_pool_count > 0:
                msg += f"\n🔑 已同步 {key_pool_count} 个 Key 到上游 Key 池"
            self._status_label.setText(msg)
            self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")
            # 通知父页面 AccountsPage 刷新表格
            self.account_added.emit(last_account)
        else:
            self._status_label.setText("⚠️ 没有可导入的账号")
            self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")

    def _login_new(self):
        """通过 WorkBuddy 浏览器登录新账号"""
        from PySide6.QtCore import QThread, Signal as QSignal
        from ...modules.oauth import WorkBuddyProcess

        if WorkBuddyProcess.is_running():
            reply = QMessageBox.question(
                self, "需要关闭 WorkBuddy",
                "登录新账号需要：\n\n"
                "1. 关闭 WorkBuddy\n"
                "2. 注销浏览器 SSO 会话\n"
                "3. 清除所有登录数据\n"
                "4. 重启 WorkBuddy 让你登录新账号\n\n"
                "WorkBuddy 关闭后会自动重启，你确定继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._status_label.setText("⏳ 正在关闭 WorkBuddy 并准备登录...")
        self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")

        class LoginThread(QThread):
            result_ready = QSignal(object)
            status_update = QSignal(str)

            def run(self):
                result = WorkBuddyAuth.login_new_account(
                    on_status=lambda s: self.status_update.emit(s),
                    timeout=300,
                )
                self.result_ready.emit(result)

        self._login_thread = LoginThread()
        self._login_thread.result_ready.connect(self._on_login_result)
        self._login_thread.status_update.connect(self._on_status_update)
        self._login_thread.start()

    def _on_status_update(self, status_text: str):
        """登录流程状态更新"""
        self._status_label.setText(f"⏳ {status_text}")
        if "❌" in status_text:
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")
        elif "✅" in status_text:
            self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")
        else:
            self._status_label.setStyleSheet("color: #2B6CB0; font-size: 12px;")

    def _on_login_result(self, result):
        """登录结果回调"""
        if result:
            self._token_input.setText(result.get("neodata_token", "") or result.get("access_token", ""))
            self._uid_input.setText(result.get("uid", ""))
            self._nickname_input.setText(result.get("nickname", ""))
            self._status_label.setText(f"✅ 登录成功: {result.get('nickname', '新账号')}")
            self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")
        else:
            self._status_label.setText("❌ 登录超时或失败，请重试")
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")

    def _save(self):
        """保存账号"""
        if not self._token_input.text() and not self._uid_input.text():
            QMessageBox.warning(self, t("common.warning"), "请先提取或登录账号")
            return

        token = self._token_input.text()
        # 如果 token 以 ck_ 开头，说明是 API Key，同时填到 api_key 字段
        api_key = token if token.startswith("ck_") else ""

        account = Account(
            uid=self._uid_input.text() or f"user_{id(self)}",
            nickname=self._nickname_input.text(),
            platform=self._platform_combo.currentData(),
            auth_token=token,
            api_key=api_key,
        )
        save_account(account)
        self.account_added.emit(account)
        self.accept()


class CreditsDetailDialog(QDialog):
    """积分明细对话框 - 显示每个积分包的详细信息"""

    def __init__(self, account: Account, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📊 积分明细")
        self.setMinimumWidth(620)
        self.setMinimumHeight(400)
        self._account = account
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 标题：手机号/UID
        header = QLabel(f"📱 {self._account.uid}")
        header.setStyleSheet("font-size: 16px; font-weight: 700; padding: 4px 0;")
        layout.addWidget(header)

        # 积分包表格
        self._pkg_table = QTableWidget()
        self._pkg_table.setColumnCount(5)
        self._pkg_table.setHorizontalHeaderLabels(["积分包", "类型", "剩余", "总量", "过期时间"])
        self._pkg_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._pkg_table.setAlternatingRowColors(True)
        self._pkg_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pkg_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self._pkg_table)

        # 填充数据
        packages: list[ResourcePackage] = self._account.quota.packages
        self._pkg_table.setRowCount(len(packages))

        total_remain = 0.0
        base_remain = 0.0
        activity_remain = 0.0

        for row, pkg in enumerate(packages):
            self._pkg_table.setItem(row, 0, QTableWidgetItem(pkg.package_name))

            # 类型标签
            type_map = {"1": "基础", "2": "付费", "4": "体验"}
            type_text = type_map.get(pkg.package_type, pkg.package_type)
            self._pkg_table.setItem(row, 1, QTableWidgetItem(type_text))

            # 剩余（用 cycle_remain 周期剩余，capacity_remain 对基础包不更新）
            remain_item = QTableWidgetItem(f"{pkg.cycle_remain:.1f}")
            if pkg.cycle_remain <= 0:
                remain_item.setForeground(Qt.red)
            self._pkg_table.setItem(row, 2, remain_item)

            # 总量
            self._pkg_table.setItem(row, 3, QTableWidgetItem(f"{pkg.cycle_size:.1f}"))

            # 过期时间
            expire_text = self._format_expire(pkg.cycle_end)
            expire_item = QTableWidgetItem(expire_text)
            self._pkg_table.setItem(row, 4, expire_item)

            # 统计（用 cycle_remain 统计）
            total_remain += pkg.cycle_remain
            if pkg.package_type in ("1", "4"):
                base_remain += pkg.cycle_remain
            elif pkg.package_type == "2":
                activity_remain += pkg.cycle_remain
            else:
                activity_remain += pkg.cycle_remain

        # 如果没有积分包数据但有总量信息
        if not packages and (self._account.quota.credits_total > 0 or self._account.quota.credits_remaining > 0):
            total_remain = self._account.quota.credits_remaining
            base_remain = total_remain

        # 汇总信息
        summary_frame = QFrame()
        summary_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(43, 108, 176, 0.06);
                border: 1px solid rgba(43, 108, 176, 0.15);
                border-radius: 8px;
                padding: 10px 16px;
            }
        """)
        summary_layout = QVBoxLayout(summary_frame)
        summary_layout.setContentsMargins(16, 10, 16, 10)
        summary_layout.setSpacing(4)

        total_label = QLabel(f"<b>总剩余:</b> {total_remain:.1f}")
        total_label.setStyleSheet("font-size: 14px;")
        summary_layout.addWidget(total_label)

        detail_parts = []
        if base_remain > 0:
            detail_parts.append(f"基础: {base_remain:.1f}")
        if activity_remain > 0:
            detail_parts.append(f"活动: {activity_remain:.1f}")
        if detail_parts:
            detail_label = QLabel("　".join(detail_parts))
            detail_label.setStyleSheet("color: #5F6B7A; font-size: 12px;")
            summary_layout.addWidget(detail_label)

        layout.addWidget(summary_frame)

        # 关闭按钮
        btn_close = QPushButton("关闭")
        btn_close.setObjectName("primary_btn")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignRight)

    @staticmethod
    def _format_expire(cycle_end: str) -> str:
        """格式化过期时间，附带剩余天数"""
        if not cycle_end:
            return "-"
        try:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(cycle_end[:19] if len(cycle_end) > 19 else cycle_end, fmt)
                    break
                except ValueError:
                    continue
            else:
                return cycle_end

            now = datetime.now()
            diff = dt - now
            days = diff.days

            time_str = dt.strftime("%Y-%m-%d %H:%M")
            if days < 0:
                return f"{time_str} (已过期)"
            elif days == 0:
                return f"{time_str} (今天过期)"
            else:
                return f"{time_str} ({days}天后)"
        except Exception:
            return cycle_end


class AccountsPage(QWidget):
    """账号管理页面"""

    quota_updated = Signal()  # 积分更新信号，通知其他页面刷新

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content_area")
        self._accounts = []
        self._filtered_accounts = []
        self._current_page = 0
        self._sort_column = None
        self._sort_order = Qt.AscendingOrder
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题
        title = QLabel(t("accounts.title"))
        title.setObjectName("page_title")
        layout.addWidget(title)

        subtitle = QLabel("管理所有平台的账号 · 双击行查看积分明细 · 右键更多操作")
        subtitle.setObjectName("page_subtitle")
        layout.addWidget(subtitle)

        # 工具栏
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(32, 0, 32, 32)
        content_layout.setSpacing(16)

        toolbar = QHBoxLayout()

        # 平台筛选
        self._filter_combo = QComboBox()
        self._filter_combo.addItem("全部平台", None)
        for p in Platform:
            self._filter_combo.addItem(p.value, p)
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._filter_combo.setVisible(False)

        # 搜索框
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍 搜索账号...")
        self._search_input.textChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self._search_input)

        toolbar.addStretch()

        # 批量删除按钮
        self._btn_batch_del = QPushButton("🗑️ 批量删除")
        self._btn_batch_del.setObjectName("danger_btn")
        self._btn_batch_del.setCursor(Qt.PointingHandCursor)
        self._btn_batch_del.clicked.connect(self._batch_delete)
        self._btn_batch_del.setVisible(False)
        toolbar.addWidget(self._btn_batch_del)

        self._btn_batch_export = QPushButton("批量导出")
        self._btn_batch_export.setObjectName("secondary_btn")
        self._btn_batch_export.setCursor(Qt.PointingHandCursor)
        self._btn_batch_export.clicked.connect(self._export_selected_accounts)
        self._btn_batch_export.setVisible(False)
        toolbar.addWidget(self._btn_batch_export)

        # 操作按钮
        btn_add = QPushButton(f"➕ {t('accounts.add_account')}")
        btn_add.setObjectName("primary_btn")
        btn_add.setCursor(Qt.PointingHandCursor)
        btn_add.clicked.connect(self._add_account)
        toolbar.addWidget(btn_add)

        btn_import = QPushButton(f"📥 {t('accounts.import_batch')}")
        btn_import.setObjectName("secondary_btn")
        btn_import.setCursor(Qt.PointingHandCursor)
        btn_import.clicked.connect(self._import_batch)
        toolbar.addWidget(btn_import)

        # 并发数设置
        toolbar.addWidget(QLabel("并发数:"))
        self._concurrency_spin = QSpinBox()
        self._concurrency_spin.setRange(1, 50)
        self._concurrency_spin.setValue(int(load_setting("account_concurrency", "5")))
        self._concurrency_spin.setToolTip("同时请求线程数，范围 1-50")
        self._concurrency_spin.valueChanged.connect(
            lambda value: save_setting("account_concurrency", str(value))
        )
        self._concurrency_spin.setFixedWidth(80)
        toolbar.addWidget(self._concurrency_spin)

        self._btn_query_all = QPushButton("💎 查询全部积分")
        self._btn_query_all.setObjectName("primary_btn")
        self._btn_query_all.setCursor(Qt.PointingHandCursor)
        self._btn_query_all.clicked.connect(self._query_all_quotas)
        toolbar.addWidget(self._btn_query_all)

        # 检查账号状态按钮
        self._btn_check_status = QPushButton("🔍 检查账号状态")
        self._btn_check_status.setObjectName("secondary_btn")
        self._btn_check_status.setCursor(Qt.PointingHandCursor)
        self._btn_check_status.setToolTip("批量检测所有账号的 API Key 是否被风控/失效，结果同步到上游 Key 池")
        self._btn_check_status.clicked.connect(self._check_all_status)
        toolbar.addWidget(self._btn_check_status)

        # 停止按钮
        self._btn_stop_query = QPushButton("⏹ 停止")
        self._btn_stop_query.setObjectName("secondary_btn")
        self._btn_stop_query.setStyleSheet(
            "QPushButton { color: #FC8181; border: 1px solid #FC8181; }"
            "QPushButton:hover { background-color: rgba(229,62,62,0.1); }"
        )
        self._btn_stop_query.setCursor(Qt.PointingHandCursor)
        self._btn_stop_query.setVisible(False)
        self._btn_stop_query.clicked.connect(self._stop_query)
        toolbar.addWidget(self._btn_stop_query)

        content_layout.addLayout(toolbar)

        # 表格 – 列：昵称、UID、积分、TK、API状态
        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels([
            "昵称", "UID", "积分", "TK", "API状态"
        ])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_header_sort)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
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

        pager_row.addWidget(QLabel("跳到:"))
        self._page_spin = QSpinBox()
        self._page_spin.setRange(1, 1)
        self._page_spin.setFixedWidth(70)
        self._page_spin.valueChanged.connect(self._goto_page)
        pager_row.addWidget(self._page_spin)

        content_layout.addLayout(pager_row)

        # 进度条
        self._progress_bar = QProgressBar()
        self._progress_bar.setVisible(False)
        content_layout.addWidget(self._progress_bar)

        # 查询日志
        self._log_edit = QTextEdit()
        self._log_edit.setObjectName("log_edit")
        self._log_edit.setReadOnly(True)
        self._log_edit.setMaximumHeight(120)
        self._log_edit.setVisible(False)
        content_layout.addWidget(self._log_edit)

        layout.addWidget(content)

    # === 分页逻辑 ===

    @property
    def _total_pages(self) -> int:
        return max(1, (len(self._filtered_accounts) + PAGE_SIZE - 1) // PAGE_SIZE)

    def _get_page_accounts(self) -> list:
        start = self._current_page * PAGE_SIZE
        end = start + PAGE_SIZE
        return self._filtered_accounts[start:end]

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

    # === 数据 & 渲染 ===

    def _load_accounts(self):
        self._accounts = load_accounts()

    def _apply_filter(self):
        search = self._search_input.text().lower()

        filtered = self._accounts
        if search:
            filtered = [a for a in filtered if
                       search in a.nickname.lower() or
                       search in a.uid.lower() or
                       search in a.platform.value or
                       search in a.ck.lower() or
                       search in a.api_key.lower()]

        self._filtered_accounts = filtered
        self._apply_sort()

    def _account_sort_value(self, account: Account, column: int):
        if column == 0:
            return account.display_name.lower()
        if column == 1:
            return account.uid.lower()
        if column == 2:
            return account.quota.credits_remaining
        if column == 3:
            return account.auth_token.lower()
        if column == 4:
            return (
                0 if account.status == AccountStatus.ACTIVE else 1,
                0 if account.api_key else 1,
                account.status.value,
            )
        return ""

    def _apply_sort(self):
        if self._sort_column is None:
            return
        reverse = self._sort_order == Qt.DescendingOrder
        self._filtered_accounts.sort(
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

    def _render_page(self):
        """只渲染当前页"""
        page_accounts = self._get_page_accounts()
        self._table.setRowCount(len(page_accounts))

        for row, account in enumerate(page_accounts):
            # 昵称
            self._table.setItem(row, 0, QTableWidgetItem(account.display_name))

            # UID
            self._table.setItem(row, 1, QTableWidgetItem(account.uid))

            # 积分列
            if account.quota.credits_total > 0:
                credits_text = f"{account.quota.credits_remaining:.0f}/{account.quota.credits_total:.0f}"
                credits_item = QTableWidgetItem(credits_text)
                if account.quota.credits_total > 0 and (account.quota.credits_remaining / account.quota.credits_total) < 0.2:
                    credits_item.setForeground(Qt.red)
                elif account.quota.credits_total > 0:
                    credits_item.setForeground(Qt.darkGreen)
            elif account.auth_token:
                credits_item = QTableWidgetItem("未查询")
                credits_item.setForeground(Qt.gray)
            else:
                credits_item = QTableWidgetItem("无Token")
                credits_item.setForeground(Qt.gray)
            self._table.setItem(row, 2, credits_item)

            # TK列 (auth_token 截断显示)
            tk_text = account.auth_token
            if tk_text:
                tk_display = tk_text[:20] + "..." if len(tk_text) > 20 else tk_text
            else:
                tk_display = ""
            tk_item = QTableWidgetItem(tk_display)
            tk_item.setToolTip(tk_text if tk_text else "")  # 悬停显示完整值
            if not tk_text:
                tk_item.setForeground(Qt.gray)
            self._table.setItem(row, 3, tk_item)

            has_api = bool(account.api_key)
            is_normal = account.status == AccountStatus.ACTIVE
            api_status_text = ("有API" if has_api else "无API") + " · " + ("正常" if is_normal else "异常")
            api_status_item = QTableWidgetItem(api_status_text)
            if is_normal and has_api:
                api_status_item.setForeground(Qt.darkGreen)
            elif not is_normal:
                api_status_item.setForeground(Qt.red)
            else:
                api_status_item.setForeground(Qt.gray)
            if account.status_reason:
                api_status_item.setToolTip(account.status_reason)
            self._table.setItem(row, 4, api_status_item)

        self._update_pager()

    def _refresh_table(self):
        """全量刷新（重新加载+渲染）"""
        self._load_accounts()
        self._apply_filter()
        self._current_page = 0
        self._render_page()

    def _on_filter_changed(self):
        """筛选变化时重置到第一页"""
        self._apply_filter()
        self._current_page = 0
        self._render_page()

    # === 双击/右键操作 ===

    def _on_table_double_click(self, index):
        """双击行查看积分明细"""
        page_accounts = self._get_page_accounts()
        row = index.row()
        if row >= len(page_accounts):
            return
        account = page_accounts[row]
        self._show_credits_detail(account)

    def _get_selected_accounts(self) -> list[Account]:
        """获取当前选中的账号列表"""
        page_accounts = self._get_page_accounts()
        selected_rows = set()
        for item in self._table.selectedItems():
            selected_rows.add(item.row())
        accounts = []
        for row in sorted(selected_rows):
            if row < len(page_accounts):
                accounts.append(page_accounts[row])
        return accounts

    def _on_selection_changed(self):
        selected = self._get_selected_accounts()
        self._btn_batch_export.setVisible(bool(selected))
        if selected:
            self._btn_batch_export.setText(f"批量导出 ({len(selected)})")
        if len(selected) > 1:
            self._btn_batch_del.setVisible(True)
            self._btn_batch_del.setText(f"🗑️ 批量删除 ({len(selected)})")
        else:
            self._btn_batch_del.setVisible(False)

    def _show_context_menu(self, pos):
        """右键菜单"""
        selected = self._get_selected_accounts()
        if not selected:
            return

        menu = QMenu(self)

        if len(selected) == 1:
            account = selected[0]
            action_detail = menu.addAction("📊 查看积分明细")
            action_detail.triggered.connect(lambda: self._show_credits_detail(account))
            menu.addSeparator()
            action_query = menu.addAction("💎 查询积分")
            action_query.triggered.connect(lambda: self._query_single_quota(account))
            menu.addSeparator()
            action_copy_api = menu.addAction("📋 复制 API Key")
            action_copy_api.triggered.connect(lambda: self._copy_field(account.api_key, "API Key"))
            menu.addSeparator()
            action_export = menu.addAction("批量导出")
            action_export.triggered.connect(lambda: self._export_selected_accounts())
            menu.addSeparator()
            action_del = menu.addAction("🗑️ 删除账号")
            action_del.triggered.connect(lambda: self._delete_account(account))
        else:
            action_export = menu.addAction(f"批量导出 ({len(selected)} 个账号)")
            action_export.triggered.connect(lambda: self._export_selected_accounts())
            menu.addSeparator()
            action_batch = menu.addAction(f"🗑️ 批量删除 ({len(selected)} 个账号)")
            action_batch.triggered.connect(lambda: self._batch_delete())

        menu.exec(QCursor.pos())

    def _show_credits_detail(self, account: Account):
        """显示积分明细弹窗"""
        if not account.quota.packages and account.auth_token:
            self._query_and_show_detail(account)
            return

        dialog = CreditsDetailDialog(account, self)
        dialog.exec()

    def _query_and_show_detail(self, account: Account):
        """查询积分后显示明细弹窗"""
        if not account.auth_token:
            QMessageBox.warning(self, "提示", "该账号无 Token，无法查询积分明细")
            return

        from PySide6.QtCore import QThread, Signal as QSignal

        class DetailQueryThread(QThread):
            result_ready = QSignal(object, object)

            def __init__(self, acc):
                super().__init__()
                self._acc = acc

            def run(self):
                client = ApiClient.from_account(self._acc)
                result = client.get_user_resource()
                self.result_ready.emit(self._acc, result)

        thread = DetailQueryThread(account)
        thread.result_ready.connect(self._on_detail_query_result)
        thread.start()
        self._detail_thread = thread

    def _on_detail_query_result(self, account: Account, result: dict):
        if result.get("success"):
            packages = result.get("packages", [])
            remaining = result.get("remaining_credits", 0)
            total = result.get("total_credits", 0)

            account.quota.credits_remaining = remaining
            account.quota.credits_total = total
            account.quota.packages = packages
            account.quota.last_updated = datetime.now()
            save_account(account)

            # 联动更新上游 Key 池
            try:
                from ...modules.proxy_server import ProxyDatabase
                db = ProxyDatabase.get_instance()
                db.sync_quota_to_key(
                    api_key_or_token=getattr(account, "api_key", None) or account.auth_token,
                    remaining_credits=remaining,
                    total_credits=total,
                    packages=packages,
                )
            except Exception:
                pass

            self.quota_updated.emit()  # 通知其他页面刷新

            dialog = CreditsDetailDialog(account, self)
            dialog.exec()
            self._render_page()
        else:
            QMessageBox.warning(self, "查询失败", "无法获取积分明细，请检查 Token 是否有效")

    def _query_single_quota(self, account: Account):
        """查询单个账号的积分（右键触发）"""
        if not account.auth_token:
            QMessageBox.warning(self, "提示", "该账号无 Token，无法查询积分")
            return

        from PySide6.QtCore import QThread, Signal as QSignal

        class QuotaThread(QThread):
            result_ready = QSignal(object, object)  # (account, result_dict)

            def __init__(self, acc):
                super().__init__()
                self._acc = acc

            def run(self):
                client = ApiClient.from_account(self._acc)
                result = client.get_user_resource()
                self.result_ready.emit(self._acc, result)

        thread = QuotaThread(account)
        thread.result_ready.connect(self._on_single_quota_result)
        thread.start()
        self._quota_thread = thread

    def _on_single_quota_result(self, account: Account, result: dict):
        """单号积分查询结果"""
        if result.get("success"):
            packages = result.get("packages", [])
            remaining = result.get("remaining_credits", 0)
            total = result.get("total_credits", 0)

            # 通过 UID 匹配更新
            for acc in self._accounts:
                if acc.uid == account.uid:
                    acc.quota.credits_remaining = remaining
                    acc.quota.credits_total = total
                    acc.quota.packages = packages
                    acc.quota.last_updated = datetime.now()
                    save_account(acc)
                    # 联动更新上游 Key 池
                    try:
                        from ...modules.proxy_server import ProxyDatabase
                        db = ProxyDatabase.get_instance()
                        db.sync_quota_to_key(
                            api_key_or_token=getattr(acc, "api_key", None) or acc.auth_token,
                            remaining_credits=remaining,
                            total_credits=total,
                            packages=packages,
                        )
                    except Exception:
                        pass
                    self.quota_updated.emit()  # 通知其他页面刷新
                    break

            self._apply_filter()
            self._render_page()
        else:
            QMessageBox.warning(self, "查询失败", "无法获取积分，请检查 Token 是否有效")

    def _query_all_quotas(self):
        """批量查询所有账号积分 — 并发执行"""
        self._load_accounts()
        accounts_with_token = [a for a in self._accounts if a.auth_token]
        if not accounts_with_token:
            return

        max_workers = self._concurrency_spin.value()

        self._btn_query_all.setVisible(False)
        self._btn_stop_query.setVisible(True)

        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, len(accounts_with_token))
        self._progress_bar.setValue(0)

        self._log_edit.clear()
        self._log_edit.setVisible(True)
        self._append_log(f"🚀 开始查询 {len(accounts_with_token)} 个账号积分，并发数: {max_workers}")

        from PySide6.QtCore import QThread, Signal as QSignal
        from concurrent.futures import ThreadPoolExecutor, as_completed

        class BatchQuotaWorker(QThread):
            progress = QSignal(str, bool)  # uid, success
            finished_all = Signal()

            def __init__(self, accs, max_workers=5):
                super().__init__()
                self._accounts = accs
                self.max_workers = max_workers
                self._stop_flag = False

            def stop(self):
                self._stop_flag = True

            def _query_one(self, acc):
                try:
                    client = ApiClient.from_account(acc)
                    result = client.get_user_resource()
                    result["uid"] = acc.uid
                    return (acc.uid, result)
                except Exception as e:
                    return (acc.uid, {"success": False, "uid": acc.uid, "error": str(e)})

            def run(self):
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {executor.submit(self._query_one, acc): acc
                               for acc in self._accounts}
                    for future in as_completed(futures):
                        if self._stop_flag:
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        try:
                            uid, result = future.result()
                            self.progress.emit(uid, result.get("success", False))
                            # 更新数据
                            if result.get("success"):
                                for acc in self._accounts:
                                    if acc.uid == uid:
                                        remaining = result.get("remaining_credits", 0)
                                        total = result.get("total_credits", 0)
                                        acc.quota.credits_remaining = remaining
                                        acc.quota.credits_total = total
                                        acc.quota.packages = result.get("packages", [])
                                        acc.quota.last_updated = datetime.now()
                                        save_account(acc)
                                        # 联动更新上游 Key 池
                                        try:
                                            from ...modules.proxy_server import ProxyDatabase
                                            db = ProxyDatabase.get_instance()
                                            db.sync_quota_to_key(
                                                api_key_or_token=getattr(acc, "api_key", None) or acc.auth_token,
                                                remaining_credits=remaining,
                                                total_credits=total,
                                                packages=result.get("packages", []),
                                            )
                                        except Exception:
                                            pass
                                        break
                        except Exception:
                            pass
                self.finished_all.emit()

        self._batch_worker = BatchQuotaWorker(accounts_with_token, max_workers=max_workers)
        self._batch_worker.progress.connect(self._on_batch_quota_progress)
        self._batch_worker.finished_all.connect(self._on_batch_quota_done)
        self._batch_worker.start()

    def _stop_query(self):
        """停止查询/检测"""
        if hasattr(self, '_batch_worker') and self._batch_worker:
            self._batch_worker.stop()
            self._append_log("⏹ 正在停止查询...")
        if hasattr(self, '_status_check_worker') and self._status_check_worker:
            self._status_check_worker.stop()
            self._append_log("⏹ 正在停止检测...")
        self._btn_stop_query.setEnabled(False)

    def _append_log(self, text: str):
        """追加日志并自动滚到底部"""
        self._log_edit.append(text)
        scrollbar = self._log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_batch_quota_progress(self, uid: str, success: bool):
        """批量查询进度"""
        current = self._progress_bar.value() + 1
        self._progress_bar.setValue(current)
        icon = "✅" if success else "❌"
        self._append_log(f"{icon} {uid[:12]}... {'成功' if success else '失败'}")

    def _on_batch_quota_done(self):
        """批量查询完成"""
        self._progress_bar.setVisible(False)
        self._btn_query_all.setVisible(True)
        self._btn_stop_query.setVisible(False)
        self._btn_stop_query.setEnabled(True)
        self._append_log("📊 查询完成！")
        self._apply_filter()
        self._render_page()
        self.quota_updated.emit()  # 通知其他页面刷新

    def _check_all_status(self):
        """检查所有账号的 API Key 状态（风控/失效），同步到上游 Key 池"""
        from PySide6.QtCore import QThread, Signal as QSignal
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self._load_accounts()
        accounts_with_key = [a for a in self._accounts if a.api_key]
        if not accounts_with_key:
            QMessageBox.information(self, "提示", "没有配置 API Key 的账号，无需检测")
            return

        max_workers = self._concurrency_spin.value()
        self._btn_check_status.setEnabled(False)
        self._btn_query_all.setEnabled(False)
        self._btn_stop_query.setVisible(True)
        self._btn_stop_query.setEnabled(True)
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, len(accounts_with_key))
        self._progress_bar.setValue(0)
        self._log_edit.clear()
        self._log_edit.setVisible(True)
        self._append_log(f"🔍 开始检测 {len(accounts_with_key)} 个账号状态，并发数: {max_workers}")

        class StatusCheckWorker(QThread):
            """后台并发检测 API Key 风控状态线程"""
            progress = QSignal(str, bool, str)  # nickname, success, status_text
            done = QSignal(int, int, int, list, list)  # (正常, 异常, 失败, 异常key列表, 限流key列表)

            def __init__(self, accounts, max_workers=5):
                super().__init__()
                self._accounts = accounts
                self.max_workers = max_workers
                self._stop_flag = False

            def stop(self):
                self._stop_flag = True

            def _check_one(self, acc):
                api_key = acc.api_key
                nickname = acc.nickname or acc.uid
                try:
                    result = check_api_key_chat_status(api_key, attempts=3)
                    return (
                        nickname,
                        result.get("success", False),
                        result.get("status_text", "check_failed"),
                        api_key,
                        result.get("flag"),
                    )
                except Exception as e:
                    return (nickname, False, f"异常: {e}", api_key, None)

            def run(self):
                normal = 0
                abnormal = 0
                failed = 0
                abnormal_keys = []
                rate_limited_keys = []
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {executor.submit(self._check_one, acc): acc
                               for acc in self._accounts}
                    for future in as_completed(futures):
                        if self._stop_flag:
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        try:
                            nickname, success, status_text, api_key, flag = future.result()
                            self.progress.emit(nickname, success, status_text)
                            if flag == "abnormal":
                                abnormal += 1
                                abnormal_keys.append(api_key)
                            elif flag == "rate_limited":
                                abnormal += 1
                                rate_limited_keys.append(api_key)
                            elif success:
                                normal += 1
                            else:
                                failed += 1
                        except Exception:
                            failed += 1
                self.done.emit(normal, abnormal, failed, abnormal_keys, rate_limited_keys)

        worker = StatusCheckWorker(accounts_with_key, max_workers=max_workers)

        def _on_progress(nickname, success, status_text):
            current = self._progress_bar.value() + 1
            self._progress_bar.setValue(current)
            icon = "✅" if success else ("⚠️" if status_text in ("风控异常", "限流(401)") else "❌")
            self._append_log(f"{icon} {nickname} → {status_text}")

        def _on_done(normal, abnormal, failed, abnormal_keys, rate_limited_keys):
            self._btn_check_status.setEnabled(True)
            self._btn_query_all.setEnabled(True)
            self._btn_stop_query.setVisible(False)
            self._btn_stop_query.setEnabled(True)
            self._progress_bar.setVisible(False)

            # 同步到上游 Key 池
            try:
                from ...modules.proxy_server import ProxyDatabase
                proxy_db = ProxyDatabase.get_instance()
                all_keys = proxy_db.get_upstream_keys()
                for k in all_keys:
                    k_api = k.get("api_key", "")
                    k_id = k.get("key_id", "")
                    if k_api in abnormal_keys and k.get("status") != "abnormal":
                        proxy_db.update_upstream_key(k_id, {"status": "abnormal"})
                    elif k_api in rate_limited_keys and k.get("status") != "rate_limited":
                        proxy_db.update_upstream_key(k_id, {"status": "rate_limited"})
                    elif (k_api not in abnormal_keys
                          and k_api not in rate_limited_keys
                          and k.get("status") in ("abnormal", "rate_limited")):
                        # 之前异常/限流，本次检测通过 → 恢复 active
                        proxy_db.update_upstream_key(k_id, {"status": "active"})
                proxy_db._dirty = True
                proxy_db._flush_to_disk()
                self._append_log("✅ 上游 Key 池已同步")
            except Exception as e:
                self._append_log(f"⚠️ 同步上游池失败: {e}")

            rate_limited_count = len(rate_limited_keys)
            msg = f"检测完成：✅ 正常 {normal} 个"
            if abnormal > 0:
                msg += f"，⚠️ 异常 {abnormal} 个（已标记到上游池）"
            if rate_limited_count > 0:
                msg += f"，⚠️ 限流 {rate_limited_count} 个（已标记限流）"
            if failed > 0:
                msg += f"，❓ 失败 {failed} 个"
            self._append_log(msg)
            QMessageBox.information(self, "检测完成", msg)

        worker.progress.connect(_on_progress)
        worker.done.connect(_on_done)
        self._status_check_worker = worker
        worker.start()

    def _copy_field(self, value: str, label: str):
        """复制指定字段到剪贴板"""
        if not value:
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(value)

    def _export_selected_accounts(self):
        selected = self._get_selected_accounts()
        rows = [
            f"{account.display_name}----{account.api_key}"
            for account in selected
            if account.api_key
        ]
        if not selected:
            return
        if not rows:
            QMessageBox.warning(self, t("common.warning"), "选中的账号没有可导出的 API Key")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出账号 API Key",
            "accounts_api_keys.txt",
            "Text Files (*.txt);;All Files (*)",
        )
        if not file_path:
            return

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("\n".join(rows))
            QMessageBox.information(self, "导出完成", f"已导出 {len(rows)} 个 API Key")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：{e}")

    def _sync_delete_key_pool(self, account: Account):
        """删除账号时同步删除 Key 池中对应的 Key"""
        try:
            from ...modules.proxy_server import ProxyDatabase
            proxy_db = ProxyDatabase.get_instance()
            keys = proxy_db.get_upstream_keys()
            # 用 api_key 或 auth_token 匹配
            tokens_to_remove = set()
            if account.api_key:
                tokens_to_remove.add(account.api_key)
            if account.auth_token:
                tokens_to_remove.add(account.auth_token)
            for k in keys:
                if k.get("api_key", "") in tokens_to_remove:
                    proxy_db.delete_upstream_key(k["key_id"])
        except Exception:
            pass  # Key池删除失败不影响账号删除

    def _delete_account(self, account: Account):
        reply = QMessageBox.question(
            self, t("common.confirm"),
            f"确定要删除账号 {account.display_name} 吗？",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            # 同步删除 Key 池中对应的 Key
            self._sync_delete_key_pool(account)
            delete_account(account.uid)
            self._refresh_table()

    def _batch_delete(self):
        selected = self._get_selected_accounts()
        if not selected:
            return

        names = [a.display_name for a in selected]
        if len(names) <= 10:
            name_list = "\n".join(f"  • {n}" for n in names)
        else:
            name_list = "\n".join(f"  • {n}" for n in names[:10])
            name_list += f"\n  ... 还有 {len(names) - 10} 个账号"

        reply = QMessageBox.question(
            self, "确认批量删除",
            f"确定要删除以下 {len(selected)} 个账号吗？\n\n{name_list}\n\n"
            f"此操作不可撤销！",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            for account in selected:
                self._sync_delete_key_pool(account)
                delete_account(account.uid)
            self._refresh_table()

    def _add_account(self):
        """添加单个账号"""
        dialog = AddAccountDialog(self)
        dialog.account_added.connect(self._on_account_added)
        dialog.exec()

    def _on_account_added(self, account: Account):
        """添加账号后刷新表格"""
        self._refresh_table()

    def _import_batch(self):
        """从文件批量导入账号（支持 JSON 含 api_key / 纯 api_key 列表 / JWT Token）"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择账号文件", "",
            "JSON 文件 (*.json);;文本文件 (*.txt);;CSV 文件 (*.csv);;所有文件 (*)"
        )
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            QMessageBox.warning(self, "读取失败", f"无法读取文件：{e}")
            return

        added = 0
        skipped = 0
        updated = 0

        # 优先尝试 JSON 格式
        try:
            import json
            data = json.loads(content)
            if isinstance(data, list):
                for item in data:
                    try:
                        # 支持字段：api_key / auth_token / access_token / uid / nickname / sub / preferred_username
                        token = item.get("auth_token", "") or item.get("access_token", "")
                        api_key = item.get("api_key", "")
                        uid = item.get("uid", "") or item.get("sub", "")
                        nickname = item.get("nickname", "") or item.get("preferred_username", "")

                        if not token and not api_key and not uid:
                            skipped += 1
                            continue

                        # 优先用 api_key（ck_xxx），其次 token
                        effective_credential = api_key or token

                        # JSON里没有uid就从token解析
                        if not uid and token and not token.startswith("ck_"):
                            from ...modules.oauth import decode_jwt
                            payload = decode_jwt(token)
                            uid = payload.get("sub", "")
                            nickname = nickname or payload.get("preferred_username", "")

                        if not uid:
                            # ck_ 开头的 api_key，uid 必填或用 api_key 前缀
                            if api_key:
                                uid = item.get("uid", "") or f"api_{api_key[3:11]}"
                                nickname = nickname or uid
                            else:
                                skipped += 1
                                continue

                        # 检查是否已存在（按uid去重）
                        existing = [a for a in load_accounts() if a.uid == uid]
                        account = Account(
                            uid=uid,
                            nickname=nickname or uid,
                            platform=Platform.CODEBUDDY,
                            auth_token=effective_credential,
                            api_key=api_key,
                        )
                        save_account(account)

                        # 同步导入到上游 Key 池
                        if api_key:
                            try:
                                from ...modules.proxy_server import ProxyDatabase
                                proxy_db = ProxyDatabase.get_instance()
                                existing_keys = {k.get("api_key", "") for k in proxy_db.get_upstream_keys()}
                                if api_key not in existing_keys:
                                    import secrets as _sec
                                    proxy_db.add_upstream_key({
                                        "key_id": f"ck_{_sec.token_hex(4)}",
                                        "api_key": api_key,
                                        "label": uid,
                                        "status": "active",
                                        "points": "",
                                        "points_updated_at": "",
                                        "packages": [],
                                        "created_at": "",
                                    })
                                    proxy_db._dirty = True
                                    proxy_db._flush_to_disk()
                            except Exception:
                                pass

                        if existing:
                            updated += 1
                        else:
                            added += 1
                    except Exception:
                        skipped += 1

                self._refresh_table()
                msg = f"✅ 成功导入 {added} 个账号"
                if updated:
                    msg += f"\n🔄 更新 {updated} 个已有账号"
                if skipped:
                    msg += f"\n⚠️ 跳过 {skipped} 个（无效数据）"
                QMessageBox.information(self, "导入完成", msg)
                return
        except (json.JSONDecodeError, TypeError):
            pass  # 不是JSON，尝试文本格式

        # 文本格式：每行一个 Token 或 API Key，支持 "手机号----apikey" 格式
        tokens = []
        for line in content.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            if "----" in line:
                # 格式：手机号----apikey
                parts = line.split("----")
                if len(parts) >= 2:
                    phone = parts[0].strip().strip('"').strip("'")
                    api_key = parts[1].strip().strip('"').strip("'")
                    if phone and api_key:
                        tokens.append({"phone": phone, "api_key": api_key})
                        continue
            if "," in line:
                for part in line.split(","):
                    part = part.strip().strip('"').strip("'")
                    if part:
                        tokens.append(part)
            else:
                tokens.append(line)

        if not tokens:
            QMessageBox.warning(self, "导入失败", "文件中没有找到有效的 Token 或 API Key")
            return

        from ...modules.oauth import decode_jwt

        for token in tokens:
            try:
                # 支持 "手机号----apikey" 格式（dict）
                if isinstance(token, dict):
                    phone = token["phone"]
                    api_key = token["api_key"]
                    uid = phone
                    nickname = phone
                    account = Account(
                        uid=uid,
                        nickname=nickname,
                        platform=Platform.CODEBUDDY,
                        auth_token=api_key,
                        api_key=api_key,
                    )
                    save_account(account)
                    # 导入上游池
                    try:
                        from ...modules.proxy_server import ProxyDatabase
                        proxy_db = ProxyDatabase.get_instance()
                        existing_keys = {k.get("api_key", "") for k in proxy_db.get_upstream_keys()}
                        if api_key not in existing_keys:
                            import secrets as _sec
                            proxy_db.add_upstream_key({
                                "key_id": f"ck_{_sec.token_hex(4)}",
                                "api_key": api_key,
                                "label": phone,
                                "status": "active",
                                "points": "",
                                "points_updated_at": "",
                                "packages": [],
                                "created_at": "",
                            })
                            proxy_db._dirty = True
                            proxy_db._flush_to_disk()
                    except Exception:
                        pass
                    added += 1
                    continue

                # ck_ 开头按 API Key 处理
                if token.startswith("ck_"):
                    uid = f"api_{token[3:11]}"
                    nickname = uid
                    account = Account(
                        uid=uid,
                        nickname=nickname,
                        platform=Platform.CODEBUDDY,
                        auth_token=token,
                        api_key=token,
                    )
                    save_account(account)
                    # 导入上游池
                    try:
                        from ...modules.proxy_server import ProxyDatabase
                        proxy_db = ProxyDatabase.get_instance()
                        existing_keys = {k.get("api_key", "") for k in proxy_db.get_upstream_keys()}
                        if token not in existing_keys:
                            import secrets as _sec
                            proxy_db.add_upstream_key({
                                "key_id": f"ck_{_sec.token_hex(4)}",
                                "api_key": token,
                                "label": uid,
                                "status": "active",
                                "points": "",
                                "points_updated_at": "",
                                "packages": [],
                                "created_at": "",
                            })
                            proxy_db._dirty = True
                            proxy_db._flush_to_disk()
                    except Exception:
                        pass
                    added += 1
                else:
                    # JWT Token
                    payload = decode_jwt(token)
                    uid = payload.get("sub", "")
                    nickname = payload.get("preferred_username", "")
                    if not uid:
                        skipped += 1
                        continue
                    account = Account(
                        uid=uid,
                        nickname=nickname,
                        platform=Platform.CODEBUDDY,
                        auth_token=token,
                    )
                    save_account(account)
                    added += 1
            except Exception:
                skipped += 1

        self._refresh_table()
        msg = f"✅ 成功导入 {added} 个账号"
        if skipped:
            msg += f"\n⚠️ 跳过 {skipped} 个（无效 Token/API Key）"
        QMessageBox.information(self, "导入完成", msg)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_table()


class ServerFetchDialog(QDialog):
    """从服务器批量获取账号对话框 — 大输入框 + 进度条 + 防卡死
    只获取账号凭证信息（CK/TK/API Key），不查积分
    """

    accounts_imported = Signal(list)  # 传入 List[dict]

    SERVER_URL = "http://103.36.63.44:9658"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🌐 从服务器获取账号")
        self.setMinimumSize(680, 620)
        self._cancel_requested = False
        # macOS 修复：嵌套 QDialog 内 QTextEdit 无法接收键盘输入
        # 原因：macOS 上 Qt 的嵌套 QDialog 会拦截子控件的键盘事件
        # 解决：设为独立窗口，让内部控件能正常接收键盘输入
        import sys
        if sys.platform == "darwin":
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ─── 说明 ───
        hint = QLabel(
            "输入卡密批量获取账号凭证（CK/TK/API Key），每行一个。支持格式：\n"
            "• 16位数字卡密\n"
            "• 手机号----登录URL\n"
            "• 子API Key (sk_xxx)"
        )
        hint.setObjectName("inline_hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ─── 大输入框 ───
        self._input = QTextEdit()
        self._input.setPlaceholderText(
            "每行一个卡密，例如：\n"
            "1234567890123456\n"
            "13800138000----https://copilot.tencent.com/login?platform=xxx&state=yyy\n"
            "sk_abc123def456"
        )
        self._input.setMinimumHeight(200)
        layout.addWidget(self._input, 1)

        # ─── 进度区域 ───
        prog_box = QVBoxLayout()
        prog_box.setSpacing(6)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("color: #2B6CB0; font-size: 12px; font-weight: 600;")
        prog_box.addWidget(self._progress_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setMinimum(0)
        self._progress_bar.setMaximum(100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%v/%m (%p%)")
        self._progress_bar.setVisible(False)
        prog_box.addWidget(self._progress_bar)

        self._detail_label = QLabel("")
        self._detail_label.setStyleSheet("color: #718096; font-size: 11px;")
        self._detail_label.setWordWrap(True)
        self._detail_label.setMaximumHeight(120)
        prog_box.addWidget(self._detail_label)

        layout.addLayout(prog_box)

        # ─── 结果表格 ───
        self._result_table = QTableWidget()
        self._result_table.setColumnCount(4)
        self._result_table.setHorizontalHeaderLabels(["手机号", "API Key", "登录URL", "状态"])
        self._result_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._result_table.setAlternatingRowColors(True)
        self._result_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._result_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._result_table.setMaximumHeight(200)
        self._result_table.setVisible(False)
        layout.addWidget(self._result_table)

        # ─── 按钮 ───
        btn_row = QHBoxLayout()

        self._btn_fetch = QPushButton("🚀 开始获取")
        self._btn_fetch.setObjectName("primary_btn")
        self._btn_fetch.setMinimumHeight(36)
        self._btn_fetch.clicked.connect(self._start_fetch)
        btn_row.addWidget(self._btn_fetch)

        self._btn_cancel = QPushButton("⏹ 取消")
        self._btn_cancel.setObjectName("secondary_btn")
        self._btn_cancel.setMinimumHeight(36)
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._cancel_fetch)
        btn_row.addWidget(self._btn_cancel)

        self._btn_import = QPushButton("📥 导入选中")
        self._btn_import.setObjectName("primary_btn")
        self._btn_import.setMinimumHeight(36)
        self._btn_import.setEnabled(False)
        self._btn_import.clicked.connect(self._import_selected)
        btn_row.addWidget(self._btn_import)

        btn_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("secondary_btn")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    # ─── 解析输入 ───

    def _parse_lines(self, text: str) -> list:
        """解析多行输入，每行一个卡密，自动识别格式"""
        items = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("sk_"):
                items.append({"sub_api_key": line, "_raw": line})
            elif "----" in line:
                items.append({"phone_url": line, "_raw": line})
            else:
                items.append({"card_code": line, "_raw": line})
        return items

    # ─── 启动获取 ───

    def _start_fetch(self):
        text = self._input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "提示", "请输入卡密")
            return

        items = self._parse_lines(text)
        if not items:
            QMessageBox.warning(self, "提示", "未识别到有效卡密")
            return

        self._items = items
        self._results = []  # List[dict] 每个账号的结果
        self._cancel_requested = False

        # UI切换到工作状态
        self._btn_fetch.setEnabled(False)
        self._btn_cancel.setEnabled(True)
        self._btn_import.setEnabled(False)
        self._progress_bar.setVisible(True)
        self._progress_bar.setMaximum(len(items))
        self._progress_bar.setValue(0)
        self._progress_label.setText(f"⏳ 准备获取 {len(items)} 个卡密的账号信息...")
        self._detail_label.setText("")
        self._result_table.setRowCount(0)
        self._result_table.setVisible(False)

        from PySide6.QtCore import QThread, Signal as QSignal

        class BatchFetchThread(QThread):
            """后台批量获取线程 — 只获取凭证，不查积分，防卡死"""
            progress = QSignal(int, str)         # current_index, status_text
            item_done = QSignal(int, dict)       # index, result_dict
            all_done = QSignal(list)             # all results

            SERVER_URL = "http://103.36.63.44:9658"

            def __init__(self, items):
                super().__init__()
                self._items = items
                self._cancelled = False

            def cancel(self):
                self._cancelled = True

            def _fetch_one(self, item: dict) -> dict:
                """获取单个卡密的账号凭证（CK/TK/API Key），不查积分"""
                import requests, json

                raw = item.get("_raw", "")
                result = {"raw": raw, "success": False, "phone": "", "api_key": "",
                          "login_url": "", "error": ""}

                # ─── 展开子API Key ───
                query_items = []
                if "sub_api_key" in item:
                    try:
                        resp = requests.post(
                            f"{self.SERVER_URL}/api/get_active_keys",
                            json={"sub_api_key": item["sub_api_key"]},
                            headers={"Content-Type": "application/json"},
                            timeout=15,
                        )
                        data = resp.json()
                        if not data.get("success"):
                            result["error"] = f"子Key验证失败: {data.get('message', '未知')}"
                            return result
                        for ak in (data.get("active_keys") or []):
                            phone = ak.get("phone", "")
                            api_url = ak.get("api_url", "")
                            if phone:
                                query_items.append({"phone_url": f"{phone}----{api_url}" if api_url else phone, "_phone": phone, "_api_url": api_url})
                        if not query_items:
                            result["error"] = "子Key无活跃主Key"
                            return result
                    except Exception as e:
                        result["error"] = f"子Key异常: {e}"
                        return result
                else:
                    query_items = [item]

                # ─── 提交 web_batch_query 获取手机号 ───
                try:
                    resp = requests.post(
                        f"{self.SERVER_URL}/api/web_batch_query",
                        json={"items": query_items},
                        headers={"Content-Type": "application/json"},
                        timeout=30,
                    )
                except requests.ConnectionError:
                    result["error"] = "连接失败"
                    return result
                except requests.Timeout:
                    result["error"] = "提交超时"
                    return result

                if not resp.ok:
                    result["error"] = f"HTTP {resp.status_code}"
                    return result

                data = resp.json()
                if not data.get("success"):
                    result["error"] = data.get("message", "查询失败")
                    return result

                # 从 results 中提取手机号和 key（不需要等SSE查分完成）
                accounts_found = []
                results_list = data.get("results", [])
                for r in results_list:
                    if r.get("success"):
                        phone = r.get("phone", "")
                        key = r.get("key", "")
                        api_key = r.get("api_key", "")  # web_batch_query 已返回 api_key，直接取
                        accounts_found.append({"phone": phone, "key": key, "api_key": api_key})
                    else:
                        # 某项失败
                        result["error"] = r.get("message", "卡密错误")
                        return result

                if not accounts_found:
                    result["error"] = "未获取到账号信息"
                    return result

                # ─── 获取 API Key ───
                api_keys_map = {}  # phone -> api_key
                try:
                    credentials = []
                    for qi in query_items:
                        if "card_code" in qi:
                            credentials.append({"type": "card_code", "value": qi["card_code"]})
                        elif "phone_url" in qi:
                            credentials.append({"type": "phone_url", "value": qi["phone_url"]})
                    if credentials:
                        kr = requests.post(
                            f"{self.SERVER_URL}/api/web_batch_get_api_keys",
                            json={"keys": credentials},
                            headers={"Content-Type": "application/json"},
                            timeout=15,
                        )
                        if kr.ok:
                            kd = kr.json()
                            if kd.get("success") and kd.get("data"):
                                for d in kd["data"]:
                                    if d.get("phone") and d.get("api_key"):
                                        api_keys_map[d["phone"]] = d["api_key"]
                except Exception:
                    pass

                # ─── 从 phone_url 中提取登录URL ───
                login_url_map = {}  # phone -> login_url
                for qi in query_items:
                    if "phone_url" in qi:
                        parts = qi["phone_url"].split("----", 1)
                        if len(parts) == 2:
                            p = parts[0].strip()
                            url = parts[1].strip()
                            login_url_map[p] = url
                    if "_phone" in qi and qi.get("_api_url"):
                        login_url_map[qi["_phone"]] = qi["_api_url"]

                # ─── 组装结果 ───
                # 优先用 web_batch_query 直接返回的 api_key，其次用 web_batch_get_api_keys 的结果
                if len(accounts_found) == 1:
                    acc = accounts_found[0]
                    phone = acc.get("phone", "")
                    direct_api_key = acc.get("api_key", "")
                    result.update({
                        "success": True,
                        "phone": phone,
                        "api_key": direct_api_key or api_keys_map.get(phone, ""),
                        "login_url": login_url_map.get(phone, ""),
                    })
                else:
                    # 多账号：第一个放主结果，其余放 extra_accounts
                    result.update({
                        "success": True,
                        "phone": "",
                        "api_key": "",
                        "login_url": "",
                        "extra_accounts": [],
                    })
                    for acc in accounts_found:
                        phone = acc.get("phone", "")
                        direct_api_key = acc.get("api_key", "")
                        sub = {
                            "success": True,
                            "phone": phone,
                            "api_key": direct_api_key or api_keys_map.get(phone, ""),
                            "login_url": login_url_map.get(phone, ""),
                            "raw": raw,
                        }
                        if not result["phone"]:
                            result.update(sub)
                        else:
                            result.setdefault("extra_accounts", []).append(sub)

                return result

            def run(self):
                for i, item in enumerate(self._items):
                    if self._cancelled:
                        break
                    raw = item.get("_raw", f"项目{i+1}")
                    self.progress.emit(i, f"正在获取第 {i+1}/{len(self._items)} 个: {raw[:20]}...")

                    try:
                        r = self._fetch_one(item)
                    except Exception as e:
                        r = {"raw": raw, "success": False, "error": str(e),
                             "phone": "", "api_key": "", "login_url": ""}

                    self.item_done.emit(i, r)

                self.all_done.emit([])

        self._thread = BatchFetchThread(items)
        self._thread.progress.connect(self._on_progress)
        self._thread.item_done.connect(self._on_item_done)
        self._thread.all_done.connect(self._on_all_done)
        self._thread.start()

    def _cancel_fetch(self):
        self._cancel_requested = True
        if hasattr(self, '_thread') and self._thread.isRunning():
            self._thread.cancel()
        self._progress_label.setText("⏹ 正在取消...")
        self._btn_cancel.setEnabled(False)

    # ─── 回调 ───

    def _on_progress(self, idx, text):
        self._progress_label.setText(f"⏳ {text}")
        self._progress_bar.setValue(idx)

    def _on_item_done(self, idx, result):
        self._progress_bar.setValue(idx + 1)
        ok = result.get("success", False)

        # 处理 extra_accounts（子Key展开的多账号）
        all_accounts = []
        if ok:
            all_accounts.append(result)
            for extra in result.get("extra_accounts", []):
                all_accounts.append(extra)

        # 更新结果表格
        self._result_table.setVisible(True)
        for acc in all_accounts:
            row = self._result_table.rowCount()
            self._result_table.insertRow(row)
            self._result_table.setItem(row, 0, QTableWidgetItem(acc.get("phone", "")))

            ak = acc.get("api_key", "")
            self._result_table.setItem(row, 1, QTableWidgetItem(ak[:30] + "..." if len(ak) > 30 else ak))

            login_url = acc.get("login_url", "")
            self._result_table.setItem(row, 2, QTableWidgetItem(login_url[:40] + "..." if len(login_url) > 40 else login_url))

            if ak:
                self._result_table.setItem(row, 3, QTableWidgetItem("✅ 有API Key"))
            elif login_url:
                self._result_table.setItem(row, 3, QTableWidgetItem("⚠️ 仅有URL"))
            else:
                self._result_table.setItem(row, 3, QTableWidgetItem("❓ 仅有手机号"))

            self._results.append(acc)

        # 失败的也显示
        if not ok:
            detail_text = self._detail_label.text()
            err_line = f"❌ {result.get('raw', '?')[:20]}: {result.get('error', '未知')}"
            self._detail_label.setText((detail_text + "\n" + err_line).strip())

            row = self._result_table.rowCount()
            self._result_table.insertRow(row)
            self._result_table.setItem(row, 0, QTableWidgetItem(result.get("raw", "")[:20]))
            self._result_table.setItem(row, 1, QTableWidgetItem(""))
            self._result_table.setItem(row, 2, QTableWidgetItem(""))
            self._result_table.setItem(row, 3, QTableWidgetItem(f"❌ {result.get('error', '未知')[:20]}"))
            for c in range(4):
                it = self._result_table.item(row, c)
                if it:
                    it.setForeground(Qt.red)

    def _on_all_done(self, results):
        self._btn_fetch.setEnabled(True)
        self._btn_cancel.setEnabled(False)

        success_count = sum(1 for r in self._results if r.get("success"))
        fail_count = sum(1 for r in self._results if not r.get("success"))
        total = len(self._results)

        if self._cancel_requested:
            self._progress_label.setText(f"⏹ 已取消 — 成功 {success_count}/{total}")
        else:
            self._progress_label.setText(f"✅ 完成 — 成功 {success_count}，失败 {fail_count}，共 {total}")

        self._progress_bar.setValue(self._progress_bar.maximum())

        if success_count > 0:
            self._btn_import.setEnabled(True)
            self._result_table.selectAll()

    # ─── 导入 ───

    def _import_selected(self):
        """导入选中的行到账号列表"""
        selected_rows = set()
        for item in self._result_table.selectedItems():
            selected_rows.add(item.row())

        if not selected_rows:
            QMessageBox.warning(self, "提示", "请先在表格中选择要导入的账号")
            return

        accounts = []
        phones_with_cookie = []  # 收集有手机号的账号，用于批量获取Cookie
        sorted_rows = sorted(selected_rows)
        for row in sorted_rows:
            phone_item = self._result_table.item(row, 0)
            if not phone_item:
                continue
            phone = phone_item.text()
            matched = [r for r in self._results if r.get("success") and r.get("phone") == phone]
            if not matched:
                continue
            r = matched[0]

            # CK 格式: phone----login_url，确保登录网页时能提取手机号和短信链接
            ck_value = ""
            login_url = r.get("login_url", "")
            if phone and login_url:
                ck_value = f"{phone}----{login_url}"
            elif login_url:
                ck_value = login_url
            elif phone:
                ck_value = phone

            acc_data = {
                "uid": phone or r.get("phone", ""),
                "nickname": phone or r.get("phone", ""),
                "auth_token": r.get("api_key", ""),
                "platform": Platform.CODEBUDDY,
                "domain": "www.codebuddy.cn",
                "ck": ck_value,
                "api_key": r.get("api_key", ""),
            }
            accounts.append(acc_data)
            if phone:
                phones_with_cookie.append(phone)

        # ── 批量从服务器获取 Cookie 并保存到本地 ──
        api_url_map = {}
        if phones_with_cookie:
            api_url_map = self._fetch_and_save_cookies(phones_with_cookie)

        # 用服务器返回的 api_url 补全缺少 login_url 的账号
        if api_url_map:
            for acc_data in accounts:
                phone = acc_data.get("uid", "")
                ck = acc_data.get("ck", "")
                # 如果 CK 里没有 URL，用服务器的 api_url 补全
                if phone and phone in api_url_map and "----" not in ck and "http" not in ck:
                    acc_data["ck"] = f"{phone}----{api_url_map[phone]}"

        if accounts:
            self.accounts_imported.emit(accounts)
            QMessageBox.information(self, "导入成功", f"已导入 {len(accounts)} 个账号")
            self.accept()
        else:
            QMessageBox.warning(self, "提示", "没有可导入的有效账号")

    def _fetch_and_save_cookies(self, phones: list):
        """从服务器批量获取 Cookie 并保存到本地文件

        调用服务器 batch_get_cookies API，把返回的 cookie_data 保存到
        ~/.antigravity-tools/cookies/cookie_{phone}.json，登录网页时可直接使用。
        同时用服务器返回的 api_url 补全缺少 login_url 的账号。
        """
        import requests, json, os
        from pathlib import Path

        try:
            resp = requests.post(
                f"{self.SERVER_URL}/api/batch_get_cookies",
                json={"phones": phones},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if not resp.ok:
                return {}

            data = resp.json()
            if not data.get("success"):
                return {}

            cookie_dir = Path(os.path.expanduser("~")) / ".antigravity-tools" / "cookies"
            cookie_dir.mkdir(parents=True, exist_ok=True)

            saved = 0
            api_url_map = {}  # phone -> api_url，用于补全 CK
            for acc in data.get("accounts", []):
                phone = acc.get("phone", "")
                # 优先使用 cookie_data，其次 original_cookie_data
                cookie_data = acc.get("cookie_data", "") or acc.get("original_cookie_data", "")
                api_url = acc.get("api_url", "")

                # 记录 api_url 用于补全 CK
                if phone and api_url:
                    api_url_map[phone] = api_url

                if not phone or not cookie_data:
                    continue

                # cookie_data 是 JSON 数组字符串，和 Playwright cookies 格式一致
                try:
                    cookies_list = json.loads(cookie_data) if isinstance(cookie_data, str) else cookie_data
                    if isinstance(cookies_list, list) and len(cookies_list) > 0:
                        cookie_file = cookie_dir / f"cookie_{phone}.json"
                        cookie_file.write_text(
                            json.dumps(cookies_list, ensure_ascii=False, indent=2),
                            encoding="utf-8"
                        )
                        saved += 1
                except (json.JSONDecodeError, TypeError):
                    continue

            if saved > 0:
                logger.info(f"从服务器获取并保存了 {saved} 个账号的 Cookie")

            return api_url_map
        except Exception as e:
            logger.warning(f"从服务器获取 Cookie 失败: {e}")
            return {}
