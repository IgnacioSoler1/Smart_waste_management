# SmartWaste MVD

**Optimizacion dinamica de rutas de recoleccion de residuos en Montevideo, Uruguay.**

Un sistema end-to-end que simula sensores IoT en ~13.000 contenedores de basura, determina en tiempo real cuales necesitan ser vaciados, y calcula la ruta optima para cada camion recolector — reduciendo kilometros recorridos, combustible consumido y contenedores desbordados.

---

## El problema

Montevideo genera aproximadamente **1.200 toneladas diarias** de residuos domiciliarios, distribuidos en ~13.000 contenedores organizados en **117 circuitos de recoleccion**. Hoy, los camiones siguen **rutas fijas**: recorren todos los contenedores de su circuito sin importar si estan llenos, a medio llenar, o practicamente vacios.

Esto genera:
- **Kilometros innecesarios** — camiones que pasan por contenedores que no necesitan ser vaciados
- **Contenedores desbordados** — los que se llenan mas rapido no reciben atencion prioritaria
- **Consumo excesivo de combustible** y desgaste de flota
- **Imposibilidad de planificar** en base a datos reales de llenado

## La solucion

SmartWaste MVD convierte las rutas estaticas en **rutas dinamicas basadas en datos**. El sistema:

1. **Simula sensores IoT** en cada contenedor, publicando niveles de llenado via MQTT cada 60 segundos
2. **Prioriza contenedores** — identifica cuales necesitan recoleccion urgente (>80%), cuales pueden esperar, y cuales se pueden omitir
3. **Calcula la ruta optima** para cada camion cada 15 minutos, considerando distancias reales por calles, capacidad del camion, y ubicacion de los sitios de disposicion
4. **Notifica al conductor en tiempo real** via WebSocket con la ruta actualizada directamente en su app

El resultado: los camiones solo visitan los contenedores que realmente necesitan ser vaciados, en el orden mas eficiente posible.

---

## Resultados

### Como medimos

No tenemos acceso a datos reales de cuanto tiempo o distancia recorre hoy cada camion. Lo que si podemos hacer es **simular la ruta actual y compararla con la ruta optimizada**, usando las mismas herramientas de ruteo (OSRM) y los mismos datos de calles.

Para cada circuito, generamos dos rutas:

- **Ruta baseline (actual)**: visita **todos** los contenedores del circuito en orden secuencial — es decir, uno tras otro segun estan numerados en el recorrido original. Esta es una aproximacion razonable de como operan hoy los camiones: siguen un recorrido fijo sin importar el nivel de llenado.
- **Ruta optimizada**: visita **solo los contenedores que necesitan recoleccion**, en el orden que minimiza la distancia total recorrida. Si un contenedor esta por debajo del 30% de llenado, se omite. El solver (NVIDIA cuOpt) decide el orden optimo y, si es necesario, divide el trabajo entre multiples camiones respetando la capacidad de cada uno.

Ambas rutas se calculan sobre el **mismo grafo vial** (OpenStreetMap Uruguay via OSRM), con distancias y tiempos reales de calle a calle. La diferencia entre ambas es el ahorro que genera la optimizacion.

### Numeros reales del sistema

Sobre **66 circuitos** optimizados en una ejecucion tipica:

| Metrica | Ruta baseline | Ruta optimizada | Mejora |
|---------|:------------:|:---------------:|:------:|
| **Distancia total** | 6.055 km | 3.424 km | **-43%** |
| **Tiempo total** | 176 horas | 88 horas | **-50%** |
| **Paradas** | 4.190 | 3.828 | 362 contenedores omitidos |

Desglose por turno:

| Turno | Circuitos | Mejora promedio en distancia | Mejora promedio en tiempo |
|-------|:---------:|:----------------------------:|:------------------------:|
| Manana | 29 | **45%** | 49% |
| Tarde | 11 | **49%** | 53% |
| Noche | 26 | **37%** | 40% |

Los mejores circuitos alcanzan reducciones de hasta **74% en distancia** y **71% en tiempo**. La mediana de mejora se ubica en **~46% en distancia** y **~50% en tiempo**.

### Que significa esto en la practica

Si estos numeros se trasladan a operacion real, significaria:
- **~2.600 km menos recorridos por dia** solo en los 66 circuitos medidos
- **~88 horas menos de operacion** de camiones por dia
- Contenedores casi vacios que se dejan de visitar, liberando tiempo para los que realmente necesitan recoleccion
- Menor consumo de combustible y desgaste de flota

