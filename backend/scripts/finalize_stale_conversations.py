"""Retired unsafe cron entry point. See docs/operations/stale-in-progress-recovery.md."""

import sys

if __name__ == '__main__':
    sys.exit('This unsafe finalizer is retired; use the opt-in durable in-progress recovery service.')
