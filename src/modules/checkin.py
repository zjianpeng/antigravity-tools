"""签到模块 - 每日签到逻辑"""

import logging
from datetime import datetime
from typing import Optional

from ..models import Account, Platform
from ..utils.store import save_account
from .api_client import ApiClient

logger = logging.getLogger(__name__)


class CheckinManager:
    """签到管理器"""

    def checkin_account(self, account: Account, proxy: Optional[str] = None) -> dict:
        """对单个账号执行签到

        优先使用 API Key (ck_xxx) 签到，更简单可靠；
        如果没有 API Key 则回退到 JWT 模式。

        签到逻辑说明：
        - daily-checkin API 签到成功返回 code=0
        - 今日已签到返回 code=10001
        - 签到获得的积分通过 get-user-resource 查询积分包来确认

        Returns:
            {"success": True, "already": True} -- 今日已签到
            {"success": True, "credit": int, "streak_days": int} -- 刚签到成功
            {"success": False, "error": str} -- 签到失败
        """
        # 优先使用 API Key 模式（更简单，无需 JWT 刷新）
        if account.api_key and account.api_key.startswith("ck_"):
            client = ApiClient.from_api_key(account.api_key, proxy=proxy)
        else:
            client = ApiClient.from_account(account, proxy=proxy)
        result = client.daily_checkin()

        if result["success"]:
            if result.get("already"):
                # 今日已签到 - 用 mark_checked_today 设置 last_checkin_time
                account.checkin.mark_checked_today()
                logger.info(f"今日已签到: {account.display_name}")
            else:
                # 新签到成功
                credit = result.get("credit", 0)
                account.checkin.mark_checked_today(credit)
                logger.info(f"签到成功: {account.display_name} +{credit}积分")

            # 从积分包推算连续签到天数和累计签到积分
            try:
                quota_result = client.get_user_resource()
                if quota_result["success"]:
                    packages = quota_result.get("packages", [])
                    self._update_checkin_stats_from_packages(account, packages)
                    # 联动更新上游 Key 池（用 API Key 或 auth_token 匹配）
                    try:
                        from .proxy_server import ProxyDatabase
                        db = ProxyDatabase.get_instance()
                        # 优先用 API Key 匹配，其次用 auth_token
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
                pass  # 积分包查询失败不影响签到状态

            save_account(account)
        else:
            logger.warning(f"签到失败: {account.display_name} - {result.get('error')}")

        return result

    def _update_checkin_stats_from_packages(self, account: Account, packages: list):
        """从积分包数据推算签到统计（仅统计本月）

        签到获得的积分包特征：
        - PackageName="CodeBuddy个人版国内运营裂变包"
        - CapacitySize=150 (每次签到)
        - PkgSourceType=2 (活动包)
        - CycleStartTime 为签到时间

        推算逻辑：
        - 本月签到积分 = 当月 CycleStartTime 开头的运营裂变包 CapacitySize 合计
        - 连续签到天数：今天签到包存在即表示已签到
        """
        checkin_keyword = "运营裂变包"
        today = datetime.now().strftime("%Y-%m-%d")
        month_prefix = datetime.now().strftime("%Y-%m")
        monthly_credits = 0
        checked_today = False

        for pkg in packages:
            name = pkg.package_name or ""
            if checkin_keyword in name:
                # 只统计本月的签到积分包
                if pkg.cycle_start and pkg.cycle_start.startswith(month_prefix):
                    monthly_credits += int(pkg.capacity_size)
                # 检查是否是今天签到的
                if pkg.cycle_start and pkg.cycle_start.startswith(today):
                    checked_today = True

        # 更新统计
        account.checkin.total_credits = monthly_credits
        if checked_today and account.checkin.streak_days == 0:
            account.checkin.streak_days = 1

    def checkin_all(self, accounts: list[Account], proxy: Optional[str] = None) -> dict:
        """批量签到所有账号"""
        results = {"success": 0, "failed": 0, "already": 0, "details": []}
        for account in accounts:
            if account.checkin.checked_today:
                results["already"] += 1
                results["details"].append({
                    "account": account.display_name,
                    "status": "already",
                    "message": f"今日已签到 (连续{account.checkin.streak_days}天)"
                })
                continue

            result = self.checkin_account(account, proxy=proxy)
            if result["success"]:
                results["success"] += 1
                results["details"].append({
                    "account": account.display_name,
                    "status": "success",
                    "streak": account.checkin.streak_days,
                    "rewards": account.checkin.rewards,
                })
            else:
                results["failed"] += 1
                results["details"].append({
                    "account": account.display_name,
                    "status": "failed",
                    "error": result.get("error", "未知错误")
                })

        logger.info(f"批量签到完成: 成功{results['success']}, 失败{results['failed']}, 已签到{results['already']}")
        return results

    def checkin_platform(self, accounts: list[Account], platform: Platform, proxy: Optional[str] = None) -> dict:
        """对指定平台的所有账号签到"""
        platform_accounts = [a for a in accounts if a.platform == platform]
        return self.checkin_all(platform_accounts, proxy=proxy)
