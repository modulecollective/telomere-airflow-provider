"""Telomere provider utilities."""

from typing import Any

__all__ = [
    "enable_telomere_tracking",
]


def __getattr__(name: str) -> Any:
    # Lazy re-export: dag_tracker imports the operators, which import
    # telomere_provider.utils.urls — an eager import here would be circular.
    if name == "enable_telomere_tracking":
        from telomere_provider.utils.dag_tracker import enable_telomere_tracking

        return enable_telomere_tracking
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
