"""一键切号：将账号登录态直接写入 CodeBuddy CN / WorkBuddy 桌面客户端。

机制复刻自 cockpit-tools（本机 ../cockpit-tools 源码核对），不经过任何 HTTP 登录：

CodeBuddy CN（macOS）：
  1. 从 Keychain 读取 "CodeBuddy CN Safe Storage" 密码
  2. session JSON 明文 → PBKDF2-HMAC-SHA1(password, salt="saltysalt", 1003 轮, 16B)
     → AES-128-CBC(IV=16 个空格, PKCS7) → 前缀 "v10"
  3. 密文字节包成 {"type":"Buffer","data":[...]} 写入
     ~/Library/Application Support/CodeBuddy CN/User/globalStorage/state.vscdb
     ItemTable.key = secret://{"extensionId":"tencent-cloud.coding-copilot","key":"planning-genie.new.accessTokencn"}

CodeBuddy CN（Windows）：
  1. 读 %APPDATA%/CodeBuddy CN/Local State → os_crypt.encrypted_key
     → base64 → 去 "DPAPI" 前缀 → CryptUnprotectData 解出 32B AES key
  2. AES-256-GCM（12B 随机 nonce）→ "v10" + nonce + (ct||tag)
  3. 同样写入 %APPDATA%/CodeBuddy CN/User/globalStorage/state.vscdb

WorkBuddy（macOS / Windows）：
  直接写明文 JSON（{account, auth, accounts}）到
  <CodeBuddyExtension>/Data/Public/auth/workbuddy-desktop.info
  并删除同路径 .logged-out 登出标记（无加密，两平台同格式）。

公共流程：先退出客户端进程 → 写入 → 读回校验 → 重新启动客户端。
所有 subprocess 调用都走 _clean_env()，防止 WorkBuddy/Electron 父进程继承下来的
ELECTRON_RUN_AS_NODE / NODE_OPTIONS 让目标客户端秒退（cockpit 同款坑）。
"""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from ..models import Account

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（与 cockpit-tools 源码一致）
# ---------------------------------------------------------------------------

CODEBUDDY_CN_SECRET_DB_KEY = (
    'secret://{"extensionId":"tencent-cloud.coding-copilot",'
    '"key":"planning-genie.new.accessTokencn"}'
)
CODEBUDDY_CN_KEYCHAIN_SERVICE = "CodeBuddy CN Safe Storage"
# security find-generic-password 的 account 候选（cockpit 顺序）
CODEBUDDY_CN_KEYCHAIN_ACCOUNTS = [
    "CodeBuddy CN",
    "codebuddy cn",
    "CodeBuddy CN Key",
    None,  # 不带 account 查一次
    CODEBUDDY_CN_KEYCHAIN_SERVICE,
]

_V10_PREFIX = b"v10"
_CBC_IV = b" " * 16  # 16 个空格（macOS CBC）
_PBKDF2_SALT = b"saltysalt"
_PBKDF2_ITERATIONS = 1003


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_windows() -> bool:
    return sys.platform == "win32"


def codebuddy_cn_user_data_dir() -> Path:
    """CodeBuddy CN 客户端 userData 目录（cockpit get_default_codebuddy_cn_user_data_dir）。"""
    if _is_macos():
        return Path.home() / "Library" / "Application Support" / "CodeBuddy CN"
    if _is_windows():
        return Path(os.environ.get("APPDATA", "")) / "CodeBuddy CN"
    return Path.home() / ".config" / "CodeBuddy CN"


def workbuddy_auth_file() -> Path:
    """WorkBuddy 登录态文件（cockpit get_default_workbuddy_auth_file_path）。

    注意：目录名是 CodeBuddyExtension（新旧版共用），不是 WorkBuddyExtension。
    """
    if _is_macos():
        base = Path.home() / "Library" / "Application Support"
    elif _is_windows():
        base = Path(os.environ.get("LOCALAPPDATA", ""))
    else:
        base = Path.home() / ".local" / "share"
    return base / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"


