"""Shared plumbing for the two Sewa Setu MCP servers.

The package is deliberately one codebase deployed twice. The two servers differ
in their role, their port and the tools they publish; they do not differ in how
they authenticate, how they reach the portal, or how they report a refusal.
"""

from .settings import Settings, load, ConfigError
from .portal import Portal, PortalError

__all__ = ["Settings", "load", "ConfigError", "Portal", "PortalError"]
