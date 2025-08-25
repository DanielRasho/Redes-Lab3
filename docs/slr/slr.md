# LinkStateRouting — Estado base y constantes

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

## Propósito del módulo

Implementa un algoritmo de **Link State Routing (LSR)** estilo OSPF simplificado, diseñado para un entorno **pub/sub** (e.g. Redis). Gestiona **descubrimiento de vecinos (HELLO)**, **difusión de LSAs (INFO)**, **cálculo SPF (Dijkstra)** y **control de flooding**/deduplicación, exponiendo una **tabla de encaminamiento** (next-hop por destino) que consumen capas superiores (p. ej., `router.py`).

## Mensajería (convenciones)

* `proto="lsr"`
* `type`:

  * `hello` (broadcast, **no** se retransmite)
  * `info` (LSA; broadcast, **sí** se retransmite con TTL--)
  * `message` / `echo` (unicast; se reenvía según la tabla SPF)
* `headers.path`: ventana de los **últimos 3 routers** para detectar bucles.
* `headers.msg_id`: UUID para **deduplicación** (LRU en `router.py`).

> **Conexión con otras partes**
>
> * `src.algorithms.base.RoutingAlgorithm`: clase base (provee `router_id` y contrato común).
> * `src.packet.Packet`: representación de paquetes que usa los headers anteriores.
> * `router.py`: orquesta I/O pub/sub y decide retransmitir cuando el algoritmo retorna una acción tipo *flood LSA*.

## Clase: `LinkStateRouting`

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

* Un planificador (en otra parte del módulo) dispara `HELLO` cada `HELLO_INTERVAL`.
* Si un vecino no envía `HELLO` dentro de `NEIGHBOR_TIMEOUT`, se marca `alive=False` y se dispara recálculo SPF.
* El LSA propio se emite **como mínimo** cada `LSA_MIN_INTERVAL` cuando hay cambios y se **refresca** cada `LSA_REFRESH_INTERVAL`.
* LSAs antiguos se purgan al superar `LSA_MAX_AGE`.

### Constructor

```python
def __init__(self, router_id: str):
    super().__init__(router_id)
    ...
```

**Parámetros**

* `router_id: str` — Identificador único del router (p. ej., “A”, “R1”, UUID corto). Viene de configuración superior.

**Retorno**

* `None` (constructor).

**Efectos/Side effects**

* Inicializa todo el **estado mutable** (vecinos, LSDB, control LSA, locks).
* Registra el propio `router_id` en `area_routers`.

**Excepciones**

* No lanza explícitamente; cualquier validación de `router_id` sucede en `RoutingAlgorithm`.

### Atributos de estado (y su rol)

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

* `neighbor_states` y `link_state_db` constituyen la **fuente de verdad** del grafo. Cualquier cambio marca `topology_changed=True`.
* `lsa_seen` + `lsa_fifo` implementan una **LRU manual**: si el tamaño supera `lsa_capacity`, se popleft de la `deque` y se elimina la clave del `set`. Evita re-procesar/re-floodear LSAs duplicados.
* `RLock` permite que callbacks internos reingresen secciones críticas sin deadlocks (útil cuando un evento dispara otro que vuelve a tocar el estado).

## Flujo lógico (de alto nivel) cubierto por este fragmento

1. **Inicialización**
   Se crea el contenedor de estado (vecinos, LSDB, dedupe) y se arman los **timers** de referencia (constantes).

2. **Recepción de HELLO / INFO (en otras funciones del módulo)**

   * HELLO: actualiza `neighbor_states[nb] = {cost,last_seen,alive=True}`.
   * INFO (LSA): si `(origin, seq)` ∉ `lsa_seen`, se integra en `link_state_db` y se marca **para flooding** y SPF.

3. **Timers**

   * Envío periódico de HELLO (`HELLO_INTERVAL`).
   * Emisión/refresh del LSA propio cuando corresponde (`LSA_MIN_INTERVAL`, `LSA_REFRESH_INTERVAL`).
   * Expiración de vecinos (`NEIGHBOR_TIMEOUT`).
   * Purga de entradas LSDB por edad (`LSA_MAX_AGE`).

4. **Integración con router.py**
   Cuando este algoritmo **indica** “flood\_lsa” (en métodos no mostrados aún), `router.py` se encarga de publicar el LSA en broadcast y de **no** retransmitir `hello`.

## Contratos y garantías

* **Thread-safety:** toda mutación de `neighbor_states`, `link_state_db`, `lsa_seen` y marcas de tiempo se debe hacer bajo `_lock`.
* **Deduplicación fuerte:** `lsa_seen` asegura que un LSA con `(origin, seq)` no se procesa ni se vuelve a inundar.
* **Rate-limiting de LSAs propios:** nunca se emite más frecuente que `LSA_MIN_INTERVAL`; siempre se renueva antes de `LSA_MAX_AGE` mediante `LSA_REFRESH_INTERVAL`.
* **Reconvergencia:** cualquier cambio en vecinos/LSDB seta `topology_changed=True` para que el **SPF** recalcule la **routing\_table** (implementación en métodos posteriores).
