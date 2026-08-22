"""Orchestrate native Git worktree sandboxes for isolated command execution."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from worktree.common.constants import GIT_SUBPROCESS_TIMEOUT_SECONDS
from worktree.core.config.context import get_current_git_branch
from worktree.core.config.loader import ConfigLoadStatus, load_config_result
from worktree.core.config.models import WorktreeConfig
from worktree.core.db import SandboxStatus
from worktree.core.db.repositories.sandboxes import SandboxesRepository


class GitPlumbingTimeoutError(RuntimeError):
    """Raised when an internal git plumbing subprocess exceeds its timeout."""


class SandboxSession(BaseModel):
    """Metadata for one isolated background git worktree."""

    model_config = {"extra": "forbid", "strict": True}

    session_id: str
    target_branch: str
    sandbox_path: Path
    base_commit: str
    name: str | None = None
    created_at: str
    command_passed: bool | None = None
    wip_applied: bool = False
    wip_paths: list[str] = Field(default_factory=list)


class SandboxCreateStatus(StrEnum):
    """Classified outcomes for creating a sandbox worktree."""

    OK = "ok"
    CAPACITY_EXCEEDED = "capacity_exceeded"
    GIT_FAILED = "git_failed"
    GIT_TIMEOUT = "git_timeout"
    NOT_INITIALIZED = "not_initialized"
    UNREADABLE_CONFIG = "unreadable_config"
    WIP_FAILED = "wip_failed"


class SandboxCreateResult(BaseModel):
    """Non-raising result of sandbox creation."""

    model_config = {"extra": "forbid", "strict": True}

    status: SandboxCreateStatus
    session: SandboxSession | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return True when a sandbox session was created successfully."""
        return self.status == SandboxCreateStatus.OK and not self.errors


def _clean_opt_str(val: str | None) -> str | None:
    if val is None:
        return None
    s = val.strip()
    return s if s else None


def _normalize_repo_rel(path: str) -> str:
    return path.strip().replace("\\", "/")


