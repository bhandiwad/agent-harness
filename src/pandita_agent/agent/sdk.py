"""Public Python SDK for service clients and operator-reviewed capability authoring."""

from pandita_agent.agent.authoring import (
    mcp_tool,
    pin_instructions,
    pin_skill,
    rest_tool,
    write_config,
)
from pandita_agent.agent.capabilities import InstructionTree
from pandita_agent.agent.checks import CompletionContext, CompletionResult
from pandita_agent.agent.client import AsyncClient, Client
from pandita_agent.agent.contracts import Agent, Connection, Model, Settings, Skill, load_settings
from pandita_agent.agent.execution import ExecutionProfile, Sampling
from pandita_agent.agent.sdk_types import (
    APIError,
    AuthenticationError,
    Event,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    PreparedCommand,
    RevisionConflict,
    SDKError,
    Session,
    StreamClosed,
    TransportError,
    Unavailable,
    WaitTimeout,
)
from pandita_agent.governance.context import Capacity
from pandita_agent.governance.tools import ToolSpec

__all__ = [
    "APIError",
    "Agent",
    "AsyncClient",
    "AuthenticationError",
    "Capacity",
    "Client",
    "CompletionContext",
    "CompletionResult",
    "Connection",
    "Event",
    "ExecutionProfile",
    "Sampling",
    "InstructionTree",
    "InvalidRequest",
    "Model",
    "NotFound",
    "PermissionDenied",
    "PreparedCommand",
    "RevisionConflict",
    "SDKError",
    "Session",
    "Settings",
    "Skill",
    "StreamClosed",
    "ToolSpec",
    "TransportError",
    "Unavailable",
    "WaitTimeout",
    "load_settings",
    "mcp_tool",
    "pin_instructions",
    "pin_skill",
    "rest_tool",
    "write_config",
]
