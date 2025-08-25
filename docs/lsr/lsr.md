# Link State Routing

## LinkStateRouting — Estado base y constantes

```python
import json
import time
import uuid
import threading
from typing import Dict, Optional, Any, Tuple, Set
from collections import defaultdict, deque
from src.algorithms.base import RoutingAlgorithm
from src.packet import Packet
```

### Propósito del módulo

Implementa un algoritmo de **Link State Routing (LSR)** estilo OSPF simplificado, diseñado para un entorno **pub/sub** (e.g. Redis). Gestiona **descubrimiento de vecinos (HELLO)**, **difusión de LSAs (INFO)**, **cálculo SPF (Dijkstra)** y **control de flooding**/deduplicación, exponiendo una **tabla de encaminamiento** (next-hop por destino) que consumen capas superiores (p. ej., `router.py`).

### Mensajería (convenciones)

- `proto="lsr"`
- `type`:

  - `hello` (broadcast, **no** se retransmite)
  - `info` (LSA; broadcast, **sí** se retransmite con TTL--)
  - `message` / `echo` (unicast; se reenvía según la tabla SPF)
- `headers.path`: ventana de los **últimos 3 routers** para detectar bucles.
- `headers.msg_id`: UUID para **deduplicación** (LRU en `router.py`).

> **Conexión con otras partes**
>
> - `src.algorithms.base.RoutingAlgorithm`: clase base (provee `router_id` y contrato común).
> - `src.packet.Packet`: representación de paquetes que usa los headers anteriores.
> - `router.py`: orquesta I/O pub/sub y decide retransmitir cuando el algoritmo retorna una acción tipo *flood LSA*.

### Clase: `LinkStateRouting`

```python
class LinkStateRouting(RoutingAlgorithm):
    """
    Link State Routing (OSPF simplificado) para entorno pub/sub.
    ...
    """
```

### Timers (segundos)

```python
HELLO_INTERVAL = 5.0          # Periodicidad de envío de HELLO
NEIGHBOR_TIMEOUT = 20.0       # Tiempo sin HELLO para marcar vecino como inactivo
LSA_MIN_INTERVAL = 8.0        # Mínimo entre LSAs propios para evitar tormenta
LSA_REFRESH_INTERVAL = 30.0   # Refresco periódico del LSA propio (keepalive/topología)
LSA_MAX_AGE = 90.0            # Envejecimiento de entradas LSDB (expiración)
```

**Efecto en el flujo:**

- Un planificador (en otra parte del módulo) dispara `HELLO` cada `HELLO_INTERVAL`.

- Si un vecino no envía `HELLO` dentro de `NEIGHBOR_TIMEOUT`, se marca `alive=False` y se dispara recálculo SPF.
- El LSA propio se emite **como mínimo** cada `LSA_MIN_INTERVAL` cuando hay cambios y se **refresca** cada `LSA_REFRESH_INTERVAL`.
- LSAs antiguos se purgan al superar `LSA_MAX_AGE`.

#### Constructor

```python
def __init__(self, router_id: str):
    super().__init__(router_id)
    ...
```

**Parámetros**

- `router_id: str` — Identificador único del router (p. ej., “A”, “R1”, UUID corto). Viene de configuración superior.

**Retorno**

- `None` (constructor).

**Efectos/Side effects**

- Inicializa todo el **estado mutable** (vecinos, LSDB, control LSA, locks).
- Registra el propio `router_id` en `area_routers`.

**Excepciones**

- No lanza explícitamente; cualquier validación de `router_id` sucede en `RoutingAlgorithm`.

#### Atributos de estado (y su rol)

