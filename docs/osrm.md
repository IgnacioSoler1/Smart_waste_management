# OSRM — SmartWaste MVD

Documentación técnica del servidor de routing auto-hospedado: qué hace, por qué lo elegimos, cómo procesa los datos de OpenStreetMap, qué devuelve cada endpoint y cómo lo usa el route-optimizer.

> **Código fuente:** `osrm/` (configuración y datos)
> **Cliente Python:** `cuopt-client/osrm_client.py`
> **Servidor en producción:** ECS Fargate (`smartwaste-dev-osrm`), accesible en `http://osrm.smartwaste.local:5000` dentro de la VPC

---

## Qué es OSRM

[OSRM (Open Source Routing Machine)](https://github.com/Project-OSRM/osrm-backend) es un motor de routing de alto rendimiento sobre datos de OpenStreetMap. Dado un grafo vial pre-procesado, calcula en milisegundos la ruta más corta entre dos o más puntos, o la tabla de distancias/duraciones entre N puntos simultáneamente.

En SmartWaste MVD, OSRM tiene una sola responsabilidad: **construir la matriz de costos** que necesita el solver VRP (cuOpt / OR-Tools). Esa matriz contiene el tiempo de viaje y la distancia en metros entre cada par de ubicaciones del circuito (depósito + contenedores).

---

## Por qué OSRM y no Google Maps

La alternativa obvia sería Google Maps Distance Matrix API, pero tiene limitaciones que la hacen inviable para este caso de uso:

| | OSRM (self-hosted) | Google Maps Distance Matrix |
|---|---|---|
| **Costo por ejecución** | $0 | USD 0.005 / elemento de la matriz |
| **Tamaño máximo de matriz** | 10.000 × 10.000 | 25 × 25 elementos |
| **Latencia** | < 50 ms (VPC interna) | 100–500 ms (internet) |
| **Datos de tráfico en tiempo real** | No (perfil estático) | Sí |
| **Dependencia de internet** | No | Sí |
| **Control sobre el perfil de vehículo** | Total (archivo Lua) | Limitado |

**El costo sería el factor decisivo.** El route-optimizer corre cada 15 minutos sobre 134 circuitos. Un circuito típico tiene ~100 contenedores + 3 camiones + 1 depósito = 104 ubicaciones. Una matriz 104×104 = 10.816 elementos. Con Google Maps: **USD 54 por ejecución × 96 ejecuciones/día = USD 5.184/día**. Con OSRM: $0.

**El límite de 25×25 de Google Maps también sería un bloqueante duro**: los circuitos de Montevideo tienen hasta ~200 contenedores.

---

## Cómo funciona internamente

### 1. Los datos: OpenStreetMap en formato PBF

La red vial de Uruguay viene de [Geofabrik](https://download.geofabrik.de/south-america/uruguay-latest.osm.pbf) en formato **PBF** (Protocol Buffer Format), un volcado comprimido de todos los objetos OpenStreetMap de Uruguay (~30 MB).

El archivo incluye:
- Todos los segmentos viales con sus atributos (tipo de calle, sentido, límite de velocidad, etc.)
- Intersecciones con sus metadatos
- Nombres de calles, restricciones de giro, etc.

Geofabrik actualiza el PBF de Uruguay aproximadamente cada mes. El script `build-data.sh` usa `curl -z` (timestamping) para descargar solo si hay una versión más nueva.

### 2. El pipeline de pre-procesamiento

El PBF no es directamente utilizable por OSRM. Hay que convertirlo en un grafo optimizado para búsqueda de rutas. Ese proceso tiene tres pasos, ejecutados por `build-data.sh`:

```
uruguay-latest.osm.pbf
        │
        ▼  osrm-extract  (perfil: truck-profile.lua)
uruguay-latest.osrm + archivos auxiliares (.ebg, .enw, .geometry, .names, ...)
        │
        ▼  osrm-partition  (MLD: divide el grafo en celdas jerárquicas)
uruguay-latest.osrm.partition + .mldgr
        │
        ▼  osrm-customize  (calcula pesos de las celdas — puede actualizarse sin re-extraer)
uruguay-latest.osrm.cell_metrics
```

**`osrm-extract`** parsea el PBF y aplica el perfil de vehículo (`truck-profile.lua`) para determinar qué calles son transitables, a qué velocidad y en qué dirección, respetando restricciones específicas de camiones pesados. El resultado es un grafo vial optimizado para camiones de recolección.

**`osrm-partition`** aplica el algoritmo MLD (Multi-Level Dijkstra): divide el grafo en celdas a múltiples niveles de jerarquía, similar a cómo un mapa se divide en regiones → provincias → países. Esto permite acelerar drasticamente las búsquedas de larga distancia.

**`osrm-customize`** precalcula los pesos (tiempos de viaje) para cada celda del grafo particionado. Este paso se puede repetir sin re-extraer, lo que permite actualizar velocidades (por ejemplo, incorporar datos de tráfico) sin reprocesar todo el grafo.

### 3. El algoritmo: MLD vs CH

OSRM soporta dos algoritmos de routing:

- **CH (Contraction Hierarchies):** pre-contrae el grafo eliminando nodos intermedios. Muy rápido para rutas punto a punto, pero la Table API con muchos orígenes/destinos simultáneos es más lenta porque cada par es independiente.

- **MLD (Multi-Level Dijkstra):** divide el grafo en celdas jerárquicas. La Table API puede reutilizar exploraciones parciales entre múltiples pares, lo que lo hace significativamente más eficiente para matrices N×N grandes.

Usamos **MLD** porque el caso de uso principal es la Table API con ~100 ubicaciones simultáneas (todos los contenedores de un circuito). La configuración está en `osrm/Dockerfile`:

```dockerfile
CMD ["osrm-routed", "--algorithm", "mld", "--port", "5000", "--max-table-size", "10000", "/data/uruguay-latest.osrm"]
```

`--max-table-size 10000` sube el límite por defecto (que era 100) para permitir matrices de hasta 10.000 ubicaciones, suficiente para cualquier circuito de Montevideo.

### 4. Por qué la descarga del PBF se hace en el host y no en el contenedor

La imagen base de OSRM está construida sobre **Debian Stretch** (EOL, fin de soporte en junio 2022). Sus repositorios de APT ya no existen en los mirrors normales. No es posible instalar `curl` ni `wget` con `apt-get` directamente.

Por eso `build-data.sh` divide el trabajo:
1. La descarga del PBF la hace `curl` del host (macOS lo tiene nativo)
2. El procesamiento (extract, partition, customize) lo hace el contenedor, que sí incluye las herramientas OSRM

Para la imagen de producción (`Dockerfile.ecr`), que necesita `curl` para el health check de ECS, redirigimos APT a los mirrors de archivo:

```dockerfile
RUN printf 'deb http://archive.debian.org/debian stretch main\n...' > /etc/apt/sources.list \
    && apt-get update -o Acquire::Check-Valid-Until=false \
    && apt-get install -y --no-install-recommends curl
```

---

## Despliegue

### Local (desarrollo)

```bash
# Primera vez: procesar los datos de Uruguay
cd osrm/
./build-data.sh       # descarga PBF + extract + partition + customize (~5 min)

# Levantar el servidor
docker compose up osrm-server

# Verificar
python osrm/test_osrm.py
```

El servidor queda disponible en `http://localhost:5000`.

### Producción (ECS Fargate en AWS)

En producción, los datos de Uruguay se **embeben en la imagen Docker** durante el CI/CD. `Dockerfile.ecr` copia los archivos procesados directamente:

```dockerfile
COPY data/uruguay-latest.osrm*  /data/
```

Esto significa que la imagen ECR (`<YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/smartwaste-dev-osrm`) ya contiene el grafo listo para servir. El contenedor arranca directamente sin necesidad de montar volúmenes ni procesar datos en runtime.

El servicio corre en **ECS Fargate** (CPU: 2048 / 2 vCPU, Memoria: 4096 MB / 4 GB) dentro de la VPC privada y es accesible mediante **AWS Cloud Map**:

```
DNS: osrm.smartwaste.local:5000
```

La Lambda route-optimizer (también en la VPC) resuelve este nombre internamente via Cloud Map, sin pasar por internet.

El health check de ECS usa `curl`:

```bash
curl -sf 'http://127.0.0.1:5000/nearest/v1/driving/-56.1645,-34.9011'
```

Si el contenedor no responde a este endpoint en el tiempo configurado, ECS lo reemplaza automáticamente.

---

## Los tres endpoints que usamos

### 1. Table API — Matriz de distancias y duraciones

**El endpoint más crítico del sistema.** Construye la matriz de costos para el solver VRP.

**URL:**
```
GET /table/v1/driving/{coordenadas}?annotations=duration,distance
```

**Coordenadas:** lista de `lon,lat` separados por `;`

**Ejemplo** (3 puntos en Montevideo):
```bash
curl "http://localhost:5000/table/v1/driving/-56.1913,-34.9059;-56.1526,-34.9008;-56.2476,-34.8924?annotations=duration,distance"
```

**Respuesta:**
```json
{
  "code": "Ok",
  "durations": [
    [0.0,  420.3, 780.1],
    [418.7,  0.0, 650.4],
    [793.2, 658.9,  0.0]
  ],
  "distances": [
    [0.0,    4820.5, 9200.3],
    [4810.0,    0.0, 7650.8],
    [9250.1, 7680.2,    0.0]
  ],
  "sources":      [...],
  "destinations": [...]
}
```

- `durations[i][j]`: segundos de viaje desde la ubicación `i` hasta la `j` usando la red vial real
- `distances[i][j]`: metros de recorrido real por las calles (no distancia en línea recta)
- La diagonal es `0` (un punto a sí mismo)
- La matriz **no es simétrica**: el tiempo de `A→B` puede diferir de `B→A` por calles de sentido único

**Cómo lo usa el route-optimizer** (`osrm_client.py:199`):

```python
result = client.get_distance_matrix(locations)  # locations: [(lat, lon), ...]
cost_matrix = [[int(d) for d in row] for row in result["durations"]]
# cost_matrix se pasa directamente a cuOpt/OR-Tools como matriz de costos
```

El límite por llamada está configurado en 500 ubicaciones (`_MAX_TABLE_SIZE = 500`), suficiente para cualquier circuito de Montevideo con margen.

---

### 2. Route API — Geometría y pasos de navegación

Dado un conjunto de waypoints ordenados, devuelve la ruta óptima pasando por todos ellos: distancia total, duración y la geometría exacta de las calles (no líneas rectas).

**URL:**
```
GET /route/v1/driving/{coordenadas}?overview=full&geometries=geojson&steps=true
```

**Ejemplo:**
```bash
curl "http://localhost:5000/route/v1/driving/-56.1913,-34.9059;-56.0967,-34.8347?overview=full&geometries=geojson&steps=true"
```

**Respuesta (simplificada):**
```json
{
  "code": "Ok",
  "routes": [{
    "distance": 12450.8,
    "duration": 1423.2,
    "geometry": {
      "type": "LineString",
      "coordinates": [
        [-56.1913, -34.9059],
        [-56.1891, -34.9043],
        ...
        [-56.0967, -34.8347]
      ]
    },
    "legs": [{
      "steps": [{
        "maneuver": {
          "type": "depart",
          "bearing_after": 47,
          "location": [-56.1913, -34.9059]
        },
        "name": "Avenida 18 de Julio",
        ...
      }]
    }]
  }]
}
```

**Cómo lo usa el route-optimizer:**

Se usa para dos propósitos distintos:

**a) Obtener la geometría real de la ruta** (para mostrarla en el mapa del conductor):

```python
# handler.py:363 — después de calcular la ruta óptima con el solver
geo_result = _osrm.get_route(waypoints, bearings=route_bearings)
route_geometry = [
    [Decimal(str(round(lat, 6))), Decimal(str(round(lon, 6)))]
    for lon, lat in geo_result["geometry"]["coordinates"]
    # ↑ OSRM devuelve [lon, lat] → invertimos a [lat, lon]
]
```

La geometría se guarda en DynamoDB y la app del conductor la renderiza como `Polyline` en el mapa.

**b) Obtener el bearing de cada calle** (para mejorar el snap de coordenadas a la red vial):

