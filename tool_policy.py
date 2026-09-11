"""Central, LLM-independent policy primitives for Orion tools.

The policy layer deliberately does not know how approvals are stored or how a
tool is executed.  Callers provide the current enablement/approval state and
receive a deterministic decision that can be enforced by the runtime,
sub-agents, CLIs, or tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ToolPolicyError(ValueError):
    """A tool policy declaration or configuration is invalid."""


class ToolClassification(str, Enum):
    READ_ONLY = "read_only"
    SIDE_EFFECT = "side_effect"
    PRIVILEGED = "privileged"


_CLASSIFICATION_RANK = {
    ToolClassification.READ_ONLY: 0,
    ToolClassification.SIDE_EFFECT: 1,
    ToolClassification.PRIVILEGED: 2,
}


def _classification(value: ToolClassification | str) -> ToolClassification:
    if isinstance(value, ToolClassification):
        return value
    try:
        return ToolClassification(str(value).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ToolClassification)
        raise ToolPolicyError(f"Unknown tool classification {value!r}; expected one of: {allowed}") from exc


@dataclass(frozen=True)
class ToolPolicyRule:
    """Effective classification for one callable tool name."""

    tool_id: str
    classification: ToolClassification
    source: str = "policy"

    @property
    def approval_required(self) -> bool:
        return self.classification is ToolClassification.PRIVILEGED

    @property
    def has_side_effects(self) -> bool:
        return self.classification is not ToolClassification.READ_ONLY


@dataclass(frozen=True)
class ToolPolicyDecision:
    """Pure authorization result for a prospective tool invocation."""

    tool_id: str
    classification: ToolClassification
    enabled: bool
    approved: bool
    approval_required: bool
    allowed: bool
    reason: str | None = None


class ToolPolicyDenied(PermissionError):
    """Raised by :meth:`ToolPolicy.require` for a denied decision."""

    def __init__(self, decision: ToolPolicyDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason or f"Tool denied by policy: {decision.tool_id}")


@dataclass(frozen=True)
class ToolPackagePolicy:
    """Policy metadata declared by one installed tool package."""

    classification: ToolClassification = ToolClassification.SIDE_EFFECT
    explicit_enable: bool = False
    tool_classifications: tuple[tuple[str, ToolClassification], ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ToolPackagePolicy":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ToolPolicyError("policy must be a TOML object")
        classification = _classification(value.get("classification", ToolClassification.SIDE_EFFECT))
        explicit_enable = value.get("explicit_enable", False)
        if not isinstance(explicit_enable, bool):
            raise ToolPolicyError("policy.explicit_enable must be a boolean")
        raw_tools = value.get("tool_classifications", {})
        if not isinstance(raw_tools, Mapping):
            raise ToolPolicyError("policy.tool_classifications must be a TOML object")
        tools: list[tuple[str, ToolClassification]] = []
        for raw_name, raw_classification in raw_tools.items():
            name = str(raw_name).strip()
            if not name:
                raise ToolPolicyError("policy.tool_classifications contains an empty tool name")
            tools.append((name, _classification(raw_classification)))
        if classification is ToolClassification.PRIVILEGED and not tools:
            raise ToolPolicyError(
                "A privileged package must declare policy.tool_classifications so calls cannot bypass approval"
            )
        return cls(
            classification=classification,
            explicit_enable=explicit_enable,
            tool_classifications=tuple(tools),
        )

    @property
    def requires_explicit_enable(self) -> bool:
        # Privileged code is never auto-loaded merely because tools.enabled is
        # empty.  A manifest may opt other sensitive packages into the same
        # behavior with explicit_enable=true.
        return (
            self.explicit_enable
            or self.classification is ToolClassification.PRIVILEGED
            or any(
                classification is ToolClassification.PRIVILEGED
                for _, classification in self.tool_classifications
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "explicit_enable": self.explicit_enable,
            "tool_classifications": {
                name: classification.value for name, classification in self.tool_classifications
            },
        }


class ToolPolicy:
    """Deterministic capability policy keyed by callable tool name."""

    def __init__(
        self,
        rules: Mapping[str, ToolClassification | str] | None = None,
        *,
        approvals_enabled: bool = True,
    ) -> None:
        self._rules: dict[str, ToolPolicyRule] = {}
        self.approvals_enabled = bool(approvals_enabled)
        for tool_id, classification in (rules or {}).items():
            self.register(tool_id, classification)

    @classmethod
    def from_config(cls, value: Mapping[str, Any] | None) -> "ToolPolicy":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ToolPolicyError("tools.policy must be a TOML object")
        policy = cls()
        declared: dict[str, ToolClassification] = {}
        for classification in ToolClassification:
            raw_values = value.get(classification.value, [])
            if not isinstance(raw_values, list):
                raise ToolPolicyError(f"tools.policy.{classification.value} must be a list")
            for raw_tool_id in raw_values:
                tool_id = str(raw_tool_id).strip()
                if not tool_id:
                    raise ToolPolicyError(f"tools.policy.{classification.value} contains an empty tool name")
                previous = declared.get(tool_id)
                if previous is not None and previous is not classification:
                    raise ToolPolicyError(
                        f"Tool {tool_id!r} appears in both {previous.value} and {classification.value} policy lists"
                    )
                declared[tool_id] = classification
                policy.register(tool_id, classification, source="operator")
        unknown = set(value) - {item.value for item in ToolClassification}
        if unknown:
            raise ToolPolicyError(f"Unknown tools.policy keys: {', '.join(sorted(str(item) for item in unknown))}")
        return policy

    def copy(self) -> "ToolPolicy":
        copied = ToolPolicy(approvals_enabled=self.approvals_enabled)
        copied._rules = dict(self._rules)
        return copied

    def set_approvals_enabled(self, enabled: bool) -> None:
        """Toggle per-call human approval without changing tool classifications."""
        self.approvals_enabled = bool(enabled)

    def register(
        self,
        tool_id: str,
        classification: ToolClassification | str,
        *,
        source: str = "policy",
    ) -> ToolPolicyRule:
        name = str(tool_id).strip()
        if not name:
            raise ToolPolicyError("Tool name cannot be empty")
        candidate = _classification(classification)
        existing = self._rules.get(name)
        if existing is not None and _CLASSIFICATION_RANK[existing.classification] >= _CLASSIFICATION_RANK[candidate]:
            return existing
        rule = ToolPolicyRule(name, candidate, str(source or "policy"))
        self._rules[name] = rule
        return rule

    def register_package(self, package_id: str, package_policy: ToolPackagePolicy) -> None:
        self.register(package_id, package_policy.classification, source=f"manifest:{package_id}")
        for tool_id, classification in package_policy.tool_classifications:
            effective = (
                package_policy.classification
                if _CLASSIFICATION_RANK[package_policy.classification]
                >= _CLASSIFICATION_RANK[classification]
                else classification
            )
            self.register(tool_id, effective, source=f"manifest:{package_id}")

    def rule_for(self, tool_id: str) -> ToolPolicyRule:
        name = str(tool_id).strip()
        if not name:
            raise ToolPolicyError("Tool name cannot be empty")
        return self._rules.get(name, ToolPolicyRule(name, ToolClassification.SIDE_EFFECT, "default"))

    def decide(self, tool_id: str, *, enabled: bool = True, approved: bool = False) -> ToolPolicyDecision:
        rule = self.rule_for(tool_id)
        approval_required = rule.approval_required and self.approvals_enabled
        reason: str | None = None
        if not enabled:
            reason = f"Tool disabled by operator policy: {rule.tool_id}"
        elif approval_required and not approved:
            reason = f"Privileged tool requires explicit approval: {rule.tool_id}"
        return ToolPolicyDecision(
            tool_id=rule.tool_id,
            classification=rule.classification,
            enabled=bool(enabled),
            approved=bool(approved),
            approval_required=approval_required,
            allowed=reason is None,
            reason=reason,
        )

    def require(self, tool_id: str, *, enabled: bool = True, approved: bool = False) -> ToolPolicyDecision:
        decision = self.decide(tool_id, enabled=enabled, approved=approved)
        if not decision.allowed:
            raise ToolPolicyDenied(decision)
        return decision

    def rules(self) -> dict[str, ToolPolicyRule]:
        return dict(self._rules)


__all__ = [
    "ToolClassification",
    "ToolPackagePolicy",
    "ToolPolicy",
    "ToolPolicyDecision",
    "ToolPolicyDenied",
    "ToolPolicyError",
    "ToolPolicyRule",
]