| Atributo           |                        Tipo | Descripción                                                        | Actualización típica                           | Consumido por                             |
| ------------------ | --------------------------: | ------------------------------------------------------------------ | ---------------------------------------------- | ----------------------------------------- |
| `neighbor_states`  | `Dict[str, Dict[str, Any]]` | Estado de **vecinos directos**: `{cost, last_seen, alive}`         | En recepción de `hello` y timers de expiración | Generación de LSA propio y disparo de SPF |
| `link_state_db`    | `Dict[str, Dict[str, Any]]` | **LSDB** por origen: `{seq, neighbors: {id: cost}, last_received}` | En recepción de `info` (LSA) y refresh propio  | SPF (Dijkstra)                            |
| `area_routers`     |                  `Set[str]` | Conjunto de routers detectados en el área                          | Al procesar LSAs y HELLOs                      | Reportes/diagnóstico; SPF                 |
| `my_lsa_seq`       |                       `int` | Secuencia del **LSA propio**                                       | Incrementa al emitir LSA                       | Deduplicación/orden de LSAs               |
| `last_lsa_time`    |             `float` (epoch) | Momento del último LSA propio                                      | Al emitir                                      | Rate-limiting (MIN/REFRESH)               |
| `topology_changed` |                      `bool` | Bandera de “cambió la topología” para **debounce** del SPF         | Al cambiar vecinos/LSDB                        | Trigger de SPF                            |
| `last_hello_time`  |             `float` (epoch) | Último HELLO emitido                                               | Al emitir HELLO                                | Programación del siguiente HELLO          |
| `lsa_seen`         |      `Set[Tuple[str, int]]` | (origin, seq) ya vistos                                            | Al recibir LSA                                 | **Deduplicación** de flooding             |
| `lsa_fifo`         |    `deque[Tuple[str, int]]` | Cola LRU de claves vistas                                          | Al recibir LSA                                 | Mantener tamaño acotado                   |
| `lsa_capacity`     |                       `int` | Capacidad máxima de la LRU de LSAs                                 | Constante                                      | Control memoria                           |
| `_lock`            |           `threading.RLock` | **Re-entrant lock** para proteger estado compartido                | En todas las mutaciones concurrentes           | Seguridad de hilo (RX/Timers)             |

**Notas importantes**

- `neighbor_states` y `link_state_db` constituyen la **fuente de verdad** del grafo. Cualquier cambio marca `topology_changed=True`.
- `lsa_seen` + `lsa_fifo` implementan una **LRU manual**: si el tamaño supera `lsa_capacity`, se popleft de la `deque` y se elimina la clave del `set`. Evita re-procesar/re-floodear LSAs duplicados.
- `RLock` permite que callbacks internos reingresen secciones críticas sin deadlocks (útil cuando un evento dispara otro que vuelve a tocar el estado).

### Flujo lógico (de alto nivel) cubierto por este fragmento

1. **Inicialización**
   Se crea el contenedor de estado (vecinos, LSDB, dedupe) y se arman los **timers** de referencia (constantes).

2. **Recepción de HELLO / INFO (en otras funciones del módulo)**

   - HELLO: actualiza `neighbor_states[nb] = {cost,last_seen,alive=True}`.
   - INFO (LSA): si `(origin, seq)` ∉ `lsa_seen`, se integra en `link_state_db` y se marca **para flooding** y SPF.

3. **Timers**

   - Envío periódico de HELLO (`HELLO_INTERVAL`).
   - Emisión/refresh del LSA propio cuando corresponde (`LSA_MIN_INTERVAL`, `LSA_REFRESH_INTERVAL`).
   - Expiración de vecinos (`NEIGHBOR_TIMEOUT`).
   - Purga de entradas LSDB por edad (`LSA_MAX_AGE`).

4. **Integración con router.py**
   Cuando este algoritmo **indica** “flood\_lsa” (en métodos no mostrados aún), `router.py` se encarga de publicar el LSA en broadcast y de **no** retransmitir `hello`.

### Contratos y garantías

- **Thread-safety:** toda mutación de `neighbor_states`, `link_state_db`, `lsa_seen` y marcas de tiempo se debe hacer bajo `_lock`.
- **Deduplicación fuerte:** `lsa_seen` asegura que un LSA con `(origin, seq)` no se procesa ni se vuelve a inundar.
- **Rate-limiting de LSAs propios:** nunca se emite más frecuente que `LSA_MIN_INTERVAL`; siempre se renueva antes de `LSA_MAX_AGE` mediante `LSA_REFRESH_INTERVAL`.
- **Reconvergencia:** cualquier cambio en vecinos/LSDB seta `topology_changed=True` para que el **SPF** recalcule la **routing\_table** (implementación en métodos posteriores).

## API requerida por `routes.py`

Esta sección expone los métodos que `router.py` invoca para integrar el algoritmo LSR con la capa de I/O pub/sub.

### `get_name() -> str`

**Propósito:** Identifica el protocolo para `router.py`.

- **Retorna:** `"lsr"`.
- **Uso típico:** `router.py` puede anunciar/registrar el algoritmo activo o enrutar por nombre de protocolo.

### `update_neighbor(neighbor_id: str, neighbor_info: Dict) -> None`

**Propósito:** Crear/actualizar el estado de un **vecino directo** (costo, vida, timestamps) a partir de metadatos que conoce `router.py` (canal físico/lógico, costo, etc.).

**Parámetros**

- `neighbor_id`: identificador único del vecino directo.
- `neighbor_info`: diccionario que puede incluir:

  - `cost: int` (opcional; por defecto `1`)
  - otros metadatos ignorados por el núcleo LSR (p. ej. canal).

**Efectos/estado**

