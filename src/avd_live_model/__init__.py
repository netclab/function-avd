"""avd-live-model: a living AVD supermodel driven by Crossplane XRs.

Milestone 1 exposes the pyavd pipeline fed from AVD Ansible examples, so we can
prove the engine reproduces AVD's golden structured configs before wrapping it
in a Crossplane Python composite function.
"""

from .ansible_inputs import build_all_inputs
from .engine import render_structured_configs, validate_all

__all__ = ["build_all_inputs", "render_structured_configs", "validate_all"]
