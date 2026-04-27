# CLAUDE.md — SmartWaste MVD

## What is this project?

Real-time route optimization for waste collection trucks in Montevideo, Uruguay. Containers have simulated IoT fill-level sensors; the system calculates optimal pickup routes for each truck based on which containers need emptying most urgently.

## Why it matters

Montevideo has ~13,000 waste containers across 117 collection circuits. Today routes are static. This system makes them dynamic — trucks skip near-empty containers and prioritize full ones, reducing km driven, fuel burned, and overflowing containers.

## Architecture (short version)

```
Sensor Simulator → AWS IoT Core (MQTT) → Lambda → DynamoDB
                                                      ↓
EventBridge (every 15 min) → Lambda route-optimizer
                                ├── reads container fill levels from DynamoDB
                                ├── calls OSRM (ECS Fargate) for distance matrix
                                ├── calls NVIDIA cuOpt for VRP solution
                                └── pushes new route via WebSocket → Driver App
```

See `docs/architecture.md` for the full breakdown.

## Key tech decisions

- **OSRM self-hosted** (not Google Maps) for distance matrices — no per-request cost, no 25x25 element limit, sub-50ms latency. Uses OpenStreetMap data for Uruguay.
- **NVIDIA cuOpt** for VRP solving — GPU-accelerated, supports capacity constraints, time windows, multi-depot. Start with NVIDIA API Catalog (free tier), migrate to EC2 GPU for production.
- **DynamoDB** for operational state — containers, trucks, routes, sensor readings.
- **S3 + Athena** for historical analytics (sensor readings via Kinesis Firehose → Parquet).
- **All infrastructure on AWS**, defined in Terraform.

## Data sources

Real locations from Intendencia de Montevideo open data:
- Containers CSV: `catalogodatos.gub.uy` — `gid, cod_circuito, turno_horario, motivo, x, y` (UTM 21S → convert to WGS84)
- Circuits shapefile: polygons defining each collection circuit with municipality info
- Historical pickup data: which containers were collected each day

Coordinates come in **SIRGAS2000 UTM 21S (EPSG:31981)** and must be converted to **WGS84 (EPSG:4326)** using `pyproj`. The conversion script is `data/scripts/convert_coordinates.py`.

## Montevideo-specific context

- 117 circuits, ~100 containers each
- 3 shifts: morning (M), afternoon (V), night (N)
- 2 zones: east and west
- Disposal site: Felipe Cardoso (northeast, -34.8347, -56.0967)
- Transfer station: Ruta 102 (west zone, -34.8128, -56.2645)
- Trucks hold ~25 tons, use lateral lift system
- ~1,200 tons/day of household waste

## Project structure

```
smartwaste-mvd/
├── terraform/              # AWS infrastructure as code
├── data/
│   ├── scripts/            # ETL: download, convert coordinates, seed DB
│   │   └── convert_coordinates.py
│   ├── raw/                # Original CSVs from Intendencia (gitignored)
│   └── processed/          # Clean CSVs with WGS84 coords
├── simulator/              # IoT sensor fill-level simulator
├── lambdas/
│   ├── process-sensor-reading/
│   ├── route-optimizer/
│   ├── websocket-connect/
│   └── websocket-message/
├── osrm/                   # Dockerfile + truck profile for OSRM
├── cuopt-client/           # NVIDIA cuOpt VRP solver client
├── frontend-driver/        # React PWA for truck drivers
├── frontend-dashboard/     # React operations dashboard
└── docs/
    └── architecture.md
```

## Conventions

- **Python 3.11+** for all backend code. Type hints required.
- **Node 20+** / **React 18+** with TypeScript for frontends.
- **Terraform** (not CDK) for infrastructure.
- AWS region: **us-east-1** (cheapest for IoT Core + GPU instances).
- Coordinates are always `(latitude, longitude)` in code, never `(x, y)` after conversion.
- Container IDs are strings matching the `gid` from Intendencia data.
- Circuit IDs are strings matching `cod_circuito` from Intendencia data.
- All timestamps in UTC ISO 8601.
- DynamoDB table names prefixed with `smartwaste-`.

## Current limitations

1. **No real sensors** — fill levels are simulated. The simulator models realistic fill curves (time-of-day, day-of-week, zone density). When real sensors are installed, they publish to the same MQTT topics and the rest of the pipeline doesn't change.
2. **No real truck GPS** — truck positions are simulated or manually set. In production, OBD-II devices would publish to IoT Core.
3. **cuOpt requires GPU** — for development, use the NVIDIA API Catalog (free 5K requests). For production, need EC2 g4dn instance. Fallback: Google OR-Tools (CPU-only, slower).
4. **OSRM data freshness** — OSM data for Uruguay is updated monthly. Street changes won't reflect until the next OSRM rebuild.
5. **No real-time traffic** — OSRM uses static speed profiles. Could integrate traffic data later.

## Useful commands

```bash
# Convert raw container data
python data/scripts/convert_coordinates.py --containers data/raw/Contenedores_domiciliarios.csv --output data/processed/

# Run OSRM locally with Uruguay data
cd osrm && docker compose up

# Deploy infrastructure
cd terraform && terraform plan && terraform apply

# Run sensor simulator locally
cd simulator && python simulator.py --circuit 101 --interval 60
```

## Environment variables

```bash
AWS_REGION=us-east-1
DYNAMODB_CONTAINERS_TABLE=smartwaste-containers
DYNAMODB_TRUCKS_TABLE=smartwaste-trucks
DYNAMODB_ROUTES_TABLE=smartwaste-routes
IOT_ENDPOINT=<your-iot-endpoint>.iot.us-east-1.amazonaws.com
OSRM_URL=http://localhost:5000
CUOPT_API_KEY=<nvidia-api-catalog-key>  # only for cloud API mode
```