- Bajo `_lock`:

  - Actualiza `neighbor_states[neighbor_id] = {cost, last_seen=now, alive=True}`.
  - Actualiza `self.neighbors[neighbor_id] = {"cost": cost}` (tabla mínima de adyacencia directa; suele venir de la clase base).
  - Marca `topology_changed = True` para disparar recálculo SPF en el ciclo correspondiente.
- **No** envía mensajes de red ni recalcula rutas de inmediato (a menos que otra parte llame a `calculateRoutes()`).

**Retorna:** `None`.

**Relación con otras partes**

- `router.py` debería llamar a este método cuando:

  - se descubre un enlace/vecino,
  - cambia el costo del enlace,
  - o se quiere “revivir” un vecino tras reconexión.

### `process_packet(packet: Packet, from_neighbor: str) -> Optional[str]`

**Propósito:** Consumir/interpretar un paquete entrante y devolver la **acción de reenvío** apropiada para que `router.py` la ejecute.

**Parámetros**

- `packet`: instancia de `Packet` con al menos:

  - `type`: `"hello" | "lsa"/"info" | "message"/"echo"`
  - `from_addr`, `to_addr`, `payload` (JSON para LSA), `headers` (con `path`, `msg_id`, etc.)
  - método `ensure_msg_id()` (genera `headers.msg_id` si falta).
- `from_neighbor`: ID del vecino por cuyo canal físico/lógico llegó el paquete (puede ser `"unknown"`).

**Valor de retorno (contrato con `router.py`):**

- `None`: consumir/terminar (no reenviar).
- `"flood_lsa"`: `router.py` debe **retransmitir en broadcast** a todos salvo el emisor (y manejar TTL--/exclusión).
- `"<neighbor_id>"`: hacer **unicast** al vecino indicado (siguiente salto).
- `"flood"`: reservado, no usado aquí.

#### Flujo por tipo de mensaje

##### 1. HELLO

- **Objetivo:** solo **refrescar** estado del vecino. **No** se retransmite.
- **Lógica clave:**

  - `packet.ensure_msg_id()` se intenta de forma defensiva.
  - Determina `nb_id`:

    1. prioriza `from_neighbor` si viene distinto de `"unknown"`;
    2. si no, usa `packet.from_addr` **solo** si ya es un vecino conocido en `self.neighbors`.
  - Si hay `nb_id`:

    - actualiza/crea `neighbor_states[nb_id]` con `last_seen=now`, `alive=True` y **asegura** `cost`.
    - refleja el costo en `self.neighbors[nb_id]`.
    - marca `topology_changed = True` (opcionalmente se podría recalcular en caliente).
- **Retorno:** `None`.

> Nota: si llega un HELLO de un remitente desconocido **y** el canal también es `"unknown"`, no se crea el vecino automáticamente (se espera un `update_neighbor` desde `router.py`).

##### 2. INFO / LSA (`packet.type in {"lsa","info"}`)

- **Objetivo:** integrar un **Link State Advertisement** a la LSDB y decidir si debe **floodearse**.
- **Pasos:**

  1. **Anti-loop** por ventana de 3 en `headers.path`: `handleHeadersPath(packet)` (método interno).

     - Si devuelve `False`, **descarta** (ciclo detectado o `path` inválido).
  2. **Parseo** de `payload` JSON y **anti-spoof**:

     - `data = json.loads(packet.payload)`
     - `origin_field` (en payload) **debe** coincidir con `packet.from_addr`; si no, descarta.
     - Extrae `origin`, `seq: int`, `neighbors: Dict[str, int]`.
  3. **Deduplicación y orden**:

     - Usa `(origin, seq)` como clave; si ya está en `lsa_seen`, descarta.
     - Mantiene LRU con `lsa_fifo` acotado a `lsa_capacity`.
     - Si existe en `link_state_db[origin]` con `seq_actual >= seq`, descarta por **obsolescencia**.
  4. **Aceptación y almacenamiento** (bajo `_lock`):

     - `link_state_db[origin] = {"seq", "neighbors", "last_received"}`
       (con claves de vecinos `str(...)` y costos `int(...)` normalizados).
     - Actualiza `area_routers` con `origin`, sus vecinos y `self.router_id`.
     - Llama a `self.calculateRoutes()` para recomputar SPF (genera/actualiza `routing_table`).
  5. **Flood controlado**:

     - Retorna `"flood_lsa"` para que `router.py` retransmita a **otros** vecinos (excluyendo al emisor y ajustando TTL).
- **Retorno:** `"flood_lsa"` si se integró un LSA nuevo; `None` si se descartó.

