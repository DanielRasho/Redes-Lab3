import socket
import threading
import logging
import json
import uuid
from collections import deque

import asyncio
import redis.asyncio as redis

from datetime import datetime
import time
from src.algorithms.dijkstra import DijkstraAlgorithm
from src.algorithms.flooding import FloodingAlgorithm
from src.algorithms.lsr import LinkStateRouting
from src.algorithms.base import RoutingAlgorithm
from src.packet import Packet
from src.utils import Colors
from typing import Dict, List, Optional, Tuple, Any
from abc import ABC, abstractmethod
from collections import deque
import uuid


class BaseRouter(ABC):
    """Base class for router implementations with common functionality"""
    
    def __init__(self, router_id: str, algorithm: str):
        self.router_id = router_id
        self.topology = {}  # Full network topology from config
        self.neighbors = {}  # Direct neighbors from topology
        self.running = False
        
        # Duplicate packet filtering
        self._rx_seen_ids = set()
        self._rx_seen_fifo = deque()
        self._rx_seen_capacity = 50000

        self._rx_seen_lock = threading.Lock()
        
        # Initialize routing algorithm
        self.routing_algorithm = self._create_routing_algorithm(algorithm)
        
        # Logger for this router
        self.logger = logging.getLogger(f"Router-{router_id}")
        
        # Packet logging
        self.packet_log = []
    
    def _create_routing_algorithm(self, algorithm: str) -> RoutingAlgorithm:
        """Factory method to create routing algorithm instance"""
        algorithms = {
            "flooding": FloodingAlgorithm,
            "dijkstra": DijkstraAlgorithm,
            "lsr": LinkStateRouting
        }
        
        if algorithm not in algorithms:
            raise ValueError(f"Unknown routing algorithm: {algorithm}")
        
        return algorithms[algorithm](self.router_id)
    
    def load_topology(self, topo_file: str):
        """Load network topology from JSON file"""
        try:
            with open(topo_file, 'r') as f:
                topo_data = json.load(f)
                if topo_data.get("type") != "topo":
                    raise ValueError("Invalid topology file format")
                
                self.topology = topo_data["config"]
                self.neighbors = {neighbor: {} for neighbor in self.topology.get(self.router_id, [])}
                
                self.logger.info(f"Loaded topology: {self.router_id} -> {list(self.neighbors.keys())}")
                
                # For Dijkstra algorithm, pass the full topology
                if isinstance(self.routing_algorithm, DijkstraAlgorithm):
                    self.routing_algorithm.set_topology(self.topology)
                else:
                    # Update routing algorithm with topology info for other algorithms
                    for neighbor_id in self.neighbors:
                        self.routing_algorithm.update_neighbor(neighbor_id, {"cost": 1})
                    
        except Exception as e:
            self.logger.error(f"Error loading topology: {e}")
            raise
    
    @abstractmethod
    def load_node_addresses(self, names_file: str):
        """Load node address mappings from JSON file (implementation specific)"""
        pass
    
