"""
Load application configuration from a YAML file.

Config file path: set CONFIG_FILE env var, or default to config.yaml in current
working directory. If the file is missing, defaults are used so the app can run
without a config file.
"""

import os
from pathlib import Path
from typing import Any, List, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

def _config_path() -> Path:
    """Use an explicit config, a local config, or the shipped demo defaults."""
    requested = os.getenv("CONFIG_FILE")
    if requested:
        path = Path(requested).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"CONFIG_FILE does not exist: {path}")
        return path
    root = Path(__file__).resolve().parents[2]
    for path in (root / "config.yaml", Path.cwd() / "config.yaml"):
        if path.is_file():
            return path
    return root / "config.sample.yaml"






def _load_yaml(path: Path) -> dict:
    """
    Load and parse a YAML configuration file.

    Args:
        path: Path to the YAML file.

    Returns:
        Parsed configuration as a dictionary, or empty dict if file
        doesn't exist or contains non-dict data.

    Raises:
        RuntimeError: If PyYAML is not installed.
    """
    if not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required for config file support. Install with: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _get(raw: dict, key: str, default: Any = None) -> Any:
    """Get nested key like 'database.url' from a dict."""
    keys = key.split(".")
    v = raw
    for k in keys:
        if isinstance(v, dict) and k in v:
            v = v[k]
        else:
            return default
    return v


