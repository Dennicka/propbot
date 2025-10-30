"""Compatibility layer that re-exports risk feature flags."""
from __future__ import annotations

from .core import FeatureFlags

__all__ = ["FeatureFlags"]
