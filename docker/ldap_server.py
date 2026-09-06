"""
LDAP Server for Authentication
A lightweight LDAP server for development and testing purposes.
Uses ldap3's mock server with persistent storage.
"""

import hashlib
import json
import socketserver
import threading
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# LDAP Protocol Constants
# ─────────────────────────────────────────────────────────────────────────────


class LDAPOperation(IntEnum):
    """LDAP protocol operation codes."""

    BIND_REQUEST = 0
    BIND_RESPONSE = 1
    UNBIND_REQUEST = 2
    SEARCH_REQUEST = 3
    SEARCH_RESULT_ENTRY = 4
    SEARCH_RESULT_DONE = 5
    MODIFY_REQUEST = 6
    MODIFY_RESPONSE = 7
    ADD_REQUEST = 8
    ADD_RESPONSE = 9
    DELETE_REQUEST = 10
    DELETE_RESPONSE = 11


class LDAPResultCode(IntEnum):
    """LDAP result status codes."""

    SUCCESS = 0
    OPERATIONS_ERROR = 1
    PROTOCOL_ERROR = 2
    INVALID_CREDENTIALS = 49
    NO_SUCH_OBJECT = 32
    ENTRY_ALREADY_EXISTS = 68
    UNWILLING_TO_PERFORM = 53


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class LDAPUser:
    """Representation of an LDAP user entry."""

    uid: str
    cn: str  # Common Name
    sn: str  # Surname
    mail: str
    password_hash: str
    object_class: list = field(default_factory=lambda: ["inetOrgPerson", "posixAccount", "user"])
    gid_number: int = 1000
    uid_number: int = 1000
    home_directory: str = ""
    login_shell: str = "/bin/bash"
    display_name: str = ""  # displayName attribute (defaults to cn)
    sam_account_name: str = ""  # sAMAccountName attribute (defaults to uid)
    member_of: list = field(default_factory=list)  # memberOf - list of group DNs

    def __post_init__(self):
        if not self.home_directory:
            self.home_directory = f"/home/{self.uid}"
        if not self.display_name:
            self.display_name = self.cn
        if not self.sam_account_name:
            self.sam_account_name = self.uid

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using SHA-256."""
        return hashlib.sha256(password.encode()).hexdigest()

    def verify_password(self, password: str) -> bool:
        """Verify a password against the stored hash."""
        return self.password_hash == self.hash_password(password)

    def to_ldap_entry(self, base_dn: str) -> dict:
        """Convert user to an LDAP entry dictionary."""
        attrs = {
            "uid": self.uid,
            "cn": self.cn,
            "sn": self.sn,
            "mail": self.mail,
            "objectClass": self.object_class,
            "gidNumber": str(self.gid_number),
            "uidNumber": str(self.uid_number),
            "homeDirectory": self.home_directory,
            "loginShell": self.login_shell,
            "displayName": self.display_name,
            "sAMAccountName": self.sam_account_name,
        }
        if self.member_of:
            attrs["memberOf"] = self.member_of
        return {"dn": f"uid={self.uid},ou=users,{base_dn}", "attributes": attrs}


@dataclass
class LDAPGroup:
    """Representation of an LDAP group entry."""

    cn: str  # Group name
    gid_number: int
    members: list = field(default_factory=list)
    object_class: list = field(default_factory=lambda: ["posixGroup"])

    def to_ldap_entry(self, base_dn: str) -> dict:
        """Convert group to an LDAP entry dictionary."""
        return {
            "dn": f"cn={self.cn},ou=groups,{base_dn}",
            "attributes": {
                "cn": self.cn,
                "gidNumber": str(self.gid_number),
                "memberUid": self.members,
                "objectClass": self.object_class,
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# LDAP Directory Storage
# ─────────────────────────────────────────────────────────────────────────────


class LDAPDirectory:
    """In-memory LDAP directory with optional JSON persistence."""

    def __init__(self, base_dn: str = "dc=example,dc=com", storage_path: Optional[Path] = None):
        self.base_dn = base_dn
        self.storage_path = storage_path
        self.users: dict[str, LDAPUser] = {}
        self.groups: dict[str, LDAPGroup] = {}
        self.admin_dn = f"cn=admin,{base_dn}"
        self.admin_password_hash = LDAPUser.hash_password("admin")

        if storage_path and storage_path.exists():
            self._load()
        else:
            self._init_default_entries()

    def _init_default_entries(self):
        """Initialize with default users and groups."""
        # Default groups (create first so we can reference their DNs)
        self.add_group(LDAPGroup(cn="admins", gid_number=1000, members=["admin"]))
        self.add_group(LDAPGroup(cn="users", gid_number=1001, members=["admin", "testuser", "bizowner2", "relayopsmember1", "relayopsmember2"]))
        self.add_group(LDAPGroup(cn="developers", gid_number=1002, members=["testuser", "bizowner2"]))
        self.add_group(LDAPGroup(cn="relayops-support", gid_number=1003, members=["relayopsmember1", "relayopsmember2"]))
        self.add_group(LDAPGroup(cn="relayops-demo-shared-ops", gid_number=1004, members=["admin", "testuser", "relayopsmember1"]))

        # Default admin user (Platform Admin via config.yaml)
        self.add_user(
            LDAPUser(
                uid="admin",
                cn="System Administrator",
                sn="Admin",
                mail="admin@example.com",
                password_hash=LDAPUser.hash_password("admin123"),
                uid_number=1000,
                display_name="System Administrator",
                sam_account_name="admin",
                member_of=[
                    f"cn=admins,ou=groups,{self.base_dn}",
                    f"cn=users,ou=groups,{self.base_dn}",
                    f"cn=relayops-demo-shared-ops,ou=groups,{self.base_dn}",
                ],
            )
        )

        # Business Owner 1 (default test user)
        self.add_user(
            LDAPUser(
                uid="testuser",
                cn="Test User",
                sn="User",
                mail="testuser@example.com",
                password_hash=LDAPUser.hash_password("test123"),
                uid_number=1001,
                display_name="Test User",
                sam_account_name="testuser",
                member_of=[
                    f"cn=users,ou=groups,{self.base_dn}",
                    f"cn=developers,ou=groups,{self.base_dn}",
                    f"cn=relayops-demo-shared-ops,ou=groups,{self.base_dn}",
                ],
            )
        )

        # Business Owner 2
        self.add_user(
            LDAPUser(
                uid="bizowner2",
                cn="Alice Wang",
                sn="Wang",
                mail="alice.wang@example.com",
                password_hash=LDAPUser.hash_password("biz123"),
                uid_number=1002,
                display_name="Alice Wang",
                sam_account_name="bizowner2",
                member_of=[
                    f"cn=users,ou=groups,{self.base_dn}",
                    f"cn=developers,ou=groups,{self.base_dn}",
                ],
            )
        )

        # Ops Member 1 (group DN contains "relayops" → relayops_member role)
        self.add_user(
            LDAPUser(
                uid="relayopsmember1",
                cn="Bob Chen",
                sn="Chen",
                mail="bob.chen@example.com",
                password_hash=LDAPUser.hash_password("relayops123"),
                uid_number=1003,
                display_name="Bob Chen",
                sam_account_name="relayopsmember1",
                member_of=[
                    f"cn=users,ou=groups,{self.base_dn}",
                    f"cn=relayops-support,ou=groups,{self.base_dn}",
                    f"cn=relayops-demo-shared-ops,ou=groups,{self.base_dn}",
                ],
            )
        )

        # Ops Member 2
        self.add_user(
            LDAPUser(
                uid="relayopsmember2",
                cn="Carol Li",
                sn="Li",
                mail="carol.li@example.com",
                password_hash=LDAPUser.hash_password("relayops123"),
                uid_number=1004,
                display_name="Carol Li",
                sam_account_name="relayopsmember2",
                member_of=[
                    f"cn=users,ou=groups,{self.base_dn}",
                    f"cn=relayops-support,ou=groups,{self.base_dn}",
                ],
            )
        )

        self._save()

    def _load(self):
        """Load directory from JSON file."""
        if not self.storage_path:
            return
        try:
            data = json.loads(self.storage_path.read_text())
            for user_data in data.get("users", []):
                self.users[user_data["uid"]] = LDAPUser(**user_data)
            for group_data in data.get("groups", []):
                self.groups[group_data["cn"]] = LDAPGroup(**group_data)
        except Exception as e:
            print(f"⚠ Failed to load directory: {e}")
            self._init_default_entries()

    def _save(self):
        """Persist directory to JSON file."""
        if not self.storage_path:
            return
        data = {
            "users": [asdict(u) for u in self.users.values()],
            "groups": [asdict(g) for g in self.groups.values()],
        }
        self.storage_path.write_text(json.dumps(data, indent=2))

    def add_user(self, user: LDAPUser) -> bool:
        """Add a user to the directory."""
        if user.uid in self.users:
            return False
        self.users[user.uid] = user
        self._save()
        return True

    def remove_user(self, uid: str) -> bool:
        """Remove a user from the directory."""
        if uid not in self.users:
            return False
        del self.users[uid]
        for group in self.groups.values():
            if uid in group.members:
                group.members.remove(uid)
        self._save()
        return True

    def get_user(self, uid: str) -> Optional[LDAPUser]:
        """Return a user by UID or None if not found."""
        return self.users.get(uid)

    def add_group(self, group: LDAPGroup) -> bool:
        """Add a group to the directory."""
        if group.cn in self.groups:
            return False
        self.groups[group.cn] = group
        self._save()
        return True

    def authenticate(self, dn: str, password: str) -> bool:
        """Authenticate a user by DN and password."""
        # Check admin bind
        if dn == self.admin_dn:
            return self.admin_password_hash == LDAPUser.hash_password(password)

        # Parse uid from DN
        if not dn.startswith("uid="):
            return False

        uid = dn.split(",")[0].replace("uid=", "")
        user = self.get_user(uid)

        if user and user.verify_password(password):
            return True
        return False

    def search(self, base_dn: str, scope: int, filter_str: str) -> list[dict]:
        """Search the directory. Supports basic filters."""
        results = []

        print(f"  📂 Searching base_dn={base_dn}, filter={filter_str}")

        # Return all users if searching users OU or base DN
        if "ou=users" in base_dn.lower() or base_dn == self.base_dn:
            for user in self.users.values():
                if self._matches_filter(user, filter_str):
                    entry = user.to_ldap_entry(self.base_dn)
                    print(f"  ✓ Matched user: {user.uid} (displayName={user.display_name})")
                    results.append(entry)

        # Return all groups if searching groups OU
        if "ou=groups" in base_dn.lower() or base_dn == self.base_dn:
            for group in self.groups.values():
                results.append(group.to_ldap_entry(self.base_dn))

        print(f"  📋 Total results: {len(results)}")
        return results

    def _matches_filter(self, user: LDAPUser, filter_str: str) -> bool:
        """Basic LDAP filter matching."""
        if not filter_str or filter_str == "(objectClass=*)":
            return True

        # Parse simple equality filters like (uid=admin) or (sAMAccountName=admin)
        if filter_str.startswith("(") and filter_str.endswith(")"):
            inner = filter_str[1:-1]
            if "=" in inner:
                attr, value = inner.split("=", 1)
                attr = attr.lower()
                value_lower = value.lower()
                if attr == "uid":
                    return user.uid.lower() == value_lower or value == "*"
                elif attr == "cn":
                    return user.cn.lower() == value_lower or value == "*"
                elif attr == "mail":
                    return user.mail.lower() == value_lower or value == "*"
                elif attr == "samaccountname":
                    # Match either sam_account_name or uid (they're usually the same)
                    return user.sam_account_name.lower() == value_lower or user.uid.lower() == value_lower or value == "*"
                elif attr == "displayname":
                    return user.display_name.lower() == value_lower or value == "*"
        return True


# ─────────────────────────────────────────────────────────────────────────────
# BER Encoding/Decoding (Simplified for LDAP)
# ─────────────────────────────────────────────────────────────────────────────


class BERDecoder:
    """Basic Encoding Rules decoder for LDAP messages."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read_byte(self) -> int:
        """Read a single byte from the buffer."""
        if self.pos >= len(self.data):
            raise ValueError("Unexpected end of data")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read_length(self) -> int:
        """Read a BER-encoded length value."""
        first = self.read_byte()
        if first < 128:
            return first
        num_octets = first & 0x7F
        length = 0
        for _ in range(num_octets):
            length = (length << 8) | self.read_byte()
        return length

    def read_tag(self) -> tuple[int, int]:
        """Read a BER tag and its length."""
        tag = self.read_byte()
        length = self.read_length()
        return tag, length

    def read_integer(self) -> int:
        """Read a BER-encoded integer."""
        tag, length = self.read_tag()
        value = 0
        for _ in range(length):
            value = (value << 8) | self.read_byte()
        return value

    def read_string(self) -> str:
        """Read a BER-encoded UTF-8 string."""
        tag, length = self.read_tag()
        value = self.data[self.pos : self.pos + length]
        self.pos += length
        return value.decode("utf-8", errors="replace")

    def read_raw(self, length: int) -> bytes:
        """Read raw bytes of the given length."""
        value = self.data[self.pos : self.pos + length]
        self.pos += length
        return value


