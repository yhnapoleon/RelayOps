from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from core.config import get_config
from core.logging import get_logger

logger = get_logger(__name__)

_db: "Database | None" = None


class Base(DeclarativeBase):
    """SQLAlchemy declarative base class for all database models."""


def _get_column_type_sql(column):
    """Convert SQLAlchemy column type to PostgreSQL type string."""
    col_type = type(column.type)
    if col_type == Integer or col_type.__name__ == "Integer":
        return "INTEGER"
    elif col_type == String or col_type.__name__ == "String":
        length = getattr(column.type, "length", None)
        return f"VARCHAR({length})" if length else "VARCHAR(255)"
    elif col_type == DateTime or col_type.__name__ == "DateTime":
        return "TIMESTAMP"
    elif col_type == Boolean or col_type.__name__ == "Boolean":
        return "BOOLEAN"
    elif col_type == Float or col_type.__name__ == "Float":
        return "DOUBLE PRECISION"
    elif col_type == JSON or col_type.__name__ == "JSON":
        return "JSON"
    elif col_type == Text or col_type.__name__ == "Text":
        return "TEXT"
    else:
        return "TEXT"


def _get_column_default_sql(column):
    """Get SQL default clause for a column."""
    if column.default is not None:
        default_val = column.default.arg
        if callable(default_val):
            return None
        if isinstance(default_val, bool):
            return "TRUE" if default_val else "FALSE"
        if isinstance(default_val, (int, float)):
            return str(default_val)
        if isinstance(default_val, str):
            return f"'{default_val}'"
    return None


