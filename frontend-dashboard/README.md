# SmartWaste Dashboard — Operaciones

Dashboard de operaciones para la plataforma SmartWaste MVD. Visualiza el estado de contenedores, camiones y rutas de recoleccion en Montevideo en tiempo real.

## Vistas

- **Mapa**: Todos los contenedores de Montevideo como circle markers (color por nivel de llenado), camiones como iconos, filtro por circuito.
- **Circuitos**: Seleccionar un circuito del dropdown, ver sus contenedores, ruta activa, stats y trigger de optimizacion manual.
- **KPIs**: Cards con metricas (total contenedores, % por nivel, camiones activos). Graficos de barras por turno y circuitos prioritarios.

## Setup

```bash
cd frontend-dashboard
npm install
```

Crear `.env.local` con la URL del API:

```
VITE_API_URL=https://<API_ID>.execute-api.us-east-1.amazonaws.com/dev
```

## Desarrollo

```bash
npm run dev        # http://localhost:5174
```

## Build

```bash
npm run build
npm run preview    # preview del build de produccion
```

## Stack

- Vite + React 18 + TypeScript
- Leaflet / react-leaflet — mapas
- Recharts — graficos
- Polling cada 30 segundos al REST API (`GET /circuits`, `GET /trucks`)

## Estructura

```
src/
  main.tsx          # Entry point
  App.tsx           # Layout con sidebar + router de vistas
  types.ts          # Tipos TypeScript
  api.ts            # Cliente REST API
  helpers.ts        # Utilidades (colores fill level, formateo)
  index.css         # Estilos globales + dark theme
  hooks/
    usePolling.ts   # Hook de polling cada 30s
  pages/
    MapView.tsx     # Vista mapa completa
    CircuitView.tsx # Vista detalle de circuito
    KPIsView.tsx    # Vista KPIs con graficos
```
