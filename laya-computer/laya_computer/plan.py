"""Validated, deliberately small plans for local desktop control."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .obstacles import is_destructive


class Action(StrEnum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    SET_VALUE = "set_value"
    PRESS_KEY = "press_key"
    SCROLL = "scroll"
    WAIT = "wait"
    VERIFY = "verify"


class PredicateKind(StrEnum):
    EXISTS = "exists"
    VALUE_CONTAINS = "value_contains"
    SELECTED = "selected"
    WINDOW_TITLE_CONTAINS = "window_title_contains"


class Selector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    label_contains: str | None = None
    description_contains: str | None = None
    index: int | str | None = None

    @model_validator(mode="after")
    def selector_is_bounded(self) -> Self:
        if not any((self.role, self.label_contains, self.description_contains)):
            raise ValueError("selector needs a role, label_contains, or description_contains")
        if isinstance(self.index, int) and self.index < 0:
            raise ValueError("selector index must be non-negative")
        if isinstance(self.index, str) and self.index not in {"first", "last"}:
            raise ValueError("selector index must be an integer, 'first', or 'last'")
        return self


class Predicate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: PredicateKind
    role: str | None = None
    label_contains: str | None = None
    value: str | None = None

    @model_validator(mode="after")
    def fields_match_kind(self) -> Self:
        if self.kind in {PredicateKind.EXISTS, PredicateKind.VALUE_CONTAINS, PredicateKind.SELECTED} and not (self.role or self.label_contains):
            raise ValueError(f"{self.kind.value} predicate needs a role or label_contains")
        if self.kind == PredicateKind.VALUE_CONTAINS and self.value is None:
            raise ValueError("value_contains predicate needs value")
        if self.kind == PredicateKind.WINDOW_TITLE_CONTAINS and not self.value:
            raise ValueError("window_title_contains predicate needs value")
        return self


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    instruction: str = Field(min_length=1, max_length=1000)
    action: Action
    target: Selector | None = None
    text: str | None = None
    key: str | None = None
    direction: str | None = None
    duration_seconds: float | None = Field(default=None, gt=0, le=10)
    success: list[Predicate] = Field(min_length=1, max_length=12)
    preconditions: list[Predicate] = Field(default_factory=list, max_length=12)
    next_step: str | None = None
    on_failure: str | None = None
    window_title: str | None = Field(default=None, max_length=200)
    allow_partial_observation: bool = False

    @model_validator(mode="after")
    def action_arguments_are_explicit(self) -> Self:
        if self.action in {Action.CLICK, Action.DOUBLE_CLICK, Action.SET_VALUE, Action.SCROLL} and self.target is None:
            raise ValueError(f"{self.action.value} requires a target selector")
        if self.action == Action.SET_VALUE and self.text is None:
            raise ValueError("set_value requires explicit text")
        if self.action == Action.PRESS_KEY and not self.key:
            raise ValueError("press_key requires an explicit key")
        if self.action == Action.SCROLL and self.direction not in {"up", "down", "left", "right"}:
            raise ValueError("scroll requires direction up, down, left, or right")
        if self.action == Action.WAIT and self.duration_seconds is None:
            raise ValueError("wait requires duration_seconds (at most 10)")
        if self.action != Action.WAIT and self.duration_seconds is not None:
            raise ValueError("duration_seconds is only valid for wait")
        if self.action != Action.SET_VALUE and self.text is not None:
            raise ValueError("text is only valid for set_value")
        if self.action != Action.PRESS_KEY and self.key is not None:
            raise ValueError("key is only valid for press_key")
        if self.action != Action.SCROLL and self.direction is not None:
            raise ValueError("direction is only valid for scroll")
        if self.action == Action.VERIFY and self.target is not None:
            raise ValueError("verify does not act on a target")
        if self.allow_partial_observation and self.target and self.target.index is not None:
            raise ValueError("partial observations cannot establish first/last/ordinal targets")
        return self


class ObstaclePolicy(BaseModel):
    """What the controller may dismiss by itself when a blocking prompt explains a failure."""

    model_config = ConfigDict(extra="forbid")

    dismiss_labels: list[str] = Field(default_factory=list, max_length=20)
    max_dismissals: int = Field(default=3, ge=0, le=5)

    @model_validator(mode="after")
    def labels_are_safe(self) -> Self:
        for label in self.dismiss_labels:
            if not label.strip() or len(label) > 60:
                raise ValueError("dismiss_labels need 1-60 characters")
            if is_destructive(label):
                raise ValueError(
                    f"dismiss label {label!r} could change data or grant access; make it an explicit plan step"
                )
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(gt=0)
    goal: str = Field(min_length=1, max_length=2000)
    app_bundle_id: str = Field(min_length=1, max_length=255)
    steps: list[Step] = Field(min_length=1, max_length=100)
    start_step: str | None = None
    max_actions: int = Field(default=60, ge=1, le=60)
    max_seconds: float = Field(default=300, gt=0, le=300)
    max_rescues: int = Field(default=1, ge=0, le=1)
    obstacles: ObstaclePolicy = Field(default_factory=ObstaclePolicy)

    @model_validator(mode="after")
    def references_are_valid(self) -> Self:
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        known = set(ids)
        if self.start_step is not None and self.start_step not in known:
            raise ValueError("start_step must refer to a step in this plan")
        for step in self.steps:
            for ref in (step.next_step, step.on_failure):
                if ref is not None and ref not in known:
                    raise ValueError(f"step {step.id!r} references unknown step {ref!r}")
        return self

    @property
    def first_step_id(self) -> str:
        return self.start_step or self.steps[0].id
