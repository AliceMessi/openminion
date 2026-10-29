from openminion.modules.storage.migrations.alembic import (
    apply_ddl_statements,
    drop_sql_objects,
)


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

DDL = (
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        timestamp REAL NOT NULL,
        data TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS om_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_session ON events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_turn ON events(session_id, turn_id)",
)

POSTGRES_DDL = (
    """
    CREATE TABLE IF NOT EXISTS events (
        id SERIAL PRIMARY KEY,
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        timestamp DOUBLE PRECISION NOT NULL,
        data TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS om_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_session ON events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_turn ON events(session_id, turn_id)",
)


def upgrade() -> None:
    from alembic import op

    bind = op.get_bind()
    apply_ddl_statements(POSTGRES_DDL if bind.dialect.name == "postgresql" else DDL)


def downgrade() -> None:
    drop_sql_objects(
        table_names=("events", "om_meta"),
        index_names=("idx_session", "idx_turn"),
    )
