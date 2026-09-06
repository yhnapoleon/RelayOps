from typing import Any, Dict, List, Optional

from ldap3 import NONE, Connection, Server

from core.logging import get_logger

logger = get_logger(__name__)


class LDAPAuthService:
    """
    LDAP authentication service for user login and directory queries.

    Provides methods to authenticate users against an LDAP server and
    retrieve user details including group memberships.

    Attributes:
        server: LDAP server connection object.
        base_dn: Base distinguished name for LDAP searches.
        users_dn: DN prefix for user lookups.
        user_connection: Dict mapping usernames to active LDAP connections.
    """

    def __init__(self, server_url: str, base_dn: str, users_dn: str, use_ssl: bool = True):
        """
        Initialize the LDAP authentication service.

        Args:
            server_url: LDAP server URL (e.g., 'ldap://localhost:389').
            base_dn: Base DN for searches (e.g., 'dc=example,dc=com').
            users_dn: DN prefix for user lookups (e.g., 'uid=').
            use_ssl: Whether to use SSL/TLS for connections.
        """
        # connect_timeout bounds the TCP-connect phase of every bind. Without
        # rule, idle NAT teardown, host down) makes Connection(auto_bind=True)
        # block for the OS default TCP timeout — tens of seconds to minutes —
        # pinning the request worker thread the entire time. receive_timeout
        # (set per Connection) only covers waiting for a response AFTER the
        # socket is connected, so it cannot cap a stuck connect.
        #
        # get_info=NONE skips the DSA-info / schema round-trips ldap3 issues on
        # first connect. Ops only does simple binds plus one user/group search,
        # so the schema is never consulted — fetching it just adds latency.
        self.server = Server(
            server_url, use_ssl=use_ssl, get_info=NONE, connect_timeout=5
        )
        self.base_dn = base_dn
        self.users_dn = users_dn
        self.user_connection = {}

    def login(self, username: str, password: str) -> bool:
        """Authenticate user against LDAP server.

        Returns:
            True if authentication successful, False otherwise.
        """
        user_dn = f"uid={username},ou=users,{self.base_dn}"
        if self.users_dn:
            user_dn = f"{self.users_dn}{username}"

        # Close any stale connection for this username before creating a new one
        old_conn = self.user_connection.pop(username, None)
        if old_conn is not None:
            try:
                old_conn.unbind()
            except Exception:
                logger.debug("Failed to unbind stale LDAP connection for user {}", username)

        connection = None
        try:
            connection = Connection(
                self.server,
                user=user_dn,
                password=password,
                auto_bind=True,
                receive_timeout=30,  # 30-second TTL to prevent connection leaks
            )
            self.user_connection[username] = connection
            return True
        except Exception:
            logger.opt(exception=True).error("LDAP bind error for user {}", username)
            # Clean up the connection object if it was partially created
            if connection is not None:
                try:
                    connection.unbind()
                except Exception:
                    pass
            return False

    def logout(self):
        """Clear all user connections."""
        for conn in self.user_connection.values():
            try:
                conn.unbind()
            except Exception:
                logger.opt(exception=True).debug("Error unbinding LDAP connection during logout")
        self.user_connection.clear()

    def search_users_and_groups(self, username: str) -> Optional[Dict[str, Any]]:
        """
        Search LDAP for user details including group memberships.

        Args:
            username: The username (sAMAccountName) to search for

        Returns:
            Dict with username, name, email, and member_of fields, or None if not found
        """
        connection = self.user_connection.get(username, None)
        if not connection:
            logger.warning("LDAP search connection not found for user {}, login first", username)
            return None

        try:
            return self._do_search(username, connection)
        finally:
            # This is the only consumer of the connection login() stored, so
            # release it here instead of leaving it parked in user_connection
            # forever. Keeping it parked accumulated one open socket per
            # distinct user across the lifetime of the single long-running CML
            # firewall — and logout() used to unbind *everyone's* parked socket
            # at once. Unbinding right after the search removes all three issues.
            self.user_connection.pop(username, None)
            try:
                connection.unbind()
            except Exception:
                logger.debug("Failed to unbind LDAP connection for user {}", username)

    def _do_search(self, username: str, connection) -> Optional[Dict[str, Any]]:
        """Run the user/group LDAP search on an already-bound connection."""
        try:
            connection.search(
                self.base_dn,
                search_filter=f"(&(objectClass=user)(sAMAccountName={username}))",
                attributes=["displayName", "mail", "memberOf", "cn"],
            )
            logger.debug(
                "LDAP search completed for {}, found {} entries",
                username,
                len(connection.entries),
            )
        except Exception:
            logger.opt(exception=True).error("LDAP search error for user {}", username)
            return None

        if not connection.entries:
            logger.warning("LDAP no entries found for user: {}", username)
            return None

        metadata = connection.entries[0]
        logger.debug("LDAP entry DN: {}", metadata.entry_dn)
        logger.debug("LDAP entry attributes: {}", metadata.entry_attributes_as_dict)

        # Get displayName, falling back to cn if not available
        display_name = None
        if hasattr(metadata, "displayName") and metadata.displayName.value:
            display_name = metadata.displayName.value
        elif hasattr(metadata, "cn") and metadata.cn.value:
            display_name = metadata.cn.value

        # Get email
        email = None
        if hasattr(metadata, "mail") and metadata.mail.value:
            email = metadata.mail.value

        # Get memberOf - normalize to list
        member_of = None
        if hasattr(metadata, "memberOf"):
            member_of_value = metadata.memberOf.value
            if member_of_value:
                if isinstance(member_of_value, list):
                    member_of = member_of_value
                else:
                    member_of = [member_of_value]

        result = {
            "username": username,
            "name": display_name,
            "email": email,
            "member_of": member_of,
        }
        logger.debug("LDAP user details: {}", result)
        return result

    def list_users(self) -> List[Dict[str, Any]]:
        """
        List all LDAP user entries for startup bootstrap sync.

        Returns:
            List of dicts with username, name, email, and member_of.
        """
        conn = None
        users: List[Dict[str, Any]] = []
        try:
            # Use anonymous bind for startup sync. This works with the mock LDAP
            # server used in MVP and keeps the bootstrap path simple.
            conn = Connection(self.server, auto_bind=True, receive_timeout=30)
            conn.search(
                self.base_dn,
                search_filter="(objectClass=user)",
                attributes=["sAMAccountName", "uid", "displayName", "mail", "memberOf", "cn"],
            )
            for entry in conn.entries:
                username = None
                if hasattr(entry, "sAMAccountName") and entry.sAMAccountName.value:
                    username = str(entry.sAMAccountName.value)
                elif hasattr(entry, "uid") and entry.uid.value:
                    username = str(entry.uid.value)

                if not username:
                    continue

                display_name = None
                if hasattr(entry, "displayName") and entry.displayName.value:
                    display_name = str(entry.displayName.value)
                elif hasattr(entry, "cn") and entry.cn.value:
                    display_name = str(entry.cn.value)

                email = None
                if hasattr(entry, "mail") and entry.mail.value:
                    email = str(entry.mail.value)

                member_of = None
                if hasattr(entry, "memberOf"):
                    member_of_value = entry.memberOf.value
                    if member_of_value:
                        if isinstance(member_of_value, list):
                            member_of = [str(v) for v in member_of_value]
                        else:
                            member_of = [str(member_of_value)]

                users.append(
                    {
                        "username": username.lower(),
                        "name": display_name,
                        "email": email,
                        "member_of": member_of,
                    }
                )
        except Exception:
            logger.opt(exception=True).error("LDAP full user sync query failed")
            return []
        finally:
            if conn is not None:
                try:
                    conn.unbind()
                except Exception:
                    logger.opt(exception=True).debug("LDAP connection unbind failed after list_users")

        logger.info("LDAP full user sync query returned {} users", len(users))
        return users

    def search_directory_entities(self, query: str) -> List[Dict[str, Any]]:
        """
        Search the directory for both users and groups.

        This is used by the support-group import UX and should not depend on
        a previously cached per-user LDAP bind.
        """
        normalized_query = (query or "").strip().lower()
        if len(normalized_query) < 2:
            return []

        conn = None
        results: List[Dict[str, Any]] = []
        try:
            conn = Connection(self.server, auto_bind=True, receive_timeout=30)
            conn.search(
                self.base_dn,
                search_filter="(objectClass=*)",
                attributes=[
                    "cn",
                    "displayName",
                    "mail",
                    "uid",
                    "sAMAccountName",
                    "memberOf",
                    "memberUid",
                    "gidNumber",
                    "objectClass",
                    "description",
                ],
            )
            for entry in conn.entries:
                attrs = entry.entry_attributes_as_dict
                dn = str(getattr(entry, "entry_dn", "") or "")
                object_classes = [str(v).lower() for v in (attrs.get("objectClass") or [])]

                is_group = (
                    "ou=groups" in dn.lower()
                    or "posixgroup" in object_classes
                    or "memberuid" in {key.lower() for key in attrs.keys()}
                )
                is_user = (
                    "ou=users" in dn.lower()
                    or "person" in object_classes
                    or "inetorgperson" in object_classes
                )

                if is_group:
                    name = self._first_attr_value(attrs, "cn")
                    if not name:
                        continue
                    member_uids = self._coerce_attr_list(attrs.get("memberUid"))
                    haystack = " ".join(
                        [
                            name,
                            dn,
                            " ".join(member_uids),
                            self._first_attr_value(attrs, "description"),
                        ]
                    ).lower()
                    if normalized_query not in haystack:
                        continue
                    results.append(
                        {
                            "type": "group",
                            "name": name,
                            "display_name": name,
                            "cn": name,
                            "dn": dn,
                            "description": self._first_attr_value(attrs, "description"),
                            "member_uids": member_uids,
                            "gid_number": self._first_attr_value(attrs, "gidNumber"),
                        }
                    )
                    continue

                if is_user:
                    username = (
                        self._first_attr_value(attrs, "sAMAccountName")
                        or self._first_attr_value(attrs, "uid")
                    )
                    display_name = (
                        self._first_attr_value(attrs, "displayName")
                        or self._first_attr_value(attrs, "cn")
                        or username
                    )
                    email = self._first_attr_value(attrs, "mail")
                    member_of = self._coerce_attr_list(attrs.get("memberOf"))
                    haystack = " ".join(
                        [
                            username or "",
                            display_name or "",
                            email or "",
                            dn,
                            " ".join(member_of),
                        ]
                    ).lower()
                    if username and normalized_query in haystack:
                        results.append(
                            {
                                "type": "user",
                                "username": username.lower(),
                                "name": display_name,
                                "display_name": display_name,
                                "email": email,
                                "dn": dn,
                                "member_of": member_of,
                            }
                        )
        except Exception:
            logger.opt(exception=True).error("LDAP directory search failed for query {}", query)
            return []
        finally:
            if conn is not None:
                try:
                    conn.unbind()
                except Exception:
                    logger.opt(exception=True).debug("LDAP connection unbind failed after directory search")

        logger.info("LDAP directory search for '{}' returned {} result(s)", query, len(results))
        return results

    @staticmethod
    def _first_attr_value(attrs: Dict[str, Any], key: str) -> str:
        value = attrs.get(key)
        if isinstance(value, list):
            return str(value[0]).strip() if value else ""
        return str(value).strip() if value is not None else ""

    @staticmethod
    def _coerce_attr_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []
