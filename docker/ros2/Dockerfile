FROM ros:humble-ros-base-jammy@sha256:d9bcf2e6f72df127fdee07048c2e87b1b4b9a59a74af0592f4489fc2844aa855
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3-pip python3-venv ros-humble-control-msgs ros-humble-sensor-msgs \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/relay-requirements.txt
RUN python3 -m venv --system-site-packages /opt/relay-venv \
    && /opt/relay-venv/bin/pip install --no-cache-dir -r /tmp/relay-requirements.txt
ENV ROS_DOMAIN_ID=74 ROS_LOCALHOST_ONLY=1 PYTHONUNBUFFERED=1 PYTHONPATH=/workspace
WORKDIR /workspace
CMD ["sleep", "infinity"]
