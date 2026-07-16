"""本地数据存储 - SQLite 持久化"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models import (
    Account, Platform, AccountStatus, PlanType,
    CheckinInfo, QuotaInfo,
)

DB_NAME = "antigravity.db"


def _get_db_path() -> Path:
    """获取数据库路径"""
    app_dir = Path.home() / ".antigravity-tools"
    try:
        app_dir.mkdir(exist_ok=True)
    except OSError:
        # 如果用户目录不可写，回退到项目目录
        project_dir = Path(__file__).parent.parent.parent
        app_dir = project_dir / "data"
        app_dir.mkdir(exist_ok=True)
    return app_dir / DB_NAME


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_get_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """初始化数据库表"""
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                uid TEXT PRIMARY KEY,
                nickname TEXT DEFAULT '',
                platform TEXT DEFAULT 'codebuddy',
                status TEXT DEFAULT 'active',
                status_reason TEXT DEFAULT '',
                plan_type TEXT DEFAULT 'free',
                domain TEXT DEFAULT '',
                enterprise_id TEXT DEFAULT '',
                enterprise_name TEXT DEFAULT '',
                auth_token TEXT DEFAULT '',
                auth_raw TEXT DEFAULT '',
                profile_raw TEXT DEFAULT '',
                usage_raw TEXT DEFAULT '',
                last_checkin_time TEXT,
                streak_days INTEGER DEFAULT 0,
                checkin_rewards TEXT DEFAULT '[]',
                daily_credit INTEGER DEFAULT 0,
                total_credits INTEGER DEFAULT 0,
                hourly_suggestions INTEGER DEFAULT 0,
                hourly_suggestions_limit INTEGER DEFAULT 0,
                weekly_chat INTEGER DEFAULT 0,
                weekly_chat_limit INTEGER DEFAULT 0,
                credits_remaining REAL DEFAULT 0,
                credits_total REAL DEFAULT 0,
                reset_time TEXT,
                quota_last_updated TEXT,
                quota_last_error TEXT,
                quota_last_error_at TEXT,
                created_at TEXT,
                last_used TEXT,
                account_group TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS fingerprints (
                device_id TEXT PRIMARY KEY,
                machine_id TEXT DEFAULT '',
                session_id TEXT DEFAULT '',
                platform TEXT DEFAULT '',
                app_version TEXT DEFAULT '',
                notes TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                key_id TEXT PRIMARY KEY,
                key_prefix TEXT DEFAULT '',
                key_value TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                bound_accounts TEXT DEFAULT '[]',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_accounts_platform ON accounts(platform);
            CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
            CREATE INDEX IF NOT EXISTS idx_accounts_group ON accounts(account_group);
        """)
        conn.commit()
    finally:
        conn.close()
    _migrate_db()


def _migrate_db():
    """数据库迁移 - 增加新列"""
    conn = get_connection()
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()]
        if "daily_credit" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN daily_credit INTEGER DEFAULT 0")
        if "total_credits" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN total_credits INTEGER DEFAULT 0")
        if "ck" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN ck TEXT DEFAULT ''")
        if "api_key" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN api_key TEXT DEFAULT ''")
        conn.commit()
    finally:
        conn.close()