def codebuddy_cn_app_candidates() -> list:
    if _is_macos():
        return [
            "/Applications/CodeBuddy CN.app",
            str(Path.home() / "Applications" / "CodeBuddy CN.app"),
        ]
    if _is_windows():
        local = os.environ.get("LOCALAPPDATA", "")
        prog = os.environ.get("PROGRAMFILES", "")
        return [
            str(Path(local) / "Programs" / "CodeBuddy CN" / "CodeBuddy CN.exe"),
            str(Path(local) / "Programs" / "CodeBuddy CN" / "CodeBuddy.exe"),
            str(Path(prog) / "CodeBuddy CN" / "CodeBuddy CN.exe"),
            str(Path(prog) / "CodeBuddy CN" / "CodeBuddy.exe"),
        ]
    return []


def workbuddy_app_candidates() -> list:
    if _is_macos():
        return [
            "/Applications/WorkBuddy.app",
            str(Path.home() / "Applications" / "WorkBuddy.app"),
        ]
    if _is_windows():
        local = os.environ.get("LOCALAPPDATA", "")
        prog = os.environ.get("PROGRAMFILES", "")
        return [
            str(Path(local) / "Programs" / "WorkBuddy" / "WorkBuddy.exe"),
            str(Path(prog) / "WorkBuddy" / "WorkBuddy.exe"),
        ]
    return []


# 旧名兼容（测试脚本引用）
CODEBUDDY_CN_USER_DATA_DIR = codebuddy_cn_user_data_dir()
CODEBUDDY_CN_APP_CANDIDATES = codebuddy_cn_app_candidates()
WORKBUDDY_AUTH_FILE = workbuddy_auth_file()
WORKBUDDY_APP_CANDIDATES = workbuddy_app_candidates()


class SwitchError(Exception):
    """切号失败（message 可直接展示给用户）"""


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

# 这些变量从 WorkBuddy/Electron 父进程继承下来后会毒化子进程：
# ELECTRON_RUN_AS_NODE=1 会让目标客户端的 Electron 以 Node 模式启动并秒退，
# NODE_OPTIONS 会让所有 node 进程加载外来 shim —— cockpit 同款清理清单。
_TOXIC_ENV_KEYS = (
    "__CFBundleIdentifier",
    "XPC_SERVICE_NAME",
    "NODE_OPTIONS",
    "NODE_PATH",
    "NODE_ENV",
    "npm_config_prefix",
    "npm_config_devdir",
    "ELECTRON_RUN_AS_NODE",
    "ELECTRON_NO_ASAR",
    "ELECTRON_FORCE_WINDOW_MENU_BAR",
    "ELECTRON_NO_ATTACH_CONSOLE",
)


def _clean_env() -> dict:
    env = dict(os.environ)
    for key in _TOXIC_ENV_KEYS:
        env.pop(key, None)
    return env


def _run_quiet(args: list) -> tuple:
    """运行命令，返回 (returncode, stdout.strip())，不抛异常。"""
    try:
        out = subprocess.run(
            args, capture_output=True, text=True, timeout=15, check=False,
            env=_clean_env(),
        )
        return out.returncode, (out.stdout or "").strip()
    except Exception as exc:
        logger.warning("命令执行失败 %s: %s", args[:2], exc)
        return -1, ""


def _find_app_bundle(candidates: list) -> str:
    for path in candidates:
        if Path(path).exists():
            return path
    return ""


def _process_alive(marker: str) -> bool:
    """marker：macOS 是路径片段，Windows 是镜像名（如 CodeBuddy CN.exe）。"""
    if _is_windows():
        code, out = _run_quiet(["tasklist", "/FI", f"IMAGENAME eq {marker}", "/NH"])
        return code == 0 and marker.lower() in out.lower()
    code, out = _run_quiet(["pgrep", "-f", marker])
    return code == 0 and bool(out)


