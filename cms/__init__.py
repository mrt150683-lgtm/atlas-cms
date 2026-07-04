"""Codebase Memory System — structural + semantic memory layer for codebases."""

from .memory import CodebaseMemory, QueryResult
from .scanner import FileRecord, scan

__all__ = ["CodebaseMemory", "QueryResult", "FileRecord", "scan"]
__version__ = "0.1.0"
