from __future__ import annotations

from pydantic import BaseModel, Field


DEFAULT_EXCLUDES = [
    ".git",
    "node_modules",
    ".cache",
    "cache",
    "logs",
    "*.log",
    "tmp",
    ".DS_Store",
]


class JobIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=4096)
    target_path: str = Field(min_length=1, max_length=2048)
    include_paths: list[str] = Field(default_factory=lambda: ["/www/wwwroot"])
    exclude_patterns: list[str] = Field(default_factory=lambda: DEFAULT_EXCLUDES.copy())
    schedule_kind: str = Field(default="daily", pattern="^(daily|weekly)$")
    day_of_week: int | None = Field(default=0, ge=0, le=6)
    hour: int = Field(default=3, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    enabled: bool = True


class JobPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=80)
    password: str | None = Field(default=None, min_length=1, max_length=4096)
    target_path: str | None = Field(default=None, min_length=1, max_length=2048)
    include_paths: list[str] | None = None
    exclude_patterns: list[str] | None = None
    schedule_kind: str | None = Field(default=None, pattern="^(daily|weekly)$")
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)
    enabled: bool | None = None

