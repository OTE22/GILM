from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
ModelName = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")]


class Function(StrictModel):
    name: Identifier
    description: str | None = Field(default=None, max_length=4096)
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})
    strict: bool | None = None


class Tool(StrictModel):
    type: Literal["function"] = "function"
    function: Function


class CallFunction(StrictModel):
    name: Identifier
    arguments: str = Field(max_length=32_768)


class ToolCall(StrictModel):
    id: Identifier
    type: Literal["function"] = "function"
    function: CallFunction


class Message(StrictModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None = Field(default=None, max_length=131_072)
    name: Identifier | None = None
    tool_calls: list[ToolCall] | None = Field(default=None, max_length=16)
    tool_call_id: Identifier | None = None

    @model_validator(mode="after")
    def relationships(self):
        if self.role == "tool":
            if not self.tool_call_id or self.content is None or self.tool_calls:
                raise ValueError("tool messages require content and tool_call_id")
        elif self.tool_call_id:
            raise ValueError("tool_call_id is allowed only on tool results")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls require the assistant role")
        if self.content is None and not self.tool_calls:
            raise ValueError("content is required except for assistant tool calls")
        return self


class Block(StrictModel):
    id: Identifier
    message_index: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: Identifier
    source_version: Identifier
    provenance: str = Field(min_length=1, max_length=512)
    role: Literal["system", "developer", "user", "assistant", "tool"]
    dependencies: list[Identifier] = Field(default_factory=list, max_length=32)
    required: bool = True
    eligible: bool = False
    retention: Literal["keep", "removable"] = "keep"
    redundant_with: Identifier | None = None
    protected: bool = False


class Extension(StrictModel):
    workflow: Literal["chat", "sales_report"] = "chat"
    plan_version: Identifier | None = None
    blocks: list[Block] = Field(default_factory=list, max_length=128)
    cache_approved: bool = False
    sensitive: bool = True


class ResponseFormat(StrictModel):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: dict | None = None

    @model_validator(mode="after")
    def validate_schema(self):
        if self.type == "json_schema":
            specification = self.json_schema
            if (
                not specification
                or not {"name", "schema"} <= specification.keys()
                or not specification.keys() <= {"name", "schema", "strict", "description"}
            ):
                raise ValueError("json_schema requires name and schema, with optional strict and description")
            if (
                not isinstance(specification["name"], str)
                or not 1 <= len(specification["name"]) <= 128
                or not isinstance(specification["schema"], dict)
            ):
                raise ValueError("json_schema name and schema have invalid types")
            if "strict" in specification and type(specification["strict"]) is not bool:
                raise ValueError("json_schema strict must be a boolean")
            if "description" in specification and not isinstance(specification["description"], str):
                raise ValueError("json_schema description must be a string")
        elif self.json_schema is not None:
            raise ValueError("json_schema is only valid with type=json_schema")
        return self


class ReasoningConfig(StrictModel):
    enabled: bool = Field(strict=True)


class Generation(StrictModel):
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    seed: int | None = None
    stop: str | list[str] | None = None
    response_format: ResponseFormat | None = None
    reasoning: ReasoningConfig | None = None

    @model_validator(mode="after")
    def stop_bounds(self):
        values = [self.stop] if isinstance(self.stop, str) else self.stop or []
        if self.stop == [] or len(values) > 4 or any(not s or len(s) > 256 for s in values):
            raise ValueError("stop allows one to four nonempty strings of at most 256 characters")
        return self


class ChatRequest(Generation):
    model: ModelName
    messages: list[Message] = Field(min_length=1, max_length=128)
    stream: bool = False
    tools: list[Tool] | None = Field(default=None, max_length=16)
    tool_choice: Literal["auto", "none", "required"] | dict | None = None
    parallel_tool_calls: bool | None = None
    gilm: Extension = Field(default_factory=Extension)

    @model_validator(mode="after")
    def validate_tools(self):
        names = [tool.function.name for tool in self.tools or []]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        if self.tool_choice is not None and not self.tools:
            raise ValueError("tool_choice requires tools")
        if self.parallel_tool_calls is not None and not self.tools:
            raise ValueError("parallel_tool_calls requires tools")
        if isinstance(self.tool_choice, dict):
            if set(self.tool_choice) != {"type", "function"} or self.tool_choice["type"] != "function":
                raise ValueError("tool_choice must identify an allowed function")
            fn = self.tool_choice["function"]
            if not isinstance(fn, dict) or set(fn) != {"name"} or fn["name"] not in names:
                raise ValueError("tool_choice function is not in tools")
        pending: set[str] = set()
        seen: set[str] = set()
        for msg in self.messages:
            if pending and msg.role != "tool":
                raise ValueError("tool calls must be followed by their results")
            for call in msg.tool_calls or []:
                if call.id in seen:
                    raise ValueError("duplicate tool call ID")
                pending.add(call.id)
                seen.add(call.id)
            if msg.role == "tool":
                if msg.tool_call_id not in pending:
                    raise ValueError("orphan or duplicate tool result")
                pending.remove(msg.tool_call_id)
        if pending:
            raise ValueError("unresolved tool calls")
        return self

    def provider_payload(self) -> dict:
        return self.model_dump(mode="json", exclude={"gilm"}, exclude_none=True)


class CachePolicy(StrictModel):
    enabled: bool = False
    ttl_seconds: int = Field(default=60, ge=1, le=3600)


class PlanConfig(StrictModel):
    workflow: Literal["chat", "sales_report"]
    remove_redundant: bool = False
    adapter_mode: Literal["detailed", "aggregate"] = "detailed"
    selected_fields: (
        list[Literal["sale_id", "branch", "date", "amount_cents", "currency", "product", "units"]] | None
    ) = None
    provider: Literal["mock", "http"] = "mock"
    model: ModelName = "mock-report-v1"
    generation: Generation = Field(default_factory=Generation)
    cache: CachePolicy = Field(default_factory=CachePolicy)
    correctness_checks: list[Literal["facts", "numeric", "completeness", "permissions", "structure", "provenance"]] = (
        Field(default_factory=lambda: ["facts", "numeric", "completeness", "permissions", "structure", "provenance"])
    )
    fallback: Literal["original"] = "original"

    @model_validator(mode="after")
    def complete_contract(self):
        required = {"facts", "numeric", "completeness", "permissions", "structure", "provenance"}
        if set(self.correctness_checks) != required:
            raise ValueError("MVP plans must retain every correctness check")
        if self.selected_fields:
            if len(self.selected_fields) != len(set(self.selected_fields)):
                raise ValueError("selected_fields must be unique")
            if not {"branch", "date", "amount_cents", "currency"} <= set(self.selected_fields):
                raise ValueError("projection must retain branch, date, amount_cents and currency")
        if self.workflow == "chat" and (self.adapter_mode != "detailed" or self.selected_fields):
            raise ValueError("SQL adapter parameters require sales_report")
        return self


class Candidate(StrictModel):
    parent_version: Identifier
    description: str = Field(min_length=1, max_length=1024)
    config: PlanConfig
    source_versions: dict[Identifier, Identifier] = Field(default_factory=dict)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def timezone_required(self):
        if self.expires_at and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return self


class ReportRequest(StrictModel):
    start: date
    end: date
    branches: list[Identifier] | None = Field(default=None, min_length=1, max_length=16)
    plan_version: Identifier | None = None
    cache_approved: bool = False

    @model_validator(mode="after")
    def valid_range(self):
        if self.end <= self.start or (self.end - self.start).days > 366:
            raise ValueError("end is exclusive; reporting range must be 1 to 366 days")
        if self.branches and len(set(self.branches)) != len(self.branches):
            raise ValueError("branches must be unique")
        return self


class LiveEvaluation(StrictModel):
    allow_paid: Literal[True]
    max_estimated_usd: Decimal = Field(ge=0, le=100)
    max_provider_attempts: int = Field(default=16, ge=1, le=64)
    max_output_tokens: int = Field(default=512, ge=1, le=8192)
