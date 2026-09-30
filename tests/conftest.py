"""Fixtures shared across test modules.

`test_app_service` owns the fake GitHub, the service fixture and the webhook
helpers. Re-exporting them here is what lets another module use them without
importing names that pytest immediately rebinds as parameters — which reads to
a linter as a redefinition, and to a reader as a puzzle.
"""

from __future__ import annotations

from tests.test_app_service import (
    client,
    github,
    no_local_context,
    review_result,
    settings,
)

__all__ = ["client", "github", "no_local_context", "review_result", "settings"]