```python
# osrm_client.py:116 — get_road_bearings()
# Para cada ubicación, calcula una ruta corta ~30m al norte
# y extrae el bearing_after del primer maneuver
offset_lat = lat + 0.00027  # ~30 metros al norte
coords = f"{lon},{lat};{lon},{offset_lat}"
data = GET /route/v1/driving/{coords}?steps=true&overview=false
bearing = data["routes"][0]["legs"][0]["steps"][0]["maneuver"]["bearing_after"]
```

Este bearing indica la dirección de la calle en ese punto (ej: 47° = nor-nordeste). Se pasa a la Table API para que OSRM haga el snap al segmento vial en la dirección correcta, evitando que un contenedor en una avenida de doble mano se "snapee" al carril equivocado.

**Circuit breaker en `get_road_bearings`:** si hay 3 fallos consecutivos (timeout / conexión), el loop se aborta y el resto de las ubicaciones se optimiza sin bearings, para no quemar N × 8s del timeout de Lambda.

---

### 3. Nearest API — Snap a la red vial

Dado un punto geográfico, devuelve el segmento de calle más cercano y la posición exacta en ese segmento.

**URL:**
```
GET /nearest/v1/driving/{lon},{lat}?number=1
```

**Ejemplo:**
```bash
curl "http://localhost:5000/nearest/v1/driving/-56.1913,-34.9059?number=1"
```

