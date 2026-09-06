"""Authentication and authorization — LDAP, JWT."""

from core.auth.auth import LDAPAuthService
from core.config import get_config

_cfg = get_config()

ldap_auth = LDAPAuthService(
    _cfg.ldap_server,
    _cfg.ldap_base_dn,
    _cfg.ldap_users_dn,
    _cfg.ldap_use_ssl,
)
