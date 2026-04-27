FROM osrm/osrm-backend:latest

# Datos pre-procesados por "docker compose run osrm-prepare" y
# persistidos en el volumen ./data/.  El CMD espera encontrar
# /data/uruguay-latest.osrm al arrancar.
WORKDIR /data

EXPOSE 5000

# MLD (Multi-Level Dijkstra):
#   - Mejor que CH para consultas Table API con muchos orígenes/destinos
#   - Soporta actualización incremental del grafo (útil si se añade tráfico)
#
# --max-table-size 10000:
#   Uruguay tiene ~13 000 contenedores; 10 000 cubre la mayoría de las
#   matrices de circuito que generará el route-optimizer.
CMD ["osrm-routed", \
     "--algorithm", "mld", \
     "--port", "5000", \
     "--max-table-size", "10000", \
     "/data/uruguay-latest.osrm"]
