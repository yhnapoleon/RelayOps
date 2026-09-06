"""
Models package — pure entity definitions.

All SQLAlchemy ORM models are defined here. Business logic lives in services/.
For backward compatibility, all entities can be imported from core.models.entities.
"""

# Re-export all for backward compatibility
from core.models.constants import *  # noqa: F401, F403
from core.models.user import User, UserRole
from core.models.project_entities import Project, ProjectMember, ProjectSupportGroup, ProjectVersion
from core.models.product_entities import Product, ProductVersion
from core.models.job_entities import Job, JobFailureScenario, JobExecution
from core.models.app_entities import (
    Application,
    ApplicationApiCheck,
    ApplicationApiCheckRun,
    ApplicationHealthCheck,
    ApplicationRecoveryScenario,
    VerificationReport,
)
from core.models.issue_entities import Issue, UserIssuePreference
from core.models.agent_entities import (
    AgentConversation,
    AgentMessage,
    AgentRun,
    OnboardingDraft,
)
from core.models.template_entities import ScenarioTemplate
from core.models.system_entities import Schedule, Notification, AuditLog, ApiKey, SupportGroup
from core.models.database import Base, Database, get_db
