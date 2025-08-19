from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple, Any
from src.packet import Packet

class RoutingAlgorithm(ABC):
    """Abstract base class for routing algorithms"""
    
    def __init__(self, router_id: str):
        self.router_id = router_id
        self.routing_table = {}  # destination -> next_hop mapping
        self.neighbors = {}      # neighbor_id -> neighbor_info mapping
        
    @abstractmethod
    def get_name(self) -> str:
        pass
    
    @abstractmethod
    def process_packet(self, packet: Packet, from_neighbor: str) -> Optional[str]:
        """Process routing-specific packet and return next hop if needed"""
        pass
    
    @abstractmethod
    def get_next_hop(self, destination: str) -> Optional[str]:
        """Get next hop for destination"""
        pass
    
    @abstractmethod
    def update_neighbor(self, neighbor_id: str, neighbor_info: Dict):
        """Update neighbor information"""
        pass
