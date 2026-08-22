"""Tests for SQLite database tables, BaseRepository, repository classes, and WorktreeDb facade."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import FileSystem
from worktree.core.db import (
    BaseRepository,
    BlueprintKind,
    CatalogItemType,
    CatalogRecord,
    CatalogRepository,
    RunRecord,
    RunsRepository,
    SandboxesRepository,
    SandboxRecord,
    WorktreeDb,
    get_db_connection,
    init_database,
)
from worktree.core.db.models import SandboxStatus

DB_REL = ".worktree/data.db"


class TestDatabaseMigrations:
    """Tests for database initialization and schema creation."""

    def test_init_creates_database_file(self, fs: FileSystem) -> None:
        db_path = init_database(cwd=fs.base_path, db_rel_path=DB_REL)
        assert db_path.is_file()
        assert RunsRepository(cwd=fs.base_path, db_rel_path=DB_REL).list() == []

    def test_init_is_idempotent(self, fs: FileSystem) -> None:
        path1 = init_database(cwd=fs.base_path, db_rel_path=DB_REL)
        path2 = init_database(cwd=fs.base_path, db_rel_path=DB_REL)
        assert path1 == path2
        assert path1.is_file()

    def test_get_db_connection_lifecycle(self, fs: FileSystem) -> None:
        db_path = init_database(cwd=fs.base_path, db_rel_path=DB_REL)
        with get_db_connection(db_path) as conn:
            cursor = conn.execute("SELECT 1 AS num")
            row = cursor.fetchone()
            assert row is not None
            assert row["num"] == 1

    def test_get_db_connection_rollback_on_error(self, fs: FileSystem) -> None:
        db_path = init_database(cwd=fs.base_path, db_rel_path=DB_REL)
        with pytest.raises(RuntimeError, match="simulated db error"):
            with get_db_connection(db_path) as conn:
                conn.execute("SELECT 1")
                raise RuntimeError("simulated db error")


class TestBaseRepository:
    """Tests for BaseRepository core path resolution, init_db, and session lifecycle."""

    def test_db_path_resolution(self, fs: FileSystem) -> None:
        repo = BaseRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        assert repo.db_path == fs.base_path / DB_REL

        custom_path = fs.base_path / "custom.db"
        repo_custom = BaseRepository(db_path=custom_path)
        assert repo_custom.db_path == custom_path

    def test_init_db_creates_file(self, fs: FileSystem) -> None:
        repo = BaseRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        path = repo.init_db()
        assert path.is_file()

    def test_session_auto_inits_db(self, fs: FileSystem) -> None:
        repo = BaseRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        with repo.session() as session:
            assert session is not None
        assert repo.db_path.is_file()

    def test_custom_db_engine(self, fs: FileSystem) -> None:
        custom_engine = BaseRepository(cwd=fs.base_path, db_rel_path=DB_REL).db_engine
        repo = BaseRepository(cwd=fs.base_path, db_engine=custom_engine)
        assert repo.db_engine is custom_engine
        assert repo.engine is custom_engine


class TestSandboxesRepository:
    """Tests for SandboxesRepository CRUD methods."""

    def test_insert_and_get(self, fs: FileSystem) -> None:
        db = SandboxesRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        sb = db.insert(
            id="sb-001",
            branch_name="feat/branch",
            base_commit="abc123",
            sandbox_path=fs.base_path / "sb-001",
        )

        assert isinstance(sb, SandboxRecord)
        assert sb.id == "sb-001"
        assert sb.branch_name == "feat/branch"
        assert sb.base_commit == "abc123"
        assert sb.sandbox_path == fs.base_path / "sb-001"
        assert sb.status == SandboxStatus.ACTIVE
        assert sb.name is None
        assert sb.created_at
        assert sb.updated_at

        fetched = db.get("sb-001")
        assert fetched == sb

    def test_insert_with_name(self, fs: FileSystem) -> None:
        db = SandboxesRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        sb = db.insert(
            id="sb-named",
            branch_name="feat/named",
            base_commit="def456",
            sandbox_path=fs.base_path / "sb-named",
            name="my-sandbox",
        )
        assert sb.name == "my-sandbox"

    def test_insert_duplicate_raises_value_error(self, fs: FileSystem) -> None:
        db = SandboxesRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        db.insert(id="dup-id", branch_name="b", base_commit="c", sandbox_path=fs.base_path / "dup")
        with pytest.raises(ValueError, match="dup-id"):
            db.insert(id="dup-id", branch_name="b2", base_commit="c2", sandbox_path=fs.base_path / "dup2")

    def test_get_missing_returns_none(self, fs: FileSystem) -> None:
        db = SandboxesRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        assert db.get("does-not-exist") is None

    def test_list_unfiltered_and_filtered(self, fs: FileSystem) -> None:
        db = SandboxesRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        db.insert(id="a", branch_name="b", base_commit="c", sandbox_path=fs.base_path / "a")
        db.insert(id="b", branch_name="b2", base_commit="c2", sandbox_path=fs.base_path / "b")
        db.update_status("b", SandboxStatus.MERGED)

        all_rows = db.list()
        assert len(all_rows) == 2

        active = db.list(status=SandboxStatus.ACTIVE)
        assert len(active) == 1
        assert active[0].id == "a"

        merged = db.list(status=SandboxStatus.MERGED)
        assert len(merged) == 1
        assert merged[0].id == "b"

    def test_update_status(self, fs: FileSystem) -> None:
        db = SandboxesRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        sb = db.insert(id="upd", branch_name="b", base_commit="c", sandbox_path=fs.base_path / "upd")
        original_updated_at = sb.updated_at

        updated = db.update_status("upd", SandboxStatus.CLEANED)
        assert updated is not None
        assert updated.status == SandboxStatus.CLEANED
        assert updated.updated_at >= original_updated_at

    def test_update_status_missing_returns_none(self, fs: FileSystem) -> None:
        db = SandboxesRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        assert db.update_status("ghost", SandboxStatus.CLEANED) is None

    def test_delete(self, fs: FileSystem) -> None:
        db = SandboxesRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        db.insert(id="del-me", branch_name="b", base_commit="c", sandbox_path=fs.base_path / "del-me")

        assert db.delete("del-me") is True
        assert db.get("del-me") is None
        assert db.delete("del-me") is False

    def test_reconcile_stale_active_all(self, fs: FileSystem) -> None:
        db = SandboxesRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        active_existing_dir = fs.base_path / "existing-dir"
        active_existing_dir.mkdir(parents=True, exist_ok=True)
        active_missing_dir1 = fs.base_path / "missing-dir-1"
        active_missing_dir2 = fs.base_path / "missing-dir-2"

        db.insert(id="sb-alive", branch_name="b1", base_commit="c1", sandbox_path=active_existing_dir)
        db.insert(id="sb-stale", branch_name="b2", base_commit="c2", sandbox_path=active_missing_dir1)
        db.insert(id="sb-cleaned", branch_name="b3", base_commit="c3", sandbox_path=active_missing_dir2)
        db.update_status("sb-cleaned", SandboxStatus.CLEANED)

        reconciled = db.reconcile_stale_active()
        assert len(reconciled) == 1
        assert reconciled[0].id == "sb-stale"
        assert reconciled[0].status == SandboxStatus.CLEANED

        alive = db.get("sb-alive")
        assert alive is not None
        assert alive.status == SandboxStatus.ACTIVE

        stale = db.get("sb-stale")
        assert stale is not None
        assert stale.status == SandboxStatus.CLEANED

    def test_reconcile_stale_active_by_id(self, fs: FileSystem) -> None:
        db = SandboxesRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        active_missing_dir1 = fs.base_path / "missing-dir-1"
        active_missing_dir2 = fs.base_path / "missing-dir-2"
        db.insert(id="sb-target", branch_name="b1", base_commit="c1", sandbox_path=active_missing_dir1)
        db.insert(id="sb-other", branch_name="b2", base_commit="c2", sandbox_path=active_missing_dir2)

        reconciled = db.reconcile_stale_active(id="sb-target")
        assert len(reconciled) == 1
        assert reconciled[0].id == "sb-target"

        target = db.get("sb-target")
        assert target is not None
        assert target.status == SandboxStatus.CLEANED

        other = db.get("sb-other")
        assert other is not None
        assert other.status == SandboxStatus.ACTIVE


class TestCatalogRepository:
    """Tests for CatalogRepository repository methods."""

    def test_upsert_insert_and_get_by_sha_and_name(self, fs: FileSystem) -> None:
        db = CatalogRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        path = Path(".worktree/catalog/workflow_a.yaml")
        rec = db.upsert(
            sha="workflow_1234567",
            item_type=CatalogItemType.WORKFLOW,
            name="workflow_a",
            path=path,
            checksum="hash1",
        )

        assert isinstance(rec, CatalogRecord)
        assert rec.id == 1
        assert rec.sha == "workflow_1234567"
        assert rec.item_type == CatalogItemType.WORKFLOW
        assert rec.name == "workflow_a"
        assert rec.path == path
        assert rec.checksum == "hash1"
        assert rec.created_at
        assert rec.updated_at

        by_sha = db.get_by_sha("workflow_1234567")
        assert by_sha == rec

        by_name = db.get_by_name("workflow_a")
        assert by_name == rec

        by_name_and_type = db.get_by_name(
            "workflow_a",
            item_type=CatalogItemType.WORKFLOW,
        )
        assert by_name_and_type == rec

    def test_upsert_update_preserves_id_and_updates_fields(self, fs: FileSystem) -> None:
        db = CatalogRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        path = Path(".worktree/catalog/task_b.yaml")
        first = db.upsert(
            sha="task_1111111",
            item_type=CatalogItemType.TASK,
            name="task_b",
            path=path,
            checksum="chk1",
        )
        first_id = first.id
        first_created_at = first.created_at

        second = db.upsert(
            sha="task_2222222",
            item_type=CatalogItemType.TASK,
            name="task_b_v2",
            path=path,
            checksum="chk2",
        )

        assert second.id == first_id
        assert second.sha == "task_2222222"
        assert second.name == "task_b_v2"
        assert second.path == path
        assert second.checksum == "chk2"
        assert second.created_at == first_created_at

    def test_get_missing_catalog_item_returns_none(self, fs: FileSystem) -> None:
        db = CatalogRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        assert db.get_by_sha("missing") is None
        assert db.get_by_name("missing_name") is None

    def test_list_catalog_items_filtering(self, fs: FileSystem) -> None:
        db = CatalogRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        db.upsert(sha="w1", item_type=CatalogItemType.WORKFLOW, name="wf1", path=Path("w1.yaml"), checksum="c1")
        db.upsert(sha="t1", item_type=CatalogItemType.TASK, name="task1", path=Path("t1.yaml"), checksum="c2")
        db.upsert(sha="s1", item_type=CatalogItemType.STEP, name="step1", path=Path("s1.yaml"), checksum="c3")

        all_items = db.list()
        assert len(all_items) == 3

        workflows = db.list(item_type=CatalogItemType.WORKFLOW)
        assert len(workflows) == 1
        assert workflows[0].sha == "w1"

        steps = db.list(item_type="step")
        assert len(steps) == 1
        assert steps[0].sha == "s1"

    def test_list_by_name(self, fs: FileSystem) -> None:
        db = CatalogRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        db.upsert(
            sha="n1", item_type=CatalogItemType.WORKFLOW, name="shared", path=Path("a/shared.yaml"), checksum="c1"
        )
        db.upsert(sha="n2", item_type=CatalogItemType.TASK, name="shared", path=Path("b/shared.yaml"), checksum="c2")

        all_shared = db.list_by_name("shared")
        assert len(all_shared) == 2

        wf_shared = db.list_by_name("shared", item_type=CatalogItemType.WORKFLOW)
        assert len(wf_shared) == 1
        assert wf_shared[0].sha == "n1"

    def test_invalid_catalog_item_type_raises_value_error(self, fs: FileSystem) -> None:
        db = CatalogRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        with pytest.raises(ValueError, match="constraint"):
            db.upsert(
                sha="invalid",
                item_type="invalid_type",  # type: ignore[arg-type]
                name="invalid",
                path=Path("invalid.yaml"),
                checksum="c",
            )

    def test_delete_catalog_item(self, fs: FileSystem) -> None:
        db = CatalogRepository(cwd=fs.base_path, db_rel_path=DB_REL)
        db.upsert(
            sha="to_delete",
            item_type=CatalogItemType.WORKFLOW,
            name="delete_item",
            path=Path("delete.yaml"),
            checksum="c_del",
        )

        assert db.delete("to_delete") is True
        assert db.get_by_sha("to_delete") is None
        assert db.delete("to_delete") is False


class TestWorktreeDbFacade:
    """Tests for WorktreeDb unified facade."""

    def test_facade_sub_repository_access(self, fs: FileSystem) -> None:
        db = WorktreeDb(cwd=fs.base_path, db_rel_path=DB_REL)
        db.init_db()

        assert db.sandboxes.db_engine is db.db_engine
        assert db.runs.db_engine is db.db_engine
        assert db.catalog.db_engine is db.db_engine
        assert db.costs.db_engine is db.db_engine
        assert db.engine is db.db_engine

        sb = db.sandboxes.insert(
            id="sb_facade",
            branch_name="feat/facade",
            base_commit="abc",
            sandbox_path=fs.base_path / "sb_facade",
        )
        assert db.sandboxes.get("sb_facade") == sb

        run = db.runs.create(
            session_id="run_facade",
            blueprint_name="demo",
            kind=BlueprintKind.WORKFLOW,
            branch_name="b",
        )
        assert isinstance(run, RunRecord)
        assert db.runs.get("run_facade") == run

        cat = db.catalog.upsert(
            sha="c_facade",
            item_type=CatalogItemType.WORKFLOW,
            name="wf_cat",
            path=Path("wf_cat.yaml"),
            checksum="c",
        )
        assert db.catalog.get_by_sha("c_facade") == cat

        cost_id = db.costs.record_token_usage(
            session_id="run_facade",
            branch_name="b",
            model_id="gpt-4o",
            prompt_tokens=10,
            completion_tokens=20,
            estimated_usd_cost=0.005,
        )
        assert cost_id is not None
        totals = db.costs.get_session_total_cost("run_facade")
        assert totals["total_tokens"] == 30

    def test_facade_custom_db_engine(self, fs: FileSystem) -> None:
        db1 = WorktreeDb(cwd=fs.base_path, db_rel_path=DB_REL)
        db2 = WorktreeDb(cwd=fs.base_path, db_engine=db1.db_engine)
        assert db2.db_engine is db1.db_engine
        assert db2.sandboxes.db_engine is db1.db_engine