**Respuesta:**
```json
{
  "code": "Ok",
  "waypoints": [{
    "name":     "Avenida 18 de Julio",
    "location": [-56.19124, -34.90587],
    "distance": 4.2
  }]
}
```

- `location`: coordenadas del punto snapeado a la red vial más cercana
- `distance`: distancia en metros desde el punto original al snap
- `name`: nombre de la calle

Se usa principalmente en el **health check de ECS** y en los tests de integración (`test_osrm.py`) para verificar que el servidor responde y que los datos de Uruguay están cargados correctamente. Los tests validan que el snap sea a menos de 50 metros del punto original y que el nombre de calle esté presente (lo que confirma que los metadatos del PBF se cargaron bien).

---

## Inversión de coordenadas: la trampa más común

**Toda la API de OSRM usa coordenadas en orden `(longitud, latitud)`**, al contrario de la convención estándar geográfica y del resto del proyecto (que siempre usa `(latitud, longitud)`).

Esto significa que el Centro de Montevideo, que en el proyecto es `(-34.9059, -56.1913)`, en una URL de OSRM debe escribirse como `-56.1913,-34.9059`.

El cliente `OSRMClient` maneja esta inversión internamente en `_to_osrm_coord()`:

```python
@staticmethod
def _to_osrm_coord(lat: float, lon: float) -> str:
    return f"{lon},{lat}"  # OSRM espera lon primero
```