def _process_packet(self, packet: Packet, from_neighbor_id: Optional[str] = None):
    """Process an incoming packet.

    from_neighbor_id: optional neighbor id string (not a socket). When flooding we map it to a socket
    to exclude the sender from retransmission.
    """
    # Duplicate detection
    if self._mark_rx_seen(packet):
        self.logger.debug(f"[DUPLICATE] Packet already seen, dropping")
        return

    # Allow routing algorithm to do protocol-specific processing if needed
    if packet.proto == "flooding":
        # flooding algorithm may keep its own state — nothing to do here by default
        pass

    # --- Broadcast / Multicast handling (multi-hop protocols like LSR may request forwarding) ---
    if packet.to_addr in ["broadcast", "multicast"]:
        neighbor_hint = from_neighbor_id if from_neighbor_id else "unknown"
        decision = self.routing_algorithm.process_packet(packet, neighbor_hint)

        # If algorithm returned None, for broadcast we interpret that as "flood"
        if decision is None:
            if packet.type == "hello":
                # HELLO must NOT be retransmitted.
                return
            decision = "flood"

        # Map neighbor id -> socket to pass to flood helpers (they expect socket to exclude)
        exclude_socket = self.active_connections.get(from_neighbor_id) if from_neighbor_id else None

        if decision == "flood":
            # Decrement TTL for multi-hop broadcast forward
            if not packet.decrement_ttl():
                self.logger.warning(f"[DROPPED] Broadcast TTL expired")
                return
            self._flood_packet(packet, exclude_socket)
            return

        if decision == "flood_lsa":
            if not packet.decrement_ttl():
                self.logger.warning(f"[DROPPED] LSA TTL expired")
                return
            self._flood_packet_except_sender(packet, exclude_socket)
            return

        # If decision was something else (e.g., a specific neighbor), let it fall through
        # to the unicast forwarding logic below.

    # --- If packet is addressed to this router ---
    if packet.to_addr == self.router_id:
        if packet.type == "message":
            self.logger.info(f"Message received: {packet.payload}")
            print(f"\n{Colors.GREEN}[MESSAGE FROM {packet.from_addr}]: {packet.payload}{Colors.ENDC}")
        elif packet.type == "echo":
            # send echo reply
            reply = Packet(
                proto=packet.proto,
                packet_type="echo_reply",
                from_addr=self.router_id,
                to_addr=packet.from_addr,
                ttl=5,
                headers=[self.router_id],
                payload=f"Echo reply from {self.router_id}"
            )
            self._forward_packet(reply)
        elif packet.type == "echo_reply":
            print(f"\n{Colors.BLUE}[ECHO REPLY FROM {packet.from_addr}]: {packet.payload}{Colors.ENDC}")
        return

    # --- Unicast forwarding ---
    # Decrement TTL now for unicast forwarding and drop if expired
    if not packet.decrement_ttl():
        self.logger.warning(f"[DROPPED] Packet TTL expired")
        return

    # Ask routing algorithm for next hop. Provide neighbor hint if available.
    neighbor_hint = from_neighbor_id if from_neighbor_id else "unknown"
    next_hop = self.routing_algorithm.process_packet(packet, neighbor_hint)

    # If algorithm explicitly requested flooding for a unicast (rare), map neighbor id -> socket
    exclude_socket = self.active_connections.get(from_neighbor_id) if from_neighbor_id else None

    if next_hop == "flood":
        self._flood_packet(packet, exclude_socket)
    elif next_hop == "flood_lsa":
        self._flood_packet_except_sender(packet, exclude_socket)
    elif next_hop:
        # next_hop is expected to be a neighbor id
        self._send_to_neighbor(packet, next_hop)
    else:
        self.logger.warning(f"[DROPPED] No route to destination {packet.to_addr}")

    
    @abstractmethod
    def _flood_packet(self, packet: Packet, exclude_neighbor_id: Optional[str]):
        """Flood packet to all neighbors except the specified neighbor_id"""
        pass
    
    @abstractmethod
    def _flood_packet_except_sender(self, packet: Packet, exclude_neighbor_id: Optional[str]):
        """Flood packet to all neighbors except the sender (by neighbor_id)"""
        pass
    
    @abstractmethod
    def _send_to_neighbor(self, packet: Packet, neighbor_id: str):
        """Send packet to specific neighbor"""
        pass
    
    def _forward_packet(self, packet: Packet):
        """Forward packet using routing algorithm"""
        next_hop = self.routing_algorithm.get_next_hop(packet.to_addr)
        if next_hop == "flood":
            self._flood_packet(packet, exclude_neighbor_id=None)
        elif next_hop:
            self._send_to_neighbor(packet, next_hop)
        else:
            if packet.to_addr in self.neighbors:
                self._send_to_neighbor(packet, packet.to_addr)
    
    @abstractmethod
    def _broadcast_packet(self, packet: Packet):
        """Broadcast packet to all active neighbors"""
        pass
    
    def _periodic_tasks(self):
        """Handle periodic tasks like hello packets and LSAs"""
        while self.running:
            time.sleep(5)  # Check every 5 seconds
            
            try:
                # Send hello packets for LSR
                if isinstance(self.routing_algorithm, LinkStateRouting):
                    if self.routing_algorithm.should_send_hello():
                        hello_packet = self.routing_algorithm.create_hello_packet()
                        self._broadcast_packet(hello_packet)
                        self._log_packet("SENT", hello_packet, "multicast")
                    
                    # Send LSA if needed
                    if self.routing_algorithm.should_send_lsa():
                        lsa_packet = self.routing_algorithm.create_lsa_packet()
                        self._broadcast_packet(lsa_packet)
                        self._log_packet("SENT", lsa_packet, "broadcast")
                    
                    # Check for dead neighbors and age LSA database
                    try:
                        self.routing_algorithm._check_neighbor_timeouts()
                    except Exception as e:
                        self.logger.error(f"Error checking neighbor timeouts: {e}")
                    
                    try:
                        self.routing_algorithm._age_lsa_database()
                    except Exception as e:
                        self.logger.error(f"Error aging LSA database: {e}")
                
                # For Dijkstra algorithm - no periodic packets needed
                elif isinstance(self.routing_algorithm, DijkstraAlgorithm):
                    pass
                
                # Send basic hello for flooding (neighbor discovery)
                else:
                    # REPLACE/ENSURE this block when creating hello packets for non-LSR algorithms
                    hello_packet = Packet(
                        proto=self.routing_algorithm.get_name(),   # keep configurable, not forcing "lsr"
                        packet_type="hello",
                        from_addr=self.router_id,
                        to_addr="broadcast",
                        ttl=5,
                        headers=[self.router_id],                  # ensure this is present
                        payload=""
                    )
                    self._broadcast_packet(hello_packet)



                    
            except Exception as e:
                self.logger.error(f"Error in periodic tasks: {e}")
                import traceback
                self.logger.error(f"Traceback: {traceback.format_exc()}")
    
    def _handle_user_input(self):
        """Handle user input for sending messages"""
        print(f"\n{Colors.BOLD}Router {self.router_id} ready.{Colors.ENDC} Commands:")
        print(f"  {Colors.CYAN}send <destination> <message>{Colors.ENDC} - Send message to destination")
        print(f"  {Colors.CYAN}echo <destination>{Colors.ENDC} - Send echo to destination")
        print(f"  {Colors.CYAN}neighbors{Colors.ENDC} - Show neighbors")
        print(f"  {Colors.CYAN}routes{Colors.ENDC} - Show routing table")
        print(f"  {Colors.CYAN}logs{Colors.ENDC} - Show packet logs")
        print(f"  {Colors.CYAN}topology{Colors.ENDC} - Show network topology")
        print(f"  {Colors.CYAN}path <destination>{Colors.ENDC} - Show path to destination (Dijkstra only)")
        print(f"  {Colors.CYAN}debug{Colors.ENDC} - Show routing algorithm state")
        print(f"  {Colors.CYAN}lsr{Colors.ENDC} - Show detailed LSR state (LSR only)")
        print(f"  {Colors.CYAN}quit{Colors.ENDC} - Exit router")
        
        while self.running:
            try:
                command = input(f"\n{Colors.BOLD}{self.router_id}>{Colors.ENDC} ").strip().split()
                if not command:
                    continue
                
                if command[0] == "send" and len(command) >= 3:
                    destination = command[1]
                    message = " ".join(command[2:])
                    packet = Packet(
                        proto=self.routing_algorithm.get_name(),
                        packet_type="message",
                        from_addr=self.router_id,
                        to_addr=destination,
                        payload=message
                    )
                    try:
                        self._forward_packet(packet)
                        self._log_packet("SENT", packet)
                    except Exception as e:
                        print(f"{Colors.RED}Error sending packet: {e}{Colors.ENDC}")
                
                elif command[0] == "echo" and len(command) >= 2:
                    destination = command[1]
                    packet = Packet(
                        proto=self.routing_algorithm.get_name(),
                        packet_type="echo",
                        from_addr=self.router_id,
                        to_addr=destination,
                        payload="Echo request"
                    )
                    try:
                        self._forward_packet(packet)
                        self._log_packet("SENT", packet)
                    except Exception as e:
                        print(f"{Colors.RED}Error sending echo: {e}{Colors.ENDC}")
                
                elif command[0] == "neighbors":
                    self._show_neighbors()
                
                elif command[0] == "routes":
                    print(f"{Colors.BOLD}Routing table:{Colors.ENDC}")
                    for dest, next_hop in self.routing_algorithm.routing_table.items():
                        print(f"  {Colors.YELLOW}{dest}{Colors.ENDC} -> {Colors.CYAN}{next_hop}{Colors.ENDC}")
                
                elif command[0] == "topology":
                    print(f"{Colors.BOLD}Network topology:{Colors.ENDC}")
                    for node, neighbors in self.topology.items():
                        status = f"{Colors.GREEN}(this node){Colors.ENDC}" if node == self.router_id else ""
                        print(f"  {Colors.YELLOW}{node}{Colors.ENDC}{status}: {neighbors}")
                
                elif command[0] == "logs":
                    print(f"{Colors.BOLD}Recent packet logs:{Colors.ENDC}")
                    for log_entry in self.packet_log[-10:]:
                        print(f"  {log_entry}")
                
                elif command[0] == "path" and len(command) >= 2:
                    destination = command[1]
                    if isinstance(self.routing_algorithm, DijkstraAlgorithm):
                        path = self.routing_algorithm.get_full_path(destination)
                        if path:
                            path_str = " → ".join(path)
                            print(f"Path to {Colors.YELLOW}{destination}{Colors.ENDC}: {Colors.CYAN}{path_str}{Colors.ENDC}")
                        else:
                            print(f"No path to {Colors.YELLOW}{destination}{Colors.ENDC}")
                    else:
                        print("Path command only available for Dijkstra algorithm")
                
                elif command[0] == "debug":
                    self._show_debug_info()
                
                elif command[0] == "lsr" and isinstance(self.routing_algorithm, LinkStateRouting):
                    print(f"{Colors.BOLD}LSR Detailed Debug:{Colors.ENDC}")
                    print(f"  Neighbor States:")
                    for nb_id, state in self.routing_algorithm.neighbor_states.items():
                        last_seen = state.get('last_seen', 0)
                        alive = state.get('alive', False)
                        cost = state.get('cost', 1)
                        print(f"    {Colors.YELLOW}{nb_id}{Colors.ENDC}: alive={alive}, cost={cost}, last_seen={last_seen}")
                    
                    print(f"  LSA Database:")
                    for origin, lsa in self.routing_algorithm.link_state_db.items():
                        seq = lsa.get('seq', 0)
                        neighbors = lsa.get('neighbors', {})
                        print(f"    {Colors.CYAN}{origin}{Colors.ENDC}: seq={seq}, neighbors={neighbors}")
                    
                    print(f"  Area Routers: {list(self.routing_algorithm.area_routers)}")
                    print(f"  My LSA Sequence: {self.routing_algorithm.my_lsa_seq}")
                
                elif command[0] == "quit":
                    self.stop()
                    break
                
                else:
                    print(f"{Colors.RED}Unknown command{Colors.ENDC}")
            
            except KeyboardInterrupt:
                self.stop()
                break
            except Exception as e:
                print(f"{Colors.RED}Error: {e}{Colors.ENDC}")
                import traceback
                self.logger.error(f"User input error: {traceback.format_exc()}")
    
    @abstractmethod
    def _show_neighbors(self):
        """Show neighbor status (implementation specific)"""
        pass
    
    def _show_debug_info(self):
        """Show routing algorithm debug information"""
        print(f"{Colors.BOLD}Routing Algorithm Debug Info:{Colors.ENDC}")
        print(f"  Algorithm: {Colors.YELLOW}{self.routing_algorithm.get_name()}{Colors.ENDC}")
        print(f"  Routing Table: {Colors.CYAN}{dict(self.routing_algorithm.routing_table)}{Colors.ENDC}")
        print(f"  Neighbors: {Colors.CYAN}{list(self.routing_algorithm.neighbors.keys())}{Colors.ENDC}")
        
        if isinstance(self.routing_algorithm, DijkstraAlgorithm):
            print(f"  Topology: {Colors.MAGENTA}{self.routing_algorithm.topology}{Colors.ENDC}")
        elif isinstance(self.routing_algorithm, LinkStateRouting):
            print(f"  LSA Database: {Colors.MAGENTA}{list(self.routing_algorithm.link_state_db.keys())}{Colors.ENDC}")
            print(f"  Area Routers: {Colors.MAGENTA}{list(self.routing_algorithm.area_routers)}{Colors.ENDC}")
            print(f"  Neighbor States: {Colors.MAGENTA}{list(self.routing_algorithm.neighbor_states.keys())}{Colors.ENDC}")
    
    def _log_packet(self, action: str, packet: Packet, neighbor: str = None):
        """Log packet activity"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        neighbor_info = f" via {neighbor}" if neighbor else ""
        mid = None
        try:
            mid = getattr(packet, "headers", {}).get("msg_id")
        except Exception:
            mid = None
        mid_info = f" [id={mid}]" if mid else ""
        log_entry = f"{timestamp} [{action}]{neighbor_info} {packet.type}{mid_info} from {packet.from_addr} to {packet.to_addr}"
        
        self.packet_log.append(log_entry)
        self.logger.info(log_entry)
        
        if len(self.packet_log) > 100:
            self.packet_log = self.packet_log[-100:]
    
    @staticmethod
    def _ensure_msg_id(self, packet):
        """
        Ensure a stable unique id in packet.headers['msg_id'] for duplicate filtering.
        Delegates to Packet.ensure_msg_id() so we don't clobber flooding path lists.
        """
        try:
            packet.ensure_msg_id()
        except Exception:
            # last-resort fallback (shouldn't be necessary with new Packet)
            if not getattr(packet, "headers", None) or not isinstance(packet.headers, dict):
                packet.headers = {"msg_id": uuid.uuid4().hex, "path": packet.get_path() if hasattr(packet, "get_path") else []}
            elif "msg_id" not in packet.headers:
                packet.headers["msg_id"] = uuid.uuid4().hex

    
    def _mark_rx_seen(self, packet) -> bool:
        """Returns True if packet.headers['msg_id'] was already seen (duplicate).
        Maintains a simple LRU (set + deque). Thread-safe using _rx_seen_lock.
        """
        try:
            # Try packet.get_msg_id() helper (Packet class). Fallback to dict lookup.
            mid = packet.get_msg_id() if hasattr(packet, "get_msg_id") else None
        except Exception:
            mid = None

        if not mid:
            # If there's no msg_id we can't dedupe; treat as unseen (could also generate one)
            return False

        with self._rx_seen_lock:
            if mid in self._rx_seen_ids:
                return True
            if len(self._rx_seen_fifo) >= self._rx_seen_capacity:
                old = self._rx_seen_fifo.popleft()
                self._rx_seen_ids.discard(old)
            self._rx_seen_fifo.append(mid)
            self._rx_seen_ids.add(mid)
        return False

    
    @abstractmethod
    async def start(self):
        """Start the router (implementation specific)"""
        pass
    
    @abstractmethod
    def stop(self):
        """Stop the router (implementation specific)"""
        pass

class SocketRouter(BaseRouter):
    """Socket-based router implementation"""
    
    def __init__(self, router_id: str, host: str, port: int, algorithm: str):
        super().__init__(router_id, algorithm)
        self.host = host
        self.port = port
        self.socket = None
        self.node_addresses = {}  # Node ID -> {host, port} mapping
        self.active_connections = {}  # neighbor_id -> socket
    
    def load_node_addresses(self, names_file: str):
        """Load node address mappings from JSON file"""
        try:
            with open(names_file, 'r') as f:
                names_data = json.load(f)
                if names_data.get("type") != "names":
                    raise ValueError("Invalid names file format")
                
                self.node_addresses = names_data["config"]
                
                # Update neighbor information with addresses
                for neighbor_id in self.neighbors:
                    if neighbor_id in self.node_addresses:
                        addr_info = self.node_addresses[neighbor_id]
                        self.neighbors[neighbor_id] = {
                            "host": addr_info["host"],
                            "port": addr_info["port"],
                            "cost": 1  # Default cost
                        }
                        # Update routing algorithm with complete neighbor info
                        self.routing_algorithm.update_neighbor(neighbor_id, self.neighbors[neighbor_id])
                
                self.logger.info(f"Loaded addresses for {len(self.node_addresses)} nodes")
                
                # Trigger initial routing calculation for LSR and Dijkstra
                if isinstance(self.routing_algorithm, (DijkstraAlgorithm, LinkStateRouting)):
                    print(f"🔧 Triggering initial routing calculation for {self.router_id}")
                    
        except Exception as e:
            self.logger.error(f"Error loading node addresses: {e}")
            raise
    
    async def start(self):
        """Start the socket router"""
        self.running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.host, self.port))
        self.socket.listen(10)
        
        self.logger.info(f"Router {Colors.BOLD}{self.router_id}{Colors.ENDC} started on {self.host}:{self.port}")
        self.logger.info(f"Using routing algorithm: {Colors.BOLD}{self.routing_algorithm.get_name()}{Colors.ENDC}")
        
        # Start listening thread
        listen_thread = threading.Thread(target=self._listen_for_connections)
        listen_thread.daemon = True
        listen_thread.start()
        
        # Start input thread for user commands
        input_thread = threading.Thread(target=self._handle_user_input)
        input_thread.daemon = True
        input_thread.start()
        
        # Connect to neighbors (non-blocking, continues even if some fail)
        self._connect_to_neighbors()
        
        # Start periodic tasks thread
        periodic_thread = threading.Thread(target=self._periodic_tasks)
        periodic_thread.daemon = True
        periodic_thread.start()
        
        # Retry connections periodically
        retry_thread = threading.Thread(target=self._retry_connections)
        retry_thread.daemon = True
        retry_thread.start()
        
        # Keep main thread alive
        while self.running:
            time.sleep(1)
    
    def _listen_for_connections(self):
        """Listen for incoming connections"""
        while self.running:
            try:
                client_socket, address = self.socket.accept()
                self.logger.info(f"Accepted connection from {address}")
                
                # Handle client in separate thread
                client_thread = threading.Thread(
                    target=self._handle_client, 
                    args=(client_socket,)
                )
                client_thread.daemon = True
                client_thread.start()
            except Exception as e:
                if self.running:
                    self.logger.error(f"Error accepting connection: {e}")
    
    def _handle_client(self, client_socket: socket.socket):
        """Handle incoming packets from a client"""
        client_neighbor_id = None
        try:
            while self.running:
                data = client_socket.recv(4096)
                if not data:
                    break
                
                try:
                    packet = Packet.from_json(data.decode('utf-8'))
                    
                    # Try to identify the neighbor by the packet's from_addr
                    if packet.from_addr in self.neighbors:
                        client_neighbor_id = packet.from_addr
                    
                    self._log_packet("RECEIVED", packet, client_neighbor_id)
                    self._process_packet(packet, client_neighbor_id)
                except Exception as e:
                    self.logger.error(f"Error processing packet: {e}")
        except Exception as e:
            self.logger.error(f"Error handling client: {e}")
        finally:
            client_socket.close()
    
    def _connect_to_neighbors(self):
        """Connect to all configured neighbors (non-blocking)"""
        for neighbor_id, neighbor_info in self.neighbors.items():
            if neighbor_id not in self.active_connections:
                self._try_connect_neighbor(neighbor_id, neighbor_info)
    
    def _try_connect_neighbor(self, neighbor_id: str, neighbor_info: Dict):
        """Try to connect to a specific neighbor"""
        try:
            if "host" not in neighbor_info or "port" not in neighbor_info:
                self.logger.warning(f"Missing address info for neighbor {neighbor_id}")
                return
                
            neighbor_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            neighbor_socket.settimeout(2)  # 2 second timeout
            neighbor_socket.connect((neighbor_info["host"], neighbor_info["port"]))
            neighbor_socket.settimeout(None)  # Remove timeout after connection
            
            self.active_connections[neighbor_id] = neighbor_socket
            self.logger.info(f"[CONNECTED] to neighbor {Colors.BOLD}{neighbor_id}{Colors.ENDC}")
            
            # For LSR, explicitly notify the routing algorithm about the connection
            if isinstance(self.routing_algorithm, LinkStateRouting):
                self.logger.info(f"LSR: Notifying algorithm about connected neighbor {neighbor_id}")
                self.routing_algorithm.update_neighbor(neighbor_id, {"cost": 1})
            
            # Start a thread to handle this neighbor connection
            neighbor_thread = threading.Thread(
                target=self._handle_neighbor_connection,
                args=(neighbor_socket, neighbor_id)
            )
            neighbor_thread.daemon = True
            neighbor_thread.start()
            
        except Exception as e:
            self.logger.warning(f"Could not connect to neighbor {neighbor_id}: {e}")
    
    def _handle_neighbor_connection(self, neighbor_socket: socket.socket, neighbor_id: str):
        """Handle ongoing communication with a neighbor"""
        try:
            while self.running:
                data = neighbor_socket.recv(4096)
                if not data:
                    break
                
                try:
                    packet = Packet.from_json(data.decode('utf-8'))
                    self._log_packet("RECEIVED", packet, neighbor_id)
                    
                    # For LSR, we need to ensure the routing algorithm knows about this neighbor
                    if isinstance(self.routing_algorithm, LinkStateRouting):
                        # Update neighbor as alive when we receive packets from them
                        if neighbor_id not in self.routing_algorithm.neighbor_states:
                            self.logger.info(f"LSR: Discovered active neighbor {neighbor_id}")
                            self.routing_algorithm.update_neighbor(neighbor_id, {"cost": 1})
                    
                    self._process_packet(packet, neighbor_id)
                except Exception as e:
                    self.logger.error(f"Error processing packet from {neighbor_id}: {e}")
        except Exception as e:
            if self.running:
                self.logger.warning(f"Lost connection to neighbor {neighbor_id}: {e}")
        finally:
            if neighbor_id in self.active_connections:
                del self.active_connections[neighbor_id]
                self.logger.info(f"[DISCONNECTED] from neighbor {Colors.BOLD}{neighbor_id}{Colors.ENDC}")
            neighbor_socket.close()
    
    def _retry_connections(self):
        """Periodically retry failed connections"""
        while self.running:
            time.sleep(15)  # Retry every 15 seconds
            for neighbor_id, neighbor_info in self.neighbors.items():
                if neighbor_id not in self.active_connections:
                    self._try_connect_neighbor(neighbor_id, neighbor_info)
    
    def _flood_packet(self, packet: Packet, exclude_neighbor_id: Optional[str]):
        """Flood packet to all neighbors except the specified neighbor_id"""
        self._ensure_msg_id(packet)
        for neighbor_id, neighbor_socket in self.active_connections.items():
            if exclude_neighbor_id and neighbor_id == exclude_neighbor_id:
                continue
            try:
                neighbor_socket.send(packet.to_json().encode('utf-8'))
                self._log_packet("FLOODED", packet, neighbor_id)
            except Exception as e:
                self.logger.error(f"Error flooding to {neighbor_id}: {e}")
    
    def _flood_packet_except_sender(self, packet: Packet, exclude_neighbor_id: Optional[str]):
        """Flood packet to all neighbors except the sender (by neighbor_id)"""
        self._ensure_msg_id(packet)
        flooded_count = 0
        for neighbor_id, neighbor_socket in self.active_connections.items():
            if exclude_neighbor_id and neighbor_id == exclude_neighbor_id:
                continue
            try:
                neighbor_socket.send(packet.to_json().encode('utf-8'))
                flooded_count += 1
            except Exception as e:
                self.logger.error(f"Error flooding LSA to {neighbor_id}: {e}")

        if flooded_count > 0:
            self._log_packet("FLOODED", packet, f"{flooded_count} neighbors")
    
    def _send_to_neighbor(self, packet: Packet, neighbor_id: str):
        """Send packet to specific neighbor"""
        self._ensure_msg_id(packet)
        if neighbor_id in self.active_connections:
            try:
                self.active_connections[neighbor_id].send(packet.to_json().encode('utf-8'))
                self._log_packet("FORWARDED", packet, neighbor_id)
            except Exception as e:
                self.logger.error(f"Error sending to {neighbor_id}: {e}")
    
    def _broadcast_packet(self, packet: Packet):
        """Broadcast packet to all active neighbors"""
        self._ensure_msg_id(packet)
        for neighbor_id, neighbor_socket in self.active_connections.items():
            try:
                neighbor_socket.send(packet.to_json().encode('utf-8'))
            except Exception as e:
                self.logger.error(f"Error broadcasting to {neighbor_id}: {e}")
    
    def _show_neighbors(self):
        """Show neighbor status"""
        print(f"{Colors.BOLD}Neighbors:{Colors.ENDC}")
        for neighbor_id, info in self.neighbors.items():
            status = f"{Colors.GREEN}Connected{Colors.ENDC}" if neighbor_id in self.active_connections else f"{Colors.RED}Disconnected{Colors.ENDC}"
            host_port = f"{info.get('host', 'unknown')}:{info.get('port', 'unknown')}"
            print(f"  {Colors.YELLOW}{neighbor_id}{Colors.ENDC}: {host_port} ({status})")
    
    def stop(self):
        """Stop the router"""
        self.running = False
        if self.socket:
            self.socket.close()
        for connection in self.active_connections.values():
            connection.close()
        self.logger.info(f"Router {self.router_id} stopped")

class RedisRouter(BaseRouter):
    """Redis pub/sub-based router implementation"""
    
    def __init__(self, router_id: str, redis_host: str, redis_port: int, redis_password: str, algorithm: str):
        super().__init__(router_id, algorithm)
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_password = redis_password
        self.redis_client = None
        self.pubsub = None
        self.node_channels = {}  # Node ID -> channel mapping
        self.my_channel = None
        self.subscribed_channels = set()
        self.reader_task = None
        self.user_input_task = None
        self.periodic_task = None
        self.event_loop = None
    
    def load_node_channels(self, names_file: str):
        """Load node channel mappings from Redis names JSON file"""
        try:
            with open(names_file, 'r') as f:
                names_data = json.load(f)
                if names_data.get("type") != "names":
                    raise ValueError("Invalid names file format")
                
                self.node_channels = names_data["config"]
                
                # Set my channel
                if self.router_id in self.node_channels:
                    self.my_channel = self.node_channels[self.router_id]["channel"]
                else:
                    raise ValueError(f"Router ID '{self.router_id}' not found in names file")
                
                # Update neighbor information with channels
                for neighbor_id in self.neighbors:
                    if neighbor_id in self.node_channels:
                        channel_info = self.node_channels[neighbor_id]
                        self.neighbors[neighbor_id] = {
                            "channel": channel_info["channel"],
                            "cost": 1  # Default cost
                        }
                        # Update routing algorithm with complete neighbor info
                        self.routing_algorithm.update_neighbor(neighbor_id, self.neighbors[neighbor_id])
                
                self.logger.info(f"Loaded channels for {len(self.node_channels)} nodes")
                self.logger.info(f"My channel: {self.my_channel}")
                
                # Trigger initial routing calculation for LSR and Dijkstra
                if isinstance(self.routing_algorithm, (DijkstraAlgorithm, LinkStateRouting)):
                    print(f"🔧 Triggering initial routing calculation for {self.router_id}")
                    
        except Exception as e:
            self.logger.error(f"Error loading node channels: {e}")
            raise
    
    def load_node_addresses(self, names_file: str):
        """For Redis router, this is equivalent to load_node_channels"""
        self.load_node_channels(names_file)

    @staticmethod
    def _ensure_msg_id(packet):
        """Ensure a stable unique id in packet.headers['msg_id'] for duplicate filtering."""
        if not getattr(packet, "headers", None) or not isinstance(packet.headers, dict):
            packet.headers = {}
        if "msg_id" not in packet.headers:
            packet.headers["msg_id"] = uuid.uuid4().hex

    def _mark_rx_seen(self, packet) -> bool:
        """Return True if packet.headers['msg_id'] was already seen (duplicate)."""
        try:
            mid = packet.headers.get("msg_id")
        except Exception:
            mid = None
        if not mid:
            return False
        if mid in self._rx_seen_ids:
            return True
        if len(self._rx_seen_fifo) >= self._rx_seen_capacity:
            old = self._rx_seen_fifo.popleft()
            self._rx_seen_ids.discard(old)
        self._rx_seen_fifo.append(mid)
        self._rx_seen_ids.add(mid)
        return False

    
    async def start(self):
        """Start the Redis router"""
        self.running = True
        self.event_loop = asyncio.get_event_loop()
        
        try:
            # Connect to Redis
            self.redis_client = redis.Redis(
                host=self.redis_host, 
                port=self.redis_port, 
                password=self.redis_password,
                decode_responses=True
            )
            
            # Test connection
            await self.redis_client.ping()
            self.logger.info(f"Connected to Redis at {self.redis_host}:{self.redis_port}")
            
            # Set up pub/sub
            self.pubsub = self.redis_client.pubsub()
            
            # Subscribe to my channel (for direct messages TO me)
            channels_to_subscribe = [self.my_channel]
            
            # For LSR and flooding, we also need to listen to neighbor channels for broadcasts
            # But we need to be careful about loops
            if isinstance(self.routing_algorithm, LinkStateRouting) or \
               isinstance(self.routing_algorithm, FloodingAlgorithm):
                for neighbor_info in self.neighbors.values():
                    if "channel" in neighbor_info:
                        neighbor_channel = neighbor_info["channel"]
                        if neighbor_channel != self.my_channel:  # Don't double-subscribe
                            channels_to_subscribe.append(neighbor_channel)
            
            # Remove duplicates
            channels_to_subscribe = list(set(channels_to_subscribe))
            
            await self.pubsub.subscribe(*channels_to_subscribe)
            self.subscribed_channels = set(channels_to_subscribe)
            
            self.logger.info(f"Router {Colors.BOLD}{self.router_id}{Colors.ENDC} started with Redis pub/sub")
            self.logger.info(f"Using routing algorithm: {Colors.BOLD}{self.routing_algorithm.get_name()}{Colors.ENDC}")
            self.logger.info(f"Subscribed to channels: {channels_to_subscribe}")
            
            # Start message reader task
            self.reader_task = asyncio.create_task(self._message_reader())
            
            # Start periodic tasks
            self.periodic_task = asyncio.create_task(self._async_periodic_tasks())
            
            # Start user input handler in a separate thread (since input() is blocking)
            input_thread = threading.Thread(target=self._handle_user_input)
            input_thread.daemon = True
            input_thread.start()
            
            # Wait for tasks to complete
            await asyncio.gather(self.reader_task, self.periodic_task)
            
        except Exception as e:
            self.logger.error(f"Error starting Redis router: {e}")
            raise
    
    async def _message_reader(self):
        """Read messages from Redis pub/sub"""
        try:
            while self.running:
                message = await self.pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is not None:
                    try:
                        # Parse the packet
                        packet_data = message['data']
                        packet = Packet.from_json(packet_data)
                        
                        # Skip packets that we sent ourselves to avoid loops
                        if packet.from_addr == self.router_id:
                            continue
                        
                        # Determine which neighbor sent this (if any)
                        from_neighbor_id = self._get_neighbor_by_channel(message['channel'])
                        
                        # If we received this on our own channel, it means someone sent it TO us
                        # If we received this on a neighbor's channel, it's a broadcast/multicast
                        if message['channel'] == self.my_channel:
                            # Direct message to us
                            self._log_packet("RECEIVED", packet, from_neighbor_id)
                            self._process_packet(packet, from_neighbor_id)
                        else:
                            # Broadcast/multicast message from a neighbor
                            # Only process if it's truly from that neighbor (not a loop)
                            if from_neighbor_id and packet.from_addr != self.router_id:
                                self._log_packet("RECEIVED", packet, from_neighbor_id)
                                self._process_packet(packet, from_neighbor_id)
                        
                    except Exception as e:
                        self.logger.error(f"Error processing message: {e}")
                        
        except asyncio.CancelledError:
            self.logger.info("Message reader cancelled")
        except Exception as e:
            self.logger.error(f"Error in message reader: {e}")
    
    async def _async_periodic_tasks(self):
        """Handle periodic tasks asynchronously"""
        try:
            while self.running:
                await asyncio.sleep(5)  # Check every 5 seconds
                
                try:
                    # Send hello packets for LSR
                    if isinstance(self.routing_algorithm, LinkStateRouting):
                        if self.routing_algorithm.should_send_hello():
                            hello_packet = self.routing_algorithm.create_hello_packet()
                            await self._broadcast_packet_async(hello_packet)
                            self._log_packet("SENT", hello_packet, "multicast")
                        
                        # Send LSA if needed
                        if self.routing_algorithm.should_send_lsa():
                            lsa_packet = self.routing_algorithm.create_lsa_packet()
                            await self._broadcast_packet_async(lsa_packet)
                            self._log_packet("SENT", lsa_packet, "broadcast")
                        
                        # Check for dead neighbors and age LSA database
                        try:
                            self.routing_algorithm._check_neighbor_timeouts()
                        except Exception as e:
                            self.logger.error(f"Error checking neighbor timeouts: {e}")
                        
                        try:
                            self.routing_algorithm._age_lsa_database()
                        except Exception as e:
                            self.logger.error(f"Error aging LSA database: {e}")
                    
                    # For Dijkstra algorithm - no periodic packets needed
                    elif isinstance(self.routing_algorithm, DijkstraAlgorithm):
                        pass
                    
                    # Send basic hello for flooding (neighbor discovery)
                    else:
                        hello_packet = Packet(
                            proto=self.routing_algorithm.get_name(),
                            packet_type="hello",
                            from_addr=self.router_id,
                            to_addr="broadcast",
                            payload=f"Hello from {self.router_id}"
                        )
                        await self._broadcast_packet_async(hello_packet)
                        
                except Exception as e:
                    self.logger.error(f"Error in periodic tasks: {e}")
                    import traceback
                    self.logger.error(f"Traceback: {traceback.format_exc()}")
                    
        except asyncio.CancelledError:
            self.logger.info("Periodic tasks cancelled")
        except Exception as e:
            self.logger.error(f"Error in periodic tasks: {e}")
    
    def _get_neighbor_by_channel(self, channel: str) -> Optional[str]:
        """Get neighbor ID by channel name"""
        for neighbor_id, neighbor_info in self.neighbors.items():
            if neighbor_info.get("channel") == channel:
                return neighbor_id
        return None
    
    def _flood_packet(self, packet: Packet, exclude_neighbor_id: Optional[str]):
        """Flood packet to all neighbors except the specified neighbor_id"""
        self._schedule_async_task(self._flood_packet_async(packet, exclude_neighbor_id))
    
    async def _flood_packet_async(self, packet: Packet, exclude_neighbor_id: Optional[str]):
        """Async version of flood packet"""
        self._ensure_msg_id(packet)
        flooded_count = 0
        
        for neighbor_id, neighbor_info in self.neighbors.items():
            if exclude_neighbor_id and neighbor_id == exclude_neighbor_id:
                continue
            
            if "channel" in neighbor_info:
                try:
                    # For flooding, send to each neighbor's channel individually
                    await self.redis_client.publish(neighbor_info["channel"], packet.to_json())
                    flooded_count += 1
                except Exception as e:
                    self.logger.error(f"Error flooding to {neighbor_id}: {e}")
        
        if flooded_count > 0:
            self._log_packet("FLOODED", packet, f"{flooded_count} neighbors")
    
    def _flood_packet_except_sender(self, packet: Packet, exclude_neighbor_id: Optional[str]):
        """Flood packet to all neighbors except the sender (by neighbor_id)"""
        self._schedule_async_task(self._flood_packet_async(packet, exclude_neighbor_id))
    
    def _send_to_neighbor(self, packet: Packet, neighbor_id: str):
        """Send packet to specific neighbor"""
        self._schedule_async_task(self._send_to_neighbor_async(packet, neighbor_id))
    
    async def _send_to_neighbor_async(self, packet: Packet, neighbor_id: str):
        """Async version of send to neighbor"""
        self._ensure_msg_id(packet)
        
        if neighbor_id in self.neighbors and "channel" in self.neighbors[neighbor_id]:
            try:
                # For direct messages, send to the neighbor's channel (they listen on their own channel)
                channel = self.neighbors[neighbor_id]["channel"]
                await self.redis_client.publish(channel, packet.to_json())
                self._log_packet("FORWARDED", packet, neighbor_id)
            except Exception as e:
                self.logger.error(f"Error sending to {neighbor_id}: {e}")
        else:
            self.logger.warning(f"No channel found for neighbor {neighbor_id}")
    
    def _broadcast_packet(self, packet: Packet):
        """Broadcast packet to all active neighbors"""
        self._schedule_async_task(self._broadcast_packet_async(packet))
    
    async def _broadcast_packet_async(self, packet: Packet):
        """Async version of broadcast packet"""
        self._ensure_msg_id(packet)
        
        for neighbor_id, neighbor_info in self.neighbors.items():
            if "channel" in neighbor_info:
                try:
                    await self.redis_client.publish(neighbor_info["channel"], packet.to_json())
                except Exception as e:
                    self.logger.error(f"Error broadcasting to {neighbor_id}: {e}")
    
    def _show_neighbors(self):
        """Show neighbor status"""
        print(f"{Colors.BOLD}Neighbors:{Colors.ENDC}")
        for neighbor_id, info in self.neighbors.items():
            channel = info.get('channel', 'unknown')
            # For Redis, we can't easily determine if a neighbor is "connected"
            # since pub/sub is connectionless, so we just show the channel
            status = f"{Colors.CYAN}Channel: {channel}{Colors.ENDC}"
            print(f"  {Colors.YELLOW}{neighbor_id}{Colors.ENDC}: {status}")
    
    def stop(self):
        """Stop the router"""
        self.running = False
        
        # Cancel async tasks
        if self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()
        
        if self.periodic_task and not self.periodic_task.done():
            self.periodic_task.cancel()
        
        # Close Redis connections using the same async scheduling mechanism
        if self.pubsub:
            self._schedule_async_task(self._cleanup_redis())
        
        self.logger.info(f"Redis router {self.router_id} stopped")
    
    async def _cleanup_redis(self):
        """Clean up Redis connections"""
        try:
            if self.pubsub:
                await self.pubsub.unsubscribe()
                await self.pubsub.close()
            
            if self.redis_client:
                await self.redis_client.close()
        except Exception as e:
            self.logger.error(f"Error cleaning up Redis connections: {e}")
    
    def _schedule_async_task(self, coro):
        """Schedule an async task from sync context"""
        if self.event_loop and self.event_loop.is_running():
            try:
                # Create task in the running loop
                future = asyncio.run_coroutine_threadsafe(coro, self.event_loop)
                # For cleanup operations, we might want to wait a bit
                if hasattr(coro, '__name__') and 'cleanup' in str(coro):
                    try:
                        future.result(timeout=2.0)  # Wait up to 2 seconds for cleanup
                    except Exception as e:
                        self.logger.warning(f"Cleanup operation timed out or failed: {e}")
            except Exception as e:
                self.logger.error(f"Error scheduling async task: {e}")
        else:
            self.logger.warning("No event loop available to schedule async task")


    def _handle_user_input(self):
        """Simple CLI for Redis mode: send/echo/inspect/quit"""
        print(f"\n{Colors.BOLD}Router {self.router_id} (Redis) ready.{Colors.ENDC} Commands:")
        print(f"  {Colors.CYAN}send <destination> <message>{Colors.ENDC} - Send message to destination")
        print(f"  {Colors.CYAN}echo <destination>{Colors.ENDC} - Send echo to destination")
        print(f"  {Colors.CYAN}neighbors{Colors.ENDC} - Show neighbors")
        print(f"  {Colors.CYAN}routes{Colors.ENDC} - Show routing table")
        print(f"  {Colors.CYAN}logs{Colors.ENDC} - Show packet logs")
        print(f"  {Colors.CYAN}topology{Colors.ENDC} - Show network topology")
        print(f"  {Colors.CYAN}quit{Colors.ENDC} - Exit router")

        while self.running:
            try:
                cmd = input(f"\n{Colors.BOLD}{self.router_id}>{Colors.ENDC} ").strip().split()
                if not cmd:
                    continue

                if cmd[0] == "send" and len(cmd) >= 3:
                    destination = cmd[1]
                    message = " ".join(cmd[2:])
                    packet = Packet(
                        proto=self.routing_algorithm.get_name(),
                        packet_type="message",
                        from_addr=self.router_id,
                        to_addr=destination,
                        ttl=5,
                        headers=[self.router_id],
                        payload=message
                    )
                    self._forward_packet(packet)
                    self._log_packet("SENT", packet)

                elif cmd[0] == "echo" and len(cmd) >= 2:
                    destination = cmd[1]
                    packet = Packet(
                        proto=self.routing_algorithm.get_name(),
                        packet_type="echo",
                        from_addr=self.router_id,
                        to_addr=destination,
                        ttl=5,
                        headers=[self.router_id],
                        payload="Echo request"
                    )
                    self._forward_packet(packet)
                    self._log_packet("SENT", packet)

                elif cmd[0] == "neighbors":
                    print(f"{Colors.BOLD}Neighbors:{Colors.ENDC}")
                    # Usa la topología cargada para listar vecinos
                    neighs = self.topology.get(self.router_id, [])
                    for nid in neighs:
                        ch = self.node_channels.get(nid, {}).get("channel", "unknown")
                        print(f"  {Colors.YELLOW}{nid}{Colors.ENDC}: channel={ch}")

                elif cmd[0] == "routes":
                    print(f"{Colors.BOLD}Routing table:{Colors.ENDC}")
                    for dest, next_hop in self.routing_algorithm.routing_table.items():
                        print(f"  {Colors.YELLOW}{dest}{Colors.ENDC} -> {Colors.CYAN}{next_hop}{Colors.ENDC}")

                elif cmd[0] == "topology":
                    print(f"{Colors.BOLD}Network topology:{Colors.ENDC}")
                    for node, neighs in self.topology.items():
                        status = f"{Colors.GREEN}(this node){Colors.ENDC}" if node == self.router_id else ""
                        print(f"  {Colors.YELLOW}{node}{Colors.ENDC}{status}: {neighs}")

                elif cmd[0] == "logs":
                    print(f"{Colors.BOLD}Recent packet logs:{Colors.ENDC}")
                    for log_entry in self.packet_log[-10:]:
                        print(f"  {log_entry}")

                elif cmd[0] == "quit":
                    self.stop()
                    break

                else:
                    print(f"{Colors.RED}Unknown command{Colors.ENDC}")

            except KeyboardInterrupt:
                self.stop()
                break
            except Exception as e:
                print(f"{Colors.RED}Error: {e}{Colors.ENDC}")

        