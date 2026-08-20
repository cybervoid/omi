"""Structural guard: every `collection_group()` filter must have a declared index.

This is not a behavioral test — it never touches a real Firestore. It statically
scans the backend source for `db.collection_group("...")` call sites, resolves the
field names filtered via `.where(...)` on that same query object, and asserts each
`(collection_group, field)` pair has either:
  * a `fieldOverrides` entry (COLLECTION_GROUP scope) for that field, or
  * a composite `indexes[]` entry with `queryScope: COLLECTION_GROUP` that includes
    that field.

Why this exists: three separate production incidents (see the selfhost fork's Aug
2026 session) were all `collection_group()` queries with no matching index — Firestore
only auto-creates single-field indexes for COLLECTION-scoped queries, never for
COLLECTION_GROUP scope, so any filtered collection_group() query is broken by default
until an index is explicitly declared and deployed. This guard catches the *next* one
at test time instead of in production logs.

Known limitation: this is a best-effort static resolver, not a real interpreter. A
call site it cannot statically resolve (e.g. the field name is computed rather than a
string literal) fails loudly with instructions to add it to `_UNRESOLVED_ALLOWLIST`
below after manual review, rather than silently passing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Iterator, NamedTuple

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
FIRESTORE_INDEXES_PATH = REPO_ROOT / 'firestore.indexes.json'

# Directories under backend/ that are never shipped runtime code (tests, harnesses,
# fakes, k8s charts) — scanning them would just find test fixtures and other guard
# tests, not real query call sites.
_EXCLUDED_DIR_NAMES = {'tests', 'testing', 'charts', '.venv', '__pycache__'}

# (collection_group, field) pairs a human has reviewed and confirmed either need no
# index (e.g. the only usage is order_by('__name__') with no filter) or are covered
# by an index shape this resolver cannot detect. Add here only after checking
# firestore.indexes.json / gcloud firestore indexes composite list by hand.
_UNRESOLVED_ALLOWLIST: set[tuple[str | None, str | None]] = {
    # database/conversation_finalization_jobs.py: collection_group(CONVERSATIONS_COLLECTION)
    # — a module constant, not a string literal, so the group name can't be resolved
    # statically. Manually verified: CONVERSATIONS_COLLECTION == 'conversations', which
    # has a `status` fieldOverride with COLLECTION_GROUP scope in firestore.indexes.json.
    (None, 'status'),
}


class CollectionGroupFilter(NamedTuple):
    collection_group: str | None  # None means "could not statically resolve the collection_group name"
    field: str | None  # None means "could not statically resolve the filtered field"
    file: Path
    lineno: int


# Sentinel: this expression is definitely a `.collection_group(...)`-derived chain, but
# its argument was not a string literal (e.g. a module-level constant), so the group
# name could not be statically resolved. Distinct from `None`, which means the `.where()`
# receiver was not a collection_group() chain at all and is out of scope for this guard.
_UNRESOLVABLE_GROUP = object()


def _iter_backend_python_files() -> Iterator[Path]:
    for path in BACKEND_DIR.rglob('*.py'):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.relative_to(BACKEND_DIR).parts[:-1]):
            continue
        yield path


def _string_constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _resolve_collection_group(node: ast.AST, var_to_group: dict[str, object]):
    """Best-effort trace of an expression back to the `collection_group("x")` call it derives from.

    Returns a resolved group name (str), `_UNRESOLVABLE_GROUP` (definitely a
    collection_group() chain, but the argument wasn't a string literal), or `None`
    (not a collection_group()-derived chain at all).
    """
    if isinstance(node, ast.Name):
        return var_to_group.get(node.id)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == 'collection_group' and node.args:
            literal = _string_constant(node.args[0])
            return literal if literal is not None else _UNRESOLVABLE_GROUP
        # Chained call (e.g. `....where(...).where(...)`) — keep unwrapping the receiver.
        return _resolve_collection_group(node.func.value, var_to_group)
    return None


def _where_field(call: ast.Call) -> str | None:
    """Extract the filtered field name from a `.where(...)` call, in either calling form."""
    if call.args:
        return _string_constant(call.args[0])
    for kw in call.keywords:
        if kw.arg == 'filter' and isinstance(kw.value, ast.Call):
            filter_call = kw.value
            if filter_call.args:
                return _string_constant(filter_call.args[0])
    return None


def _scan_function(func: ast.AST, file: Path) -> list[CollectionGroupFilter]:
    var_to_group: dict[str, object] = {}
    # Fixed-point pass over assignments so `query = query.where(...)` (reassigning the
    # same name) resolves regardless of source order within the function body.
    for _ in range(4):
        changed = False
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                resolved = _resolve_collection_group(node.value, var_to_group)
                if resolved is not None and var_to_group.get(name) != resolved:
                    var_to_group[name] = resolved
                    changed = True
        if not changed:
            break

    findings: list[CollectionGroupFilter] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'where':
            group = _resolve_collection_group(node.func.value, var_to_group)
            if group is None:
                continue
            field = _where_field(node)
            resolved_group = None if group is _UNRESOLVABLE_GROUP else group
            findings.append(
                CollectionGroupFilter(collection_group=resolved_group, field=field, file=file, lineno=node.lineno)
            )
    return findings


def _scan_file(file: Path) -> list[CollectionGroupFilter]:
    try:
        tree = ast.parse(file.read_text(encoding='utf-8'), filename=str(file))
    except SyntaxError:
        return []
    findings: list[CollectionGroupFilter] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            findings.extend(_scan_function(node, file))
    return findings


def _load_firestore_indexes() -> dict:
    return json.loads(FIRESTORE_INDEXES_PATH.read_text(encoding='utf-8'))


def _collection_group_field_is_covered(indexes_doc: dict, collection_group: str, field: str) -> bool:
    for override in indexes_doc.get('fieldOverrides', []):
        if override.get('collectionGroup') != collection_group or override.get('fieldPath') != field:
            continue
        if any(entry.get('queryScope') == 'COLLECTION_GROUP' for entry in override.get('indexes', [])):
            return True
    for composite in indexes_doc.get('indexes', []):
        if composite.get('collectionGroup') != collection_group or composite.get('queryScope') != 'COLLECTION_GROUP':
            continue
        if any(f.get('fieldPath') == field for f in composite.get('fields', [])):
            return True
    return False


def test_every_collection_group_filter_has_a_declared_index():
    if not FIRESTORE_INDEXES_PATH.exists():
        # A backend-only sparse checkout (e.g. the self-hosted VM) never has the
        # repo-root indexes file; nothing to compare against, so skip rather than fail.
        import pytest

        pytest.skip(f'{FIRESTORE_INDEXES_PATH} not present in this checkout')

    indexes_doc = _load_firestore_indexes()

    all_findings: list[CollectionGroupFilter] = []
    for file in _iter_backend_python_files():
        all_findings.extend(_scan_file(file))

    unresolved: list[CollectionGroupFilter] = []
    uncovered: list[CollectionGroupFilter] = []
    for finding in all_findings:
        key = (finding.collection_group, finding.field)
        if key in _UNRESOLVED_ALLOWLIST:
            continue
        if finding.collection_group is None or finding.field is None:
            unresolved.append(finding)
            continue
        if not _collection_group_field_is_covered(indexes_doc, finding.collection_group, finding.field):
            uncovered.append(finding)

    problems = unresolved + uncovered
    if not problems:
        return

    lines = []
    for finding in unresolved:
        lines.append(
            f'{finding.file.relative_to(REPO_ROOT)}:{finding.lineno}: could not statically resolve the '
            f'collection_group name and/or filtered field (group={finding.collection_group!r}, '
            f'field={finding.field!r}) — review by hand and add (group, field) to _UNRESOLVED_ALLOWLIST '
            f'if it is genuinely fine.'
        )
    for finding in uncovered:
        lines.append(
            f'{finding.file.relative_to(REPO_ROOT)}:{finding.lineno}: collection_group("{finding.collection_group}")'
            f'.where({finding.field!r}, ...) has no COLLECTION_GROUP-scope index/fieldOverride declared in '
            f'firestore.indexes.json — this query will fail with FailedPrecondition in production. Add a '
            f'fieldOverrides (or composite indexes[]) entry with queryScope COLLECTION_GROUP for '
            f'collectionGroup={finding.collection_group!r} fieldPath={finding.field!r}, then deploy it with '
            f'`firebase deploy --only firestore:indexes` before merging.'
        )
    raise AssertionError('Undeclared collection_group() index(es):\n' + '\n'.join(lines))
