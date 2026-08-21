"""An explicit ``null`` in a design, carried past an API server that prunes it.

AVD reads a key set to ``null`` differently from an absent one, and the
difference decides configuration. Kubernetes does not keep it -- measured:

    scalar: null                 ->  the key is gone
    list_items: [a, null, b]     ->  [a, null, b]           (kept)
    marker: avd.netclab.dev/null ->  avd.netclab.dev/null   (kept)

A null map value is pruned, a null list item is not, and there is nowhere to put
``nullable: true``: keys under an open design have no node in the schema. So the
value travels as a marker and becomes ``None`` again after layering, on both the
``spec.requires`` and the hand-written ``spec.design`` path.

⚠ The marker is API -- a reader of a published example sees it. ``"None"`` is one
capital from a real AVD value (``spanning_tree_mode: none``, 116 uses in the
corpus) and ``"null"`` is two quotes from the silently-pruned null this module
exists to remove; a qualified marker cannot be typed by accident.
"""

from __future__ import annotations

MARKER = "avd.netclab.dev/null"


def encoded(node):
    """A design with every explicitly-null map value replaced by the marker."""
    if isinstance(node, dict):
        return {k: MARKER if v is None else encoded(v) for k, v in node.items()}
    if isinstance(node, list):
        # List items survive intact, so they are left alone -- including a
        # genuine null item, which must stay a null item.
        return [None if v is None else encoded(v) for v in node]
    return node


def restored(node):
    """A design with every marker turned back into ``None``."""
    if isinstance(node, dict):
        return {k: None if v == MARKER else restored(v) for k, v in node.items()}
    if isinstance(node, list):
        return [None if v is None else restored(v) for v in node]
    return node


def carries(node) -> bool:
    """Whether the marker already appears as a value -- the one unsafe case."""
    if isinstance(node, dict):
        return any(v == MARKER or carries(v) for v in node.values())
    if isinstance(node, list):
        return any(v == MARKER or carries(v) for v in node)
    return False


def count(node) -> int:
    """How many map values are explicitly null -- what :func:`encoded` marks."""
    if isinstance(node, dict):
        return sum(1 if v is None else count(v) for v in node.values())
    if isinstance(node, list):
        return sum(count(v) for v in node if v is not None)
    return 0
