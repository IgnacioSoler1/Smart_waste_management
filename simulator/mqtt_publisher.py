"""
mqtt_publisher.py — SmartWaste MVD

Cliente MQTT sobre TLS mutuo (mTLS) para publicar lecturas de sensores
simulados a AWS IoT Core.

Usa el SDK oficial de AWS IoT para Python v2 (awsiotsdk / awscrt), que
gestiona reconexión automática, keep-alive y cola de mensajes offline.

Uso:
  from simulator.mqtt_publisher import MQTTPublisher

  pub = MQTTPublisher(
      endpoint  = "xxxx-ats.iot.us-east-1.amazonaws.com",
      cert_path = "certs/device.pem.crt",
      key_path  = "certs/device.pem.key",
      ca_path   = "certs/AmazonRootCA1.pem",
  )
  pub.connect()
  pub.publish("smartwaste/sensors/12345", {"fill_level": 78.5})
  pub.disconnect()

Dependencias:
  pip install awsiotsdk
"""

import json
import logging
import socket
import threading
import time
from typing import Any

from awscrt import mqtt
from awsiot import mqtt_connection_builder

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────

MQTT_PORT: int = 8883
KEEP_ALIVE_SECS: int = 30
CONNECT_TIMEOUT_SECS: float = 15.0
PUBLISH_TIMEOUT_SECS: float = 10.0

# Backoff para reconexión manual (segundos): 1, 2, 4, 8, 16, 32
_RECONNECT_DELAYS: list[float] = [1, 2, 4, 8, 16, 32]


# ─────────────────────────────────────────────────────────
# Clase principal
# ─────────────────────────────────────────────────────────

