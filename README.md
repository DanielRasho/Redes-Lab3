# Lab 3 - Routers simulation

## Dependencies

```bash
pip install redis
```

## How to run

Start a router node is as easy as **3 steps**:

1. Define the connection between nodes through a `topo.json` file:

```json
{
  "type": "topo",
  "config": {
    "A": ["B", "D"],
    "B": ["A", "C"],
    "C": ["B", "D"],
    "D": ["A", "C"]
  }
}
```

2. Define the ports and hosts where the each nodes is will be listening events through a names.json:

```json
{
  "type": "names",
  "config": {
    "A": {"host": "localhost", "port": 8001},
    "B": {"host": "localhost", "port": 8002}, 
    "C": {"host": "localhost", "port": 8003},
    "D": {"host": "localhost", "port": 8004}
  }
}
```

3. Start a node by passing the right arguments!

```bash
python c.py --id A --algorithm flooding --topo topo.json --names names.json
```

## Project Structure

```bash
.
├── main.py             # entrypoing
├── README.md
├── src
│   ├── algorithms      # Definitions of routing algorithms
│   │   ├── base.py
│   │   ├── dijkstra.py
│   │   ├── flooding.py
│   │   └── lsr.py
│   ├── packet.py       # Definition of packet
│   ├── router.py       # Main router logic
│   └── utils.py        # logging utilities
├── names.json
└── topo.json
```
