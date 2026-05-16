"""
JITR (Just-in-Time Registration) Lambda for SmartWaste MVD.

Triggered by IoT Rule on $aws/events/certificates/registered/<caCertId>.
When a device connects for the first time with a CA-signed cert, this Lambda:
1. Extracts the CN from the certificate (used as Thing name)
2. Creates the Thing in IoT Core
3. Activates the certificate
4. Attaches the IoT policy to the certificate
5. Attaches the certificate to the Thing
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

iot = boto3.client("iot")

POLICY_NAME = os.environ.get("IOT_POLICY_NAME", "smartwaste-dev-sensor-policy")
THING_TYPE = os.environ.get("IOT_THING_TYPE", "")


def handler(event, context):
    """Handle certificate registered event from IoT Core."""
    logger.info("JITR event: %s", json.dumps(event))

    certificate_id = event["certificateId"]
    ca_certificate_id = event.get("caCertificateId", "unknown")

    # Get certificate details to extract CN
    cert_response = iot.describe_certificate(certificateId=certificate_id)
    cert_pem = cert_response["certificateDescription"]["certificatePem"]
    cert_arn = cert_response["certificateDescription"]["certificateArn"]

    # Extract CN from certificate PEM
    thing_name = _extract_cn(cert_pem)
    if not thing_name:
        logger.error("Could not extract CN from certificate %s", certificate_id)
        return {"statusCode": 400, "body": "Could not extract CN"}

    logger.info(
        "Provisioning: cert=%s, thing=%s, ca=%s",
        certificate_id[:12],
        thing_name,
        ca_certificate_id[:12],
    )

    # 1. Create Thing
    thing_params = {"thingName": thing_name}
    if THING_TYPE:
        thing_params["thingTypeName"] = THING_TYPE
    try:
        iot.create_thing(**thing_params)
        logger.info("Created thing: %s", thing_name)
    except iot.exceptions.ResourceAlreadyExistsException:
        logger.info("Thing already exists: %s", thing_name)

    # 2. Activate certificate
    iot.update_certificate(certificateId=certificate_id, newStatus="ACTIVE")
    logger.info("Activated certificate: %s", certificate_id[:12])

    # 3. Attach policy to certificate
    iot.attach_policy(policyName=POLICY_NAME, target=cert_arn)
    logger.info("Attached policy %s to cert", POLICY_NAME)

    # 4. Attach certificate to thing
    iot.attach_thing_principal(thingName=thing_name, principal=cert_arn)
    logger.info("Attached cert to thing %s", thing_name)

    logger.info("JITR provisioning complete for %s", thing_name)
    return {"statusCode": 200, "body": f"Provisioned {thing_name}"}


def _extract_cn(pem: str) -> str:
    """Extract Common Name from a PEM certificate using basic parsing."""
    # Use OpenSSL-free approach: decode the cert and find CN in subject
    import base64
    import re

    # For simplicity, use the cryptography library if available,
    # otherwise fall back to regex on the DER-decoded ASN.1
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(pem.encode())
        cn_attrs = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        if cn_attrs:
            return cn_attrs[0].value
    except ImportError:
        pass

    # Fallback: call IoT describe and parse subject string
    # The subject field from describe-certificate looks like:
    # "CN=smartwaste-dev-101941, O=SmartWaste MVD, C=UY"
    # But we already have the PEM, so let's try ASN.1 basic decode
    # Actually, let's just use boto3 to get the subject from describe
    try:
        # Re-parse subject from AWS API (not ideal but works without extra deps)
        import subprocess

        # Lambda has openssl available
        result = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-nameopt", "RFC2253"],
            input=pem.encode(),
            capture_output=True,
        )
        # Output like: subject=CN=smartwaste-dev-101941,O=SmartWaste MVD,C=UY
        output = result.stdout.decode()
        match = re.search(r"CN=([^,/\n]+)", output)
        if match:
            return match.group(1).strip()
    except Exception:
        pass

    return ""
