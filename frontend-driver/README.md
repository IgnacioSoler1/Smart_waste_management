# SmartWaste Driver App

PWA mobile-first para conductores de recolección de residuos en Montevideo.

## Stack

- **Vite 5** + **React 18** + **TypeScript 5**
- **react-leaflet 4** + OpenStreetMap (sin costo, sin API key)
- **vite-plugin-pwa** — service worker, web manifest, cacheo de tiles OSM
- Sin CSS framework — estilos inline mobile-first

## Setup rápido

```bash
cd frontend-driver
npm install
cp .env.example .env.local
# editar .env.local con los valores de `terraform output`
npm run dev
```

La app corre en `http://localhost:5173`.

## Variables de entorno

| Variable | Valor | Fuente |
|---|---|---|
| `VITE_API_URL` | URL REST API sin trailing slash | `terraform output api_url` |
| `VITE_WS_URL` | URL WebSocket (`wss://...`) | `terraform output websocket_url` |

Ejemplo con Terraform:

```bash
cd ../terraform
export VITE_API_URL=$(terraform output -raw api_url)
export VITE_WS_URL=$(terraform output -raw websocket_url)
```

## Builds

```bash
npm run build     # genera dist/ (optimizado, con service worker)
npm run preview   # sirve dist/ localmente para probar la PWA
```

## Estructura de la app

```
src/
├── types.ts                # Tipos TypeScript compartidos
├── App.tsx                 # Root: sesión en localStorage, routing Login↔Map
├── main.tsx
├── index.css
│
├── pages/
│   ├── Login.tsx           # Selección de camión + circuito
│   └── Map.tsx             # Mapa + sidebar + lógica de sesión
│
├── components/
│   ├── RouteMap.tsx        # Mapa Leaflet: ruta, markers, polyline
│   └── StopList.tsx        # Panel lateral con lista de paradas y botón Vaciar
│
└── hooks/
    ├── useRoute.ts         # GET /circuits/{id}/route
    └── useWebSocket.ts     # Conexión WS con reconexión automática
```

## Flujo de uso

1. **Login**: el conductor selecciona su camión y escribe el ID de circuito.
2. **Mapa**: se carga la ruta activa del circuito (REST API).
   - Polyline verde con las paradas en orden.
   - Markers coloreados por nivel de llenado: 🟢 <40% · 🟡 40-70% · 🔴 >70%.
3. **Vaciar contenedor**: tap en "Vaciar" en el panel → envía `container_emptied` por WebSocket → el marker se torna gris.
4. **Actualización automática**: cuando el route-optimizer recalcula la ruta, llega un mensaje `route_update` por WebSocket → la app recarga la ruta y muestra un toast.

## PWA

Para instalar como app nativa en Android/iOS:
- Chrome/Safari → "Agregar a pantalla de inicio"
- Service worker cachea los tiles OSM para uso offline.

### Iconos PWA

Los archivos `public/pwa-192.png` y `public/pwa-512.png` son requeridos.
Generarlos con cualquier herramienta (por ej. [favicon.io](https://favicon.io)):

```bash
# Con ImageMagick (si disponible):
convert public/favicon.svg -resize 192x192 public/pwa-192.png
convert public/favicon.svg -resize 512x512 public/pwa-512.png
```

## Desarrollo local con API mock

Para desarrollar sin backend desplegado, se pueden usar los archivos de fixture:

```bash
# Servir un JSON de prueba en puerto 3001
npx serve -p 3001 fixtures/
# Luego: VITE_API_URL=http://localhost:3001 npm run dev
```
