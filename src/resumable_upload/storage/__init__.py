"""Storage backends. Phase 1 ships the local filesystem only."""

from .base import FinalizeResult, Storage
from .local import LocalStorage

__all__ = ["Storage", "FinalizeResult", "LocalStorage"]