class MQTTPublisher:
    """
    Publica mensajes JSON a AWS IoT Core via MQTT sobre mTLS.

    Maneja:
      - Conexión inicial con timeout configurable
      - Reconexión automática (SDK) + reintentos manuales en publish()
      - Serialización de payload a JSON con soporte para tipos no-estándar
        (Decimal, datetime)

    Args:
        endpoint:   IoT Core ATS endpoint (sin schema ni puerto).
                    Ejemplo: "xxxx-ats.iot.us-east-1.amazonaws.com"
        cert_path:  Path al certificado del dispositivo (.pem.crt)
        key_path:   Path a la clave privada (.pem.key)
        ca_path:    Path al CA raíz de Amazon (AmazonRootCA1.pem)
        client_id:  ID MQTT del cliente. Default: "smartwaste-sim-{hostname}-{pid}"
        qos:        Calidad de servicio MQTT (0 o 1). Default: AT_LEAST_ONCE (1)
    """

    def __init__(
        self,
        endpoint: str,
        cert_path: str,
        key_path: str,
        ca_path: str,
        client_id: str | None = None,
        qos: mqtt.QoS = mqtt.QoS.AT_LEAST_ONCE,
    ) -> None:
        self._endpoint = endpoint
        self._cert_path = cert_path
        self._key_path = key_path
        self._ca_path = ca_path
        self._qos = qos
        self._connection = None
        self._connected = threading.Event()

        if client_id is None:
            import os
            hostname = socket.gethostname().replace(".", "-")
            self._client_id = f"smartwaste-sim-{hostname}-{os.getpid()}"
        else:
            self._client_id = client_id

    # ── Callbacks del SDK ────────────────────────────────

    def _on_connection_interrupted(self, connection, error, **kwargs) -> None:
        """Llamado por el SDK cuando la conexión se interrumpe."""
        self._connected.clear()
        logger.warning("MQTT: conexión interrumpida — %s", error)

    def _on_connection_resumed(
        self, connection, return_code, session_present, **kwargs
    ) -> None:
        """Llamado por el SDK cuando la conexión se restablece automáticamente."""
        self._connected.set()
        logger.info(
            "MQTT: conexión restablecida — return_code=%s session_present=%s",
            return_code,
            session_present,
        )

    # ── Ciclo de vida ────────────────────────────────────

    def connect(self) -> None:
        """
        Establece la conexión MQTT con IoT Core.

        Bloquea hasta que la conexión esté activa o se agote el timeout.

        Raises:
            RuntimeError: si la conexión no se establece en CONNECT_TIMEOUT_SECS.
            Exception:    cualquier error del SDK de transporte/TLS.
        """
        logger.info(
            "MQTT: conectando a %s como client_id=%s", self._endpoint, self._client_id
        )
        self._connection = mqtt_connection_builder.mtls_from_path(
            endpoint=self._endpoint,
            cert_filepath=self._cert_path,
            pri_key_filepath=self._key_path,
            ca_filepath=self._ca_path,
            client_id=self._client_id,
            on_connection_interrupted=self._on_connection_interrupted,
            on_connection_resumed=self._on_connection_resumed,
            clean_session=False,
            keep_alive_secs=KEEP_ALIVE_SECS,
        )

        connect_future = self._connection.connect()
        connect_future.result(timeout=CONNECT_TIMEOUT_SECS)
        self._connected.set()
        logger.info("MQTT: conectado ✓")

    def disconnect(self) -> None:
        """Desconecta limpiamente. Seguro de llamar aunque no se haya conectado."""
        if self._connection is None:
            return
        try:
            disconnect_future = self._connection.disconnect()
            disconnect_future.result(timeout=5.0)
            logger.info("MQTT: desconectado ✓")
        except Exception as exc:
            logger.warning("MQTT: error al desconectar — %s", exc)
        finally:
            self._connected.clear()
            self._connection = None

    # ── Publicación ──────────────────────────────────────

    def publish(self, topic: str, payload_dict: dict[str, Any]) -> None:
        """
        Serializa `payload_dict` a JSON y lo publica en `topic`.

        Reintenta hasta len(_RECONNECT_DELAYS) veces ante errores transitorios,
        con backoff exponencial entre reintentos.

        Args:
            topic:        topic MQTT completo, p.ej. "smartwaste/sensors/12345"
            payload_dict: dict serializable a JSON. Acepta Decimal y datetime
                          (se convierten a float y str ISO respectivamente).

        Raises:
            RuntimeError: si no hay conexión activa y no se puede reconectar.
            Exception:    error del SDK tras agotar los reintentos.
        """
        if self._connection is None:
            raise RuntimeError(
                "MQTTPublisher.publish() llamado sin conexión activa. "
                "Llamar connect() primero."
            )

        payload_str = json.dumps(payload_dict, default=_json_default)

        for attempt, delay in enumerate([0.0] + _RECONNECT_DELAYS):
            if delay > 0:
                logger.warning(
                    "MQTT publish: reintento %d/%d en %.0fs…",
                    attempt,
                    len(_RECONNECT_DELAYS),
                    delay,
                )
                time.sleep(delay)

            try:
                future, _ = self._connection.publish(
                    topic=topic,
                    payload=payload_str,
                    qos=self._qos,
                )
                future.result(timeout=PUBLISH_TIMEOUT_SECS)
                return  # ✓ éxito

            except Exception as exc:
                logger.warning("MQTT publish error (intento %d): %s", attempt + 1, exc)
                if attempt == len(_RECONNECT_DELAYS):
                    raise

        # No debería llegar aquí, pero por si acaso:
        raise RuntimeError("MQTTPublisher: agotados todos los reintentos de publish.")


# ─────────────────────────────────────────────────────────
# Utilidades de serialización
# ─────────────────────────────────────────────────────────

def _json_default(obj: Any) -> Any:
    """
    Serializer de fallback para json.dumps().
    Convierte tipos no estándar que pueden aparecer en los payloads:
      - decimal.Decimal → float
      - datetime        → str ISO 8601
    """
    from decimal import Decimal
    from datetime import datetime

    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