class BEREncoder:
    """Basic Encoding Rules encoder for LDAP messages."""

    @staticmethod
    def encode_length(length: int) -> bytes:
        """Encode an integer as a BER length field."""
        if length < 128:
            return bytes([length])
        result = []
        while length:
            result.append(length & 0xFF)
            length >>= 8
        result.reverse()
        return bytes([0x80 | len(result)]) + bytes(result)

    @staticmethod
    def encode_integer(value: int, tag: int = 0x02) -> bytes:
        """Encode an integer as a BER element."""
        if value == 0:
            return bytes([tag, 1, 0])
        result = []
        while value:
            result.append(value & 0xFF)
            value >>= 8
        result.reverse()
        if result[0] & 0x80:
            result.insert(0, 0)
        return bytes([tag]) + BEREncoder.encode_length(len(result)) + bytes(result)

    @staticmethod
    def encode_string(value: str, tag: int = 0x04) -> bytes:
        """Encode a string as a BER octet string."""
        encoded = value.encode("utf-8")
        return bytes([tag]) + BEREncoder.encode_length(len(encoded)) + encoded

    @staticmethod
    def encode_sequence(contents: bytes, tag: int = 0x30) -> bytes:
        """Encode bytes as a BER sequence."""
        return bytes([tag]) + BEREncoder.encode_length(len(contents)) + contents

    @staticmethod
    def encode_enumerated(value: int) -> bytes:
        """Encode an integer as a BER enumerated value."""
        return bytes([0x0A, 1, value])


