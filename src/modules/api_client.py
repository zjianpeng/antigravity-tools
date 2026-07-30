"""API 客户端 - CodeBuddy/WorkBuddy 积分查询、签到等

两种认证模式：
1. JWT 模式（旧）：使用 Keycloak JWT access_token + X-User-Id + X-Domain
2. API Key 模式（新）：直接用 ck_xxx API Key 认证，更简单可靠

推荐使用 API Key 模式（from_api_key()），不需要 JWT 刷新，不需要 X-User-Id。

核心发现：
- 积分/签到 API 的 base URL 是 https://copilot.tencent.com
- 所有 /v2/billing/meter/* 接口必须使用 POST 方法
- API Key 模式：只需 Authorization: Bearer {api_key} + Content-Type + Accept
- JWT 模式：需要附加 X-User-Id 和 X-Domain 请求头

API 响应格式：
- 成功: {"code": 0, "msg": "OK", "data": {...}}
- 失败: {"code": <error_code>, "msg": "<error_msg>"}

数据结构（get-user-resource）：
- data.Response.Data.Accounts[]: 资源包列表
- 每个资源包包含: PackageName, CapacityRemain, CapacitySize, CycleStartTime 等
"""

import json
import logging
import random
import time
from datetime import datetime
from typing import Optional

import requests

from ..models import Account, ResourcePackage, CheckinStatus

logger = logging.getLogger(__name__)

# === API 基础 URL ===
# 关键：积分/签到 API 在 copilot.tencent.com，不在 codebuddy.cn
BILLING_API_BASE = "https://copilot.tencent.com"

# 公开 API 在 codebuddy.cn
PUBLIC_API_BASE = "https://codebuddy.cn"

# === API 路径 ===
BILLING_API_PATHS = {
    "user_resource": "/v2/billing/meter/get-user-resource",
    "payment_type": "/v2/billing/meter/get-payment-type",
    "checkin_status": "/v2/billing/meter/checkin-status",
    "daily_checkin": "/v2/billing/meter/daily-checkin",
    "dosage_notify": "/v2/billing/meter/get-dosage-notify",
}

PUBLIC_API_PATHS = {
    "config": "/v3/config",
    "activity_banner": "/v2/activity/banner",
}

# token 续期口（桌面端同款，实测可用；Keycloak 直连 refresh 恒 401，死路）
TOKEN_REFRESH_URL = "https://www.codebuddy.cn/v2/plugin/auth/token/refresh"


def _retry_delay(attempt: int) -> float:
    """Short jittered backoff for WorkBuddy APIs."""
    return min(0.6 * (2 ** (attempt - 1)), 3.0) + random.uniform(0, 0.35)


def _safe_response_text(resp: requests.Response, limit: int = 1000) -> str:
    try:
        return resp.text[:limit]
    except Exception:
        return ""