Todos los métodos públicos del cliente (`get_distance_matrix`, `get_route`, `get_road_bearings`) aceptan `(latitud, longitud)` como el resto del proyecto. La conversión nunca es responsabilidad del caller.

El único lugar donde hay que tener cuidado es al **leer la geometría devuelta** por la Route API, que también viene en `[lon, lat]`:

```python
route_geometry = [
    [lat, lon]
    for lon, lat in geo_result["geometry"]["coordinates"]  # invertir explícitamente
]
```

---

## Fallback: haversine cuando OSRM no está disponible

Para desarrollo local sin Docker, o en tests donde OSRM no está corriendo, el cliente implementa un fallback que calcula la matriz usando la **fórmula haversine** (distancia en línea recta entre dos puntos de la esfera terrestre) con dos ajustes empíricos:

```python
_URBAN_SPEED_MS = 30_000 / 3600   # 30 km/h — velocidad media urbana Montevideo
_DETOUR_FACTOR  = 1.35             # calles no son línea recta
```

El detour factor de 1.35 significa que la distancia real por calles suele ser un 35% mayor que la línea recta. Este valor es una aproximación empírica razonable para la trama urbana de Montevideo.

Se activa con la variable de entorno:

```bash
OSRM_FALLBACK=haversine
```

En producción esta variable nunca está seteada. En desarrollo local la Lambda route-optimizer detecta que `osrm_url == "http://localhost:5000"` y activa el fallback automáticamente (ver `lambdas/shared/` — la lógica está documentada en `MEMORY.md`).

