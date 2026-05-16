# cuOpt self-hosted — requires GPU (RAPIDS/RMM)
#
# NVIDIA cuOpt image requires NGC authentication.
# Before building, log in to NGC:
#   docker login nvcr.io -u '$oauthtoken' -p <NGC_API_KEY>
#
# The cuOpt server exposes a REST API on port 5000.
# Health check: GET /cuopt/health
# Solver:       POST /cuopt/
#
# NOTE: cuOpt requires GPU. Fargate (CPU-only) will fail with
# MemoryError from RMM. Use EC2 launch type with g4dn instances.

FROM nvcr.io/nvidia/cuopt/cuopt:25.02

EXPOSE 5000
