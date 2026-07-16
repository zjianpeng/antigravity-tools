"""OAuth 认证模块 - WorkBuddy/CodeBuddy Keycloak SSO 登录

核心认证流程：
1. WorkBuddy 使用 Keycloak SSO，通过系统浏览器登录
2. 新版 WorkBuddy 登录后写入 CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info（明文 JSON）
3. 该文件包含完整的账号信息（uid, nickname, phone）和认证信息（accessToken, refreshToken）
4. 不再需要读取或解密旧版 state.vscdb
5. 本模块支持三种方式获取账号：
   a. extract_current_session() - 从 workbuddy-desktop.info 提取当前登录会话
   b. login_new_account() - 完整流程：关闭WB → 注销SSO → 清除认证 → 重启WB → 轮询检测
   c. 从备份目录 workbuddy-desktop.*.info 导入历史账号

重要：login_new_account() 必须关闭并重启 WorkBuddy，因为：
- 浏览器登录 codebuddy.cn 不会自动产生 workbuddy-desktop.info
- workbuddy-desktop.info 是 WorkBuddy 客户端在 Keycloak 登录成功后自己写的
- 必须清除旧认证数据（16个位置），否则 WB 重启后自动登录旧账号
"""

import base64
import json
import logging
import os
import platform
import shutil
import sqlite3
import subprocess
import time
import webbrowser
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# === 平台检测 ===
IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

# === 路径常量（跨平台）===
USER_PROFILE = os.path.expanduser("~")

if IS_MACOS:
    APPDATA = os.path.join(USER_PROFILE, "Library", "Application Support")
    LOCALAPPDATA = APPDATA
    WORKBUDDY_HOME = os.path.join(USER_PROFILE, ".workbuddy")
    WORKBUDDY_ROAMING = os.path.join(APPDATA, "WorkBuddy")
    CODEBUDDY_EXT_AUTH_DIR = os.path.join(APPDATA, "CodeBuddyExtension", "Data", "Public", "auth")
    WORKBUDDY_EXE = "/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy"
else:
    APPDATA = os.environ.get("APPDATA", os.path.join(USER_PROFILE, "AppData", "Roaming"))
    LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.join(USER_PROFILE, "AppData", "Local"))
    WORKBUDDY_HOME = os.path.join(USER_PROFILE, ".workbuddy")
    WORKBUDDY_ROAMING = os.path.join(APPDATA, "WorkBuddy")
    CODEBUDDY_EXT_AUTH_DIR = os.path.join(LOCALAPPDATA, "CodeBuddyExtension", "Data", "Public", "auth")
    WORKBUDDY_EXE = os.path.join(LOCALAPPDATA, "Programs", "WorkBuddy", "WorkBuddy.exe")

# 新版认证文件（优先读取）
WORKBUDDY_DESKTOP_INFO = os.path.join(CODEBUDDY_EXT_AUTH_DIR, "workbuddy-desktop.info")

# 旧版认证文件（兼容回退）
NEODATA_TOKEN = os.path.join(WORKBUDDY_HOME, ".neodata_token")
LOCAL_STATE = os.path.join(WORKBUDDY_ROAMING, "Local State")
STORAGE_JSON = os.path.join(WORKBUDDY_ROAMING, "User", "globalStorage", "storage.json")
STATE_VSCDB = os.path.join(WORKBUDDY_ROAMING, "User", "globalStorage", "state.vscdb")
STATE_VSCDB_BACKUP = os.path.join(WORKBUDDY_ROAMING, "User", "globalStorage", "state.vscdb.backup")
WORKBUDDY_DB = os.path.join(WORKBUDDY_HOME, "workbuddy.db")
SESSIONS_VSCDB = os.path.join(WORKBUDDY_ROAMING, "codebuddy-sessions.vscdb")
# WORKBUDDY_EXE 已在上方跨平台定义

# state.vscdb 中的 AccessToken key（旧版）
ACCESS_TOKEN_SECRET_KEY = (
    'secret://{"extensionId":"tencent-cloud.coding-copilot",'
    '"key":"planning-genie.new.accessTokencn"}'
)