def _quit_app_bundle(app_root: str, timeout_secs: float = 15.0):
    """退出客户端所有进程（cockpit 在注入前也先杀进程）。

    macOS: pkill -f SIGTERM → 等待 → SIGKILL 兜底。
    Windows: taskkill /F /T /IM 镜像名（/T 杀整棵进程树）。
    退出可能需要 10-25 秒（窗口/扩展宿主清理），死不透则报错。
    """
    if not app_root:
        return
    if _is_windows():
        marker = Path(app_root).name  # CodeBuddy CN.exe / WorkBuddy.exe
        if not _process_alive(marker):
            logger.info("[切号] %s 无运行进程，跳过退出步骤", marker)
            return
        logger.info("[切号] taskkill %s", marker)
        subprocess.run(
            ["taskkill", "/F", "/T", "/IM", marker],
            capture_output=True, check=False, env=_clean_env(),
        )
    else:
        marker = f"{app_root}/Contents/MacOS"
        if not _process_alive(marker):
            logger.info("[切号] %s 无运行进程，跳过退出步骤", app_root)
            return
        logger.info("[切号] 发送 SIGTERM 退出 %s", app_root)
        subprocess.run(["pkill", "-f", marker], capture_output=True, check=False, env=_clean_env())

    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if not _process_alive(marker):
            logger.info("[切号] %s 已退出", app_root)
            return
        time.sleep(0.5)

    if _is_windows():
        raise SwitchError("无法退出正在运行的客户端进程，请手动退出后重试")
    logger.warning("[切号] SIGTERM 超时（%.0fs），对 %s 执行 SIGKILL", timeout_secs, app_root)
    subprocess.run(["pkill", "-9", "-f", marker], capture_output=True, check=False, env=_clean_env())
    time.sleep(1.0)
    if _process_alive(marker):
        raise SwitchError("无法退出正在运行的客户端进程，请手动退出后重试")


def _start_app_bundle(app_root: str):
    """启动客户端并验证进程存活。必须用干净环境，
    否则 ELECTRON_RUN_AS_NODE 会让目标 app 秒退。"""
    if _is_windows():
        try:
            subprocess.Popen(
                [app_root], env=_clean_env(), close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0),
            )
        except Exception as exc:
            raise SwitchError(f"启动客户端失败: {exc}")
        marker = Path(app_root).name
    else:
        code, out = _run_quiet(["open", "-na", app_root])  # -n = 新实例，与 cockpit 一致
        if code != 0:
            raise SwitchError(f"启动客户端失败: {out or app_root}")
        marker = f"{app_root}/Contents/MacOS"

    for _ in range(20):  # 最多等 10 秒
        time.sleep(0.5)
        if _process_alive(marker):
            logger.info("[切号] %s 已启动", app_root)
            return
    raise SwitchError("客户端启动后未检测到运行进程，请手动打开客户端确认")


def _decode_jwt_exp_ms(token: str):
    """从 JWT payload 解 exp（秒）并转毫秒；失败返回 None。"""
    try:
        import base64

        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = data.get("exp")
        return int(exp) * 1000 if exp else None
    except Exception:
        return None


def _account_tokens(account: Account) -> tuple:
    """从 Account 取 (access_token, refresh_token)。auth_raw 优先，兜底 auth_token 字段。"""
    access, refresh = "", ""
    if account.auth_raw:
        try:
            raw = json.loads(account.auth_raw)
            access = raw.get("accessToken") or raw.get("access_token") or ""
            refresh = raw.get("refreshToken") or raw.get("refresh_token") or ""
        except Exception:
            pass
    if not access:
        access = account.auth_token or ""
    return access, refresh


def _refresh_if_stale(account: Account):
    """切号前尽量保证 token 新鲜（JWT 过期且有 refreshToken 时先续期）。

    复用 proxy_server.ensure_fresh_jwt：非 JWT / 新鲜 token 直接透传，零开销。
    """
    try:
        from .proxy_server import ensure_fresh_jwt

        ensure_fresh_jwt(account)
    except Exception as exc:
        logger.warning("切号前刷新 JWT 失败（继续用现有 token 写入）: %s", exc)


# ---------------------------------------------------------------------------
# CodeBuddy CN：Safe Storage v10 加密 + state.vscdb 写入
# ---------------------------------------------------------------------------

