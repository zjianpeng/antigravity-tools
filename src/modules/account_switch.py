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


def workbuddy_data_dir() -> Path:
    """WorkBuddy 客户端数据根目录（workbuddy.db / memory/ / connectors/ 所在）。"""
    if _is_windows():
        return Path(os.environ.get("USERPROFILE", "")) / ".workbuddy"
    return Path.home() / ".workbuddy"


def read_current_workbuddy_uid() -> str:
    """从当前 WorkBuddy 登录态文件读 account.uid（切号前的旧账号 user_id）。"""
    try:
        data = json.loads(workbuddy_auth_file().read_text(encoding="utf-8"))
        return (data.get("account") or {}).get("uid") or ""
    except Exception:
        return ""


def migrate_workbuddy_user_data(old_uid: str, new_uid: str, data_dir: Path | None = None) -> str:
    """无感换号：把旧账号的对话/记忆/连接器迁移到新账号名下。

    原理（本机实测核对，2026-08-08）：WorkBuddy 按 user_id 做账号隔离——
    - sessions:  workbuddy.db sessions.user_id
    - 长期记忆:  memory/{uid}_memory.md
    - 连接器:    connectors/{uid}/
    数据没丢，只是新账号 UI 看不到。这里把归属改到新 uid。

    安全措施：
    - 迁移前整体备份到 data_dir/antigravity-switch-backups/{时间戳}_{old_uid前8位}/
    - 只复制/追加/UPDATE，不删除旧账号任何文件
    - 迁移前后 WAL checkpoint，改完验证旧 uid 的 sessions 归零
    必须在 WorkBuddy 客户端退出后调用（DB 不被占用）。
    """
    if not old_uid or not new_uid or old_uid == new_uid:
        return ""
    data_dir = data_dir or workbuddy_data_dir()
    if not data_dir.is_dir():
        return ""

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = data_dir / "antigravity-switch-backups" / f"{ts}_{old_uid[:8]}"
    n = 1
    while backup_dir.exists():  # 同一秒内重复迁移同一账号时避免撞目录
        n += 1
        backup_dir = data_dir / "antigravity-switch-backups" / f"{ts}_{old_uid[:8]}_{n}"
    backup_dir.mkdir(parents=True)
    (backup_dir / "meta.json").write_text(
        json.dumps(
            {"old_uid": old_uid, "new_uid": new_uid, "time": ts},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    summary = []

    # 1) sessions 对话记录
    db_path = data_dir / "workbuddy.db"
    if db_path.is_file():
        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            shutil.copy2(db_path, backup_dir / "workbuddy.db")
            cur = conn.execute(
                "UPDATE sessions SET user_id = ? WHERE user_id = ?",
                (new_uid, old_uid),
            )
            moved = cur.rowcount
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            left = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (old_uid,)
            ).fetchone()[0]
            if left != 0:
                raise SwitchError(f"对话迁移后旧账号仍剩 {left} 条，已中止（备份在 {backup_dir}）")
            summary.append(f"对话 {moved} 条")
        finally:
            conn.close()

    # 2) 长期记忆（追加去重，不动旧文件）
    old_mem = data_dir / "memory" / f"{old_uid}_memory.md"
    new_mem = data_dir / "memory" / f"{new_uid}_memory.md"
    if old_mem.is_file():
        shutil.copy2(old_mem, backup_dir / old_mem.name)
        old_lines = old_mem.read_text(encoding="utf-8", errors="replace").splitlines()
        if new_mem.is_file():
            shutil.copy2(new_mem, backup_dir / new_mem.name)
            existing = set(new_mem.read_text(encoding="utf-8", errors="replace").splitlines())
            add = [ln for ln in old_lines if ln.strip() and ln not in existing]
            if add:
                with new_mem.open("a", encoding="utf-8") as f:
                    f.write(f"\n\n---\n> 以下迁移自旧账号 {old_uid[:8]}（{ts}）\n")
                    f.write("\n".join(add) + "\n")
                summary.append(f"记忆追加 {len(add)} 行")
        else:
            new_mem.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_mem, new_mem)
            summary.append("记忆已复制")

    # 3) MCP 连接器（JSON 深度合并，新账号已有配置优先；不删旧目录）
    old_conn = data_dir / "connectors" / old_uid
    new_conn = data_dir / "connectors" / new_uid
    if old_conn.is_dir():
        shutil.copytree(old_conn, backup_dir / f"connectors_{old_uid[:8]}")
        if not new_conn.is_dir():
            shutil.copytree(old_conn, new_conn)
            summary.append("连接器已复制")
        else:
            merged = 0
            for src in old_conn.rglob("*"):
                if not src.is_file():
                    continue
                dst = new_conn / src.relative_to(old_conn)
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    merged += 1
                elif src.suffix == ".json":
                    try:
                        old_j = json.loads(src.read_text(encoding="utf-8"))
                        new_j = json.loads(dst.read_text(encoding="utf-8"))
                        if isinstance(old_j, dict) and isinstance(new_j, dict):
                            for k, v in old_j.items():
                                new_j.setdefault(k, v)  # 新账号已有配置不动
                            dst.write_text(
                                json.dumps(new_j, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                            merged += 1
                    except Exception:
                        logger.warning("连接器合并跳过（JSON 解析失败）: %s", src)
            if merged:
                summary.append(f"连接器合并 {merged} 项")

    logger.info("workbuddy 数据迁移完成 %s -> %s: %s（备份 %s）", old_uid, new_uid, summary, backup_dir)
    tail = "、".join(summary) if summary else "旧账号无可迁移数据"
    return f"{tail}\n备份目录：{backup_dir}"


def list_workbuddy_sessions(data_dir: Path | None = None) -> list:
    """列出本地全部未删除的 WorkBuddy 对话（只读），供「按对话恢复」挑选。

    返回 [{id, user_id, title, updated_at, created_at}]，按 updated_at 倒序。
    title 取 custom_title 优先，其次 title，都没有则为 "（无标题）"。
    """
    data_dir = data_dir or workbuddy_data_dir()
    db_path = data_dir / "workbuddy.db"
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute(
            "SELECT id, user_id, title, custom_title, created_at, updated_at "
            "FROM sessions WHERE deleted_at IS NULL ORDER BY updated_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "user_id": r[1],
            "title": (r[3] or r[2] or "（无标题）").strip() or "（无标题）",
            "created_at": r[4],
            "updated_at": r[5],
        }
        for r in rows
    ]


def restore_workbuddy_sessions(
    session_ids: list, target_uid: str, data_dir: Path | None = None
) -> str:
    """按对话恢复：把选中的若干条对话改归 target_uid 名下（精准 UPDATE，不动其他对话）。

    与 migrate_workbuddy_user_data 同一套安全流程：备份 DB → UPDATE → WAL checkpoint → 验证。
    WorkBuddy 客户端有会话列表缓存，恢复后需重启客户端才能在 UI 看到。
    """
    session_ids = [s for s in session_ids if s]
    if not session_ids or not target_uid:
        raise SwitchError("参数为空：未选择对话或未识别到当前账号")
    data_dir = data_dir or workbuddy_data_dir()
    db_path = data_dir / "workbuddy.db"
    if not db_path.is_file():
        raise SwitchError(f"未找到 WorkBuddy 数据库：{db_path}")

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = data_dir / "antigravity-switch-backups" / f"{ts}_restore_{target_uid[:8]}"
    n = 1
    while backup_dir.exists():  # 同一秒内重复执行时避免撞目录
        n += 1
        backup_dir = data_dir / "antigravity-switch-backups" / f"{ts}_restore_{target_uid[:8]}_{n}"
    backup_dir.mkdir(parents=True)
    (backup_dir / "meta.json").write_text(
        json.dumps(
            {"type": "restore_sessions", "target_uid": target_uid,
             "session_ids": session_ids, "time": ts},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    placeholders = ",".join("?" for _ in session_ids)
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(db_path, backup_dir / "workbuddy.db")
        cur = conn.execute(
            f"UPDATE sessions SET user_id = ? WHERE id IN ({placeholders})",
            [target_uid, *session_ids],
        )
        moved = cur.rowcount
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        left = conn.execute(
            f"SELECT COUNT(*) FROM sessions WHERE id IN ({placeholders}) AND user_id != ?",
            [*session_ids, target_uid],
        ).fetchone()[0]
        if left != 0:
            raise SwitchError(f"恢复后仍有 {left} 条对话未归属当前账号，已中止（备份在 {backup_dir}）")
    finally:
        conn.close()

    logger.info("workbuddy 按对话恢复完成: %s 条 -> %s（备份 %s）", moved, target_uid, backup_dir)
    return f"对话 {moved} 条已恢复到当前账号\n备份目录：{backup_dir}"


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


def switch_to_workbuddy(account: Account, keep_sessions: bool = False) -> str:
    """把 account 的登录态写入 WorkBuddy 客户端并重启它。返回成功提示文案。

    keep_sessions=True 时（无感换号）：切号前记录当前账号 uid，写入新登录态后
    把旧账号的对话/记忆/连接器迁移到新账号名下（先自动备份）。
    """
    if not (_is_macos() or _is_windows()):
        raise SwitchError("切换 WorkBuddy 账号目前支持 macOS / Windows")

    _refresh_if_stale(account)
    payload = build_workbuddy_auth_json(account)

    # 切号前记录旧账号 uid（覆盖 auth 文件之前）
    old_uid = read_current_workbuddy_uid() if keep_sessions else ""
    new_uid = payload["account"].get("uid") or ""

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

    # 无感换号：客户端已退出，趁 DB 空闲迁移旧账号数据归属
    migrate_msg = ""
    if keep_sessions and old_uid and new_uid and old_uid != new_uid:
        migrate_msg = migrate_workbuddy_user_data(old_uid, new_uid)

    _start_app_bundle(app_root)
    msg = f"已切换 WorkBuddy 到账号「{account.display_name}」，客户端已重启"
    if migrate_msg:
        msg += f"\n\n对话记录已跟随：{migrate_msg}"
    return msg
