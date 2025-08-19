from src.algorithms.base import RoutingAlgorithm
from typing import Dict, List, Optional, Tuple, Any
from src.packet import Packet

class DijkstraAlgorithm(RoutingAlgorithm):
    """Dijkstra's shortest path routing algorithm - calculates paths once on startup"""
    
    def __init__(self, router_id: str):
        super().__init__(router_id)
        self.topology = {}  # Full network topology: {node: [neighbors]}
        self.link_costs = {}  # Edge costs: {(node1, node2): cost}
        self.calculated = False  # Flag to ensure we only calculate once
    
    def get_name(self) -> str:
        return "dijkstra"
    
    def process_packet(self, packet: Packet, from_neighbor: str) -> Optional[str]:
        """Process packet - for data packets, just return next hop from routing table"""
        # For Dijkstra, we don't process any special routing packets
        # Just use the pre-calculated routing table
        return self.get_next_hop(packet.to_addr)
    
    def get_next_hop(self, destination: str) -> Optional[str]:
        """Get next hop for destination from pre-calculated routing table"""
        return self.routing_table.get(destination)
    
    def update_neighbor(self, neighbor_id: str, neighbor_info: Dict):
        """Update neighbor information and calculate paths if this is startup"""
        self.neighbors[neighbor_id] = neighbor_info
        print(f"🔗 Dijkstra: Added neighbor {neighbor_id}")
    
    def set_topology(self, topology: Dict):
        """Set the full network topology and calculate shortest paths"""
        self.topology = topology
        self._initialize_link_costs()
        self._calculate_shortest_paths()
        self.calculated = True
        print(f"📊 Dijkstra: Initialized with topology: {topology}")
    
    def _initialize_link_costs(self):
        """Initialize all link costs to 1 (can be modified for weighted graphs)"""
        self.link_costs.clear()
        
        for node, neighbors in self.topology.items():
            for neighbor in neighbors:
                # Create bidirectional links with cost 1
                self.link_costs[(node, neighbor)] = 1
                self.link_costs[(neighbor, node)] = 1
    
    def _calculate_shortest_paths(self):
        """Calculate shortest paths from this router to all other nodes using Dijkstra's algorithm"""
        print(f"🔄 Dijkstra: Calculating shortest paths from {self.router_id}")
        
        # Clear routing table
        self.routing_table.clear()
        
        # Get all nodes in the topology
        all_nodes = set(self.topology.keys())
        
        # Add nodes that appear as neighbors but not as keys
        for neighbors in self.topology.values():
            all_nodes.update(neighbors)
        
        # Initialize distances and predecessors
        distances = {node: float('inf') for node in all_nodes}
        distances[self.router_id] = 0
        predecessors = {}
        unvisited = all_nodes.copy()
        
        print(f"🌐 All nodes in topology: {sorted(all_nodes)}")
        
        # Dijkstra's algorithm
        while unvisited:
            # Find unvisited node with minimum distance
            current = min(unvisited, key=lambda x: distances[x])
            
            if distances[current] == float('inf'):
                # No more reachable nodes
                break
            
            unvisited.remove(current)
            
            # Get neighbors of current node
            current_neighbors = self.topology.get(current, [])
            
            for neighbor in current_neighbors:
                if neighbor in unvisited:
                    # Calculate new distance through current node
                    edge_cost = self.link_costs.get((current, neighbor), 1)
                    new_distance = distances[current] + edge_cost
                    
                    if new_distance < distances[neighbor]:
                        distances[neighbor] = new_distance
                        predecessors[neighbor] = current
        
        # Build routing table from shortest paths
        for destination in all_nodes:
            if destination != self.router_id and distances[destination] != float('inf'):
                # Trace back path to find next hop
                next_hop = self._find_next_hop(destination, predecessors)
                if next_hop:
                    self.routing_table[destination] = next_hop
        
        # Log the results
        print(f"🗺️ Dijkstra routing table for {self.router_id}:")
        if self.routing_table:
            for dest, next_hop in sorted(self.routing_table.items()):
                cost = distances.get(dest, "∞")
                print(f"   {dest} via {next_hop} (cost: {cost})")
        else:
            print("   (empty - no reachable destinations)")
        
        print(f"📏 Distance table: {dict(sorted(distances.items()))}")
    
    def _find_next_hop(self, destination: str, predecessors: Dict) -> Optional[str]:
        """Find the next hop by tracing back the shortest path"""
        if destination not in predecessors:
            return None
        
        # Trace path from destination back to source
        path = []
        current = destination
        
        while current != self.router_id:
            path.append(current)
            if current not in predecessors:
                return None  # No path exists
            current = predecessors[current]
        
        # Path is now [destination, ..., next_hop]
        # Return the last element which is our next hop
        if path:
            return path[-1]
        
        return None
    
    def get_full_path(self, destination: str) -> List[str]:
        """Get the full path to destination (useful for debugging)"""
        if destination not in self.routing_table:
            return []
        
        # Recalculate path for debugging
        path = [self.router_id]
        current = self.router_id
        
        # Follow routing table to build path
        visited = set()
        while current != destination and current not in visited:
            visited.add(current)
            next_hop = self.routing_table.get(destination) if current == self.router_id else None
            
            # For intermediate nodes, we'd need to query their routing tables
            # For now, just show the next hop
            if next_hop and next_hop != destination:
                path.append(next_hop)
                current = next_hop
            else:
                path.append(destination)
                break
        
        return path