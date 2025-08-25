import json
import time
import uuid
import threading
from typing import Dict, Optional, Any, Tuple, Set
from collections import defaultdict, deque
from src.algorithms.base import RoutingAlgorithm
from src.packet import Packet


class LinkStateRouting(RoutingAlgorithm):
    """
    Link State Routing (OSPF simplificado) para entorno pub/sub.
    Componentes:
      - HELLO periódicos: detectar/actualizar estado de vecinos directos.
      - INFO (LSA): secuencia + envejecimiento en una LSDB (link_state_db).
      - SPF (Dijkstra): arma routing_table con next-hop por destino.
      - Flood controlado: router.py decide el envío cuando devolvemos "flood_lsa".
    
    Estándares:
      - Mensajes:
        HELLO:  proto="lsr", type="hello", to="broadcast" (no retransmitir)
        INFO :  proto="lsr", type="info",  to="broadcast" (retransmitir y TTL--)
        MESSAGE/ECHO: unicast, se reenvía con tabla SPF.
      - headers.path = últimos 3 routers (ventana de 3) para detectar loops.
      - headers.msg_id = id único para deduplicación (router.py mantiene LRU).
    """

    # Timers (segundos)
    HELLO_INTERVAL = 5.0
    NEIGHBOR_TIMEOUT = 20.0
    LSA_MIN_INTERVAL = 8.0
    LSA_REFRESH_INTERVAL = 30.0
    LSA_MAX_AGE = 90.0

    def __init__(self, router_id: str):
        super().__init__(router_id)

        # Estado de vecinos: nb -> {cost,last_seen,alive}
        self.neighbor_states: Dict[str, Dict[str, Any]] = {}
        # Base de estado de enlaces (LSDB): origin -> {seq, neighbors{}, last_received}
        self.link_state_db: Dict[str, Dict[str, Any]] = {}
        # Conjunto de routers "vistos" en el área
        self.area_routers: Set[str] = set([router_id])

        # Control LSA propio
        self.my_lsa_seq: int = 0
        self.last_lsa_time: float = 0.0
        self.topology_changed: bool = True
        self.last_hello_time: float = 0.0

        # Filtro de duplicados de LSA (origin, seq)
        self.lsa_seen: Set[Tuple[str, int]] = set()
        self.lsa_fifo: deque[Tuple[str, int]] = deque()
        self.lsa_capacity: int = 50000

        # Concurrencia
        self._lock = threading.RLock()
