import json
import time
import uuid
from typing import Dict, Optional, Tuple, Any, List
from collections import defaultdict, deque

from src.algorithms.base import RoutingAlgorithm
from src.packet import Packet


class LinkStateRouting(RoutingAlgorithm):
    """
    Link State Routing (tipo OSPF simplificado):
      - HELLO periódicos para detectar vecinos (last_seen).
      - LSA con número de secuencia y envejecimiento (age).
      - DB de estado de enlace y SPF (Dijkstra) para tabla de ruteo.
      - Flooding de LSA a todos excepto el emisor (coordinado por router.py).
    """

    # Parámetros (puedes ajustarlos si quieres)
    HELLO_INTERVAL = 5.0          # s
    NEIGHBOR_TIMEOUT = 20.0       # s sin HELLO => vecino muerto
    LSA_MIN_INTERVAL = 8.0        # s mínimo entre LSAs propios
    LSA_REFRESH_INTERVAL = 30.0   # s (refresco aun sin cambios)
    LSA_MAX_AGE = 90.0            # s para desechar LSAs viejos

    def __init__(self, router_id: str):
        super().__init__(router_id)

        # Vecinos inmediatos (descubiertos/arrancados por config):
        # neighbor_states[nb] = {"cost": int, "last_seen": float, "alive": bool}
        self.neighbor_states: Dict[str, Dict[str, Any]] = {}

        # Base de estado de enlace (LSDB):
        # link_state_db[origin] = {
        #   "seq": int,
        #   "neighbors": {nb: cost, ...},
        #   "last_received": float
        # }
        self.link_state_db: Dict[str, Dict[str, Any]] = {}

        # Conjunto de routers “en el área” (para debug)
        self.area_routers: set[str] = set([router_id])

        # Control de LSA propio
        self._my_lsa_seq: int = 0
        self._last_lsa_time: float = 0.0
        self._topology_changed: bool = True  # forzar primer LSA

        # Control de HELLO
        self._last_hello_time: float = 0.0

    # ---- API requerida por router.py ----

    def get_name(self) -> str:
        return "lsr"

    def update_neighbor(self, neighbor_id: str, neighbor_info: Dict):
        # Se invoca al cargar topología/nombres; inicializa costo y estado
        cost = int(neighbor_info.get("cost", 1))
        now = time.time()
        st = self.neighbor_states.get(neighbor_id, {})
        st.update({"cost": cost, "last_seen": now, "alive": True})
        self.neighbor_states[neighbor_id] = st

        # Base.neighbors para compatibilidad con comandos de debug
        self.neighbors[neighbor_id] = {"cost": cost}

        # Cambió la adyacencia directa -> anunciar en próximo LSA
        self._topology_changed = True

    def process_packet(self, packet: Packet, from_neighbor: str) -> Optional[str]:
        # Asegura headers como dict
        if not isinstance(getattr(packet, "headers", {}), dict):
            packet.headers = {}

        # --- HELLO ---
        if packet.type == "hello":
            # Marca last_seen del vecino que nos lo envió
            if from_neighbor and from_neighbor != "unknown":
                st = self.neighbor_states.get(from_neighbor, {"cost": 1})
                st["last_seen"] = time.time()
                st["alive"] = True
                self.neighbor_states[from_neighbor] = st
                self.neighbors[from_neighbor] = {"cost": st.get("cost", 1)}
            # No se reenvía HELLO
            return None

        # --- LSA (info de estado de enlace) ---
        if packet.type in ("lsa", "info"):
            try:
                data = json.loads(packet.payload)
            except Exception:
                return None

            origin = packet.from_addr
            seq = int(data.get("seq", 0))
            neighs = data.get("neighbors", {})
            # Solo aceptamos LSA nuevo (seq mayor)
            current = self.link_state_db.get(origin)
            if current and seq <= int(current.get("seq", -1)):
                return None  # viejo/duplicado => no flood

            # Guarda/actualiza entrada
            self.link_state_db[origin] = {
                "seq": seq,
                "neighbors": {str(k): int(v) for k, v in neighs.items()},
                "last_received": time.time()
            }
            self.area_routers.update([origin, *self.link_state_db[origin]["neighbors"].keys(), self.router_id])

            # Recalcula rutas
            self._calculate_routes()
            # Propaga (flood except sender)
            return "flood_lsa"

        # --- DATA/ECHO: usar tabla de ruteo ---
        return self.get_next_hop(packet.to_addr)

    def get_next_hop(self, destination: str) -> Optional[str]:
        if destination == self.router_id:
            return None
        return self.routing_table.get(destination)

    # ---- Usado por router._send_hello_packets() ----

    def should_send_hello(self) -> bool:
        return (time.time() - self._last_hello_time) >= self.HELLO_INTERVAL

    def create_hello_packet(self) -> Packet:
        self._last_hello_time = time.time()
        headers = {"msg_id": uuid.uuid4().hex, "ts": self._last_hello_time}
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
        # Envía si: hay cambios y respetamos el min interval, o por refresco periódico
        if self._topology_changed and (now - self._last_lsa_time) >= self.LSA_MIN_INTERVAL:
            return True
        if (now - self._last_lsa_time) >= self.LSA_REFRESH_INTERVAL:
            return True
        return False

    def create_lsa_packet(self) -> Packet:
        self._my_lsa_seq += 1
        self._last_lsa_time = time.time()
        self._topology_changed = False

        # Vecinos vivos actuales
        neighs: Dict[str, int] = {}
        now = time.time()
        for nb, st in self.neighbor_states.items():
            if st.get("alive", False) and (now - st.get("last_seen", 0)) < self.NEIGHBOR_TIMEOUT:
                neighs[nb] = int(st.get("cost", 1))

        payload = {
            "origin": self.router_id,
            "seq": self._my_lsa_seq,
            "neighbors": neighs,
            "ts": self._last_lsa_time
        }
        headers = {"msg_id": uuid.uuid4().hex, "seq": self._my_lsa_seq}

        return Packet(
            proto=self.get_name(),
            packet_type="lsa",
            from_addr=self.router_id,
            to_addr="broadcast",
            ttl=16,
            headers=headers,
            payload=json.dumps(payload)
        )

    # ---- Mantenimientos periódicos que llama router.py (con try/except) ----

    def _check_neighbor_timeouts(self):
        """Marca vecinos muertos y dispara LSA si cambia algo."""
        now = time.time()
        changed = False
        for nb, st in list(self.neighbor_states.items()):
            last = st.get("last_seen", 0)
            alive_before = st.get("alive", False)
            alive_now = (now - last) < self.NEIGHBOR_TIMEOUT
            if alive_now != alive_before:
                st["alive"] = alive_now
                self.neighbor_states[nb] = st
                changed = True
        if changed:
            self._topology_changed = True
            self._calculate_routes()

    def _age_lsa_database(self):
        """Elimina LSAs viejos del LSDB y recalcula si cambia algo."""
        now = time.time()
        removed = False
        for origin, entry in list(self.link_state_db.items()):
            if (now - entry.get("last_received", 0)) >= self.LSA_MAX_AGE:
                del self.link_state_db[origin]
                removed = True
        if removed:
            self._topology_changed = True
            self._calculate_routes()

    # ---- SPF (Dijkstra) sobre el grafo armado desde el LSDB + vecinos locales ----

    def _calculate_routes(self):
        # Construye adyacencia no dirigida con costos
        adj: Dict[str, Dict[str, int]] = defaultdict(dict)

        # 1) Nuestros vecinos inmediatos
        for nb, st in self.neighbor_states.items():
            if st.get("alive", False):
                c = int(st.get("cost", 1))
                adj[self.router_id][nb] = c
                adj[nb][self.router_id] = c

        # 2) LSAs aprendidos
        for origin, entry in self.link_state_db.items():
            neighs = entry.get("neighbors", {})
            for nb, c in neighs.items():
                c = int(c)
                adj[origin][nb] = min(adj[origin].get(nb, c), c)
                adj[nb][origin] = min(adj[nb].get(origin, c), c)

        # Mantén lista de routers conocida (para debug)
        self.area_routers = set(adj.keys())

        # Dijkstra
        src = self.router_id
        dist: Dict[str, float] = {node: float("inf") for node in adj.keys()}
        prev: Dict[str, Optional[str]] = {node: None for node in adj.keys()}
        if src not in dist:
            # Sin vecinos/LSAs, limpia tabla
            self.routing_table.clear()
            return
        dist[src] = 0.0
        unvisited = set(adj.keys())

        while unvisited:
            u = min(unvisited, key=lambda n: dist.get(n, float("inf")))
            unvisited.remove(u)
            if dist[u] == float("inf"):
                break
            for v, cost in adj[u].items():
                if v not in unvisited:
                    continue
                alt = dist[u] + cost
                if alt < dist[v]:
                    dist[v] = alt
                    prev[v] = u

        # Construye tabla: destino -> next_hop
        new_table: Dict[str, str] = {}
        for dst in adj.keys():
            if dst == src or dist.get(dst, float("inf")) == float("inf"):
                continue
            nh = self._first_hop(dst, prev)
            if nh:
                new_table[dst] = nh

        self.routing_table = new_table

    def _first_hop(self, dst: str, prev: Dict[str, Optional[str]]) -> Optional[str]:
        """Primer salto desde self.router_id hacia dst usando predecesores."""
        # retrocede desde dst hasta el origen
        cur = dst
        path_rev: List[str] = [cur]
        while prev.get(cur) is not None and prev[cur] != self.router_id:
            cur = prev[cur]
            path_rev.append(cur)
            # protección contra bucles raros
            if len(path_rev) > 1024:
                return None
        # Si el predecesor inmediato es el origen, el primer hop es dst
        if prev.get(cur) == self.router_id:
            return cur
        # Si dst es vecino directo
        if dst in self.neighbor_states and self.neighbor_states[dst].get("alive", False):
            return dst
        return None
