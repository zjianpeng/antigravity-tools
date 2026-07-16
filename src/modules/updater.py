"""自动更新模块 - 检测新版本 + 下载 + 应用更新

更新流程:
1. 启动时 & 每小时检测更新
   - 优先查国内服务器 (http://103.36.63.44:9680/version.json)
   - 服务器失败时 fallback 到 GitHub Release API (https://api.github.com/repos/qinchangxv/antigravity-tools/releases/latest)
2. 对比本地版本号 (src/VERSION)
3. 有新版本 → 弹窗提示(含changelog) → 用户确认 → 下载更新包
4. 源码模式: 解压覆盖 src/ → 提示重启
   打包模式: 下载 zip → 批处理替换 → 自动重启

双源策略：服务器优先（国内下载快），GitHub 兜底（服务器不可用时仍能更新）。
"""

import json
import logging
import os
import shlex
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

from PySide6.QtCore import QObject, Signal, QTimer, Slot

logger = logging.getLogger(__name__)

# ─── 更新源（双源策略：服务器优先，GitHub 兜底）───
GITHUB_REPO = "qinchangxv/antigravity-tools"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
# GitHub token 认证（避免 rate limit，从环境变量读取，不在代码中硬编码）
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# 旧服务器（fallback）
UPDATE_SERVER = "http://103.36.63.44:9680"
VERSION_URL = f"{UPDATE_SERVER}/version.json"

# 本地版本文件
VERSION_FILE = Path(__file__).parent.parent / "VERSION"


def get_current_version() -> str:
    """读取本地版本号"""
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    return "0.0.0"


def _get_platform_key() -> str:
    """获取当前平台标识（windows / mac）。"""
    return "mac" if sys.platform == "darwin" else "windows"


def _get_macos_arch() -> str:
    """返回元数据使用的 macOS 架构键。"""
    import platform

    machine = platform.machine().lower()
    return "arm64" if machine in {"arm64", "aarch64"} else "x86_64"


def _get_platform_asset_keyword() -> str:
    """GitHub Release asset 文件名关键词，用于匹配当前平台的下载包。"""
    if sys.platform == "darwin":
        return "macOS-ARM" if _get_macos_arch() == "arm64" else "macOS-Intel"
    return "Windows-x64"


def _compare_versions(remote: str, local: str) -> bool:
    """比较版本号，remote > local 返回 True"""
    try:
        r_parts = [int(x) for x in remote.strip().split(".")]
        l_parts = [int(x) for x in local.strip().split(".")]
        # 补齐长度
        max_len = max(len(r_parts), len(l_parts))
        r_parts += [0] * (max_len - len(r_parts))
        l_parts += [0] * (max_len - len(l_parts))
        return r_parts > l_parts
    except (ValueError, AttributeError):
        return False


def _is_src_asset(asset_name: str) -> bool:
    """判断 GitHub Release asset 是否为增量 src 包。"""
    asset_lower = asset_name.lower()
    return "-src" in asset_lower or ".src." in asset_lower or "src-" in asset_lower


def _is_windows_asset(asset_name: str) -> bool:
    """判断 asset 名称是否显式指向 Windows，避免 darwin 中的 win 误判。"""
    import re

    asset_lower = asset_name.lower()
    return "windows" in asset_lower or bool(
        re.search(r"(?:^|[-_.])win(?:32|64|x64|x86)?(?:[-_.]|$)", asset_lower)
    )


def _is_macos_asset(asset_name: str) -> bool:
    """判断 asset 名称是否显式指向 macOS 平台。"""
    asset_lower = asset_name.lower()
    return "macos" in asset_lower or "mac" in asset_lower or "darwin" in asset_lower


def _is_zip_download_url(url: str) -> bool:
    """仅接受 HTTP(S) zip 下载地址，拒绝 Release HTML 页面。"""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.path.lower().endswith(".zip")
    )


def _is_zip_asset(asset_name: str, asset_url: str) -> bool:
    """仅允许真正的 zip asset，拒绝 Release HTML 页面。"""
    return asset_name.lower().endswith(".zip") and _is_zip_download_url(asset_url)


def _select_github_assets(assets: list[dict], platform_keyword: str) -> tuple[str, str]:
    """按平台和当前架构选择完整包与增量包。"""
    keyword_lower = platform_keyword.lower()
    is_macos = keyword_lower.startswith("macos")
    exact_full = ""
    exact_src = ""
    platform_full = ""
    platform_src = ""
    generic_src = ""

    for asset in assets:
        asset_name = str(asset.get("name", ""))
        asset_url = str(asset.get("browser_download_url", ""))
        if not asset_url or not _is_zip_asset(asset_name, asset_url):
            continue
        asset_lower = asset_name.lower()
        is_src = _is_src_asset(asset_name)
        is_windows = _is_windows_asset(asset_name)
        is_mac = _is_macos_asset(asset_name)
        exact = keyword_lower in asset_lower

        if exact and is_src and not exact_src:
            exact_src = asset_url
        elif exact and not is_src and not exact_full:
            exact_full = asset_url
        elif is_macos and is_mac and not is_windows:
            # 兼容旧的、未区分架构的 macOS asset；绝不跨架构回退。
            has_explicit_arch = any(
                marker in asset_lower
                for marker in ("arm", "aarch64", "intel", "x86_64", "x64")
            )
            if is_src and not platform_src and not has_explicit_arch:
                platform_src = asset_url
            elif not is_src and not platform_full and not has_explicit_arch:
                platform_full = asset_url
        elif not is_macos and is_windows:
            if is_src and not platform_src:
                platform_src = asset_url
            elif not is_src and not platform_full:
                platform_full = asset_url
        elif is_src and not is_mac and not is_windows and not generic_src:
            generic_src = asset_url

    return exact_full or platform_full, exact_src or platform_src or generic_src


