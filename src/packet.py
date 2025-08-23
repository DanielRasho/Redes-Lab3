import json
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple, Any

class Packet:
    """Represents a network packet following the specified JSON pattern"""
    
    def __init__(self, proto: str, packet_type: str, from_addr: str, 
                 to_addr: str, ttl: int = 5, headers: List[Dict] = None, 
                 payload: str = ""):
        self.proto = proto
        self.type = packet_type
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.ttl = ttl
        self.headers = headers or []
        self.payload = payload
    
    def to_json(self) -> str:
        """Convert packet to JSON string"""
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
    def from_json(cls, json_str: str) -> 'Packet':
        """Create packet from JSON string"""
        data = json.loads(json_str)
        return cls(
            proto=data["proto"],
            packet_type=data["type"],
            from_addr=data["from"],
            to_addr=data["to"],
            ttl=data["ttl"],
            headers=data.get("headers", []),
            payload=data.get("payload", "")
        )
    
    def decrement_ttl(self) -> bool:
        """Decrement TTL and return True if packet is still valid"""
        self.ttl -= 1
        return self.ttl > 0