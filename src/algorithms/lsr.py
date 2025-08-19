import json
import time
from typing import Dict, List, Optional, Tuple, Any
from src.algorithms.base import RoutingAlgorithm
from src.packet import Packet

class LinkStateRouting(RoutingAlgorithm):
    """Link State Routing algorithm"""
    
    def __init__(self, router_id: str):
        super().__init__(router_id)
        self.link_state_db = {}
        
    def get_name(self) -> str:
        return "lsr"
    
    def process_packet(self, packet: Packet, from_neighbor: str) -> Optional[str]:
        if packet.type == "hello":
            # Process hello packet
            self.update_neighbor(from_neighbor, {"last_seen": time.time()})
        elif packet.type == "info":
            # Process link state advertisement
            try:
                ls_info = json.loads(packet.payload)
                self.link_state_db[packet.from_addr] = ls_info
                self._calculate_routes()
            except json.JSONDecodeError:
                pass
        
        return self.get_next_hop(packet.to_addr)
    
    def get_next_hop(self, destination: str) -> Optional[str]:
        return self.routing_table.get(destination)
    
    def update_neighbor(self, neighbor_id: str, neighbor_info: Dict):
        self.neighbors[neighbor_id] = neighbor_info
    
    def _calculate_routes(self):
        """Calculate routes using link state database"""
        # Simplified SPF calculation
        for dest in self.link_state_db:
            if dest in self.neighbors:
                self.routing_table[dest] = dest