**Limitación importante:** el fallback haversine produce matrices menos precisas. Las rutas optimizadas con haversine pueden diferir significativamente de las reales porque no considera calles de sentido único, autopistas, ni la topología real de Montevideo. Solo apto para desarrollo y testing.

---

## Limitaciones del sistema actual

### Sin tráfico en tiempo real

OSRM usa **perfiles estáticos de velocidad** definidos en `truck-profile.lua`. No hay integración con datos de tráfico en tiempo real (Google Maps, HERE, TomTom). Esto significa que:

- La duración estimada para las 8:00 AM (hora punta) es la misma que para las 3:00 AM
- Eventos inesperados (cortes de calle, manifestaciones, obras) no se reflejan
- Las rutas están optimizadas para condiciones de tráfico "promedio"

**Por qué no integramos tráfico todavía:** los datos de tráfico en tiempo real para Uruguay tienen cobertura limitada y calidad variable. Además, el ciclo del optimizer (15 minutos) es suficientemente corto como para que las variaciones de tráfico dentro de un ciclo sean menores que la incertidumbre del modelo.

En el futuro se podría usar el mecanismo `osrm-customize` (el tercer paso del pipeline) para actualizar los pesos del grafo con datos de tráfico sin necesidad de re-extraer todo el grafo desde el PBF.

### Freshness de los datos viales

Geofabrik actualiza el PBF de Uruguay aproximadamente **una vez al mes**. Los cambios en la red vial (nuevas calles, cambios de sentido, obras permanentes) no se reflejan hasta la próxima actualización del PBF y el re-proceso de los datos.

Para actualizar:
```bash
cd osrm/
./build-data.sh  # curl -z descarga solo si el PBF remoto es más nuevo
```

### Perfil de vehículo: `truck-profile.lua`

El procesamiento usa el perfil de camión personalizado `osrm/truck-profile.lua`, diseñado específicamente para camiones de recolección lateral de Montevideo (~20 t, 4 m de alto, 2.5 m de ancho).

#### Cómo está implementado

OSRM no incluye ningún perfil de camión por defecto — solo viene con `car.lua`, `foot.lua` y `bicycle.lua`. Escribimos `truck-profile.lua` tomando `car.lua` de la imagen como base y aplicando cambios quirúrgicos sobre los parámetros del `setup()`.

Un detalle crítico de implementación: la imagen `osrm/osrm-backend:latest` usa **`api_version = 4`**. En versiones anteriores del API (v1, v2), los perfiles implementaban `process_way` manualmente con lógica propia. En v4, toda la lógica de extracción está encapsulada en **`WayHandlers`** — un sistema de handlers encadenados que leen los parámetros directamente desde el objeto `profile` devuelto por `setup()`. Esto significa que `process_way`, `process_node` y `process_turn` son idénticos a `car.lua`; los únicos cambios están en los valores del `setup()`.

```lua
api_version = 4  -- debe coincidir con la versión de la imagen

-- Los handlers leen vehicle_height, vehicle_weight, etc. desde el profile:
WayHandlers.handle_height,   -- verifica maxheight vs profile.vehicle_height
WayHandlers.handle_width,    -- verifica maxwidth  vs profile.vehicle_width
WayHandlers.handle_weight,   -- verifica maxweight vs profile.vehicle_weight
WayHandlers.access,          -- verifica tags de acceso según access_tags_hierarchy
```

#### Cambios respecto a `car.lua`

