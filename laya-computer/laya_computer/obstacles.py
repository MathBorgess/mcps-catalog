"""Obstacle detection: code owns the policy, Laya only ranks already-allowed buttons.

An *obstacle* is transient UI that blocks the plan (a sheet, popover or alert that the
plan did not ask for). The controller only looks for one on a failure path it cannot
otherwise explain, and it may only press a button whose label is on an explicit
allow-list and contains no destructive or permission-granting word. Anything else is a
rescue. Success is never judged here: the step's predicates still decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Roles that can hold a blocking prompt. Cua's window elements expose ``role`` but not
# ``subrole``, so dialog windows that only differ by subrole are outside this V1.
OBSTACLE_ROLES = frozenset({"AXSheet", "AXPopover", "AXDialog", "AXSystemDialog", "AXAlert"})
BUTTON_ROLES = frozenset({"AXButton", "AXPopUpButton"})

# Exact (casefolded) labels that only dismiss a notice without changing anything.
DEFAULT_DISMISS_LABELS = frozenset({
    "not now", "later", "remind me later", "maybe later", "no thanks", "skip", "dismiss", "close",
})

# A label containing any of these is never pressed automatically, even if allow-listed.
NEVER_SUBSTRINGS = (
    "delete", "remove", "erase", "replace", "overwrite", "discard", "trash", "reset", "sign in", "log in",
    "sign out", "install", "update", "restart", "buy", "purchase", "subscribe", "allow", "grant", "turn on",
    "enable", "agree", "accept", "confirm", "share", "send", "upload", "publish", "don’t allow", "don't allow",
)

MAX_BUTTONS = 6


def label_key(label: Any) -> str:
    return " ".join(str(label or "").split()).casefold()


def is_destructive(label: Any) -> bool:
    key = label_key(label)
    return any(word in key for word in NEVER_SUBSTRINGS)


@dataclass
class Obstacle:
    container: dict[str, Any]
    allowed: list[dict[str, Any]] = field(default_factory=list)
    other_labels: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "container": f"{self.container.get('role')} {str(self.container.get('label') or '')[:80]}".strip(),
            "allowed_buttons": [str(b.get("label", ""))[:60] for b in self.allowed],
            "other_buttons": [label[:60] for label in self.other_labels[:MAX_BUTTONS]],
        }


def _descends_from(element: dict[str, Any], container: dict[str, Any], by_index: dict[Any, dict[str, Any]]) -> bool:
    target = container.get("element_index")
    if target is None:
        return False
    cursor, seen = element, set()
    while cursor is not None:
        parent = cursor.get("parent_index")
        if parent is None or parent in seen:
            return False
        if parent == target:
            return True
        seen.add(parent)
        cursor = by_index.get(parent)
    return False


def find_obstacle(snapshot: dict[str, Any], extra_labels: frozenset[str] | set[str] = frozenset()) -> Obstacle | None:
    """Return the first blocking container in the snapshot, or ``None``.

    Buttons are matched only when they descend from the container through the observed
    ``parent_index`` chain. ``extra_labels`` come from the plan and are already validated
    not to be destructive.
    """
    elements = [e for e in snapshot.get("elements", []) if isinstance(e, dict)]
    by_index = {e["element_index"]: e for e in elements if e.get("element_index") is not None}
    allow = DEFAULT_DISMISS_LABELS | {label_key(label) for label in extra_labels}
    for container in elements:
        if container.get("role") not in OBSTACLE_ROLES:
            continue
        obstacle = Obstacle(container)
        for element in elements:
            if element.get("role") not in BUTTON_ROLES or element.get("enabled") is False:
                continue
            if not _descends_from(element, container, by_index):
                continue
            label = label_key(element.get("label"))
            if label in allow and not is_destructive(label):
                obstacle.allowed.append(element)
            elif label:
                obstacle.other_labels.append(str(element.get("label")))
        return obstacle
    return None
