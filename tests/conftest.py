"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _strict_mode(monkeypatch):
    """Tests always run with MAGICWAND_STRICT=1 so soft-fail doesn't hide bugs."""
    monkeypatch.setenv("MAGICWAND_STRICT", "1")


@pytest.fixture
def clean_wdl_env(monkeypatch):
    """Strip WDL_* env vars so cwd-based detection isn't preempted."""
    for var in ("WDL_WORKFLOW_ID", "WDL_TASK_NAME", "WDL_CALL_FQN"):
        monkeypatch.delenv(var, raising=False)