> **Formato esperado de LSA (payload JSON):**
>
> ```json
> {
>   "origin": "R1",
>   "seq": 12,
>   "neighbors": { "R2": 5, "R3": 1 }
> }
> ```

##### 3. Mensajes unicast / echo

- Para `type` distintos de `hello` y `lsa/info`, se asume **unicast**.
- **Acción:** delega en `get_next_hop(packet.to_addr)` y retorna ese vecino (o `None` si no hay ruta).

### `get_next_hop(destination: str) -> Optional[str]`

**Propósito:** Resolver el **siguiente salto** (vecino) hacia un destino final usando la `routing_table` calculada por SPF.

**Parámetros**

- `destination`: ID del router destino final.

**Retorna**

- `neighbor_id` (str) si hay ruta en `routing_table`.
- `None` si:

  - el destino es el propio router (`destination == self.router_id`), o
  - no existe ruta conocida.

**Notas**

- Lectura ligera (O(1)). Se apoya en que `calculateRoutes()` mantiene `routing_table` coherente.

### Interacciones y contratos cruzados

- **`router.py`**

  - Invoca `update_neighbor()` cuando cambia la adyacencia/costo físico.
  - Entrega cada paquete a `process_packet()`.

    - Si recibe `"flood_lsa"`, **retransmite** en broadcast a todos menos el emisor y aplica políticas (TTL--, evitar eco).
    - Si recibe `"<neighbor_id>"`, **reenvía unicast** por ese canal.
    - Si recibe `None`, **consume** el paquete.
- **`Packet`**

  - Debe soportar `ensure_msg_id()`, y exponer `type`, `from_addr`, `to_addr`, `payload` y `headers.path`.
- **Métodos internos no mostrados aquí**

  - `handleHeadersPath(packet) -> bool`: valida la ventana de 3 saltos en `headers.path` (detección de loops).
  - `calculateRoutes()`: ejecuta SPF (Dijkstra) sobre `link_state_db` + enlaces directos (`self.neighbors`) y actualiza `routing_table`.
    *(OJO: en un comentario aparece `calculate_routes()`; mantener una sola convención.)*

### Errores y condiciones límite

- **HELLO desconocido:** si no se puede mapear a `nb_id` (canal `"unknown"` y `from_addr` no está en `self.neighbors`), no se crea/actualiza estado (se espera `update_neighbor()`).
- **LSA inválido:** JSON malformado, `origin` falsificado, `seq` obsoleto o **duplicado** → se descarta silenciosamente (`None`).
- **LRU de LSAs:** si se supera `lsa_capacity`, se expulsa la entrada más antigua de `lsa_fifo` y su marca en `lsa_seen` para evitar crecimiento sin límite.

### Ejemplo de decisiones de `process_packet`

| Entrada                                    | Estado                     | Retorno       | Acción de `router.py` |
| ------------------------------------------ | -------------------------- | ------------- | --------------------- |
| `type="hello"` desde vecino conocido       | —                          | `None`        | Solo refresca vecino  |
| LSA nuevo con `seq` mayor                  | LSDB actualizada           | `"flood_lsa"` | Re-flood controlado   |
| LSA duplicado (`(origin,seq)` ya visto)    | —                          | `None`        | Consumir              |
| `type="message"` a `to_addr="R9"` con ruta | `routing_table["R9"]="R2"` | `"R2"`        | Unicast hacia `R2`    |
| `type="message"` a destino sin ruta        | no hay entrada             | `None`        | Drop                  |

## Emisión periódica de HELLO / INFO (LSA)

Estas funciones permiten a `router.py` decidir **cuándo** emitir mensajes de presencia (`HELLO`) y anuncios de estado de enlace (`INFO`/LSA), y **cómo** construir dichos paquetes.

### Timers relevantes

- `HELLO_INTERVAL = 5s` — frecuencia mínima de HELLO.
- `LSA_MIN_INTERVAL = 8s` — anti–ráfaga de LSAs cuando cambia la topología.
- `LSA_REFRESH_INTERVAL = 30s` — refresco periódico de LSAs aunque no haya cambios.
- `NEIGHBOR_TIMEOUT = 20s` — ventana para considerar **vivo** a un vecino.

### `should_send_hello() -> bool`

**Propósito:** Señala a `router.py` si ya pasó el intervalo mínimo para enviar un nuevo HELLO.

- **Lógica:** `time.now - last_hello_time >= HELLO_INTERVAL`.
- **Estado que toca:** ninguno (solo lectura).
- **Notas:** En arranque `last_hello_time = 0.0`, por lo que devuelve `True` y permite un HELLO inmediato.

### `create_hello_packet() -> Packet`

**Propósito:** Construir un paquete `HELLO` para **presencia/refresh** de vecinos.