class Database:
    """
    Database connection manager for PostgreSQL.

    Handles connection setup, schema management, and session creation.
    Configuration is loaded from config.yaml.

    Attributes:
        engine: SQLAlchemy engine instance.
        schema: Optional schema name for table isolation.
        SessionLocal: Session factory for creating database sessions.
    """

    def __init__(self):
        """
        Initialize PostgreSQL database connection with retries.

        URL composition is delegated to ``config.database_url`` so that the
        CML path (runtime credential lookup) and the docker-compose path
        (host/port/credential fields) share a single entry point here.
        """
        import time
        from sqlalchemy.exc import OperationalError

        config = get_config()

        db_url = config.database_url
        self.schema = config.database_schema

        # For logging only — strip credentials so we don't leak them on retry.
        try:
            safe_target = db_url.split("@", 1)[1]
        except IndexError:
            safe_target = "<unknown>"

        # Retry logic for database availability (crucial for Docker environments)
        max_retries = 5
        retry_interval = 5
        last_exception = None

        for attempt in range(1, max_retries + 1):
            try:
                logger.info("Connecting to database at {} (attempt {}/{})", safe_target, attempt, max_retries)
                self.engine = create_engine(
                    db_url,
                    pool_size=10,
                    max_overflow=20,
                    pool_pre_ping=True,
                    pool_recycle=300,
                    pool_timeout=10,
                    # YugabyteDB raises a 40001 SerializationFailure ("Restart
                    # read required") when a snapshot/repeatable-read read needs
                    # a read-restart but result data was already streamed, so
                    # the YSQL layer can't retry it transparently. Under READ
                    # COMMITTED, Yugabyte instead restarts the read internally
                    # *before* sending data, which eliminates the error for the
                    # normal (small-result) OLTP queries this app issues. This
                    # is the documented Yugabyte mitigation and matches plain
                    # PostgreSQL's default isolation, so behaviour is unchanged
                    # on a vanilla-PG dev DB.
                    isolation_level="READ COMMITTED",
                )

                # Apply a 30s statement timeout to every pooled connection so a
                # stuck query can never hold a worker thread / DB connection
                # indefinitely (without this a contended write — e.g. the
                # users-row UPDATE on login — blocks forever, presenting as "no
                # response, nothing in the log"). Registered before the first
                # checkout so the SELECT 1 ping and every later connection — incl.
                # those opened after the schema-setup dispose() below — inherit it.
                self._set_statement_timeout()

                # Test the connection
                with self.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))

                # Schema setup
                if self.schema:
                    self._ensure_schema_exists()
                    self._set_search_path()
                    # The connect-event listener registered by _set_search_path
                    # only fires for connections opened AFTER it was attached.
                    # The connection used by _ensure_schema_exists (and the
                    # earlier SELECT 1 ping) is already in the pool without
                    # search_path set — dispose it so the next checkout opens
                    # a fresh connection that goes through the listener.
                    # Vanilla PostgreSQL tolerated the missing search_path by
                    # falling back to "public", but YugabyteDB rejects
                    # unqualified CREATE TABLE with "no schema has been
                    # selected to create in".
                    self.engine.dispose()

                self.SessionLocal = sessionmaker(bind=self.engine)
                logger.success("Database connection established and schema initialized")
                return
            except (OperationalError, Exception) as e:
                last_exception = e
                if attempt < max_retries:
                    logger.warning("Database not ready yet, retrying in {}s... ({})", retry_interval, str(e))
                    time.sleep(retry_interval)
                else:
                    logger.error("Failed to connect to database after {} attempts", max_retries)
                    raise last_exception

    def _set_statement_timeout(self):
        """Set a 30-second statement timeout on every new connection."""

        @event.listens_for(self.engine, "connect")
        def set_timeout(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("SET statement_timeout = '30s'")
            cursor.close()

    def _ensure_schema_exists(self):
        """Create the schema if it doesn't exist.

        NOTE: self.schema comes from config.yaml (operator-controlled), not user input.
        """
        logger.debug("Ensuring schema '{}' exists", self.schema)
        quoted_schema = self.schema.replace('"', "")
        with self.engine.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{quoted_schema}"'))
            conn.commit()

    def _set_search_path(self):
        """Set search_path on every new connection so tables use the configured schema.

        IMPORTANT: ``SET search_path`` is transactional in PostgreSQL — if it
        runs inside an uncommitted transaction and that transaction is later
        rolled back, the change is undone. psycopg2's default autocommit=False
        means ``cursor.execute("SET ...")`` opens an implicit transaction, so
        we MUST commit before returning, otherwise SQLAlchemy's default
        ``pool_reset_on_return="rollback"`` will silently undo the SET the
        next time the connection is returned to the pool. That bug presented
        as: first query worked (still in the SET's transaction), every
        subsequent query failed with ``relation "X" does not exist`` because
        search_path had reverted to the user's default.
        """
        quoted_schema = self.schema.replace('"', "")

        @event.listens_for(self.engine, "connect")
        def set_search_path(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute(f'SET search_path TO "{quoted_schema}", public')
            cursor.close()
            dbapi_conn.commit()

    def create_tables(self):
        """Create all database tables defined in SQLAlchemy models."""
        from core.models import constants  # noqa: F401
        from core.models import user  # noqa: F401
        from core.models import project_entities  # noqa: F401
        from core.models import product_entities  # noqa: F401
        from core.models import job_entities  # noqa: F401
        from core.models import app_entities  # noqa: F401
        from core.models import issue_entities  # noqa: F401
        from core.models import system_entities  # noqa: F401
        from core.models import mmp_entities  # noqa: F401
        from core.models import template_entities  # noqa: F401

        logger.debug("Creating database tables in schema '{}'", self.schema)
        if self.schema:
            Base.metadata.schema = self.schema
        Base.metadata.create_all(self.engine)

    def sync_schema(self):
        """
        Synchronize database schema with SQLAlchemy models.

        Compares existing table columns with model definitions and adds
        any missing columns. Checks BOTH the configured schema and the
        public schema to handle legacy tables that may have been created
        before newer columns were added to the models.

        Only adds columns — does not modify or remove existing ones.
        """
        from core.models import constants  # noqa: F401
        from core.models import user  # noqa: F401
        from core.models import project_entities  # noqa: F401
        from core.models import product_entities  # noqa: F401
        from core.models import job_entities  # noqa: F401
        from core.models import app_entities  # noqa: F401
        from core.models import issue_entities  # noqa: F401
        from core.models import system_entities  # noqa: F401
        from core.models import mmp_entities  # noqa: F401

        self._run_custom_schema_migrations()
        inspector = inspect(self.engine)

        # Check both the configured schema AND public for tables needing migration.
        # This handles the case where old tables exist in 'public' without newer columns,
        # which would cause queries to fail since search_path includes public as fallback.
        schemas_to_check = []
        if self.schema:
            schemas_to_check.append(self.schema)
        schemas_to_check.append("public")

        for check_schema in schemas_to_check:
            try:
                existing_tables = inspector.get_table_names(schema=check_schema)
            except Exception:
                logger.warning("Could not inspect schema '{}', skipping", check_schema)
                continue

            for _table_key, table in Base.metadata.tables.items():
                if table.name not in existing_tables:
                    continue

                existing_columns = {col["name"] for col in inspector.get_columns(table.name, schema=check_schema)}
                model_columns = {col.name: col for col in table.columns}

                missing_columns = set(model_columns.keys()) - existing_columns

                if missing_columns:
                    logger.info("Table '{}' in schema '{}' is missing columns: {}", table.name, check_schema, missing_columns)

                    with self.engine.connect() as conn:
                        for col_name in missing_columns:
                            column = model_columns[col_name]
                            col_type = _get_column_type_sql(column)
                            nullable = column.nullable if column.nullable is not None else True
                            default = _get_column_default_sql(column)

                            safe_schema = check_schema.replace('"', "")
                            safe_table = table.name.replace('"', "")
                            safe_col = col_name.replace('"', "")
                            qualified_table = f'"{safe_schema}"."{safe_table}"'
                            sql = f'ALTER TABLE {qualified_table} ADD COLUMN "{safe_col}" {col_type}'

                            if default is not None:
                                sql += f" DEFAULT {default}"

                            if not nullable:
                                if default is not None:
                                    sql += " NOT NULL"
                                else:
                                    sql += " NULL"

                            logger.info("Adding column to {}: {}", qualified_table, sql)
                            conn.execute(text(sql))

                        conn.commit()
                        logger.info("Added {} column(s) to '{}' in schema '{}'",
                                    len(missing_columns), table.name, check_schema)

        # Create any completely missing tables in the configured schema
        if self.schema:
            Base.metadata.schema = self.schema
        Base.metadata.create_all(self.engine)
        logger.info("Schema synchronization complete")

    def _run_custom_schema_migrations(self):
        """Run one-off schema migrations that require type changes."""
        schemas_to_check = []
        if self.schema:
            schemas_to_check.append(self.schema)
        schemas_to_check.append("public")

        with self.engine.connect() as conn:
            for check_schema in schemas_to_check:
                safe_schema = check_schema.replace('"', "")
                product_versions_exists = conn.execute(
                    text(
                        """
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = :schema
                          AND table_name = 'product_versions'
                          AND column_name = 'version_number'
                        """
                    ),
                    {"schema": safe_schema},
                ).first()
                if product_versions_exists is not None:
                    data_type = conn.execute(
                        text(
                            """
                            SELECT data_type
                            FROM information_schema.columns
                            WHERE table_schema = :schema
                              AND table_name = 'product_versions'
                              AND column_name = 'version_number'
                            """
                        ),
                        {"schema": safe_schema},
                    ).scalar()
                    if data_type in {"integer", "bigint", "smallint", "numeric"}:
                        conn.execute(text(
                            f'ALTER TABLE "{safe_schema}"."product_versions" '
                            'ALTER COLUMN "version_number" TYPE VARCHAR(50) USING "version_number"::text'
                        ))

                # Drop products.prod_stat_url — was a display-only field, no
                # longer in the model. Safe to drop: no checker / probe ever
                # read it, only audit / serializer / version snapshots.
                prod_stat_url_exists = conn.execute(
                    text(
                        """
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = :schema
                          AND table_name = 'products'
                          AND column_name = 'prod_stat_url'
                        """
                    ),
                    {"schema": safe_schema},
                ).first()
                if prod_stat_url_exists is not None:
                    conn.execute(text(
                        f'ALTER TABLE "{safe_schema}"."products" DROP COLUMN "prod_stat_url"'
                    ))

                # Drop the per-asset prod_stat_url columns — the field moved up
                # to the project (a project usually maps to one repo, so it
                # carries a single prod-stat URL). Display-only, never probed,
                # so dropping is safe. Old per-asset values are not migrated.
                for asset_table in ("jobs", "applications"):
                    asset_prod_stat_exists = conn.execute(
                        text(
                            """
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = :schema
                              AND table_name = :table
                              AND column_name = 'prod_stat_url'
                            """
                        ),
                        {"schema": safe_schema, "table": asset_table},
                    ).first()
                    if asset_prod_stat_exists is not None:
                        conn.execute(text(
                            f'ALTER TABLE "{safe_schema}"."{asset_table}" DROP COLUMN "prod_stat_url"'
                        ))

                # Merge applications.application_type into cml_app_type, then
                # drop the column. Mapping: ray→ray, fastapi→fastapi,
                # runtime_cluster→runtime, other→leave cml_app_type alone (other
                # was the default; user intent is captured in cml_app_type).
                application_type_exists = conn.execute(
                    text(
                        """
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = :schema
                          AND table_name = 'applications'
                          AND column_name = 'application_type'
                        """
                    ),
                    {"schema": safe_schema},
                ).first()
                if application_type_exists is not None:
                    conn.execute(text(
                        f'''
                        UPDATE "{safe_schema}"."applications"
                        SET cml_app_type = CASE application_type
                            WHEN 'ray' THEN 'ray'
                            WHEN 'fastapi' THEN 'fastapi'
                            WHEN 'runtime_cluster' THEN 'runtime'
                            ELSE cml_app_type
                        END
                        WHERE application_type IS NOT NULL
                          AND application_type != 'other'
                        '''
                    ))
                    conn.execute(text(
                        f'ALTER TABLE "{safe_schema}"."applications" DROP COLUMN "application_type"'
                    ))

                products_exists = conn.execute(
                    text(
                        """
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = :schema
                          AND table_name = 'products'
                          AND column_name = 'latest_version_number'
                        """
                    ),
                    {"schema": safe_schema},
                ).first()
                if products_exists is not None:
                    data_type = conn.execute(
                        text(
                            """
                            SELECT data_type
                            FROM information_schema.columns
                            WHERE table_schema = :schema
                              AND table_name = 'products'
                              AND column_name = 'latest_version_number'
                            """
                        ),
                        {"schema": safe_schema},
                    ).scalar()
                    if data_type in {"integer", "bigint", "smallint", "numeric"}:
                        conn.execute(text(
                            f'ALTER TABLE "{safe_schema}"."products" '
                            'ALTER COLUMN "latest_version_number" DROP DEFAULT'
                        ))
                        conn.execute(text(
                            f'ALTER TABLE "{safe_schema}"."products" '
                            'ALTER COLUMN "latest_version_number" TYPE VARCHAR(50) '
                            'USING CASE WHEN "latest_version_number" = 0 THEN NULL ELSE "latest_version_number"::text END'
                        ))
            conn.commit()

    def get_session(self):
        """
        Create and return a new database session.

        Returns:
            A SQLAlchemy Session instance bound to the database engine.
        """
        return self.SessionLocal()


def get_db() -> Database:
    """
    Get the application-wide Database singleton.

    The shared instance owns engine/session initialization and schema sync.
    Entity-specific database modules should consume this helper rather than
    owning their own global Database lifecycle.
    """
    global _db
    if _db is None:
        _db = Database()
        _db.sync_schema()
    return _db
