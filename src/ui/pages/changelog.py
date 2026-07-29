"""更新日志页面 — 版本更新记录

新版本发布时，在 CHANGELOG 列表最前面追加一条即可。
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QScrollArea
)
from PySide6.QtCore import Qt

from ...i18n import t


# 更新日志数据：新版本加在最前面
CHANGELOG = [
    {
        "version": "2.0.0",
        "date": "2026-07-29",
        "items": [
            "账号管理：新增「按状态全选」下拉，一键选中正常 / 异常 / 有API / 无API 账号，支持取消选择",
            "账号管理：工具栏按钮精简（添加 / 导入 / 查询 / 检查 / 并发），批量按钮显示选中数量",
            "API 代理：上游 Key 池新增批量操作「永禁 / 禁用 / 解禁」，按积分范围筛选，实时预览本次实际生效数量，执行前二次确认",
            "API 代理：最低积分 / 自动启用阈值改为实时自动生效，修改即按当前积分扫描更新 Key 状态，无需重启服务",
            "API 代理：新增「限流冷却」设置，Key 被 429 限流后的冷却秒数可配（默认 10 秒，1~3600），即时生效",
            "模型支持：新增 kimi-k3（1M 上下文），/v1/models 返回 maxInputTokens 字段",
        ],
    },
]


class ChangelogPage(QWidget):
    """更新日志页面"""

    def __init__(self):
        super().__init__()
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel(t("nav.changelog"))
        title.setObjectName("page_title")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(32, 20, 32, 32)
        content_layout.setSpacing(16)

        for entry in CHANGELOG:
            content_layout.addWidget(self._build_version_card(entry))
        content_layout.addStretch()

        scroll.setWidget(content)
        layout.addWidget(scroll)

    def _build_version_card(self, entry: dict) -> QFrame:
        """单个版本的卡片"""
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setSpacing(8)

        header = QHBoxLayout()
        ver_label = QLabel(f"v{entry['version']}")
        ver_label.setStyleSheet("font-size: 16px; font-weight: 700;")
        header.addWidget(ver_label)
        date_label = QLabel(entry["date"])
        date_label.setStyleSheet("color: #9BA4B0; font-size: 12px;")
        header.addWidget(date_label)
        header.addStretch()
        v.addLayout(header)

        for item in entry["items"]:
            line = QLabel(f"• {item}")
            line.setWordWrap(True)
            v.addWidget(line)

        return card
