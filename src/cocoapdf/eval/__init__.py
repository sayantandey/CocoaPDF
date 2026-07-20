"""Deterministic evaluation helpers for CocoaPDF semantic output."""

from .tables import TableMetrics, evaluate_table, teds_score

__all__ = ["TableMetrics", "evaluate_table", "teds_score"]
