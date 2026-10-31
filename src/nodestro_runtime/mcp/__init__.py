"""Nodestro's MCP client (ADR-0007): remote MCP servers' tools for agents, behind Nodestro's policy.

- :mod:`.netguard`: outbound network protection (threat model T7).
- :mod:`.models`: the stored configuration (servers, tools, approvals, audit events); needs no MCP SDK.
- :mod:`.vault`: where tokens live (D-3).
- :mod:`.hub`: connections through the official MCP SDK (version negotiation by the SDK).
- :mod:`.service`: configuration, discovery, approvals and the policy check.
- :mod:`.tools`: MCP tools as Orkexa tools.

The MCP SDK needs Python 3.11 or later; on older interpreters the configuration can be read and edited, but no
server can be reached (``mcp.unavailable``).
"""
