# OSRM — SmartWaste MVD

Servidor de routing auto-hospedado para Montevideo usando
[OSRM](https://github.com/Project-OSRM/osrm-backend) con datos de
OpenStreetMap (Geofabrik).

## Por qué OSRM y no Google Maps

| | OSRM (self-hosted) | Google Maps Distance Matrix |
|---|---|---|
| Costo | Gratis | USD 0.005 / elemento |
| Límite de matriz | 10 000 × 10 000 | 25 × 25 |
| Latencia | < 50 ms (local) | 100–500 ms |
| Datos de tráfico | No (perfil estático) | Sí |
| Dependencia externa | No | Sí |

Para una matriz 100 × 100 con Google Maps: **USD 50 por ejecución**.
Con OSRM: gratis.

---

## Requisitos

- Docker Desktop ≥ 4.x (o Docker Engine + Compose plugin)
- ~500 MB de espacio en disco para los datos procesados
- ~2 GB de RAM para el servidor

---

## Primera vez — preparar los datos

```bash
cd osrm/
./build-data.sh
```

El script hace 4 pasos automáticamente:

1. **Descarga** `uruguay-latest.osm.pbf` desde Geofabrik (~30 MB) — usando `curl` del host
2. **osrm-extract** — convierte el PBF a grafo OSRM con perfil `car.lua`
3. **osrm-partition** — divide el grafo en celdas para MLD
4. **osrm-customize** — calcula pesos de las celdas

Tiempo estimado: **~5 minutos** (red + CPU).
Los archivos resultantes quedan en `./data/`.

> **¿Por qué un script y no `docker compose run`?**
> La imagen de OSRM está basada en Debian Stretch (EOL). Sus repositorios
> de apt ya no existen, así que no se puede instalar `curl` ni `wget` dentro
> del contenedor. La descarga se hace en el host (macOS tiene `curl` nativo)
> y el contenedor solo procesa los datos.

> Para actualizar los datos (Geofabrik actualiza Uruguay mensualmente):
> volvé a ejecutar `./build-data.sh`. El flag `-z` de curl sólo descarga
> si el archivo remoto es más nuevo que el local.

---

## Levantar el servidor

```bash
docker compose up osrm-server
```

El servidor queda disponible en `http://localhost:5000`.

Para correrlo en background:

```bash
docker compose up -d osrm-server
```

---

## Verificar que funciona

### Tabla de distancias (el endpoint más usado por el route-optimizer)

```bash
curl "http://localhost:5000/table/v1/driving/-56.17,-34.88;-56.10,-34.83?annotations=duration,distance"
```

Respuesta esperada: JSON con matrices `durations` y `distances`.

### Ruta Centro → Felipe Cardoso (sitio de disposición)

```bash
curl "http://localhost:5000/route/v1/driving/-56.1913,-34.9059;-56.0967,-34.8347?overview=false"
```

### Snap de un punto a la red vial

```bash
curl "http://localhost:5000/nearest/v1/driving/-56.1913,-34.9059"
```

### Test completo con Python

```bash
# Desde la raíz del proyecto
python osrm/test_osrm.py
```

---

## Estructura de archivos

```
osrm/
├── Dockerfile           # Imagen del servidor (osrm-routed con MLD)
├── docker-compose.yml   # Servicios: osrm-prepare + osrm-server
├── README.md            # Este archivo
├── test_osrm.py         # Script de pruebas de integración
├── truck-profile.lua    # Perfil de camión (pendiente — Fase 4)
└── data/                # Archivos procesados (gitignored)
    ├── uruguay-latest.osm.pbf
    ├── uruguay-latest.osrm
    └── ...
```

> `data/` está en `.gitignore`. Cada developer/servidor debe correr
> `osrm-prepare` la primera vez.

---

## Notas de la API

**Importante**: la API de OSRM usa coordenadas en orden `(longitud, latitud)`,
al revés de la convención del proyecto. El `cuopt-client` y cualquier
código que llame a OSRM debe invertir el orden antes de armar la URL.

Ejemplo Python:

```python
# Convención del proyecto: (lat, lon)
centro = (-34.9059, -56.1913)

# Para OSRM: "{lon},{lat}"
osrm_coord = f"{centro[1]},{centro[0]}"  # "-56.1913,-34.9059"
```

---

## Perfil de vehículo

Por ahora se usa `car.lua` (incluido en la imagen base de OSRM).
En la Fase 4 se activará `truck-profile.lua` con restricciones de:
- Peso máximo (20 t)
- Altura máxima (4 m)
- Prohibición de ciertas calles residenciales
- Velocidades reducidas en zonas urbanas densas
