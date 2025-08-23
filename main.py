import json
import time
import argparse
import sys
import asyncio
import redis.asyncio as redis

from src.router import RedisRouter, SocketRouter
from src.utils import setup_colored_logging, Colors

setup_colored_logging()

def main():
    parser = argparse.ArgumentParser(description="Router Network Simulation")
    parser.add_argument("--id", required=True, help="Router ID")
    parser.add_argument("--algorithm", choices=["flooding", "dijkstra", "lsr"], 
                       required=True, help="Routing algorithm")
    parser.add_argument("--topo", required=True, help="Topology configuration file")
    parser.add_argument("--names", required=True, help="Node names and addresses file")
    parser.add_argument("--mode", choices=["socket", "redis"], default="redis",
                       help="Communication mode: socket or redis")
    
    args = parser.parse_args()

    if args.mode == "redis":
        asyncio.run(main_redis(args))
    else:
        asyncio.run(main_socket(args))

async def main_redis(args):
    """Main function for Redis mode"""
    router = None
    try:
        # Load Redis configuration from names file
        with open(args.names, 'r') as f:
            names_data = json.load(f)
            if names_data.get("type") != "names":
                raise ValueError("Invalid names file format")
            
            redis_host = names_data.get("host", "localhost")
            redis_port = names_data.get("port", 6379)
            redis_password = names_data.get("pwd", "")
            
            if args.id not in names_data["config"]:
                raise ValueError(f"Router ID '{args.id}' not found in names file")
        
        # Create Redis router
        router = RedisRouter(args.id, redis_host, redis_port, redis_password, args.algorithm)
        
        # Load topology and channels
        router.load_topology(args.topo)
        router.load_node_channels(args.names)
        
        # Start router
        await router.start()
            
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Shutting down Redis router {args.id}...{Colors.ENDC}")
        if router:
            router.stop()
            # Give a moment for cleanup
            await asyncio.sleep(1)
    except Exception as e:
        print(f"{Colors.RED}Configuration error: {e}{Colors.ENDC}")
        if router:
            router.stop()
        sys.exit(1)

async def main_socket(args):
    """Main function for Socket mode"""
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
        
        # Create socket router
        router = SocketRouter(args.id, host, port, args.algorithm)
        
        # Load topology and names
        router.load_topology(args.topo)
        router.load_node_addresses(args.names)
        
        # Start router
        await router.start()
        
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Shutting down socket router {args.id}...{Colors.ENDC}")
        router.stop()
    except Exception as e:
        print(f"{Colors.RED}Configuration error: {e}{Colors.ENDC}")
        sys.exit(1)

if __name__ == "__main__":
    main()