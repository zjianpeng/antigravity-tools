"""i18n 国际化模块"""

from pathlib import Path
import json

# 当前语言
_current_lang = "zh-CN"

# 翻译字典
_translations: dict[str, dict[str, str]] = {}

# 默认中文翻译
_zh_cn = {
    # 侧边栏
    "nav.dashboard": "仪表盘",
    "nav.accounts": "账号管理",
    "nav.checkin": "每日签到",
    "nav.quota": "配额监控",
    "nav.api_proxy": "API 代理",
    "nav.settings": "设置",

    # 平台名
    "platform.codebuddy": "CodeBuddy",
    "platform.workbuddy": "WorkBuddy",
    "platform.codex": "Codex",
    "platform.windsurf": "Windsurf",
    "platform.github_copilot": "GitHub Copilot",
    "platform.gemini_cli": "Gemini CLI",
    "platform.codebuddy_cn": "CodeBuddy CN",
    "platform.trae": "Trae",
    "platform.qoder": "Qoder",
    "platform.zed": "Zed",

    # 通用
    "common.add": "添加",
    "common.edit": "编辑",
    "common.delete": "删除",
    "common.save": "保存",
    "common.cancel": "取消",
    "common.confirm": "确认",
    "common.refresh": "刷新",
    "common.search": "搜索",
    "common.status": "状态",
    "common.loading": "加载中...",
    "common.success": "成功",
    "common.error": "错误",
    "common.warning": "警告",
    "common.close": "关闭",
    "common.enable": "启用",
    "common.disable": "禁用",
    "common.all": "全部",
    "common.active": "活跃",
    "common.inactive": "未激活",

    # 账号管理
    "accounts.title": "账号管理",
    "accounts.add_account": "添加账号",
    "accounts.import_batch": "批量导入",
    "accounts.oauth_login": "OAuth 登录",
    "accounts.browser_login": "浏览器登录",
    "accounts.quick_switch": "快速切换",
    "accounts.account_group": "账号分组",
    "accounts.nickname": "昵称",
    "accounts.uid": "UID",
    "accounts.plan_type": "套餐类型",
    "accounts.status": "状态",
    "accounts.last_used": "最后使用",
    "accounts.token": "令牌",
    "accounts.export": "导出",
    "accounts.copy_token": "复制令牌",

    # 签到
    "checkin.title": "每日签到",
    "checkin.checkin_all": "全部签到",
    "checkin.checkin_selected": "选中签到",
    "checkin.streak_days": "连续签到",
    "checkin.rewards": "签到奖励",
    "checkin.checked": "已签到",
    "checkin.not_checked": "未签到",
    "checkin.checkin_success": "签到成功",
    "checkin.checkin_failed": "签到失败",

    # 配额监控
    "quota.title": "配额监控",
    "quota.hourly_suggestions": "每小时建议",
    "quota.weekly_chat": "每周聊天",
    "quota.credits_remaining": "剩余积分",
    "quota.credits_total": "总积分",
    "quota.reset_time": "重置时间",
    "quota.auto_refresh": "自动刷新",
    "quota.auto_switch": "自动切换",
    "quota.switch_threshold": "切换阈值",

    # API 代理
    "api_proxy.title": "API 代理服务",
    "api_proxy.start": "启动服务",
    "api_proxy.stop": "停止服务",
    "api_proxy.port": "端口",
    "api_proxy.api_key": "API Key",
    "api_proxy.local_only": "仅本地访问",
    "api_proxy.lan_access": "局域网访问",
    "api_proxy.generate_key": "生成 Key",
    "api_proxy.rotate_key": "轮换 Key",

    # 设置
    "settings.title": "设置",
    "settings.general": "通用",
    "settings.theme": "主题",
    "settings.theme_light": "浅色",
    "settings.theme_dark": "深色",
    "settings.theme_system": "跟随系统",
    "settings.language": "语言",
    "settings.ui_scale": "界面缩放",
    "settings.close_behavior": "关闭行为",
    "settings.close_minimize": "最小化到托盘",
    "settings.close_exit": "直接退出",
    "settings.proxy": "代理",
    "settings.proxy_type": "代理类型",
    "settings.proxy_url": "代理地址",
    "settings.float_card": "浮动卡片",
    "settings.always_on_top": "始终置顶",
    "settings.show_on_startup": "开机自启",
    "settings.data_dir": "数据目录",
    "settings.auto_refresh": "自动刷新间隔",
    "settings.app_paths": "应用路径",
    "settings.two_fa": "两步验证",
}

_en = {
    "nav.dashboard": "Dashboard",
    "nav.accounts": "Accounts",
    "nav.checkin": "Check In",
    "nav.quota": "Quota",
    "nav.api_proxy": "API Proxy",
    "nav.settings": "Settings",
    "common.add": "Add",
    "common.edit": "Edit",
    "common.delete": "Delete",
    "common.save": "Save",
    "common.cancel": "Cancel",
    "common.confirm": "Confirm",
    "common.refresh": "Refresh",
    "common.search": "Search",
    "common.status": "Status",
    "common.loading": "Loading...",
    "common.success": "Success",
    "common.error": "Error",
    "common.close": "Close",
    "accounts.title": "Account Management",
    "checkin.title": "Daily Check In",
    "quota.title": "Quota Monitor",
    "api_proxy.title": "API Proxy Service",
    "settings.title": "Settings",
    "settings.theme": "Theme",
    "settings.theme_light": "Light",
    "settings.theme_dark": "Dark",
    "settings.theme_system": "System",
}

_translations["zh-CN"] = _zh_cn
_translations["en"] = _en


def set_language(lang: str):
    global _current_lang
    _current_lang = lang


def get_language() -> str:
    return _current_lang


def t(key: str, **kwargs) -> str:
    """翻译 key 到当前语言的文本"""
    trans = _translations.get(_current_lang, _zh_cn)
    text = trans.get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text
