"""Inventory facade over local `.worktree/catalog/` YAML documents."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import yaml

from worktree.common.fs import atomic_write_text, read_yaml_file
from worktree.core.catalog.exceptions import (
    CatalogFileNotFoundError,
    CatalogWriteError,
    CatalogYamlError,
)
from worktree.core.catalog.models import CatalogResolveResult, CatalogResolveStatus
from worktree.core.catalog.services.inventory import (
    ensure_catalog_dirs,
    get_catalog_dir,
    scan_and_index_catalog,
)
from worktree.core.db import CatalogItemType, CatalogRecord
from worktree.core.db.repositories.catalog import CatalogRepository


class Catalog:
    """Inventory only. Returns raw YAML / records — never Blueprint or Step."""

    _TASK_AND_WORKFLOW: ClassVar[frozenset[CatalogItemType]] = frozenset(
        {CatalogItemType.TASK, CatalogItemType.WORKFLOW}
    )
    _STEP_ONLY: ClassVar[frozenset[CatalogItemType]] = frozenset({CatalogItemType.STEP})

    def __init__(self, cwd: Path | None = None, repo: CatalogRepository | None = None) -> None:
        self.cwd = (cwd or Path.cwd()).resolve()
        self.db = repo or CatalogRepository(self.cwd)

    def resolve(self, name: str) -> CatalogResolveResult:
        """Load a task or workflow YAML by SHA or catalog name."""
        return self._resolve(name, self._TASK_AND_WORKFLOW)

    def resolve_step(self, name: str) -> CatalogResolveResult:
        """Load a reusable step YAML by SHA or catalog name."""
        return self._resolve(name, self._STEP_ONLY)

    def list(self, kind: CatalogItemType | str | None = None) -> list[CatalogRecord]:
        """Return indexed catalog records, optionally filtered by item type."""
        scan_and_index_catalog(self.cwd, repo=self.db)
        if kind is None:
            return self.db.list()
        return self.db.list(item_type=self._coerce_item_type(kind))

    def save(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        item_type: CatalogItemType | str,
    ) -> CatalogRecord:
        """Write YAML under the type folder and reindex. Overwrites an existing file."""
        type_enum = self._coerce_item_type(item_type)
        catalog_dir = ensure_catalog_dirs(self.cwd)
        stem = self._strip_yaml_suffix(name)
        rel_path = Path(f"{type_enum.value}s") / f"{stem}.yml"
        target_path = catalog_dir / rel_path
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, default_flow_style=False)
        if not text.endswith("\n"):
            text += "\n"
        try:
            atomic_write_text(target_path, text)
        except OSError as exc:
            raise CatalogWriteError(f"Failed to write catalog blueprint '{target_path}': {exc}") from exc
        scan_and_index_catalog(self.cwd, repo=self.db)
        record = self._record_for_rel_path(rel_path)
        if record is None:
            raise CatalogWriteError(f"Failed to reindex catalog blueprint '{rel_path.as_posix()}'.")
        return record

    @staticmethod
    def read_yaml(path: Path) -> dict[str, Any]:
        """Load a YAML object from ``path`` or raise a classified catalog error."""
        if not path.exists():
            raise CatalogFileNotFoundError(f"Catalog file not found at '{path}'.")
        yaml_file = read_yaml_file(path)
        if yaml_file.error or yaml_file.parsed is None or not isinstance(yaml_file.parsed, dict):
            detail = yaml_file.error or "invalid or non-object YAML content."
            raise CatalogYamlError(f"Failed to load catalog blueprint '{path}': {detail}")
        return yaml_file.parsed

    def _resolve(self, name: str, allowed_types: frozenset[CatalogItemType]) -> CatalogResolveResult:
        """Reindex, find typed matches, and load the winning YAML object."""
        scan_and_index_catalog(self.cwd, repo=self.db)
        matches = self._find_typed_matches(name, allowed_types)
        if not matches:
            return CatalogResolveResult(
                status=CatalogResolveStatus.NOT_FOUND,
                name=name,
                errors=[f"Catalog blueprint '{name}' not found."],
            )
        winner = matches[0]
        warnings = [self._duplicate_name_warning(name, winner, matches)] if len(matches) > 1 else []
        raw, parse_errors = self._parse_catalog_yaml(get_catalog_dir(self.cwd) / winner.path, winner.path)
        if parse_errors or raw is None:
            return CatalogResolveResult(
                status=CatalogResolveStatus.LOAD_ERROR,
                name=name,
                record=winner,
                matches=matches,
                errors=parse_errors,
                warnings=warnings,
            )
        return CatalogResolveResult(
            status=CatalogResolveStatus.OK,
            name=name,
            raw=raw,
            record=winner,
            matches=matches,
            warnings=warnings,
        )

    @staticmethod
    def _coerce_item_type(value: CatalogItemType | str) -> CatalogItemType:
        """Parse a catalog item type or raise ValueError with allowed choices."""
        if isinstance(value, CatalogItemType):
            return value
        try:
            return CatalogItemType(str(value).lower())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in CatalogItemType)
            raise ValueError(f"Invalid item_type '{value}'. Allowed choices: {allowed}") from exc

    @staticmethod
    def _strip_yaml_suffix(name: str) -> str:
        """Remove a trailing ``.yml`` / ``.yaml`` suffix when present."""
        if name.endswith(".yaml"):
            return name[:-5]
        if name.endswith(".yml"):
            return name[:-4]
        return name

    def _find_typed_matches(self, name: str, allowed_types: frozenset[CatalogItemType]) -> list[CatalogRecord]:
        """Return SHA or name matches restricted to ``allowed_types``, path-ascending."""
        by_sha = self.db.get_by_sha(name)
        if by_sha is not None:
            if by_sha.item_type in allowed_types:
                return [by_sha]
            return []
        matches: list[CatalogRecord] = []
        for item_type in allowed_types:
            matches.extend(self.db.list_by_name(name, item_type=item_type))
        return sorted(matches, key=lambda record: record.path.as_posix())

    @staticmethod
    def _duplicate_name_warning(name: str, winner: CatalogRecord, matches: list[CatalogRecord]) -> str:
        """Match ``get_catalog_item`` duplicate-name warning wording."""
        other_matching_paths = ", ".join(match.path.as_posix() for match in matches if match.path != winner.path)
        return f"Duplicate catalog name '{name}'; using '{winner.path.as_posix()}' (also found in: {other_matching_paths})."

    @staticmethod
    def _parse_catalog_yaml(file_path: Path, rel_path: Path) -> tuple[dict[str, Any] | None, list[str]]:
        """Read ``file_path`` as a YAML object, using ``rel_path`` in fallback errors."""
        yaml_file = read_yaml_file(file_path)
        if yaml_file.error or yaml_file.parsed is None or not isinstance(yaml_file.parsed, dict):
            error_message = (
                yaml_file.error or f"Failed to load catalog blueprint '{rel_path}': invalid or non-object YAML content."
            )
            return None, [error_message]
        return yaml_file.parsed, []

    def _record_for_rel_path(self, rel_path: Path) -> CatalogRecord | None:
        """Return the indexed record whose path equals ``rel_path``."""
        expected = rel_path.as_posix()
        for record in self.db.list():
            if record.path.as_posix() == expected:
                return record
        return None