# ─────────────────────────────────────────────────────────────────────────────
# LDAP Protocol Handler
# ─────────────────────────────────────────────────────────────────────────────


class LDAPRequestHandler(socketserver.BaseRequestHandler):
    """Handle LDAP protocol requests."""

    def handle(self):
        """Handle an incoming LDAP connection."""
        self.bound = False
        self.bound_dn = None

        while True:
            try:
                # Read LDAP message
                header = self.request.recv(2)
                if not header or len(header) < 2:
                    break

                # Parse BER sequence header
                if header[0] != 0x30:
                    break

                # Get message length
                if header[1] < 128:
                    msg_len = header[1]
                else:
                    num_octets = header[1] & 0x7F
                    len_bytes = self.request.recv(num_octets)
                    msg_len = int.from_bytes(len_bytes, "big")

                # Read message body
                body = b""
                while len(body) < msg_len:
                    chunk = self.request.recv(msg_len - len(body))
                    if not chunk:
                        break
                    body += chunk

                if len(body) < msg_len:
                    break

                # Process the message
                response = self.process_message(body)
                if response:
                    self.request.sendall(response)

            except Exception as e:
                print(f"⚠ Error handling request: {e}")
                break

    def process_message(self, body: bytes) -> Optional[bytes]:
        """Parse and dispatch an LDAP message to the appropriate handler."""
        decoder = BERDecoder(body)

        # Read message ID
        message_id = decoder.read_integer()

        # Read operation tag
        op_tag = decoder.read_byte()
        op_length = decoder.read_length()

        # Handle different operations
        if op_tag == 0x60:  # Bind Request
            return self.handle_bind(message_id, decoder)
        elif op_tag == 0x42:  # Unbind Request
            return None  # No response for unbind
        elif op_tag == 0x63:  # Search Request
            return self.handle_search(message_id, decoder, op_length)
        else:
            return self.make_response(
                message_id,
                LDAPOperation.BIND_RESPONSE,
                LDAPResultCode.UNWILLING_TO_PERFORM,
                "",
                "Operation not supported",
            )

    def handle_bind(self, message_id: int, decoder: BERDecoder) -> bytes:
        """Handle an LDAP bind (authentication) request."""
        try:
            decoder.read_integer()
            dn = decoder.read_string()

            # Read authentication (simple)
            decoder.read_byte()
            auth_len = decoder.read_length()
            password = decoder.read_raw(auth_len).decode("utf-8", errors="replace")

            directory: LDAPDirectory = self.server.directory

            if not dn:
                # Anonymous bind
                self.bound = True
                self.bound_dn = ""
                result_code = LDAPResultCode.SUCCESS
                message = ""
            elif directory.authenticate(dn, password):
                self.bound = True
                self.bound_dn = dn
                result_code = LDAPResultCode.SUCCESS
                message = ""
                print(f"✓ Authenticated: {dn}")
            else:
                result_code = LDAPResultCode.INVALID_CREDENTIALS
                message = "Invalid credentials"
                print(f"✗ Auth failed: {dn}")

            return self.make_response(message_id, LDAPOperation.BIND_RESPONSE, result_code, dn, message)
        except Exception as e:
            print(f"⚠ Bind error: {e}")
            return self.make_response(
                message_id,
                LDAPOperation.BIND_RESPONSE,
                LDAPResultCode.PROTOCOL_ERROR,
                "",
                str(e),
            )

    def handle_search(self, message_id: int, decoder: BERDecoder, length: int) -> bytes:
        """Handle an LDAP search request."""
        try:
            base_dn = decoder.read_string()
            scope = decoder.read_integer()
            decoder.read_integer()
            decoder.read_integer()
            decoder.read_integer()
            decoder.read_integer()

            # Read filter (simplified - extract from BER data)
            decoder.read_byte()
            filter_len = decoder.read_length()
            filter_data = decoder.read_raw(filter_len)

            # For development mock server: use bound user's UID as filter
            # This ensures we return the authenticated user's info
            filter_str = "(objectClass=*)"  # Default: return all

            if self.bound and self.bound_dn and self.bound_dn.startswith("uid="):
                # Extract uid from bound DN like "uid=admin,ou=users,dc=example,dc=com"
                uid = self.bound_dn.split(",")[0].replace("uid=", "")
                filter_str = f"(uid={uid})"
                print(f"🔍 Search for bound user: uid={uid}")
            else:
                # Try to extract from filter data as fallback
                filter_bytes = filter_data.decode("utf-8", errors="ignore").lower()
                import re

                sam_match = re.search(r"samaccountname[^\w]*(\w+)", filter_bytes)
                uid_match = re.search(r"uid[^\w]*(\w+)", filter_bytes)

                if sam_match:
                    username = sam_match.group(1)
                    filter_str = f"(sAMAccountName={username})"
                    print(f"🔍 Search filter extracted: sAMAccountName={username}")
                elif uid_match:
                    username = uid_match.group(1)
                    filter_str = f"(uid={username})"
                    print(f"🔍 Search filter extracted: uid={username}")
                else:
                    print(f"🔍 Using default filter (all users)")

            directory: LDAPDirectory = self.server.directory
            results = directory.search(base_dn, scope, filter_str)

            response = b""

            # Send each entry
            for entry in results:
                entry_response = self.make_search_entry(message_id, entry)
                response += entry_response

            # Send search done
            response += self.make_response(
                message_id,
                LDAPOperation.SEARCH_RESULT_DONE,
                LDAPResultCode.SUCCESS,
                "",
                "",
            )
            return response

        except Exception as e:
            print(f"⚠ Search error: {e}")
            return self.make_response(
                message_id,
                LDAPOperation.SEARCH_RESULT_DONE,
                LDAPResultCode.OPERATIONS_ERROR,
                "",
                str(e),
            )

    def make_search_entry(self, message_id: int, entry: dict) -> bytes:
        """Encode a search result entry."""
        dn = entry["dn"]
        attrs = entry["attributes"]

        # Encode attributes
        attrs_encoded = b""
        for name, value in attrs.items():
            if isinstance(value, list):
                values = value
            else:
                values = [value]

            vals_encoded = b""
            for v in values:
                vals_encoded += BEREncoder.encode_string(str(v))

            attr_encoded = BEREncoder.encode_string(name) + BEREncoder.encode_sequence(vals_encoded, 0x31)
            attrs_encoded += BEREncoder.encode_sequence(attr_encoded)

        # Entry = DN + Attributes
        entry_body = BEREncoder.encode_string(dn) + BEREncoder.encode_sequence(attrs_encoded)

        # Wrap in SearchResultEntry (tag 0x64)
        entry_msg = BEREncoder.encode_sequence(entry_body, 0x64)

        # Wrap in LDAPMessage
        msg_body = BEREncoder.encode_integer(message_id) + entry_msg
        return BEREncoder.encode_sequence(msg_body)

    def make_response(
        self,
        message_id: int,
        operation: LDAPOperation,
        result_code: LDAPResultCode,
        matched_dn: str,
        diagnostic_message: str,
    ) -> bytes:
        """Create an LDAP response message."""
        # Result body: resultCode, matchedDN, diagnosticMessage
        result_body = (
            BEREncoder.encode_enumerated(result_code)
            + BEREncoder.encode_string(matched_dn)
            + BEREncoder.encode_string(diagnostic_message)
        )

        # Operation tag based on type
        op_tags = {
            LDAPOperation.BIND_RESPONSE: 0x61,
            LDAPOperation.SEARCH_RESULT_DONE: 0x65,
            LDAPOperation.MODIFY_RESPONSE: 0x67,
            LDAPOperation.ADD_RESPONSE: 0x69,
            LDAPOperation.DELETE_RESPONSE: 0x6B,
        }
        op_tag = op_tags.get(operation, 0x61)

        # Wrap in operation-specific sequence
        op_msg = BEREncoder.encode_sequence(result_body, op_tag)

        # Wrap in LDAPMessage sequence
        msg_body = BEREncoder.encode_integer(message_id) + op_msg
        return BEREncoder.encode_sequence(msg_body)