def _get_macos_keychain_password(service: str, account_candidates: list) -> str:
    for account in account_candidates:
        args = ["security", "find-generic-password", "-w", "-s", service]
        if account:
            args += ["-a", account]
        code, out = _run_quiet(args)
        if code == 0 and out:
            return out
    raise SwitchError(
        f"无法从钥匙串读取「{service}」密码。\n\n"
        "通常是因为该客户端从未登录过。请先手动打开客户端登录任意账号一次，"
        "退出后再使用切号功能。"
    )


def _encrypt_v10(plaintext: bytes, password: str) -> bytes:
    """VS Code Safe Storage v10：PBKDF2-HMAC-SHA1(1003) + AES-128-CBC(IV=16 空格)。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding

    key = hashlib.pbkdf2_hmac(
        "sha1", password.encode("utf-8"), _PBKDF2_SALT, _PBKDF2_ITERATIONS, dklen=16
    )
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(_CBC_IV)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return _V10_PREFIX + ciphertext


def _decrypt_v10(encrypted: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding

    if not encrypted.startswith(_V10_PREFIX):
        raise SwitchError("回读校验失败：密文缺少 v10 前缀")
    key = hashlib.pbkdf2_hmac(
        "sha1", password.encode("utf-8"), _PBKDF2_SALT, _PBKDF2_ITERATIONS, dklen=16
    )
    decryptor = Cipher(algorithms.AES(key), modes.CBC(_CBC_IV)).decryptor()
    padded = decryptor.update(encrypted[len(_V10_PREFIX):]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


# ---------------------------------------------------------------------------
# Windows：Local State 的 DPAPI 密钥 + AES-256-GCM（与 cockpit Windows 分支一致）
# ---------------------------------------------------------------------------

def _dpapi_decrypt(data: bytes) -> bytes:
    """CryptUnprotectData（当前用户作用域），ctypes 零依赖实现。"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    blob_in = DATA_BLOB(
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)),
    )
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise SwitchError("DPAPI 解密失败（CryptUnprotectData）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _get_windows_safe_storage_key(user_data_dir: Path) -> bytes:
    """从 <userData>/Local State 读 os_crypt.encrypted_key → 去 DPAPI 前缀 → 解出 32B AES key。"""
    import base64

    local_state = user_data_dir / "Local State"
    if not local_state.is_file():
        raise SwitchError(
            f"未找到 {local_state}。\n\n通常是因为客户端从未登录过，请先打开客户端登录一次。"
        )
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
        encrypted_key = base64.b64decode(data["os_crypt"]["encrypted_key"])
    except Exception as exc:
        raise SwitchError(f"解析 Local State 失败: {exc}")
    if encrypted_key[:5] != b"DPAPI":
        raise SwitchError("Local State 中的 encrypted_key 缺少 DPAPI 前缀")
    key = _dpapi_decrypt(encrypted_key[5:])
    if len(key) != 32:
        raise SwitchError(f"DPAPI 解出的密钥长度异常: {len(key)}")
    return key


def _encrypt_v10_windows(plaintext: bytes, key: bytes) -> bytes:
    """Windows v10：AES-256-GCM，12 字节随机 nonce，v10 + nonce + (ct||tag)。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return _V10_PREFIX + nonce + ct


def _decrypt_v10_windows(encrypted: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not encrypted.startswith(_V10_PREFIX) or len(encrypted) < 3 + 12 + 16:
        raise SwitchError("回读校验失败：密文格式异常")
    nonce = encrypted[3:15]
    return AESGCM(key).decrypt(nonce, encrypted[15:], None)


def _resolve_codebuddy_cn_state_db() -> Path:
    """候选：User/globalStorage/state.vscdb → globalStorage/state.vscdb → state.vscdb；
    都不存在则返回首选路径（目录会被创建）。"""
    root = codebuddy_cn_user_data_dir()
    candidates = [
        root / "User" / "globalStorage" / "state.vscdb",
        root / "globalStorage" / "state.vscdb",
        root / "state.vscdb",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def build_codebuddy_cn_session_json(account: Account) -> str:
    """与 cockpit build_session_json 完全一致的结构。

    关键点：顶层 accessToken 必须是 "uid+access_token"，缺 uid 切号会静默失败。
    """
    access_token, refresh_token = _account_tokens(account)
    if not access_token:
        raise SwitchError("该账号没有 accessToken，无法切号")
    if not account.uid:
        raise SwitchError("该账号缺少 UID，无法切号（accessToken 需拼接 uid）")

    expires_at = _decode_jwt_exp_ms(access_token) or 0
    now_ms = int(time.time() * 1000)
    domain = account.domain or ""

    session = {
        "id": "Tencent-Cloud.genie-ide-cn",
        "token": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at,
        "domain": domain,
        "accessToken": f"{account.uid}+{access_token}",
        "converted": True,
        "account": {
            "id": account.uid,
            "uid": account.uid,
            "label": account.nickname or "",
            "nickname": account.nickname or "",
            "enterpriseId": account.enterprise_id or "",
            "enterpriseName": account.enterprise_name or "",
            "pluginEnabled": True,
            "lastLogin": True,
        },
        "auth": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "tokenType": "Bearer",
            "domain": domain,
            "expiresAt": expires_at,
            "expiresIn": expires_at,
            "refreshExpiresIn": 0,
            "refreshExpiresAt": 0,
            "lastRefreshTime": now_ms,
        },
    }
    return json.dumps(session, ensure_ascii=False, separators=(",", ":"))


def switch_to_codebuddy_cn(account: Account) -> str:
    """把 account 的登录态写入 CodeBuddy CN 客户端并重启它。返回成功提示文案。"""
    if not (_is_macos() or _is_windows()):
        raise SwitchError("切换 CodeBuddy CN 账号目前支持 macOS / Windows")

    _refresh_if_stale(account)
    session_json = build_codebuddy_cn_session_json(account)

    app_root = _find_app_bundle(codebuddy_cn_app_candidates())
    if not app_root:
        hint = (
            "/Applications/CodeBuddy CN.app"
            if _is_macos()
            else "%LOCALAPPDATA%\\Programs\\CodeBuddy CN"
        )
        raise SwitchError(f"未找到 CodeBuddy CN 客户端（{hint}）")

    # 取一次密钥（macOS=Keychain 密码，Windows=DPAPI 解出的 AES key）
    if _is_macos():
        secret = _get_macos_keychain_password(
            CODEBUDDY_CN_KEYCHAIN_SERVICE, CODEBUDDY_CN_KEYCHAIN_ACCOUNTS
        )
        encrypt = lambda pt: _encrypt_v10(pt, secret)  # noqa: E731
        decrypt = lambda ct: _decrypt_v10(ct, secret)  # noqa: E731
    else:
        secret = _get_windows_safe_storage_key(codebuddy_cn_user_data_dir())
        encrypt = lambda pt: _encrypt_v10_windows(pt, secret)  # noqa: E731
        decrypt = lambda ct: _decrypt_v10_windows(ct, secret)  # noqa: E731

    db_path = _resolve_codebuddy_cn_state_db()
    if not db_path.parent.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)

    # 注入前必须退出客户端，否则它退出时会用内存里的旧状态覆盖 DB
    _quit_app_bundle(app_root)

    # 备份 state.vscdb（首次切号时留一份）
    if db_path.is_file():
        backup = db_path.with_suffix(".vscdb.antigravity-bak")
        if not backup.exists():
            try:
                shutil.copy2(db_path, backup)
            except Exception as exc:
                logger.warning("备份 state.vscdb 失败（继续写入）: %s", exc)

    encrypted = encrypt(session_json.encode("utf-8"))
    buffer_json = json.dumps({"type": "Buffer", "data": list(encrypted)})

    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
            (CODEBUDDY_CN_SECRET_DB_KEY, buffer_json),
        )
        conn.commit()
    finally:
        conn.close()

    # 读回解密校验（比 cockpit 的存在性校验更强）
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (CODEBUDDY_CN_SECRET_DB_KEY,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise SwitchError("写入后回读失败：state.vscdb 中未找到目标 key")
    stored = json.loads(row[0])
    decrypted = decrypt(bytes(stored["data"])).decode("utf-8")
    if decrypted != session_json:
        raise SwitchError("写入后回读校验失败：解密内容与会话不一致")

    _start_app_bundle(app_root)
    return f"已切换 CodeBuddy CN 到账号「{account.display_name}」，客户端已重启"


# ---------------------------------------------------------------------------
# WorkBuddy：明文 auth 文件
# ---------------------------------------------------------------------------

def build_workbuddy_auth_json(account: Account) -> dict:
    """与 cockpit build_default_client_auth_session 一致：{account, auth, accounts}。"""
    access_token, refresh_token = _account_tokens(account)
    if not access_token:
        raise SwitchError("该账号没有 accessToken，无法切号")

    now_ms = int(time.time() * 1000)
    expires_at = _decode_jwt_exp_ms(access_token) or 0
    domain = account.domain or ""

    account_obj = {
        "uid": account.uid or "",
        "nickname": account.nickname or "",
        "type": "personal",
        "accountType": "",
        "idp": "",
        "oneidAccountId": "",
        "areaInfoComplete": False,
        "isCurrentOneIdEnterprise": False,
        "isFirstLogin": False,
        "lastLogin": True,
        "pluginEnabled": True,
        "deployStatus": {"statusCode": 0, "statusMsg": "", "detailMsg": ""},
        "sso": {"domain": "", "domainModifiedTimes": 0},
    }

    auth_obj = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "tokenType": "Bearer",
        "domain": domain,
        "lastRefreshTime": now_ms,
        "scope": "openid profile offline_access email",
    }
    if expires_at:
        auth_obj["expiresAt"] = expires_at
        auth_obj["expiresIn"] = max(0, (expires_at - now_ms) // 1000)
        auth_obj["refreshExpiresAt"] = expires_at
        auth_obj["refreshExpiresIn"] = max(0, (expires_at - now_ms) // 1000)
    else:
        auth_obj["expiresIn"] = 0
        auth_obj["refreshExpiresIn"] = 0

    return {"account": account_obj, "auth": auth_obj, "accounts": [account_obj]}


def switch_to_workbuddy(account: Account) -> str:
    """把 account 的登录态写入 WorkBuddy 客户端并重启它。返回成功提示文案。"""
    if not (_is_macos() or _is_windows()):
        raise SwitchError("切换 WorkBuddy 账号目前支持 macOS / Windows")

    _refresh_if_stale(account)
    payload = build_workbuddy_auth_json(account)

    app_root = _find_app_bundle(workbuddy_app_candidates())
    if not app_root:
        hint = (
            "/Applications/WorkBuddy.app"
            if _is_macos()
            else "%LOCALAPPDATA%\\Programs\\WorkBuddy"
        )
        raise SwitchError(f"未找到 WorkBuddy 客户端（{hint}）")

    auth_file = workbuddy_auth_file()
    marker_path = Path(str(auth_file) + ".logged-out")

    # 注入前必须退出客户端
    _quit_app_bundle(app_root)

    auth_file.parent.mkdir(parents=True, exist_ok=True)

    # 备份（首次）
    if auth_file.is_file():
        backup = auth_file.with_suffix(".info.antigravity-bak")
        if not backup.exists():
            try:
                shutil.copy2(auth_file, backup)
            except Exception as exc:
                logger.warning("备份 workbuddy-desktop.info 失败（继续写入）: %s", exc)

    # 登出标记必须删掉，否则客户端视为已登出
    if marker_path.exists():
        marker_path.unlink()

    content = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp_file = auth_file.with_suffix(".info.tmp")
    tmp_file.write_text(content, encoding="utf-8")
    os.replace(tmp_file, auth_file)

    # 读回校验
    written = json.loads(auth_file.read_text(encoding="utf-8"))
    if written.get("auth", {}).get("accessToken") != payload["auth"]["accessToken"]:
        raise SwitchError("写入后回读校验失败：accessToken 不一致")

    _start_app_bundle(app_root)
    return f"已切换 WorkBuddy 到账号「{account.display_name}」，客户端已重启"
