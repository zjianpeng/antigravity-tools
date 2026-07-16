"""侧边栏导航组件"""

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QButtonGroup, QSpacerItem, QSizePolicy
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QIcon


def _get_version() -> str:
    """读取版本号"""
    version_file = os.path.join(os.path.dirname(__file__), "..", "..", "VERSION")
    if os.path.exists(version_file):
        try:
            with open(version_file, "r", encoding="utf-8") as f:
                return f"v{f.read().strip()}"
        except Exception:
            pass
    return "v1.8.0"


# 导航项定义： (id, 图标emoji, 标签)
NAV_ITEMS = [
    ("dashboard", "📊", "nav.dashboard"),
    ("accounts", "👥", "nav.accounts"),
    ("checkin", "✅", "nav.checkin"),
    ("api_proxy", "🔗", "nav.api_proxy"),
    ("settings", "⚙️", "nav.settings"),
]


class Sidebar(QWidget):
    """侧边栏导航"""

    page_changed = Signal(str)  # 发出页面 ID

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self._current_page = "dashboard"
        self._buttons = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Logo
        logo_label = QLabel("⚡ Antigravity")
        logo_label.setObjectName("sidebar_logo")
        layout.addWidget(logo_label)

        # 版本 + QQ群
        version_qq_label = QLabel(f"{_get_version()}  💬QQ群:1025605799")
        version_qq_label.setObjectName("sidebar_version")
        version_qq_label.setCursor(Qt.PointingHandCursor)
        version_qq_label.setToolTip("点击复制QQ群号")
        version_qq_label.mousePressEvent = lambda e: self._copy_qq_group()
        layout.addWidget(version_qq_label)

        # 分隔线
        sep = QFrame()
        sep.setObjectName("sidebar_sep")
        sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        # 导航按钮
        btn_group = QButtonGroup(self)
        btn_group.setExclusive(True)

        from ...i18n import t

        for page_id, icon, label_key in NAV_ITEMS:
            btn = QPushButton(f"  {icon}  {t(label_key)}")
            btn.setObjectName("nav_btn")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setProperty("active", page_id == self._current_page)
            btn.clicked.connect(lambda checked, pid=page_id: self._on_nav_clicked(pid))
            btn_group.addButton(btn)
            self._buttons[page_id] = btn
            layout.addWidget(btn)

        # 弹性空间
        layout.addSpacerItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding))

        # 底部信息
        info_label = QLabel("  🎯 Multi-Platform\n  IDE Tool Manager")
        info_label.setObjectName("sidebar_version")
        info_label.setStyleSheet("padding: 16px; opacity: 0.4;")
        layout.addWidget(info_label)

        # 默认选中 dashboard
        self._buttons["dashboard"].setChecked(True)

    def _copy_qq_group(self):
        """复制QQ群号到剪贴板"""
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText("1025605799")

    def _on_nav_clicked(self, page_id: str):
        """导航按钮点击"""
        if page_id == self._current_page:
            return
        self._current_page = page_id
        # 更新按钮状态
        for pid, btn in self._buttons.items():
            btn.setProperty("active", pid == page_id)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self.page_changed.emit(page_id)

    def refresh_translations(self):
        """刷新多语言文本"""
        from ...i18n import t
        for page_id, icon, label_key in NAV_ITEMS:
            btn = self._buttons.get(page_id)
            if btn:
                btn.setText(f"  {icon}  {t(label_key)}")


# 需要导入 QFrame
from PySide6.QtWidgets import QFrame
