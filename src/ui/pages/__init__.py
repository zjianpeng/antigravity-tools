"""页面模块"""

from .dashboard import DashboardPage
from .accounts import AccountsPage
from .checkin import CheckinPage
from .settings import SettingsPage
from .api_proxy import ApiProxyPage
from .changelog import ChangelogPage

__all__ = [
    "DashboardPage", "AccountsPage", "CheckinPage",
    "SettingsPage", "ApiProxyPage", "ChangelogPage",
]
