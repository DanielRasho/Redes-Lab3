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

    # ===== API requerida por router.py =====

    def get_name(self) -> str:
        return "lsr"

    def update_neighbor(self, neighbor_id: str, neighbor_info: Dict):
        """
        Actualiza/crea registro de vecino a partir de información del Router:
        neighbor_info puede incluir "cost" y metadatos (canal, etc).
        """
        cost = int(neighbor_info.get("cost", 1))
        now = time.time()
        with self._lock:
            st = self.neighbor_states.get(neighbor_id, {})
            st.update({"cost": cost, "last_seen": now, "alive": True})
            self.neighbor_states[neighbor_id] = st
            # table mínima para "vecino directo"
            self.neighbors[neighbor_id] = {"cost": cost}
            self.topology_changed = True

    def process_packet(self, packet: Packet, from_neighbor: str) -> Optional[str]:
        """
        Procesa paquetes recibidos y decide:
          - None: consumir/no reenviar
          - "flood_lsa": router reenvía broadcast a todos menos el emisor
          - <neighbor_id>: next-hop unicast
          - "flood": (no usado aquí; reservado)
        """
        # Asegura msg_id sin mutar la forma de headers (dict o list)
        try:
            packet.ensure_msg_id()
        except Exception:
            pass

        # ===== HELLO =====
        # Solo refresca estado de vecino, NO se retransmite.
        if packet.type == "hello":
            now = time.time()
            sender = packet.from_addr  # quien dijo "hello"
            with self._lock:
                nb_id = None

                # 1) Preferimos el vecino por el canal si viene seteado
                if from_neighbor and from_neighbor != "unknown":
                    nb_id = from_neighbor

                # 2) Si no, usamos el "from" del paquete cuando es un vecino conocido
                if nb_id is None and sender in self.neighbors:
                    nb_id = sender

                if nb_id is not None:
                    st = self.neighbor_states.get(nb_id, {"cost": int(self.neighbors.get(nb_id, {}).get("cost", 1))})
                    st["last_seen"] = now
                    st["alive"] = True
                    # asegura costo (por si no estaba)
                    st["cost"] = int(self.neighbors.get(nb_id, {}).get("cost", st.get("cost", 1)))
                    self.neighbor_states[nb_id] = st
                    self.neighbors[nb_id] = {"cost": st["cost"]}

                    # marca cambio y, opcionalmente, recalcula rápido
                    self.topology_changed = True
                    # self.calculate_routes()  # <- si quieres convergencia aún más ágil

            return None  # hello no se reenvía


        # ===== INFO (LSA) =====
        if packet.type in ("lsa", "info"):
            # 1) Anti-loop por ventana de 3 en headers.path
            if not self.handleHeadersPath(packet):
                return None  # ciclo detectado o path inválido

            # 2) Parse payload y anti-spoof
            try:
                data = json.loads(packet.payload)
            except Exception:
                return None

            origin_field = str(data.get("origin", packet.from_addr))
            if origin_field != packet.from_addr:
                # Evita que alguien "falsifique" el origin dentro del payload
                return None

            origin = packet.from_addr
            seq = int(data.get("seq", 0))
            neighs = data.get("neighbors", {})

            # 3) Dedupe y actualización de LSDB
            key = (origin, seq)
            with self._lock:
                # duplicado exacto
                if key in self.lsa_seen:
                    return None
                if len(self.lsa_fifo) >= self.lsa_capacity:
                    old = self.lsa_fifo.popleft()
                    self.lsa_seen.discard(old)
                self.lsa_fifo.append(key)
                self.lsa_seen.add(key)

                # obsoleta
                current = self.link_state_db.get(origin)
                if current and seq <= int(current.get("seq", -1)):
                    return None

                # acepta y almacena
                self.link_state_db[origin] = {
                    "seq": seq,
                    "neighbors": {str(k): int(v) for k, v in neighs.items()},
                    "last_received": time.time()
                }
                # actualiza universo de routers
                self.area_routers.update(
                    [origin, *self.link_state_db[origin]["neighbors"].keys(), self.router_id]
                )

                # recalcula rutas
                self.calculateRoutes()

            # 4) Solicita flood controlado (router hará TTL-- y excluirá al emisor)
            return "flood_lsa"

        # ===== Mensajes unicast / echo =====
        # Usa la tabla SPF para decidir siguiente salto.
        return self.get_next_hop(packet.to_addr)

    def get_next_hop(self, destination: str) -> Optional[str]:
        """Devuelve el vecino next-hop para 'destination' o None si no hay ruta."""
        if destination == self.router_id:
            return None
        return self.routing_table.get(destination)

    # ===== Emisión periódica de HELLO / INFO (router.py consulta estos) =====

    def should_send_hello(self) -> bool:
        return (time.time() - self.last_hello_time) >= self.HELLO_INTERVAL

    def create_hello_packet(self) -> Packet:
        """
        Crea HELLO (no se retransmite). Se usa para presencia/refresh de vecinos.
        """
        self.last_hello_time = time.time()
        headers = {"msg_id": uuid.uuid4().hex, "ts": self.last_hello_time, "path": []}
        return Packet(
            proto=self.get_name(),
            packet_type="hello",
            from_addr=self.router_id,
            to_addr="broadcast",  # estándar del curso
            ttl=5,
            headers=headers,
            payload=""  # payload vacío según guía
        )

    def should_send_lsa(self) -> bool:
        now = time.time()
        if self.topology_changed and (now - self.last_lsa_time) >= self.LSA_MIN_INTERVAL:
            return True
        if (now - self.last_lsa_time) >= self.LSA_REFRESH_INTERVAL:
            return True
        return False

    def create_lsa_packet(self) -> Packet:
        """
        Emite mi LSA como paquete INFO (compatibilidad: receptor acepta "lsa" o "info").
        Pre-instala mi propio LSA en la LSDB y en el filtro de duplicados.
        """
        self.my_lsa_seq += 1
        self.last_lsa_time = time.time()
        self.topology_changed = False

        # Vecinos vivos en ventana de timeout
        neighs: Dict[str, int] = {}
        now = time.time()
        with self._lock:
            for nb, st in self.neighbor_states.items():
                if st.get("alive", False) and (now - st.get("last_seen", 0)) < self.NEIGHBOR_TIMEOUT:
                    neighs[nb] = int(st.get("cost", 1))

            # Pre-instalo mi propio LSA en LSDB y dedupe
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

        self.calculateRoutes()
        payload = {
            "origin": self.router_id,
            "seq": self.my_lsa_seq,
            "neighbors": neighs,
            "ts": self.last_lsa_time
        }
        headers = {"msg_id": uuid.uuid4().hex, "seq": self.my_lsa_seq, "path": []}

        return Packet(
            proto=self.get_name(),
            packet_type="info",      # <-- estándar acordado
            from_addr=self.router_id,
            to_addr="broadcast",
            ttl=16,
            headers=headers,
            payload=json.dumps(payload)
        )

    # ===== Mantenimientos periódicos =====

    def _check_neighbor_timeouts(self):
        """
        Marca vecinos como vivos/muertos según NEIGHBOR_TIMEOUT.
        Si cambia algo, recalcula rutas y marca topología cambiada.
        """
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
            self.calculateRoutes()

    def _age_lsa_database(self):
        """
        Envejece/retira LSAs viejas (LSA_MAX_AGE). Si cambia algo, recalcula rutas.
        """
        now = time.time()
        removed = False
        with self._lock:
            for origin, entry in list(self.link_state_db.items()):
                if (now - entry.get("last_received", 0)) >= self.LSA_MAX_AGE:
                    del self.link_state_db[origin]
                    removed = True
        if removed:
            self.topology_changed = True
            self.calculateRoutes()

    # ===== SPF (Dijkstra) =====

    def calculateRoutes(self):
        """
        Construye el grafo y corre SPF. Calcula el first-hop
        durante la relajación (más robusto que reconstruir al final).
        """
        adj: Dict[str, Dict[str, int]] = defaultdict(dict)

        with self._lock:
            # 1) Vecinos directos vivos
            for nb, st in self.neighbor_states.items():
                if st.get("alive", False):
                    c = int(st.get("cost", 1))
                    adj[self.router_id][nb] = c
                    adj[nb][self.router_id] = c

            # 2) LSAs aprendidas (ya envejecidas por _age_lsa_database)
            for origin, entry in self.link_state_db.items():
                for nb, c in entry.get("neighbors", {}).items():
                    c = int(c)
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
            first: Dict[str, Optional[str]] = {n: None for n in adj.keys()}
            dist[src] = 0.0

            unvisited = set(adj.keys())
            while unvisited:
                # desempate determinista
                u = min(unvisited, key=lambda n: (dist.get(n, inf), n))
                unvisited.remove(u)
                if dist[u] == inf:
                    break

                for v in sorted(adj[u].keys()):
                    if v not in unvisited:
                        continue
                    alt = dist[u] + adj[u][v]
                    cand_first = v if u == src else first[u]

                    if (alt < dist[v]) or (alt == dist[v] and self.preferFirstHop(cand_first, first[v])):
                        dist[v] = alt
                        first[v] = cand_first

            new_table: Dict[str, str] = {}
            for dst, fh in first.items():
                if dst == src or fh is None or dist.get(dst, inf) == inf:
                    continue
                new_table[dst] = fh  # next-hop definitivo

            self.routing_table = new_table

    def preferFirstHop(self, cand: Optional[str], cur: Optional[str]) -> bool:
        """
        Criterio de desempate cuando dos rutas tienen igual costo:
        1) Prefiere tener algún first-hop a no tener (None).
        2) Prefiere vecinos directos vivos.
        3) Desempata por nombre para determinismo.
        """
        if cur is None:
            return True
        if cand is None:
            return False
        cand_nb = cand in self.neighbor_states and self.neighbor_states[cand].get("alive", False)
        cur_nb  = cur  in self.neighbor_states and self.neighbor_states[cur].get("alive", False)
        if cand_nb != cur_nb:
            return cand_nb
        return cand < cur

    # === Helpers ===

    def firstHop(self, dst: str, prev: Dict[str, Optional[str]]) -> Optional[str]:
        """
        Obtiene el primer salto desde self.router_id hacia dst recorriendo 'prev'.
        """
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

    def handleHeadersPath(self, packet: Packet) -> bool:
        """
        Mantiene headers.path como ventana de 3 nodos y corta ciclos.
        True  -> seguro continuar
        False -> ciclo detectado o path inválido (drop)
        """
        try:
            path = packet.get_path()  # siempre list (internamente maneja dict/list)
        except Exception:
            path = []

        # ciclo: si ya pasé por aquí, no reenvío
        if self.router_id in path:
            return False

        # ventana de 3: quita primero si ya hay 3 y agrega mi id
        new_path = list(path)
        if len(new_path) >= 3:
            new_path.pop(0)
        new_path.append(self.router_id)

        try:
            packet.set_path(new_path)
        except Exception:
            return False

        return True