# 内嵌浏览器 session 目录
APP_SESSION_DIR = os.path.join(WORKBUDDY_HOME, "app", "session")
LOCAL_STORAGE_DIR = os.path.join(WORKBUDDY_HOME, "local_storage")

# Roaming 下需要清除的浏览器会话目录
ROAMING_SESSION_DIRS = [
    os.path.join(WORKBUDDY_ROAMING, "Network"),
    os.path.join(WORKBUDDY_ROAMING, "Session Storage"),
    os.path.join(WORKBUDDY_ROAMING, "Local Storage"),
    os.path.join(WORKBUDDY_ROAMING, "Partitions"),
    os.path.join(WORKBUDDY_ROAMING, "Service Worker"),
    os.path.join(WORKBUDDY_ROAMING, "Cache"),
    os.path.join(WORKBUDDY_ROAMING, "WebStorage"),
    os.path.join(WORKBUDDY_ROAMING, "blob_storage"),
    os.path.join(WORKBUDDY_ROAMING, "IndexedDB"),
]

# Cookies
COOKIES = [
    os.path.join(WORKBUDDY_ROAMING, "Network", "Cookies"),
    os.path.join(WORKBUDDY_HOME, "app", "session", "Network", "Cookies"),
]

# 需要清除缓存的关键词
CACHE_KEYWORDS = [
    "CodeBuddy-Product-Cache",
    "cloud_product_config_cache",
    "ACC_SHARE_CACHE_PRODUCT_MODELS",
    "ACC_SHARE_CACHE_PRODUCT_REMOTE_AGENTS",
    "CodeBuddy-LLMDataReportCACHE",
    "CodeBuddy-Endpoint-Cache",
]


def _b64url_decode(s: str) -> bytes:
    """Base64url 解码"""
    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def decode_jwt(token: str) -> dict:
    """解码 JWT token（不验证签名）"""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise ValueError(f"无效的 JWT 格式")
    return json.loads(_b64url_decode(parts[1]))


