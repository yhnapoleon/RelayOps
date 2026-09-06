"""App-level configuration: APP_NAME, LDAP. Used by routers and services."""

from core.auth import ldap_auth  # noqa: F401
from core.config import get_config

_cfg = get_config()

APP_NAME = _cfg.app_name
SPLASH_TEXT = _cfg.splash_text
FOOTNOTE = _cfg.footnote
LOGO = _cfg.logo
