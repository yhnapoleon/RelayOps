"""
Audit logging utility — records all write operations to the audit_logs table.

Usage:
    from core.services.audit_service import log_audit

    log_audit(
        user_id=current_user.user_id,
        action="create",
        entity_type="project",
        entity_id=project.id,
        new_value={"name": project.name, "description": project.description},
    )
"""

from datetime import datetime
from typing import Any, Dict, Optional

from core.logging import get_logger
from core.models.database import get_db
from core.models.entities import AuditLog, ProductVersion, ProjectVersion
from core.models.user import User

logger = get_logger(__name__)


def log_audit(
    user_id: int,
    action: str,
    entity_type: str,
    entity_id: int,
    old_value: Optional[Dict[str, Any]] = None,
    new_value: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Write an audit log entry to the database.

    This function is fire-and-forget: it logs errors but never raises,
    so it won't break the calling operation if auditing fails.

    Args:
        user_id: The user who performed the action.
        action: The action performed (create, update, delete, handover, approve, reject, etc.).
        entity_type: The type of entity affected (project, product, job, application, issue, schedule).
        entity_id: The ID of the affected entity.
        old_value: JSON-serializable dict of the entity before the change (None for create).
        new_value: JSON-serializable dict of the entity after the change (None for delete).
    """
    try:
        db = get_db()
        session = db.get_session()
        try:
            entry = AuditLog(
                user_id=user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                old_value=old_value,
                new_value=new_value,
                timestamp=datetime.utcnow(),
            )
            session.add(entry)
            session.commit()
            logger.debug(
                "Audit: user={} action={} entity={}:{}", user_id, action, entity_type, entity_id
            )
        except Exception:
            session.rollback()
            logger.opt(exception=True).warning(
                "Failed to write audit log: user={} action={} entity={}:{}",
                user_id, action, entity_type, entity_id,
            )
        finally:
            session.close()
    except Exception:
        logger.opt(exception=True).warning("Failed to get DB for audit logging")


def resolve_audit_user_id(preferred_user_id: Optional[int] = None) -> Optional[int]:
    """
    Pick a stable user id for automated audit entries.

    Priority:
      1. Explicit preferred user id
      2. First configured platform owner found in the users table
      3. First available user in the users table
    """
    if preferred_user_id is not None:
        return preferred_user_id

    try:
        from core.config import get_config

        db = get_db()
        session = db.get_session()
        try:
            for username in get_config().platform_owners:
                user = (
                    session.query(User)
                    .filter(User.username == username)
                    .first()
                )
                if user:
                    return user.id

            fallback = session.query(User).order_by(User.id.asc()).first()
            return fallback.id if fallback else None
        finally:
            session.close()
    except Exception:
        logger.opt(exception=True).warning("Failed to resolve audit user id for automated action")
        return None


def _serialize_model(obj, fields: list) -> Dict[str, Any]:
    """
    Serialize a SQLAlchemy model instance to a dict for audit logging.

    Only includes the specified fields. Converts datetime to ISO string.
    """
    result = {}
    for f in fields:
        val = getattr(obj, f, None)
        if isinstance(val, datetime):
            val = val.isoformat()
        result[f] = val
    return result


# ── Convenience serializers for each entity type ─────────────────────

PROJECT_FIELDS = [
    "id",
    "name",
    "description",
    "owner_id",
    "owner_group_id",
    "owner_group_name_snapshot",
    "is_system",
    "cml_project_name",
    "cml_project_id",
    "mmp_project_id",
    "prod_stat_url",
    "status",
    "lifecycle_status",
    "current_draft_version_id",
    "current_approved_version_id",
    "latest_version_number",
]
PROJECT_VERSION_FIELDS = [
    "id",
    "project_id",
    "version_number",
    "version_status",
    "change_summary",
    "snapshot_json",
    "completeness_summary_json",
    "submitted_by",
    "submitted_at",
    "reviewed_by",
    "reviewed_at",
    "rejection_reason",
    "derived_from_version_id",
]
PRODUCT_FIELDS = [
    "id",
    "project_id",
    "name",
    "status",
    "lifecycle_status",
    "current_draft_version_id",
    "current_approved_version_id",
    "latest_version_number",
    "is_system",
]
PRODUCT_VERSION_FIELDS = [
    "id",
    "product_id",
    "version_number",
    "version_status",
    "change_summary",
    "snapshot_json",
    "completeness_summary_json",
    "submitted_by",
    "submitted_at",
    "reviewed_by",
    "reviewed_at",
    "rejection_reason",
    "derived_from_version_id",
]
JOB_FIELDS = [
    "id",
    "product_id",
    "mmp_project_id",
    "mmp_model_id",
    "control_m_job_name",
    "cml_project_name",
    "cml_job_name",
    "cml_project_id",
    "cml_job_id",
    "schedule_cron",
    "description",
    "dependencies",
    "failure_strategy_summary",
    "dependency_notes",
    "owner_contact",
    "support_group_id",
    "support_group_name_snapshot",
    "support_group",
    "runbook_required",
    "has_mmp_dependency",
    "sla_preset",
    "sla_custom_minutes",
    "is_system",
]
APP_FIELDS = [
    "id",
    "product_id",
    "application_url",
    "health_check_url",
    "cml_project_name",
    "cml_application_name",
    "cml_subdomain",
    "cml_app_type",
    "cml_project_id",
    "cml_application_id",
    "cml_serving_url",
    "description",
    "restart_supported",
    "restart_summary",
    "owner_contact",
    "support_group_id",
    "support_group_name_snapshot",
    "support_group",
    "is_system",
]
JOB_FAILURE_SCENARIO_FIELDS = [
    "id",
    "job_id",
    "scenario_type",
    "scenario_name",
    "condition_description",
    "detection_source",
    "diagnostic_steps",
    "action_steps",
    "verification_steps",
    "escalation_target",
    "fallback_owner_type",
    "threshold_operator",
    "threshold_value",
    "threshold_feature_list",
    "email_template",
    "is_not_applicable",
    "not_applicable_signoff_by",
    "not_applicable_signoff_at",
    "is_active",
]
APP_RECOVERY_SCENARIO_FIELDS = [
    "id",
    "application_id",
    "scenario_type",
    "scenario_name",
    "condition_description",
    "action_steps",
    "verification_steps",
    "escalation_target",
    "fallback_owner_type",
    "email_template",
    "is_not_applicable",
    "not_applicable_signoff_by",
    "not_applicable_signoff_at",
    "is_active",
]
ISSUE_FIELDS = [
    "id",
    "type",
    "status",
    "title",
    "product_id",
    "product_version_id",
    "project_id",
    "project_version_id",
    "assignee_id",
    "support_group_id",
    "support_group_name",
    "owner_group_id",
    "owner_group_name",
    "assigned_via",
    "resolution_description",
    "rejection_reason",
    "selected_scenario_type",
    "selected_scenario_name",
    "action_summary_json",
    "resolution_summary_json",
]
SCHEDULE_FIELDS = ["id", "start_time", "end_time", "assignee_id", "duty_role", "note"]
NOTIFICATION_FIELDS = [
    "id",
    "user_id",
    "title",
    "message",
    "type",
    "is_read",
    "related_entity_type",
    "related_entity_id",
]
SUPPORT_GROUP_FIELDS = [
    "id",
    "group_key",
    "group_name",
    "description",
    "source_type",
    "external_ref",
    "sync_status",
    "last_synced_at",
    "is_active",
    "created_by",
]
PROJECT_SUPPORT_GROUP_FIELDS = [
    "id",
    "project_id",
    "support_group_id",
    "support_group_name_snapshot",
    "created_by",
]


def serialize_project(obj) -> Dict[str, Any]:
    return _serialize_model(obj, PROJECT_FIELDS)


def serialize_product(obj) -> Dict[str, Any]:
    return _serialize_model(obj, PRODUCT_FIELDS)


def serialize_job(obj) -> Dict[str, Any]:
    return _serialize_model(obj, JOB_FIELDS)


def serialize_app(obj) -> Dict[str, Any]:
    return _serialize_model(obj, APP_FIELDS)


def serialize_product_version(obj) -> Dict[str, Any]:
    return _serialize_model(obj, PRODUCT_VERSION_FIELDS)


def serialize_project_version(obj) -> Dict[str, Any]:
    return _serialize_model(obj, PROJECT_VERSION_FIELDS)


def serialize_job_failure_scenario(obj) -> Dict[str, Any]:
    return _serialize_model(obj, JOB_FAILURE_SCENARIO_FIELDS)


def serialize_application_recovery_scenario(obj) -> Dict[str, Any]:
    return _serialize_model(obj, APP_RECOVERY_SCENARIO_FIELDS)


def serialize_issue(obj) -> Dict[str, Any]:
    result = _serialize_model(obj, ISSUE_FIELDS)
    product_version = getattr(obj, "__dict__", {}).get("product_version")
    if product_version is None and result.get("product_version_id") is not None:
        try:
            db = get_db()
            session = db.get_session()
            try:
                product_version = (
                    session.query(ProductVersion)
                    .filter(ProductVersion.id == result["product_version_id"])
                    .first()
                )
            finally:
                session.close()
        except Exception:
            product_version = None
    result["product_version_number"] = getattr(product_version, "version_number", None)
    result["product_version_status"] = getattr(product_version, "version_status", None)

    project_version = getattr(obj, "__dict__", {}).get("project_version")
    if project_version is None and result.get("project_version_id") is not None:
        try:
            db = get_db()
            session = db.get_session()
            try:
                project_version = (
                    session.query(ProjectVersion)
                    .filter(ProjectVersion.id == result["project_version_id"])
                    .first()
                )
            finally:
                session.close()
        except Exception:
            project_version = None
    result["project_version_number"] = getattr(project_version, "version_number", None)
    result["project_version_status"] = getattr(project_version, "version_status", None)
    return result


def serialize_schedule(obj) -> Dict[str, Any]:
    return _serialize_model(obj, SCHEDULE_FIELDS)


def serialize_notification(obj) -> Dict[str, Any]:
    return _serialize_model(obj, NOTIFICATION_FIELDS)


def serialize_support_group(obj) -> Dict[str, Any]:
    return _serialize_model(obj, SUPPORT_GROUP_FIELDS)


def serialize_project_support_group(obj) -> Dict[str, Any]:
    return _serialize_model(obj, PROJECT_SUPPORT_GROUP_FIELDS)