def _platform_metadata(info: dict, platform_key: str) -> dict:
    """规范化旧 schema 与 macOS 分架构 schema。"""
    platforms = info.get("platforms") or {}
    platform_info = platforms.get(platform_key) or {}
    if not isinstance(platform_info, dict):
        platform_info = {}

    if platform_key == "mac":
        arch = _get_macos_arch()
        arch_info = platform_info.get(arch) or platforms.get(arch) or {}
        if not isinstance(arch_info, dict):
            arch_info = {}
        aliases = ("arm", "macos_arm") if arch == "arm64" else ("intel", "macos_intel")
        for alias in aliases:
            candidate = platform_info.get(alias) or platforms.get(alias)
            if isinstance(candidate, dict):
                arch_info = {**candidate, **arch_info}
        suffix = "arm64" if arch == "arm64" else "x86_64"
        legacy_arch_fields = {
            "download_url": platform_info.get(f"download_url_{suffix}", ""),
            "src_download_url": platform_info.get(f"src_download_url_{suffix}", ""),
            "sha256": platform_info.get(f"sha256_{suffix}", ""),
            "src_sha256": platform_info.get(f"src_sha256_{suffix}", ""),
        }
        platform_info = {
            **platform_info,
            **{key: value for key, value in legacy_arch_fields.items() if value},
            **arch_info,
        }

    download_url = str(platform_info.get("download_url", info.get("download_url", ""))).strip()
    src_download_url = str(
        platform_info.get("src_download_url", info.get("src_download_url", ""))
    ).strip()
    if download_url and not _is_zip_download_url(download_url):
        logger.warning("拒绝非 zip 完整包地址: %s", download_url)
        download_url = ""
    if src_download_url and not _is_zip_download_url(src_download_url):
        logger.warning("拒绝非 zip 增量包地址: %s", src_download_url)
        src_download_url = ""

    return {
        "version": str(platform_info.get("version", info.get("version", ""))).strip(),
        "changelog": str(platform_info.get("changelog", info.get("changelog", ""))),
        "download_url": download_url,
        "src_download_url": src_download_url,
        "sha256": str(platform_info.get("sha256", info.get("sha256", ""))).strip(),
        "src_sha256": str(platform_info.get("src_sha256", info.get("src_sha256", ""))).strip(),
    }


def _sh_quote(value) -> str:
    """Return a POSIX shell-safe quoted string for paths containing spaces/CJK."""
    return shlex.quote(str(value))


def _find_running_macos_app_path(current_exe: Path) -> Path | None:
    """Locate the containing .app bundle for the running macOS executable."""
    candidate = current_exe
    while candidate.parent != candidate and not candidate.name.endswith(".app"):
        candidate = candidate.parent
    if candidate.name.endswith(".app"):
        return candidate

    for parent in current_exe.parents:
        if parent.name.endswith(".app"):
            return parent
    return None


def _write_macos_update_script(script_path: Path, script_content: str) -> None:
    """Write a macOS updater shell script and mark it executable."""
    script_path.write_text(script_content, encoding="utf-8")
    script_path.chmod(0o755)


