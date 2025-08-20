import json
import time
import uuid
from typing import Dict, List, Optional, Tuple, Any
from src.algorithms.base import RoutingAlgorithm
from src.packet import Packet
from collections import deque


class FloodingAlgorithm(RoutingAlgorithm):
    """Flooding routing algorithm implementation"""
    
    def __init__(self, router_id: str):
        super().__init__(router_id)
        self.seen_packets = set()  # Track packet IDs to avoid loops
        # src/algorithms/flooding.py
        self._seen_ids: set[str] = set()
        self._seen_fifo: deque[str] = deque()
        self._seen_capacity: int = 50000  # tamaño del filtro (LRU simple)

    
    def get_name(self) -> str:
        return "flooding"
    
    def process_packet(self, packet: Packet, from_neighbor: str) -> Optional[str]:
        # --- New duplicate filter based on a stable per-message ID in headers['msg_id'] ---
        mid = _ensure_msg_id(packet)
        if _mark_seen_LRU(mid, self._seen_ids, self._seen_fifo, self._seen_capacity):
            return None  # Duplicate detected via msg_id, do not forward

        # --- Existing heuristic fallback (kept as-is) ---
        packet_id = f"{packet.from_addr}-{packet.to_addr}-{packet.payload[:20]}"
        if packet_id in self.seen_packets:
            return None  # Already seen, don't forward
        
        self.seen_packets.add(packet_id)
        return "flood"  # Special return value indicating flood to all neighbors except sender
    
    def get_next_hop(self, destination: str) -> Optional[str]:
        return "flood"  # Always flood
    
    def update_neighbor(self, neighbor_id: str, neighbor_info: Dict):
        self.neighbors[neighbor_id] = neighbor_info
        # For flooding, we don't maintain a routing table - we always flood
        print(f"🌊 Flooding: Added neighbor {neighbor_id}")


def _ensure_msg_id(pkt: Packet) -> str:
    """
    Ensure the packet has a stable unique identifier at headers['msg_id'].
    If headers are missing, they are created. Returns the msg_id string.
    """
    headers = getattr(pkt, "headers", None)
    if not isinstance(headers, dict):
        # create headers dict if missing
        setattr(pkt, "headers", {})
        headers = pkt.headers
    mid = headers.get("msg_id")
    if not mid:
        mid = uuid.uuid4().hex
        headers["msg_id"] = mid
    return mid


def _mark_seen_LRU(mid: str, seen_ids: set, seen_fifo: deque, capacity: int) -> bool:
    """
    Record the id in a simple LRU cache (set + deque).
    Returns True if this id was already seen (duplicate), False if it's new.
    """
    if mid in seen_ids:
        return True
    if len(seen_fifo) >= capacity:
        old = seen_fifo.popleft()
        seen_ids.discard(old)
    seen_fifo.append(mid)
    seen_ids.add(mid)
    return False