> **Nota importante**: estos resultados comparan contra una baseline simulada (ruta secuencial fija), no contra datos reales de GPS de los camiones. Los numeros reales pueden variar — pero la magnitud del ahorro potencial es clara.

---

## Como funciona

### Flujo general

```
Sensores IoT (simulados)
    | MQTT cada 60s
    v
AWS IoT Core ---- fanout ---+-- SQS -> Lambda -> DynamoDB (estado operativo)
                             +-- Kinesis -> S3 (data lake historico)

EventBridge (cada 15 min) -> Lambda route-optimizer
    |-- Lee niveles de llenado desde DynamoDB
    |-- Calcula matriz de distancias reales con OSRM
    |-- Resuelve el problema de ruteo vehicular con NVIDIA cuOpt
    +-- Envia la ruta optimizada al conductor via WebSocket
```

> Para el detalle completo de la arquitectura, ver [`docs/architecture.md`](docs/architecture.md).

### Optimizacion de rutas: el nucleo del sistema

El problema de asignar contenedores a camiones y definir el orden optimo de visita es un **Vehicle Routing Problem con capacidad (CVRP)** — un problema de optimizacion combinatoria NP-hard. SmartWaste lo resuelve en dos pasos:

**1. Matriz de distancias reales — OSRM**

[OSRM](http://project-osrm.org/) (Open Source Routing Machine) es un motor de ruteo vehicular de alta performance que usa datos de OpenStreetMap. Lo desplegamos self-hosted en AWS (ECS Fargate) con datos del mapa vial de Uruguay y un perfil configurado para camiones recolectores. Esto nos permite calcular la distancia y tiempo real de viaje entre cualquier par de contenedores en **menos de 50ms**, sin depender de APIs externas, sin limites de requests, y sin costo por consulta.

**2. Solver de ruteo vehicular — NVIDIA cuOpt**

[NVIDIA cuOpt](https://developer.nvidia.com/cuopt-logistics-optimization) es un solver de optimizacion de rutas acelerado por GPU que resuelve problemas VRP complejos en segundos. Recibe la matriz de distancias, la ubicacion de cada contenedor que necesita recoleccion, la capacidad de cada camion, y la ubicacion de los sitios de disposicion final — y devuelve la asignacion optima de contenedores a camiones y el orden de visita que minimiza la distancia total.

Como fallback para desarrollo y testing, el sistema tambien puede usar [Google OR-Tools](https://developers.google.com/optimization), un solver CPU que resuelve el mismo problema con mayor tiempo de ejecucion.

### Pipeline de datos y analytics

Cada lectura de sensor se almacena en un **data lake en S3** siguiendo una arquitectura **Bronze -> Silver -> Gold**:

- **Bronze**: datos crudos tal como llegan de los sensores (JSON comprimido)
- **Silver**: datos limpios y estructurados (Parquet, particionados por fecha)
- **Gold**: metricas pre-calculadas listas para consumo (JSON)

Un job de AWS Glue procesa diariamente los datos para generar analytics: patrones de llenado por hora, heatmaps de la ciudad, tendencias por circuito, y metricas de eficiencia de rutas. Estas metricas son las que alimentan los dashboards y permiten medir los resultados de la optimizacion.

---

## El producto final

### Driver App — App para conductores

Una PWA (Progressive Web App) que el conductor instala en su celular. Muestra el mapa con la ruta optimizada en tiempo real, la lista de paradas ordenadas con nivel de llenado de cada contenedor, y se actualiza automaticamente cuando el sistema recalcula la ruta.

- Mapa interactivo con la geometria real de la ruta sobre las calles
- Contenedores coloreados por urgencia (verde < 40%, amarillo 40-70%, rojo > 70%)
- Accion de "Vaciar contenedor" que actualiza el estado en tiempo real
- Funciona offline gracias al service worker

### Dashboard de operaciones

Un panel de control para el equipo de operaciones que permite monitorear todo el sistema:

- **Vista de mapa**: los ~13.000 contenedores geolocalizados, filtrables por circuito y nivel de llenado
- **Vista de circuito**: rutas multi-camion con visualizacion de la ruta sobre el mapa
- **KPIs en tiempo real**: distribucion de llenado, contenedores criticos, estadisticas por turno (manana, tarde, noche)
- **Analytics**: heatmaps de llenado, patrones horarios, tendencias por circuito, y metricas de eficiencia de rutas optimizadas vs. rutas base

---

## Stack tecnologico

| Componente | Tecnologia | Por que |
|-----------|-----------|---------|
| **Infraestructura** | AWS (Terraform IaC) | 40+ recursos gestionados como codigo |
| **Ingesta IoT** | AWS IoT Core + MQTT | Protocolo estandar para IoT, escala a millones de mensajes |
| **Procesamiento** | AWS Lambda + SQS | Serverless, escala automaticamente con la carga |
| **Base operativa** | DynamoDB | Baja latencia, on-demand, ideal para estado de contenedores |
| **Data lake** | S3 + Kinesis Firehose + Glue | Pipeline Bronze->Silver->Gold para analytics historico |
| **Ruteo vehicular** | OSRM (ECS Fargate) | Distancias reales por calles, self-hosted, sin costo por query |
| **Optimizacion VRP** | NVIDIA cuOpt + OR-Tools fallback | Solver GPU para CVRP en segundos |
| **Comunicacion real-time** | API Gateway WebSocket | Push de rutas al conductor sin polling |
| **Frontends** | React 18 + TypeScript + Leaflet | PWA instalable, mapas interactivos |

---

## Datos

Las ubicaciones de los contenedores provienen de los **datos abiertos de la Intendencia de Montevideo** ([catalogodatos.gub.uy](https://catalogodatos.gub.uy/)). Las coordenadas originales en SIRGAS2000 UTM 21S se convierten a WGS84 para uso en mapas y ruteo.

Los circuitos de recoleccion, turnos (manana/tarde/noche), y zonas (este/oeste) tambien provienen de datos oficiales de la Intendencia.

---

## Estructura del proyecto

```
smartwaste-mvd/
├── terraform/                # Infraestructura AWS completa (IaC)
├── data/                     # ETL de datos abiertos, seed de base de datos
├── lambdas/
│   ├── sensor-simulator/     # Simulador IoT (publica MQTT via IoT Core)
│   ├── process-sensor-reading/  # Procesamiento batch de lecturas
│   ├── route-optimizer/      # Nucleo: OSRM + cuOpt + notificacion WebSocket
│   ├── glue-analytics/       # ETL diario Bronze→Silver→Gold
│   ├── api/                  # REST API para dashboards
│   └── websocket-*/          # Handlers WebSocket (connect/disconnect/message)
├── osrm/                     # Dockerfile + perfil de camion + datos Uruguay
├── frontend-driver/          # App del conductor (React PWA)
├── frontend-dashboard/       # Dashboard de operaciones (React + Recharts)
└── docs/                     # Documentacion tecnica detallada
```

---

## Documentacion

| Documento | Que cubre |
|-----------|-----------|
| [`docs/architecture.md`](docs/architecture.md) | Arquitectura completa, flujos end-to-end, decisiones de diseno |
| [`docs/datalake.md`](docs/datalake.md) | Pipeline de datos Bronze->Silver->Gold, Glue ETL |
| [`docs/osrm.md`](docs/osrm.md) | OSRM self-hosted, configuracion del perfil de camion |
| [`docs/cuopt-implementation.md`](docs/cuopt-implementation.md) | NVIDIA cuOpt, modelado VRP, fallback OR-Tools |

---

## Limitaciones y aclaraciones

Este es un sistema de **simulacion y prototipo**. Los sensores IoT son simulados (no hay hardware real conectado), y los resultados comparan contra una ruta baseline simulada, no contra datos GPS reales de los camiones.

- **Sensores simulados**: los niveles de llenado se generan con un modelo que reproduce curvas realistas (hora del dia, dia de la semana, densidad de zona). Cuando se instalen sensores reales, publicarian al mismo topic MQTT y el resto del pipeline no cambia.
- **Sin GPS real de camiones**: las posiciones de los camiones son simuladas. En produccion, dispositivos OBD-II publicarian a IoT Core.
- **Sin trafico en tiempo real**: OSRM usa perfiles de velocidad estaticos basados en OpenStreetMap.

La arquitectura esta disenada para que la transicion de simulacion a produccion sea transparente: los sensores reales publicarian al mismo broker MQTT, y el pipeline downstream no requiere cambios.

---

## Contexto

Montevideo ya tiene la infraestructura de contenedores. Lo que falta es la inteligencia para usarla mejor.

SmartWaste MVD es un sistema completo que demuestra como la optimizacion de rutas basada en datos puede transformar la recoleccion de residuos urbana — desde la ingesta de datos IoT hasta la app en la mano del conductor, pasando por un pipeline de analytics que permite medir y mejorar continuamente los resultados.
