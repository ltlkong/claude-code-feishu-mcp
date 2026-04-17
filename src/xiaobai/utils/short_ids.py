"""Short-id maps for message_ids (#N) and request_ids (rN).

Ported from ``feishu_channel/server.py`` lines 424–426 and 493–511. The live
server keeps its inline maps until Session 2 migrates to this module.
"""

from __future__ import annotations


class ShortIdMap:
    """Bidirectional map: synthetic short id ↔ opaque full id.

    Two parallel counters are intentionally tied to one — the same sequence
    number is used for both ``#N`` (message) and ``rN`` (request) so a
    message and its request can be referenced by the same ordinal. Matches
    legacy behavior in ``server.py``.
    """

    def __init__(self, msg_prefix: str = "#", req_prefix: str = "r") -> None:
        self._msg_prefix = msg_prefix
        self._req_prefix = req_prefix
        self._counter = 0
        self._msg_map: dict[str, str] = {}   # "#1" -> "om_xxx..."
        self._req_map: dict[str, str] = {}   # "r1" -> "uuid..."

    def register(self, message_id: str, request_id: str) -> tuple[str, str]:
        """Store full ids; return the new short aliases."""
        self._counter += 1
        n = self._counter
        short_msg = f"{self._msg_prefix}{n}"
        short_req = f"{self._req_prefix}{n}"
        if message_id:
            self._msg_map[short_msg] = message_id
        if request_id:
            self._req_map[short_req] = request_id
        return short_msg, short_req

    def resolve_message(self, short_or_full: str) -> str:
        """Resolve ``#N`` to the full message id; pass through anything else."""
        return self._msg_map.get(short_or_full, short_or_full)

    def resolve_request(self, short_or_full: str) -> str:
        """Resolve ``rN`` to the full request id; pass through anything else."""
        return self._req_map.get(short_or_full, short_or_full)
