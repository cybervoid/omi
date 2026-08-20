#!/usr/bin/env python3
"""Deep health check for a self-hosted Omi backend deployment.

Run this **locally** (developer machine with `gcloud`/`firebase` auth and the full
repo checkout), not on the VM or inside the backend container: `firestore.indexes.json`
lives at the repo root, which the VM's sparse checkout and Docker image both exclude
(see the "Firestore index audit follow-up" plan for why).

    python -m scripts.check_selfhost_health \\
        --base-url https://35.223.15.33.sslip.io \\
        --project project-cda24f5f-2bb9-457d-b5a

Checks:
  1. `{base_url}/v1/health` and `/docs` respond 200 (basic reachability smoke).
  2. Every Firestore index/field-override declared in `firestore.indexes.json` is
     actually deployed and in state READY (catches "still building after a fresh
     deploy" and "declared but never deployed" drift — both hit production this
     session). Also reports (informationally) any deployed index not declared in
     the file.
  3. `{base_url}/metrics` (bearer-token gated) current values for a handful of
     known error/fallback counters, so an operator gets an at-a-glance signal
     instead of grepping `docker compose logs`.

Exit code is 0 only if every check is PASS; nonzero (1) on any FAIL. WARN does not
fail the run (it's an advisory signal, not a hard break) but is printed clearly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPTS_DIR.parent
REPO_ROOT = BACKEND_DIR.parent
FIRESTORE_INDEXES_PATH = REPO_ROOT / 'firestore.indexes.json'

STATUS_PASS = 'PASS'
STATUS_WARN = 'WARN'
STATUS_FAIL = 'FAIL'
STATUS_NOT_RUN = 'NOT_RUN'

# Known error/fallback counters worth a glance after a deploy. Nonzero => WARN
# (they may be stale from before a fix landed, not necessarily still happening —
# an operator should check timestamps in logs before treating this as urgent).
_WATCHED_COUNTERS: list[tuple[str, dict[str, str]]] = [
    ('async_supervisor_exit_total', {'reason': 'crash'}),
    ('listen_finalization_stale_processing_reconciliations_total', {'outcome': 'error'}),
    ('omi_fallback_total', {}),
]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    summary: str
    details: dict[str, Any]


def _http_get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 15.0) -> tuple[int, str]:
    request = urllib.request.Request(url, method='GET', headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(65536).decode('utf-8', errors='replace')
    except urllib.error.HTTPError as error:
        return error.code, error.read(4096).decode('utf-8', errors='replace')
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return 0, str(error)


def check_http_reachability(base_url: str, timeout: float) -> list[CheckResult]:
    results = []
    for path, expect_json_ok in (('/v1/health', True), ('/docs', False)):
        status_code, body = _http_get(f'{base_url.rstrip("/")}{path}', timeout=timeout)
        if status_code != 200:
            hint = (
                ' If this is a TLS/cert error on a network with an intercepting proxy '
                '(e.g. corporate/school), re-run this check from the VM itself over SSH '
                'before assuming the backend is actually down.'
                if status_code == 0
                else ''
            )
            results.append(
                CheckResult(
                    f'http:{path}',
                    STATUS_FAIL,
                    f'{path} returned HTTP {status_code or "unreachable"}.{hint}',
                    {'body': body[:500]},
                )
            )
            continue
        if expect_json_ok:
            try:
                ok = json.loads(body).get('status') == 'ok'
            except ValueError:
                ok = False
            if not ok:
                results.append(
                    CheckResult(
                        f'http:{path}', STATUS_FAIL, f'{path} returned 200 without status=ok.', {'body': body[:500]}
                    )
                )
                continue
        results.append(CheckResult(f'http:{path}', STATUS_PASS, f'{path} returned 200.', {}))
    return results


def _run_gcloud_json(args: list[str]) -> Any:
    completed = subprocess.run(
        ['gcloud', *args, '--format=json'],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f'gcloud {" ".join(args)} failed: {completed.stderr.strip()[:2000]}')
    return json.loads(completed.stdout or '[]')


def _composite_key(fields: list[dict[str, Any]]) -> tuple:
    return tuple((f.get('fieldPath'), f.get('order'), f.get('arrayConfig')) for f in fields)


def _collection_group_from_resource_name(name: str) -> str | None:
    """`gcloud ... --format=json` embeds collectionGroup in the resource `name`
    (`.../collectionGroups/<group>/indexes/<id>`), not as its own top-level field."""
    marker = '/collectionGroups/'
    if marker not in name:
        return None
    return name.split(marker, 1)[1].split('/', 1)[0]


def check_firestore_indexes(project: str) -> list[CheckResult]:
    if not FIRESTORE_INDEXES_PATH.exists():
        return [
            CheckResult(
                'firestore_indexes',
                STATUS_NOT_RUN,
                f'{FIRESTORE_INDEXES_PATH} not found — run this script from a full repo checkout, not the sparse VM clone.',
                {},
            )
        ]

    declared = json.loads(FIRESTORE_INDEXES_PATH.read_text(encoding='utf-8'))

    try:
        deployed_composites = _run_gcloud_json(
            ['firestore', 'indexes', 'composite', 'list', f'--project={project}', "--database=(default)"]
        )
    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as error:
        return [
            CheckResult('firestore_indexes', STATUS_FAIL, f'Could not list deployed composite indexes: {error}', {})
        ]

    results: list[CheckResult] = []

    # 1. Every declared composite index must be deployed and READY.
    deployed_by_key: dict[tuple, dict[str, Any]] = {}
    for entry in deployed_composites:
        fields = [f for f in entry.get('fields', []) if f.get('fieldPath') != '__name__']
        collection_group = _collection_group_from_resource_name(entry.get('name', ''))
        deployed_by_key[(collection_group, entry.get('queryScope'), _composite_key(fields))] = entry

    for declared_index in declared.get('indexes', []):
        fields = [f for f in declared_index.get('fields', []) if f.get('fieldPath') != '__name__']
        key = (declared_index.get('collectionGroup'), declared_index.get('queryScope'), _composite_key(fields))
        match = deployed_by_key.get(key)
        label = (
            f"{declared_index.get('collectionGroup')} {[f.get('fieldPath') for f in declared_index.get('fields', [])]}"
        )
        if match is None:
            results.append(
                CheckResult(
                    f'index:{label}',
                    STATUS_FAIL,
                    'Declared in firestore.indexes.json but not deployed at all.',
                    {'declared': declared_index},
                )
            )
        elif match.get('state') != 'READY':
            results.append(
                CheckResult(
                    f'index:{label}',
                    STATUS_WARN,
                    f"Deployed but state={match.get('state')} (still building?).",
                    {'deployed': match},
                )
            )
        else:
            results.append(CheckResult(f'index:{label}', STATUS_PASS, 'Deployed and READY.', {}))

    # 2. Field overrides (collection_group single-field indexes) — check via `fields describe`.
    for override in declared.get('fieldOverrides', []):
        collection_group = override.get('collectionGroup')
        field_path = override.get('fieldPath')
        label = f'{collection_group}.{field_path}'
        try:
            field_state = _run_gcloud_json(
                [
                    'firestore',
                    'indexes',
                    'fields',
                    'describe',
                    field_path,
                    f'--collection-group={collection_group}',
                    f'--project={project}',
                    '--database=(default)',
                ]
            )
        except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as error:
            results.append(
                CheckResult(f'field_override:{label}', STATUS_FAIL, f'Could not describe field: {error}', {})
            )
            continue
        wanted_scopes = {entry.get('queryScope') for entry in override.get('indexes', [])}
        deployed_indexes = (field_state.get('indexConfig') or {}).get('indexes', [])
        missing_scopes = set(wanted_scopes)
        not_ready: list[str] = []
        for deployed in deployed_indexes:
            scope = deployed.get('queryScope')
            if scope in missing_scopes:
                if deployed.get('state') == 'READY':
                    missing_scopes.discard(scope)
                else:
                    not_ready.append(f"{scope}={deployed.get('state')}")
        if missing_scopes:
            results.append(
                CheckResult(
                    f'field_override:{label}',
                    STATUS_FAIL,
                    f'Missing declared scope(s): {sorted(missing_scopes)}.',
                    {'deployed': deployed_indexes},
                )
            )
        elif not_ready:
            results.append(
                CheckResult(
                    f'field_override:{label}',
                    STATUS_WARN,
                    f'Not READY yet: {not_ready}.',
                    {'deployed': deployed_indexes},
                )
            )
        else:
            results.append(
                CheckResult(f'field_override:{label}', STATUS_PASS, 'All declared scopes deployed and READY.', {})
            )

    # 3. Informational: deployed composites not declared in the file at all (drift the
    # other direction — `firebase deploy` already warns about this interactively).
    declared_keys = {
        (
            d.get('collectionGroup'),
            d.get('queryScope'),
            _composite_key([f for f in d.get('fields', []) if f.get('fieldPath') != '__name__']),
        )
        for d in declared.get('indexes', [])
    }
    undeclared = [key for key in deployed_by_key if key not in declared_keys]
    if undeclared:
        results.append(
            CheckResult(
                'firestore_indexes:undeclared_drift',
                STATUS_WARN,
                f'{len(undeclared)} deployed composite index(es) are not declared in firestore.indexes.json.',
                {'undeclared': [{'collectionGroup': k[0], 'queryScope': k[1], 'fields': k[2]} for k in undeclared]},
            )
        )

    return results


_METRIC_LINE_RE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+0-9.eE]+)\s*$'
)
_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _parse_prometheus_text(text: str) -> list[tuple[str, dict[str, str], float]]:
    parsed: list[tuple[str, dict[str, str], float]] = []
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        match = _METRIC_LINE_RE.match(line)
        if not match:
            continue
        labels = dict(_LABEL_RE.findall(match.group('labels') or ''))
        try:
            value = float(match.group('value'))
        except ValueError:
            continue
        parsed.append((match.group('name'), labels, value))
    return parsed


def check_metrics(base_url: str, metrics_secret: str | None, timeout: float) -> list[CheckResult]:
    if not metrics_secret:
        return [
            CheckResult(
                'metrics',
                STATUS_NOT_RUN,
                'No METRICS_SECRET provided (--metrics-secret or $METRICS_SECRET) — skipping.',
                {},
            )
        ]
    status_code, body = _http_get(
        f'{base_url.rstrip("/")}/metrics', headers={'Authorization': f'Bearer {metrics_secret}'}, timeout=timeout
    )
    if status_code != 200:
        return [CheckResult('metrics', STATUS_FAIL, f'/metrics returned HTTP {status_code or "unreachable"}.', {})]

    samples = _parse_prometheus_text(body)
    results: list[CheckResult] = []
    for metric_name, wanted_labels in _WATCHED_COUNTERS:
        total = 0.0
        matched_any = False
        for name, labels, value in samples:
            if name != metric_name:
                continue
            matched_any = True
            if all(labels.get(k) == v for k, v in wanted_labels.items()):
                total += value
        label_desc = f'{metric_name}{wanted_labels or ""}'
        if not matched_any:
            results.append(
                CheckResult(
                    f'metric:{label_desc}',
                    STATUS_NOT_RUN,
                    'Metric series not present (idle process or not yet emitted).',
                    {},
                )
            )
        elif total > 0:
            results.append(
                CheckResult(
                    f'metric:{label_desc}',
                    STATUS_WARN,
                    f'Current value is {total:g} (nonzero since process start).',
                    {},
                )
            )
        else:
            results.append(CheckResult(f'metric:{label_desc}', STATUS_PASS, 'Current value is 0.', {}))
    return results


def build_report(base_url: str, project: str, metrics_secret: str | None, timeout: float) -> dict[str, Any]:
    checks: list[CheckResult] = []
    checks.extend(check_http_reachability(base_url, timeout))
    checks.extend(check_firestore_indexes(project))
    checks.extend(check_metrics(base_url, metrics_secret, timeout))

    counts = {
        status: sum(1 for c in checks if c.status == status)
        for status in (STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_NOT_RUN)
    }
    overall = STATUS_FAIL if counts[STATUS_FAIL] else (STATUS_WARN if counts[STATUS_WARN] else STATUS_PASS)
    return {
        'status': overall,
        'summary': counts,
        'checks': [{'name': c.name, 'status': c.status, 'summary': c.summary, 'details': c.details} for c in checks],
    }


def print_human_summary(report: dict[str, Any]) -> None:
    print(f"Self-host health check: {report['status']} {report['summary']}")
    for check in report['checks']:
        print(f"  [{check['status']:>7}] {check['name']}: {check['summary']}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base-url', default=os.environ.get('OMI_SELFHOST_BASE_URL'), required=False)
    parser.add_argument('--project', default=os.environ.get('GOOGLE_CLOUD_PROJECT'), required=False)
    parser.add_argument('--metrics-secret', default=os.environ.get('METRICS_SECRET'))
    parser.add_argument('--timeout-seconds', type=float, default=15.0)
    parser.add_argument('--json-only', action='store_true')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not args.base_url:
        print('ERROR: --base-url is required (or set OMI_SELFHOST_BASE_URL)', file=sys.stderr)
        return 1
    if not args.project:
        print('ERROR: --project is required (or set GOOGLE_CLOUD_PROJECT)', file=sys.stderr)
        return 1

    report = build_report(args.base_url, args.project, args.metrics_secret, args.timeout_seconds)
    if args.json_only:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human_summary(report)
    return 1 if report['status'] == STATUS_FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
