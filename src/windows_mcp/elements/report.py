"""Action reports, delivery dispositions and structured element errors.

Every action tells the caller two things beyond "ok": whether the input actually
reached the application (*delivery*) and therefore whether repeating the call is safe
(*retry*). Only ``not_delivered`` is safe to retry blindly; anything that may have
landed must be checked (Snapshot, ElementAction read) before it is repeated, because a
repeated click can submit twice and a repeated toggle undoes itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Delivery = Literal[
    "not_delivered",
    "delivery_uncertain",
    "delivered_unverified",
    "delivered_verified",
    "unknown",
]

Mechanism = Literal["semantic_api", "physical_synthetic"]


def retry_for(delivery: Delivery) -> str:
    """Retry safety implied by *delivery*."""
    if delivery == "not_delivered":
        return "safe"
    if delivery == "unknown":
        return "unknown"
    return "unsafe"


def disposition_text(delivery: Delivery) -> str:
    return f"[{delivery}, retry: {retry_for(delivery)}]"


@dataclass
class ActionStep:
    label: str
    outcome: Literal["succeeded", "failed", "skipped"]
    mechanism: Mechanism = "semantic_api"
    detail: str | None = None

    def to_text(self) -> str:
        text = f"{self.label}={self.outcome}"
        if self.detail:
            text += f" ({self.detail})"
        return text


@dataclass
class ActionReport:
    """Outcome of one element action."""

    action: str
    target: str
    delivery: Delivery
    steps: list[ActionStep] = field(default_factory=list)
    post_state: dict[str, Any] = field(default_factory=dict)
    verification: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def retry(self) -> str:
        return retry_for(self.delivery)

    def to_text(self) -> str:
        lines = [f"{self.action} on {self.target}: {self.delivery} (retry: {self.retry})"]
        if self.steps:
            lines.append("steps: " + "; ".join(step.to_text() for step in self.steps))
        if self.post_state:
            state = ", ".join(f"{key}={value!r}" for key, value in self.post_state.items())
            lines.append(f"post_state: {state}")
        if self.verification:
            lines.append(f"verification: {self.verification}")
        lines.extend(self.notes)
        return "\n".join(lines)


ErrorCode = Literal[
    "STALE_REF",
    "SNAPSHOT_MISMATCH",
    "ELEMENT_NOT_FOUND",
    "ACTION_NOT_SUPPORTED",
    "ACTION_FAILED",
    "NOT_ACTIONABLE",
    "PERM_DENIED",
    "POLICY_DENIED",
    "INVALID_ARGS",
    "TIMEOUT",
]

_DEFAULT_HINTS: dict[str, str] = {
    "STALE_REF": "Take a new Snapshot (or Find) and use the new label.",
    "SNAPSHOT_MISMATCH": "Use a label from the latest Snapshot, or omit snapshot_id.",
    "ELEMENT_NOT_FOUND": "Check the label against the latest Snapshot.",
    "ACTION_NOT_SUPPORTED": "Use another action or a physical Click/Type instead.",
    "NOT_ACTIONABLE": "Wait for the UI to settle, close what covers it, or raise wait_timeout.",
}


class ElementError(ValueError):
    """Structured failure of an element lookup or action.

    It subclasses ValueError so the MCP layer reports it as a tool error, and its message
    carries the code, the delivery disposition and a recovery hint as plain text.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        delivery: Delivery = "not_delivered",
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.delivery = delivery
        self.hint = hint if hint is not None else _DEFAULT_HINTS.get(code)
        self.details = details or {}
        text = f"{code}: {message} {disposition_text(delivery)}"
        if self.hint:
            text += f" Hint: {self.hint}"
        super().__init__(text)

    @property
    def retry(self) -> str:
        return retry_for(self.delivery)
