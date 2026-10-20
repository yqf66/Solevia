"""Small, provider-independent wire contracts and shared research deadlines."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic


_deadline: ContextVar[float | None] = ContextVar("research_model_deadline", default=None)
MODEL_FINISH_GRACE_SECONDS = 2.0


@contextmanager
def model_deadline(deadline: float):
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_model_timeout(requested: float, *, deadline: float) -> float:
    outer = _deadline.get()
    remaining = min(deadline, outer if outer is not None else deadline) - monotonic()
    # Socket timeout is not a wall-clock deadline. Leave room for parsing and
    # the outer watchdog; repairs share the same absolute deadline.
    reserve = min(0.25, requested * 0.05)
    if remaining <= reserve:
        raise TimeoutError("Research model deadline exhausted")
    return min(requested, remaining - reserve)


def compact_model_schema(schema: dict) -> dict:
    """Remove annotations only; preserve fields, refs, enums and all limits."""
    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        return {
            key: ({name: visit(child) for name, child in item.items()}
                  if key in {"properties", "$defs", "definitions"} else visit(item))
            for key, item in value.items()
            if key not in {"title", "default", "examples"}
        }
    return visit(schema)