- **Efectos de estado:**

  - Actualiza `last_hello_time = now`.
- **Headers:**

  - `msg_id`: UUID nuevo (deduplicación a nivel de router).
  - `ts`: timestamp de emisión.
  - `path`: `[]` (HELLO no participa en detección de loops).
- **Packet:**

  - `proto = "lsr"`, `packet_type = "hello"`, `from = self.router_id`,
    `to = "broadcast"`, `ttl = 5`, `payload = ""`.
- **Relación con `router.py`:** Se envía **sin retransmisión** (HELLO no se floodéa).

### `should_send_lsa() -> bool`

**Propósito:** Indicar a `router.py` si corresponde emitir un nuevo LSA (como `INFO`).

- **Retorna `True` si:**

  1. **Hay cambios de topología** (`topology_changed = True`) **y**
     ha pasado `LSA_MIN_INTERVAL` desde el último LSA; **o**
  2. Ha pasado `LSA_REFRESH_INTERVAL` desde el último LSA (keepalive/aging).
- **Notas:** Combina rapidez de convergencia con control de ráfagas.

### `create_lsa_packet() -> Packet`

**Propósito:** Construir y **pre-instalar** el LSA propio en la LSDB y en el filtro de duplicados, y devolver un paquete `INFO` listo para broadcast.

**Flujo y efectos:**

1. **Secuencia y marcadores**

   - `my_lsa_seq += 1`
   - `last_lsa_time = now`
   - `topology_changed = False` (los cambios quedan cubiertos por este LSA).

2. **Selección de vecinos vivos** (bajo `_lock`)

   - Recorre `neighbor_states` y conserva `nb` con `alive=True` y `now - last_seen < NEIGHBOR_TIMEOUT`.
   - Compone `neighs: Dict[neighbor_id, cost:int]`.

3. **Pre-instalación (self-origin) en LSDB + dedupe** (bajo `_lock`)

   - `link_state_db[self.router_id] = {"seq", "neighbors": neighs, "last_received": last_lsa_time}`.
   - Marca `(self.router_id, my_lsa_seq)` en `lsa_seen` y `lsa_fifo` (expulsa el más antiguo si supera `lsa_capacity`).
   - **Motivo:** evitar que nuestro propio LSA re-inyectado por la red sea tratado como “nuevo” al regresar.

4. **SPF inmediato**

   - Llama a `calculateRoutes()` para actualizar `routing_table` con la topología recién anunciada.

5. **Construcción del paquete**

   - **Payload (JSON):**

     ```json
     {
       "origin": "<self.router_id>",
       "seq": <my_lsa_seq>,
       "neighbors": { "<nb>": <cost>, ... },
       "ts": <last_lsa_time>
     }
     ```

   - **Headers:** `{"msg_id": <uuid>, "seq": <my_lsa_seq>, "path": []}`.
   - **Packet:** `proto="lsr"`, `packet_type="info"` (compatibilidad: el receptor acepta `"lsa"` o `"info"`),
     `from=self.router_id`, `to="broadcast"`, `ttl=16`, `payload=json.dumps(payload)`.

**Relación con `router.py`:**

- `router.py` envía el `INFO` en broadcast. Cuando otros routers lo procesen,
  devolverán `"flood_lsa"` si fue aceptado, y `router.py` hará el flood controlado
  (excluyendo al emisor, gestionando TTL, etc.).

**Condiciones límite:**

- Si no hay vecinos vivos, `neighbors` va vacío. Aun así se emite el LSA (anuncia aislamiento).
- Si `lsa_fifo` alcanza `lsa_capacity`, se purga la entrada más antigua para contener memoria.

### Contratos e integración

- `router.py` debe consultar periódicamente:

  - `should_send_hello()` → si `True`, llamar `create_hello_packet()` y **enviar**.
  - `should_send_lsa()` → si `True`, llamar `create_lsa_packet()` y **enviar**.
- El **payload** `INFO` y los **headers** están alineados con `process_packet()`:

  - `process_packet()` valida `origin == from_addr`, deduplica por `(origin, seq)`, y al aceptar dispara SPF y retorna `"flood_lsa"`.

## Mantenimiento: timeouts de vecinos y envejecimiento de LSAs

Estas funciones mantienen coherente el **estado local** del enrutador cuando no hay tráfico suficiente para refrescarlo continuamente. Ambas son **idempotentes**, **thread-safe** (usan `self._lock`) y, si detectan cambios, **disparan recalculo SPF** y marcan `topology_changed = True`, lo que habilita la emisión de un nuevo LSA según `should_send_lsa()`.

### `_check_neighbor_timeouts() -> None`