class Config:
    """
    Application configuration loaded from YAML config file.

    Provides typed access to all configuration settings with sensible defaults.
    Settings are organized by category: app, database, jwt, ldap, cors, admin,
    server, and logging.

    Attributes:
        _raw: Raw configuration dictionary loaded from YAML.
        _path: Path to the configuration file.
    """

    _raw: dict
    _path: Path

    def __init__(self) -> None:
        """Initialize configuration by loading from the config file path."""
        self._path = _config_path()
        self._raw = _load_yaml(self._path)

    # --- App ---
    @property
    def app_name(self) -> str:
        """Return the application name."""
        return _get(self._raw, "app.name") or "RelayOps"

    @property
    def splash_text(self) -> str:
        """Return the splash screen text."""
        return _get(self._raw, "app.splash_text") or "Welcome to RelayOps"

    @property
    def footnote(self) -> Optional[str]:
        """Footnote to show in the frontend."""
        return _get(self._raw, "app.footnote")

    @property
    def logo(self) -> Optional[str]:
        """Path to a logo image file."""
        return _get(self._raw, "app.logo")

    @property
    def app_base_url(self) -> str:
        """Public base URL of this RelayOps deployment, e.g.
        ``https://relayopscenter.<workspace>.apps.<...>.example.com``.

        The server can't infer its own external host, so absolute deep links
        in outbound email (e.g. "open this product") need it configured. Empty
        string (the default) disables the link. Any trailing slash is trimmed
        so callers can append ``/?...`` safely."""
        value = _get(self._raw, "app.base_url")
        if not value:
            return ""
        return str(value).strip().rstrip("/")

    # --- Environment ---
    @property
    def app_env(self):
        return os.getenv("APP_ENV", "development").lower()

    @property
    def is_production(self):
        return self.app_env in ("prod", "production")

    # --- Database ---
    def _db_env(self) -> str:
        """Return the active database environment based on APP_ENV."""
        return self.app_env


    @property
    def database_url(self):
        override = os.getenv("RELAYOPS_DATABASE_URL")
        if override:
            return override
        from sqlalchemy.engine import URL
        return URL.create("postgresql", username=self.database_user,
                          password=self.database_credential, host=self.database_host,
                          port=self.database_port, database=self.database_name).render_as_string(hide_password=False)

    @property
    def database_host(self) -> str:
        """Return the database host (local docker-compose only)."""
        return _get(self._raw, f"database.{self._db_env()}.host") or "localhost"

    @property
    def database_port(self) -> int:
        """Return the database port (local docker-compose only)."""
        return int(_get(self._raw, f"database.{self._db_env()}.port") or 5433)

    @property
    def database_name(self) -> str:
        """Return the database name (local docker-compose only)."""
        return _get(self._raw, f"database.{self._db_env()}.dbname") or "yugabyte"

    @property
    def database_user(self) -> str:
        """Return the database user (local docker-compose only)."""
        return _get(self._raw, f"database.{self._db_env()}.user") or "yugabyte"

    @property
    def database_credential(self) -> str:
        """Return the database credential (local docker-compose only)."""
        return _get(self._raw, f"database.{self._db_env()}.credential") or "yugabyte"

    @property
    def database_schema(self):
        return _get(self._raw, f"database.{self._db_env()}.schema")

    # --- JWT ---
    @property
    def jwt_secret_key(self):
        key = os.getenv("RELAYOPS_JWT_SECRET") or _get(self._raw, "jwt.secret_key")
        if key:
            return str(key)
        if self.is_production:
            raise RuntimeError("Set RELAYOPS_JWT_SECRET before running in production")
        return "relayops-public-demo-signing-key-not-for-production"

    @property
    def jwt_expire_minutes(self) -> int:
        """Return the JWT expiration time in minutes."""
        return int(_get(self._raw, "jwt.expire_minutes") or 60)

    # --- LDAP ---
    @property
    def ldap_server(self) -> str:
        """Return the LDAP server URL."""
        return os.getenv("RELAYOPS_LDAP_SERVER") or _get(self._raw, "ldap.server") or "ldap://localhost:1389"

    @property
    def ldap_base_dn(self) -> str:
        """Return the LDAP base DN."""
        return _get(self._raw, "ldap.base_dn") or "dc=example,dc=com"

    @property
    def ldap_use_ssl(self) -> bool:
        """Return whether LDAP should use SSL."""
        v = _get(self._raw, "ldap.use_ssl")
        if v is None:
            return False
        return str(v).lower() in ("true", "1", "yes")

    @property
    def ldap_users_dn(self) -> Optional[str]:
        """Return the LDAP users DN."""
        return _get(self._raw, "ldap.users_dn")

    # --- CORS ---
    @property
    def cors_allow_origins(self) -> List[str]:
        """Return the list of allowed CORS origins."""
        origins = _get(self._raw, "cors.allow_origins")
        if origins is None:
            return ["http://localhost:5173", "http://localhost:3000"]
        if isinstance(origins, list):
            return [str(o) for o in origins]
        return [str(origins)]

    # --- Admin ---
    @property
    def admin_username(self) -> Optional[str]:
        """Return the admin username."""
        return _get(self._raw, "admin.username")

    @property
    def admin_password(self) -> Optional[str]:
        """Return the admin password."""
        return _get(self._raw, "admin.password")

    @property
    def platform_owners(self) -> list:
        """Return the list of platform owners."""
        owners = _get(self._raw, "platform_owners")
        if owners is None:
            return []
        if isinstance(owners, list):
            return [str(o) for o in owners]
        return [str(owners)]

    # --- Mock Services ---
    @property
    def mock_services_url(self) -> str:
        """Base URL for mock services used by scheduler checks."""
        return os.getenv("RELAYOPS_MOCK_SERVICES_URL") or _get(self._raw, "mock_services.url") or "http://localhost:9000"

    # --- Server ---
    @property
    def server_workers(self) -> int:
        """Number of uvicorn worker processes. Defaults to 1."""
        return int(_get(self._raw, "server.workers") or 1)

    @property
    def server_reload(self) -> bool:
        """Enable hot-reload (dev only, forces single worker)."""
        v = _get(self._raw, "server.reload")
        if v is None:
            return False
        return str(v).lower() in ("true", "1", "yes")

    @property
    def server_timeout(self) -> int:
        """Worker heartbeat timeout in seconds."""
        return int(_get(self._raw, "server.timeout") or 120)

    @property
    def server_graceful_timeout(self) -> int:
        """Seconds to finish in-flight requests after SIGTERM before force-kill."""
        return int(_get(self._raw, "server.graceful_timeout") or 30)

    @property
    def server_keep_alive(self) -> int:
        """Seconds to keep idle HTTP connections open."""
        return int(_get(self._raw, "server.keep_alive") or 5)

    @property
    def server_max_requests(self) -> int:
        """Restart worker after N requests (0 = never). Prevents memory leaks."""
        return int(_get(self._raw, "server.max_requests") or 0)

    @property
    def server_max_requests_jitter(self) -> int:
        """Random jitter added to max_requests to avoid thundering herd restarts."""
        return int(_get(self._raw, "server.max_requests_jitter") or 0)

    @property
    def server_backlog(self) -> int:
        """TCP connection backlog queue size."""
        return int(_get(self._raw, "server.backlog") or 2048)

    @property
    def server_access_log(self) -> bool:
        """Enable HTTP access logging."""
        v = _get(self._raw, "server.access_log")
        if v is None:
            return False
        return str(v).lower() in ("true", "1", "yes")

    # --- Analytics ---
    @property
    def analytics_anomaly_min_runs(self) -> int:
        """Minimum job runs required before applying high-failure anomaly rule."""
        return int(_get(self._raw, "analytics.anomaly.min_runs") or 20)

    @property
    def analytics_anomaly_min_failure_rate_percent(self) -> int:
        """Minimum failure rate percentage required for high-failure anomaly rule."""
        return int(_get(self._raw, "analytics.anomaly.min_failure_rate_percent") or 40)

    @property
    def analytics_anomaly_min_repeat_failure_streak(self) -> int:
        """Minimum consecutive failures required for repeat-failure anomaly rule."""
        return int(_get(self._raw, "analytics.anomaly.min_repeat_failure_streak") or 5)

    @property
    def analytics_anomaly_min_open_issues(self) -> int:
        """Minimum open issues required for open-issues anomaly rule."""
        return int(_get(self._raw, "analytics.anomaly.min_open_issues") or 3)

    @property
    def analytics_anomaly_recent_failure_hours(self) -> int:
        """Recent-failure lookback window in hours for anomaly rule C."""
        return int(_get(self._raw, "analytics.anomaly.recent_failure_hours") or 24)

    # --- CML Platform ---
    @property
    def cml_platform_base_url(self) -> str:
        """Base URL for the CML Platform API.

        Resolution order:
          1. env RELAYOPS_CML_BASE_URL — explicit override
          2. config.yaml cml_platform.base_url
          3. env CDSW_API_URL — auto-injected by CDSW; we substitute /v1 -> /v2
          4. fallback http://localhost:9000 (mock)
        """
        # NB: control_interface._request prepends "/api/v2/..." itself, so
        # base_url must be JUST the host root (no /api/vN suffix). Otherwise
        # requests end up at .../api/api/v2/... and 404 everywhere.
        #
        # Strip *repeatedly* so we tolerate inputs like ".../api/api/v2"
        # (mis-edited config) or ".../api/v1/api/v2" (some CDSW versions
        # expose CDSW_API_URL like that). Stop as soon as a pass makes no
        # progress.
        def _strip_api_suffix(u: str) -> str:
            u = u.strip().rstrip("/")
            while True:
                for suffix in ("/api/v1", "/api/v2"):
                    if u.endswith(suffix):
                        u = u[: -len(suffix)].rstrip("/")
                        break
                else:
                    return u

        env_override = os.getenv("RELAYOPS_CML_BASE_URL")
        if env_override:
            return _strip_api_suffix(env_override)
        value = _get(self._raw, "cml_platform.base_url")
        if value:
            return _strip_api_suffix(str(value))
        import logging
        logging.getLogger(__name__).warning(
            "cml_platform.base_url not configured, using default: http://localhost:9000"
        )
        return "http://localhost:9000"

    @property
    def cml_platform_api_key(self) -> str:
        """Bearer token sent on every CML v2 request as ``Authorization: Bearer ...``.

        Resolution order:
          1. env RELAYOPS_CML_API_KEY — explicit override
          2. config.yaml cml_platform.api_key (or legacy auth_token)
          3. env CDSW_APIV2_KEY — auto-injected by CDSW each Session/Application;
             session-scoped so reading it dynamically is correct.
        """
        env_override = os.getenv("RELAYOPS_CML_API_KEY")
        if env_override:
            return env_override
        value = _get(self._raw, "cml_platform.api_key")
        if value is None:
            value = _get(self._raw, "cml_platform.auth_token")
        if value:
            return str(value)
        import logging
        logging.getLogger(__name__).warning(
            "cml_platform.api_key not configured, using default: empty string"
        )
        return ""

    @property
    def cml_platform_auth_token(self) -> str:
        """DEPRECATED alias for :attr:`cml_platform_api_key`. Retained so existing
        call sites keep working until Phase 2 fully rewires them."""
        return self.cml_platform_api_key

    @property
    def cml_platform_default_project_name(self) -> str:
        """CML project name Ops resolves at startup to populate the default project_id.
        Per-Job / per-Application bindings may override with their own ``cml_project_name``."""
        value = _get(self._raw, "cml_platform.default_project_name")
        if value is None:
            return ""
        return str(value)

    @property
    def cml_platform_verify_ssl(self):
        value = _get(self._raw, "cml_platform.verify_ssl", True)
        return str(value).lower() in ("true", "1", "yes")

    @property
    def cml_platform_ca_bundle_path(self) -> str:
        """RelayOps cml platform ca bundle path."""
        value = _get(self._raw, "cml_platform.ca_bundle_path")
        if value is None:
            return ""
        return str(value)

    @property
    def cml_platform_timeout_seconds(self) -> int:
        """Timeout in seconds for CML Platform API requests. Defaults to 10."""
        value = _get(self._raw, "cml_platform.timeout_seconds")
        if value is None:
            import logging
            logging.getLogger(__name__).warning(
                "cml_platform.timeout_seconds not configured, using default: 10"
            )
            return 10
        return int(value)

    @property
    def cml_platform_check_interval_seconds(self) -> int:
        """Polling interval in seconds for the monitoring Controller (covers all sub-checks). Defaults to 300 (5 minutes)."""
        value = _get(self._raw, "cml_platform.check_interval_seconds")
        if value is None:
            import logging
            logging.getLogger(__name__).warning(
                "cml_platform.check_interval_seconds not configured, using default: 300"
            )
            return 300
        return int(value)

    @property
    def cml_platform_health_failure_threshold(self) -> int:
        """Number of consecutive health check failures before creating an APP_OFFLINE issue. Defaults to 3."""
        value = _get(self._raw, "cml_platform.health_failure_threshold")
        if value is None:
            import logging
            logging.getLogger(__name__).warning(
                "cml_platform.health_failure_threshold not configured, using default: 3"
            )
            return 3
        return int(value)

    @property
    def cml_platform_job_staleness_threshold_minutes(self) -> int:
        """Minutes after which a 'success' job with a stale last_run is flagged as a miss. Defaults to 120."""
        value = _get(self._raw, "cml_platform.job_staleness_threshold_minutes")
        if value is None:
            return 120
        return int(value)

    # --- MMP (Model Management Platform) ---
    @property
    def mmp_base_url(self) -> str:
        """Base URL for the MMP API.

        Resolution order:
          1. env RELAYOPS_MMP_BASE_URL
          2. config.yaml mmp.base_url
          3. empty string (MmpChecker becomes a no-op until configured)
        """
        env_override = os.getenv("RELAYOPS_MMP_BASE_URL")
        if env_override:
            return env_override.rstrip("/")
        value = _get(self._raw, "mmp.base_url")
        if value:
            return str(value).rstrip("/")
        return ""

    @property
    def mmp_bearer_token(self) -> str:
        """Bearer token for MMP API.

        Resolution order:
          1. env RELAYOPS_MMP_BEARER_TOKEN (production)
          2. config.yaml mmp.bearer_token (dev convenience only — do not
             commit a populated value)
        """
        env_override = os.getenv("RELAYOPS_MMP_BEARER_TOKEN")
        if env_override:
            return env_override.strip()
        value = _get(self._raw, "mmp.bearer_token")
        return str(value).strip() if value else ""

    @property
    def mmp_refresh_token(self) -> str:
        """Refresh token for MMP API (sent in the Refresh-token header).

        Same resolution order as :attr:`mmp_bearer_token` — RELAYOPS_MMP_REFRESH_TOKEN
        env var preferred over the YAML fallback.
        """
        env_override = os.getenv("RELAYOPS_MMP_REFRESH_TOKEN")
        if env_override:
            return env_override.strip()
        value = _get(self._raw, "mmp.refresh_token")
        return str(value).strip() if value else ""

    @property
    def mmp_verify_ssl(self):
        value = _get(self._raw, "mmp.verify_ssl", True)
        return str(value).lower() in ("true", "1", "yes")

    @property
    def mmp_ca_bundle_path(self) -> str:
        """RelayOps mmp ca bundle path."""
        value = _get(self._raw, "mmp.ca_bundle_path")
        if value is None:
            return ""
        return str(value)

    @property
    def mmp_timeout_seconds(self) -> int:
        """Timeout for MMP HTTP requests. Defaults to 30 (some endpoints take ~5s)."""
        value = _get(self._raw, "mmp.timeout_seconds")
        if value is None:
            return 30
        return int(value)

    @property
    def mmp_check_interval_seconds(self) -> int:
        """Cadence at which MmpChecker actually runs (in seconds). Independent
        of the global Controller tick; defaults to 900 (15 min) because each
        cycle is ~7.5s and drift is a slow-moving signal."""
        value = _get(self._raw, "mmp.check_interval_seconds")
        if value is None:
            return 900
        return int(value)

    @property
    def mmp_web_base_url(self):
        return str(_get(self._raw, "mmp.web_base_url") or self.mmp_base_url).rstrip("/")

    def mmp_model_web_url(self, project_numeric_id: Optional[int]) -> str:
        """Build the MMP web deep link to a project's models page, e.g.
        ``https://runtime-mmp-web-prod.../project/189/models``. Returns "" when
        the web base is unknown or the project id is missing."""
        base = self.mmp_web_base_url
        if not base or project_numeric_id is None:
            return ""
        return f"{base}/project/{int(project_numeric_id)}/models"

    # --- LLM (AI assistant gateway) ---



    @property
    def llm_endpoint(self):
        return str(os.getenv("RELAYOPS_LLM_ENDPOINT") or _get(self._raw, "llm.endpoint") or "").strip().rstrip("/")

    @property
    def llm_api_format(self):
        return "openai"

    @property
    def llm_structured_output_mode(self) -> str:
        """How the structured-output compatibility layer obtains JSON:
          * ``auto`` (default) — native ``with_structured_output`` for gateways
            that support it; a JSON-prompt + parse fallback for DeepSeek (which
            rejects OpenAI's ``response_format``). Detection is by model/endpoint.
          * ``native`` — always use the client's ``with_structured_output``.
          * ``json_prompt`` — always use the JSON-instruction + parse path
            (useful for any self-hosted model that can't do native).
        See :mod:`core.agent.structured`."""
        value = _get(self._raw, "llm.structured_output_mode")
        mode = str(value).strip().lower() if value else "auto"
        if mode not in ("auto", "native", "json_prompt"):
            return "auto"
        return mode

    @property
    def llm_bearer_token(self):
        """Read only the explicitly named environment variable; never search files."""
        return os.getenv("RELAYOPS_LLM_API_KEY", "").strip()

    @property
    def llm_model(self):
        return str(os.getenv("RELAYOPS_LLM_MODEL") or _get(self._raw, "llm.model") or "").strip()

    @property
    def llm_light_model(self):
        return str(_get(self._raw, "llm.light_model") or self.llm_model)

    @property
    def llm_vision_model(self):
        return str(_get(self._raw, "llm.vision_model") or "")

    @property
    def llm_timeout_seconds(self) -> int:
        """Timeout for a single LLM call. Defaults to 60 — generation is slow."""
        value = _get(self._raw, "llm.timeout_seconds")
        if value is None:
            return 60
        return int(value)

    @property
    def llm_verify_ssl(self):
        value = _get(self._raw, "llm.verify_ssl", True)
        return str(value).lower() in ("true", "1", "yes")

    @property
    def llm_ca_bundle_path(self) -> str:
        """Optional CA bundle (.pem) for the gateway — same field semantics as
        ``cml_platform.ca_bundle_path`` / ``mmp.ca_bundle_path``."""
        value = _get(self._raw, "llm.ca_bundle_path")
        if value is None:
            return ""
        return str(value)

    @property
    def llm_configured(self):
        return bool(self.llm_endpoint and self.llm_bearer_token and self.llm_model)

    # --- Confluence (handover-page fetch for the onboarding agent) ---
    @property
    def confluence_base_url(self) -> str:
        """Base URL of the internal Confluence (e.g. https://confluence.example.com).
        Empty means derive it from the page URL the user pastes."""
        value = _get(self._raw, "confluence.base_url")
        return str(value).strip().rstrip("/") if value else ""

    @property
    def confluence_bearer_token(self) -> str:
        """Service PAT for Confluence REST. Env RELAYOPS_CONFLUENCE_BEARER_TOKEN
        wins; a per-request user token (when supplied) wins over both."""
        env_override = os.getenv("RELAYOPS_CONFLUENCE_BEARER_TOKEN")
        if env_override:
            return env_override.strip()
        value = _get(self._raw, "confluence.bearer_token")
        return str(value).strip() if value else ""

    @property
    def confluence_verify_ssl(self):
        value = _get(self._raw, "confluence.verify_ssl", True)
        return str(value).lower() in ("true", "1", "yes")

    @property
    def confluence_ca_bundle_path(self) -> str:
        """RelayOps confluence ca bundle path."""
        value = _get(self._raw, "confluence.ca_bundle_path")
        return str(value) if value is not None else ""

    @property
    def confluence_timeout_seconds(self) -> int:
        value = _get(self._raw, "confluence.timeout_seconds")
        return int(value) if value is not None else 30

    # --- Email (on-duty issue notifications) ---
    @property
    def email_enabled(self) -> bool:
        """Master switch for outbound issue emails. When False the connector
        is a no-op regardless of backend. Defaults to True so a configured
        backend works without an extra flag; flip to False to silence email
        without removing config."""
        v = _get(self._raw, "email.enabled")
        if v is None:
            return True
        return str(v).lower() in ("true", "1", "yes")

    @property
    def email_backend(self):
        return str(_get(self._raw, "email.backend") or "log").strip().lower()

    @property
    def email_domain(self):
        return str(_get(self._raw, "email.domain") or "example.com")

    @property
    def email_fallback_domain(self):
        return str(_get(self._raw, "email.fallback_domain") or "")

    @property
    def email_from_address(self) -> str:
        """The ``From`` address on outbound mail. Defaults to a no-reply on the
        configured domain."""
        value = _get(self._raw, "email.from_address")
        if value:
            return str(value).strip()
        return f"relayops-noreply@{self.email_domain}"

    @property
    def email_subject_prefix(self) -> str:
        """Prefix prepended to every subject line for easy inbox filtering."""
        value = _get(self._raw, "email.subject_prefix")
        if value is None:
            return "[RelayOps]"
        return str(value)

    @property
    def email_smtp_host(self) -> str:
        """SMTP relay host. Empty string disables the smtp backend (and makes
        ``auto`` skip smtp)."""
        value = _get(self._raw, "email.smtp.host")
        if value is None:
            return ""
        return str(value).strip()

    @property
    def email_smtp_port(self) -> int:
        """SMTP relay port. Defaults to 587 (STARTTLS submission)."""
        value = _get(self._raw, "email.smtp.port")
        if value is None:
            return 587
        return int(value)

    @property
    def email_smtp_username(self) -> str:
        """SMTP auth username. Empty string means unauthenticated relay."""
        env_override = os.getenv("RELAYOPS_SMTP_USERNAME")
        if env_override:
            return env_override
        value = _get(self._raw, "email.smtp.username")
        return str(value).strip() if value else ""

    @property
    def email_smtp_password(self) -> str:
        """SMTP auth password. Prefer the env var so it never lands in config.yaml."""
        env_override = os.getenv("RELAYOPS_SMTP_PASSWORD")
        if env_override:
            return env_override
        value = _get(self._raw, "email.smtp.password")
        return str(value) if value else ""

    @property
    def email_smtp_use_tls(self) -> bool:
        """Whether to issue STARTTLS after connecting. Defaults to True."""
        v = _get(self._raw, "email.smtp.use_tls")
        if v is None:
            return True
        return str(v).lower() in ("true", "1", "yes")

    @property
    def email_smtp_timeout_seconds(self) -> int:
        """Socket timeout for SMTP operations. Defaults to 10."""
        value = _get(self._raw, "email.smtp.timeout_seconds")
        if value is None:
            return 10
        return int(value)

    @property
    def email_send_timeout_seconds(self) -> int:
        """Hard wall-clock cap for a single send, enforced by the connector
        regardless of backend. The runtime/SMTP transports can otherwise block
        indefinitely (runtime's send_email builds smtplib.SMTP with no timeout);
        this guarantees a send never ties up a worker thread or DB connection
        for longer than this. Defaults to 10."""
        value = _get(self._raw, "email.send_timeout_seconds")
        if value is None:
            return 10
        return int(value)

    @property
    def email_circuit_fail_threshold(self) -> int:
        """Consecutive send timeouts before the connector's circuit breaker
        trips and stops feeding the (by then poisoned) send pool. Defaults to 4
        — one full pool's worth of stuck sends."""
        value = _get(self._raw, "email.circuit_fail_threshold")
        if value is None:
            return 4
        return int(value)

    @property
    def email_circuit_cooldown_seconds(self) -> float:
        """How long the send circuit stays open (sends fail fast, no worker used)
        before letting one probe through to test whether the relay recovered.
        Defaults to 60."""
        value = _get(self._raw, "email.circuit_cooldown_seconds")
        if value is None:
            return 60.0
        return float(value)

    # --- Duty morning report (值班晨报) ---
    @property
    def duty_report_enabled(self) -> bool:
        """Master switch for the scheduled daily duty morning report.
        Defaults to True — the report is cheap (a handful of queries) and
        degrades gracefully when email / LLM are unavailable."""
        v = _get(self._raw, "duty_report.enabled")
        if v is None:
            return True
        return str(v).lower() in ("true", "1", "yes")

    @property
    def duty_report_send_time(self) -> str:
        """Local wall-clock time (``HH:MM``) after which the day's report is
        generated. Defaults to ``08:30``. Invalid values fall back to the
        default so a config typo can't silence the report."""
        value = _get(self._raw, "duty_report.send_time")
        if value is None:
            return "08:30"
        return str(value).strip()

    @property
    def duty_report_send_time_parts(self) -> tuple:
        """``(hour, minute)`` parsed from :attr:`duty_report_send_time`,
        falling back to ``(8, 30)`` on any parse error."""
        raw = self.duty_report_send_time
        try:
            hour_s, minute_s = raw.split(":", 1)
            hour, minute = int(hour_s), int(minute_s)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (hour, minute)
        except (ValueError, AttributeError):
            pass
        return (8, 30)

    @property
    def duty_report_tz_offset_minutes(self) -> int:
        """UTC offset (minutes) defining the report's "local" day and send
        time. Falls back to env ``RELAYOPS_CRON_TZ_OFFSET_MINUTES`` and then 480
        (UTC+8 / SGT) — the same convention the cron staleness checker uses."""
        value = _get(self._raw, "duty_report.tz_offset_minutes")
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
        raw = os.getenv("RELAYOPS_CRON_TZ_OFFSET_MINUTES")
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        return 480

    @property
    def duty_report_lookback_hours(self) -> int:
        """Window for the "past N hours" sections (anomalies/recoveries and
        audit activity). Defaults to 24."""
        value = _get(self._raw, "duty_report.lookback_hours")
        if value is None:
            return 24
        return int(value)

    @property
    def duty_report_max_items(self) -> int:
        """Per-section cap on listed rows (exact totals are kept separately).
        Bounds both the stored JSON and the email size. Defaults to 50."""
        value = _get(self._raw, "duty_report.max_items")
        if value is None:
            return 50
        return int(value)

    @property
    def duty_report_llm_summary_enabled(self) -> bool:
        """Whether to ask the LLM for the executive summary on top of the
        deterministic sections. Effective only when :attr:`llm_configured`;
        the report itself never depends on the LLM succeeding."""
        v = _get(self._raw, "duty_report.llm_summary.enabled")
        if v is None:
            return True
        return str(v).lower() in ("true", "1", "yes")

    @property
    def duty_report_email_enabled(self) -> bool:
        """Whether the generated report is emailed to on-duty members
        (AND-ed with the global :attr:`email_enabled`). Defaults to True."""
        v = _get(self._raw, "duty_report.email.enabled")
        if v is None:
            return True
        return str(v).lower() in ("true", "1", "yes")

    # --- Logging ---
    @property
    def log_level(self) -> str:
        """Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL. Defaults to INFO."""
        return _get(self._raw, "logging.level") or "INFO"

    @property
    def log_format(self) -> str:
        """Log format string."""
        return _get(self._raw, "logging.format") or "{time:YYYY-MM-DD HH:mm:ss} - {extra[name]} - {level} - {message}"


_config: Optional[Config] = None


def get_config() -> Config:
    """
    Get the singleton Config instance.

    Creates the instance on first call and returns the same instance
    on subsequent calls.

    Returns:
        The application Config singleton.
    """
    global _config
    if _config is None:
        _config = Config()
    return _config
