"""The Dictionary of full and partial functions. User partials import from here::

    from jeeves.functions import partial, Arg, FunctionError
"""
from .base import Arg, Cancelled, FunctionDef, FunctionError, full, partial

__all__ = ["Arg", "Cancelled", "FunctionDef", "FunctionError", "full", "partial"]
