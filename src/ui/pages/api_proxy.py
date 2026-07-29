"""API 代理服务页面 - 本地 API 中转服务

功能：
- 启动/停止本地代理服务
- 上游 Key 池管理（仅从已获取账号导入）
- 子 API Key 管理（创建、删除、模型限制、使用限制）
- 服务状态监控
- 使用日志
"""

import json
import os
import secrets
import copy

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QLineEdit, QSpinBox, QCheckBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QTabWidget, QTextEdit, QComboBox, QDialog,
    QFormLayout, QDialogButtonBox, QMessageBox, QApplication
)
from PySide6.QtCore import Qt, QThread, QTimer, Signal as QSignal, Signal
from PySide6.QtGui import QCursor, QColor, QBrush, QFont

from ...i18n import t
from ...utils.store import save_setting, load_setting, load_accounts
from ..styles.theme import LIGHT_THEME, DARK_THEME
from ...modules.proxy_server import (
    ProxyServer, SUPPORTED_MODELS, ProxyDatabase, MODEL_CONTEXT_LENGTHS, MODEL_MAX_OUTPUT_TOKENS
)


def _fmt_tokens(n: int) -> str:
    """智能 Token 单位转换：千(K)、万(W)、百万(M)、亿(B)"""
    if n <= 0:
        return "-"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f}亿"
    if n >= 1_000_000:
        return f"{n / 10_000:.1f}万"
    if n >= 10_000:
        return f"{n / 10_000:.2f}万"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_credits(c: float) -> str:
    """智能积分单位转换：万、亿"""
    if c <= 0:
        return "-"
    if c >= 100_000_000:
        return f"{c / 100_000_000:.2f}亿"
    if c >= 10_000:
        return f"{c / 10_000:.2f}万"
    return f"{c:.2f}"


def _set_item(table, row, col, text, tooltip=None):
    """设置表格单元格，自动加 tooltip 显示完整内容"""
    item = QTableWidgetItem(text)
    if tooltip:
        item.setToolTip(tooltip)
    else:
        item.setToolTip(text)
    table.setItem(row, col, item)
    return item


def _get_account_concurrency_setting() -> int:
    try:
        return max(1, min(50, int(load_setting("account_concurrency", "5"))))
    except Exception:
        return 5


def _current_theme_colors() -> dict:
    theme = load_setting("theme", "system")
    if theme == "system":
        app = QApplication.instance()
        is_dark = bool(app and app.styleHints().colorScheme() == Qt.ColorScheme.Dark)
        theme = "dark" if is_dark else "light"
    return DARK_THEME if theme == "dark" else LIGHT_THEME


def _style_popup_menu(menu):
    colors = _current_theme_colors()
    menu.setAttribute(Qt.WA_StyledBackground, True)
    menu.setStyleSheet(f"""
        QMenu {{
            background-color: {colors['bg_card']};
            color: {colors['text_primary']};
            border: 1px solid {colors['border']};
            border-radius: 8px;
            padding: 4px;
        }}
        QMenu::item {{
            background-color: transparent;
            color: {colors['text_primary']};
            padding: 8px 24px;
            border-radius: 4px;
        }}
        QMenu::item:selected {{
            background-color: {colors['accent_light']};
            color: {colors['accent']};
        }}
        QMenu::separator {{
            height: 1px;
            background-color: {colors['border_light']};
            margin: 4px 8px;
        }}
    """)


def _apply_context_aliases(entry: dict, context_tokens: int):
    """Write common context-window field names for different clients."""
    if context_tokens:
        max_output_tokens = min(MODEL_MAX_OUTPUT_TOKENS.get(entry.get("id"), 131072), context_tokens)
        entry["maxInputTokens"] = context_tokens
        entry["max_input_tokens"] = context_tokens
        entry["maxOutputTokens"] = max_output_tokens
        entry["max_output_tokens"] = max_output_tokens
        entry["maxTokens"] = max_output_tokens
        entry["contextWindow"] = context_tokens
        entry["contextLength"] = context_tokens
        entry["context_length"] = context_tokens
        entry["maxContextTokens"] = context_tokens
        entry["maxAllowedSize"] = context_tokens
        entry["max_allowed_size"] = context_tokens
    return entry


def _apply_model_protocol_fields(entry: dict, images: bool):
    """Add WorkBuddy catalog-style fields used by newer model capability checks."""
    return entry


def _read_existing_models(target_path: str) -> list:
    """读取 models.json 中已有的模型列表。

    兼容两种格式：
    - WorkBuddy：裸数组 ``[ {...}, {...} ]``
    - CodeBuddy：包裹对象 ``{"models": [ {...} ]}``

    文件不存在或解析失败时返回空列表（不抛异常）。
    """
    if not os.path.exists(target_path):
        return []
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("models"), list):
        return data["models"]
    return []


def _incremental_merge_models(existing: list, new_entries: list):
    """增量合并模型列表。

    匹配规则：当且仅当 ``id`` 与 ``name`` 都相同视为同一模型：
    - 已存在：替换该条目，并做“保留字段合并”——以旧条目为底，新条目字段覆盖之，
      旧条目中独有的字段（新条目未提供）予以保留。
    - 不存在：追加到列表末尾。

    Returns:
        (merged_list, replaced_count, added_count)
    """
    merged = list(existing)
    # 建立 (id, name) -> 索引 的查找表（仅记录首次出现位置，避免重复条目互相覆盖）
    index = {}
    for i, m in enumerate(merged):
        if not isinstance(m, dict):
            continue
        key = (str(m.get("id", "")).strip(), str(m.get("name", "")).strip())
        if key not in index:
            index[key] = i

    replaced = 0
    added = 0
    for entry in new_entries:
        key = (str(entry.get("id", "")).strip(), str(entry.get("name", "")).strip())
        if key in index:
            idx = index[key]
            base = dict(merged[idx]) if isinstance(merged[idx], dict) else {}
            base.update(entry)  # 新字段覆盖旧字段，旧字段中独有的保留
            merged[idx] = base
            replaced += 1
        else:
            merged.append(entry)
            index[key] = len(merged) - 1
            added += 1
    return merged, replaced, added


def _write_models_json(target_path: str, merged: list, wrapper: str) -> None:
    """将合并后的模型列表写回 models.json。

    Args:
        wrapper: ``"array"`` => WorkBuddy 裸数组；``"object"`` => CodeBuddy ``{"models": [...]}``
    """
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        if wrapper == "object":
            json.dump({"models": merged}, f, ensure_ascii=False, indent=2)
        else:
            json.dump(merged, f, ensure_ascii=False, indent=2)


LEGACY_MODEL_FIELDS = {
    "api", "provider", "baseUrl", "input", "compat",
    "disabledMultimodal", "disabled_multimodal", "supports_images",
    "maxInputTokens", "max_input_tokens", "maxOutputTokens", "max_output_tokens",
    "maxTokens", "contextWindow", "contextLength", "context_length",
    "maxContextTokens", "maxAllowedSize", "max_allowed_size",
}

MODEL_ID_ALIASES = {
    "kimi-k2.7-code": "kimi-k2.7",
}

MODEL_NAME_ALIASES = {
    "kimi-k2.7": "Kimi-K2.7-Code",
}


def _normal_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _entry_matches_proxy(entry: dict, proxy_base_urls: set[str]) -> bool:
    if not isinstance(entry, dict):
        return False
    url = _normal_url(entry.get("url", ""))
    return bool(url and url in proxy_base_urls)


def _has_legacy_model_fields(entry: dict) -> bool:
    return isinstance(entry, dict) and any(field in entry for field in LEGACY_MODEL_FIELDS)


def _official_model_entry(entry: dict, include_custom_protocol: bool) -> dict:
    """Convert a legacy generated model entry to WorkBuddy's simple custom-model shape."""
    model_id = str(entry.get("id", "")).strip()
    model_id = MODEL_ID_ALIASES.get(model_id, model_id)
    model_name = entry.get("name") or model_id
    if str(entry.get("id", "")).strip() in MODEL_ID_ALIASES:
        model_name = MODEL_NAME_ALIASES.get(model_id, model_name)

    supports_images = entry.get("supportsImages")
    if supports_images is None and "supports_images" in entry:
        supports_images = entry.get("supports_images")
    if supports_images is None and "disabledMultimodal" in entry:
        supports_images = not bool(entry.get("disabledMultimodal"))
    if supports_images is None and "disabled_multimodal" in entry:
        supports_images = not bool(entry.get("disabled_multimodal"))

    supports_reasoning = bool(entry.get("supportsReasoning", True))
    new_entry = {
        "id": model_id,
        "name": model_name,
        "vendor": entry.get("vendor") or "Custom",
        "url": entry.get("url", ""),
        "apiKey": entry.get("apiKey", ""),
        "supportsToolCall": bool(entry.get("supportsToolCall", True)),
        "supportsImages": bool(True if supports_images is None else supports_images),
        "supportsReasoning": supports_reasoning,
    }

    if include_custom_protocol or "useCustomProtocol" in entry:
        new_entry["useCustomProtocol"] = bool(entry.get("useCustomProtocol", False))

    # Preserve the user's existing thinking/reasoning settings exactly when present.
    if "reasoning" in entry:
        new_entry["reasoning"] = copy.deepcopy(entry.get("reasoning"))
    elif supports_reasoning:
        new_entry["reasoning"] = {"supportedEfforts": ["max"]}

    return new_entry


def _cleanup_legacy_proxy_models(target_path: str, wrapper: str, proxy_base_urls: set[str]) -> int:
    """Clean legacy generated model fields for entries that point at this proxy."""
    models = _read_existing_models(target_path)
    if not models:
        return 0

    changed = 0
    cleaned = []
    cleaned_index = {}
    for entry in models:
        new_entry = entry
        if (
            isinstance(entry, dict)
            and _entry_matches_proxy(entry, proxy_base_urls)
            and (
                _has_legacy_model_fields(entry)
                or str(entry.get("id", "")).strip() in MODEL_ID_ALIASES
            )
        ):
            new_entry = _official_model_entry(entry, include_custom_protocol=(wrapper == "array"))
            changed += 1

        if isinstance(new_entry, dict) and _entry_matches_proxy(new_entry, proxy_base_urls):
            key = (
                str(new_entry.get("id", "")).strip(),
                str(new_entry.get("name", "")).strip(),
                _normal_url(new_entry.get("url", "")),
            )
            if key in cleaned_index:
                cleaned[cleaned_index[key]].update(new_entry)
                changed += 1
                continue
            cleaned_index[key] = len(cleaned)
        cleaned.append(new_entry)

    if changed:
        _write_models_json(target_path, cleaned, wrapper=wrapper)
    return changed


