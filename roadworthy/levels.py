from __future__ import annotations
from enum import IntEnum


class Level(IntEnum):
    """Ordered permission levels. min() of two levels is always the more restrictive one."""
    BLOCKED = 0
    APPROVAL_REQUIRED = 1
    AUTO = 2
