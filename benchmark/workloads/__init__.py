"""Measured workloads and their per-dialect query text."""

from .base import Workload
from .queries import ALL_WORKLOADS, BY_NAME

__all__ = ["ALL_WORKLOADS", "BY_NAME", "Workload"]