def _extract_macos_app_with_ditto(zip_path: Path, destination: Path) -> bool:
    """使用系统 ditto 保留 .app 的权限、扩展属性和符号链接。"""
    import subprocess

    try:
        result = subprocess.run(
            ["/usr/bin/ditto", "-x", "-k", str(zip_path), str(destination)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.error("ditto 解压失败: %s", result.stderr.strip())
            return False
        return True
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.error("ditto 解压异常: %s", error)
        return False


def _ditto_copy(source: Path, destination: Path) -> bool:
    """使用 ditto 复制目录，供 macOS 更新准备阶段使用。"""
    import subprocess

    try:
        subprocess.run(
            ["/usr/bin/ditto", str(source), str(destination)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        logger.error("ditto 复制异常: %s", error)
        return False


def _read_packaged_version(app_root: Path, platform_name: str) -> str:
    """读取完整包内与 src/VERSION 同源的目标版本。"""
    if platform_name == "mac":
        version_path = app_root / "Contents" / "Resources" / "src" / "VERSION"
    else:
        version_path = app_root / "_internal" / "src" / "VERSION"
    if not version_path.is_file():
        return ""
    return version_path.read_text(encoding="utf-8").strip()


def _fetch_github_release(timeout: int = 15) -> dict | None:
    """从 GitHub Release API 获取最新版本信息

    返回格式与旧服务器 version.json 兼容：
    {
        "version": "1.8.0",
        "changelog": "...",
        "download_url": "https://github.com/.../Antigravity-Tools-Windows-x64.zip",
        "sha256": "",
        "source": "github"
    }
    """
    try:
        import urllib.request
        req = urllib.request.Request(GITHUB_API_URL)
        req.add_header("User-Agent", "AntigravityTools/1.0")
        req.add_header("Accept", "application/vnd.github+json")
        if GITHUB_TOKEN:
            req.add_header("Authorization", f"token {GITHUB_TOKEN}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # 从 tag_name 提取版本号 (v1.5.9 → 1.5.9)
        tag = data.get("tag_name", "")
        version = tag.lstrip("v").strip()
        if not version:
            logger.warning("GitHub Release 无 tag_name")
            return None

        # changelog 用 release body
        changelog = data.get("body", "") or f"v{version} 更新"

        # 匹配当前平台的 asset（优先增量包，其次完整包）
        keyword = _get_platform_asset_keyword()
        download_url, src_download_url = _select_github_assets(data.get("assets", []), keyword)

        if not download_url:
            logger.warning(f"GitHub Release 无可用的 {keyword} zip asset")

        result = {
            "version": version,
            "changelog": changelog,
            "download_url": download_url,
            "src_download_url": src_download_url,
            "sha256": "",
            "source": "github",
        }
        logger.info(f"GitHub Release 检测到版本 {version}（源: GitHub），增量包={'有' if src_download_url else '无'}")
        return result

    except Exception as e:
        logger.warning(f"GitHub Release 检测失败: {e}，尝试旧服务器")
        return None


def _fetch_server_version(timeout: int = 10) -> dict | None:
    """从旧服务器获取版本信息（fallback）"""
    try:
        import urllib.request
        req = urllib.request.Request(VERSION_URL)
        req.add_header("User-Agent", "AntigravityTools/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            data["source"] = "server"
            logger.info(f"服务器检测到版本 {data.get('version', '?')}（源: 服务器）")
            return data
    except Exception as e:
        logger.warning(f"服务器检查更新也失败: {e}")
        return None


def _fetch_version_info(timeout: int = 15) -> dict | None:
    """获取版本信息 — 服务器优先，GitHub 兜底

    服务器拿到版本信息后，额外查 GitHub 补充 src_download_url 作为下载备选。
    这样下载增量包时优先用国内服务器链接（快），失败再切 GitHub 链接。
    """
    # 1. 先查国内服务器（下载快）
    info = _fetch_server_version(timeout=timeout)
    if info:
        platform_key = _get_platform_key()
        server_platform = _platform_metadata(info, platform_key)

        github_src_url = ""
        github_full_url = ""
        github_info = _fetch_github_release(timeout=8)
        if github_info:
            github_src_url = github_info.get("src_download_url", "")
            github_full_url = github_info.get("download_url", "")

        return {
            "version": info.get("version", ""),
            "platforms": {
                platform_key: {
                    **server_platform,
                    "download_url": server_platform["download_url"] or github_full_url,
                    "src_download_url_fallback": github_src_url,
                    # GitHub API 当前不提供 digest；fallback 与主增量包不同源时
                    # 不得复用服务器 src_sha256，否则会错误拒绝合法 fallback。
                    "src_sha256_fallback": "",
                }
            },
            "source": "server",
        }

    # 2. 服务器失败，查 GitHub Release
    info = _fetch_github_release(timeout=timeout)
    if info:
        platform_key = _get_platform_key()
        return {
            "version": info["version"],
            "platforms": {
                platform_key: {
                    "version": info["version"],
                    "changelog": info["changelog"],
                    "download_url": info["download_url"],
                    "src_download_url": info.get("src_download_url", ""),
                    "src_download_url_fallback": "",
                    "sha256": info.get("sha256", ""),
                    "src_sha256": info.get("src_sha256", ""),
                }
            },
            "source": "github",
        }

    logger.warning("所有更新源均不可用")
    return None


def _download_update(url: str, dest: Path, progress_callback=None, timeout: int = 300) -> bool:
    """下载更新包，支持进度回调。"""
    if not _is_zip_download_url(url):
        logger.error("拒绝非 zip 更新地址: %s", url)
        return False
    try:
        import urllib.request
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "AntigravityTools/1.0")

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 65536

            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total > 0:
                        progress_callback(downloaded, total)

        return dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        logger.error(f"下载更新失败: {e}")
        return False


def _verify_sha256(file_path: Path, expected: str) -> bool:
    """验证文件 SHA256"""
    if not expected:
        return True  # 没有校验值则跳过
    import hashlib
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    normalized = expected.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.split(":", 1)[1].strip()
    return sha256.hexdigest().lower() == normalized


def _ps_quote(value) -> str:
    """Return a PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _get_powershell_exe() -> str | None:
    """Locate Windows PowerShell for post-exit update scripts."""
    candidates = [
        shutil.which("powershell.exe"),
        os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
        ),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _apply_src_only_update(zip_path: Path) -> bool:
    """增量更新：只替换 src/ 目录

    Windows: PowerShell 等待进程退出后替换 _internal/src/
    macOS: shell 脚本等待进程退出后替换 .app/Contents/Resources/src/
    """
    import subprocess

    try:
        # [v1.6.1-fix] 不用 TemporaryDirectory，因为它在函数 return 后会自动删除，
        # 而批处理是异步执行的，等它跑 robocopy 时临时目录已经没了。
        # 改用手动管理的持久临时目录，批处理完成后自己清理。
        tmp_dir = Path(tempfile.gettempdir()) / "ag_src_update"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_dir)

        # 找到解压后的 src 目录
        extracted_src = tmp_dir / "src"
        if not extracted_src.exists():
            for sub in tmp_dir.iterdir():
                if sub.is_dir() and (sub / "src").exists():
                    extracted_src = sub / "src"
                    break
        if not extracted_src.exists():
            logger.error("增量包中未找到 src/ 目录")
            return False

        target_version_file = extracted_src / "VERSION"
        if not target_version_file.exists():
            logger.error("增量包缺少 src/VERSION，拒绝应用")
            return False
        target_version = target_version_file.read_text(encoding="utf-8").strip()
        if not target_version:
            logger.error("增量包 src/VERSION 为空，拒绝应用")
            return False

        # [v1.6.1-fix] 把解压后的 src 复制到批处理旁边，避免临时目录被清理
        # 批处理用这个副本做 robocopy，而不是用解压目录本身
        stable_copy = Path(tempfile.gettempdir()) / "ag_src_stable"
        if stable_copy.exists():
            shutil.rmtree(stable_copy, ignore_errors=True)
        shutil.copytree(extracted_src, stable_copy)
        logger.info(f"增量包已复制到稳定目录: {stable_copy}")

        if sys.platform == "darwin":
            # macOS: 不能在当前进程运行时删除 Resources/src，改为退出后脚本替换并重启。
            current_exe = Path(sys.executable)
            app_path = _find_running_macos_app_path(current_exe)
            if app_path is None:
                logger.error(f"无法定位 .app 目录，当前 exe: {current_exe}")
                return False

            src_dir = app_path / "Contents" / "Resources" / "src"
            if not src_dir.exists():
                logger.error(f"macOS src 目录不存在: {src_dir}")
                return False

            script_path = Path(tempfile.gettempdir()) / "ag_src_updater.sh"
            log_path = Path(tempfile.gettempdir()) / "ag_update_error.log"
            backup_dir = Path(tempfile.gettempdir()) / f"ag_src_backup_{os.getpid()}"
            script_content = f"""#!/bin/sh
set -u
LOG={_sh_quote(log_path)}
SRC_DIR={_sh_quote(src_dir)}
STABLE_COPY={_sh_quote(stable_copy)}
TMP_DIR={_sh_quote(tmp_dir)}
APP_PATH={_sh_quote(app_path)}
BACKUP_DIR={_sh_quote(backup_dir)}
SCRIPT_PATH={_sh_quote(script_path)}
TARGET_VERSION={_sh_quote(target_version)}
OLD_PID={os.getpid()}

log() {{
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG"
}}

fail() {{
    log "ERROR: $1"
    exit 1
}}

cleanup() {{
    rm -rf "$TMP_DIR" "$STABLE_COPY"
    rm -f "$SCRIPT_PATH"
}}
rollback() {{
    log "rolling back src"
    rm -rf "$SRC_DIR"
    [ ! -d "$BACKUP_DIR" ] || ditto "$BACKUP_DIR" "$SRC_DIR" || log "CRITICAL: src rollback failed"
}}

log "macOS src update script started"
sleep 2
WAIT_COUNT=0
while kill -0 "$OLD_PID" 2>/dev/null; do
    [ "$WAIT_COUNT" -lt 120 ] || {{ cleanup; fail "timeout waiting for old process"; }}
    WAIT_COUNT=$((WAIT_COUNT + 1))
    sleep 0.5
done
log "old process exited"

[ -d "$STABLE_COPY" ] || {{ cleanup; fail "stable src copy missing: $STABLE_COPY"; }}
PARENT_DIR=$(dirname "$SRC_DIR")
mkdir -p "$PARENT_DIR" || {{ cleanup; fail "create src parent failed: $PARENT_DIR"; }}
rm -rf "$BACKUP_DIR"
if [ -d "$SRC_DIR" ]; then
    ditto "$SRC_DIR" "$BACKUP_DIR" || {{ cleanup; fail "backup src failed: $SRC_DIR"; }}
    rm -rf "$SRC_DIR" || {{ cleanup; fail "delete old src failed: $SRC_DIR"; }}
fi

if ! ditto "$STABLE_COPY" "$SRC_DIR"; then
    rollback
    cleanup
    fail "ditto src failed"
fi
chmod -R u+rwX "$SRC_DIR" || {{ rollback; cleanup; fail "chmod src failed"; }}
[ -f "$SRC_DIR/VERSION" ] || {{ rollback; cleanup; fail "VERSION missing after copy: $SRC_DIR/VERSION"; }}
ACTUAL_VERSION=$(tr -d '\r\n' < "$SRC_DIR/VERSION")
[ "$ACTUAL_VERSION" = "$TARGET_VERSION" ] || {{ rollback; cleanup; fail "VERSION verify failed: expected $TARGET_VERSION, got $ACTUAL_VERSION"; }}
log "version verified: $ACTUAL_VERSION"

rm -rf "$BACKUP_DIR"
cleanup
open -n "$APP_PATH" || fail "restart failed: $APP_PATH"
log "restart launched: $APP_PATH"
"""
            _write_macos_update_script(script_path, script_content)
            subprocess.Popen(
                ["/bin/sh", str(script_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info("macOS 增量更新脚本已启动，即将退出应用以完成更新")
            return True

        else:
            # Windows: PowerShell 替换 _internal/src/
            # [v1.6.1-fix] 改用 PowerShell 替代 bat，避免中文路径 GBK/UTF-8 编码乱码
            current_exe = Path(sys.executable)
            src_dir = current_exe.parent / "_internal" / "src"
            if not src_dir.exists():
                logger.error(f"Windows src 目录不存在: {src_dir}")
                return False
            powershell_exe = _get_powershell_exe()
            if not powershell_exe:
                logger.error("未找到 powershell.exe，无法执行 Windows 增量更新")
                return False

            ps_path = Path(tempfile.gettempdir()) / "ag_src_updater.ps1"
            log_path = Path(tempfile.gettempdir()) / "ag_update_error.log"
            ps_content = f"""$ErrorActionPreference = "Stop"
$LogPath = {_ps_quote(log_path)}
$SrcDir = {_ps_quote(src_dir)}
$StableCopy = {_ps_quote(stable_copy)}
$TmpDir = {_ps_quote(tmp_dir)}
$CurrentExe = {_ps_quote(current_exe)}
$ScriptPath = {_ps_quote(ps_path)}
$TargetVersion = {_ps_quote(target_version)}
$BackupDir = {_ps_quote(Path(tempfile.gettempdir()) / f"ag_src_backup_{os.getpid()}")}
$OldPid = {os.getpid()}

function Write-UpdateLog([string]$Message) {{
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    "$ts $Message" | Add-Content -LiteralPath $LogPath -Encoding UTF8
}}

try {{
    "update script started" | Out-File -LiteralPath $LogPath -Encoding UTF8
    Write-UpdateLog "waiting old pid: $OldPid"
    Start-Sleep -Seconds 2

    $Deadline = (Get-Date).AddSeconds(60)
    while (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {{
        if ((Get-Date) -ge $Deadline) {{ throw "timeout waiting for old process" }}
        Start-Sleep -Milliseconds 500
    }}
    Write-UpdateLog "old process exited"

    if (-not (Test-Path -LiteralPath $StableCopy)) {{
        throw "stable src copy missing: $StableCopy"
    }}

    Remove-Item -LiteralPath $BackupDir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $SrcDir) {{
        Copy-Item -LiteralPath $SrcDir -Destination $BackupDir -Recurse -Force
        Remove-Item -LiteralPath $SrcDir -Recurse -Force
    }}
    if (Test-Path -LiteralPath $SrcDir) {{
        throw "delete src failed: $SrcDir"
    }}

    $ParentDir = Split-Path -Parent $SrcDir
    if (-not (Test-Path -LiteralPath $ParentDir)) {{
        New-Item -ItemType Directory -Path $ParentDir -Force | Out-Null
    }}

    Copy-Item -LiteralPath $StableCopy -Destination $SrcDir -Recurse -Force

    $VersionFile = Join-Path $SrcDir "VERSION"
    if (-not (Test-Path -LiteralPath $VersionFile)) {{
        throw "VERSION missing after copy: $VersionFile"
    }}

    $ActualVersion = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
    if ($ActualVersion -ne $TargetVersion) {{
        throw "VERSION verify failed: expected $TargetVersion, got $ActualVersion"
    }}
    Write-UpdateLog "version verified: $ActualVersion"

    Remove-Item -LiteralPath $BackupDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $TmpDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $StableCopy -Recurse -Force -ErrorAction SilentlyContinue

    $ExeDir = Split-Path -Parent $CurrentExe
    Start-Process -FilePath $CurrentExe -WorkingDirectory $ExeDir
    Write-UpdateLog "restart launched: $CurrentExe"

    Remove-Item -LiteralPath $ScriptPath -Force -ErrorAction SilentlyContinue
}}
catch {{
    $err = ($_ | Out-String).Trim()
    Write-UpdateLog "ERROR: $err"
    if (Test-Path -LiteralPath $BackupDir) {{
        Remove-Item -LiteralPath $SrcDir -Recurse -Force -ErrorAction SilentlyContinue
        Copy-Item -LiteralPath $BackupDir -Destination $SrcDir -Recurse -Force
        Write-UpdateLog "src rollback completed"
    }}
    Remove-Item -LiteralPath $TmpDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $StableCopy -Recurse -Force -ErrorAction SilentlyContinue
    exit 1
}}
"""
            ps_path.write_text(ps_content, encoding="utf-8-sig")

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0

            subprocess.Popen(
                [
                    powershell_exe,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy", "Bypass",
                    "-File", str(ps_path),
                ],
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            logger.info("Windows 增量更新 PowerShell 已启动")
            return True

    except Exception as e:
        logger.error(f"增量更新失败: {e}")
        return False


def _apply_frozen_update(zip_path: Path) -> bool:
    """打包模式下的自动更新

    Windows: 下载 zip → 批处理替换 → 重启
    macOS: 下载 zip → shell 脚本等待退出后替换 .app → 重启
    """
    import subprocess

    try:
        if sys.platform == "darwin":
            return _apply_frozen_update_mac(zip_path)
        else:
            return _apply_frozen_update_windows(zip_path)
    except Exception as e:
        logger.error(f"打包模式更新失败: {e}")
        return False


def _apply_frozen_update_mac(zip_path: Path) -> bool:
    """macOS 打包模式：退出后用 shell 脚本替换整个 .app 并重启。"""
    import subprocess

    try:
        current_exe = Path(sys.executable)
        app_path = _find_running_macos_app_path(current_exe)
        if app_path is None:
            logger.error(f"无法定位 .app 目录，当前 exe: {current_exe}")
            return False

        logger.info(f"当前 .app 路径: {app_path}")

        tmp_dir = Path(tempfile.gettempdir()) / "ag_full_update"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # 禁止 zipfile 解压 .app：ditto 才能可靠保留权限、符号链接与扩展属性。
        if not _extract_macos_app_with_ditto(zip_path, tmp_dir):
            return False

        new_app = None
        for app_dir in tmp_dir.rglob("*.app"):
            new_app = app_dir
            break

        if not new_app:
            logger.error("更新包中未找到 .app 文件")
            return False

        logger.info(f"新版本 .app: {new_app}")
        target_version = _read_packaged_version(new_app, "mac")
        if not target_version:
            logger.error("macOS 完整包缺少 src/VERSION，拒绝应用")
            return False

        stable_app = Path(tempfile.gettempdir()) / "ag_full_stable.app"
        if stable_app.exists():
            shutil.rmtree(stable_app, ignore_errors=True)
        if not _ditto_copy(new_app, stable_app):
            logger.error("ditto 创建稳定 .app 副本失败")
            return False
        logger.info(f"完整包已复制到稳定目录: {stable_app}")

        script_path = Path(tempfile.gettempdir()) / "ag_full_updater.sh"
        log_path = Path(tempfile.gettempdir()) / "ag_update_error.log"
        backup_app = Path(tempfile.gettempdir()) / f"ag_app_backup_{os.getpid()}.app"
        script_content = f"""#!/bin/sh
set -u
LOG={_sh_quote(log_path)}
APP_PATH={_sh_quote(app_path)}
STABLE_APP={_sh_quote(stable_app)}
TMP_DIR={_sh_quote(tmp_dir)}
BACKUP_APP={_sh_quote(backup_app)}
SCRIPT_PATH={_sh_quote(script_path)}
TARGET_VERSION={_sh_quote(target_version)}
EXPECTED_ARCH={_sh_quote(_get_macos_arch())}
NEW_EXE_NAME={_sh_quote(current_exe.name)}
OLD_PID={os.getpid()}

log() {{
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG"
}}

fail() {{
    log "ERROR: $1"
    exit 1
}}

cleanup() {{
    rm -rf "$TMP_DIR" "$STABLE_APP"
    rm -f "$SCRIPT_PATH"
}}

rollback() {{
    log "rolling back full app"
    rm -rf "$APP_PATH"
    if [ -d "$BACKUP_APP" ]; then
        ditto "$BACKUP_APP" "$APP_PATH" || log "CRITICAL: rollback failed"
    fi
}}

log "macOS full update script started"
sleep 2
WAIT_COUNT=0
while kill -0 "$OLD_PID" 2>/dev/null; do
    [ "$WAIT_COUNT" -lt 120 ] || {{ cleanup; fail "timeout waiting for old process"; }}
    WAIT_COUNT=$((WAIT_COUNT + 1))
    sleep 0.5
done
log "old process exited"

[ -d "$STABLE_APP" ] || {{ cleanup; fail "stable app copy missing: $STABLE_APP"; }}
NEW_EXE="$STABLE_APP/Contents/MacOS/$NEW_EXE_NAME"
[ -f "$NEW_EXE" ] && [ -x "$NEW_EXE" ] || {{ cleanup; fail "new app executable missing or not executable: $NEW_EXE"; }}
if command -v lipo >/dev/null 2>&1; then
    ARCH_INFO=$(lipo -archs "$NEW_EXE" 2>/dev/null) || {{ cleanup; fail "cannot inspect executable architecture: $NEW_EXE"; }}
else
    ARCH_INFO=$(file "$NEW_EXE") || {{ cleanup; fail "cannot inspect executable architecture: $NEW_EXE"; }}
fi
printf '%s\n' "$ARCH_INFO" | tr ' ' '\n' | grep -qx "$EXPECTED_ARCH" || {{ cleanup; fail "architecture mismatch: expected $EXPECTED_ARCH, got $ARCH_INFO"; }}
if codesign -dv "$STABLE_APP" >/dev/null 2>&1; then
    codesign --verify --deep --strict "$STABLE_APP" || {{ cleanup; fail "signed app verification failed"; }}
else
    log "unsigned test package accepted"
fi

APP_PARENT=$(dirname "$APP_PATH")
mkdir -p "$APP_PARENT" || {{ cleanup; fail "create app parent failed: $APP_PARENT"; }}
rm -rf "$BACKUP_APP"
if [ -d "$APP_PATH" ]; then
    ditto "$APP_PATH" "$BACKUP_APP" || {{ cleanup; fail "backup app failed: $APP_PATH"; }}
    rm -rf "$APP_PATH" || {{ cleanup; fail "delete old app failed: $APP_PATH"; }}
fi

if ! ditto "$STABLE_APP" "$APP_PATH"; then
    rollback
    cleanup
    fail "ditto app failed"
fi
chmod -R u+rwX "$APP_PATH" || {{ rollback; cleanup; fail "chmod app failed"; }}
chmod -R u+x "$APP_PATH/Contents/MacOS" || {{ rollback; cleanup; fail "chmod executable failed"; }}
VERSION_FILE="$APP_PATH/Contents/Resources/src/VERSION"
[ -f "$VERSION_FILE" ] || {{ rollback; cleanup; fail "VERSION missing after copy"; }}
ACTUAL_VERSION=$(tr -d '\r\n' < "$VERSION_FILE")
[ "$ACTUAL_VERSION" = "$TARGET_VERSION" ] || {{ rollback; cleanup; fail "VERSION mismatch: expected $TARGET_VERSION, got $ACTUAL_VERSION"; }}
if codesign -dv "$APP_PATH" >/dev/null 2>&1; then
    codesign --verify --deep --strict "$APP_PATH" || {{ rollback; cleanup; fail "installed signature verification failed"; }}
fi

rm -rf "$BACKUP_APP"
cleanup
open -n "$APP_PATH" || fail "restart failed: $APP_PATH"
log "restart launched: $APP_PATH"
"""
        _write_macos_update_script(script_path, script_content)
        subprocess.Popen(
            ["/bin/sh", str(script_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        logger.info("macOS 完整包更新脚本已启动，即将退出应用以完成更新")
        return True

    except Exception as e:
        logger.error(f"macOS 打包模式更新失败: {e}")
        return False


def _apply_frozen_update_windows(zip_path: Path) -> bool:
    """Windows 打包模式：PowerShell 等待退出后替换整个应用目录"""
    import subprocess

    try:
        current_exe = Path(sys.executable)
        app_dir = current_exe.parent
        powershell_exe = _get_powershell_exe()
        if not powershell_exe:
            logger.error("未找到 powershell.exe，无法执行 Windows 完整包更新")
            return False

        tmp_dir = Path(tempfile.gettempdir()) / "ag_full_update"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_dir)

        new_exe = None
        for exe_path in tmp_dir.rglob("*.exe"):
            if "Antigravity Tools" in exe_path.name or "antigravity" in exe_path.name.lower():
                new_exe = exe_path
                break
        if not new_exe:
            for exe_path in tmp_dir.rglob("*.exe"):
                new_exe = exe_path
                break
        if not new_exe:
            logger.error("更新包中未找到 exe 文件")
            return False

        new_app_dir = new_exe.parent
        target_version = _read_packaged_version(new_app_dir, "windows")
        if not target_version:
            logger.error("Windows 完整包缺少 _internal/src/VERSION，拒绝应用")
            return False

        stable_app = Path(tempfile.gettempdir()) / "ag_full_stable"
        if stable_app.exists():
            shutil.rmtree(stable_app, ignore_errors=True)
        shutil.copytree(new_app_dir, stable_app)

        ps_path = Path(tempfile.gettempdir()) / "ag_full_updater.ps1"
        log_path = Path(tempfile.gettempdir()) / "ag_update_error.log"
        ps_content = f"""$ErrorActionPreference = "Stop"
$LogPath = {_ps_quote(log_path)}
$AppDir = {_ps_quote(app_dir)}
$StableApp = {_ps_quote(stable_app)}
$TmpDir = {_ps_quote(tmp_dir)}
$CurrentExe = {_ps_quote(current_exe)}
$ScriptPath = {_ps_quote(ps_path)}
$TargetVersion = {_ps_quote(target_version)}
$BackupApp = {_ps_quote(Path(tempfile.gettempdir()) / f"ag_full_backup_{os.getpid()}")}
$OldPid = {os.getpid()}

function Write-UpdateLog([string]$Message) {{
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    "$ts $Message" | Add-Content -LiteralPath $LogPath -Encoding UTF8
}}

try {{
    "full update script started" | Out-File -LiteralPath $LogPath -Encoding UTF8
    Write-UpdateLog "waiting old pid: $OldPid"
    Start-Sleep -Seconds 2

    $Deadline = (Get-Date).AddSeconds(60)
    while (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {{
        if ((Get-Date) -ge $Deadline) {{ throw "timeout waiting for old process" }}
        Start-Sleep -Milliseconds 500
    }}
    Write-UpdateLog "old process exited"

    if (-not (Test-Path -LiteralPath $StableApp)) {{
        throw "stable app copy missing: $StableApp"
    }}

    Remove-Item -LiteralPath $BackupApp -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $AppDir) {{
        Copy-Item -LiteralPath $AppDir -Destination $BackupApp -Recurse -Force
        Remove-Item -LiteralPath $AppDir -Recurse -Force
    }}
    if (Test-Path -LiteralPath $AppDir) {{
        throw "delete app dir failed: $AppDir"
    }}

    New-Item -ItemType Directory -Path $AppDir -Force | Out-Null
    Get-ChildItem -LiteralPath $StableApp -Force | ForEach-Object {{
        Copy-Item -LiteralPath $_.FullName -Destination $AppDir -Recurse -Force
    }}

    if (-not (Test-Path -LiteralPath $CurrentExe)) {{
        throw "exe missing after copy: $CurrentExe"
    }}

    if ($TargetVersion) {{
        $VersionFile = Join-Path $AppDir "_internal\\src\\VERSION"
        if (-not (Test-Path -LiteralPath $VersionFile)) {{
            throw "VERSION missing after full copy: $VersionFile"
        }}
        $ActualVersion = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
        if ($ActualVersion -ne $TargetVersion) {{
            throw "VERSION verify failed: expected $TargetVersion, got $ActualVersion"
        }}
        Write-UpdateLog "version verified: $ActualVersion"
    }}

    Remove-Item -LiteralPath $BackupApp -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $TmpDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $StableApp -Recurse -Force -ErrorAction SilentlyContinue

    $ExeDir = Split-Path -Parent $CurrentExe
    Start-Process -FilePath $CurrentExe -WorkingDirectory $ExeDir
    Write-UpdateLog "restart launched: $CurrentExe"

    Remove-Item -LiteralPath $ScriptPath -Force -ErrorAction SilentlyContinue
}}
catch {{
    $err = ($_ | Out-String).Trim()
    Write-UpdateLog "ERROR: $err"
    if (Test-Path -LiteralPath $BackupApp) {{
        Remove-Item -LiteralPath $AppDir -Recurse -Force -ErrorAction SilentlyContinue
        Copy-Item -LiteralPath $BackupApp -Destination $AppDir -Recurse -Force
        Write-UpdateLog "full app rollback completed"
    }}
    Remove-Item -LiteralPath $TmpDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $StableApp -Recurse -Force -ErrorAction SilentlyContinue
    exit 1
}}
"""

        ps_path.write_text(ps_content, encoding="utf-8-sig")

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0

        subprocess.Popen(
            [
                powershell_exe,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-File", str(ps_path),
            ],
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        logger.info("Windows 完整包更新 PowerShell 已启动，即将退出应用以完成更新")
        return True

    except Exception as e:
        logger.error(f"Windows 打包模式更新失败: {e}")
        return False


def _apply_update(zip_path: Path) -> bool:
    """应用更新包 — 解压覆盖 src/ 目录

    注意：PyInstaller 打包后此功能不可用（代码在 _MEIPASS 临时目录中）。
    打包模式下提示用户到官网下载新版。
    """
    try:
        if getattr(sys, 'frozen', False):
            logger.warning("打包模式下不支持自动更新覆盖，请手动下载新版")
            return False

        project_root = Path(__file__).parent.parent.parent  # 项目根目录
        src_dir = project_root / "src"

        # 解压到临时目录
        with tempfile.TemporaryDirectory(prefix="ag_update_") as tmp_dir:
            tmp = Path(tmp_dir)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmp)

            # 查找解压后的 src 目录（可能在根目录或子目录下）
            extracted_src = tmp / "src"
            if not extracted_src.exists():
                # 可能有一层包装目录
                for sub in tmp.iterdir():
                    if sub.is_dir() and (sub / "src").exists():
                        extracted_src = sub / "src"
                        break

            if not extracted_src.exists():
                logger.error("更新包中未找到 src/ 目录")
                return False

            target_version_file = extracted_src / "VERSION"
            if not target_version_file.is_file():
                logger.error("更新包缺少 src/VERSION，拒绝应用")
                return False
            target_version = target_version_file.read_text(encoding="utf-8").strip()
            if not target_version:
                logger.error("更新包 src/VERSION 为空，拒绝应用")
                return False

            backup_dir = project_root / f"src_backup_{os.getpid()}"
            replacement_dir = project_root / f"src_replacement_{os.getpid()}"
            shutil.rmtree(backup_dir, ignore_errors=True)
            shutil.rmtree(replacement_dir, ignore_errors=True)
            shutil.copytree(extracted_src, replacement_dir)
            shutil.copytree(src_dir, backup_dir)
            try:
                shutil.rmtree(src_dir)
                replacement_dir.replace(src_dir)
                actual_version = VERSION_FILE.read_text(encoding="utf-8").strip()
                if actual_version != target_version:
                    raise RuntimeError(
                        f"VERSION verify failed: expected {target_version}, got {actual_version}"
                    )
            except Exception:
                shutil.rmtree(src_dir, ignore_errors=True)
                if backup_dir.exists():
                    backup_dir.replace(src_dir)
                raise
            finally:
                shutil.rmtree(replacement_dir, ignore_errors=True)
            shutil.rmtree(backup_dir, ignore_errors=True)

        logger.info("更新应用成功")
        return True

    except Exception as e:
        logger.error(f"应用更新失败: {e}")
        return False


class UpdateChecker(QObject):
    """自动更新检查器 — 在主线程中运行，通过信号通知UI"""

    # 信号：发现新版本 (version, changelog, download_url, sha256)
    update_available = Signal(str, str, str, str)
    # 信号：更新下载进度 (downloaded_bytes, total_bytes)
    download_progress = Signal(int, int)
    # 信号：更新完成 (success: bool, message: str)
    update_finished = Signal(bool, str)
    # 信号：检查完成但无更新 (is_manual: bool)
    no_update = Signal(bool)

    # 存储增量包 URL（检测到时保存，下载时用）
    _src_download_url = ""
    _src_download_url_fallback = ""
    _src_sha256 = ""
    _src_sha256_fallback = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.check_update)
        self._checking = False
        self._downloading = False
        self._manual_check = False  # 标记是否为手动检查
        self._notified_version = ""  # 已经提示过的版本号，同一版本不重复弹窗

    def start_periodic_check(self, interval_ms: int = 3600_000):
        """启动定期检查（默认1小时）"""
        # 首次延迟5秒检查（等UI完全加载）
        QTimer.singleShot(5000, self.check_update)
        self._timer.start(interval_ms)

    def stop(self):
        """停止定期检查"""
        self._timer.stop()

    @Slot()
    def check_update(self):
        """检查是否有新版本（后台线程）"""
        if self._checking or self._downloading:
            return
        self._checking = True

        def _do_check():
            current = get_current_version()
            info = _fetch_version_info()
            self._checking = False

            if not info:
                self.no_update.emit(self._manual_check)
                return

            # 分平台读取版本信息
            platform_key = _get_platform_key()
            platforms = info.get("platforms", {})

            if platforms:
                # 新格式：分平台
                platform_info = platforms.get(platform_key)
                if not platform_info:
                    # 该平台没有发布更新，不提示
                    logger.info(f"平台 {platform_key} 暂无更新信息")
                    self.no_update.emit(self._manual_check)
                    return
                remote_ver = platform_info.get("version", "0.0.0")
                changelog = platform_info.get("changelog", "")
                download_url = platform_info.get("download_url", "")
                sha256 = platform_info.get("sha256", "")
                self._src_download_url = platform_info.get("src_download_url", "")
                self._src_download_url_fallback = platform_info.get("src_download_url_fallback", "")
                self._src_sha256 = platform_info.get("src_sha256", "")
                self._src_sha256_fallback = platform_info.get("src_sha256_fallback", "")
            else:
                # 兼容旧格式（无 platforms 字段）
                remote_ver = info.get("version", "0.0.0")
                changelog = info.get("changelog", "")
                download_url = info.get("download_url", "")
                sha256 = info.get("sha256", "")
                self._src_download_url = info.get("src_download_url", "")
                self._src_download_url_fallback = info.get("src_download_url_fallback", "")
                self._src_sha256 = info.get("src_sha256", "")
                self._src_sha256_fallback = info.get("src_sha256_fallback", "")

            if _compare_versions(remote_ver, current):
                # 检查是否跳过了此版本
                from ..utils.store import load_setting
                skip_ver = load_setting("skip_version", "")
                if skip_ver == remote_ver and not self._manual_check:
                    logger.info(f"版本 {remote_ver} 已被跳过")
                    self.no_update.emit(self._manual_check)
                elif self._notified_version == remote_ver and not self._manual_check:
                    # 已经提示过这个版本了，用户没关窗口前不重复弹
                    logger.info(f"版本 {remote_ver} 已提示过，不重复弹窗")
                    self.no_update.emit(self._manual_check)
                else:
                    self._notified_version = remote_ver
                    self.update_available.emit(remote_ver, changelog, download_url, sha256)
            else:
                self.no_update.emit(self._manual_check)

            self._manual_check = False

        threading.Thread(target=_do_check, daemon=True).start()

    def download_and_apply(self, download_url: str, sha256: str = ""):
        """下载并应用更新（后台线程）

        源码模式：下载 zip 解压覆盖 src/
        打包模式：打开浏览器下载完整安装包
        """
        if self._downloading:
            return
        self._downloading = True

        def _do_download():
            try:
                # 打包模式
                if getattr(sys, 'frozen', False):
                    tmp_dir = Path(tempfile.gettempdir()) / "antigravity-update"
                    tmp_dir.mkdir(exist_ok=True)

                    def _progress(downloaded, total):
                        self.download_progress.emit(downloaded, total)

                    # 优先尝试增量更新；每个候选都执行相同的完整性校验。
                    src_urls = [self._src_download_url]
                    if self._src_download_url_fallback not in src_urls:
                        src_urls.append(self._src_download_url_fallback)
                    for src_url in (url for url in src_urls if url):
                        src_zip = tmp_dir / "update-src.zip"
                        src_zip.unlink(missing_ok=True)
                        logger.info("尝试增量更新: %s", src_url)
                        if not _download_update(src_url, src_zip, _progress, timeout=120):
                            logger.warning("增量包下载失败，尝试下一个候选")
                            continue
                        if not _verify_sha256(src_zip, self._src_sha256):
                            logger.warning("增量更新包 SHA256 校验失败，尝试下一个候选")
                            continue
                        if _apply_src_only_update(src_zip):
                            self.update_finished.emit(True, "UPDATE_NEED_RESTART")
                            return
                        logger.warning("增量更新应用失败，回退到完整包")
                        break

                    # 完整包更新
                    zip_path = tmp_dir / "update.zip"

                    if not _download_update(download_url, zip_path, _progress):
                        self.update_finished.emit(False, "下载更新包失败")
                        return

                    # 校验
                    if sha256 and not _verify_sha256(zip_path, sha256):
                        self.update_finished.emit(False, "文件校验失败，可能被篡改")
                        return

                    # 应用更新（完整包替换）
                    if _apply_frozen_update(zip_path):
                        self.update_finished.emit(True, "UPDATE_NEED_RESTART")
                    else:
                        self.update_finished.emit(False, "应用更新失败")
                    return

                # 源码模式：下载 zip 解压覆盖 src/
                tmp_dir = Path(tempfile.gettempdir()) / "antigravity-update"
                tmp_dir.mkdir(exist_ok=True)
                zip_path = tmp_dir / "update.zip"

                def _progress(downloaded, total):
                    self.download_progress.emit(downloaded, total)

                if not _download_update(download_url, zip_path, _progress):
                    self.update_finished.emit(False, "下载更新包失败")
                    return

                # 校验
                if sha256 and not _verify_sha256(zip_path, sha256):
                    self.update_finished.emit(False, "文件校验失败，可能被篡改")
                    return

                # 应用更新
                if _apply_update(zip_path):
                    self.update_finished.emit(True, "更新成功，需要重启应用才能生效。")
                else:
                    self.update_finished.emit(False, "应用更新失败")

            except Exception as e:
                self.update_finished.emit(False, f"更新出错: {e}")
            finally:
                self._downloading = False
                # 清理临时文件
                try:
                    tmp_dir = Path(tempfile.gettempdir()) / "antigravity-update"
                    if tmp_dir.exists():
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass

        threading.Thread(target=_do_download, daemon=True).start()
