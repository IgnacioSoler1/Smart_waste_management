"""
conftest.py — pytest fixtures para tests del route-optimizer.

handler.py inicializa clientes boto3 y OSRM a nivel módulo, por lo que
necesitamos preparar el entorno (env vars + mocks) ANTES de importar el handler.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Variables de entorno requeridas por handler.py al importarse
os.environ.setdefault("CONTAINERS_TABLE", "smartwaste-test-containers")
os.environ.setdefault("TRUCKS_TABLE", "smartwaste-test-trucks")
os.environ.setdefault("ROUTES_TABLE", "smartwaste-test-routes")
os.environ.setdefault("OSRM_URL", "http://localhost:5000")
os.environ.setdefault("CUOPT_MODE", "ortools")
os.environ.setdefault("MAX_LOCATIONS_PER_GROUP", "500")
os.environ.setdefault("MIN_CIRCUITS_FOR_CONSOLIDATION", "2")

# Para que el handler pueda importar osrm_client y constraints como en el build:
# el build.sh copia esos módulos al lambda dir. En tests, los referenciamos
# desde cuopt-client/ via sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "cuopt-client"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# Mock boto3 para evitar conexiones reales en el import del handler
def _install_boto3_mock() -> None:
    import boto3

    fake_table = MagicMock()
    fake_table.query.return_value = {"Items": []}
    fake_table.scan.return_value = {"Items": []}
    fake_table.put_item.return_value = {}
    fake_table.update_item.return_value = {}

    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    fake_client = MagicMock()
    fake_client.put_record.return_value = {}

    boto3.resource = MagicMock(return_value=fake_resource)
    boto3.client = MagicMock(return_value=fake_client)


_install_boto3_mock()