def check_api_key_chat_status(api_key: str, attempts: int = 3) -> dict:
    """Probe whether a WorkBuddy API key can start a chat request.

    Returns:
        {
            "success": bool,
            "status_text": str,
            "flag": "abnormal" | None,
            "http_status": int | None,
            "body": str,
        }
    """
    url = "https://copilot.tencent.com/v2/chat/completions"
    payload = {
        "model": "auto",
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hi"},
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {api_key}",
    }
    transient_statuses = {408, 500, 502, 503, 504}
    last_error = ""

    with requests.Session() as session:
        session.trust_env = False
        session.proxies = {"http": None, "https": None}

        for attempt in range(1, max(1, attempts) + 1):
            try:
                with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=(10, 30),
                    stream=True,
                ) as resp:
                    status = resp.status_code
                    if status == 200:
                        return {
                            "success": True,
                            "status_text": "正常",
                            "flag": None,
                            "http_status": status,
                            "body": "",
                        }

                    body = _safe_response_text(resp)
                    if status == 403 and '"code":11140' in body:
                        return {
                            "success": False,
                            "status_text": "风控异常",
                            "flag": "abnormal",
                            "http_status": status,
                            "body": body,
                        }
                    if status == 401 and "invalid_secret" in body:
                        return {
                            "success": False,
                            "status_text": "限流(401)",
                            "flag": "rate_limited",
                            "http_status": status,
                            "body": body,
                        }
                    if status == 429:
                        return {
                            "success": True,
                            "status_text": "限流(正常)",
                            "flag": None,
                            "http_status": status,
                            "body": body,
                        }
                    if status == 401:
                        # 401 但非 invalid_secret：也视为系统限流，标记 rate_limited
                        return {
                            "success": False,
                            "status_text": "限流(401)",
                            "flag": "rate_limited",
                            "http_status": status,
                            "body": body,
                        }

                    last_error = f"HTTP {status}: {body[:300]}"
                    if status in transient_statuses and attempt < attempts:
                        logger.warning(
                            "Key 状态检测临时失败，第 %s/%s 次重试: %s",
                            attempt,
                            attempts,
                            last_error,
                        )
                        time.sleep(_retry_delay(attempt))
                        continue

                    return {
                        "success": False,
                        "status_text": f"HTTP {status}",
                        "flag": None,
                        "http_status": status,
                        "body": body,
                    }
            except (requests.Timeout, requests.ConnectionError) as e:
                last_error = str(e)
                if attempt < attempts:
                    logger.warning(
                        "Key 状态检测网络失败，第 %s/%s 次重试: %s",
                        attempt,
                        attempts,
                        e,
                    )
                    time.sleep(_retry_delay(attempt))
                    continue
                return {
                    "success": False,
                    "status_text": f"异常: {e}",
                    "flag": None,
                    "http_status": None,
                    "body": last_error,
                }
            except requests.RequestException as e:
                last_error = str(e)
                logger.warning("Key 状态检测请求失败: %s", e)
                return {
                    "success": False,
                    "status_text": f"异常: {e}",
                    "flag": None,
                    "http_status": None,
                    "body": last_error,
                }

    return {
        "success": False,
        "status_text": "检测失败",
        "flag": None,
        "http_status": None,
        "body": last_error,
    }


