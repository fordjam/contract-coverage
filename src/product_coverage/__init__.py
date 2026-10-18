"""Vendor-independent, offline minimum product coverage calculations.

Importing this package does not import a vendor SDK or open any files.
"""

from importlib import import_module

__all__ = ["AnalysisResult", "analyze", "evaluate", "normalize"]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    return getattr(import_module(".schema" if name == "normalize" else ".engine", __name__), name)
