"""Unified database container for Worktree CLI."""

from pathlib import Path

from sqlalchemy import Engine

from worktree.core.db.connection import (
    DEFAULT_DB_REL_PATH,
    get_engine,
    resolve_db_path,
)
from worktree.core.db.migrations import init_database
from worktree.core.db.repositories.catalog import CatalogRepository
from worktree.core.db.repositories.costs import CostsRepository
from worktree.core.db.repositories.runs import RunsRepository
from worktree.core.db.repositories.sandboxes import SandboxesRepository


class WorktreeDb:
    """Unified entry point providing access to all domain DB repositories under a single configuration."""

    def __init__(
        self,
        cwd: Path | None = None,
        db_rel_path: str = DEFAULT_DB_REL_PATH,
        db_engine: Engine | None = None,
    ) -> None:
        self.cwd = cwd
        self.db_rel_path = db_rel_path
        self.db_engine = db_engine if db_engine is not None else get_engine(resolve_db_path(cwd, db_rel_path))
        self.sandboxes = SandboxesRepository(cwd, db_rel_path=db_rel_path, auto_init=False, db_engine=self.db_engine)
        self.runs = RunsRepository(cwd, db_rel_path=db_rel_path, auto_init=False, db_engine=self.db_engine)
        self.catalog = CatalogRepository(cwd, db_rel_path=db_rel_path, auto_init=False, db_engine=self.db_engine)
        self.costs = CostsRepository(cwd, db_rel_path=db_rel_path, auto_init=False, db_engine=self.db_engine)

    @property
    def engine(self) -> Engine:
        """Alias to db_engine for compatibility."""
        return self.db_engine

    def init_db(self) -> Path:
        """Run migrations and mark all child repositories as initialized."""
        path = init_database(self.cwd, self.db_rel_path)
        self.sandboxes._initialized = True
        self.runs._initialized = True
        self.catalog._initialized = True
        self.costs._initialized = True
        return path
