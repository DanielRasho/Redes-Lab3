import json
import time
import uuid
import threading
from typing import Dict, Optional, Any, List, Tuple, Set
from collections import defaultdict, deque

from src.algorithms.base import RoutingAlgorithm
from src.packet import Packet


class LinkStateRouting(RoutingAlgorithm):
    """
    Link State Routing (OSPF simplificado)
      - HELLO periódicos para detectar vecinos
      - LSA con secuencia y envejecimiento
      - LSDB + SPF (Dijkstra) para armar tabla
      - Flood controlado (lo ejecuta router.py cuando devolvemos "flood_lsa")
    """

    # Timers
    HELLO_INTERVAL = 5.0
    NEIGHBOR_TIMEOUT = 20.0
    LSA_MIN_INTERVAL = 8.0
    LSA_REFRESH_INTERVAL = 30.0
    LSA_MAX_AGE = 90.0

    def __init__(self, router_id: str):
        super().__init__(router_id)

        # Estado local
        self.neighbor_states: Dict[str, Dict[str, Any]] = {}   # nb -> {cost,last_seen,alive}
        self.link_state_db: Dict[str, Dict[str, Any]] = {}     # origin -> {seq, neighbors{}, last_received}
        self.area_routers: Set[str] = set([router_id])

        # Control LSA propio
        self.my_lsa_seq: int = 0
        self.last_lsa_time: float = 0.0
        self.topology_changed: bool = True
        self.last_hello_time: float = 0.0

        # Filtro de duplicados de LSA
        self.lsa_seen: Set[Tuple[str, int]] = set()
        self.lsa_fifo: deque[Tuple[str, int]] = deque()
        self.lsa_capacity: int = 50000

        # Acceso concurrente
        self._lock = threading.RLock()

    # API usada por router.py
    def get_name(self) -> str:
        return "lsr"

    def update_neighbor(self, neighbor_id: str, neighbor_info: Dict):
        cost = int(neighbor_info.get("cost", 1))
        now = time.time()
        with self._lock:
            st = self.neighbor_states.get(neighbor_id, {})
            st.update({"cost": cost, "last_seen": now, "alive": True})
            self.neighbor_states[neighbor_id] = st
            self.neighbors[neighbor_id] = {"cost": cost}
            self.topology_changed = True

    def process_packet(self, packet: Packet, from_neighbor: str) -> Optional[str]:
        # normaliza headers
        if not isinstance(getattr(packet, "headers", {}), dict):
            packet.headers = {}

        # HELLO: solo refresca vecino si sabemos quién lo envió
        if packet.type == "hello":
            if from_neighbor and from_neighbor != "unknown":
                with self._lock:
                    st = self.neighbor_states.get(from_neighbor, {"cost": 1})
                    st["last_seen"] = time.time()
                    st["alive"] = True
                    self.neighbor_states[from_neighbor] = st
                    self.neighbors[from_neighbor] = {"cost": st.get("cost", 1)}
            return None

        # LSA/INFO: valida, filtra duplicados, actualiza LSDB y pide flood
        if packet.type in ("lsa", "info"):
            try:
                data = json.loads(packet.payload)
            except Exception:
                return None

            origin_field = str(data.get("origin", packet.from_addr))
            if origin_field != packet.from_addr:
                # evitar spoofing de origen
                return None

            origin = packet.from_addr
            seq = int(data.get("seq", 0))
            neighs = data.get("neighbors", {})

            key = (origin, seq)
            with self._lock:
                if key in self.lsa_seen:
                    return None
                if len(self.lsa_fifo) >= self.lsa_capacity:
                    old = self.lsa_fifo.popleft()
                    self.lsa_seen.discard(old)
                self.lsa_fifo.append(key)
                self.lsa_seen.add(key)

                current = self.link_state_db.get(origin)
                if current and seq <= int(current.get("seq", -1)):
                    return None

                self.link_state_db[origin] = {
                    "seq": seq,
                    "neighbors": {str(k): int(v) for k, v in neighs.items()},
                    "last_received": time.time()
                }
                self.area_routers.update(
                    [origin, *self.link_state_db[origin]["neighbors"].keys(), self.router_id]
                )

                self.calculate_routes()

            return "flood_lsa"

        # Datos/Echo: usa tabla
        return self.get_next_hop(packet.to_addr)

    def get_next_hop(self, destination: str) -> Optional[str]:
        if destination == self.router_id:
            return None
        # lectura rápida; tabla se reemplaza atómica bajo lock
        return self.routing_table.get(destination)

    # Timers/creación de paquetes (router.py los llama)
    def should_send_hello(self) -> bool:
        return (time.time() - self.last_hello_time) >= self.HELLO_INTERVAL

    def create_hello_packet(self) -> Packet:
        self.last_hello_time = time.time()
        headers = {"msg_id": uuid.uuid4().hex, "ts": self.last_hello_time}
        return Packet(
            proto=self.get_name(),
            packet_type="hello",
            from_addr=self.router_id,
            to_addr="multicast",
            ttl=5,
            headers=headers,
            payload=f"HELLO {self.router_id}"
        )

    def should_send_lsa(self) -> bool:
        now = time.time()
        if self.topology_changed and (now - self.last_lsa_time) >= self.LSA_MIN_INTERVAL:
            return True
        if (now - self.last_lsa_time) >= self.LSA_REFRESH_INTERVAL:
            return True
        return False

    def create_lsa_packet(self) -> Packet:
        self.my_lsa_seq += 1
        self.last_lsa_time = time.time()
        self.topology_changed = False

        # Solo vecinos vivos al momento de emitir
        neighs: Dict[str, int] = {}
        now = time.time()
        with self._lock:
            for nb, st in self.neighbor_states.items():
                if st.get("alive", False) and (now - st.get("last_seen", 0)) < self.NEIGHBOR_TIMEOUT:
                    neighs[nb] = int(st.get("cost", 1))

            # --- Pre-instalar mi propio LSA en LSDB y filtro de duplicados ---
            self.link_state_db[self.router_id] = {
                "seq": self.my_lsa_seq,
                "neighbors": dict(neighs),
                "last_received": self.last_lsa_time,
            }
            key = (self.router_id, self.my_lsa_seq)
            if len(self.lsa_fifo) >= self.lsa_capacity:
                old = self.lsa_fifo.popleft()
                self.lsa_seen.discard(old)
            self.lsa_fifo.append(key)
            self.lsa_seen.add(key)
            # ---------------------------------------------------------------

        payload = {
            "origin": self.router_id,
            "seq": self.my_lsa_seq,
            "neighbors": neighs,
            "ts": self.last_lsa_time
        }
        headers = {"msg_id": uuid.uuid4().hex, "seq": self.my_lsa_seq}

        return Packet(
            proto=self.get_name(),
            packet_type="lsa",
            from_addr=self.router_id,
            to_addr="broadcast",
            ttl=16,
            headers=headers,
            payload=json.dumps(payload)
        )


    # Mantenimientos periódicos (router.py los invoca)
    def _check_neighbor_timeouts(self):
        now = time.time()
        changed = False
        with self._lock:
            for nb, st in list(self.neighbor_states.items()):
                last = st.get("last_seen", 0)
                alive_now = (now - last) < self.NEIGHBOR_TIMEOUT
                if alive_now != st.get("alive", False):
                    st["alive"] = alive_now
                    self.neighbor_states[nb] = st
                    changed = True
        if changed:
            self.topology_changed = True
            self.calculate_routes()

    def _age_lsa_database(self):
        now = time.time()
        removed = False
        with self._lock:
            for origin, entry in list(self.link_state_db.items()):
                if (now - entry.get("last_received", 0)) >= self.LSA_MAX_AGE:
                    del self.link_state_db[origin]
                    removed = True
        if removed:
            self.topology_changed = True
            self.calculate_routes()

    # SPF (Dijkstra)
    def calculate_routes(self):
        adj: Dict[str, Dict[str, int]] = defaultdict(dict)

        with self._lock:
            # vecinos directos vivos
            for nb, st in self.neighbor_states.items():
                if st.get("alive", False):
                    c = int(st.get("cost", 1))
                    adj[self.router_id][nb] = c
                    adj[nb][self.router_id] = c

            # LSAs aprendidas
            for origin, entry in self.link_state_db.items():
                for nb, c in entry.get("neighbors", {}).items():
                    c = int(c)
                    # mantener costo mínimo si llegan versiones diferentes
                    prev = adj[origin].get(nb, c)
                    adj[origin][nb] = min(prev, c)
                    prev2 = adj[nb].get(origin, c)
                    adj[nb][origin] = min(prev2, c)

            self.area_routers = set(adj.keys())

            src = self.router_id
            if src not in adj:
                self.routing_table.clear()
                return

            inf = float("inf")
            dist: Dict[str, float] = {n: inf for n in adj.keys()}
            prev: Dict[str, Optional[str]] = {n: None for n in adj.keys()}
            dist[src] = 0.0

            unvisited = set(adj.keys())
            # desempate determinista por nombre
            while unvisited:
                u = min(unvisited, key=lambda n: (dist.get(n, inf), n))
                unvisited.remove(u)
                if dist[u] == inf:
                    break
                for v in sorted(adj[u].keys()):
                    if v not in unvisited:
                        continue
                    alt = dist[u] + adj[u][v]
                    if alt < dist[v]:
                        dist[v] = alt
                        prev[v] = u

            new_table: Dict[str, str] = {}
            for dst in adj.keys():
                if dst == src or dist.get(dst, inf) == inf:
                    continue
                nh = self.firstHop(dst, prev)
                if nh:
                    new_table[dst] = nh

            # swap atómico
            self.routing_table = new_table

    def firstHop(self, dst: str, prev: Dict[str, Optional[str]]) -> Optional[str]:
        cur = dst
        steps = 0
        while prev.get(cur) is not None and prev[cur] != self.router_id:
            cur = prev[cur]
            steps += 1
            if steps > 1024:  # protección
                return None
        if prev.get(cur) == self.router_id:
            return cur
        # vecino directo
        if dst in self.neighbor_states and self.neighbor_states[dst].get("alive", False):
            return dst
        return None