class WorkBuddyProcess:
    """WorkBuddy 进程管理（跨平台）"""

    PROCESS_NAME = "WorkBuddy"  # macOS: "WorkBuddy", Windows: "WorkBuddy.exe"

    @staticmethod
    def is_running() -> bool:
        try:
            if IS_MACOS:
                result = subprocess.run(
                    ["pgrep", "-x", "WorkBuddy"],
                    capture_output=True, text=True, timeout=5,
                )
                return result.returncode == 0
            else:
                result = subprocess.run(
                    ["tasklist"], capture_output=True, text=True, timeout=5,
                )
                return "WorkBuddy.exe" in result.stdout
        except Exception:
            return False

    @staticmethod
    def kill() -> bool:
        """终止 WorkBuddy 进程（先优雅，再强制）"""
        try:
            if IS_MACOS:
                # macOS: 先 SIGTERM
                subprocess.run(["pkill", "-x", "WorkBuddy"], capture_output=True, timeout=5)
                time.sleep(2)
                if WorkBuddyProcess.is_running():
                    logger.info("WorkBuddy 未响应优雅关闭，强制终止...")
                    subprocess.run(["pkill", "-9", "-x", "WorkBuddy"], capture_output=True, timeout=5)
                    time.sleep(2)
            else:
                # Windows: taskkill
                subprocess.run(
                    ["taskkill", "/IM", "WorkBuddy.exe"],
                    capture_output=True, timeout=5,
                )
                time.sleep(2)
                if WorkBuddyProcess.is_running():
                    logger.info("WorkBuddy 未响应优雅关闭，强制终止...")
                    subprocess.run(
                        ["taskkill", "/F", "/IM", "WorkBuddy.exe"],
                        capture_output=True, timeout=5,
                    )
                    time.sleep(2)

            if WorkBuddyProcess.is_running():
                logger.error("无法终止 WorkBuddy 进程")
                return False

            logger.info("WorkBuddy 已终止")
            return True
        except Exception as e:
            logger.error(f"终止 WorkBuddy 失败: {e}")
            return False

    @staticmethod
    def start() -> bool:
        """启动 WorkBuddy"""
        exe_path = WORKBUDDY_EXE
        if IS_MACOS:
            exe_path = "/Applications/WorkBuddy.app"
            if not os.path.exists(exe_path):
                logger.error(f"WorkBuddy 未安装: {exe_path}")
                return False
        else:
            if not os.path.exists(exe_path):
                logger.error(f"WorkBuddy 未安装: {exe_path}")
                return False
        try:
            if IS_MACOS:
                subprocess.Popen(
                    ["open", "-a", "WorkBuddy"],
                    close_fds=True,
                )
            else:
                subprocess.Popen(
                    [exe_path],
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
            logger.info("WorkBuddy 已启动")
            return True
        except Exception as e:
            logger.error(f"启动 WorkBuddy 失败: {e}")
            return False


class WorkBuddyAuth:
    """WorkBuddy/CodeBuddy 认证管理器

    支持两种获取账号的方式：
    1. extract_current_session() - 从当前已登录的会话中提取
    2. login_new_account() - 完整流程（关闭WB → 清除认证 → 重启WB → 轮询检测）
    """

    @staticmethod
    def extract_current_session() -> Optional[dict]:
        """从当前已登录的 WorkBuddy 会话中提取账号信息

        读取优先级：
        1. workbuddy-desktop.info（新版，明文 JSON，包含完整账号+认证信息）
        2. .neodata_token + state.vscdb（旧版，兼容回退）

        Returns:
            账号信息字典，包含:
            - neodata_token / access_token: JWT access token
            - refresh_token: 刷新令牌（新版）
            - uid: WorkBuddy 用户 ID
            - nickname: 昵称
            - preferred_username: Keycloak 用户名
            - keycloak_sub: Keycloak subject
            - token_expires_at: 过期时间戳
            - phone_number: 手机号（新版）
            如果未登录返回 None
        """
        # === 优先读取新版 workbuddy-desktop.info ===
        if os.path.exists(WORKBUDDY_DESKTOP_INFO):
            try:
                with open(WORKBUDDY_DESKTOP_INFO, "r", encoding="utf-8") as f:
                    info = json.load(f)

                account = info.get("account", {})
                auth = info.get("auth", {})
                access_token = auth.get("accessToken", "")

                if not access_token:
                    logger.debug("workbuddy-desktop.info 中 accessToken 为空")
                else:
                    # 解码 JWT 获取更多信息
                    payload = decode_jwt(access_token)
                    keycloak_sub = payload.get("sub", "")
                    preferred_username = payload.get("preferred_username", "")
                    token_expires_at = payload.get("exp", 0)

                    # 检查 token 是否过期
                    if token_expires_at and token_expires_at < time.time():
                        logger.info("workbuddy-desktop.info 中的 access token 已过期")
                        # 过期不直接返回 None，仍返回信息让上层判断
                    nickname = account.get("nickname", "") or preferred_username
                    uid = account.get("uid", "") or keycloak_sub
                    phone = account.get("phoneNumber", "")

                    result = {
                        "access_token": access_token,
                        "neodata_token": access_token,  # 兼容旧字段名
                        "refresh_token": auth.get("refreshToken", ""),
                        "uid": uid,
                        "nickname": nickname,
                        "preferred_username": preferred_username,
                        "keycloak_sub": keycloak_sub,
                        "token_expires_at": token_expires_at,
                        "phone_number": phone,
                        "source": "workbuddy-desktop.info",
                    }

                    logger.info(f"提取到当前会话: {nickname} (UID={uid[:8] if uid else 'N/A'}...)")
                    return result

            except Exception as e:
                logger.error(f"读取 workbuddy-desktop.info 失败: {e}")

        # === 回退到旧版 .neodata_token ===
        try:
            if not os.path.exists(NEODATA_TOKEN):
                logger.debug("未找到 workbuddy-desktop.info 和 .neodata_token，当前未登录")
                return None

            with open(NEODATA_TOKEN, "r", encoding="utf-8") as f:
                token = f.read().strip()

            if not token:
                logger.debug(".neodata_token 为空")
                return None

            # 解码 JWT
            payload = decode_jwt(token)
            keycloak_sub = payload.get("sub", "")
            preferred_username = payload.get("preferred_username", "")
            token_expires_at = payload.get("exp", 0)

            # 检查 token 是否过期
            if token_expires_at and token_expires_at < time.time():
                logger.info("JWT token 已过期")
                return None

            # 读取 storage.json 获取 WorkBuddy UID
            uid = ""
            if os.path.exists(STORAGE_JSON):
                try:
                    with open(STORAGE_JSON, "r", encoding="utf-8") as f:
                        storage = json.load(f)
                    uid = storage.get("genie.userId", "")
                except Exception:
                    pass

            # 尝试从 state.vscdb 读取更多账号信息
            nickname = preferred_username
            phone = ""
            try:
                if os.path.exists(STATE_VSCDB):
                    conn = sqlite3.connect(STATE_VSCDB)
                    row = conn.execute(
                        "SELECT value FROM ItemTable WHERE key = ?",
                        (ACCESS_TOKEN_SECRET_KEY,)
                    ).fetchone()
                    conn.close()

                    if row:
                        raw = row[0]
                        if isinstance(raw, str) and raw.startswith('{"type":"Buffer"'):
                            buf = json.loads(raw)
                            blob = bytes(buf["data"])
                        elif isinstance(raw, bytes):
                            blob = raw
                        else:
                            blob = None

                        if blob:
                            try:
                                data = _decrypt_vscdb_secret(blob)
                                if isinstance(data, dict):
                                    account_info = data.get("account", {})
                                    nickname = account_info.get("nickname", nickname)
                                    phone = account_info.get("phoneNumber", "")
                                    if not uid:
                                        uid = account_info.get("uid", "")
                            except Exception as e:
                                logger.debug(f"解密 AccessToken 失败（非致命）: {e}")
            except Exception as e:
                logger.debug(f"读取 state.vscdb 失败（非致命）: {e}")

            result = {
                "neodata_token": token,
                "access_token": token,
                "refresh_token": "",
                "uid": uid,
                "nickname": nickname or preferred_username,
                "preferred_username": preferred_username,
                "keycloak_sub": keycloak_sub,
                "token_expires_at": token_expires_at,
                "phone_number": phone,
                "source": ".neodata_token",
            }

            logger.info(f"提取到当前会话（旧版）: {nickname or preferred_username} (UID={uid[:8] if uid else 'N/A'}...)")
            return result

        except Exception as e:
            logger.error(f"提取当前会话失败: {e}")
            return None

    @staticmethod
    def login_new_account(
        on_status: Optional[Callable[[str], None]] = None,
        on_confirm: Optional[Callable[[], bool]] = None,
        timeout: int = 300,
        poll_interval: int = 3,
    ) -> Optional[dict]:
        """登录新账号（完整流程）

        流程：
        1. 保存当前账号信息（内存记录）
        2. 关闭 WorkBuddy（必须，否则文件被锁）
        3. 注销 Keycloak SSO（否则浏览器自动登录旧账号）
        4. 清除所有认证文件（17个位置，含新版 workbuddy-desktop.info）
        5. 启动 WorkBuddy（弹出浏览器让用户登录新账号）
        6. 轮询检测 workbuddy-desktop.info 或 .neodata_token 变化
        7. 返回新账号信息

        Args:
            on_status: 状态回调函数 (status_text: str)
            on_confirm: 确认回调（用于 UI 弹窗确认是否关闭 WorkBuddy）
            timeout: 超时秒数
            poll_interval: 轮询间隔秒数

        Returns:
            新账号信息字典，超时返回 None
        """
        # 记录当前账号信息
        old_sub = ""
        old_token = ""
        current = WorkBuddyAuth.extract_current_session()
        if current:
            old_sub = current.get("keycloak_sub", "")
            old_token = current.get("neodata_token", "") or current.get("access_token", "")

        # === 阶段 1: 关闭 WorkBuddy ===
        if WorkBuddyProcess.is_running():
            if on_status:
                on_status("正在关闭 WorkBuddy（必须关闭才能切换账号）...")

            if on_confirm and not on_confirm():
                if on_status:
                    on_status("用户取消，登录中止")
                return None

            if not WorkBuddyProcess.kill():
                if on_status:
                    on_status("❌ 无法关闭 WorkBuddy，请手动关闭后重试")
                return None

            # 等待进程完全退出 + 文件锁释放
            time.sleep(3)

        # === 阶段 2: 注销 Keycloak SSO ===
        if on_status:
            on_status("注销 Keycloak SSO 会话（防止自动登录旧账号）...")
        _logout_keycloak_sso(current)
        time.sleep(2)

        # === 阶段 3: 清除所有认证文件 ===
        if on_status:
            on_status("清除登录状态（含新版 workbuddy-desktop.info）...")
        _clear_all_auth()

        # === 阶段 4: 启动 WorkBuddy ===
        if on_status:
            on_status("启动 WorkBuddy，请在浏览器中登录新账号...")
        if not WorkBuddyProcess.start():
            if on_status:
                on_status("❌ 启动 WorkBuddy 失败，请检查是否已安装")
            return None

        # === 阶段 5: 轮询检测新登录 ===
        if on_status:
            on_status("等待新账号登录...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            time.sleep(poll_interval)

            new_session = WorkBuddyAuth.extract_current_session()
            if new_session:
                new_sub = new_session.get("keycloak_sub", "")
                # 情况1：检测到新的 sub（不同账号登录）
                if new_sub and new_sub != old_sub:
                    if on_status:
                        on_status(f"✅ 登录成功: {new_session.get('nickname', '新账号')}")
                    logger.info(f"检测到新登录: {new_session.get('nickname')}")
                    return new_session
                # 情况2：token 内容变化了（同账号重新登录也算）
                new_token = new_session.get("neodata_token", "") or new_session.get("access_token", "")
                if new_token and new_token != old_token:
                    if on_status:
                        on_status(f"✅ 检测到登录变化: {new_session.get('nickname', '账号')}")
                    logger.info(f"检测到 token 变化: {new_session.get('nickname')}")
                    return new_session

        if on_status:
            on_status("❌ 登录超时（5分钟）")
        logger.warning("登录超时")
        return None


# ============================================================
# 以下是内部辅助函数
# ============================================================

def _decrypt_vscdb_secret(blob: bytes) -> dict:
    """解密 state.vscdb 中的 secret 条目（跨平台）

    Windows: DPAPI + AESGCM
    macOS: Keychain + AESGCM
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    # 1. 获取 master key
    if not os.path.exists(LOCAL_STATE):
        raise FileNotFoundError(f"Local State 文件不存在: {LOCAL_STATE}")

    with open(LOCAL_STATE, "r", encoding="utf-8") as f:
        local_state = json.load(f)

    encrypted_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key", "")
    if not encrypted_key_b64:
        raise RuntimeError("Local State 中未找到 os_crypt.encrypted_key")

    encrypted_key = base64.b64decode(encrypted_key_b64)

    if IS_MACOS:
        # macOS: 前 3 字节是 "v11" 前缀，后面是 Keychain 加密的 key
        # 但实际上 macOS Electron 用的是 Keychain Access，不经过 DPAPI
        # 这里需要用 security 命令解密，或直接用 PyObjC
        # 简化实现：对于 macOS，WorkBuddy 使用新版 workbuddy-desktop.info，
        # 不需要走 vscdb 解密路径
        import subprocess as _sp
        # 尝试用 security 命令解密（Electron macOS 标准做法）
        encrypted_key_data = encrypted_key  # 包含 "v11" 前缀
        try:
            result = _sp.run(
                ["security", "find-generic-password",
                 "-s", "WorkBuddy", "-a", "WorkBuddy", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                master_key = base64.b64decode(result.stdout.strip())
            else:
                raise RuntimeError("无法从 Keychain 获取 master key")
        except Exception as e:
            raise RuntimeError(f"macOS Keychain 解密失败: {e}")
    else:
        # Windows: DPAPI
        if encrypted_key[:5] != b"DPAPI":
            raise RuntimeError("encrypted_key 格式异常，缺少 DPAPI 前缀")
        import win32crypt
        master_key = win32crypt.CryptUnprotectData(encrypted_key[5:], None, None, None, 0)[1]

    # 2. 解密 blob
    if blob[:3] != b"v10":
        raise ValueError("加密数据格式异常，缺少 v10 前缀")

    data = blob[3:]
    nonce = data[:12]
    ciphertext = data[12:]

    aesgcm = AESGCM(master_key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode("utf-8"))


def _logout_keycloak_sso(current_session: Optional[dict] = None):
    """注销系统浏览器中的 Keycloak SSO 会话

    这是解决"切换账号后自动登录旧账号"的关键步骤。
    即使清除了所有本地认证数据，WorkBuddy 启动后仍会通过浏览器 SSO 自动登录旧账号。
    """
    try:
        if not current_session:
            return

        token = current_session.get("neodata_token", "")
        if not token:
            return

        payload = decode_jwt(token)
        issuer = payload.get("iss", "")
        if issuer:
            logout_url = f"{issuer.rstrip('/')}/protocol/openid-connect/logout"
            webbrowser.open(logout_url)
            time.sleep(3)
            logger.info(f"Keycloak SSO 注销请求已发送")
    except Exception as e:
        logger.debug(f"SSO 注销失败（非致命）: {e}")


def _clear_all_auth():
    """清除所有认证文件，让 WorkBuddy 回到未登录状态

    覆盖所有已知残留位置（17个位置）：
    0. workbuddy-desktop.info — 新版登录文件（明文 JSON，含 accessToken + refreshToken）
    1. .neodata_token — JWT token（旧版）
    2. state.vscdb AccessToken — 加密的账号凭证
    3. storage.json genie.userId — 用户标识
    4. .workbuddy/local_storage/ — 存储 userId 和 agent 配置的关键目录
    5. 内嵌浏览器整个 session 目录
    6. 主进程 Roaming 下所有浏览器会话目录
    7. state.vscdb Tencent-Cloud.coding-copilot 缓存
    8. 所有 secret:// 条目
    9. settings.json 中的 claw.channels
    10. .workbuddy/memory/ 中以 userId 命名的记忆文件
    11. .workbuddy/memery/ 中以 userId 命名的 memery 文件
    12. .workbuddy/sessions/ 目录
    13. state.vscdb __$__targetStorageMarker
    14. workbuddy.db sessions 表
    15. codebuddy-sessions.vscdb session 记录
    16. state.vscdb.backup 中所有认证相关数据
    """

    # 0. 删除新版 workbuddy-desktop.info
    if os.path.exists(WORKBUDDY_DESKTOP_INFO):
        try:
            os.remove(WORKBUDDY_DESKTOP_INFO)
            logger.debug("已删除 workbuddy-desktop.info")
        except Exception as e:
            logger.debug(f"删除 workbuddy-desktop.info 失败: {e}")

    # 1. 删除 .neodata_token
    if os.path.exists(NEODATA_TOKEN):
        os.remove(NEODATA_TOKEN)
        logger.debug("已删除 .neodata_token")

    # 2. 删除 state.vscdb 中的 AccessToken
    _delete_from_vscdb(STATE_VSCDB, ACCESS_TOKEN_SECRET_KEY)

    # 3. 清除 storage.json 中的 genie.userId
    if os.path.exists(STORAGE_JSON):
        try:
            with open(STORAGE_JSON, "r", encoding="utf-8") as f:
                storage = json.load(f)
            if "genie.userId" in storage:
                del storage["genie.userId"]
                with open(STORAGE_JSON, "w", encoding="utf-8") as f:
                    json.dump(storage, f, indent=2, ensure_ascii=False)
                logger.debug("已清除 genie.userId")
        except Exception as e:
            logger.debug(f"清除 genie.userId 失败: {e}")

    # 4. 删除 .workbuddy/local_storage/ 目录
    _force_rmtree(LOCAL_STORAGE_DIR, "local_storage")

    # 5. 删除内嵌浏览器整个 session 目录
    _force_rmtree(APP_SESSION_DIR, "内嵌浏览器 session")

    # 6. 删除主进程 Roaming 下所有浏览器会话目录
    for dir_path in ROAMING_SESSION_DIRS:
        if os.path.exists(dir_path):
            try:
                shutil.rmtree(dir_path)
                logger.debug(f"已删除主进程会话目录: {os.path.basename(dir_path)}")
            except Exception as e:
                logger.debug(f"删除主进程会话目录失败 {dir_path}: {e}")

    # 7. 清除 state.vscdb 中的产品配置缓存
    _delete_from_vscdb(STATE_VSCDB, "Tencent-Cloud.coding-copilot")

    # 8. 清除所有 secret:// 条目（保留 AccessToken 已在步骤2删除）
    _clear_secret_entries(STATE_VSCDB)

    # 9. 清除 settings.json 中的 claw.channels
    _clear_claw_channels()

    # 10. 删除 .workbuddy/memory/ 中以 userId 命名的文件
    _clear_user_id_files(os.path.join(WORKBUDDY_HOME, "memory"))

    # 11. 删除 .workbuddy/memery/ 中以 userId 命名的文件
    _clear_user_id_files(os.path.join(WORKBUDDY_HOME, "memery"))

    # 12. 删除 .workbuddy/sessions/ 目录
    _force_rmtree(os.path.join(WORKBUDDY_HOME, "sessions"), "sessions")

    # 13. 清除 __$__targetStorageMarker
    _delete_from_vscdb(STATE_VSCDB, "__$__targetStorageMarker")

    # 14. 清除 workbuddy.db 中的 sessions 表
    _clear_workbuddy_db_sessions()

    # 15. 清除 codebuddy-sessions.vscdb
    _clear_codebuddy_sessions_vscdb()

    # 16. 清除 state.vscdb.backup 中所有认证相关数据
    _clear_state_vscdb_backup()

    logger.info("所有认证文件已清除（17个位置，含新版 workbuddy-desktop.info）")


# ============================================================
# vscdb 操作辅助函数
# ============================================================

def _delete_from_vscdb(db_path: str, key: str):
    """从 vscdb 删除指定 key"""
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM ItemTable WHERE key = ?", (key,))
        conn.commit()
        conn.close()
        logger.debug(f"已从 vscdb 删除: {key[:50]}...")
    except Exception as e:
        logger.debug(f"从 vscdb 删除失败: {e}")


def _clear_secret_entries(db_path: str):
    """清除 state.vscdb 中的缓存 secret 条目"""
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT key FROM ItemTable WHERE key LIKE 'secret://%'")
        all_keys = [row[0] for row in cursor.fetchall()]
        deleted = 0
        for key in all_keys:
            # 跳过 AccessToken（已在步骤2单独删除）
            if "accessTokencn" in key:
                continue
            cursor.execute("DELETE FROM ItemTable WHERE key = ?", (key,))
            deleted += 1
        conn.commit()
        conn.close()
        if deleted:
            logger.debug(f"已清除 {deleted} 个缓存 secret 条目")
    except Exception as e:
        logger.debug(f"清除 secret 条目失败: {e}")


def _clear_claw_channels():
    """清除 settings.json 中的 claw.channels"""
    for label, path in [
        (".workbuddy/settings.json", os.path.join(WORKBUDDY_HOME, "settings.json")),
        ("AppData/User/settings.json", os.path.join(WORKBUDDY_ROAMING, "User", "settings.json")),
    ]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            changed = False
            if "claw" in data and "channels" in data.get("claw", {}):
                del data["claw"]["channels"]
                if not data["claw"]:
                    del data["claw"]
                changed = True
            keys_to_remove = [k for k in data if "claw.channels" in k]
            for k in keys_to_remove:
                del data[k]
            if keys_to_remove:
                changed = True
            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                logger.debug(f"已清除 {label} 中的 claw.channels")
        except Exception:
            pass


def _clear_user_id_files(directory: str):
    """删除目录中以 userId (UUID格式) 命名的文件"""
    if not os.path.exists(directory):
        return
    for fname in os.listdir(directory):
        if "-" in fname and len(fname.split("_")[0]) == 36:
            try:
                os.remove(os.path.join(directory, fname))
            except Exception:
                pass
    # 同时删除 user-memery-state.json
    state_file = os.path.join(directory, "user-memery-state.json")
    if os.path.exists(state_file):
        try:
            os.remove(state_file)
        except Exception:
            pass


def _clear_workbuddy_db_sessions():
    """清除 workbuddy.db 中的 sessions 和 workspaces 表"""
    if not os.path.exists(WORKBUDDY_DB):
        return
    try:
        conn = sqlite3.connect(WORKBUDDY_DB)
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM workspaces")
        conn.commit()
        conn.close()
        logger.debug("已清除 workbuddy.db sessions/workspaces")
    except Exception as e:
        logger.debug(f"清除 workbuddy.db 失败: {e}")


def _clear_codebuddy_sessions_vscdb():
    """清除 codebuddy-sessions.vscdb 中的 session 记录"""
    if not os.path.exists(SESSIONS_VSCDB):
        return
    try:
        conn = sqlite3.connect(SESSIONS_VSCDB)
        conn.execute("DELETE FROM ItemTable WHERE key LIKE 'session:%'")
        conn.commit()
        conn.close()
        logger.debug("已清除 codebuddy-sessions.vscdb")
    except Exception as e:
        logger.debug(f"清除 codebuddy-sessions.vscdb 失败: {e}")


def _clear_state_vscdb_backup():
    """清除 state.vscdb.backup 中所有认证相关数据

    VS Code 启动时会从 backup 恢复 state.vscdb 中被删除的条目，
    如果不清理，清除操作会无效！
    """
    if not os.path.exists(STATE_VSCDB_BACKUP):
        return
    try:
        conn = sqlite3.connect(STATE_VSCDB_BACKUP)
        cursor = conn.cursor()
        # 删除 AccessToken
        cursor.execute("DELETE FROM ItemTable WHERE key = ?", (ACCESS_TOKEN_SECRET_KEY,))
        # 删除所有 secret://
        cursor.execute("SELECT key FROM ItemTable WHERE key LIKE 'secret://%'")
        for row in cursor.fetchall():
            cursor.execute("DELETE FROM ItemTable WHERE key = ?", (row[0],))
        # 删除 copilot 缓存
        cursor.execute("DELETE FROM ItemTable WHERE key = 'Tencent-Cloud.coding-copilot'")
        # 删除存储标记
        cursor.execute("DELETE FROM ItemTable WHERE key = '__$__targetStorageMarker'")
        conn.commit()
        conn.close()
        logger.debug("已清除 state.vscdb.backup 认证数据")
    except Exception as e:
        logger.debug(f"清除 state.vscdb.backup 失败: {e}")


def _force_rmtree(path: str, label: str, retries: int = 3, delay: float = 2.0):
    """强制删除目录，带重试"""
    if not os.path.exists(path):
        return
    for attempt in range(retries):
        try:
            shutil.rmtree(path)
            logger.info(f"已删除 {label} 目录: {path}")
            return
        except Exception as e:
            if attempt < retries - 1:
                logger.debug(f"删除 {label} 失败（第{attempt+1}次），{delay}s 后重试...")
                time.sleep(delay)
            else:
                logger.warning(f"删除 {label} 目录失败（已重试{retries}次）: {e}")
