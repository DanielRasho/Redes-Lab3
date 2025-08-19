import json
import time
import argparse
import sys

from src.router import Router
from src.utils import setup_colored_logging, Colors

setup_colored_logging()


def main():
    parser = argparse.ArgumentParser(description="Router Network Simulation")
    parser.add_argument("--id", required=True, help="Router ID")
    parser.add_argument("--algorithm", choices=["flooding", "dijkstra", "lsr"], 
                       required=True, help="Routing algorithm")
    parser.add_argument("--topo", required=True, help="Topology configuration file")
    parser.add_argument("--names", required=True, help="Node names and addresses file")
    
    args = parser.parse_args()
    
    # Load configuration files
    try:
        # Load node addresses first to get this router's address
        with open(args.names, 'r') as f:
            names_data = json.load(f)
            if names_data.get("type") != "names":
                raise ValueError("Invalid names file format")
            
            node_addresses = names_data["config"]
            if args.id not in node_addresses:
                raise ValueError(f"Router ID '{args.id}' not found in names file")
            
            router_addr = node_addresses[args.id]
            host = router_addr.get("host", "localhost")
            port = router_addr["port"]
        
        # Create router
        router = Router(args.id, host, port, args.algorithm)
        
        # Load topology and names
        router.load_topology(args.topo)
        router.load_node_addresses(args.names)
        
    except Exception as e:
        print(f"{Colors.RED}Configuration error: {e}{Colors.ENDC}")
        sys.exit(1)
    
    try:
        router.start()
        
        # Keep main thread alive
        while router.running:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Shutting down router {args.id}...{Colors.ENDC}")
        router.stop()

if __name__ == "__main__":
    main()