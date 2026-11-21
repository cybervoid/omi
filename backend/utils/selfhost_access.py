"""Self-host single-owner access gate (fork-only).

When ``OMI_SELFHOST_ALLOWED_UIDS`` is set (comma-separated Firebase uids), every
Firebase ID token verified anywhere in the process must belong to one of those
uids. The gate wraps ``firebase_admin.auth.verify_id_token`` once at startup, so
all call sites (HTTP dependencies, WebSocket auth, OAuth/MCP/support routes)
are covered without editing upstream routers, and new upstream call sites are
covered automatically as long as they use the module attribute.

A token for any other uid raises ``InvalidIdTokenError``, which every caller
already maps to an authentication failure. ``ADMIN_KEY`` impersonation bypasses
Firebase entirely, so it is force-disabled while the gate is active.

Unset or empty keeps upstream behaviour unchanged.
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Any, Callable, FrozenSet

logger = logging.getLogger(__name__)

ENV_VAR = 'OMI_SELFHOST_ALLOWED_UIDS'
_WRAPPED_MARKER = '_omi_selfhost_allowlist'


def allowed_uids(env: dict[str, str] | None = None) -> FrozenSet[str]:
    raw = (os.environ if env is None else env).get(ENV_VAR, '')
    return frozenset(part.strip() for part in raw.split(',') if part.strip())


def _gate(verify: Callable[..., Any], allowed: FrozenSet[str], error_type: type[Exception]) -> Callable[..., Any]:
    @functools.wraps(verify)
    def verify_id_token(*args: Any, **kwargs: Any) -> Any:
        claims = verify(*args, **kwargs)
        uid = claims.get('uid') if isinstance(claims, dict) else None
        if uid not in allowed:
            logger.warning('event=selfhost_access outcome=denied reason=uid_not_allowed')
            raise error_type('This self-hosted Omi server accepts only its owner account.')
        return claims

    setattr(verify_id_token, _WRAPPED_MARKER, True)
    return verify_id_token


def install(firebase_auth_module: Any | None = None) -> bool:
    """Install the gate; returns True when active. Idempotent."""
    allowed = allowed_uids()
    if not allowed:
        return False
    if firebase_auth_module is None:
        from firebase_admin import auth as firebase_auth_module  # noqa: PLC0415

    current = firebase_auth_module.verify_id_token
    if not getattr(current, _WRAPPED_MARKER, False):
        firebase_auth_module.verify_id_token = _gate(current, allowed, firebase_auth_module.InvalidIdTokenError)
    if os.environ.get('ADMIN_KEY') and os.environ.get('ADMIN_KEY_AUTH_ENABLED', 'true').lower() != 'false':
        logger.warning('event=selfhost_access ADMIN_KEY impersonation disabled because the owner allowlist is active')
    os.environ['ADMIN_KEY_AUTH_ENABLED'] = 'false'
    logger.info('event=selfhost_access outcome=active allowed_count=%d', len(allowed))
    return True
