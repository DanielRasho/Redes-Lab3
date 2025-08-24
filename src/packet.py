import json
from typing import Dict, List, Optional, Any, Union


class Packet:
    """Represents a network packet following the specified JSON pattern

    Fields JSON:
      - proto: str
      - type: str
      - from: str
      - to: str
      - ttl: int
      - headers: List[str]
      - payload: str | dict | any JSON-serializable
    """

    def __init__(
        self,
        proto: str,
        packet_type: str,
        from_addr: str,
        to_addr: str,
        ttl: int = 5,
        headers: Optional[List[str]] = None,
        payload: Union[str, Dict[str, Any], List[Any], int, float, None] = "",
    ):
        self.proto: str = proto
        self.type: str = packet_type
        self.from_addr: str = from_addr
        self.to_addr: str = to_addr
        self.ttl: int = int(ttl)
        # headers debe ser una lista de strings según el protocolo ["A","B","C"]
        self.headers: List[str] = headers[:] if headers else []
        self.payload: Union[str, Dict[str, Any], List[Any], int, float, None] = payload

        if not isinstance(self.headers, list):
            raise TypeError("headers must be a list of strings")
        for h in self.headers:
            if not isinstance(h, str):
                raise TypeError("each header entry must be a string")

    def to_json(self) -> str:
        """Serializa el paquete a una cadena JSON."""
        obj = {
            "proto": self.proto,
            "type": self.type,
            "from": self.from_addr,
            "to": self.to_addr,
            "ttl": self.ttl,
            "headers": self.headers,
            "payload": self.payload,
        }
        # json.dumps must be serializable;
        return json.dumps(obj, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "Packet":
        """Build a Packet from a JSON string."""
        data = json.loads(json_str)
        proto = data.get("proto")
        packet_type = data.get("type")
        from_addr = data.get("from")
        to_addr = data.get("to")
        ttl = data.get("ttl", 5)
        headers = data.get("headers", [])
        payload = data.get("payload", "")

        if headers is None:
            headers = []
        if not isinstance(headers, list):
            raise TypeError("headers must be a list of strings")

        # Build Packet
        return cls(
            proto=proto,
            packet_type=packet_type,
            from_addr=from_addr,
            to_addr=to_addr,
            ttl=ttl,
            headers=headers,
            payload=payload,
        )

    def decrement_ttl(self) -> bool:
        """Decreases TTL by 1 and returns True if the packet is still valid (>0)."""
        try:
            self.ttl = int(self.ttl) - 1
        except Exception:
            # If ttl is not convertible, we force it to 0 and consider it invalid.
            self.ttl = 0
        return self.ttl > 0

    def __repr__(self) -> str:
        return (
            f"Packet(proto={self.proto!r}, type={self.type!r}, from={self.from_addr!r}, "
            f"to={self.to_addr!r}, ttl={self.ttl!r}, headers={self.headers!r}, payload={self.payload!r})"
        )
