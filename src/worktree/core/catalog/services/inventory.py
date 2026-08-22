"""Catalog blueprint directory scanner, legacy migration engine, and inventory helper functions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, cast

from worktree.common.exceptions import DefinitionLoadError, DefinitionValidationError
from worktree.common.fs import (
    atomic_write_text,
    compute_content_checksum,
    delete_file,
    get_catalog_templates_dir,
    read_yaml_file,
    scan_yaml_directory,
)
from worktree.common.models import (
    DefinitionResolutionResult,
    DefinitionResolutionStatus,
    YamlFile,
)
from worktree.core.catalog.models import (
    CatalogScanResult,
    CatalogSubdirectoryScanResult,
    DefinitionValidationOutcome,
    YamlParseOutcome,
)
from worktree.core.db import (
    CatalogItemType,
    CatalogRecord,
)
from worktree.core.db.repositories.catalog import CatalogRepository


class _PydanticModel(Protocol):
    """Minimal protocol for catalog definition classes validated via Pydantic."""

    @classmethod
    def model_validate(cls, obj: Any) -> Any: ...


def get_catalog_dir(cwd: Path | None = None) -> Path:
    """Return absolute path to local `.worktree/catalog/` blueprint directory."""
    base_dir = (cwd or Path.cwd()).resolve()
    return base_dir / ".worktree" / "catalog"


def ensure_catalog_dirs(cwd: Path | None = None) -> Path:
    """Ensure directory structure under `.worktree/catalog/` exists."""
    catalog_dir = get_catalog_dir(cwd)
    for sub in ("workflows", "tasks", "steps"):
        (catalog_dir / sub).mkdir(parents=True, exist_ok=True)
    return catalog_dir


def compute_catalog_sha(item_type: CatalogItemType | str, content: str) -> tuple[str, str]:
    """Compute SHA-256 checksum and formatted SHA string (e.g. `workflow_a1b2c3d`)."""
    type_str = item_type.value if isinstance(item_type, CatalogItemType) else str(item_type)
    checksum = compute_content_checksum(content)
    sha = f"{type_str}_{checksum[:7]}"
    return sha, checksum


def _index_catalog_entry(
    repo: CatalogRepository,
    item_type: CatalogItemType,
    catalog_dir: Path,
    file_entry: YamlFile,
) -> tuple[CatalogRecord | None, str | None]:
    """Upsert a single scanned YAML file into the catalog DB, or return an error message."""
    if file_entry.error:
        return None, file_entry.error

    sha, checksum = compute_catalog_sha(item_type, str(file_entry.content))
    rel_path = file_entry.path.relative_to(catalog_dir)

    try:
        record = repo.upsert(
            sha=sha,
            item_type=item_type,
            name=file_entry.name,
            path=rel_path,
            checksum=checksum,
        )
        return record, None
    except Exception as exc:
        return None, f"Failed to index catalog record for '{rel_path}': {exc}"


def _index_scanned_entry(
    repo: CatalogRepository,
    item_type: CatalogItemType,
    catalog_dir: Path,
    file_entry: YamlFile,
) -> tuple[CatalogRecord | None, str | None]:
    """Index one catalog YAML entry and normalize the optional error payload."""
    record, error = _index_catalog_entry(repo, item_type, catalog_dir, file_entry)
    if error is not None:
        return None, error
    return record, None


def _append_scan_result(
    result: CatalogSubdirectoryScanResult,
    *,
    record: CatalogRecord | None,
    error: str | None,
) -> None:
    """Accumulate one indexed catalog entry into the scan result."""
    if error is not None:
        result.errors.append(error)
        return
    if record is None:
        return
    result.scanned_records.append(record)
    result.scanned_shas.add(record.sha)


def _scan_catalog_subdirectories(
    *, repo: CatalogRepository, catalog_dir: Path, subdirs: list[tuple[CatalogItemType, Path]]
) -> CatalogSubdirectoryScanResult:
    result = CatalogSubdirectoryScanResult(scanned_records=[], errors=[], scanned_shas=set())

    for item_type, sub_dir in subdirs:
        if not sub_dir.exists():
            continue
        for file_entry in scan_yaml_directory(sub_dir):
            record, error = _index_scanned_entry(repo, item_type, catalog_dir, file_entry)
            _append_scan_result(result, record=record, error=error)

    return result


def scan_and_index_catalog(
    cwd: Path | None = None,
    repo: CatalogRepository | None = None,
) -> CatalogScanResult:
    """Scan `.worktree/catalog/` subdirectories, compute SHA checksums, and sync SQLite database."""
    catalog_dir = ensure_catalog_dirs(cwd)
    catalog_repo = repo or CatalogRepository(cwd)

    subdirs: list[tuple[CatalogItemType, Path]] = [
        (CatalogItemType.WORKFLOW, catalog_dir / "workflows"),
        (CatalogItemType.TASK, catalog_dir / "tasks"),
        (CatalogItemType.STEP, catalog_dir / "steps"),
    ]
    scan_result = _scan_catalog_subdirectories(repo=catalog_repo, catalog_dir=catalog_dir, subdirs=subdirs)
    errors = scan_result.errors

    # Remove stale DB records for files no longer on disk
    try:
        db_items = catalog_repo.list()
        for record in db_items:
            if record.sha not in scan_result.scanned_shas:
                disk_file = catalog_dir / record.path
                if not disk_file.exists():
                    catalog_repo.delete(record.sha)
    except Exception as exc:
        errors.append(f"Error purging stale catalog DB records: {exc}")

    return CatalogScanResult(items=scan_result.scanned_records, errors=errors)


def _get_initial_template_content(type_enum: CatalogItemType, stem: str) -> str:
    template_path = get_catalog_templates_dir() / f"{type_enum.value}s" / "default.yml"
    try:
        content = template_path.read_text(encoding="utf-8")
        return content.replace("my-workflow", stem).replace("my-task", stem).replace("my-step", stem)
    except Exception:
        # Defensive fallback if the packaged resource is unreadable
        if type_enum == CatalogItemType.WORKFLOW:
            return f'version: "1.0"\nname: {stem}\ndescription: Custom workflow blueprint\nsteps: []\n'
        if type_enum == CatalogItemType.TASK:
            return f"name: {stem}\ndescription: Custom task blueprint\nuse_sandbox: false\nsteps: []\n"
        return f"name: {stem}\ndescription: Custom step blueprint\naction: run\n"


def create_catalog_item(
    item_type: CatalogItemType | str,
    name: str,
    cwd: Path | None = None,
    repo: CatalogRepository | None = None,
) -> CatalogRecord:
    """Create a new catalog blueprint under `.worktree/catalog/<type>s/<name>.yml` and sync database."""
    try:
        type_enum = item_type if isinstance(item_type, CatalogItemType) else CatalogItemType(str(item_type).lower())
    except ValueError as exc:
        allowed = ", ".join([t.value for t in CatalogItemType])
        raise ValueError(f"Invalid item_type '{item_type}'. Allowed choices: {allowed}") from exc

    catalog_dir = ensure_catalog_dirs(cwd)
    stem = name[:-4] if name.endswith(".yml") or name.endswith(".yaml") else name
    filename = f"{stem}.yml"
    target_path = catalog_dir / f"{type_enum.value}s" / filename

    if target_path.exists():
        rel_path = target_path.relative_to(catalog_dir)
        raise FileExistsError(f"Catalog blueprint collision at path '{rel_path}'")

    content = _get_initial_template_content(type_enum, stem)
    atomic_write_text(target_path, content)

    sha, checksum = compute_catalog_sha(type_enum, content)
    rel_path = target_path.relative_to(catalog_dir)

    catalog_repo = repo or CatalogRepository(cwd)
    return catalog_repo.upsert(
        sha=sha,
        item_type=type_enum,
        name=stem,
        path=rel_path,
        checksum=checksum,
    )


def _find_catalog_matches(
    cwd: Path | None,
    sha_or_name: str,
    type_filter: CatalogItemType | str | None,
    repo: CatalogRepository | None = None,
) -> list[CatalogRecord]:
    type_filter_string = (
        type_filter.value
        if isinstance(type_filter, CatalogItemType)
        else (str(type_filter).lower() if type_filter is not None else None)
    )

    catalog_repo = repo or CatalogRepository(cwd)
    item_by_sha = catalog_repo.get_by_sha(sha_or_name)
    if item_by_sha is not None:
        if type_filter_string is None or item_by_sha.item_type.value == type_filter_string:
            return [item_by_sha]
        return []
    return catalog_repo.list_by_name(sha_or_name, item_type=type_filter)


def _read_and_parse_yaml(file_path: Path, rel_path: Path) -> YamlParseOutcome:
    yaml_file = read_yaml_file(file_path)
    if yaml_file.error or yaml_file.parsed is None or not isinstance(yaml_file.parsed, dict):
        error_message = (
            yaml_file.error or f"Failed to load catalog blueprint '{rel_path}': invalid or non-object YAML content."
        )
        return YamlParseOutcome(parsed_data=None, errors=[error_message])
    return YamlParseOutcome(parsed_data=yaml_file.parsed, errors=[])


def _validate_definition[T](
    winner: CatalogRecord,
    definition_cls: type[T],
    cwd: Path | None,
    sha_or_name: str,
) -> DefinitionValidationOutcome:
    catalog_dir = get_catalog_dir(cwd)
    file_path = catalog_dir / winner.path
    parse_outcome = _read_and_parse_yaml(file_path, winner.path)
    if parse_outcome.errors or parse_outcome.parsed_data is None:
        return DefinitionValidationOutcome(
            definition=None,
            status=DefinitionResolutionStatus.LOAD_ERROR,
            errors=parse_outcome.errors,
        )

    parsed_data = parse_outcome.parsed_data
    schema_validator = getattr(definition_cls, "schema_validator", None)
    if schema_validator is not None and hasattr(schema_validator, "validate"):
        validation_result = schema_validator.validate(parsed_data)
        if hasattr(validation_result, "ok") and not validation_result.ok:
            validation_errors = list(getattr(validation_result, "errors", [str(validation_result)]))
            return DefinitionValidationOutcome(
                definition=None,
                status=DefinitionResolutionStatus.LOAD_ERROR,
                errors=validation_errors,
            )

    try:
        model_cls = cast(type[_PydanticModel], definition_cls)
        definition = model_cls.model_validate(parsed_data)
        return DefinitionValidationOutcome(
            definition=definition,
            status=DefinitionResolutionStatus.OK,
            errors=[],
        )
    except (Exception, DefinitionLoadError, DefinitionValidationError) as exc:
        return DefinitionValidationOutcome(
            definition=None,
            status=DefinitionResolutionStatus.LOAD_ERROR,
            errors=[f"Model validation failed for '{sha_or_name}': {exc}"],
        )


def get_catalog_item[T](
    sha_or_name: str,
    type_filter: CatalogItemType | str | None = None,
    *,
    definition_cls: type[T] | None = None,
    cwd: Path | None = None,
    repo: CatalogRepository | None = None,
) -> DefinitionResolutionResult[CatalogRecord]:
    """Retrieve catalog blueprint record by SHA or name, optionally validating its content into ``definition_cls``."""
    catalog_repo = repo or CatalogRepository(cwd)
    scan_and_index_catalog(cwd, repo=catalog_repo)
    matches = _find_catalog_matches(cwd, sha_or_name, type_filter, repo=catalog_repo)

    if not matches:
        return DefinitionResolutionResult(
            status=DefinitionResolutionStatus.NOT_FOUND,
            requested_name=sha_or_name,
            resolved=None,
            matches=[],
            errors=[f"Catalog blueprint '{sha_or_name}' not found."],
        )

    winner = matches[0]
    warnings: list[str] = []
    if len(matches) > 1:
        other_matching_paths = ", ".join(m.path.as_posix() for m in matches if m.path != winner.path)
        warnings.append(
            f"Duplicate catalog name '{sha_or_name}'; using '{winner.path.as_posix()}' (also found in: {other_matching_paths})."
        )

    definition: Any | None = None
    errors: list[str] = []
    status = DefinitionResolutionStatus.OK

    if definition_cls is not None:
        validation_outcome = _validate_definition(winner, definition_cls, cwd, sha_or_name)
        definition = validation_outcome.definition
        status = validation_outcome.status
        errors = validation_outcome.errors

    return DefinitionResolutionResult(
        status=status,
        requested_name=sha_or_name,
        resolved=winner,
        definition=definition,
        matches=matches,
        errors=errors,
        warnings=warnings,
    )


def delete_catalog_item_by_sha_or_name(
    sha_or_name: str,
    cwd: Path | None = None,
    repo: CatalogRepository | None = None,
) -> CatalogRecord | None:
    """Delete a catalog blueprint file and its database record."""
    catalog_repo = repo or CatalogRepository(cwd)
    result = get_catalog_item(sha_or_name, cwd=cwd, repo=catalog_repo)
    item = result.resolved
    if item is None:
        return None

    catalog_dir = get_catalog_dir(cwd)
    file_path = catalog_dir / item.path
    delete_file(file_path)

    catalog_repo.delete(item.sha)
    return item


def list_packaged_template_defaults() -> list[tuple[str, str]]:
    """Return (type, relative_path) pairs for the three packaged `default.yml` templates."""
    root = get_catalog_templates_dir()
    rows: list[tuple[str, str]] = []
    for item_type in (CatalogItemType.WORKFLOW, CatalogItemType.TASK, CatalogItemType.STEP):
        rel_path = f"{item_type.value}s/default.yml"
        if (root / rel_path).is_file():
            rows.append((item_type.value, rel_path))
    return rows


def find_packaged_templates(sha_or_name: str) -> list[tuple[str, str]]:
    """Return (relative_path, content) pairs for packaged templates matching `sha_or_name`."""
    root = get_catalog_templates_dir()
    found: list[tuple[str, str]] = []
    for type_dir in ("workflows", "tasks", "steps"):
        candidate = (
            (root / type_dir / "default.yml")
            if sha_or_name == "default"
            else (root / type_dir / "wt" / f"{sha_or_name}.yml")
        )
        if candidate.is_file():
            rel_path = f"{type_dir}/default.yml" if sha_or_name == "default" else f"{type_dir}/wt/{sha_or_name}.yml"
            found.append((rel_path, candidate.read_text(encoding="utf-8")))
    return found
