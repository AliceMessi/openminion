"""Tools defined in a real module with postponed annotations (PEP 563)."""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field

from openminion.tools import tool


class Window(Enum):
    DAY = "day"
    WEEK = "week"


class Range(BaseModel):
    start: int
    end: int


@tool
def lookback(
    days: Annotated[int, Field(description="Look-back days", ge=1)] = 2,
) -> int:
    return days


@tool
def summarize(window: Window, span: Range, label, limit: int = 5) -> str:
    return f"{window.value}:{span.start}-{span.end}:{label}:{limit}"
