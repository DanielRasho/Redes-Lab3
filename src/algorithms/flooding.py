# flooding.py
from typing import Dict, List, Optional
from src.algorithms.base import RoutingAlgorithm
from src.packet import Packet

class FloodingAlgorithm(RoutingAlgorithm):
    """
    Simple flooding algorithm that:
      - does NOT retransmit 'hello' packets (HELLO used for neighbor intro)
      - retransmits 'info' and other broadcast packets, maintaining a headers list
      - uses headers as a 3-entry rolling window to detect loops (cycle detection)
    """

    def __init__(self, router_id: str):
        super().__init__(router_id)
        self.neighbors: Dict[str, dict] = {}

    def get_name(self) -> str:
        return "flooding"

    def update_neighbor(self, neighbor_id: str, info: dict):
        """Keep neighbor list up to date"""
        self.neighbors[neighbor_id] = info
        # routing_table not needed for pure flooding, but keep interface
        self.routing_table[neighbor_id] = neighbor_id

    def process_packet(self, packet: Packet, from_neighbor_id: str) -> Optional[str]:
        """
        Decide what to do with an incoming packet.
        Return values:
          - "flood": router should flood the packet to (all) neighbors (except sender)
          - None: packet should NOT be forwarded (consume locally / drop)
        Behavior:
          - HELLO: do NOT retransmit (only local processing)
          - INFO: retransmit (flood) but maintain headers rolling list and detect cycles
          - MESSAGE destined to a particular node: for pure flooding, forward (flood) unless addressed to me
        """

        # Normalize headers to a list
        headers = packet.headers if isinstance(packet.headers, list) else []

        # 1) HELLO = neighbor introduction -> don't retransmit
        if packet.type == "hello":
            # Optionally register neighbor presence here (higher layers may do it)
            return None

        # 2) INFO or broadcasted packets -> need to flood with headers management
        if packet.to_addr in ["broadcast", "multicast"] or packet.type == "info":
            # Cycle detection: if this router already appears in headers -> drop
            if self.router_id in headers:
                return None

            # Maintain rolling window of last 3 routers:
            # When forwarding, remove first element if >=3 and append our own id.
            # (Router._process_packet should call decrement_ttl; still check TTL here)
            if len(headers) >= 3:
                headers.pop(0)
            headers.append(self.router_id)
            packet.headers = headers

            # If TTL is exhausted, do not forward
            if packet.ttl <= 0:
                return None

            return "flood"

        # 3) MESSAGE directed at a single node:
        # Flooding strategy: if not for this router, flood (best-effort).
        if packet.type == "message":
            if packet.to_addr == self.router_id:
                return None
            # update headers similarly for messages we forward so they can be loop-detected
            if len(headers) >= 3:
                headers.pop(0)
            headers.append(self.router_id)
            packet.headers = headers
            if packet.ttl <= 0:
                return None
            return "flood"

        # Default: don't forward
        return None