def _list_wip_paths(repo_root: Path) -> list[str]:
    """Return sorted repo-relative paths with uncommitted changes.

    Includes tracked modifications/deletions and untracked non-ignored files.
    """
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "-u"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitPlumbingTimeoutError(
            f"Git timed out after {GIT_SUBPROCESS_TIMEOUT_SECONDS}s ('git status --porcelain -u') (GIT_TIMEOUT)"
        ) from exc
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    paths: set[str] = set()
    for raw in completed.stdout.splitlines():
        if len(raw) < 4:
            continue
        entry = raw[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip().strip('"')
        rel = _normalize_repo_rel(entry)
        if rel:
            paths.add(rel)
    return sorted(paths)


def _remove_dst(dst: Path) -> None:
    """Remove *dst* regardless of whether it is a file, symlink, or directory."""
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()


def _copy_wip_file(source_root: Path, dest_root: Path, rel: str) -> None:
    """Mirror a single working-tree path from *source_root* into *dest_root*.

    Behaviour by case:

    - **Source deleted**: remove the corresponding destination path (file,
      symlink, or directory tree) so the sandbox stays in sync.
    - **Source is a plain directory**: skip — directory entries are created
      implicitly when their children are copied.
    - **Source is a symlink**: recreate the symlink at the destination,
      replacing whatever was there before.
    - **Source is a regular file**: copy the file (preserving metadata via
      :func:`shutil.copy2`), creating any missing parent directories.

    Args:
        source_root: Absolute path to the primary repository checkout.
        dest_root: Absolute path to the sandbox worktree.
        rel: Repo-relative path of the entry to mirror.
    """
    src = source_root / rel
    dst = dest_root / rel
    if not src.exists():
        _remove_dst(dst)
        return
    if src.is_dir() and not src.is_symlink():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        _remove_dst(dst)
        dst.symlink_to(src.readlink())
        return
    shutil.copy2(src, dst)


def apply_wip_to_sandbox(*, source_root: Path, sandbox_path: Path) -> list[str]:
    """Overlay uncommitted working-tree changes into an existing sandbox.

    Copies tracked and untracked (non-ignored) paths from ``source_root`` into
    ``sandbox_path``. Deleted tracked files are removed in the sandbox.

    Args:
        source_root: Primary repository checkout (WIP source).
        sandbox_path: Sandbox worktree path.

    Returns:
        Sorted list of repo-relative paths touched by the overlay.

    Raises:
        RuntimeError: When overlay fails.
    """
    root = source_root.expanduser().resolve()
    dest = sandbox_path.expanduser().resolve()
    if not dest.is_dir():
        raise RuntimeError(f"sandbox path does not exist: {dest}")

    paths = _list_wip_paths(root)
    try:
        for rel in paths:
            _copy_wip_file(root, dest, rel)
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc
    return paths


class GitSandboxManager:
    """Manages creation, cleanup, and pruning of background Git worktrees."""

    def __init__(
        self,
        cwd: Path | None = None,
        sandboxes_db: SandboxesRepository | None = None,
    ) -> None:
        """Bind to an absolute repository root.

        Args:
            cwd: Repository root. Defaults to the process current directory.
            sandboxes_db: Optional SandboxesRepository instance.
        """
        self.cwd = (cwd or Path.cwd()).expanduser().resolve()
        self.sandbox_base_dir = self.cwd / ".worktree" / "sandboxes"
        self.sandboxes_db = sandboxes_db or SandboxesRepository(self.cwd)
        self._config: WorktreeConfig | None = None

    @property
    def config(self) -> WorktreeConfig | None:
        """Return the config last loaded by a successful create attempt.

        Populated when ``create_sandbox_result`` loads config successfully.
        ``None`` before the first successful load or when create failed before
        assigning config.
        """
        return self._config

    def _ensure_sandbox_dir(self) -> None:
        """Create the parent sandbox storage directory if missing."""
        self.sandbox_base_dir.mkdir(parents=True, exist_ok=True)

    def _run_git_cmd(self, args: list[str], cwd: Path | None = None) -> str:
        """Execute a git command and return stripped stdout.

        Args:
            args: Git arguments after ``git``.
            cwd: Working directory for the command.

        Returns:
            Stripped stdout text.

        Raises:
            RuntimeError: When git exits non-zero.
        """
        target_dir = cwd or self.cwd
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=target_dir,
                capture_output=True,
                text=True,
                check=True,
                timeout=GIT_SUBPROCESS_TIMEOUT_SECONDS,
            )
            return result.stdout.strip()
        except FileNotFoundError as exc:
            raise RuntimeError(f"Git execution failed ('git {' '.join(args)}'): git not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitPlumbingTimeoutError(
                f"Git timed out after {GIT_SUBPROCESS_TIMEOUT_SECONDS}s ('git {' '.join(args)}') (GIT_TIMEOUT)"
            ) from exc
        except subprocess.CalledProcessError as exc:
            err_msg = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise RuntimeError(f"Git execution failed ('git {' '.join(args)}'): {err_msg}") from exc

    def get_active_sandboxes(self) -> list[Path]:
        """List immediate child directories under the sandbox base path.

        Returns:
            Sandbox directory paths, or an empty list when the base is missing.
        """
        if not self.sandbox_base_dir.exists():
            return []
        return [p for p in self.sandbox_base_dir.iterdir() if p.is_dir()]

    def _discard_partial_sandbox(self, sandbox_path: Path, temp_branch: str) -> None:
        """Best-effort remove of a partial worktree/branch after failed create."""
        if sandbox_path.exists():
            try:
                self._run_git_cmd(["worktree", "remove", "--force", str(sandbox_path)])
            except RuntimeError:
                shutil.rmtree(sandbox_path, ignore_errors=True)
        try:
            self._run_git_cmd(["branch", "-D", temp_branch])
        except RuntimeError:
            pass
        try:
            self._run_git_cmd(["worktree", "prune"])
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # create_sandbox_result helpers
    # ------------------------------------------------------------------

    def _load_and_validate_config(
        self,
    ) -> tuple[SandboxCreateResult, None] | tuple[None, WorktreeConfig]:
        """Load and validate worktree config.

        Returns:
            ``(error_result, None)`` when config is missing or unreadable,
            ``(None, config)`` on success.
        """
        load = load_config_result(cwd=self.cwd)
        if load.status == ConfigLoadStatus.NOT_FOUND:
            return (
                SandboxCreateResult(
                    status=SandboxCreateStatus.NOT_INITIALIZED,
                    errors=[
                        f"Worktree is not initialized; config missing at "
                        f"'{load.config_path}' (SANDBOX_NOT_INITIALIZED).\n"
                        "Fix:\n"
                        "- run `wt init` to create `.worktree/config.json`"
                    ],
                ),
                None,
            )
        if not load.ok or load.config is None:
            detail = load.errors[0] if load.errors else str(load.status)
            return (
                SandboxCreateResult(
                    status=SandboxCreateStatus.UNREADABLE_CONFIG,
                    errors=[
                        f"Unable to load Worktree config for sandbox create "
                        f"(SANDBOX_CONFIG_UNREADABLE): {detail}\n"
                        "Fix:\n"
                        "- repair `.worktree/config.json` or run `wt init --repair`"
                    ],
                ),
                None,
            )
        self._config = load.config
        return None, load.config

    def _check_capacity(self, config: WorktreeConfig) -> SandboxCreateResult | None:
        """Return an error result when the active-sandbox cap has been reached."""
        active = self.get_active_sandboxes()
        max_allowed = config.sandbox.max_active_sandboxes
        if len(active) >= max_allowed:
            return SandboxCreateResult(
                status=SandboxCreateStatus.CAPACITY_EXCEEDED,
                errors=[
                    f"Maximum active sandboxes reached "
                    f"({len(active)}/{max_allowed}).\n"
                    "Fix:\n"
                    "- run `wt prune` to remove stale sandboxes, or\n"
                    "- raise sandbox.max_active_sandboxes in .worktree/config.json"
                ],
            )
        return None

    def _resolve_base_ref(self, override_base_ref: str | None, config: WorktreeConfig) -> str:
        """Return the git ref to branch the sandbox from.

        Uses *override_base_ref* when provided; otherwise falls back to the
        current branch or the configured ``sandbox.base_ref``.
        """
        if override_base_ref is not None:
            return override_base_ref
        source_branch = get_current_git_branch(self.cwd)
        if source_branch not in ("unknown", "HEAD (detached)"):
            return source_branch
        return config.sandbox.base_ref

    def _run_git_worktree_add(
        self,
        sandbox_path: Path,
        temp_branch: str,
        resolved_base_ref: str,
    ) -> SandboxCreateResult | None:
        """Run ``git worktree add`` and return an error result on failure."""
        try:
            self._run_git_cmd(["worktree", "add", "-b", temp_branch, str(sandbox_path), resolved_base_ref])
            return None
        except GitPlumbingTimeoutError as exc:
            self._discard_partial_sandbox(sandbox_path, temp_branch)
            return SandboxCreateResult(
                status=SandboxCreateStatus.GIT_TIMEOUT,
                errors=[
                    f"Git worktree operation timed out "
                    f"(SANDBOX_GIT_TIMEOUT): {exc}\n"
                    "Fix:\n"
                    "- check for git lock files, credential prompts, or a "
                    "stuck git process, then retry"
                ],
            )
        except RuntimeError as exc:
            self._discard_partial_sandbox(sandbox_path, temp_branch)
            return SandboxCreateResult(
                status=SandboxCreateStatus.GIT_FAILED,
                errors=[
                    f"Git worktree operation failed (SANDBOX_GIT_FAILED): {exc}\n"
                    "Fix:\n"
                    "- ensure this directory is a Git repository with a valid "
                    "base ref"
                ],
            )

    def _get_base_commit(
        self,
        sandbox_path: Path,
        temp_branch: str,
    ) -> tuple[str, None] | tuple[None, SandboxCreateResult]:
        """Resolve HEAD of the new worktree.

        Returns:
            ``(commit_sha, None)`` on success,
            ``(None, error_result)`` on git failure.
        """
        try:
            commit = self._run_git_cmd(["rev-parse", "HEAD"], cwd=sandbox_path)
            return commit, None
        except GitPlumbingTimeoutError as exc:
            self._discard_partial_sandbox(sandbox_path, temp_branch)
            return None, SandboxCreateResult(
                status=SandboxCreateStatus.GIT_TIMEOUT,
                errors=[
                    f"Git worktree operation timed out "
                    f"(SANDBOX_GIT_TIMEOUT): {exc}\n"
                    "Fix:\n"
                    "- check for git lock files, credential prompts, or a "
                    "stuck git process, then retry"
                ],
            )
        except RuntimeError as exc:
            self._discard_partial_sandbox(sandbox_path, temp_branch)
            return None, SandboxCreateResult(
                status=SandboxCreateStatus.GIT_FAILED,
                errors=[
                    f"Git worktree operation failed (SANDBOX_GIT_FAILED): {exc}\n"
                    "Fix:\n"
                    "- ensure this directory is a Git repository with a valid "
                    "base ref"
                ],
            )

    def _apply_wip_overlay(
        self,
        sandbox_path: Path,
        temp_branch: str,
    ) -> tuple[list[str], None] | tuple[None, SandboxCreateResult]:
        """Overlay uncommitted working-tree changes into *sandbox_path*.

        Returns:
            ``(wip_paths, None)`` on success,
            ``(None, error_result)`` on failure.
        """
        try:
            paths = apply_wip_to_sandbox(source_root=self.cwd, sandbox_path=sandbox_path)
            return paths, None
        except GitPlumbingTimeoutError as exc:
            self._discard_partial_sandbox(sandbox_path, temp_branch)
            return None, SandboxCreateResult(
                status=SandboxCreateStatus.GIT_TIMEOUT,
                errors=[
                    f"Git timed out while overlaying uncommitted WIP "
                    f"(SANDBOX_GIT_TIMEOUT): {exc}\n"
                    "Fix:\n"
                    "- check for git lock files or a stuck git process, "
                    "then retry, or\n"
                    "- omit --wip and commit changes first"
                ],
            )
        except RuntimeError as exc:
            self._discard_partial_sandbox(sandbox_path, temp_branch)
            return None, SandboxCreateResult(
                status=SandboxCreateStatus.WIP_FAILED,
                errors=[
                    f"Failed to overlay uncommitted WIP into sandbox "
                    f"(SANDBOX_WIP_FAILED): {exc}\n"
                    "Fix:\n"
                    "- resolve local conflicts / binary issues and retry, or\n"
                    "- omit --wip and commit changes first"
                ],
            )

    def _persist_sandbox_session(self, session: SandboxSession) -> list[str]:
        """Insert *session* into the local DB; return any warning messages."""
        try:
            self.sandboxes_db.insert(
                id=session.session_id,
                name=session.name,
                branch_name=session.target_branch,
                base_commit=session.base_commit,
                sandbox_path=session.sandbox_path,
            )
            return []
        except Exception as exc:
            return [f"Failed to persist sandbox metadata to the local database: {exc}"]

    def _collect_wip_paths(
        self,
        include_wip: bool,
        sandbox_path: Path,
        temp_branch: str,
    ) -> tuple[list[str], None] | tuple[None, SandboxCreateResult]:
        """Return WIP paths to overlay, or an error result.

        When *include_wip* is ``False`` returns an empty list immediately.
        Otherwise delegates to :meth:`_apply_wip_overlay`.

        Returns:
            ``(paths, None)`` on success, ``(None, error_result)`` on failure.
        """
        if not include_wip:
            return [], None
        return self._apply_wip_overlay(sandbox_path, temp_branch)

    def _build_session(
        self,
        *,
        sid: str,
        temp_branch: str,
        sandbox_path: Path,
        base_commit: str,
        resolved_name: str | None,
        include_wip: bool,
        wip_paths: list[str],
    ) -> SandboxSession:
        """Construct and return a :class:`SandboxSession` from resolved fields."""
        return SandboxSession(
            session_id=sid,
            target_branch=temp_branch,
            sandbox_path=sandbox_path,
            base_commit=base_commit,
            name=resolved_name,
            created_at=datetime.now(UTC).isoformat(),
            wip_applied=bool(include_wip),
            wip_paths=wip_paths,
        )

    def _prepare_sandbox_session(
        self,
        *,
        sid: str,
        sandbox_path: Path,
        temp_branch: str,
        resolved_base_ref: str,
        resolved_name: str | None,
        include_wip: bool,
    ) -> tuple[SandboxCreateResult | None, SandboxSession | None]:
        add_err = self._run_git_worktree_add(sandbox_path, temp_branch, resolved_base_ref)
        if add_err is not None:
            return add_err, None

        base_commit, commit_err = self._get_base_commit(sandbox_path, temp_branch)
        if commit_err is not None or base_commit is None:
            return commit_err or SandboxCreateResult(
                status=SandboxCreateStatus.GIT_FAILED,
                errors=["Failed to determine base commit"],
            ), None

        wip_paths, wip_err = self._collect_wip_paths(include_wip, sandbox_path, temp_branch)
        if wip_err is not None or wip_paths is None:
            return wip_err or SandboxCreateResult(
                status=SandboxCreateStatus.GIT_FAILED,
                errors=["Failed to collect WIP paths"],
            ), None

        session = self._build_session(
            sid=sid,
            temp_branch=temp_branch,
            sandbox_path=sandbox_path,
            base_commit=base_commit,
            resolved_name=resolved_name,
            include_wip=include_wip,
            wip_paths=wip_paths,
        )
        return None, session

    def create_sandbox_result(
        self,
        session_id: str | None = None,
        *,
        include_wip: bool = False,
        name: str | None = None,
        base_ref: str | None = None,
    ) -> SandboxCreateResult:
        """Create a sandbox without raising for classified failures.

        Orchestrates config loading, capacity checks, worktree creation, and
        optional WIP overlay. Each phase is delegated to a private helper so
        that errors are returned as structured :class:`SandboxCreateResult`
        values rather than exceptions.

        Args:
            session_id: Optional fixed session id; otherwise ``sbx_`` + 8 hex.
            include_wip: When True, overlay uncommitted working-tree changes.
            name: Optional human-readable sandbox name. Whitespace-only values
                are stored as ``None``.
            base_ref: Optional git ref for ``git worktree add``.

        Returns:
            Structured create result with session on success.
        """
        resolved_name = _clean_opt_str(name)
        override_base_ref = _clean_opt_str(base_ref)

        config_err, config = self._load_and_validate_config()
        if config_err is not None or config is None:
            return config_err or SandboxCreateResult(
                status=SandboxCreateStatus.NOT_INITIALIZED,
                errors=["Configuration not loaded"],
            )

        self._ensure_sandbox_dir()
        capacity_err = self._check_capacity(config)
        if capacity_err is not None:
            return capacity_err

        sid = session_id or f"sbx_{uuid.uuid4().hex[:8]}"
        sandbox_path = (self.sandbox_base_dir / sid).resolve()
        temp_branch = f"worktree/sandbox-{sid}"
        resolved_base_ref = self._resolve_base_ref(override_base_ref, config)

        err, session = self._prepare_sandbox_session(
            sid=sid,
            sandbox_path=sandbox_path,
            temp_branch=temp_branch,
            resolved_base_ref=resolved_base_ref,
            resolved_name=resolved_name,
            include_wip=include_wip,
        )
        if err is not None or session is None:
            return err or SandboxCreateResult(
                status=SandboxCreateStatus.GIT_FAILED,
                errors=["Failed to prepare sandbox session"],
            )

        warnings = self._persist_sandbox_session(session)
        return SandboxCreateResult(
            status=SandboxCreateStatus.OK,
            session=session,
            warnings=warnings,
        )

    def create_sandbox(
        self,
        session_id: str | None = None,
        *,
        include_wip: bool = False,
        name: str | None = None,
        base_ref: str | None = None,
    ) -> SandboxSession:
        """Create a sandbox or raise with the classified error message.

        Args:
            session_id: Optional fixed session id.
            include_wip: When True, overlay uncommitted working-tree changes.
            name: Optional human-readable sandbox name.
            base_ref: Optional git ref override for worktree creation.

        Returns:
            Created session metadata.

        Raises:
            RuntimeError: When creation fails for any classified reason.
        """
        result = self.create_sandbox_result(
            session_id=session_id,
            include_wip=include_wip,
            name=name,
            base_ref=base_ref,
        )
        if not result.ok or result.session is None:
            message = result.errors[0] if result.errors else f"Sandbox create failed: {result.status}"
            raise RuntimeError(message)
        return result.session

    def cleanup_sandbox(self, session: SandboxSession, *, force: bool = True) -> None:
        """Remove worktree, delete throwaway branch, and prune (idempotent).

        Args:
            session: Session returned from create.
            force: Pass ``--force`` to ``git worktree remove`` when True.
        """
        if session.sandbox_path.exists():
            cmd = ["worktree", "remove", str(session.sandbox_path)]
            if force:
                cmd.append("--force")
            try:
                self._run_git_cmd(cmd)
            except RuntimeError:
                shutil.rmtree(session.sandbox_path, ignore_errors=True)

        try:
            self.sandboxes_db.update_status(
                session.session_id,
                SandboxStatus.CLEANED,
            )
        except Exception:
            # Intentional best-effort local-DB bookkeeping during cleanup:
            # worktree removal and branch deletion proceed independently.
            pass

        try:
            self._run_git_cmd(["branch", "-D", session.target_branch])
        except RuntimeError:
            pass

        try:
            self.prune()
        except RuntimeError:
            pass

    def prune(self) -> None:
        """Prune stale Git worktree registrations."""
        self._run_git_cmd(["worktree", "prune"])
