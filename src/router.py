import socket
import threading
import logging
import json
import uuid
from collections import deque

from datetime import datetime
import time
from src.algorithms.dijkstra import DijkstraAlgorithm
from src.algorithms.flooding import FloodingAlgorithm
from src.algorithms.lsr import LinkStateRouting
from src.algorithms.base import RoutingAlgorithm
from src.packet import Packet
from src.utils import Colors
from typing import Dict, List, Optional, Tuple, Any

class Router:
    """Main router class that handles packet forwarding and communication"""
    
    def __init__(self, router_id: str, host: str, port: int, algorithm: str):
        self.router_id = router_id
        self.host = host
        self.port = port
        self.socket = None
        self.topology = {}  # Full network topology from config
        self.node_addresses = {}  # Node ID -> {host, port} mapping
        self.neighbors = {}  # Direct neighbors from topology
        self.active_connections = {}  # neighbor_id -> socket
        self.running = False
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
    
    def add_neighbor(self, neighbor_id: str, host: str, port: int, cost: int = 1):
        """Add a neighbor router (kept for backward compatibility)"""
        self.neighbors[neighbor_id] = {"host": host, "port": port, "cost": cost}
        self.routing_algorithm.update_neighbor(neighbor_id, {"cost": cost})
        self.logger.info(f"Added neighbor {neighbor_id} at {host}:{port}")
    
    def start(self):
        """Start the router"""
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
        
        # Send hello packets periodically
        hello_thread = threading.Thread(target=self._send_hello_packets)
        hello_thread.daemon = True
        hello_thread.start()
        
        # Retry connections periodically
        retry_thread = threading.Thread(target=self._retry_connections)
        retry_thread.daemon = True
        retry_thread.start()
    
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
        try:
            while self.running:
                data = client_socket.recv(4096)
                if not data:
                    break
                
                try:
                    packet = Packet.from_json(data.decode('utf-8'))
                    self._log_packet("RECEIVED", packet)
                    self._process_packet(packet, client_socket, None)
                except Exception as e:
                    self.logger.error(f"Error processing packet: {e}")
        except Exception as e:
            self.logger.error(f"Error handling client: {e}")
        finally:
            client_socket.close()
    
    def _process_packet(self, packet: Packet, from_socket: socket.socket, from_neighbor_id: Optional[str]):
        """Process an incoming packet"""
        if packet.proto == "flooding":
            if self._mark_rx_seen(packet):
                return

        # Broadcast/Multicast: LSR necesita multi-salto y conocer el vecino real
        if packet.to_addr in ["broadcast", "multicast"]:
            neighbor_hint = from_neighbor_id if from_neighbor_id else "unknown"
            decision = self.routing_algorithm.process_packet(packet, neighbor_hint)

            if decision == "flood":
                if not packet.decrement_ttl():
                    self.logger.warning(f"[DROPPED] Broadcast TTL expired")
                    return
                self._flood_packet(packet, exclude_neighbor_id=from_neighbor_id)
                return

            if decision == "flood_lsa":
                if not packet.decrement_ttl():
                    self.logger.warning(f"[DROPPED] LSA TTL expired")
                    return
                self._flood_packet_except_sender(packet, exclude_neighbor_id=from_neighbor_id)
            return

        # Check if packet is for this router
        if packet.to_addr == self.router_id:
            if packet.type == "message":
                self.logger.info(f"Message received: {packet.payload}")
                print(f"\n{Colors.GREEN}[MESSAGE FROM {packet.from_addr}]: {packet.payload}{Colors.ENDC}")
            elif packet.type == "echo":
                reply = Packet(
                    proto=packet.proto,
                    packet_type="echo_reply",
                    from_addr=self.router_id,
                    to_addr=packet.from_addr,
                    payload=f"Echo reply from {self.router_id}"
                )
                self._forward_packet(reply)
            elif packet.type == "echo_reply":
                print(f"\n{Colors.BLUE}[ECHO REPLY FROM {packet.from_addr}]: {packet.payload}{Colors.ENDC}")
            return

        # Check TTL
        if not packet.decrement_ttl():
            self.logger.warning(f"[DROPPED] Packet TTL expired")
            return

        # Forward packet using routing algorithm
        next_hop = self.routing_algorithm.process_packet(packet, from_neighbor_id or "unknown")

        if next_hop == "flood":
            self._flood_packet(packet, exclude_neighbor_id=from_neighbor_id)
        elif next_hop == "flood_lsa":
            self._flood_packet_except_sender(packet, exclude_neighbor_id=from_neighbor_id)
        elif next_hop:
            self._send_to_neighbor(packet, next_hop)
        else:
            self.logger.warning(f"[DROPPED] No route to destination {packet.to_addr}")

    
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


        
    def _send_to_neighbor(self, packet: Packet, neighbor_id: str):
        """Send packet to specific neighbor"""
        self._ensure_msg_id(packet)
        if neighbor_id in self.active_connections:
            try:
                self.active_connections[neighbor_id].send(packet.to_json().encode('utf-8'))
                self._log_packet("FORWARDED", packet, neighbor_id)
            except Exception as e:
                self.logger.error(f"Error sending to {neighbor_id}: {e}")
    
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
                    self._process_packet(packet, neighbor_socket, neighbor_id)
                except Exception as e:
                    self.logger.error(f"Error processing packet from {neighbor_id}: {e}")
        except Exception as e:
            if self.running:
                self.logger.warning(f"Lost connection to neighbor {neighbor_id}: {e}")
        finally:
            if neighbor_id in self.active_connections:
                del self.active_connections[neighbor_id]
            neighbor_socket.close()
    
    def _retry_connections(self):
        """Periodically retry failed connections"""
        while self.running:
            time.sleep(15)  # Retry every 15 seconds
            for neighbor_id, neighbor_info in self.neighbors.items():
                if neighbor_id not in self.active_connections:
                    self._try_connect_neighbor(neighbor_id, neighbor_info)
    
    def _send_hello_packets(self):
        """Send hello packets and LSAs periodically"""
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
                    
                    # Check for dead neighbors and age LSA database (with error handling)
                    try:
                        self.routing_algorithm._check_neighbor_timeouts()
                    except Exception as e:
                        self.logger.error(f"Error checking neighbor timeouts: {e}")
                    
                    try:
                        self.routing_algorithm._age_lsa_database()
                    except Exception as e:
                        self.logger.error(f"Error aging LSA database: {e}")
                
                # For Dijkstra algorithm - no periodic packets needed!
                elif isinstance(self.routing_algorithm, DijkstraAlgorithm):
                    # Dijkstra calculates paths once on startup, no periodic updates needed
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
                    self._broadcast_packet(hello_packet)
                    
            except Exception as e:
                self.logger.error(f"Error in periodic tasks: {e}")
                import traceback
                self.logger.error(f"Traceback: {traceback.format_exc()}")
    
    def _broadcast_packet(self, packet: Packet):
        """Broadcast packet to all active neighbors"""
        self._ensure_msg_id(packet)
        for neighbor_id, neighbor_socket in self.active_connections.items():
            try:
                neighbor_socket.send(packet.to_json().encode('utf-8'))
            except Exception as e:
                self.logger.error(f"Error broadcasting to {neighbor_id}: {e}")
    
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
                    self._forward_packet(packet)
                    self._log_packet("SENT", packet)
                
                elif command[0] == "echo" and len(command) >= 2:
                    destination = command[1]
                    packet = Packet(
                        proto=self.routing_algorithm.get_name(),
                        packet_type="echo",
                        from_addr=self.router_id,
                        to_addr=destination,
                        payload="Echo request"
                    )
                    self._forward_packet(packet)
                    self._log_packet("SENT", packet)
                
                elif command[0] == "neighbors":
                    print(f"{Colors.BOLD}Neighbors:{Colors.ENDC}")
                    for neighbor_id, info in self.neighbors.items():
                        status = f"{Colors.GREEN}Connected{Colors.ENDC}" if neighbor_id in self.active_connections else f"{Colors.RED}Disconnected{Colors.ENDC}"
                        host_port = f"{info.get('host', 'unknown')}:{info.get('port', 'unknown')}"
                        print(f"  {Colors.YELLOW}{neighbor_id}{Colors.ENDC}: {host_port} ({status})")
                
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
                    print(f"{Colors.BOLD}Routing Algorithm Debug Info:{Colors.ENDC}")
                    print(f"  Algorithm: {Colors.YELLOW}{self.routing_algorithm.get_name()}{Colors.ENDC}")
                    print(f"  Routing Table: {Colors.CYAN}{dict(self.routing_algorithm.routing_table)}{Colors.ENDC}")
                    print(f"  Neighbors: {Colors.CYAN}{list(self.routing_algorithm.neighbors.keys())}{Colors.ENDC}")
                    
                    if isinstance(self.routing_algorithm, DijkstraAlgorithm):
                        print(f"  Link State DB: {Colors.MAGENTA}{list(self.routing_algorithm.link_state_db.keys())}{Colors.ENDC}")
                        print(f"  Sequence Numbers: {Colors.MAGENTA}{self.routing_algorithm.sequence_numbers}{Colors.ENDC}")
                    elif isinstance(self.routing_algorithm, LinkStateRouting):
                        print(f"  LSA Database: {Colors.MAGENTA}{list(self.routing_algorithm.link_state_db.keys())}{Colors.ENDC}")
                        print(f"  Area Routers: {Colors.MAGENTA}{list(self.routing_algorithm.area_routers)}{Colors.ENDC}")
                        print(f"  Neighbor States: {Colors.MAGENTA}{list(self.routing_algorithm.neighbor_states.keys())}{Colors.ENDC}")
                
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
    
    def _log_packet(self, action: str, packet: Packet, neighbor: str = None):
        """Log packet activity (now also shows headers['msg_id'])"""
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

    
    def stop(self):
        """Stop the router"""
        self.running = False
        if self.socket:
            self.socket.close()
        for connection in self.active_connections.values():
            connection.close()
        self.logger.info(f"Router {self.router_id} stopped")

    @staticmethod
    def _ensure_msg_id(packet):
        """
        Ensure a stable unique id in packet.headers['msg_id'] for duplicate filtering.
        """
        if not getattr(packet, "headers", None) or not isinstance(packet.headers, dict):
            packet.headers = {}
        if "msg_id" not in packet.headers:
            packet.headers["msg_id"] = uuid.uuid4().hex

    def _mark_rx_seen(self, packet) -> bool:
        """
        Returns True if packet.headers['msg_id'] was already seen (duplicate).
        Thread-safe LRU (set + deque).
        """
        try:
            mid = packet.headers.get("msg_id")
        except Exception:
            mid = None
        if not mid:
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


