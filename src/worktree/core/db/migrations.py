"""Database migration execution and database initialization logic."""

from pathlib import Path

from alembic import command
from alembic.config import Config

from worktree.core.db.connection import (
    DEFAULT_DB_REL_PATH,
    resolve_db_path,
)


def init_database(
    cwd: Path | None = None,
    db_rel_path: str = DEFAULT_DB_REL_PATH,
    db_path: Path | None = None,
) -> Path:
    """Run table migrations and initialize local SQLite database layout."""
    target_path = db_path if db_path is not None else resolve_db_path(cwd, db_rel_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    alembic_cfg = Config()
    alembic_dir = Path(__file__).parent / "alembic"
    alembic_cfg.set_main_option("script_location", str(alembic_dir))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{target_path}")

    command.upgrade(alembic_cfg, "head")

    return target_path
