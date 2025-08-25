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