**Propósito:**
Actualizar el flag `alive` de cada vecino en `neighbor_states` comparando el `last_seen` con `NEIGHBOR_TIMEOUT`.

**Entrada / Parámetros:**
*No recibe parámetros.* Usa:

- `self.NEIGHBOR_TIMEOUT` (segundos)
- `self.neighbor_states[nb] = {"last_seen", "alive", "cost", ...}`

**Salida / Retorno:**
`None`.

**Efectos de estado:**

- Para cada vecino `nb`:

  - Calcula `alive_now = (now - last_seen) < NEIGHBOR_TIMEOUT`.
  - Si `alive_now` difiere de `st["alive"]`, actualiza `st["alive"]` y marca `changed = True`.
- Si `changed`:

  - `self.topology_changed = True`
  - `self.calculateRoutes()` (SPF inmediato)

**Flujo (alto nivel):**

1. Lee `now = time.time()`.
2. Bajo `_lock`, recorre `neighbor_states` y actualiza `alive`.
3. Si hubo cambios, marca topología cambiada y corre SPF.

**Relación con otras partes:**

- `process_packet(HELLO)` y `update_neighbor()` refrescan `last_seen`; esta rutina “apaga” vecinos que **no** se refrescaron a tiempo.
- `create_lsa_packet()` solo anuncia como vecinos los **vivos** dentro de la ventana → un vecino marcado como no vivo dejará de aparecer en el LSA propio.
- Al poner `topology_changed = True`, `should_send_lsa()` podrá autorizar un LSA “rápido” (respetando `LSA_MIN_INTERVAL`).

**Complejidad:** `O(|neighbors|)`.

**Consideraciones:**

- No elimina entradas; solo conmuta `alive`. La eliminación (si se desea) es una decisión de diseño separada.
- Usa `time.time()` (no monotónico); si el reloj del sistema retrocede, los cálculos podrían verse afectados.

### `_age_lsa_database() -> None`

**Propósito:**
Retirar de `link_state_db` aquellas LSAs cuya marca `last_received` excede `LSA_MAX_AGE`.

**Entrada / Parámetros:**
*No recibe parámetros.* Usa:

- `self.LSA_MAX_AGE` (segundos)
- `self.link_state_db[origin] = {"seq", "neighbors", "last_received"}`

**Salida / Retorno:**
`None`.

**Efectos de estado:**

- Bajo `_lock`, elimina `link_state_db[origin]` si `now - last_received >= LSA_MAX_AGE`.
- Si se eliminó al menos una entrada:

  - `self.topology_changed = True`
  - `self.calculateRoutes()` (SPF inmediato)

**Flujo (alto nivel):**

1. Lee `now = time.time()`.
2. Bajo `_lock`, itera `link_state_db` (en copia `list(...)` para permitir `del`).
3. Borra LSAs vencidas y, si hubo cambios, marca y recalcula.

**Relación con otras partes:**

- Mantiene limpia la LSDB ante routers que dejaron de anunciarse.
- Al limpiar LSAs, cambia la topología efectiva → recalcula SPF y habilita (vía `topology_changed`) un nuevo LSA propio cuando `should_send_lsa()` lo permita.
- No toca el filtro de duplicados `lsa_seen` (que se limpia por FIFO en otros flujos).

**Complejidad:** `O(|LSDB|)`.

**Consideraciones:**

- Puede expulsar incluso el LSA propio si no se ha refrescado (debería prevenirse por `LSA_REFRESH_INTERVAL` en `should_send_lsa()` + `create_lsa_packet()`).
- El borrado puede provocar particiones visibles en rutas inmediatamente tras el SPF.

### Integración sugerida (ciclo de mantenimiento)

En el bucle principal del router (o en un *ticker* periódico):

```python
# pseudocódigo de servicio cada ~1s (o acorde a tus necesidades)
algo._check_neighbor_timeouts()
algo._age_lsa_database()

if algo.should_send_hello():
    pkt = algo.create_hello_packet()
    send(pkt)  # broadcast sin retransmisión

if algo.should_send_lsa():
    pkt = algo.create_lsa_packet()
    send(pkt)  # broadcast; el router hará flood controlado
```

**Frecuencia típica:** 500–1000 ms es suficiente; ambas rutinas son `O(n)` y baratas para topologías pequeñas/medianas.

### Contratos e invariantes

- **Thread-safety:** Ambas funciones toman `self._lock` para evitar carreras con `process_packet()` y `create_lsa_packet()`.
- **Consistencia SPF:** Cualquier cambio topológico local (vecino que “muere”/“resucita” o LSA que expira) **siempre** provoca un SPF inmediato.
- **Emisión LSA:** La bandera `topology_changed` es el gatillo que `should_send_lsa()` observa, amortiguado por `LSA_MIN_INTERVAL`.