def _row_to_account(row: sqlite3.Row) -> Account:
    """数据库行转 Account 对象"""
    return Account(
        uid=row["uid"],
        nickname=row["nickname"],
        platform=Platform(row["platform"]),
        status=AccountStatus(row["status"]),
        status_reason=row["status_reason"],
        plan_type=PlanType(row["plan_type"]),
        domain=row["domain"],
        enterprise_id=row["enterprise_id"],
        enterprise_name=row["enterprise_name"],
        auth_token=row["auth_token"],
        auth_raw=row["auth_raw"],
        ck=row["ck"] if "ck" in row.keys() else "",
        api_key=row["api_key"] if "api_key" in row.keys() else "",
        profile_raw=row["profile_raw"],
        usage_raw=row["usage_raw"],
        checkin=CheckinInfo(
            last_checkin_time=datetime.fromisoformat(row["last_checkin_time"]) if row["last_checkin_time"] else None,
            streak_days=row["streak_days"],
            rewards=json.loads(row["checkin_rewards"]),
            daily_credit=row["daily_credit"] if "daily_credit" in row.keys() else 0,
            total_credits=row["total_credits"] if "total_credits" in row.keys() else 0,
        ),
        quota=QuotaInfo(
            hourly_suggestions=row["hourly_suggestions"],
            hourly_suggestions_limit=row["hourly_suggestions_limit"],
            weekly_chat=row["weekly_chat"],
            weekly_chat_limit=row["weekly_chat_limit"],
            credits_remaining=row["credits_remaining"],
            credits_total=row["credits_total"],
            reset_time=datetime.fromisoformat(row["reset_time"]) if row["reset_time"] else None,
            last_updated=datetime.fromisoformat(row["quota_last_updated"]) if row["quota_last_updated"] else None,
            last_error=row["quota_last_error"],
            last_error_at=datetime.fromisoformat(row["quota_last_error_at"]) if row["quota_last_error_at"] else None,
        ),
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        last_used=datetime.fromisoformat(row["last_used"]) if row["last_used"] else None,
    )


def save_account(account: Account):
    """保存账号到数据库"""
    conn = get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO accounts (
                uid, nickname, platform, status, status_reason, plan_type,
                domain, enterprise_id, enterprise_name, auth_token,
                auth_raw, ck, api_key, profile_raw, usage_raw,
                last_checkin_time, streak_days, checkin_rewards,
                daily_credit, total_credits,
                hourly_suggestions, hourly_suggestions_limit,
                weekly_chat, weekly_chat_limit,
                credits_remaining, credits_total,
                reset_time, quota_last_updated,
                quota_last_error, quota_last_error_at,
                created_at, last_used
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            account.uid, account.nickname, account.platform.value,
            account.status.value, account.status_reason, account.plan_type.value,
            account.domain, account.enterprise_id, account.enterprise_name,
            account.auth_token, account.auth_raw, account.ck, account.api_key, account.profile_raw, account.usage_raw,
            account.checkin.last_checkin_time.isoformat() if account.checkin.last_checkin_time else None,
            account.checkin.streak_days,
            json.dumps(account.checkin.rewards),
            account.checkin.daily_credit,
            account.checkin.total_credits,
            account.quota.hourly_suggestions, account.quota.hourly_suggestions_limit,
            account.quota.weekly_chat, account.quota.weekly_chat_limit,
            account.quota.credits_remaining, account.quota.credits_total,
            account.quota.reset_time.isoformat() if account.quota.reset_time else None,
            account.quota.last_updated.isoformat() if account.quota.last_updated else None,
            account.quota.last_error,
            account.quota.last_error_at.isoformat() if account.quota.last_error_at else None,
            account.created_at.isoformat() if account.created_at else None,
            account.last_used.isoformat() if account.last_used else None,
        ))
        conn.commit()
    finally:
        conn.close()


def load_accounts(platform: Optional[Platform] = None) -> list[Account]:
    """加载账号列表"""
    conn = get_connection()
    try:
        if platform:
            rows = conn.execute("SELECT * FROM accounts WHERE platform = ?", (platform.value,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM accounts ORDER BY last_used DESC").fetchall()
        return [_row_to_account(r) for r in rows]
    finally:
        conn.close()


def delete_account(uid: str):
    """删除账号"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM accounts WHERE uid = ?", (uid,))
        conn.commit()
    finally:
        conn.close()


def save_setting(key: str, value: str):
    """保存设置项"""
    conn = get_connection()
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def load_setting(key: str, default: str = "") -> str:
    """加载设置项"""
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def load_all_settings() -> dict[str, str]:
    """加载所有设置"""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()