class ModelSelectDialog(QDialog):
    """模型多选对话框 —— 让用户勾选需要配置的模型。

    采用增量写入策略：未勾选的模型在 models.json 中保持不变。
    默认全部勾选，用户可按需取消。
    """

    def __init__(self, title: str, base_url: str, models: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        self._model_ids = list(models)
        self._setup_ui(base_url)

    def _setup_ui(self, base_url: str):
        from PySide6.QtWidgets import QListWidget, QListWidgetItem

        layout = QVBoxLayout(self)

        info = QLabel(
            "请勾选要配置的模型（增量写入，未勾选的模型保留不变）。\n"
            f"接口地址: {base_url}"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # 全选 / 全不选
        btn_row = QHBoxLayout()
        btn_all = QPushButton("全选")
        btn_none = QPushButton("全不选")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        btn_row.addWidget(btn_all)
        btn_row.addWidget(btn_none)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._list = QListWidget()
        self._list.setMaximumHeight(320)
        for mid in self._model_ids:
            item = QListWidgetItem(mid)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)  # 默认全选
            self._list.addItem(item)
        layout.addWidget(self._list)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _set_all(self, checked: bool):
        """勾选或取消全部模型。"""
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(state)

    def selected_models(self) -> list:
        """返回勾选的模型 id 列表（保持原始顺序）。"""
        result = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.Checked:
                result.append(self._model_ids[i])
        return result


class CreateSubKeyDialog(QDialog):
    """创建子 API Key 对话框"""

    def __init__(self, upstream_keys: list, parent=None, edit_data: dict = None):
        super().__init__(parent)
        self._upstream_keys = upstream_keys
        self._edit_data = edit_data  # 编辑模式时传入已有的子 Key 数据
        self.setWindowTitle("编辑子 API Key" if edit_data else "创建子 API Key")
        self.setMinimumWidth(560)
        self._setup_ui()

    def _setup_ui(self):
        layout = QFormLayout(self)
        layout.setSpacing(12)

        self._label_input = QLineEdit()
        self._label_input.setPlaceholderText("子 Key 标签（如用户名）")
        if self._edit_data:
            self._label_input.setText(self._edit_data.get("label", ""))
        layout.addRow("标签:", self._label_input)

        # 允许的模型 — 多选下拉
        self._model_list_widget = self._create_multi_select(
            items=["全部模型"] + [m for m in SUPPORTED_MODELS if m != "auto"],
            selected=self._edit_data.get("allowed_models", []) if self._edit_data else [],
            all_option="全部模型",
        )
        layout.addRow("限制模型:", self._model_list_widget)

        # 最大使用次数
        self._max_usage_spin = QSpinBox()
        self._max_usage_spin.setRange(0, 999999)
        self._max_usage_spin.setValue(self._edit_data.get("max_usage", 0) if self._edit_data else 0)
        self._max_usage_spin.setSpecialValueText("无限")
        self._max_usage_spin.setToolTip("0 = 无限制")
        layout.addRow("最大使用:", self._max_usage_spin)

        # RPM 限制
        self._rpm_spin = QSpinBox()
        self._rpm_spin.setRange(1, 10000)
        self._rpm_spin.setValue(self._edit_data.get("rate_limit_rpm", 1000) if self._edit_data else 1000)
        self._rpm_spin.setSuffix(" RPM")
        layout.addRow("限流:", self._rpm_spin)

        # 关联的上游 Key — 多选列表
        existing_allowed = self._edit_data.get("allowed_key_ids", []) if self._edit_data else []
        self._key_list_widget = self._create_multi_select(
            items=["全部上游 Key"] + [
                k.get("label", "") or k.get("key_id", "") for k in self._upstream_keys
            ],
            selected_indices=[i for i, k in enumerate(self._upstream_keys) if k.get("key_id") in existing_allowed],
            all_option="全部上游 Key",
        )
        layout.addRow("上游 Key:", self._key_list_widget)

        # 调用模式
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("1 - 专一模式", 1)
        self._mode_combo.addItem("2 - 临期优先", 2)
        self._mode_combo.addItem("3 - 轮询模式", 3)
        self._mode_combo.addItem("4 - 会话亲和", 4)
        self._mode_combo.setToolTip(
            "专一模式：粘住一个 Key 用到不可用才换，适合稳定使用\n"
            "临期优先：优先调用绑定的号池里所有 Key 中积分最快过期的\n"
            "轮询模式：每次请求轮换到下一个 Key，均匀分散压力\n"
            "会话亲和：同一会话绑定同一上游 Key，TTL 1 小时，适合多轮对话保持上下文一致性"
        )
        # 设置 Item 的 tooltip（鼠标悬停在选项上时显示）
        self._mode_combo.setItemData(0, "粘住一个 Key 用到不可用才换下一个，适合稳定使用", Qt.ToolTipRole)
        self._mode_combo.setItemData(1, "优先调用号池里积分最快过期的 Key，最大化利用即将到期的积分", Qt.ToolTipRole)
        self._mode_combo.setItemData(2, "每次请求轮换到下一个 Key，均匀分散压力", Qt.ToolTipRole)
        self._mode_combo.setItemData(3, "同一会话绑定同一上游 Key（基于 system+首条 user 消息 hash），TTL 1 小时，适合多轮对话", Qt.ToolTipRole)
        if self._edit_data:
            key_mode = self._edit_data.get("key_mode", 1)
            idx = {1: 0, 2: 1, 3: 2, 4: 3}.get(key_mode, 0)
            self._mode_combo.setCurrentIndex(idx)
        layout.addRow("调用模式:", self._mode_combo)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addRow(btn_box)

    def _create_multi_select(self, items: list, selected: list = None,
                              selected_indices: list = None, all_option: str = None) -> 'QListWidget':
        """创建多选列表控件

        Args:
            items: 选项列表
            selected: 已选中的值列表（用于模型选择）
            selected_indices: 已选中的索引列表（用于 Key 选择）
            all_option: "全部" 选项的文本，勾选时自动全选其他项
        """
        from PySide6.QtWidgets import QListWidget, QListWidgetItem

        list_widget = QListWidget()
        list_widget.setMaximumHeight(180)

        for i, item_text in enumerate(items):
            item = QListWidgetItem(item_text)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)

            if all_option and item_text == all_option:
                # "全部" 选项
                is_all_selected = not selected and not selected_indices
                item.setCheckState(Qt.Checked if is_all_selected else Qt.Unchecked)
                item.setData(Qt.UserRole, "__all__")
            elif selected is not None and item_text in selected:
                item.setCheckState(Qt.Checked)
                item.setData(Qt.UserRole, item_text)
            elif selected_indices is not None:
                # selected_indices 基于 self._upstream_keys（0-based），
                # 有 all_option 时 items[0] 是"全部"，key 从 i=1 开始，需 i-1 对齐
                actual_idx = (i - 1) if all_option else i
                if actual_idx in selected_indices:
                    item.setCheckState(Qt.Checked)
                    if 0 <= actual_idx < len(self._upstream_keys):
                        item.setData(Qt.UserRole, self._upstream_keys[actual_idx].get("key_id", ""))
            else:
                item.setCheckState(Qt.Unchecked)
                if all_option and item_text != all_option:
                    # 存储实际值
                    if self._upstream_keys and i > 0:
                        actual_key_idx = i - 1
                        if actual_key_idx < len(self._upstream_keys):
                            item.setData(Qt.UserRole, self._upstream_keys[actual_key_idx].get("key_id", ""))
                    else:
                        item.setData(Qt.UserRole, item_text)

            if item.data(Qt.UserRole) is None and (not all_option or item_text != all_option):
                if selected_indices is not None:
                    actual_key_idx = (i - 1) if all_option else i
                    if 0 <= actual_key_idx < len(self._upstream_keys):
                        item.setData(Qt.UserRole, self._upstream_keys[actual_key_idx].get("key_id", ""))
                    else:
                        item.setData(Qt.UserRole, item_text)
                    item.setCheckState(Qt.Unchecked)
                else:
                    item.setData(Qt.UserRole, item_text)
                    item.setCheckState(Qt.Unchecked)

            list_widget.addItem(item)

        # 全选/取消全选联动
        if all_option:
            def _on_item_changed(check_item):
                if check_item.data(Qt.UserRole) == "__all__":
                    state = check_item.checkState()
                    for i in range(list_widget.count()):
                        it = list_widget.item(i)
                        if it.data(Qt.UserRole) != "__all__":
                            it.setCheckState(state)

            list_widget.itemChanged.connect(_on_item_changed)

        return list_widget

    def _get_selected_models(self) -> list:
        """获取选中的模型列表"""
        models = []
        has_all = False
        for i in range(self._model_list_widget.count()):
            item = self._model_list_widget.item(i)
            if item.checkState() == Qt.Checked:
                if item.data(Qt.UserRole) == "__all__":
                    has_all = True
                else:
                    models.append(item.text())
        return [] if has_all and not models else models

    def _get_selected_key_ids(self) -> list:
        """获取选中的上游 Key ID 列表"""
        key_ids = []
        has_all = False
        for i in range(self._key_list_widget.count()):
            item = self._key_list_widget.item(i)
            if item.checkState() == Qt.Checked:
                if item.data(Qt.UserRole) == "__all__":
                    has_all = True
                elif item.data(Qt.UserRole):
                    key_ids.append(item.data(Qt.UserRole))
        return [] if has_all and not key_ids else key_ids

    def get_data(self) -> dict:
        return {
            "label": self._label_input.text().strip(),
            "allowed_models": self._get_selected_models(),
            "max_usage": self._max_usage_spin.value(),
            "rate_limit_rpm": self._rpm_spin.value(),
            "allowed_key_ids": self._get_selected_key_ids(),
            "key_mode": self._mode_combo.currentData(),
        }


