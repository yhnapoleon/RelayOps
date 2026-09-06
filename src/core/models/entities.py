"""BACKWARD COMPAT: Import all entities from their domain-specific modules."""
from core.models.constants import *  # noqa: F401, F403
from core.models.project_entities import *  # noqa: F401, F403
from core.models.product_entities import *  # noqa: F401, F403
from core.models.job_entities import *  # noqa: F401, F403
from core.models.app_entities import *  # noqa: F401, F403
from core.models.issue_entities import *  # noqa: F401, F403
from core.models.template_entities import *  # noqa: F401, F403
from core.models.system_entities import *  # noqa: F401, F403
