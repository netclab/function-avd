"""function-avd: a living AVD supermodel driven by Crossplane XRs.

Exposes the pyavd pipeline fed from AVD Ansible examples, which proves the
engine reproduces AVD's golden structured configs -- the same engine the
Crossplane composite function (`fn.py`) wraps.
"""

from .ansible_inputs import build_all_inputs
from .engine import render_structured_configs, validate_all

__all__ = ["build_all_inputs", "render_structured_configs", "validate_all"]