class ImportFromAccountsDialog(QDialog):
    """从已获取账号导入到上游 Key 池对话框"""

    def __init__(self, parent=None, existing_api_keys=None):
        super().__init__(parent)
        self.setWindowTitle("从账号导入到 Key 池")
        self.setMinimumWidth(600)
        self.setMinimumHeight(400)
        self._selected_accounts = []
        self._existing_api_keys = existing_api_keys or set()  # 已导入的 api_key 集合
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        hint = QLabel("选择已有账号导入到上游 Key 池。只有含 API Key 的账号才可导入。已导入的会默认勾选。")
        hint.setStyleSheet("color: #718096; font-size: 12px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 账号表格
        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["选择", "昵称/手机号", "UID", "API状态"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self._table)

        # 加载账号数据
        self._load_accounts()

        # 按钮
        btn_row = QHBoxLayout()
        btn_select_all = QPushButton("全选")
        btn_select_all.setObjectName("secondary_btn")
        btn_select_all.setCursor(Qt.PointingHandCursor)
        btn_select_all.clicked.connect(self._select_all)
        btn_row.addWidget(btn_select_all)

        btn_deselect_all = QPushButton("取消全选")
        btn_deselect_all.setObjectName("secondary_btn")
        btn_deselect_all.setCursor(Qt.PointingHandCursor)
        btn_deselect_all.clicked.connect(self._deselect_all)
        btn_row.addWidget(btn_deselect_all)

        btn_row.addStretch()

        btn_import = QPushButton("📥 导入选中")
        btn_import.setObjectName("primary_btn")
        btn_import.setCursor(Qt.PointingHandCursor)
        btn_import.clicked.connect(self._do_import)
        btn_row.addWidget(btn_import)

        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        layout.addLayout(btn_row)

    def _load_accounts(self):
        """加载账号列表"""
        accounts = load_accounts()
        self._accounts = accounts  # 保留全部账号用于匹配
        self._importable_accounts = []  # 只有可导入的账号（有 API Key 的）

        for acc in accounts:
            import_key = acc.api_key if (acc.api_key and acc.api_key.startswith("ck_")) else acc.auth_token
            if import_key:
                self._importable_accounts.append(acc)

        self._table.setRowCount(len(self._importable_accounts))

        for row, acc in enumerate(self._importable_accounts):
            import_key = acc.api_key if (acc.api_key and acc.api_key.startswith("ck_")) else acc.auth_token
            is_already_imported = import_key in self._existing_api_keys

            # 复选框
            check = QCheckBox()
            if is_already_imported:
                check.setChecked(True)  # 已导入的默认勾选
            check_widget = QWidget()
            check_layout = QHBoxLayout(check_widget)
            check_layout.addWidget(check)
            check_layout.setAlignment(Qt.AlignCenter)
            check_layout.setContentsMargins(0, 0, 0, 0)
            self._table.setCellWidget(row, 0, check_widget)

            # 昵称
            name_item = QTableWidgetItem(acc.display_name or acc.uid)
            if is_already_imported:
                name_item.setForeground(Qt.gray)
            self._table.setItem(row, 1, name_item)

            # UID
            uid_item = QTableWidgetItem(acc.uid)
            if is_already_imported:
                uid_item.setForeground(Qt.gray)
            self._table.setItem(row, 2, uid_item)

            # API状态列
            status_parts = []
            if acc.api_key:
                api_preview = acc.api_key[:15] + "..." if len(acc.api_key) > 15 else acc.api_key
                status_parts.append(f"✅ API: {api_preview}")
            if acc.auth_token:
                tk_preview = acc.auth_token[:15] + "..." if len(acc.auth_token) > 15 else acc.auth_token
                status_parts.append(f"✅ TK: {tk_preview}")

            if status_parts:
                status_text = "\n".join(status_parts)
                status_item = QTableWidgetItem(status_text)
                status_item.setForeground(Qt.darkGreen)
            else:
                status_item = QTableWidgetItem("—")
                status_item.setForeground(Qt.gray)
            self._table.setItem(row, 3, status_item)

    def _select_all(self):
        for row in range(self._table.rowCount()):
            check_widget = self._table.cellWidget(row, 0)
            if check_widget:
                check = check_widget.findChild(QCheckBox)
                if check:
                    check.setChecked(True)

    def _deselect_all(self):
        for row in range(self._table.rowCount()):
            check_widget = self._table.cellWidget(row, 0)
            if check_widget:
                check = check_widget.findChild(QCheckBox)
                if check:
                    check.setChecked(False)

    def _do_import(self):
        """确认导入"""
        self.accept()

    def get_selected_accounts(self) -> list:
        """获取选中的账号列表"""
        selected = []
        for row in range(self._table.rowCount()):
            check_widget = self._table.cellWidget(row, 0)
            if check_widget:
                check = check_widget.findChild(QCheckBox)
                if check and check.isChecked() and row < len(self._importable_accounts):
                    selected.append(self._importable_accounts[row])
        return selected


class ApiProxyPage(QWidget):
    """API 代理服务页面"""

    quota_updated = Signal()  # 积分更新信号，通知其他页面刷新

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content_area")
        self._proxy_server: ProxyServer = None
        self._db = ProxyDatabase.get_instance()
        self._keys_sort_column = None
        self._keys_sort_order = Qt.AscendingOrder
        self._subkeys_sort_column = None
        self._subkeys_sort_order = Qt.AscendingOrder
        self._setup_ui()

        # 日志自动刷新定时器
        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._refresh_log)
        self._last_log_timestamp = 0.0  # 跟踪上次最新日志时间戳，避免无变化时重复刷新
        self._log_cleared_since = 0.0   # 清空日志时间戳，只显示此时间之后的日志

        # 上游Key/子Key表格定时刷新（每10秒，用于实时积分变化）
        self._table_timer = QTimer(self)
        self._table_timer.timeout.connect(self._refresh_tables_if_visible)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel(t("api_proxy.title"))
        title.setObjectName("page_title")
        layout.addWidget(title)

        subtitle = QLabel("本地 API 中转服务 · OpenAI 兼容接口 · 支持多模型转发")
        subtitle.setObjectName("page_subtitle")
        layout.addWidget(subtitle)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(32, 0, 32, 32)
        content_layout.setSpacing(16)

        # ─── 服务控制区 ───
        control_card = QFrame()
        control_card.setObjectName("card")
        control_layout = QVBoxLayout(control_card)
        control_layout.setSpacing(10)

        # 端口和访问控制
        config_row = QHBoxLayout()
        config_row.addWidget(QLabel("端口:"))
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1024, 65535)
        self._port_spin.setValue(int(load_setting("proxy_port", "8002")))
        config_row.addWidget(self._port_spin)

        config_row.addWidget(QLabel("  "))
        self._listen_mode_combo = QComboBox()
        self._listen_mode_combo.addItem("🔒 本地模式", "local")
        self._listen_mode_combo.addItem("🌐 开放模式", "open")
        self._listen_mode_combo.setCurrentIndex(0)
        self._listen_mode_combo.currentIndexChanged.connect(self._on_listen_mode_changed)
        config_row.addWidget(self._listen_mode_combo)

        config_row.addWidget(QLabel("    "))
        config_row.addWidget(QLabel("最低积分:"))
        self._min_credits_spin = QSpinBox()
        self._min_credits_spin.setRange(0, 100000)
        self._min_credits_spin.setValue(int(load_setting("min_credits_threshold", "0")))
        self._min_credits_spin.setSuffix(" 分")
        self._min_credits_spin.setMinimumWidth(100)
        self._min_credits_spin.setToolTip("低于此积分自动禁用 Key（0=不限制）")
        config_row.addWidget(self._min_credits_spin)

        config_row.addWidget(QLabel("  "))
        config_row.addWidget(QLabel("自动启用:"))
        self._auto_enable_spin = QSpinBox()
        self._auto_enable_spin.setRange(0, 100000)
        self._auto_enable_spin.setValue(int(load_setting("auto_enable_threshold", "100")))
        self._auto_enable_spin.setSuffix(" 分")
        self._auto_enable_spin.setMinimumWidth(100)
        self._auto_enable_spin.setToolTip("查分高于此值自动恢复禁用的 Key")
        config_row.addWidget(self._auto_enable_spin)

        config_row.addWidget(QLabel("  "))
        config_row.addWidget(QLabel("限流冷却:"))
        self._cooldown_spin = QSpinBox()
        self._cooldown_spin.setRange(1, 3600)
        self._cooldown_spin.setValue(int(load_setting("cooldown_seconds", "10")))
        self._cooldown_spin.setSuffix(" 秒")
        self._cooldown_spin.setMinimumWidth(90)
        self._cooldown_spin.setToolTip("Key 被限流(429)后冷却多少秒再自动恢复")
        config_row.addWidget(self._cooldown_spin)
        self._cooldown_spin.valueChanged.connect(
            lambda v: save_setting("cooldown_seconds", str(v))
        )

        # 积分阈值变更自动保存并同步
        self._min_credits_spin.valueChanged.connect(self._apply_thresholds_now)
        self._auto_enable_spin.valueChanged.connect(self._apply_thresholds_now)

        config_row.addStretch()

        control_layout.addLayout(config_row)

        # 服务 URL 显示
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("接口地址:"))
        self._url_label = QLabel("http://127.0.0.1:8002/v1")
        self._url_label.setStyleSheet("color: #2B6CB0; font-weight: 600; font-size: 13px;")
        self._url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        url_row.addWidget(self._url_label)

        btn_copy_url = QPushButton("📋 复制")
        btn_copy_url.setObjectName("secondary_btn")
        btn_copy_url.setCursor(Qt.PointingHandCursor)
        btn_copy_url.setFixedWidth(60)
        btn_copy_url.clicked.connect(self._copy_url)
        url_row.addWidget(btn_copy_url)

        url_row.addStretch()

        self._status_label = QLabel("⏹ 已停止")
        self._status_label.setStyleSheet("font-weight: 600; color: #9BA4B0;")
        url_row.addWidget(self._status_label)

        self._toggle_btn = QPushButton("▶ 启动服务")
        self._toggle_btn.setObjectName("primary_btn")
        self._toggle_btn.setCursor(Qt.PointingHandCursor)
        self._toggle_btn.clicked.connect(self._toggle_service)
        url_row.addWidget(self._toggle_btn)

        control_layout.addLayout(url_row)

        # 开放模式提示（默认隐藏）
        self._open_mode_hint = QLabel("")
        self._open_mode_hint.setStyleSheet("color: #FC8181; font-size: 12px; padding: 4px 8px; background: rgba(229,62,62,0.1); border-radius: 4px;")
        self._open_mode_hint.setWordWrap(True)
        self._open_mode_hint.setVisible(False)
        control_layout.addWidget(self._open_mode_hint)

        # 注意：上游代理地址已加密隐藏，不在UI显示
        # 管理密码已移除，不需要管理员后台

        content_layout.addWidget(control_card)

        # ─── Tab 区：上游 Key 池 / 子 API Keys / 使用日志 ───
        self._tab_widget = QTabWidget()

        # === Tab 1: 上游 Key 池 ===
        keys_tab = QWidget()
        keys_layout = QVBoxLayout(keys_tab)
        keys_layout.setSpacing(10)

        # 统计行
        stats_row = QHBoxLayout()
        self._stat_total = QLabel("📋 总 Key: 0")
        self._stat_total.setStyleSheet("font-size: 13px; font-weight: 600;")
        stats_row.addWidget(self._stat_total)
        self._stat_active = QLabel("✅ 活跃: 0")
        self._stat_active.setStyleSheet("font-size: 13px; font-weight: 600; color: #38A169;")
        stats_row.addWidget(self._stat_active)
        self._stat_exhausted = QLabel("❌ 耗尽: 0")
        self._stat_exhausted.setStyleSheet("font-size: 13px; font-weight: 600; color: #E53E3E;")
        stats_row.addWidget(self._stat_exhausted)
        self._stat_abnormal = QLabel("⚠️ 异常: 0")
        self._stat_abnormal.setStyleSheet("font-size: 13px; font-weight: 600; color: #DD6B20;")
        stats_row.addWidget(self._stat_abnormal)
        self._stat_total_used = QLabel("📊 总调用: 0")
        self._stat_total_used.setStyleSheet("font-size: 13px; font-weight: 600; color: #805AD5;")
        stats_row.addWidget(self._stat_total_used)
        stats_row.addStretch()

        btn_refresh_keys = QPushButton("🔄 刷新")
        btn_refresh_keys.setObjectName("secondary_btn")
        btn_refresh_keys.setCursor(Qt.PointingHandCursor)
        btn_refresh_keys.clicked.connect(self._refresh_upstream_keys)
        stats_row.addWidget(btn_refresh_keys)

        keys_layout.addLayout(stats_row)

        # 工具栏 — 从账号导入 + 刷新积分 + 批量操作
        keys_toolbar = QHBoxLayout()
        keys_toolbar.setSpacing(8)
        keys_toolbar.setContentsMargins(0, 0, 0, 0)
        btn_import_from_accounts = QPushButton("📥 导入")
        btn_import_from_accounts.setObjectName("primary_btn")
        btn_import_from_accounts.setCursor(Qt.PointingHandCursor)
        btn_import_from_accounts.setToolTip("从已获取的账号中导入 Token/API Key 到上游 Key 池")
        btn_import_from_accounts.clicked.connect(self._import_from_accounts)
        keys_toolbar.addWidget(btn_import_from_accounts)

        btn_refresh_points = QPushButton("🔄 积分")
        btn_refresh_points.setObjectName("secondary_btn")
        btn_refresh_points.setCursor(Qt.PointingHandCursor)
        btn_refresh_points.setToolTip("查询所有关联账号的积分并同步到 Key 池")
        btn_refresh_points.clicked.connect(self._refresh_all_points)
        keys_toolbar.addWidget(btn_refresh_points)

        btn_check_status = QPushButton("🔍 检测")
        btn_check_status.setObjectName("secondary_btn")
        btn_check_status.setCursor(Qt.PointingHandCursor)
        btn_check_status.setToolTip("批量检测所有上游 Key 是否被风控（403），异常的自动标记")
        btn_check_status.clicked.connect(self._check_all_key_status)
        keys_toolbar.addWidget(btn_check_status)

        # 批量操作
        btn_batch_perm_disable = QPushButton("🚫 永禁")
        btn_batch_perm_disable.setObjectName("danger_btn")
        btn_batch_perm_disable.setCursor(Qt.PointingHandCursor)
        btn_batch_perm_disable.setToolTip("按积分范围批量永久禁用 Key（不会自动恢复）")
        btn_batch_perm_disable.clicked.connect(lambda: self._open_batch_status_dialog(enable=False, permanent=True))
        keys_toolbar.addWidget(btn_batch_perm_disable)

        btn_batch_temp_disable = QPushButton("禁用")
        btn_batch_temp_disable.setObjectName("secondary_btn")
        btn_batch_temp_disable.setCursor(Qt.PointingHandCursor)
        btn_batch_temp_disable.setToolTip("按积分范围批量临时禁用 Key（查分后可自动恢复）")
        btn_batch_temp_disable.clicked.connect(lambda: self._open_batch_status_dialog(enable=False, permanent=False))
        keys_toolbar.addWidget(btn_batch_temp_disable)

        btn_batch_enable = QPushButton("✅ 解禁")
        btn_batch_enable.setObjectName("secondary_btn")
        btn_batch_enable.setCursor(Qt.PointingHandCursor)
        btn_batch_enable.setToolTip("按积分范围批量恢复 Key 为可用")
        btn_batch_enable.clicked.connect(lambda: self._open_batch_status_dialog(enable=True))
        keys_toolbar.addWidget(btn_batch_enable)

        # 当天/总计切换
        self._keys_today_only = False
        self._chk_keys_today = QPushButton("📅 当天")
        self._chk_keys_today.setObjectName("secondary_btn")
        self._chk_keys_today.setCheckable(True)
        self._chk_keys_today.setCursor(Qt.PointingHandCursor)
        self._chk_keys_today.setToolTip("开启后只显示当天统计，关闭显示总计")
        self._chk_keys_today.clicked.connect(self._toggle_keys_today)
        keys_toolbar.addWidget(self._chk_keys_today)

        self._keys_search_input = QLineEdit()
        self._keys_search_input.setPlaceholderText("🔍 搜索上游 Key...")
        self._keys_search_input.textChanged.connect(lambda _text: self._refresh_upstream_keys())
        keys_toolbar.addWidget(self._keys_search_input)

        keys_toolbar.addStretch()
        keys_layout.addLayout(keys_toolbar)

        # Key 表格
        self._keys_table = QTableWidget()
        self._keys_table.setColumnCount(9)
        self._keys_table.setHorizontalHeaderLabels([
            "Key ID", "标签", "状态", "调用次数", "积分", "Token", "积分消耗", "缓存命中", "操作"
        ])
        keys_header = self._keys_table.horizontalHeader()
        keys_header.setSectionResizeMode(QHeaderView.Stretch)
        keys_header.setSectionsClickable(True)
        keys_header.setSortIndicatorShown(True)
        keys_header.sectionClicked.connect(self._on_keys_header_sort)
        self._keys_table.setAlternatingRowColors(True)
        self._keys_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._keys_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._keys_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._keys_table.customContextMenuRequested.connect(self._on_keys_context_menu)
        keys_layout.addWidget(self._keys_table)

        self._tab_widget.addTab(keys_tab, "🔑 上游 Key 池")

        # === Tab 2: 子 API Keys ===
        subkeys_tab = QWidget()
        subkeys_layout = QVBoxLayout(subkeys_tab)
        subkeys_layout.setSpacing(10)

        # 统计行
        sk_stats_row = QHBoxLayout()
        self._sk_stat_total = QLabel("📋 总 Key: 0")
        self._sk_stat_total.setStyleSheet("font-size: 13px; font-weight: 600;")
        sk_stats_row.addWidget(self._sk_stat_total)
        self._sk_stat_active = QLabel("✅ 活跃: 0")
        self._sk_stat_active.setStyleSheet("font-size: 13px; font-weight: 600; color: #38A169;")
        sk_stats_row.addWidget(self._sk_stat_active)
        self._sk_stat_disabled = QLabel("🚫 禁用: 0")
        self._sk_stat_disabled.setStyleSheet("font-size: 13px; font-weight: 600; color: #D69E2E;")
        sk_stats_row.addWidget(self._sk_stat_disabled)
        self._sk_stat_total_used = QLabel("📊 总调用: 0")
        self._sk_stat_total_used.setStyleSheet("font-size: 13px; font-weight: 600; color: #805AD5;")
        sk_stats_row.addWidget(self._sk_stat_total_used)
        sk_stats_row.addStretch()
        subkeys_layout.addLayout(sk_stats_row)

        # 工具栏
        sk_toolbar = QHBoxLayout()
        btn_create_sk = QPushButton("➕ 创建子 Key")
        btn_create_sk.setObjectName("primary_btn")
        btn_create_sk.setCursor(Qt.PointingHandCursor)
        btn_create_sk.clicked.connect(self._create_sub_key)
        sk_toolbar.addWidget(btn_create_sk)

        btn_refresh_sk = QPushButton("🔄 刷新")
        btn_refresh_sk.setObjectName("secondary_btn")
        btn_refresh_sk.setCursor(Qt.PointingHandCursor)
        btn_refresh_sk.clicked.connect(self._refresh_sub_keys)
        sk_toolbar.addWidget(btn_refresh_sk)

        btn_del_wb = QPushButton("🗑️ 删除WB配置")
        btn_del_wb.setObjectName("secondary_btn")
        btn_del_wb.setCursor(Qt.PointingHandCursor)
        btn_del_wb.setToolTip("删除 WorkBuddy 的 models.json 配置文件")
        btn_del_wb.clicked.connect(self._delete_workbuddy_config)
        sk_toolbar.addWidget(btn_del_wb)

        btn_del_cb = QPushButton("🗑️ 删除CB配置")
        btn_del_cb.setObjectName("secondary_btn")
        btn_del_cb.setCursor(Qt.PointingHandCursor)
        btn_del_cb.setToolTip("删除 CodeBuddy 的 models.json 配置文件")
        btn_del_cb.clicked.connect(self._delete_codebuddy_config)
        sk_toolbar.addWidget(btn_del_cb)

        # 当天/总计切换
        self._subkeys_today_only = False
        self._chk_subkeys_today = QPushButton("📅 当天")
        self._chk_subkeys_today.setObjectName("secondary_btn")
        self._chk_subkeys_today.setCheckable(True)
        self._chk_subkeys_today.setCursor(Qt.PointingHandCursor)
        self._chk_subkeys_today.setToolTip("开启后只显示当天统计，关闭显示总计")
        self._chk_subkeys_today.clicked.connect(self._toggle_subkeys_today)
        sk_toolbar.addWidget(self._chk_subkeys_today)

        sk_toolbar.addStretch()
        subkeys_layout.addLayout(sk_toolbar)

        # 子 Key 表格
        self._subkeys_table = QTableWidget()
        self._subkeys_table.setColumnCount(12)
        self._subkeys_table.setHorizontalHeaderLabels([
            "API Key", "标签", "状态", "模型限制", "已用/上限", "总积分", "调用模式", "RPM", "Token", "积分消耗", "缓存命中", "操作"
        ])
        subkeys_header = self._subkeys_table.horizontalHeader()
        subkeys_header.setSectionResizeMode(QHeaderView.Stretch)
        subkeys_header.setSectionsClickable(True)
        subkeys_header.setSortIndicatorShown(True)
        subkeys_header.sectionClicked.connect(self._on_subkeys_header_sort)
        self._subkeys_table.setAlternatingRowColors(True)
        self._subkeys_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._subkeys_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._subkeys_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._subkeys_table.customContextMenuRequested.connect(self._on_subkeys_context_menu)
        self._subkeys_table.cellDoubleClicked.connect(self._on_subkey_double_clicked)
        subkeys_layout.addWidget(self._subkeys_table)

        self._tab_widget.addTab(subkeys_tab, "🗝️ 子 API Keys")

        # === Tab 3: 使用日志 ===
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

        content_layout.addWidget(self._tab_widget, 1)
        layout.addWidget(content)

    # === 服务控制 ===

    def _on_listen_mode_changed(self, index: int):
        """监听模式切换时更新提示和 URL"""
        mode = self._listen_mode_combo.currentData()  # "local" or "open"
        port = self._port_spin.value()

        if mode == "open":
            ips = self._get_local_ips()
            ip_list = "、".join(ips) if ips else "未检测到"
            self._open_mode_hint.setText(
                f"⚠️ 开放模式：所有能连到本机的用户都可访问。必须创建子Key并分发给用户，未携带子Key的请求将被拒绝。"
                f"\n本机IP: {ip_list}，其他用户连接地址: http://{ips[0] if ips else '本机IP'}:{port}/v1"
            )
            self._open_mode_hint.setVisible(True)
            self._url_label.setText(f"http://{ips[0] if ips else '0.0.0.0'}:{port}/v1")
        else:
            self._open_mode_hint.setVisible(False)
            self._url_label.setText(f"http://127.0.0.1:{port}/v1")

    @staticmethod
    def _get_local_ips() -> list:
        """获取本机所有非回环 IP 地址"""
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
        # 如果 hostname 解析不到，尝试 UDP trick
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

    def _toggle_service(self):
        """启动/停止代理服务"""
        if self._proxy_server and self._proxy_server.is_running:
            self._proxy_server.stop()
            self._proxy_server = None
            # 服务停止后恢复独立 db 实例（从文件重新加载）
            self._db = ProxyDatabase.get_instance()
            self._status_label.setText("⏹ 已停止")
            self._status_label.setStyleSheet("font-weight: 600; color: #9BA4B0;")
            self._toggle_btn.setText("▶ 启动服务")
            self._toggle_btn.setObjectName("primary_btn")
            self._port_spin.setEnabled(True)
            self._listen_mode_combo.setEnabled(True)
        else:
            port = self._port_spin.value()
            mode = self._listen_mode_combo.currentData()  # "local" or "open"
            host = "127.0.0.1" if mode == "local" else "0.0.0.0"
            self._cleanup_legacy_models_on_start(port, mode)

            self._proxy_server = ProxyServer(host=host, port=port, mode=mode)

            if self._proxy_server.start():
                # 关键：使用 ProxyServer 的 db 实例，确保日志读写共享同一内存
                # 否则页面自己的 _db 实例看不到服务端写入的日志
                self._db = self._proxy_server.db
                save_setting("proxy_port", str(port))
                # 保存积分阈值设置，并同步到所有上游 Key
                min_val = self._min_credits_spin.value()
                auto_val = self._auto_enable_spin.value()
                save_setting("min_credits_threshold", str(min_val))
                save_setting("auto_enable_threshold", str(auto_val))
                # 同步阈值到所有上游 Key
                for k in self._db.get_upstream_keys():
                    self._db.update_upstream_key(k["key_id"], {
                        "min_credits_threshold": float(min_val),
                        "auto_enable_threshold": float(auto_val),
                    })
                self._status_label.setText(f"▶ 运行中 :{port}")
                self._status_label.setStyleSheet("font-weight: 600; color: #38A169;")
                self._toggle_btn.setText("⏹ 停止服务")
                self._toggle_btn.setObjectName("danger_btn")
                # 更新 URL 显示：开放模式显示本机 IP
                if mode == "open":
                    ips = self._get_local_ips()
                    display_host = ips[0] if ips else "0.0.0.0"
                else:
                    display_host = "127.0.0.1"
                self._url_label.setText(f"http://{display_host}:{port}/v1")
                self._listen_mode_combo.setEnabled(False)
                self._port_spin.setEnabled(False)
            else:
                QMessageBox.warning(self, "启动失败", f"无法在端口 {port} 启动代理服务，可能端口已被占用")

    def _proxy_base_urls_for_port(self, port: int, mode: str) -> set[str]:
        hosts = {"127.0.0.1", "localhost", "0.0.0.0"}
        hosts.update(self._get_local_ips())
        return {_normal_url(f"http://{host}:{port}/v1") for host in hosts if host}

    def _cleanup_legacy_models_on_start(self, port: int, mode: str) -> int:
        """Normalize old generated WorkBuddy/CodeBuddy model entries before starting."""
        proxy_base_urls = self._proxy_base_urls_for_port(port, mode)
        targets = [
            (os.path.join(os.path.expanduser("~"), ".workbuddy", "models.json"), "array", "WorkBuddy"),
            (os.path.join(os.path.expanduser("~"), ".codebuddy", "models.json"), "object", "CodeBuddy"),
        ]
        cleaned_total = 0
        for target_path, wrapper, label in targets:
            try:
                cleaned = _cleanup_legacy_proxy_models(target_path, wrapper, proxy_base_urls)
                cleaned_total += cleaned
                if cleaned:
                    print(f"[models.json] {label} cleaned {cleaned} legacy model(s): {target_path}")
            except Exception as e:
                print(f"[models.json] {label} cleanup failed: {e}")
        return cleaned_total

    def _copy_url(self):
        """复制服务地址"""
        clipboard = QApplication.clipboard()
        clipboard.setText(self._url_label.text())

    # === 上游 Key 管理 ===

    def _sync_points_from_accounts(self, keys: list):
        """从账号已保存的积分数据回填到上游 Key 的 points 字段

        当上游 Key 的 points 为空时，查找匹配的账号（api_key 或 auth_token），
        用账号已保存的 quota 数据来填充 points，避免页面显示全部为"-"。
        """
        from ...utils.store import load_accounts
        try:
            accounts = load_accounts()
        except Exception:
            return

        # 构建 auth_token/api_key → (remaining, total) 的映射
        token_to_quota = {}
        for acc in accounts:
            if acc.quota and acc.quota.credits_total > 0:
                if acc.auth_token:
                    token_to_quota[acc.auth_token] = (
                        acc.quota.credits_remaining,
                        acc.quota.credits_total,
                    )
                if acc.api_key and acc.api_key not in token_to_quota:
                    token_to_quota[acc.api_key] = (
                        acc.quota.credits_remaining,
                        acc.quota.credits_total,
                    )

        # 回填空的 points
        updated = False
        for k in keys:
            if not k.get("points"):
                api_key = k.get("api_key", "")
                if api_key in token_to_quota:
                    remaining, total = token_to_quota[api_key]
                    k["points"] = f"{remaining:.0f}/{total:.0f}"
                    k["points_updated_at"] = "synced_from_account"
                    updated = True

        # 如果有更新，写回数据库
        if updated:
            try:
                with self._db._lock:
                    db_keys = self._db._data.setdefault("upstream_keys", [])
                    for dk in db_keys:
                        if not dk.get("points"):
                            api_key = dk.get("api_key", "")
                            if api_key in token_to_quota:
                                remaining, total = token_to_quota[api_key]
                                dk["points"] = f"{remaining:.0f}/{total:.0f}"
                                dk["points_updated_at"] = "synced_from_account"
                    self._db._dirty = True
                    self._db._save()
            except Exception:
                pass

    def _toggle_keys_today(self):
        """切换上游 Key 表格的当天/总计显示"""
        self._keys_today_only = self._chk_keys_today.isChecked()
        self._chk_keys_today.setText("📅 当天✓" if self._keys_today_only else "📅 当天")
        self._refresh_upstream_keys()

    def _toggle_subkeys_today(self):
        """切换子 Key 表格的当天/总计显示"""
        self._subkeys_today_only = self._chk_subkeys_today.isChecked()
        self._chk_subkeys_today.setText("📅 当天✓" if self._subkeys_today_only else "📅 当天")
        self._refresh_sub_keys()

    def _show_daily_detail(self, category: str, key_id: str, title: str):
        """显示每日消耗明细对话框"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QTableWidget, QHeaderView, QPushButton, QHBoxLayout
        daily = self._db.get_daily_stats(category, key_id)
        if not daily:
            QMessageBox.information(self, "明细", f"{title}\n\n暂无历史数据")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"📊 {title} - 每日消耗明细")
        dlg.setMinimumSize(600, 400)
        layout = QVBoxLayout(dlg)

        table = QTableWidget()
        table.setAlternatingRowColors(True)
        sorted_dates = sorted(daily.keys(), reverse=True)
        table.setRowCount(len(sorted_dates))
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["日期", "调用次数", "Token(输入+输出)", "积分消耗", "缓存命中", "缓存率"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setEditTriggers(QTableWidget.NoEditTriggers)

        for row, date in enumerate(sorted_dates):
            d = daily[date]
            prompt = d.get("prompt_tokens", 0)
            completion = d.get("completion_tokens", 0)
            total = d.get("total_tokens", 0)
            cached = d.get("cached_tokens", 0)
            credits = d.get("credits", 0.0)
            count = d.get("count", 0)
            cache_rate = f"{cached/total*100:.1f}%" if total > 0 else "-"

            _set_item(table, row, 0, date)
            _set_item(table, row, 1, f"{count}")
            _set_item(table, row, 2, f"{_fmt_tokens(prompt)}+{_fmt_tokens(completion)}",
                      tooltip=f"输入: {prompt:,}  输出: {completion:,}  总计: {total:,}")
            _set_item(table, row, 3, f"{credits:.2f}")
            _set_item(table, row, 4, _fmt_tokens(cached))
            _set_item(table, row, 5, cache_rate)

        layout.addWidget(table)

        btn_close = QPushButton("关闭")
        btn_close.setObjectName("secondary_btn")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(dlg.accept)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

        dlg.exec()

    @staticmethod
    def _points_remaining(points) -> float:
        try:
            text = str(points or "")
            if "/" in text:
                return float(text.split("/", 1)[0])
            return float(text)
        except (TypeError, ValueError):
            return -1.0

    def _key_sort_value(self, key: dict, column: int):
        if column == 0:
            return key.get("key_id", "").lower()
        if column == 1:
            return key.get("label", "").lower()
        if column == 2:
            return key.get("status", "active")
        if column == 3:
            return key.get("used_count", 0)
        if column == 4:
            return self._points_remaining(key.get("points", ""))
        if column == 5:
            return key.get("total_tokens", 0)
        if column == 6:
            return key.get("total_credits", 0.0)
        if column == 7:
            total_t = key.get("total_tokens", 0)
            cached = key.get("total_cached_tokens", 0)
            return cached / total_t if total_t else 0
        return ""

    def _subkey_sort_value(self, sub_key: dict, column: int):
        allowed_key_ids = sub_key.get("allowed_key_ids", [])
        if column == 0:
            return sub_key.get("api_key", "").lower()
        if column == 1:
            return sub_key.get("label", "").lower()
        if column == 2:
            return 0 if sub_key.get("is_active", True) else 1
        if column == 3:
            return ",".join(sub_key.get("allowed_models", []))
        if column == 4:
            return sub_key.get("used_count", 0)
        if column == 5:
            return self._db.get_total_points_for_sub_key(allowed_key_ids if allowed_key_ids else None)
        if column == 6:
            return sub_key.get("key_mode", 1)
        if column == 7:
            return sub_key.get("rate_limit_rpm", 0)
        if column == 8:
            return sub_key.get("total_tokens", 0)
        if column == 9:
            return sub_key.get("total_credits", 0.0)
        if column == 10:
            total_t = sub_key.get("total_tokens", 0)
            cached = sub_key.get("total_cached_tokens", 0)
            return cached / total_t if total_t else 0
        return ""

    def _on_keys_header_sort(self, section: int):
        if section >= self._keys_table.columnCount() - 1:
            return
        if self._keys_sort_column == section:
            self._keys_sort_order = Qt.DescendingOrder if self._keys_sort_order == Qt.AscendingOrder else Qt.AscendingOrder
        else:
            self._keys_sort_column = section
            self._keys_sort_order = Qt.AscendingOrder
        self._keys_table.horizontalHeader().setSortIndicator(section, self._keys_sort_order)
        self._refresh_upstream_keys()

    def _on_subkeys_header_sort(self, section: int):
        if section >= self._subkeys_table.columnCount() - 1:
            return
        if self._subkeys_sort_column == section:
            self._subkeys_sort_order = Qt.DescendingOrder if self._subkeys_sort_order == Qt.AscendingOrder else Qt.AscendingOrder
        else:
            self._subkeys_sort_column = section
            self._subkeys_sort_order = Qt.AscendingOrder
        self._subkeys_table.horizontalHeader().setSortIndicator(section, self._subkeys_sort_order)
        self._refresh_sub_keys()

    def _refresh_upstream_keys(self, reload_from_disk=False):
        """刷新上游 Key 列表

        Args:
            reload_from_disk: 是否从磁盘重新加载数据（账号页面查分后通过信号触发时需要，
                              因为账号页面用的是独立的 ProxyDatabase 实例写盘，
                              本页面的 db 实例内存可能还是旧数据）
        """
        if reload_from_disk:
            # 服务运行中时，使用服务的 db 实例（共享内存），需要从文件重载
            # 服务停止时，_db 是独立实例，也需要从文件重载
            # 关键修复：reload 前先 flush，确保延迟写盘的数据落盘，避免被旧数据覆盖
            self._db._flush_to_disk()
            self._db._data = self._db._load()
            self._db._dirty = False
            # 关键：刷新 router 缓存，让 select_key 立即用新数据
            if self._proxy_server and self._proxy_server.is_running:
                self._proxy_server.router._upstream_keys_cache_time = 0

        # 获取当前正在使用的 Key（有并发请求的 Key）
        concurrent_keys = {}
        if self._proxy_server and self._proxy_server.is_running:
            try:
                concurrent_keys = self._proxy_server.router.get_concurrent_keys()
            except Exception:
                pass

        keys = self._db.get_upstream_keys()

        # 自动回填：对 points 为空的上游 Key，从账号已保存的积分数据中填充
        self._sync_points_from_accounts(keys)

        search = self._keys_search_input.text().strip().lower() if hasattr(self, "_keys_search_input") else ""
        if search:
            keys = [
                k for k in keys
                if search in k.get("key_id", "").lower()
                or search in k.get("label", "").lower()
                or search in k.get("status", "").lower()
                or search in k.get("api_key", "").lower()
                or search in str(k.get("points", "")).lower()
            ]

        if self._keys_sort_column is None:
            # 默认排序：最近使用优先
            keys.sort(key=lambda k: k.get("last_used_at", ""), reverse=True)
        else:
            keys.sort(
                key=lambda k: self._key_sort_value(k, self._keys_sort_column),
                reverse=self._keys_sort_order == Qt.DescendingOrder,
            )

        # 使用中的 Key 永远置顶；稳定排序会保留组内原有顺序。
        if concurrent_keys:
            keys.sort(key=lambda k: 0 if k.get("key_id", "") in concurrent_keys else 1)

        self._keys_table.setRowCount(len(keys))

        active = 0
        exhausted = 0
        abnormal = 0
        total_used = 0

        for row, k in enumerate(keys):
            key_id = k.get("key_id", "")
            label = k.get("label", "")
            status = k.get("status", "active")

            # 统计数据（当天或总计）— used 也必须跟随切换，否则"调用次数"列永远显示总计
            if self._keys_today_only:
                today = self._db.get_today_stats("upstream", key_id)
                used = today.get("count", 0)
                total_prompt = today.get("prompt_tokens", 0)
                total_completion = today.get("completion_tokens", 0)
                total_t = today.get("total_tokens", 0)
                total_cached = today.get("cached_tokens", 0)
                total_credits = today.get("credits", 0.0)
            else:
                used = k.get("used_count", 0)
                total_prompt = k.get("total_prompt_tokens", 0)
                total_completion = k.get("total_completion_tokens", 0)
                total_t = k.get("total_tokens", 0)
                total_cached = k.get("total_cached_tokens", 0)
                total_credits = k.get("total_credits", 0.0)

            points = k.get("points", "-")

            if status == "active":
                active += 1
            elif status == "exhausted":
                exhausted += 1
            elif status == "abnormal":
                abnormal += 1
            total_used += used

            _set_item(self._keys_table, row, 0, key_id, tooltip=f"Key ID: {key_id}")
            _set_item(self._keys_table, row, 1, label or "-", tooltip=label or "无标签")

            status_map = {
                "active": "✅ 活跃", "exhausted": "❌ 已耗尽",
                "disabled": "🚫 已禁用", "rate_limited": "⚠️ 限流中", "cooldown": "🧊 冷却中",
                "abnormal": "⚠️ 异常", "permanent_disabled": "⛔ 永久禁用",
            }
            status_text = status_map.get(status, status)
            # 正在使用的 Key 在状态文字后加并发数标记
            if key_id in concurrent_keys:
                concurrent_count = concurrent_keys[key_id]
                status_text = f"🟢 使用中({concurrent_count})"
            status_item = _set_item(self._keys_table, row, 2, status_text, tooltip=f"状态: {status}" + (f"，并发: {concurrent_count}" if key_id in concurrent_keys else ""))
            if key_id in concurrent_keys:
                # 正在使用的 Key 整行绿色背景标记
                green_bg = QBrush(QColor(200, 255, 200))  # 浅绿色
                for col in range(self._keys_table.columnCount()):
                    item = self._keys_table.item(row, col)
                    if item:
                        item.setBackground(green_bg)
            elif status == "active":
                status_item.setForeground(Qt.darkGreen)
            elif status == "exhausted":
                status_item.setForeground(Qt.red)
            elif status == "abnormal":
                status_item.setForeground(QColor("#E53E3E"))

            _set_item(self._keys_table, row, 3, str(used), tooltip=f"调用次数: {used:,}")

            # 积分列 — 格式 "剩余/总量"，根据剩余比例着色
            points_str = str(points) if points else "-"
            points_item = _set_item(self._keys_table, row, 4, points_str, tooltip=f"积分: {points_str}")
            if points and "/" in str(points):
                try:
                    remain_str, total_str = str(points).split("/")
                    remain_f = float(remain_str)
                    total_f = float(total_str)
                    if total_f > 0:
                        pct = remain_f / total_f * 100
                        if pct <= 0:
                            points_item.setForeground(Qt.red)
                        elif pct < 20:
                            points_item.setForeground(QColor("#D69E2E"))
                        else:
                            points_item.setForeground(Qt.darkGreen)
                except (ValueError, IndexError):
                    pass

            # Token 统计（智能单位转换）
            if total_t > 0:
                token_display = f"{_fmt_tokens(total_prompt)}+{_fmt_tokens(total_completion)}"
                token_tip = f"输入: {total_prompt:,}  输出: {total_completion:,}  总计: {total_t:,}"
            else:
                token_display = "-"
                token_tip = "暂无数据"
            _set_item(self._keys_table, row, 5, token_display, tooltip=token_tip)

            # 积分消耗
            if total_credits > 0:
                credit_display = f"{total_credits:.2f}"
                credit_tip = f"累计积分消耗: {total_credits:.4f}"
            else:
                credit_display = "-"
                credit_tip = "暂无数据"
            _set_item(self._keys_table, row, 6, credit_display, tooltip=credit_tip)

            # 缓存命中率
            if total_t > 0 and total_cached > 0:
                cache_rate = total_cached / total_t * 100
                cache_text = f"{cache_rate:.1f}%"
                cache_tip = f"缓存命中: {total_cached:,} / 总计: {total_t:,} = {cache_rate:.1f}%"
            else:
                cache_text = "-"
                cache_tip = "暂无数据"
            _set_item(self._keys_table, row, 7, cache_text, tooltip=cache_tip)

            # 操作栏：一个按钮触发下拉菜单
            ops_widget = QWidget()
            ops_widget.setAttribute(Qt.WA_TranslucentBackground, True)
            ops_layout = QHBoxLayout(ops_widget)
            ops_layout.setContentsMargins(4, 0, 4, 0)
            ops_layout.setSpacing(0)

            from PySide6.QtWidgets import QToolButton
            btn_ops = QToolButton()
            btn_ops.setObjectName("ops_btn")
            btn_ops.setText("操作 ▾")
            btn_ops.setCursor(Qt.PointingHandCursor)
            btn_ops.setToolButtonStyle(Qt.ToolButtonTextOnly)
            btn_ops.setPopupMode(QToolButton.InstantPopup)

            from PySide6.QtWidgets import QMenu
            ops_menu = QMenu(btn_ops)
            _style_popup_menu(ops_menu)
            if status != "active":
                act = ops_menu.addAction("✅ 恢复")
                act.triggered.connect(lambda checked, kid=key_id: self._reset_key(kid))
            else:
                act = ops_menu.addAction("🚫 禁用")
                act.triggered.connect(lambda checked, kid=key_id: self._disable_key(kid))
            if status != "permanent_disabled":
                act = ops_menu.addAction("⛔ 永久禁用")
                act.triggered.connect(lambda checked, kid=key_id: self._permanent_disable_key(kid))
            ops_menu.addSeparator()
            act = ops_menu.addAction("📊 明细")
            act.triggered.connect(lambda checked, kid=key_id, lbl=label: self._show_daily_detail("upstream", kid, f"上游Key {lbl or kid}"))
            ops_menu.addSeparator()
            act = ops_menu.addAction("🗑️ 删除")
            act.triggered.connect(lambda checked, kid=key_id: self._delete_upstream_key(kid))

            btn_ops.setMenu(ops_menu)
            ops_layout.addWidget(btn_ops)
            ops_layout.addStretch()
            self._keys_table.setCellWidget(row, 8, ops_widget)

        # 更新统计
        self._stat_total.setText(f"📋 总 Key: {len(keys)}")
        self._stat_active.setText(f"✅ 活跃: {active}")
        self._stat_exhausted.setText(f"❌ 耗尽: {exhausted}")
        self._stat_abnormal.setText(f"⚠️ 异常: {abnormal}")
        self._stat_total_used.setText(f"📊 {'今日调用' if self._keys_today_only else '总调用'}: {total_used}")

    def _import_from_accounts(self):
        """从已获取账号导入到上游 Key 池"""
        existing_keys = self._db.get_upstream_keys()
        existing_api_keys = {k.get("api_key", "") for k in existing_keys}
        dialog = ImportFromAccountsDialog(self, existing_api_keys=existing_api_keys)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            accounts = dialog.get_selected_accounts()
            if not accounts:
                QMessageBox.warning(self, "提示", "请选择要导入的账号")
                return

            count = 0
            for acc in accounts:
                # 优先用 API Key (ck_xxx)，其次用 auth_token
                import_key = acc.api_key if (acc.api_key and acc.api_key.startswith("ck_")) else acc.auth_token
                if not import_key:
                    continue

                # 检查是否已存在（避免重复导入）
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

            self._refresh_upstream_keys()
            if count > 0:
                QMessageBox.information(self, "导入完成", f"成功导入 {count} 个 Key 到上游 Key 池")
            else:
                QMessageBox.information(self, "导入完成", "没有新的 Key 需要导入（可能已存在）")

    def _apply_thresholds_now(self):
        """防抖：spinbox 值变时 500ms 后才真正执行"""
        if not hasattr(self, '_threshold_debounce_timer'):
            self._threshold_debounce_timer = QTimer()
            self._threshold_debounce_timer.setSingleShot(True)
            self._threshold_debounce_timer.timeout.connect(self._do_apply_thresholds)
        self._threshold_debounce_timer.start(500)

    def _do_apply_thresholds(self):
        """按当前阈值立即更新所有 Key 状态（不查分，按存的 points 走规则）"""
        min_val = self._min_credits_spin.value()
        auto_val = self._auto_enable_spin.value()

        # 1. 阈值持久化到全局 + 每个 Key
        save_setting("min_credits_threshold", str(min_val))
        save_setting("auto_enable_threshold", str(auto_val))
        keys = self._db.get_upstream_keys()
        for k in keys:
            self._db.update_upstream_key(k["key_id"], {
                "min_credits_threshold": float(min_val),
                "auto_enable_threshold": float(auto_val),
            })

        # 2. 按存的 points 跑规则
        disabled_cnt = 0
        enabled_cnt = 0
        skipped_no_points = 0
        for k in keys:
            status = k.get("status", "active")
            if status in ("abnormal", "permanent_disabled"):
                continue  # 不动风控和永久禁用
            pts_str = k.get("points", "")
            pts = ApiProxyPage._points_remaining(pts_str)
            if pts < 0:
                skipped_no_points += 1
                continue

            # 积分 <= min → 禁用（只覆盖 active/cooldown/rate_limited/exhausted）
            if pts <= min_val and status in ("active", "cooldown", "rate_limited", "exhausted"):
                self._db.update_upstream_key(k["key_id"], {"status": "disabled"})
                disabled_cnt += 1
            # 积分 > auto → 恢复 active（不动 abnormal/permanent_disabled）
            elif pts > auto_val and status == "disabled":
                self._db.update_upstream_key(k["key_id"], {"status": "active"})
                enabled_cnt += 1

        self._refresh_upstream_keys()
        msg = (
            f"已按阈值同步（min={min_val}, auto={auto_val}）：\n"
            f"  - 禁用 {disabled_cnt} 个\n"
            f"  - 启用 {enabled_cnt} 个"
        )
        if skipped_no_points:
            msg += f"\n  - 跳过 {skipped_no_points} 个（无积分数据，需先刷新积分）"
        QMessageBox.information(self, "按阈值同步", msg)

    def _refresh_all_points(self):
        """主动查询所有上游 Key 对应账号的积分并同步（优先用 API Key 直接查分）"""
        from ...modules.api_client import ApiClient
        from PySide6.QtCore import QThread, Signal as QSignal
        from concurrent.futures import ThreadPoolExecutor, as_completed

        keys = self._db.get_upstream_keys()
        if not keys:
            QMessageBox.information(self, "提示", "上游 Key 池为空，无需查询")
            return

        # 收集需要查询的 Key（每个上游 Key 直接用它的 api_key 查分）
        keys_to_query = []
        for k in keys:
            api_key = k.get("api_key", "")
            if api_key:
                keys_to_query.append(k)

        if not keys_to_query:
            QMessageBox.information(self, "提示", "未找到可查询的 Key")
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
        self._points_worker = PointsRefreshWorker(keys_to_query, self._db, max_workers=max_workers)
        self._points_worker.progress.connect(
            lambda msg: self._stat_total.setText(f"⏳ {msg}")
        )
        self._points_worker.done.connect(self._on_points_refresh_done)
        self._points_worker.start()

    def _on_points_refresh_done(self, success: int, failed: int):
        """积分刷新完成回调"""
        self._refresh_upstream_keys()
        self._refresh_sub_keys()
        msg = f"积分刷新完成：✅ {success} 个成功"
        if failed > 0:
            msg += f"，❌ {failed} 个失败"
        self._stat_total.setText(f"📋 {msg}")
        self.quota_updated.emit()  # 通知其他页面刷新

    def _check_all_key_status(self):
        """一键检测所有上游 Key 是否被风控（403 code:11140）"""
        from PySide6.QtCore import QThread, Signal as QSignal
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ...modules.api_client import check_api_key_chat_status

        keys = self._db.get_upstream_keys()
        if not keys:
            QMessageBox.information(self, "提示", "上游 Key 池为空，无需检测")
            return

        # 只检测 active / cooldown 状态的 Key（exhausted/disabled/abnormal 跳过）
        keys_to_check = []
        for k in keys:
            status = k.get("status", "active")
            api_key = k.get("api_key", "")
            if api_key and status in ("active", "cooldown", "rate_limited"):
                keys_to_check.append(k)

        if not keys_to_check:
            QMessageBox.information(self, "提示", "没有需要检测的 Key（活跃状态的 Key 为空）")
            return

        class KeyStatusCheckWorker(QThread):
            """Background worker for checking upstream Key status."""
            progress = QSignal(str)
            done = QSignal(int, int, int)  # normal, abnormal, failed

            def __init__(self, keys, db, max_workers=5):
                super().__init__()
                self._keys = keys
                self._db = db
                self._stop_flag = False
                self.max_workers = max_workers

            def stop(self):
                self._stop_flag = True

            def _check_one(self, k):
                api_key = k.get("api_key", "")
                label = k.get("label", api_key[:12])
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
                        if self._stop_flag:
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        try:
                            k, label, result = future.result()
                            key_id = k.get("key_id", "")
                            status_text = result.get("status_text", "check_failed")
                            self.progress.emit(f"{label}: {status_text}")
                            if result.get("flag") == "abnormal":
                                self._db.update_upstream_key(key_id, {"status": "abnormal"})
                                abnormal += 1
                            elif result.get("flag") == "rate_limited":
                                self._db.update_upstream_key(key_id, {"status": "rate_limited"})
                                abnormal += 1
                            elif result.get("success"):
                                # 检测通过：如果之前是 abnormal/rate_limited，恢复为 active
                                old_status = k.get("status", "active")
                                if old_status in ("abnormal", "rate_limited"):
                                    self._db.update_upstream_key(key_id, {"status": "active"})
                                normal += 1
                            else:
                                failed += 1
                        except Exception as e:
                            self.progress.emit(f"检测失败: {e}")
                            failed += 1
                self.done.emit(normal, abnormal, failed)

        max_workers = _get_account_concurrency_setting()
        self._status_check_worker = KeyStatusCheckWorker(keys_to_check, self._db, max_workers=max_workers)
        self._status_check_worker.progress.connect(
            lambda msg: self._stat_total.setText(f"🔍 {msg}")
        )
        self._status_check_worker.done.connect(self._on_status_check_done)
        self._status_check_worker.start()

    def _on_status_check_done(self, normal: int, abnormal: int, failed: int):
        """账号状态检测完成回调"""
        self._refresh_upstream_keys()
        msg = f"检测完成：✅ 正常 {normal} 个"
        if abnormal > 0:
            msg += f"，⚠️ 异常 {abnormal} 个（已自动标记）"
        if failed > 0:
            msg += f"，❓ 失败 {failed} 个"
        self._stat_total.setText(f"📋 {msg}")
        if abnormal > 0:
            QMessageBox.warning(
                self, "检测完成",
                f"发现 {abnormal} 个 Key 被风控（403），已自动标记为异常状态。\n"
                f"异常状态的 Key 不会再被调用。\n\n"
                f"正常: {normal}  异常: {abnormal}  失败: {failed}",
            )
        else:
            QMessageBox.information(
                self, "检测完成",
                f"所有 Key 状态正常。\n\n正常: {normal}  异常: {abnormal}  失败: {failed}",
            )

    def _reset_key(self, key_id: str):
        """恢复 Key（手动恢复，可恢复 permanent_disabled / abnormal / disabled 等）"""
        self._db.update_upstream_key(key_id, {"status": "active"})
        self._refresh_upstream_keys()

    def _disable_key(self, key_id: str):
        """禁用 Key（临时禁用，查分>100会自动恢复）"""
        self._db.update_upstream_key(key_id, {"status": "disabled"})
        self._refresh_upstream_keys()

    def _permanent_disable_key(self, key_id: str):
        """永久禁用 Key（不会自动恢复，需手动恢复）"""
        reply = QMessageBox.question(
            self, "永久禁用",
            f"确定永久禁用 Key {key_id}？\n\n永久禁用后不会被查分等操作自动恢复，\n只能手动点击「恢复」来重新启用。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._db.update_upstream_key(key_id, {"status": "permanent_disabled"})
            self._refresh_upstream_keys()

    def _open_batch_status_dialog(self, enable: bool, permanent: bool = True):
        """批量禁/解禁弹框（enable=False 永久禁用 / enable=True 解禁）"""
        keys = self._db.get_upstream_keys()
        if enable:
            action_text = "批量解禁"
            status_to = "active"
            confirm_template = "确定将上述 {n} 个 Key 恢复为可用吗？"
        elif permanent:
            action_text = "批量永久禁用"
            status_to = "permanent_disabled"
            confirm_template = "确定永久禁用上述 {n} 个 Key 吗？\n（永久禁用后只能手动解禁）"
        else:
            action_text = "批量临时禁用"
            status_to = "disabled"
            confirm_template = "确定临时禁用上述 {n} 个 Key 吗？\n（查分后可自动恢复）"

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
        v.addWidget(QLabel(f"筛选条件：积分（剩余）在上述范围内的 Key\n操作类型：{action_text}"))

        # 预览（实时刷新）
        preview_label = QLabel("")
        preview_label.setWordWrap(True)
        preview_label.setObjectName("preview_label")
        v.addWidget(preview_label)

        def _refresh_preview():
            lo, hi = min_spin.value(), max_spin.value()
            matched = self._filter_keys_by_points(keys, lo, hi, status_to)
            total = len(keys)
            skip_count = total - len([k for k in keys if (lo <= ApiProxyPage._points_remaining(k.get("points", "")) <= hi)])
            already_done = len([k for k in keys if
                                (lo <= ApiProxyPage._points_remaining(k.get("points", "")) <= hi)
                                and k.get("status") == status_to
                                ])
            preview_label.setText(
                f"范围匹配 {total - skip_count} 个，将实际生效 <b>{len(matched)}</b> 个"
                + (f"（{already_done} 个已{'启用' if enable else '永久禁用'}，跳过）" if already_done else "")
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
            self._db.update_upstream_key(k["key_id"], {"status": status_to})

        self._refresh_upstream_keys()
        QMessageBox.information(
            self, "完成",
            f"已{'解禁' if enable else '永久禁用'} {len(matched)} 个 Key",
        )

    @staticmethod
    def _filter_keys_by_points(keys: list, lo: int, hi: int, target_status: str = None) -> list:
        """按 points 剩余积分在 [lo, hi] 范围内筛选 Key，排除已处于目标状态的"""
        result = []
        for k in keys:
            if target_status and k.get("status") == target_status:
                continue  # 已经是目标状态，跳过
            pts = ApiProxyPage._points_remaining(k.get("points", ""))
            if pts < 0:
                continue  # 解析失败的跳过（没有积分数据）
            if lo <= pts <= hi:
                result.append(k)
        return result

    def _delete_upstream_key(self, key_id: str):
        """删除上游 Key"""
        reply = QMessageBox.question(
            self, "确认删除", f"确定删除 Key {key_id}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._db.delete_upstream_key(key_id)
            self._refresh_upstream_keys()

    def _on_keys_context_menu(self, pos):
        """上游Key池右键菜单"""
        selected_rows = set()
        for item in self._keys_table.selectedItems():
            selected_rows.add(item.row())

        if not selected_rows:
            return

        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)

        if len(selected_rows) == 1:
            row = list(selected_rows)[0]
            key_id_item = self._keys_table.item(row, 0)
            if not key_id_item:
                return
            key_id = key_id_item.text()

            # 复制 API Key
            action_copy = menu.addAction("📋 复制 API Key")
            action_copy.triggered.connect(lambda: self._copy_upstream_key(key_id))

            menu.addSeparator()

            # 状态操作
            status_item = self._keys_table.item(row, 2)
            status_text = status_item.text() if status_item else ""
            if "活跃" not in status_text:
                action_enable = menu.addAction("✅ 恢复")
                action_enable.triggered.connect(lambda: self._reset_key(key_id))
            else:
                action_disable = menu.addAction("🚫 禁用")
                action_disable.triggered.connect(lambda: self._disable_key(key_id))

            menu.addSeparator()

            # 一键配置（直连上游 copilot.tencent.com/v2，不走本地代理）
            action_cfg_wb = menu.addAction("🔧 一键配置 WorkBuddy")
            action_cfg_wb.triggered.connect(lambda: self._config_workbuddy_upstream(key_id))

            action_cfg_cb = menu.addAction("🔧 一键配置 CodeBuddy")
            action_cfg_cb.triggered.connect(lambda: self._config_codebuddy_upstream(key_id))

            menu.addSeparator()

            action_del = menu.addAction("🗑️ 删除")
            action_del.triggered.connect(lambda: self._delete_upstream_key(key_id))
        else:
            action_batch_del = menu.addAction(f"🗑️ 批量删除 ({len(selected_rows)} 个)")
            action_batch_del.triggered.connect(lambda: self._batch_delete_keys(selected_rows))

        menu.exec(QCursor.pos())

    def _copy_upstream_key(self, key_id: str):
        """复制上游Key的 api_key 值到剪贴板"""
        keys = self._db.get_upstream_keys()
        for k in keys:
            if k.get("key_id") == key_id:
                api_key = k.get("api_key", "")
                if api_key:
                    QApplication.clipboard().setText(api_key)
                break

    def _batch_delete_keys(self, rows: set):
        """批量删除选中的Key"""
        reply = QMessageBox.question(
            self, "确认批量删除",
            f"确定删除选中的 {len(rows)} 个 Key？\n此操作不可撤销！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            for row in rows:
                key_id_item = self._keys_table.item(row, 0)
                if key_id_item:
                    self._db.delete_upstream_key(key_id_item.text())
            self._refresh_upstream_keys()

    # === 子 Key 管理 ===

    def _refresh_sub_keys(self):
        """刷新子 API Key 列表"""
        sub_keys = self._db.get_sub_api_keys()
        if self._subkeys_sort_column is not None:
            sub_keys.sort(
                key=lambda sk: self._subkey_sort_value(sk, self._subkeys_sort_column),
                reverse=self._subkeys_sort_order == Qt.DescendingOrder,
            )
        self._subkeys_table.setRowCount(len(sub_keys))

        # 预加载上游 Key 数据，用于汇总积分
        upstream_keys = self._db.get_upstream_keys()
        upstream_map = {uk.get("key_id"): uk for uk in upstream_keys}

        for row, sk in enumerate(sub_keys):
            api_key = sk.get("api_key", "")
            label = sk.get("label", "")
            is_active = sk.get("is_active", True)
            models = sk.get("allowed_models", [])
            max_usage = sk.get("max_usage", 0)
            used = sk.get("used_count", 0)
            rpm = sk.get("rate_limit_rpm", 1000)
            allowed_key_ids = sk.get("allowed_key_ids", [])
            key_mode = sk.get("key_mode", 1)

            # 子 Key 自己的统计（当天或总计）
            sk_id = sk.get("key_id", "")
            if self._subkeys_today_only:
                today_sk = self._db.get_today_stats("sub", sk_id)
                sk_prompt = today_sk.get("prompt_tokens", 0)
                sk_completion = today_sk.get("completion_tokens", 0)
                sk_total = today_sk.get("total_tokens", 0)
                sk_cached = today_sk.get("cached_tokens", 0)
                sk_credits = today_sk.get("credits", 0.0)
            else:
                sk_prompt = sk.get("total_prompt_tokens", 0)
                sk_completion = sk.get("total_completion_tokens", 0)
                sk_total = sk.get("total_tokens", 0)
                sk_cached = sk.get("total_cached_tokens", 0)
                sk_credits = sk.get("total_credits", 0.0)

            # Key 前缀显示
            key_display = api_key[:12] + "..." if len(api_key) > 12 else api_key
            key_item = _set_item(self._subkeys_table, row, 0, key_display, tooltip=api_key)
            key_item.setData(Qt.UserRole, sk_id)

            _set_item(self._subkeys_table, row, 1, label or "-", tooltip=label or "无标签")

            status_text = "✅ 启用" if is_active else "🚫 禁用"
            status_item = _set_item(self._subkeys_table, row, 2, status_text, tooltip=f"状态: {'启用' if is_active else '禁用'}")
            status_item.setForeground(Qt.darkGreen if is_active else Qt.red)

            # 模型限制
            models_text = "全部" if not models else ", ".join(models[:3]) + ("..." if len(models) > 3 else "")
            models_tip = "全部模型" if not models else ", ".join(models)
            _set_item(self._subkeys_table, row, 3, models_text, tooltip=models_tip)

            # 使用量
            usage_text = f"{used}/{max_usage}" if max_usage > 0 else f"{used}/∞"
            _set_item(self._subkeys_table, row, 4, usage_text, tooltip=f"已用: {used:,} / 上限: {max_usage if max_usage > 0 else '∞'}")

            # 总积分：该子 Key 可调用的所有上游 Key 的剩余积分总和
            total_points = self._db.get_total_points_for_sub_key(allowed_key_ids if allowed_key_ids else None)
            points_display = f"{total_points:.0f}" if total_points > 0 else "-"
            points_item = _set_item(self._subkeys_table, row, 5, points_display, tooltip=f"总积分: {total_points:.2f}")
            if total_points > 0:
                points_item.setForeground(Qt.darkGreen)

            # 调用模式
            mode_map = {1: ("1-专一", "专一模式：粘住一个 Key 用到不可用才换"),
                        2: ("2-临期", "临期优先：优先调用积分最快过期的 Key"),
                        3: ("3-轮询", "轮询模式：每次请求轮换到下一个 Key"),
                        4: ("4-亲和", "会话亲和：同一会话绑定同一上游 Key，TTL 1 小时")}
            mode_text, mode_tip = mode_map.get(key_mode, ("1-专一", "专一模式"))
            _set_item(self._subkeys_table, row, 6, mode_text, tooltip=mode_tip)

            _set_item(self._subkeys_table, row, 7, f"{rpm}", tooltip=f"速率限制: {rpm} 请求/分钟")

            # Token 统计（子 Key 自己的消耗）
            if sk_total > 0:
                sk_token_display = f"{_fmt_tokens(sk_prompt)}+{_fmt_tokens(sk_completion)}"
                sk_token_tip = f"输入: {sk_prompt:,}  输出: {sk_completion:,}  总计: {sk_total:,}"
            else:
                sk_token_display = "-"
                sk_token_tip = "暂无数据"
            _set_item(self._subkeys_table, row, 8, sk_token_display, tooltip=sk_token_tip)

            # 积分消耗（子 Key 自己的消耗）
            if sk_credits > 0:
                sk_credit_display = _fmt_credits(sk_credits)
                sk_credit_tip = f"累计积分消耗: {sk_credits:.4f}"
            else:
                sk_credit_display = "-"
                sk_credit_tip = "暂无数据"
            _set_item(self._subkeys_table, row, 9, sk_credit_display, tooltip=sk_credit_tip)

            # 缓存命中率（子 Key 自己的统计）
            if sk_total > 0 and sk_cached > 0:
                sk_cache_rate = sk_cached / sk_total * 100
                sk_cache_text = f"{sk_cache_rate:.1f}%"
                sk_cache_tip = f"缓存命中: {sk_cached:,} / 总计: {sk_total:,} = {sk_cache_rate:.1f}%"
            else:
                sk_cache_text = "-"
                sk_cache_tip = "暂无数据"
            _set_item(self._subkeys_table, row, 10, sk_cache_text, tooltip=sk_cache_tip)

            # 操作栏：一个按钮触发下拉菜单
            ops_widget = QWidget()
            ops_widget.setAttribute(Qt.WA_TranslucentBackground, True)
            ops_layout = QHBoxLayout(ops_widget)
            ops_layout.setContentsMargins(4, 0, 4, 0)
            ops_layout.setSpacing(0)

            from PySide6.QtWidgets import QToolButton
            btn_ops = QToolButton()
            btn_ops.setObjectName("ops_btn")
            btn_ops.setText("操作 ▾")
            btn_ops.setCursor(Qt.PointingHandCursor)
            btn_ops.setToolButtonStyle(Qt.ToolButtonTextOnly)
            btn_ops.setPopupMode(QToolButton.InstantPopup)

            from PySide6.QtWidgets import QMenu
            ops_menu = QMenu(btn_ops)
            _style_popup_menu(ops_menu)
            act = ops_menu.addAction("✏️ 编辑")
            act.triggered.connect(lambda checked, kid=sk_id: self._edit_sub_key(kid))
            act = ops_menu.addAction("📋 复制")
            act.triggered.connect(lambda checked, k=api_key: self._copy_sub_key(k))
            ops_menu.addSeparator()
            act = ops_menu.addAction("🚫 禁用" if is_active else "✅ 启用")
            act.triggered.connect(lambda checked, kid=sk_id, active=is_active: self._toggle_sub_key(kid, not active))
            act = ops_menu.addAction("📊 明细")
            act.triggered.connect(lambda checked, kid=sk_id, lbl=label: self._show_daily_detail("sub", kid, f"子Key {lbl or kid}"))
            ops_menu.addSeparator()
            act = ops_menu.addAction("🗑️ 删除")
            act.triggered.connect(lambda checked, kid=sk_id: self._delete_sub_key(kid))

            btn_ops.setMenu(ops_menu)
            ops_layout.addWidget(btn_ops)
            ops_layout.addStretch()
            self._subkeys_table.setCellWidget(row, 11, ops_widget)

        # 更新统计
        active_count = sum(1 for sk in sub_keys if sk.get("is_active", True))
        disabled_count = len(sub_keys) - active_count
        total_used_sk = sum(sk.get("used_count", 0) for sk in sub_keys)
        self._sk_stat_total.setText(f"📋 总 Key: {len(sub_keys)}")
        self._sk_stat_active.setText(f"✅ 活跃: {active_count}")
        self._sk_stat_disabled.setText(f"🚫 禁用: {disabled_count}")
        self._sk_stat_total_used.setText(f"📊 总调用: {total_used_sk}")

    def _create_sub_key(self):
        """创建子 API Key"""
        upstream_keys = self._db.get_upstream_keys()
        dialog = CreateSubKeyDialog(upstream_keys, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            data = dialog.get_data()
            sub_key_data = {
                "key_id": f"sk_{secrets.token_hex(4)}",
                "api_key": f"sk-{secrets.token_urlsafe(32)}",
                "label": data.get("label", ""),
                "is_active": True,
                "allowed_models": data.get("allowed_models", []),
                "allowed_key_ids": data.get("allowed_key_ids", []),
                "max_usage": data.get("max_usage", 0),
                "used_count": 0,
                "rate_limit_rpm": data.get("rate_limit_rpm", 1000),
                "key_mode": data.get("key_mode", 1),
                "created_at": __import__('datetime').datetime.now().isoformat(),
            }
            self._db.add_sub_api_key(sub_key_data)
            self._invalidate_proxy_auth_cache()
            self._refresh_sub_keys()

            # 提示创建成功
            QMessageBox.information(self, "创建成功", "子 API Key 创建成功！")

    def _edit_sub_key(self, key_id: str):
        """编辑子 API Key"""
        sub_keys = self._db.get_sub_api_keys()
        edit_data = None
        for sk in sub_keys:
            if sk.get("key_id") == key_id:
                edit_data = sk
                break
        if not edit_data:
            return

        upstream_keys = self._db.get_upstream_keys()
        dialog = CreateSubKeyDialog(upstream_keys, self, edit_data=edit_data)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            data = dialog.get_data()
            updates = {
                "label": data.get("label", ""),
                "allowed_models": data.get("allowed_models", []),
                "allowed_key_ids": data.get("allowed_key_ids", []),
                "max_usage": data.get("max_usage", 0),
                "rate_limit_rpm": data.get("rate_limit_rpm", 1000),
                "key_mode": data.get("key_mode", 1),
            }
            self._db.update_sub_api_key(key_id, updates)
            self._invalidate_proxy_auth_cache()
            self._refresh_sub_keys()

    def _on_subkey_double_clicked(self, row: int, col: int):
        """双击子Key行复制API Key"""
        if col == 0:  # API Key 列
            key_item = self._subkeys_table.item(row, 0)
            if key_item:
                full_key = key_item.toolTip()
                if full_key:
                    QApplication.clipboard().setText(full_key)
                    # 视觉反馈：临时改变单元格文字
                    old_text = key_item.text()
                    key_item.setText("✅ 已复制!")
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(800, lambda: key_item.setText(old_text) if key_item else None)

    def _copy_sub_key(self, api_key: str):
        """复制子 API Key"""
        clipboard = QApplication.clipboard()
        clipboard.setText(api_key)

    def _invalidate_proxy_auth_cache(self):
        """Refresh the running proxy's auth cache after sub-key changes."""
        try:
            if self._proxy_server and self._proxy_server.router:
                self._proxy_server.router.invalidate_upstream_cache()
        except Exception:
            pass

    def _subkey_item_for_row(self, row: int):
        return self._subkeys_table.item(row, 0)

    def _subkey_id_for_row(self, row: int) -> str:
        item = self._subkey_item_for_row(row)
        if not item:
            return ""
        return str(item.data(Qt.UserRole) or "")

    def _subkey_api_for_row(self, row: int) -> str:
        item = self._subkey_item_for_row(row)
        if not item:
            return ""
        return item.toolTip() or ""

    def _on_subkeys_context_menu(self, pos):
        """子API Key右键菜单"""
        selected_rows = set()
        for item in self._subkeys_table.selectedItems():
            selected_rows.add(item.row())

        if not selected_rows:
            return

        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)

        if len(selected_rows) == 1:
            row = list(selected_rows)[0]
            key_item = self._subkeys_table.item(row, 0)
            if not key_item:
                return
            # 从 tooltip 获取完整 api_key
            full_key = key_item.toolTip() or key_item.text()
            sk_id = self._subkey_id_for_row(row)

            action_copy = menu.addAction("📋 复制 API Key")
            action_copy.triggered.connect(lambda: self._copy_sub_key(full_key))

            menu.addSeparator()

            # 状态切换
            status_item = self._subkeys_table.item(row, 2)
            status_text = status_item.text() if status_item else ""
            # 获取 key_id
            if "禁用" in status_text and sk_id:
                action_enable = menu.addAction("✅ 启用")
                action_enable.triggered.connect(lambda: self._toggle_sub_key(sk_id, True))
            elif "启用" in status_text and sk_id:
                action_disable = menu.addAction("🚫 禁用")
                action_disable.triggered.connect(lambda: self._toggle_sub_key(sk_id, False))

            menu.addSeparator()

            if sk_id:
                action_edit = menu.addAction("✏️ 编辑")
                action_edit.triggered.connect(lambda: self._edit_sub_key(sk_id))

                action_del = menu.addAction("🗑️ 删除")
                action_del.triggered.connect(lambda: self._delete_sub_key(sk_id))

            menu.addSeparator()

            # 一键配置（仅单选时可用）
            action_cfg_wb = menu.addAction("🔧 一键配置 WorkBuddy")
            action_cfg_wb.triggered.connect(lambda: self._config_workbuddy(full_key))

            action_cfg_cb = menu.addAction("🔧 一键配置 CodeBuddy")
            action_cfg_cb.triggered.connect(lambda: self._config_codebuddy(full_key))
        else:
            action_copy_all = menu.addAction(f"📋 批量复制 ({len(selected_rows)} 个)")
            action_copy_all.triggered.connect(lambda: self._batch_copy_subkeys(selected_rows))

            menu.addSeparator()

            action_batch_del = menu.addAction(f"🗑️ 批量删除 ({len(selected_rows)} 个)")
            action_batch_del.triggered.connect(lambda: self._batch_delete_subkeys(selected_rows))

        menu.exec(QCursor.pos())

    def _batch_copy_subkeys(self, rows: set):
        """批量复制子Key"""
        keys_to_copy = []
        for row in rows:
            api_key = self._subkey_api_for_row(row)
            if api_key:
                keys_to_copy.append(api_key)
        if keys_to_copy:
            QApplication.clipboard().setText("\n".join(keys_to_copy))

    def _batch_delete_subkeys(self, rows: set):
        """批量删除子Key"""
        reply = QMessageBox.question(
            self, "确认批量删除",
            f"确定删除选中的 {len(rows)} 个子 Key？\n此操作不可撤销！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            deleted = 0
            for row in rows:
                sk_id = self._subkey_id_for_row(row)
                if sk_id:
                    self._db.delete_sub_api_key(sk_id)
                    deleted += 1
            if deleted:
                self._invalidate_proxy_auth_cache()
            self._refresh_sub_keys()

    # ─── 一键配置 WorkBuddy / CodeBuddy ───

    SUPPORTED_CONFIG_MODELS = [
        "hy3", "hy3-preview", "hunyuan-chat", "hunyuan-2.0-thinking",
        "deepseek-v4-pro", "deepseek-v4-flash",
        "deepseek-v3-2-volc", "deepseek-v3-1", "deepseek-v3-0324", "deepseek-r1",
        "glm-5.2", "glm-5.1", "glm-5.0", "glm-5.0-turbo", "glm-5v-turbo", "glm-4.7", "glm-4.6",
        "kimi-k2.6", "kimi-k2.5", "kimi-k2.7",
        "minimax-m3", "minimax-m2.7", "minimax-m2.5",
        "auto",
    ]

    # 模型显示名（按截图大小写处理；未列出的模型显示名等于 id）
    # 注意：图片文件名使用小写 id，与此处显示名解耦
    MODEL_DISPLAY_NAMES = {
        "hy3": "Hy3",
        "kimi-k2.7": "Kimi-K2.7-Code",
    }

    # 模型能力定义 (tool_call, images, reasoning)
    # 全部模型均支持图片输入（vision: true），避免 WorkBuddy 误判禁图
    MODEL_CAPABILITIES = {
        "hy3":                      (True,  True,  True),
        "hy3-preview":              (True,  True,  True),
        "hunyuan-chat":             (True,  True,  True),
        "hunyuan-2.0-thinking":     (True,  True,  True),
        "deepseek-v4-pro":          (True,  True,  True),
        "deepseek-v4-flash":        (True,  True,  True),
        "deepseek-v3-2-volc":       (True,  True,  True),
        "deepseek-v3-1":            (True,  True,  True),
        "deepseek-v3-0324":         (True,  True,  True),
        "deepseek-r1":              (True,  True,  True),
        "glm-5.2":                  (True,  True,  True),
        "glm-5.1":                  (True,  True,  True),
        "glm-5.0":                  (True,  True,  True),
        "glm-5.0-turbo":            (True,  True,  True),
        "glm-5v-turbo":             (True,  True,  True),
        "glm-4.7":                  (True,  True,  True),
        "glm-4.6":                  (True,  True,  True),
        "kimi-k2.6":                (True,  True,  True),
        "kimi-k2.5":                (True,  True,  True),
        "kimi-k2.7":                (True,  True,  True),
        "minimax-m3":               (True,  True,  True),
        "minimax-m2.7":             (True,  True,  True),
        "minimax-m2.5":             (True,  True,  True),
        "auto":                     (True,  True,  True),
    }

    def _build_model_entries(self, selected_ids: list, base_url: str,
                             api_key: str, include_custom_protocol: bool = True) -> list:
        """根据选中的模型 id 列表构建 models.json 条目。

        Args:
            selected_ids: 用户勾选的模型 id 列表
            base_url: 接口地址
            api_key: 写入条目的 apiKey
            include_custom_protocol: 是否写入 useCustomProtocol 字段（WorkBuddy 需要，CodeBuddy 不需要）
        """
        entries = []
        for model_id in selected_ids:
            tool_call, images, reasoning = self.MODEL_CAPABILITIES.get(model_id, (True, True, True))
            display_name = self.MODEL_DISPLAY_NAMES.get(model_id, model_id)
            entry = {
                "id": model_id,
                "name": display_name,
                "vendor": "Custom",
                "url": base_url,
                "apiKey": api_key,
                "supportsToolCall": tool_call,
                "supportsImages": images,
                "supportsReasoning": reasoning,
            }
            if include_custom_protocol:
                entry["useCustomProtocol"] = False
            if reasoning:
                entry["reasoning"] = {"supportedEfforts": ["max"]}
            entries.append(entry)
        return entries

    def _config_workbuddy(self, api_key: str):
        """一键配置 WorkBuddy 的 models.json（用户选择模型 + 增量写入）"""
        port = self._port_spin.value()
        mode = self._listen_mode_combo.currentData() if hasattr(self, '_listen_mode_combo') else "local"
        if mode == "open":
            ips = self._get_local_ips()
            host = ips[0] if ips else "0.0.0.0"
        else:
            host = "127.0.0.1"
        base_url = f"http://{host}:{port}/v1"

        dlg = ModelSelectDialog("一键配置 WorkBuddy", base_url, self.SUPPORTED_CONFIG_MODELS, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dlg.selected_models()
        if not selected:
            QMessageBox.information(self, "提示", "未勾选任何模型，已取消配置。")
            return

        entries = self._build_model_entries(selected, base_url, api_key, include_custom_protocol=True)
        wb_dir = os.path.join(os.path.expanduser("~"), ".workbuddy")
        target_path = os.path.join(wb_dir, "models.json")

        existing = _read_existing_models(target_path)
        merged, replaced, added = _incremental_merge_models(existing, entries)
        _write_models_json(target_path, merged, wrapper="array")

        QMessageBox.information(
            self, "✅ 配置完成",
            f"WorkBuddy 已配置 {len(entries)} 个模型！\n"
            f"（新增 {added} 个，更新 {replaced} 个，当前共 {len(merged)} 个模型）\n"
            f"接口地址: {base_url}\n\n"
            f"文件位置:\n{target_path}"
        )

    def _config_codebuddy(self, api_key: str):
        """一键配置 CodeBuddy 的 models.json（用户选择模型 + 增量写入）"""
        port = self._port_spin.value()
        mode = self._listen_mode_combo.currentData() if hasattr(self, '_listen_mode_combo') else "local"
        if mode == "open":
            ips = self._get_local_ips()
            host = ips[0] if ips else "0.0.0.0"
        else:
            host = "127.0.0.1"
        base_url = f"http://{host}:{port}/v1"

        dlg = ModelSelectDialog("一键配置 CodeBuddy", base_url, self.SUPPORTED_CONFIG_MODELS, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dlg.selected_models()
        if not selected:
            QMessageBox.information(self, "提示", "未勾选任何模型，已取消配置。")
            return

        entries = self._build_model_entries(selected, base_url, api_key, include_custom_protocol=False)
        cb_dir = os.path.join(os.path.expanduser("~"), ".codebuddy")
        target_path = os.path.join(cb_dir, "models.json")

        existing = _read_existing_models(target_path)
        merged, replaced, added = _incremental_merge_models(existing, entries)
        _write_models_json(target_path, merged, wrapper="object")

        QMessageBox.information(
            self, "✅ 配置完成",
            f"CodeBuddy 已配置 {len(entries)} 个模型！\n"
            f"（新增 {added} 个，更新 {replaced} 个，当前共 {len(merged)} 个模型）\n"
            f"接口地址: {base_url}\n\n"
            f"文件位置:\n{target_path}"
        )

    def _config_workbuddy_upstream(self, key_id: str):
        """一键配置 WorkBuddy（直连上游，不走代理；用户选择模型 + 增量写入）"""
        # 查找上游 Key 的 api_key
        keys = self._db.get_upstream_keys()
        api_key = ""
        key_label = ""
        for k in keys:
            if k.get("key_id") == key_id:
                api_key = k.get("api_key", "")
                key_label = k.get("label", "")
                break
        if not api_key:
            QMessageBox.warning(self, "无法配置", "未找到该上游 Key 的 API Key")
            return

        # 直连上游，不走本地代理
        base_url = "https://copilot.tencent.com/v2"

        dlg = ModelSelectDialog(
            "一键配置 WorkBuddy（直连上游）", base_url, self.SUPPORTED_CONFIG_MODELS, self
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dlg.selected_models()
        if not selected:
            QMessageBox.information(self, "提示", "未勾选任何模型，已取消配置。")
            return

        entries = self._build_model_entries(selected, base_url, api_key, include_custom_protocol=True)
        wb_dir = os.path.join(os.path.expanduser("~"), ".workbuddy")
        target_path = os.path.join(wb_dir, "models.json")

        existing = _read_existing_models(target_path)
        merged, replaced, added = _incremental_merge_models(existing, entries)
        _write_models_json(target_path, merged, wrapper="array")

        QMessageBox.information(
            self, "✅ 配置完成",
            f"WorkBuddy 已配置 {len(entries)} 个模型！\n"
            f"（新增 {added} 个，更新 {replaced} 个，当前共 {len(merged)} 个模型）\n"
            f"直连上游: {base_url}\n"
            f"使用上游 Key: {key_label or key_id}\n\n"
            f"文件位置:\n{target_path}"
        )

    def _config_codebuddy_upstream(self, key_id: str):
        """一键配置 CodeBuddy（直连上游，不走代理；用户选择模型 + 增量写入）"""
        # 查找上游 Key 的 api_key
        keys = self._db.get_upstream_keys()
        api_key = ""
        key_label = ""
        for k in keys:
            if k.get("key_id") == key_id:
                api_key = k.get("api_key", "")
                key_label = k.get("label", "")
                break
        if not api_key:
            QMessageBox.warning(self, "无法配置", "未找到该上游 Key 的 API Key")
            return

        # 直连上游，不走本地代理
        base_url = "https://copilot.tencent.com/v2"

        dlg = ModelSelectDialog(
            "一键配置 CodeBuddy（直连上游）", base_url, self.SUPPORTED_CONFIG_MODELS, self
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dlg.selected_models()
        if not selected:
            QMessageBox.information(self, "提示", "未勾选任何模型，已取消配置。")
            return

        entries = self._build_model_entries(selected, base_url, api_key, include_custom_protocol=False)
        cb_dir = os.path.join(os.path.expanduser("~"), ".codebuddy")
        target_path = os.path.join(cb_dir, "models.json")

        existing = _read_existing_models(target_path)
        merged, replaced, added = _incremental_merge_models(existing, entries)
        _write_models_json(target_path, merged, wrapper="object")

        QMessageBox.information(
            self, "✅ 配置完成",
            f"CodeBuddy 已配置 {len(entries)} 个模型！\n"
            f"（新增 {added} 个，更新 {replaced} 个，当前共 {len(merged)} 个模型）\n"
            f"直连上游: {base_url}\n"
            f"使用上游 Key: {key_label or key_id}\n\n"
            f"文件位置:\n{target_path}"
        )

    def _delete_workbuddy_config(self):
        """删除 WorkBuddy 的 models.json 配置文件"""
        import os
        target_path = os.path.join(os.path.expanduser("~"), ".workbuddy", "models.json")
        if not os.path.exists(target_path):
            QMessageBox.information(self, "提示", "WorkBuddy 配置文件不存在，无需删除。\n\n路径: %USERPROFILE%\\.workbuddy\\models.json")
            return
        reply = QMessageBox.warning(
            self, "⚠️ 确认删除",
            f"确定删除 WorkBuddy 配置文件？\n\n路径:\n{target_path}\n\n删除后 WorkBuddy 将无法使用已配置的模型。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                os.remove(target_path)
                QMessageBox.information(self, "✅ 删除成功", f"WorkBuddy 配置文件已删除！\n\n{target_path}")
            except Exception as e:
                QMessageBox.critical(self, "删除失败", f"删除文件时出错：\n{e}")

    def _delete_codebuddy_config(self):
        """删除 CodeBuddy 的 models.json 配置文件"""
        import os
        target_path = os.path.join(os.path.expanduser("~"), ".codebuddy", "models.json")
        if not os.path.exists(target_path):
            QMessageBox.information(self, "提示", "CodeBuddy 配置文件不存在，无需删除。\n\n路径: %USERPROFILE%\\.codebuddy\\models.json")
            return
        reply = QMessageBox.warning(
            self, "⚠️ 确认删除",
            f"确定删除 CodeBuddy 配置文件？\n\n路径:\n{target_path}\n\n删除后 CodeBuddy 将无法使用已配置的模型。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                os.remove(target_path)
                QMessageBox.information(self, "✅ 删除成功", f"CodeBuddy 配置文件已删除！\n\n{target_path}")
            except Exception as e:
                QMessageBox.critical(self, "删除失败", f"删除文件时出错：\n{e}")

    def _toggle_sub_key(self, key_id: str, enable: bool):
        """启用/禁用子 Key"""
        self._db.update_sub_api_key(key_id, {"is_active": enable})
        self._invalidate_proxy_auth_cache()
        self._refresh_sub_keys()

    def _delete_sub_key(self, key_id: str):
        """删除子 Key"""
        reply = QMessageBox.question(
            self, "确认删除", f"确定删除子 Key {key_id}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._db.delete_sub_api_key(key_id)
            self._invalidate_proxy_auth_cache()
            self._refresh_sub_keys()

    # === 使用日志 ===

    def _refresh_log(self):
        """刷新请求日志（智能跳过无变化时）"""
        # 只获取清空之后的日志（_log_cleared_since 为 0 时获取全部）
        logs = self._db.get_request_logs(since=self._log_cleared_since, limit=200)

        # 无日志
        if not logs:
            if self._log_edit.toPlainText() != "暂无日志":
                self._log_edit.setPlainText("暂无日志")
            self._last_log_timestamp = 0.0
            return

        # 智能跳过：最新日志的 timestamp 没变则跳过
        latest_ts = logs[-1].get("timestamp", 0) if logs else 0
        if latest_ts == self._last_log_timestamp and self._log_edit.toPlainText() != "暂无日志":
            return  # 没有新日志，跳过

        self._last_log_timestamp = latest_ts

        lines = []
        for l in reversed(logs):
            ts = l.get("timestamp", 0)
            time_str = __import__('datetime').datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""
            event = l.get("event", "")
            model = l.get("model", "-")
            sub_label = l.get("sub_key_label", "")
            main_label = l.get("main_key_label", "")
            duration = l.get("duration_ms", 0)
            pt = l.get("prompt_tokens", 0)
            ct = l.get("completion_tokens", 0)
            error = l.get("error", "")
            upstream_status = l.get("upstream_status", 0)
            request_path = l.get("request_path", "")

            # 根据事件类型显示不同格式
            if event == "request":
                # 收到的请求（WorkBuddy 发过来的）
                icon = "📨"
                line = f"{icon} {time_str} 收到请求  {error}"
            elif event == "auth_fail":
                # 认证失败
                icon = "🔒"
                detail = f"sub={sub_label}" if sub_label else ""
                line = f"{icon} {time_str} 认证失败  {detail}  ❌{error}"
            elif event == "upstream_error":
                # 上游返回错误
                icon = "⚠️"
                line = f"{icon} {time_str} 上游错误  key={main_label}  model={model}  HTTP={upstream_status}  ❌{error}"
            elif event == "upstream_429":
                # 上游限流
                icon = "🐌"
                line = f"{icon} {time_str} 上游限流  key={main_label}  model={model}  ❌{error}"
            elif event == "sensitive_replace":
                icon = "🛡️"
                line = f"{icon} {time_str} 敏感信息检测  sub={sub_label}  model={model}  {error}"
            elif event == "start":
                icon = "🟢"
                line = f"{icon} {time_str} START  sub={sub_label}  key={main_label}  model={model}"
            elif event == "end":
                icon = "🔵"
                line = f"{icon} {time_str} END    sub={sub_label}  key={main_label}  model={model}  {duration}ms  p={pt} c={ct}"
            elif event == "error":
                icon = "🔴"
                line = f"{icon} {time_str} ERROR  sub={sub_label}  key={main_label}  model={model}  ❌{error}"
            else:
                icon = "⚪"
                detail_parts = []
                if error:
                    detail_parts.append(f"❌{error}")
                if request_path:
                    detail_parts.append(f"path={request_path}")
                line = f"{icon} {time_str} {event}  sub={sub_label}  key={main_label}  {'  '.join(detail_parts)}"

            lines.append(line)

        self._log_edit.setPlainText("\n".join(lines))

    def _clear_log(self):
        """清空日志显示 — 记录清空时间戳，之后只显示新产生的日志"""
        import time
        self._log_cleared_since = time.time()
        self._last_log_timestamp = 0.0
        self._log_edit.clear()

    # === 页面生命周期 ===

    def showEvent(self, event):
        """页面显示时刷新数据"""
        super().showEvent(event)
        self._refresh_upstream_keys()
        self._refresh_sub_keys()
        self._refresh_log()
        # 启动日志自动刷新（每2秒）
        self._log_timer.start(2000)
        # 启动表格定时刷新（每10秒，实时积分更新）
        self._table_timer.start(5000)  # 每5秒刷新

    def hideEvent(self, event):
        """页面隐藏时停止刷新"""
        super().hideEvent(event)
        self._log_timer.stop()
        self._table_timer.stop()

    def _refresh_tables_if_visible(self):
        """定时刷新表格（仅服务运行中时有效，积分实时更新需要从 db 读内存）"""
        if self.isVisible():
            # 服务运行中时直接用 db 内存数据（积分/token/并发都在内存实时更新），
            # 不从文件重载（否则会把延迟写入的新积分覆盖回旧值）
            self._refresh_upstream_keys(reload_from_disk=not (self._proxy_server and self._proxy_server.is_running))
            self._refresh_sub_keys()

    def _cleanup(self):
        """清理资源"""
        if self._proxy_server and self._proxy_server.is_running:
            self._proxy_server.stop()
