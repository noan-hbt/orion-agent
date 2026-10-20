"""`[modules.mcp]`: the MCP servers the owner plugged in (D95)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orion.config import SecretRef
from orion.modules.base import ModuleSettings


class McpServer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,23}$", description="Short, e.g. 'notion'")
    description: str = Field(default="", max_length=300, description="What it is for")
    url: str = Field(default="", description="A remote server (streamable HTTP)")
    command: list[str] = Field(
        default_factory=list[str],
        description="A server Orion runs itself, e.g. ['npx', '-y', '@x/mcp']: set on the server "
        "only, never by message",
    )
    token: SecretRef | None = Field(
        default=None, description="Sent as `Authorization: Bearer`; held by the guardian split"
    )
    read_only: list[str] = Field(
        default_factory=list[str],
        description="Its tools that only read ('*': all): used without asking. Every other "
        "tool asks the owner first",
    )

    @model_validator(mode="after")
    def _one_way(self) -> McpServer:
        if bool(self.url) == bool(self.command):
            raise ValueError(f"{self.name}: give a `url` or a `command`, not both")
        if self.command and self.token is not None:
            raise ValueError(f"{self.name}: a `token` is for a `url` server")
        return self


class McpSettings(ModuleSettings):
    enabled: bool = False
    servers: list[McpServer] = Field(default_factory=list[McpServer])