## Cálculo de rutas (SPF/Dijkstra)

### `calculateRoutes() -> None`

**Propósito**
Construir el grafo de la topología a partir de:

- Vecinos directos **vivos** (`neighbor_states`)
- LSAs vigentes en la **LSDB** (`link_state_db`)

y ejecutar **Dijkstra** para obtener, para cada destino alcanzable, el **primer salto (first-hop)** desde `self.router_id`. La tabla resultante se guarda en `self.routing_table` como `{destino: vecino_next_hop}`.

**Parámetros / Retorno**
No recibe parámetros. Retorna `None`.
Actualiza estado interno:

- `self.area_routers`: conjunto de nodos presentes en el grafo.
- `self.routing_table`: mapa destino → next-hop.

**Sincronización**
Toda la rutina corre bajo `self._lock` para evitar condiciones de carrera con:

- `process_packet()` (ingreso de LSAs/HELLO)
- `_check_neighbor_timeouts()` (cambios de vecinos vivos)
- `_age_lsa_database()` (expiración de LSAs)
- `create_lsa_packet()` (publicación del propio LSA)

**Construcción del grafo (`adj`)**

- Se usa `defaultdict(dict)` bidireccional (grafo no dirigido con costo entero positivo).
- **Vecinos directos**: se agregan sólo si `alive == True`; costo por `st["cost"]`.
- **LSAs**: para cada `origin` y su lista `neighbors`, se crean aristas en ambos sentidos.
  Si ya existía una arista, se toma el **mínimo** costo observado (`min(prev, c)`) para robustez ante anuncios redundantes o asimetrías de entrada.

**Dijkstra (determinista)**

- Inicializa `dist[n] = ∞` y `first[n] = None`; `dist[src] = 0.0`.
- Conjunto `unvisited` con todos los nodos del grafo.
- Selección del próximo nodo `u`: `min(unvisited, key=(dist, nombre))` → desempate **determinista**.
- Relajación para cada vecino `v` de `u` (iterados en orden alfabético para más determinismo):

  - `alt = dist[u] + adj[u][v]`
  - `cand_first = v si u == src else first[u]`
    (Se “arrastra” el **primer salto** durante la relajación; evita reconstruir ruta al final).
  - Actualiza `dist[v]` y `first[v]` si:

    - `alt < dist[v]`, **o**
    - `alt == dist[v]` y `preferFirstHop(cand_first, first[v])` es `True`.

**Emisión de la FIB (tabla de reenvío)**

- Para cada `dst` con `dist[dst] < ∞` y `first[dst]` no nulo:

  - `new_table[dst] = first[dst]`
- Asigna `self.routing_table = new_table`.
  Si `src` no aparece en `adj` (sin vecinos/topología vacía), limpia la tabla.

**Criterios de diseño / Invariantes**

- **Determinismo**:

  1. elección de `u` desempata por nombre;
  2. vecinos de `u` se recorren ordenados;
  3. igual costo desempata con `preferFirstHop()`.
     Con esto se reduce el *route flapping* ante empates.
- **Robustez**: el **first-hop** se fija durante la relajación; evita ambigüedades en la reconstrucción.
- **Separación de responsabilidades**:

  - La **vigencia** de vecinos la decide `_check_neighbor_timeouts()` y `HELLO`.
  - La **vigencia** de LSAs la decide `_age_lsa_database()`.
  - `calculateRoutes()` consume un grafo ya “higienizado”.

**Complejidad**

- Construcción del grafo: `O(E)`.
- Dijkstra con selección por `min` lineal: `O(V^2 + E log 1)` ≈ `O(V^2 + E)` (sin heap).
  Suficiente para topologías pequeñas/medianas típicas de laboratorio.
  (Puede optimizarse con un heap si `V` crece).

**Casos borde**

- Topología desconectada: destinos con `dist = ∞` quedan **sin entrada** en `routing_table`.
- Costos duplicados/contradictorios en LSAs: se toma el **mínimo** observado.
- Sin vecinos vivos ni LSAs: `routing_table` se vacía.

### `preferFirstHop(cand: Optional[str], cur: Optional[str]) -> bool`

**Propósito**
Desempatar **rutas de costo igual** eligiendo el **primer salto** más estable y predecible.

**Reglas (en orden de prioridad)**

1. **Preferir alguno** frente a ninguno:

   - Si `cur is None` → `True` (acepta `cand`).
   - Si `cand is None` → `False`.
