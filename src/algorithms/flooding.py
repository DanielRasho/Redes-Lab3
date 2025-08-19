import json
import time
from typing import Dict, List, Optional, Tuple, Any
from src.algorithms.base import RoutingAlgorithm
from src.packet import Packet

class FloodingAlgorithm(RoutingAlgorithm):
    """Flooding routing algorithm implementation"""
    
    def __init__(self, router_id: str):
        super().__init__(router_id)
        self.seen_packets = set()  # Track packet IDs to avoid loops
    
    def get_name(self) -> str:
        return "flooding"
    
    def process_packet(self, packet: Packet, from_neighbor: str) -> Optional[str]:
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