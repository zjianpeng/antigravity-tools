"""设置页面"""

import json
import os
import plistlib
import subprocess
import sys

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QComboBox, QSpinBox, QLineEdit, QCheckBox, QGroupBox, QFormLayout,
    QRadioButton, QButtonGroup, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QHeaderView, QScrollArea, QMessageBox
)
from PySide6.QtCore import Qt

from ...i18n import t, set_language, get_language
from ...utils.store import save_setting, load_setting
from ..styles import get_stylesheet


class SettingsPage(QWidget):
    """设置页面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content_area")
        self._main_window = None
        self._setup_ui()

    def set_main_window(self, window):
        self._main_window = window

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel(t("settings.title"))
        title.setObjectName("page_title")
        layout.addWidget(title)

        subtitle = QLabel("配置应用行为和外观")
        subtitle.setObjectName("page_subtitle")
        layout.addWidget(subtitle)

        scroll_area = QScrollArea()
        scroll_area.setObjectName("settings_scroll_area")
        scroll_area.viewport().setObjectName("settings_scroll_viewport")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("settings_scroll_content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(32, 0, 32, 32)
        content_layout.setSpacing(20)

        # === 通用设置 ===
        general_group = QGroupBox("🎨 " + t("settings.general"))
        general_form = QFormLayout(general_group)
        general_form.setSpacing(12)
        general_form.setContentsMargins(20, 24, 20, 20)

        # 主题
        self._theme_combo = QComboBox()
        self._theme_combo.addItems([t("settings.theme_light"), t("settings.theme_dark"), t("settings.theme_system")])
        self._theme_combo.setCurrentIndex(2)  # 默认跟随系统
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        general_form.addRow(t("settings.theme") + ":", self._theme_combo)

        # 语言
        self._lang_combo = QComboBox()
        self._lang_combo.addItems(["简体中文", "English"])
        self._lang_combo.setCurrentIndex(0)
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        general_form.addRow(t("settings.language") + ":", self._lang_combo)

        # UI 缩放
        self._scale_spin = QSpinBox()
        self._scale_spin.setRange(80, 200)
        self._scale_spin.setValue(100)
        self._scale_spin.setSuffix("%")
        general_form.addRow(t("settings.ui_scale") + ":", self._scale_spin)

        # 关闭行为
        self._close_combo = QComboBox()
        self._close_combo.addItems([t("settings.close_minimize"), t("settings.close_exit")])
        general_form.addRow(t("settings.close_behavior") + ":", self._close_combo)

        # 开机自启
        self._startup_check = QCheckBox(t("settings.show_on_startup"))
        general_form.addRow("", self._startup_check)

        content_layout.addWidget(general_group)

        # === 刷新设置 ===
        refresh_group = QGroupBox("🔄 配额刷新")
        refresh_form = QFormLayout(refresh_group)
        refresh_form.setSpacing(12)
        refresh_form.setContentsMargins(20, 24, 20, 20)

        self._refresh_spin = QSpinBox()
        self._refresh_spin.setRange(5, 120)
        self._refresh_spin.setValue(30)
        self._refresh_spin.setSuffix(" 分钟")
        refresh_form.addRow(t("settings.auto_refresh") + ":", self._refresh_spin)

        content_layout.addWidget(refresh_group)

        # === 敏感信息检测 ===
        sensitive_group = QGroupBox("🛡️ 敏感信息检测")
        sensitive_layout = QVBoxLayout(sensitive_group)
        sensitive_layout.setSpacing(12)
        sensitive_layout.setContentsMargins(20, 24, 20, 20)

        switch_row = QHBoxLayout()
        switch_label = QLabel("检测系统提示词:")
        self._sensitive_off_radio = QRadioButton("关")
        self._sensitive_on_radio = QRadioButton("开")
        self._sensitive_radio_group = QButtonGroup(self)
        self._sensitive_radio_group.addButton(self._sensitive_off_radio)
        self._sensitive_radio_group.addButton(self._sensitive_on_radio)
        self._sensitive_off_radio.setChecked(True)
        self._sensitive_radio_group.buttonClicked.connect(lambda _button: self._on_sensitive_switch_changed())
        switch_row.addWidget(switch_label)
        switch_row.addWidget(self._sensitive_off_radio)
        switch_row.addWidget(self._sensitive_on_radio)
        switch_row.addStretch()
        sensitive_layout.addLayout(switch_row)

        self._sensitive_table = QTableWidget(0, 2)
        self._sensitive_table.setHorizontalHeaderLabels(["敏感关键词 K（必填）", "替换词 V（可空）"])
        self._sensitive_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._sensitive_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._sensitive_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._sensitive_table.setEditTriggers(
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.SelectedClicked |
            QAbstractItemView.EditKeyPressed
        )
        self._sensitive_table.setMinimumHeight(130)
        self._sensitive_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._sensitive_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        sensitive_layout.addWidget(self._sensitive_table)

        sensitive_btn_row = QHBoxLayout()
        self._add_sensitive_btn = QPushButton("添加")
        self._add_sensitive_btn.setCursor(Qt.PointingHandCursor)
        self._add_sensitive_btn.clicked.connect(self._add_sensitive_row)
        self._remove_sensitive_btn = QPushButton("删除选中")
        self._remove_sensitive_btn.setCursor(Qt.PointingHandCursor)
        self._remove_sensitive_btn.clicked.connect(self._remove_sensitive_rows)
        sensitive_btn_row.addWidget(self._add_sensitive_btn)
        sensitive_btn_row.addWidget(self._remove_sensitive_btn)
        sensitive_btn_row.addStretch()
        sensitive_layout.addLayout(sensitive_btn_row)

        content_layout.addWidget(sensitive_group)
        self._on_sensitive_switch_changed()

        # 保存按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        btn_save = QPushButton("💾 保存设置")
        btn_save.setObjectName("primary_btn")
        btn_save.setCursor(Qt.PointingHandCursor)
        btn_save.clicked.connect(self._save_settings)
        btn_row.addWidget(btn_save)

        content_layout.addLayout(btn_row)
        content_layout.addStretch()
        scroll_area.setWidget(content)
        layout.addWidget(scroll_area, 1)

    def _on_theme_changed(self, index: int):
        """主题切换"""
        themes = ["light", "dark", "system"]
        theme = themes[index]
        save_setting("theme", theme)
        if self._main_window:
            self._main_window.apply_theme(theme)

    def _on_language_changed(self, index: int):
        """语言切换"""
        langs = ["zh-CN", "en"]
        lang = langs[index]
        set_language(lang)
        save_setting("language", lang)

    def _save_settings(self):
        """保存所有设置。"""
        startup_enabled = self._startup_check.isChecked()
        startup_ok, startup_error = self._set_auto_startup(startup_enabled)
        if not startup_ok:
            self._startup_check.setChecked(self._get_auto_startup_enabled())
            QMessageBox.warning(
                self,
                "开机自启设置失败",
                f"无法{'启用' if startup_enabled else '关闭'}开机自启：\n{startup_error}",
            )
            return

        save_setting("ui_scale", str(self._scale_spin.value()))
        save_setting("close_behavior", "minimize" if self._close_combo.currentIndex() == 0 else "exit")
        save_setting("startup", str(startup_enabled))
        save_setting("refresh_interval", str(self._refresh_spin.value()))
        save_setting("system_prompt_sensitive_enabled", "True" if self._sensitive_on_radio.isChecked() else "False")

        sensitive_pairs = self._collect_sensitive_pairs()
        if sensitive_pairs is None:
            return
        save_setting("system_prompt_sensitive_replacements", json.dumps(sensitive_pairs, ensure_ascii=False))

        QMessageBox.information(self, t("common.success"), "设置已保存")

    def _on_sensitive_switch_changed(self):
        """切换敏感信息检测配置启用状态"""
        enabled = self._sensitive_on_radio.isChecked()
        self._sensitive_table.setEnabled(enabled)
        self._add_sensitive_btn.setEnabled(enabled)
        self._remove_sensitive_btn.setEnabled(enabled)

    def _add_sensitive_row(self, key: str = "", value: str = ""):
        """添加一行敏感信息配置"""
        row = self._sensitive_table.rowCount()
        self._sensitive_table.insertRow(row)
        self._sensitive_table.setItem(row, 0, QTableWidgetItem(key))
        self._sensitive_table.setItem(row, 1, QTableWidgetItem(value))

    def _remove_sensitive_rows(self):
        """删除选中的敏感信息配置行"""
        selected_rows = sorted(
            {index.row() for index in self._sensitive_table.selectedIndexes()},
            reverse=True,
        )
        for row in selected_rows:
            self._sensitive_table.removeRow(row)

    def _collect_sensitive_pairs(self) -> list[dict] | None:
        """收集敏感信息检测配置"""
        from PySide6.QtWidgets import QMessageBox

        pairs = []
        seen_keys = set()
        for row in range(self._sensitive_table.rowCount()):
            key_item = self._sensitive_table.item(row, 0)
            value_item = self._sensitive_table.item(row, 1)
            key = key_item.text().strip() if key_item else ""
            value = value_item.text() if value_item else ""
            if not key and not value:
                continue
            if not key:
                QMessageBox.warning(self, t("common.warning"), f"第 {row + 1} 行敏感关键词 K 不能为空")
                return None
            if key in seen_keys:
                QMessageBox.warning(self, t("common.warning"), f"敏感关键词重复: {key}")
                return None
            seen_keys.add(key)
            pairs.append({"key": key, "value": value})
        return pairs

    @staticmethod
    def _project_root() -> str:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

    @staticmethod
    def _python_gui_executable() -> str:
        exe = sys.executable
        if sys.platform == "win32" and os.path.basename(exe).lower() == "python.exe":
            pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(pythonw):
                return pythonw
        return exe

    @staticmethod
    def _startup_app_name() -> str:
        return "AntigravityTools"

    @staticmethod
    def _windows_startup_command() -> str:
        if getattr(sys, "frozen", False):
            work_dir = os.path.dirname(sys.executable)
            args = ["cmd.exe", "/c", "start", "", "/d", work_dir, sys.executable]
        else:
            work_dir = SettingsPage._project_root()
            args = ["cmd.exe", "/c", "start", "", "/d", work_dir, SettingsPage._python_gui_executable(), "-m", "src.main"]
        return subprocess.list2cmdline(args)

    @staticmethod
    def _set_auto_startup(enable: bool) -> tuple[bool, str]:
        """设置开机自启动，返回 (是否成功, 错误信息)。"""
        try:
            if sys.platform == "win32":
                import winreg
                key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
                app_name = SettingsPage._startup_app_name()
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                    if enable:
                        command = SettingsPage._windows_startup_command()
                        winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, command)
                    else:
                        try:
                            winreg.DeleteValue(key, app_name)
                        except FileNotFoundError:
                            pass
                return True, ""

            if sys.platform == "darwin":
                plist_dir = os.path.expanduser("~/Library/LaunchAgents")
                os.makedirs(plist_dir, exist_ok=True)
                plist_path = os.path.join(plist_dir, "com.antigravity.tools.plist")
                if enable:
                    if getattr(sys, "frozen", False):
                        args = [sys.executable]
                        work_dir = os.path.dirname(sys.executable)
                    else:
                        args = [sys.executable, "-m", "src.main"]
                        work_dir = SettingsPage._project_root()
                    plist_data = {
                        "Label": "com.antigravity.tools",
                        "ProgramArguments": args,
                        "WorkingDirectory": work_dir,
                        "RunAtLoad": True,
                    }
                    with open(plist_path, "wb") as f:
                        plistlib.dump(plist_data, f)
                else:
                    if os.path.exists(plist_path):
                        os.remove(plist_path)
                return True, ""

            return False, f"当前平台不支持开机自启: {sys.platform}"
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("设置开机自启动失败: %s", e)
            return False, str(e)

    @staticmethod
    def _get_auto_startup_enabled() -> bool:
        try:
            if sys.platform == "win32":
                import winreg
                key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                    value, _ = winreg.QueryValueEx(key, SettingsPage._startup_app_name())
                    return bool(str(value).strip())
            if sys.platform == "darwin":
                plist_path = os.path.expanduser("~/Library/LaunchAgents/com.antigravity.tools.plist")
                return os.path.exists(plist_path)
        except Exception:
            pass
        return load_setting("startup", "False") == "True"

    def showEvent(self, event):
        """加载已保存的设置"""
        super().showEvent(event)
        # 防止重复加载导致信号触发
        self._theme_combo.blockSignals(True)
        self._lang_combo.blockSignals(True)
        self._close_combo.blockSignals(True)

        try:
            # 主题
            theme = load_setting("theme", "system")
            themes = ["light", "dark", "system"]
            if theme in themes:
                self._theme_combo.setCurrentIndex(themes.index(theme))

            # 语言
            lang = load_setting("language", "zh-CN")
            self._lang_combo.setCurrentIndex(0 if lang == "zh-CN" else 1)

            # 缩放
            scale = int(load_setting("ui_scale", "100"))
            self._scale_spin.setValue(scale)

            # 关闭行为
            close_behavior = load_setting("close_behavior", "minimize")
            self._close_combo.setCurrentIndex(0 if close_behavior == "minimize" else 1)

            # 开机自启
            self._startup_check.setChecked(self._get_auto_startup_enabled())

            # 刷新间隔
            refresh = int(load_setting("refresh_interval", "30"))
            self._refresh_spin.setValue(refresh)

            # 敏感信息检测
            self._sensitive_on_radio.setChecked(load_setting("system_prompt_sensitive_enabled", "False") == "True")
            self._sensitive_off_radio.setChecked(not self._sensitive_on_radio.isChecked())
            self._load_sensitive_rows(load_setting("system_prompt_sensitive_replacements", "[]"))
            self._on_sensitive_switch_changed()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"加载设置失败: {e}")
        finally:
            self._theme_combo.blockSignals(False)
            self._lang_combo.blockSignals(False)
            self._close_combo.blockSignals(False)

    def _load_sensitive_rows(self, raw_value: str):
        """加载敏感信息检测配置行"""
        self._sensitive_table.setRowCount(0)
        try:
            pairs = json.loads(raw_value or "[]")
        except json.JSONDecodeError:
            pairs = []
        if not isinstance(pairs, list):
            return
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            key = str(pair.get("key", "")).strip()
            value = str(pair.get("value", ""))
            if key:
                self._add_sensitive_row(key, value)
