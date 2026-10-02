from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Principal:
    tenant: str
    branches: tuple[str, ...]
    can_manage: bool = False

    def __post_init__(self):
        if not isinstance(self.tenant, str) or not self.tenant or type(self.can_manage) is not bool:
            raise ValueError("Principals need a tenant and a boolean management permission")
        if not self.branches or len(self.branches) > 16 or len(set(self.branches)) != len(self.branches):
            raise ValueError("Principals need 1 to 16 unique authorized branches")
        if any(not isinstance(branch, str) or not branch for branch in self.branches):
            raise ValueError("Authorized branches must be nonempty strings")

    @property
    def scope(self) -> str:
        from .models import digest

        return digest({"tenant": self.tenant, "branches": sorted(self.branches)})


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path("data"))
    dev_mode: bool = True
    keys: dict[str, Principal] = field(default_factory=dict, repr=False)
    host: str = "127.0.0.1"
    max_body_bytes: int = 262_144
    max_response_bytes: int = 2_097_152
    timeout_seconds: float = 30.0
    retention_days: int = 7
    http_url: str | None = None
    http_key: str | None = field(default=None, repr=False)
    http_models: tuple[str, ...] = ()
    http_ca_file: Path | None = None
    allow_loopback_http: bool = False
    openrouter_free_only: bool = False
    http_min_interval_seconds: float = 0.0
    http_pricing: dict = field(default_factory=dict)
    evaluation_amortization_tasks: int | None = None
    default_provider: str = "mock"
    production: bool = False
    max_inflight_requests: int = 16
    upload_timeout_seconds: float = 10.0
    requests_per_minute: int = 120

    def __post_init__(self):
        self.data_dir = self.data_dir.resolve()
        if self.production and (self.dev_mode or not self.keys or any(len(key) < 32 for key in self.keys)):
            raise ValueError("Production requires authenticated mode and client keys of at least 32 characters")
        if not 1 <= self.max_inflight_requests <= 256 or not 1 <= self.requests_per_minute <= 10000:
            raise ValueError("Admission and rate limits must be bounded positive values")
        if not math.isfinite(self.upload_timeout_seconds) or not 0 < self.upload_timeout_seconds <= 60:
            raise ValueError("Upload deadline must be positive and at most 60 seconds")
        if self.http_ca_file is not None:
            self.http_ca_file = self.http_ca_file.resolve()
            if not self.http_ca_file.is_file():
                raise ValueError("HTTP CA file must exist")
        if self.dev_mode and self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Development mode must bind to loopback")
        if not self.dev_mode and not self.keys:
            raise ValueError("Authenticated mode requires GILM_API_KEYS")
        if any(not isinstance(key, str) or not key or key.strip() != key for key in self.keys):
            raise ValueError("API keys must be nonempty and must not contain surrounding whitespace")
        if self.retention_days < 1 or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Retention and timeout must be positive")
        if not math.isfinite(self.http_min_interval_seconds) or not 0 <= self.http_min_interval_seconds <= 60:
            raise ValueError("HTTP minimum dispatch interval must be between zero and 60 seconds")
        if self.evaluation_amortization_tasks is not None and self.evaluation_amortization_tasks < 1:
            raise ValueError("Evaluation amortization divisor must be positive")
        if self.http_url:
            parsed = urlparse(self.http_url)
            local_http = (
                self.allow_loopback_http
                and parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            )
            if (
                (parsed.scheme != "https" and not local_http)
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("HTTP provider requires an HTTPS endpoint without URL credentials")
            if parsed.query or parsed.fragment:
                raise ValueError("HTTP provider URL must not contain query or fragment")
            if not self.http_key or not self.http_models:
                raise ValueError("HTTP provider requires a key and explicit model allowlist")
        if self.default_provider not in {"mock", "http"} or (self.default_provider == "http" and not self.http_url):
            raise ValueError("Default provider must be mock or an explicitly configured HTTP provider")
        if self.openrouter_free_only:
            if self.http_url != "https://openrouter.ai/api/v1/chat/completions":
                raise ValueError("OpenRouter free-only mode requires the official HTTPS completion endpoint")
            if not self.http_models or any(not model.endswith(":free") for model in self.http_models):
                raise ValueError("OpenRouter free-only mode requires explicit :free model IDs")
            if any(
                rates.get(name, 0) != 0
                for rates in self.http_pricing.values()
                for name in ("input", "output", "cached_input")
            ):
                raise ValueError("OpenRouter free-only mode rejects nonzero rate cards")
        for model, rates in self.http_pricing.items():
            if model not in self.http_models or not rates.get("version"):
                raise ValueError("Each configured rate card needs an allowed model and version")
            for name in ("input", "output", "cached_input"):
                if name in rates and (
                    type(rates[name]) not in (float, int) or not math.isfinite(rates[name]) or rates[name] < 0
                ):
                    raise ValueError("Rate cards use nonnegative USD per million tokens")
            if "input_token_ceiling" in rates and (
                type(rates["input_token_ceiling"]) is not int or rates["input_token_ceiling"] < 1
            ):
                raise ValueError("Live evaluation input_token_ceiling must be a positive integer")

    @classmethod
    def from_env(cls) -> Settings:
        keys = {
            key: Principal(value["tenant"], tuple(sorted(value["branches"])), value.get("can_manage", False))
            for key, value in json.loads(os.getenv("GILM_API_KEYS", "{}")).items()
        }
        return cls(
            data_dir=Path(os.getenv("GILM_DATA_DIR", "data")),
            dev_mode=os.getenv("GILM_DEV_MODE", "true").lower() == "true",
            host=os.getenv("GILM_HOST", "127.0.0.1"),
            keys=keys,
            timeout_seconds=float(os.getenv("GILM_TIMEOUT_SECONDS", "30")),
            retention_days=int(os.getenv("GILM_RETENTION_DAYS", "7")),
            http_url=os.getenv("GILM_HTTP_URL") or None,
            http_key=os.getenv("GILM_HTTP_KEY") or None,
            http_models=tuple(filter(None, os.getenv("GILM_HTTP_MODELS", "").split(","))),
            http_ca_file=Path(os.environ["GILM_HTTP_CA_FILE"]) if os.getenv("GILM_HTTP_CA_FILE") else None,
            allow_loopback_http=os.getenv("GILM_ALLOW_LOOPBACK_HTTP", "false").lower() == "true",
            openrouter_free_only=os.getenv("GILM_OPENROUTER_FREE_ONLY", "false").lower() == "true",
            http_min_interval_seconds=float(os.getenv("GILM_HTTP_MIN_INTERVAL_SECONDS", "0")),
            http_pricing=json.loads(os.getenv("GILM_HTTP_PRICING", "{}")),
            default_provider=os.getenv("GILM_DEFAULT_PROVIDER", "mock"),
            production=os.getenv("GILM_PRODUCTION", "false").lower() == "true",
            max_inflight_requests=int(os.getenv("GILM_MAX_INFLIGHT_REQUESTS", "16")),
            upload_timeout_seconds=float(os.getenv("GILM_UPLOAD_TIMEOUT_SECONDS", "10")),
            requests_per_minute=int(os.getenv("GILM_REQUESTS_PER_MINUTE", "120")),
            evaluation_amortization_tasks=int(os.environ["GILM_EVALUATION_AMORTIZATION_TASKS"])
            if os.getenv("GILM_EVALUATION_AMORTIZATION_TASKS")
            else None,
        )
