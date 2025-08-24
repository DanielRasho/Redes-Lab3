# packet.py
import json
import uuid
from typing import Dict, List, Optional, Any, Union

class Packet:
    """
    Packet representation compatible with two header styles:
      - legacy: headers = ["A","B","C"]   (used by flooding for path)
      - extended: headers = {"msg_id": "...", "path": ["A","B","C"], ...}

    Provides convenience methods so other modules don't need to branch.
    """
    def __init__(
        self,
        proto: str,
        packet_type: str,
        from_addr: str,
        to_addr: str,
        ttl: int = 5,
        headers: Optional[Union[List[str], Dict[str, Any]]] = None,
        payload: Any = ""
    ):
        self.proto = proto
        self.type = packet_type
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.ttl = ttl
        # Accept list or dict; keep as-is for backwards compatibility
        self.headers: Union[List[str], Dict[str, Any]]
        if headers is None:
            self.headers = []
        else:
            self.headers = headers
        self.payload = payload

    # --- convenience helpers for header handling ----
    def _is_headers_dict(self) -> bool:
        return isinstance(self.headers, dict)

    def get_msg_id(self) -> Optional[str]:
        """Return the msg_id if present, otherwise None"""
        if self._is_headers_dict():
            return self.headers.get("msg_id")
        return None

    def ensure_msg_id(self) -> str:
        """
        Ensure there's a stable unique id stored in headers['msg_id'].
        If headers was a list, convert to dict preserving 'path'.
        Returns the msg_id.
        """
        mid = self.get_msg_id()
        if mid:
            return mid

        new_id = uuid.uuid4().hex
        if self._is_headers_dict():
            self.headers["msg_id"] = new_id
        else:
            # convert list -> dict preserving path
            path = list(self.headers) if isinstance(self.headers, list) else []
            self.headers = {"path": path, "msg_id": new_id}
        return new_id

    def get_path(self) -> List[str]:
        """Return headers path as a list of node ids (empty list if none)"""
        if self._is_headers_dict():
            return list(self.headers.get("path", []))
        if isinstance(self.headers, list):
            return list(self.headers)
        return []

    def set_path(self, path: List[str]):
        """Set/update path preserving header shape (dict or list)"""
        if self._is_headers_dict():
            self.headers["path"] = list(path)
        else:
            self.headers = list(path)

    def to_json(self) -> str:
        """Serialize to JSON string"""
        return json.dumps({
            "proto": self.proto,
            "type": self.type,
            "from": self.from_addr,
            "to": self.to_addr,
            "ttl": self.ttl,
            "headers": self.headers,
            "payload": self.payload
        })

    @classmethod
    def from_json(cls, json_str: str) -> "Packet":
        """Construct Packet from JSON string"""
        data = json.loads(json_str)
        return cls(
            proto=data["proto"],
            packet_type=data["type"],
            from_addr=data["from"],
            to_addr=data["to"],
            ttl=data.get("ttl", 5),
            headers=data.get("headers", []),
            payload=data.get("payload", "")
        )

    def decrement_ttl(self) -> bool:
        """Decrement TTL by one. Return True if still > 0 after decrement."""
        self.ttl -= 1
        return self.ttl > 0

    # nice repr for debugging
    def __repr__(self):
        return f"Packet(proto={self.proto}, type={self.type}, from={self.from_addr}, to={self.to_addr}, ttl={self.ttl}, headers={self.headers})"
