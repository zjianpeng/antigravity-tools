"""数据模型 - 账号、平台、配置等核心数据结构"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Platform(Enum):
    """支持的平台 - CodeBuddy 与 WorkBuddy 账号通用，仅登录方式不同"""
    CODEBUDDY = "codebuddy"
    WORKBUDDY = "workbuddy"


class AccountStatus(Enum):
    """账号状态"""
    ACTIVE = "active"
    INACTIVE = "inactive"
    EXPIRED = "expired"
    QUOTA_EXHAUSTED = "quota_exhausted"
    ERROR = "error"


class PlanType(Enum):
    """套餐类型"""
    FREE = "free"
    PRO = "pro"
    TEAM = "team"
    ENTERPRISE = "enterprise"


@dataclass
class CheckinInfo:
    """签到信息"""
    last_checkin_time: Optional[datetime] = None
    streak_days: int = 0
    rewards: list = field(default_factory=list)
    daily_credit: int = 0          # 今日获得积分
    total_credits: int = 0         # 累计签到获得积分

    @property
    def checked_today(self) -> bool:
        """今日是否已签到（基于 last_checkin_time 判断）"""
        if self.last_checkin_time is None:
            return False
        # 兼容：last_checkin_time 可能是 datetime 或字符串
        t = self.last_checkin_time
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t)
            except (ValueError, TypeError):
                return False
        now = datetime.now()
        return (t.year == now.year and
                t.month == now.month and
                t.day == now.day)

    def mark_checked_today(self, credit: int = 0):
        """标记今日已签到"""
        self.last_checkin_time = datetime.now()
        if credit > 0:
            self.daily_credit = credit


@dataclass
class ResourcePackage:
    """资源包（积分包）信息 - 对应 /v2/billing/meter/get-user-resource 的 Account 条目"""
    package_name: str = ""          # 资源包名称（如 "CodeBuddy个人体验版"）
    package_type: str = ""         # 资源包类型（1=免费, 2=付费, 4=体验）
    product_name: str = ""         # 产品名称（如 "腾讯云代码助手"）
    sub_product_name: str = ""     # 子产品名称（如 "腾讯云代码助手 (IDE)"）
    capacity_unit: str = "credits" # 单位
    capacity_size: float = 0.0     # 总量
    capacity_remain: float = 0.0   # 剩余
    capacity_used: float = 0.0     # 已用
    cycle_size: float = 0.0        # 当前周期总量
    cycle_remain: float = 0.0      # 当前周期剩余
    cycle_start: str = ""          # 周期开始时间
    cycle_end: str = ""            # 周期结束时间
    status: int = 0                # 状态（0=正常）
    resource_id: str = ""          # 资源 ID

    @property
    def usage_percentage(self) -> float:
        """已用百分比"""
        if self.cycle_size > 0:
            return ((self.cycle_size - self.cycle_remain) / self.cycle_size) * 100
        return 0.0

    @property
    def remain_percentage(self) -> float:
        """剩余百分比"""
        return 100.0 - self.usage_percentage

    @property
    def is_exhausted(self) -> bool:
        """是否耗尽"""
        return self.cycle_remain <= 0

    @property
    def type_label(self) -> str:
        """类型中文标签"""
        type_map = {"1": "免费", "2": "付费", "4": "体验"}
        return type_map.get(self.package_type, self.package_type)


@dataclass
class CheckinStatus:
    """签到状态 - 对应 /v2/billing/meter/checkin-status 的响应"""
    active: bool = False               # 签到活动是否开启
    today_checked_in: bool = False      # 今日是否已签到
    streak_days: int = 0               # 连续签到天数
    daily_credit: int = 0              # 每日签到积分
    today_credit: int = 0              # 今日获得积分
    is_streak_day: bool = False        # 是否是连续签到日
    next_streak_day: int = 0           # 下一个连续签到日
    streak_bonus_days: int = 0         # 连续签到奖励天数
    streak_bonus_credit: int = 0       # 连续签到奖励积分
    week_checkin_days: int = 0         # 本周签到天数
    week_progress: list = field(default_factory=lambda: [False]*7)  # 本周每天签到进度
    total_credits: int = 0             # 签到总积分
    activity_name: str = ""            # 活动名称


@dataclass
class QuotaInfo:
    """配额信息（旧版，保留兼容）"""
    hourly_suggestions: int = 0
    hourly_suggestions_limit: int = 0
    weekly_chat: int = 0
    weekly_chat_limit: int = 0
    credits_remaining: float = 0.0
    credits_total: float = 0.0
    reset_time: Optional[datetime] = None
    last_updated: Optional[datetime] = None
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None
    # 新增：多资源包
    packages: list = field(default_factory=list)  # List[ResourcePackage]
    payment_type: str = ""            # 付费类型
    checkin_status: Optional[CheckinStatus] = field(default=None)


@dataclass
class Account:
    """通用账号模型"""
    uid: str = ""
    nickname: str = ""
    platform: Platform = Platform.CODEBUDDY
    status: AccountStatus = AccountStatus.ACTIVE
    status_reason: str = ""
    plan_type: PlanType = PlanType.FREE
    domain: str = ""
    enterprise_id: str = ""
    enterprise_name: str = ""
    auth_token: str = ""
    auth_raw: str = ""
    ck: str = ""                    # Cookie / 登录URL
    api_key: str = ""               # API Key (从服务器获取)
    profile_raw: str = ""
    usage_raw: str = ""
    checkin: CheckinInfo = field(default_factory=CheckinInfo)
    quota: QuotaInfo = field(default_factory=QuotaInfo)
    created_at: Optional[datetime] = None
    last_used: Optional[datetime] = None

    @property
    def display_name(self) -> str:
        return self.nickname or self.uid or "未知账号"

    @property
    def quota_percentage(self) -> float:
        if self.quota.weekly_chat_limit > 0:
            return (self.quota.weekly_chat / self.quota.weekly_chat_limit) * 100
        return 0.0
