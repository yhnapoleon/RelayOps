"""Common/shared schemas — export payload, API keys, job execution snapshots, project members."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ExportPayloadResponse(BaseModel):
    """Structured JSON export payload for project/product/issue/audit exports."""

    export_type: str
    generated_at: datetime
    filters: Optional[Dict[str, Any]] = None
    data: Dict[str, Any]


class ApiKeyCreateRequest(BaseModel):
    """Request body for creating an API key."""

    name: str = Field(..., min_length=1, max_length=255, description="Human-readable name for this key")


class ApiKeyCreateResponse(BaseModel):
    """Response after creating an API key (includes the plaintext key — shown only once)."""

    id: int
    name: str
    key: str  # Plaintext key — only returned on creation
    key_prefix: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyResponse(BaseModel):
    """API key response (without the plaintext key)."""

    id: int
    name: str
    key_prefix: str
    is_active: int
    created_by: int
    created_at: datetime

    model_config = {"from_attributes": True}


class JobExecutionResponse(BaseModel):
    """Job execution snapshot response (recorded by the monitoring Controller's CML poll)."""

    id: int
    job_id: int
    status: str
    timestamp: datetime
    metadata_json: Optional[Dict[str, Any]] = None
    received_at: datetime
    # True when this run's failure/stale alert was dismissed as a false
    # positive. Charts/timelines render such a run as a success point rather
    # than a failure, while ``status`` keeps the real CML outcome.
    false_positive: bool = False

    model_config = {"from_attributes": True}


class ProjectMemberAdd(BaseModel):
    """Request body for adding a member to a project.

    The per-project role is set here and may be ``relayops_member`` (view-only
    collaborator) or ``product_member`` (a project editor with the same
    modify rights as the owner over the project's assets). The project's
    ``business_owner`` role is held by the creator alone (one per project)
    and changes via the owner-transfer endpoint, not by adding a member —
    so it is intentionally rejected here to prevent creating a second
    project bizowner.

    Note: this no longer touches ``User.role``. The global identity
    (admin / relayops_member / regular_user) is independent and not modified
    when assigning project membership.
    """

    username: str = Field(..., min_length=1, max_length=255)
    # Per-project role; project-bizowner is reserved for the owner. Only the
    # Members-UI-assignable roles are accepted (see ProjectRole.ASSIGNABLE).
    role: str = Field(default="relayops_member", pattern=r"^(relayops_member|product_member)$")
    support_group_id: Optional[int] = None


class ProjectMemberRoleUpdate(BaseModel):
    """Request body for changing a project member's per-project role.

    Accepts ``relayops_member`` or ``product_member``. The ``business_owner``
    role is reserved for the project's owner and changes via the
    owner-transfer endpoint, never this one.
    """

    role: str = Field(..., pattern=r"^(relayops_member|product_member)$")


class ProjectOwnerTransferRequest(BaseModel):
    """Request body for transferring a project's Business Owner.

    The username is the target user's username. The backend pre-provisions
    a pending user row if needed (same flow as ProjectMemberAdd). An admin
    must set a local password before the pending account can sign in.
    """

    username: str = Field(..., min_length=1, max_length=255)


class UserRoleChangeRequest(BaseModel):
    """Admin-side: change a user's *global* login role.

    Distinct from ProjectMember.role (which is per-project). Allowed
    values match ``UserRole.ALL`` — admin / regular_user / relayops_member.
    The legacy 'business_owner' value is normalized to 'regular_user'
    server-side, but accepting only the canonical names here keeps the
    UI honest.
    """

    role: str = Field(..., pattern=r"^(admin|regular_user|relayops_member)$")


class AdminUserResponse(BaseModel):
    """User row returned by the admin user-management list.

    ``role_locked`` records an administrator's role assignment.
    ``is_platform_owner`` protects configured owners from role demotion.
    Password hashes are never included.
    """

    id: int
    username: str
    display_name: Optional[str] = None
    role: str
    role_locked: bool
    is_platform_owner: bool
    project_count: int
    created_at: Optional[str] = None


class ProjectMemberResponse(BaseModel):
    """Project member response object.

    ``role`` is the per-project role (from ProjectMember.role) — not the
    user's global role. UI consumers should treat this as "what they are
    in this project" and read User.role separately if they need login
    identity.
    """

    id: int
    project_id: int
    user_id: int
    username: str
    display_name: Optional[str] = None
    role: Optional[str] = None
    global_role: Optional[str] = None  # The user's login-time role, informational.
    support_group_id: Optional[int] = None
    support_group_name: Optional[str] = None
    active_issue_count: int = 0
    is_owner: bool = False
    added_by: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ProjectMemberCandidateResponse(BaseModel):
    """Candidate user that can be added as a project member."""

    user_id: int
    username: str
    display_name: Optional[str] = None
    role: str

    model_config = {"from_attributes": True}