2. **Preferir vecinos directos vivos**:

   - Si `cand` es vecino con `alive=True` y `cur` no lo es → `True`.
   - Si `cur` es vivo y `cand` no → `False`.
3. **Orden lexicográfico** del nombre del vecino (determinismo):

   - Retorna `cand < cur`.

**Cuándo se usa**
Sólo cuando `alt == dist[v]` en Dijkstra (empate en costo total). Aporta **estabilidad** (elige nexthops activos) y **reproducibilidad** (mismo grafo ⇒ misma tabla).

### Interacción con el resto del sistema

- **HELLO/update\_neighbor** marcan y refrescan vecinos → afectan aristas “locales”.
- **LSAs** recibidos/emitidos pueblan/eliminan aristas “remotas”.
- **Timers** (`_check_neighbor_timeouts`, `_age_lsa_database`) gatillan `topology_changed`; el bucle principal, vía `should_send_lsa()`, puede emitir nuevos LSAs tras cambios significativos.
- **Reenvío**: `get_next_hop(dest)` consulta `self.routing_table` para decidir el vecino siguiente.

## Helpers

### `firstHop(dst: str, prev: Dict[str, Optional[str]]) -> Optional[str]`

**Propósito**
Obtener el **primer salto** desde `self.router_id` hacia `dst` recorriendo el mapa de **predecesores** `prev` (típico de Dijkstra si se guarda `prev[nodo] = padre`).

**Parámetros**

- `dst`: destino al que se quiere reenviar.
- `prev`: diccionario `nodo -> predecesor` (o `None` si es el origen).

**Retorno**

- `str` con el **vecino next-hop** si existe ruta.
- `None` si no hay ruta determinable.

**Algoritmo / Flujo**

1. Parte en `cur = dst` y camina hacia atrás por `prev[cur]` hasta:

   - Encontrar que `prev[cur] == self.router_id` → el **primer salto** es `cur`.
   - Encontrar `prev[cur] is None` (o falta de clave) → no hay ruta completa.
2. **Protección** anti-bucles: corta si se superan **1024** pasos.
3. Si no se pudo reconstruir pero `dst` es **vecino directo vivo**, retorna `dst`.
4. En otro caso, retorna `None`.

**Complejidad**

- `O(h)` donde `h` es la longitud del camino (acotado por 1024 para seguridad).

**Errores / Edge cases**

- Usa `prev.get(...)` para evitar `KeyError`.
- Si `prev` está incompleto o hay ciclo en `prev`, devuelve `None` por el límite de pasos.

**Relación con otras partes**

- Es una utilidad genérica para reconstrucción de **first-hop** a partir de un `prev`.
  En esta implementación, el SPF actual calcula el first-hop **durante la relajación** (ver `calculateRoutes()`), por lo que `firstHop()` queda como helper alternativo/legado.

### `handleHeadersPath(packet: Packet) -> bool`

**Propósito**
Mantener y validar `headers.path` como una **ventana deslizante de 3 nodos** para:

- Detectar y **cortar ciclos** locales.
- Anotar el paso del paquete por este router.

**Parámetros**

- `packet`: instancia de `Packet` a inspeccionar y actualizar.

**Retorno**

- `True` si es seguro continuar (path actualizado).
- `False` si se detecta **ciclo** o el `path` es inválido/no actualizable.

**Algoritmo / Flujo**

1. Obtiene la ruta actual con `packet.get_path()`; si falla, asume `[]`.
2. **Anti-loop local**: si `self.router_id` **ya está** en `path`, retorna `False` (drop).
3. Crea `new_path`:

   - Si `len(new_path) >= 3`, **pop(0)** para mantener ventana de 3.
   - `append(self.router_id)` para registrar el tránsito.
4. Intenta persistir con `packet.set_path(new_path)`.
   Si hay excepción → `False`.
5. Si todo va bien → `True`.

**Complejidad**

- `O(1)` amortizado; la lista nunca supera **3** elementos.

**Errores / Edge cases**

- Maneja silenciosamente `get_path()` inválido iniciando con `[]`.
- Si `set_path()` falla (p. ej., headers no mutables), retorna `False`.

**Relación con otras partes**

- Se invoca en `process_packet()` para **LSA/INFO** antes de aceptar/floodear el anuncio.
- Complementa el control de bucles junto con:

  - `ttl` (controlado por `router.py` en el **flood**).
  - Filtro de **duplicados** (`lsa_seen`/`lsa_fifo`) que evita re-procesar LSAs ya vistos.

**Notas de diseño**

- La ventana corta (3) equilibra coste y utilidad: suficiente para detectar **loops inmediatos** sin inflar headers.
- No es un mecanismo criptográfico; evita reenvíos triviales cíclicos en el plano de **control**.
