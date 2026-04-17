"""MCP tool groups for xiaobai.

Each module defines handler functions that the central dispatcher in
``xiaobai.mcp_server`` calls after resolving aliases + short ids. Handlers
are pure coroutines — they receive already-resolved IDs and channel
references, not raw ``arguments`` dicts.
"""