**Dimensiones del vehículo** — el handler correspondiente excluye automáticamente cualquier vía cuyo tag OSM sea inferior al valor configurado:

| Parámetro | `car.lua` | `truck-profile.lua` | Descripción |
|---|---|---|---|
| `vehicle_weight` | 2.000 kg | **20.000 kg** | Excluye vías con `maxweight < 20 t` |
| `vehicle_height` | 2,0 m | **4,0 m** | Excluye pasos bajos con `maxheight < 4 m` |
| `vehicle_width` | 1,9 m | **2,5 m** | Excluye calles con `maxwidth < 2,5 m` |
| `vehicle_length` | 4,8 m | **9,5 m** | Excluye vías con `maxlength < 9,5 m` |

**Jerarquía de acceso** — se agregaron `hgv` y `goods` con mayor prioridad que `motorcar`, para que el tag estándar OSM de vehículos pesados sea respetado:

```lua
-- truck-profile.lua
access_tags_hierarchy = Sequence { 'hgv', 'goods', 'motorcar', 'motor_vehicle', 'vehicle', 'access' }

-- car.lua (referencia)
access_tags_hierarchy = Sequence { 'motorcar', 'motor_vehicle', 'vehicle', 'access' }
```

**`delivery` permitido** — en `car.lua`, `delivery` está en la blacklist (los coches no suelen tener acceso a vías de reparto). Para camiones de recolección, `delivery` debe estar en la whitelist porque la recolección de residuos es técnicamente un servicio de recogida y debe acceder a vías etiquetadas `hgv=delivery`:

```lua
access_tag_whitelist = Set { 'yes', ..., 'delivery' }  -- agregado
access_tag_blacklist = Set { 'no', 'private', ... }    -- 'delivery' eliminado
```

**Velocidades reducidas** — los camiones circulan más lento que los coches, especialmente en zonas residenciales donde están los contenedores:

| Tipo de vía | `car.lua` | `truck-profile.lua` |
|---|---|---|
| `motorway` | 90 km/h | 70 km/h |
| `primary` | 65 km/h | 40 km/h |
| `secondary` | 55 km/h | 35 km/h |
| `residential` | 25 km/h | 20 km/h |
| `living_street` | 10 km/h | 10 km/h |
| `service` | 15 km/h | 15 km/h |

**`weight_name = 'duration'`** — a diferencia de `car.lua` que usa `'routability'` (duración con bonus por tipo de vía), el perfil de camión usa optimización pura por tiempo. Los camiones tienen que pasar por calles residenciales obligatoriamente, no tiene sentido penalizar esas vías.

#### Restricciones que aplica sobre el grafo vial

| Tag OSM | Comportamiento |
|---|---|
| `hgv=no` | Excluye la vía del grafo |
| `hgv=delivery` | Permite el paso |
| `hgv=private` | Excluye la vía |
| `maxweight < 20` (toneladas) | Excluye puentes y calles con límite de peso insuficiente |
| `maxheight < 4.0` (metros) | Excluye pasos bajos (túneles, viaductos, garajes) |
| `maxwidth < 2.5` (metros) | Excluye calles demasiado estrechas |
| `maxlength < 9.5` (metros) | Excluye calles demasiado cortas para maniobrar |

**Nota:** los tags `maxweight`, `maxheight` y `maxwidth` tienen cobertura limitada en OSM Uruguay. En la práctica, pocos segmentos tienen estas restricciones pobladas. El principal efecto del truck profile hoy es la jerarquía de acceso HGV y las velocidades reducidas.

#### Cómo actualizar el perfil y hacer deploy

Cualquier cambio en `truck-profile.lua` requiere re-procesar los datos y reconstruir la imagen ECR, porque el grafo vial se genera en tiempo de build (no en runtime):

