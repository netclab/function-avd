"""function-avd: a living AVD supermodel driven by Crossplane XRs.

Exposes the pyavd pipeline the Crossplane composite function (`fn.py`) wraps.
Nothing here reaches the migration harness: the runtime is handed input XRs and
never sees an inventory, so `ansible_cli` and `migrate` are imported by the
tools that need them and by nothing else.
"""

from .engine import render_structured_configs, validate_all

__all__ = ["render_structured_configs", "validate_all"]
