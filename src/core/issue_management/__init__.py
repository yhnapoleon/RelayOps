"""
Issue Management — combines issue creation engine and anomaly-to-issue assignment.

Provides:
- issue_engine: Automatic Issue creation, SLA calculation, and auto-dispatch
- assign: AssignModule facade for converting AnomalyEvents into Issues
"""

from core.issue_management.assign import AssignModule

__all__ = ["AssignModule"]