# ─────────────────────────────────────────────────────────────────────────────
# LDAP Server
# ─────────────────────────────────────────────────────────────────────────────


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Multi-threaded TCP server for handling concurrent LDAP connections."""

    allow_reuse_address = True
    daemon_threads = True


class LDAPServer:
    """
    Lightweight LDAP server for authentication testing.

    Usage:
        server = LDAPServer(host="localhost", port=1389)
        server.add_user("john", "John Doe", "john@example.com", "password123")
        server.start()
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1389,
        base_dn: str = "dc=example,dc=com",
        storage_path: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.base_dn = base_dn

        storage = Path(storage_path) if storage_path else None
        self.directory = LDAPDirectory(base_dn, storage)

        self.server = None
        self._thread = None

    def add_user(
        self,
        uid: str,
        full_name: str,
        email: str,
        password: str,
        uid_number: Optional[int] = None,
        groups: Optional[list[str]] = None,
    ) -> bool:
        """Add a user to the directory."""
        # Auto-assign UID number
        if uid_number is None:
            existing_uids = [u.uid_number for u in self.directory.users.values()]
            uid_number = max(existing_uids, default=999) + 1

        name_parts = full_name.rsplit(" ", 1)
        sn = name_parts[1] if len(name_parts) > 1 else name_parts[0]

        # Build memberOf DNs from group names
        member_of = []
        if groups:
            for group_name in groups:
                member_of.append(f"cn={group_name},ou=groups,{self.base_dn}")

        user = LDAPUser(
            uid=uid,
            cn=full_name,
            sn=sn,
            mail=email,
            password_hash=LDAPUser.hash_password(password),
            uid_number=uid_number,
            display_name=full_name,
            sam_account_name=uid,
            member_of=member_of,
        )
        return self.directory.add_user(user)

    def remove_user(self, uid: str) -> bool:
        """Remove a user from the directory."""
        return self.directory.remove_user(uid)

    def add_group(
        self,
        name: str,
        members: Optional[list[str]] = None,
        gid_number: Optional[int] = None,
    ) -> bool:
        """Add a group to the directory."""
        if gid_number is None:
            existing_gids = [g.gid_number for g in self.directory.groups.values()]
            gid_number = max(existing_gids, default=999) + 1

        group = LDAPGroup(cn=name, gid_number=gid_number, members=members or [])
        return self.directory.add_group(group)

    def start(self, blocking: bool = True):
        """Start the LDAP server."""
        self.server = ThreadedTCPServer((self.host, self.port), LDAPRequestHandler)
        self.server.directory = self.directory

        print(
            f"""
╔══════════════════════════════════════════════════════════════════╗
║                    LDAP Authentication Server                     ║
╠══════════════════════════════════════════════════════════════════╣
║  Server:   ldap://{self.host}:{self.port:<5}                              ║
║  Base DN:  {self.base_dn:<40}          ║
║  Admin DN: cn=admin,{self.base_dn:<35}   ║
╠══════════════════════════════════════════════════════════════════╣
║  Users:                                                           ║
║    • admin      / admin123  (Platform Admin)                      ║
║    • testuser   / test123   (Business Owner)                      ║
║    • bizowner2  / biz123    (Business Owner)                      ║
║    • relayopsmember1 / relayops123    (Ops Member)                          ║
║    • relayopsmember2 / relayops123    (Ops Member)                          ║
╠══════════════════════════════════════════════════════════════════╣
║  Groups:                                                          ║
║    • admins      → admin                                          ║
║    • users       → all users                                      ║
║    • developers  → testuser, bizowner2                            ║
║    • relayops-support → relayopsmember1, relayopsmember2                         ║
╠══════════════════════════════════════════════════════════════════╣
║  Press Ctrl+C to stop the server                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""
        )

        if blocking:
            try:
                self.server.serve_forever()
            except KeyboardInterrupt:
                print("\n⏹ Server stopped.")
                self.stop()
        else:
            self._thread = threading.Thread(target=self.server.serve_forever)
            self._thread.daemon = True
            self._thread.start()

    def stop(self):
        """Stop the LDAP server."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI Interface
# ─────────────────────────────────────────────────────────────────────────────


def main():
    """Run the mock LDAP server from the command line."""
    import argparse

    parser = argparse.ArgumentParser(
        description="LDAP Authentication Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python ldap_server.py                          # Start with defaults
  python ldap_server.py -p 3890                  # Custom port
  python ldap_server.py --base-dn dc=myorg,dc=com  # Custom base DN
  python ldap_server.py --storage users.json    # Persist users to file
        """,
    )

    parser.add_argument("-H", "--host", default="localhost", help="Host to bind to (default: localhost)")
    parser.add_argument("-p", "--port", type=int, default=1389, help="Port to listen on (default: 1389)")
    parser.add_argument("--base-dn", default="dc=example,dc=com", help="Base DN for the directory")
    parser.add_argument(
        "--storage",
        type=str,
        default=None,
        help="Path to JSON file for persistent storage",
    )
    parser.add_argument(
        "--add-user",
        nargs=4,
        metavar=("UID", "NAME", "EMAIL", "PASSWORD"),
        action="append",
        help="Add a user (can be repeated)",
    )

    args = parser.parse_args()

    server = LDAPServer(
        host=args.host,
        port=args.port,
        base_dn=args.base_dn,
        storage_path=args.storage,
    )

    # Add any extra users from CLI
    if args.add_user:
        for uid, name, email, password in args.add_user:
            server.add_user(uid, name, email, password)
            print(f"➕ Added user: {uid}")

    server.start()


if __name__ == "__main__":
    main()