class ApiClient:
    """CodeBuddy/WorkBuddy 平台 API 客户端

    支持两种认证模式：
    1. API Key 模式（推荐）：from_api_key("ck_xxx") — 只需 API Key，更简单
    2. JWT 模式（旧版）：from_account(account) — 需要 JWT token + uid + domain

    使用方法：
        # API Key 模式（推荐）
        client = ApiClient.from_api_key("ck_xxxxxxxxxx")
        result = client.get_user_resource()
        result = client.daily_checkin()

        # JWT 模式（旧版）
        client = ApiClient(access_token=token, uid=uid, domain="www.codebuddy.cn")
        result = client.get_user_resource()
    """

    def __init__(
        self,
        access_token: str,
        uid: str = "",
        domain: str = "www.codebuddy.cn",
        refresh_token: str = "",
        proxy: Optional[str] = None,
        account: Optional[Account] = None,
    ):
        """初始化 API 客户端（JWT 模式）

        Args:
            access_token: Keycloak JWT access token 或 API Key (ck_xxx)
            uid: 用户 UID（JWT 模式需要）
            domain: 域名（JWT 模式需要，默认 www.codebuddy.cn）
            refresh_token: Keycloak refresh token（用于自动刷新）
            proxy: HTTP 代理地址
            account: 可选的 Account 对象（用于兼容旧接口）
        """
        self.access_token = access_token
        self.uid = uid
        self.domain = domain
        self.refresh_token = refresh_token
        self.account = account

        # 检测是否为 API Key 模式（ck_ 开头的直接用 API Key 认证）
        self._is_api_key_mode = access_token.startswith("ck_")

        # 创建 HTTP session
        self.session = requests.Session()
        # 禁用系统代理：Win11 默认开 PAC/TUN 代理会导致请求被拦截或 SSL 证书验证失败
        self.session.trust_env = False
        self.session.proxies = {"http": None, "https": None}

        if self._is_api_key_mode:
            # API Key 模式：简单请求头，只需 Authorization
            self.session.headers.update({
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
        else:
            # JWT 模式：需要更多请求头
            self.session.headers.update({
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            # X-User-Id / X-Domain 仅在值安全时设置
            try:
                uid.encode("latin-1")
                self.session.headers["X-User-Id"] = uid
            except UnicodeEncodeError:
                logger.warning(f"UID 含非ASCII字符，跳过 X-User-Id header: {uid}")
            try:
                domain.encode("latin-1")
                self.session.headers["X-Domain"] = domain
            except UnicodeEncodeError:
                logger.warning(f"Domain 含非ASCII字符，跳过 X-Domain header: {domain}")

        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    @staticmethod
    def from_api_key(api_key: str, proxy: Optional[str] = None) -> "ApiClient":
        """从 API Key (ck_xxx) 创建客户端（推荐方式）

        API Key 模式优势：
        - 不需要 JWT token 刷新
        - 不需要 X-User-Id / X-Domain 头
        - 请求头简单，与官方一致

        Args:
            api_key: CodeBuddy API Key (ck_xxx 格式)
            proxy: HTTP 代理地址

        Returns:
            ApiClient 实例
        """
        return ApiClient(
            access_token=api_key,
            uid="",
            domain="",
            proxy=proxy,
        )

    def _billing_request(self, path: str, body: dict = None, retry_on_401: bool = True) -> Optional[dict]:
        """Send billing API request with short retries for transient upstream failures."""
        url = f"{BILLING_API_BASE}{path}"
        transient_statuses = {408, 429, 500, 502, 503, 504}
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.post(url, json=body or {}, timeout=(10, 30))
                body_preview = _safe_response_text(resp, 500)

                if resp.status_code == 401 and retry_on_401 and self.refresh_token and not self._is_api_key_mode:
                    logger.info("收到 401，尝试刷新 token 后重试...")
                    if self._refresh_token():
                        self.session.headers["Authorization"] = f"Bearer {self.access_token}"
                        retry_on_401 = False
                        continue
                    logger.warning("Token 刷新失败")
                    return None

                if not resp.ok:
                    is_checkin_already = resp.status_code == 400 and "daily-checkin" in path
                    log_level = logging.WARNING if is_checkin_already else logging.ERROR
                    logger.log(
                        log_level,
                        "API 非 2xx 响应 [POST %s] attempt=%s/%s status=%s body=%s",
                        path,
                        attempt,
                        max_attempts,
                        resp.status_code,
                        body_preview,
                    )

                    parsed = None
                    try:
                        parsed = resp.json()
                    except Exception:
                        parsed = None

                    if "daily-checkin" in path and parsed and parsed.get("code") == 10001:
                        return parsed

                    business_code = parsed.get("code") if isinstance(parsed, dict) else None
                    should_retry = resp.status_code in transient_statuses or business_code == 10000
                    if should_retry and attempt < max_attempts:
                        time.sleep(_retry_delay(attempt))
                        continue

                    if parsed:
                        logger.info(
                            "非 2xx JSON [POST %s] code=%s msg=%s",
                            path,
                            parsed.get("code"),
                            parsed.get("msg"),
                        )
                    return None

                result = resp.json()
                if result.get("code") != 0:
                    logger.error(
                        "API 返回错误 [%s]: code=%s, msg=%s",
                        path,
                        result.get("code"),
                        result.get("msg"),
                    )
                    if result.get("code") == 10000 and attempt < max_attempts:
                        time.sleep(_retry_delay(attempt))
                        continue
                    return None

                return result

            except (requests.Timeout, requests.ConnectionError) as e:
                logger.warning(
                    "API 请求网络失败 [POST %s] attempt=%s/%s: %s",
                    path,
                    attempt,
                    max_attempts,
                    e,
                )
                if attempt < max_attempts:
                    time.sleep(_retry_delay(attempt))
                    continue
                return None
            except requests.RequestException as e:
                logger.error(f"API 请求失败 [POST {path}]: {e}")
                return None
            except ValueError as e:
                logger.error(f"API 响应不是 JSON [POST {path}]: {e}")
                return None

        return None

    def _refresh_token(self) -> bool:
        """刷新 access token（走 console 后端续期口，2026-07-30 实测）

        桌面端同款接口（WorkBuddy 日志实锤）：只需 X-Refresh-Token 一个头。
        RT 无旋转无次数限制，每次刷新重新计 120 天。
        注意：Keycloak 标准 refresh（grant_type=refresh_token + client_id=console）
        恒 401 unauthorized_client（confidential client，secret 在 APISIX 网关）——死路勿回退。

        Returns:
            是否刷新成功
        """
        if not self.refresh_token:
            return False

        try:
            resp = self.session.post(
                TOKEN_REFRESH_URL,
                json={},
                headers={"X-Refresh-Token": self.refresh_token},
                timeout=15,
            )
            if resp.status_code == 200:
                result = resp.json().get("data") or {}
                if not result.get("accessToken"):
                    logger.warning(f"Token 刷新响应缺 accessToken: {resp.text[:100]}")
                    return False
                old_token = self.access_token
                self.access_token = result["accessToken"]
                self.refresh_token = result.get("refreshToken", self.refresh_token)
                self.session.headers["Authorization"] = f"Bearer {self.access_token}"
                self._persist_tokens(old_token)
                logger.info("Token 刷新成功")
                return True
            else:
                logger.warning(f"Token 刷新失败: {resp.status_code} {resp.text[:100]}")
                return False
        except Exception as e:
            logger.error(f"Token 刷新异常: {e}")
            return False

    def _persist_tokens(self, old_token: str = ""):
        """刷新成功后持久化新 token（不持久化的话，重启后续期链就断了）

        1. 回写 accounts 表（auth_token + auth_raw）；
        2. 同步内存中的 Account 对象；
        3. 上游 Key 池里持有旧 token 的 Key 一并换成新 token
           （旧 token 不吊销、到期前仍可用，但池与账号保持一致更干净）。
        任何一步失败只记日志，不影响本次会话（内存 token 已是新的）。
        """
        if not self.account or not self.account.uid:
            return

        try:
            from ..utils.store import update_account_tokens
            update_account_tokens(self.account.uid, self.access_token, self.refresh_token)
        except Exception as e:
            logger.error(f"Token 回写 accounts 表失败 uid={self.account.uid}: {e}")

        # 内存 Account 同步，避免调用方拿到旧 token
        self.account.auth_token = self.access_token
        try:
            raw = json.loads(self.account.auth_raw) if self.account.auth_raw else {}
        except ValueError:
            raw = {}
        raw["accessToken"] = self.access_token
        if self.refresh_token:
            raw["refreshToken"] = self.refresh_token
        self.account.auth_raw = json.dumps(raw)

        # 池里的 Key 同步换新（懒加载 proxy_server，避免模块级耦合）
        if old_token and old_token != self.access_token:
            try:
                from .proxy_server import ProxyDatabase
                proxy_db = ProxyDatabase.get_instance()
                synced = 0
                for k in proxy_db.get_upstream_keys():
                    if k.get("api_key") == old_token:
                        proxy_db.update_upstream_key(k.get("key_id", ""), {"api_key": self.access_token})
                        synced += 1
                if synced:
                    proxy_db._flush_to_disk()
                    logger.info(f"上游 Key 池已同步新 token（{synced} 个 Key）")
            except Exception as e:
                logger.error(f"上游 Key 池同步新 token 失败: {e}")

    # === 积分查询 API ===

    def get_user_resource(self) -> dict:
        """获取用户资源包（积分）信息

        返回的 Accounts 列表中每个元素包含：
        - PackageName: 资源包名称（如 "CodeBuddy个人体验版"）
        - PackageType: 资源包类型（1=免费, 2=付费, 4=体验）
        - CapacityUnit: 单位（credits）
        - CapacitySize: 总量
        - CapacityRemain: 剩余
        - CapacityUsed: 已用
        - CycleStartTime / CycleEndTime: 当前计费周期
        - CapacitySizePrecise / CapacityRemainPrecise / CapacityUsedPrecise: 精确值

        Returns:
            {"success": True, "packages": [...], "total_credits": int, "remaining_credits": int}
            或 {"success": False, "error": str}
        """
        result = self._billing_request(BILLING_API_PATHS["user_resource"])
        if not result:
            return {"success": False, "error": "请求失败"}

        # ⚠️ 双重校验：code != 0 视为失败（防止上游 500 返回 {"code":10000} 被 truthy 判断通过）
        if result.get("code") != 0:
            logger.warning(f"[积分查询] 上游返回 code={result.get('code')}, msg={result.get('msg')}, 不更新积分")
            return {"success": False, "error": f"上游错误: code={result.get('code')}"}

        try:
            response_data = result.get("data", {}).get("Response", {}).get("Data", {})
            accounts = response_data.get("Accounts", [])
            total_count = response_data.get("TotalCount", 0)
            total_dosage = response_data.get("TotalDosage", 0)

            packages = []
            total_credits = 0
            remaining_credits = 0

            for acc in accounts:
                pkg = ResourcePackage(
                    package_name=acc.get("PackageName", ""),
                    package_type=str(acc.get("PackageType", "")),
                    product_name=acc.get("ProductName", ""),
                    sub_product_name=acc.get("SubProductName", ""),
                    capacity_unit=acc.get("CapacityUnit", "credits"),
                    capacity_size=float(acc.get("CapacitySizePrecise", acc.get("CapacitySize", 0))),
                    capacity_remain=float(acc.get("CapacityRemainPrecise", acc.get("CapacityRemain", 0))),
                    capacity_used=float(acc.get("CapacityUsedPrecise", acc.get("CapacityUsed", 0))),
                    cycle_size=float(acc.get("CycleCapacitySizePrecise", acc.get("CycleCapacitySize", 0))),
                    cycle_remain=float(acc.get("CycleCapacityRemainPrecise", acc.get("CycleCapacityRemain", 0))),
                    cycle_start=acc.get("CycleStartTime", ""),
                    cycle_end=acc.get("CycleEndTime", ""),
                    status=acc.get("Status", 0),
                    resource_id=acc.get("ResourceId", ""),
                )
                packages.append(pkg)

                # 累加 credits
                if pkg.capacity_unit == "credits":
                    total_credits += pkg.cycle_size
                    remaining_credits += pkg.cycle_remain

            return {
                "success": True,
                "packages": packages,
                "total_count": total_count,
                "total_dosage": total_dosage,
                "total_credits": total_credits,
                "remaining_credits": remaining_credits,
            }

        except Exception as e:
            logger.error(f"解析用户资源数据失败: {e}")
            return {"success": False, "error": f"解析失败: {e}"}

    def get_payment_type(self) -> dict:
        """获取付费类型

        Returns:
            {"success": True, "payment_type": "free"|"pro"|"team"|"enterprise"}
        """
        result = self._billing_request(BILLING_API_PATHS["payment_type"])
        if result:
            data = result.get("data", {})
            return {"success": True, "payment_type": data.get("paymentType", "unknown")}
        return {"success": False, "error": "获取付费类型失败"}

    def get_checkin_status(self) -> dict:
        """获取签到状态

        Returns:
            CheckinStatus 的字典形式
        """
        result = self._billing_request(BILLING_API_PATHS["checkin_status"])
        if not result:
            return {"success": False, "error": "获取签到状态失败"}

        try:
            data = result.get("data", {})
            status = CheckinStatus(
                active=data.get("active", False),
                today_checked_in=data.get("today_checked_in", False),
                streak_days=data.get("streak_days", 0),
                daily_credit=data.get("daily_credit", 0),
                today_credit=data.get("today_credit", 0),
                is_streak_day=data.get("is_streak_day", False),
                next_streak_day=data.get("next_streak_day", 0),
                streak_bonus_days=data.get("streak_bonus_days", 0),
                streak_bonus_credit=data.get("streak_bonus_credit", 0),
                week_checkin_days=data.get("week_checkin_days", 0),
                week_progress=data.get("week_progress", [False]*7),
                total_credits=data.get("total_credits", 0),
                activity_name=data.get("activity_name", ""),
            )
            return {"success": True, "data": status}
        except Exception as e:
            logger.error(f"解析签到状态失败: {e}")
            return {"success": False, "error": f"解析失败: {e}"}

    def daily_checkin(self) -> dict:
        """执行每日签到

        API 行为：
        - 签到成功: HTTP 200, code=0, data 含 credit/streak_days
        - 今日已签到: HTTP 400, code=10001, msg="今天已签到，请明天再来"

        Returns:
            {"success": True, "credit": int, "streak_days": int}
            {"success": True, "already": True}  -- 今日已签到
            或 {"success": False, "error": str}
        """
        result = self._billing_request(BILLING_API_PATHS["daily_checkin"])
        if not result:
            return {"success": False, "error": "签到请求失败"}

        code = result.get("code", -1)
        msg = result.get("msg", "")

        # 签到成功 (code=0)
        if code == 0:
            data = result.get("data", {})
            return {
                "success": True,
                "credit": data.get("credit", 0),
                "streak_days": data.get("streak_days", 0),
                "is_streak_day": data.get("is_streak_day", False),
            }

        # 已签到：code=10001 是服务端返回的"今天已签到"错误码
        if code == 10001:
            logger.info(f"今日已签到: code={code}, msg={msg}")
            return {"success": True, "already": True}

        # 其他常见的已签到关键词检测
        already_keywords = ["already", "已签", "已领", "重复签到", "今日已"]
        if any(kw in msg.lower() for kw in [k.lower() for kw in already_keywords]):
            logger.info(f"今日已签到(关键词): code={code}, msg={msg}")
            return {"success": True, "already": True}

        # 其他业务错误
        logger.warning(f"签到返回业务错误: code={code}, msg={msg}")
        return {"success": False, "error": f"签到失败: {msg} (code={code})"}

    # === 兼容旧接口 ===

    def checkin(self) -> dict:
        """兼容旧接口 - 执行每日签到"""
        result = self.daily_checkin()
        if result["success"]:
            return {"success": True, "data": result}
        return result

    def get_quota(self) -> dict:
        """兼容旧接口 - 获取配额信息"""
        return self.get_user_resource()

    def verify_token(self) -> bool:
        """验证 token 是否有效（通过获取付费类型来验证）"""
        result = self._billing_request(BILLING_API_PATHS["payment_type"], retry_on_401=False)
        return result is not None

    @staticmethod
    def from_account(account: Account, proxy: Optional[str] = None) -> "ApiClient":
        """从 Account 对象创建 ApiClient

        Args:
            account: 账号对象
            proxy: HTTP 代理地址

        Returns:
            ApiClient 实例
        """
        # 从 auth_raw（{"accessToken":..., "refreshToken":...}）抠 refresh_token 用于自动续期
        refresh_token = ""
        if account.auth_raw:
            try:
                refresh_token = json.loads(account.auth_raw).get("refreshToken", "")
            except (ValueError, AttributeError):
                pass
        return ApiClient(
            access_token=account.auth_token,
            uid=account.uid,
            domain=account.domain or "www.codebuddy.cn",
            refresh_token=refresh_token,
            proxy=proxy,
            account=account,
        )
