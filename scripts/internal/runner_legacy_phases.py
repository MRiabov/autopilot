#!/usr/bin/env python3
"""Compatibility re-export for legacy phase mixins."""

from __future__ import annotations

from .runner_legacy_pr_phases import LegacyPrPhasesMixin
from .runner_legacy_workflow_phases import LegacyWorkflowPhasesMixin

__all__ = ["LegacyPrPhasesMixin", "LegacyWorkflowPhasesMixin"]

