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
        "version": "2.3.0",
        "date": "2026-08-08",
        "items": [
            "新增「无感换号」页面（侧边栏 🔀）：本地透明中转接管 CodeBuddy / WorkBuddy 的 API 流量，仅在对话计费请求经过时替换为「上游 Key 池」的 token——客户端登录账号、历史对话、UI 完全不受影响，限流 / 额度耗尽 / 风控自动换池里下一个 Key；独立 Key 池（与 API 代理主池状态隔离，只收 JWT token），支持端口 / 监听模式（本地 / 局域网开放）/ 最低积分 / 自动启用 / 限流冷却调节；使用日志 + 真实用量统计（调用次数、Token、缓存命中、📅当天筛选、使用中的 Key 置顶）；软件重启后中转与双客户端接入自动恢复，无需手动重开",
            "切换 WorkBuddy 账号新增「保留当前账号的对话记录」选项（默认勾选）：切号时自动把旧账号的对话记录、长期记忆、MCP 连接器迁移到新账号名下，迁移前自动备份，换号不再丢对话",
            "账号管理：工具栏新增「💬 恢复对话」——换号 / 微信重登后对话不见了？按账号分组列出本地全部历史对话，勾选想要的精准恢复到当前账号名下（操作前自动备份数据库，重启 WorkBuddy 客户端后可见）",
            "账号管理：右键新增「📋 复制网页注入脚本」——复制后在 workbuddy.cn / codebuddy.cn 页面按 F12 粘贴到控制台，浏览器即切换为该账号（无需手机验证码，刷新页面后失效）",
        ],
    },
    {
        "version": "2.2.0",
        "date": "2026-08-01",
        "items": [
            "账号管理：右键新增「切换 CodeBuddy CN 账号 / 切换 WorkBuddy 账号」——把所选 Token 账号的登录态一键写入对应桌面客户端（支持 macOS / Windows，客户端需至少登录过一次）。此功能需慎重：不同账号的会话相互独立；建议先手动关闭客户端再切换（自动关闭不一定生效），切换后如有异常重启客户端即可",
            "账号管理：右键新增「复制 Token」——按 Token 导入格式（昵称----accessToken----refreshToken）复制账号当前最新凭证，自动续期后的新 token 也能原样取出、直接再导入",
        ],
    },
    {
        "version": "2.1.0",
        "date": "2026-07-30",
        "items": [
            "账号管理：新增「Token 导入」——按 昵称----accessToken----refreshToken 格式批量导入 JWT 账号（refreshToken 可省，昵称省略时自动从 token 识别手机号）",
            "账号管理：JWT 账号支持无限续期——access token 90 天、refresh token 120 天双有效期，到期自动换新并写回本地，重启软件续期链不中断",
            "API 代理：上游 Key 池的 JWT Key 到期前自动续期——转发请求与池检测前都会检查有效期，临期自动换新，Key 池与账号表同步更新",
            "账号管理：批量状态检测覆盖纯 Token 账号——没有 API Key、只有 Token 的账号也能检测风控状态，检测前自动续期，避免可续期账号被误判为限流",
        ],
    },
    {
        "version": "2.0.1",
        "date": "2026-07-30",
        "items": [
            "账号管理：修复「按状态全选 → 异常」选不中的问题——批量检测结果现在会写回账号表，异常账号红色显示、可一键全选，复查通过后自动恢复正常",
            "API 代理：一键配置补上 kimi-k3 模型，可选模型与官方客户端完全对齐",
            "API 代理：编辑子 Key 的「限制模型」与一键配置统一为官方当前模型集合，移除已下架的旧模型",
        ],
    },
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
