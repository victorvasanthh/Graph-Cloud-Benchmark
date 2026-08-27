"""Database adapters, one per wire protocol rather than one per product."""

from .base import GraphAdapter, IngestPayload, IngestReport
from .registry import ADAPTERS, build_adapter

__all__ = [
    "ADAPTERS",
    "GraphAdapter",
    "IngestPayload",
    "IngestReport",
    "build_adapter",
]