```bash
# 1. Re-procesar el grafo con el nuevo perfil
cd osrm/
./build-data.sh

# 2. Reconstruir la imagen ECR con los nuevos datos
cd ..
aws ecr get-login-password --region us-east-1 --profile personal-smart-recycle \
  | docker login --username AWS --password-stdin \
      <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com

docker build --platform linux/amd64 \
  -f osrm/Dockerfile.ecr \
  -t smartwaste-osrm:latest \
  osrm/

docker tag smartwaste-osrm:latest \
  <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/smartwaste-dev-osrm:latest

docker push \
  <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/smartwaste-dev-osrm:latest

# 3. Forzar nuevo deployment en ECS
aws ecs update-service \
  --cluster smartwaste-dev-cluster \
  --service smartwaste-dev-osrm \
  --force-new-deployment \
  --region us-east-1 \
  --profile personal-smart-recycle
```

ECS descarta el task viejo, arranca uno nuevo con la imagen actualizada, y lo marca como healthy cuando el health check (`/nearest`) responde HTTP 200.

---

## Estructura de archivos

```
osrm/
├── Dockerfile            # Servidor local (monta ./data/ como volumen)
├── Dockerfile.ecr        # Imagen producción (datos embedidos, curl para health check)
├── docker-compose.yml    # Servicios: osrm-prepare (build) + osrm-server (runtime)
├── build-data.sh         # Script: descarga PBF + extract + partition + customize
├── truck-profile.lua     # Perfil camión activo (api_version=4, basado en car.lua)
├── test_osrm.py          # Tests de integración (Table, Route, Nearest)
└── data/                 # Archivos procesados (gitignored)
    ├── uruguay-latest.osm.pbf       # Mapa fuente (~30 MB)
    ├── uruguay-latest.osrm          # Grafo base
    ├── uruguay-latest.osrm.ebg      # Edge-based graph
    ├── uruguay-latest.osrm.enw      # Edge-node weights
    ├── uruguay-latest.osrm.partition # Partición MLD
    ├── uruguay-latest.osrm.mldgr    # Multi-level graph
    └── uruguay-latest.osrm.cell_metrics  # Pesos de celdas (customize)
```

> `data/` está en `.gitignore`. Cada developer y el CI/CD deben ejecutar `./build-data.sh` la primera vez.

---

## Comandos de referencia

```bash
# Preparar datos (primera vez o para actualizar)
cd osrm/
./build-data.sh

# Servidor local en background
docker compose up -d osrm-server

# Ver logs del servidor
docker compose logs -f osrm-server

# Tests de integración
python osrm/test_osrm.py
python osrm/test_osrm.py --base-url http://osrm.smartwaste.local:5000

# Health check manual
curl -sf "http://localhost:5000/nearest/v1/driving/-56.1645,-34.9011"

# Matriz 2×2 rápida (Centro → Pocitos)
curl "http://localhost:5000/table/v1/driving/-56.1913,-34.9059;-56.1526,-34.9008?annotations=duration,distance"

# Verificar servidor en ECS
aws ecs describe-services \
  --cluster smartwaste-dev-cluster \
  --services smartwaste-dev-osrm \
  --region us-east-1 \
  --profile personal-smart-recycle \
  --query 'services[0].{Status:status,Running:runningCount,Desired:desiredCount}'

# ── Rebuild y deploy a producción (tras cambiar truck-profile.lua o actualizar datos) ──

# Login ECR
aws ecr get-login-password --region us-east-1 --profile personal-smart-recycle \
  | docker login --username AWS --password-stdin \
      <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com

# Build imagen (desde la raíz del proyecto)
docker build --platform linux/amd64 \
  -f osrm/Dockerfile.ecr \
  -t smartwaste-osrm:latest \
  osrm/

# Push a ECR
docker tag smartwaste-osrm:latest \
  <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/smartwaste-dev-osrm:latest
docker push \
  <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/smartwaste-dev-osrm:latest

# Deploy (ECS reemplaza el task automáticamente)
aws ecs update-service \
  --cluster smartwaste-dev-cluster \
  --service smartwaste-dev-osrm \
  --force-new-deployment \
  --region us-east-1 \
  --profile personal-smart-recycle

# Seguir los logs del nuevo task
aws logs tail /ecs/smartwaste-dev/osrm --follow \
  --region us-east-1 \
  --profile personal-smart-recycle
```
