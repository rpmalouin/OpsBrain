# Ops Brain - optional containerized deployment.
#
# NOTE: the containerized build is for METRICS + LOGS pollution; the collector's
# GPU (nvidia-smi), VM (journalctl/top/df) and ACTION remediation (docker/systemctl)
# need host access. For FULL fidelity run the scheduler directly on the host
# (./opsbrain install) or add the extra mounts below.
FROM python:3.10-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /etc/opsbrain
COPY . .

RUN pip install --no-cache-dir pyyaml

ENV OPSBRAIN_REPO=/etc/ops \
    PYTHONUNBUFFERED=1

# Executor entry: scheduler daemon.
# --no-dry-run is NOT passed here; actions honour config.actions.dry_run (default safe).
ENTRYPOINT ["python3", "scheduler/scheduler.py", "--daemon"]