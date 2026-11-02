FROM ros:humble-ros-base-jammy@sha256:d9bcf2e6f72df127fdee07048c2e87b1b4b9a59a74af0592f4489fc2844aa855 AS runtime
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3-pip python3-venv ros-humble-control-msgs ros-humble-sensor-msgs \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/relay-requirements.txt
RUN python3 -m venv --system-site-packages /opt/relay-venv \
    && /opt/relay-venv/bin/pip install --no-cache-dir -r /tmp/relay-requirements.txt
ENV ROS_DOMAIN_ID=74 ROS_LOCALHOST_ONLY=1 PYTHONUNBUFFERED=1 PYTHONPATH=/workspace
WORKDIR /workspace
CMD ["sleep", "infinity"]

# Optional viewing environment. The default target below stays headless.
FROM runtime AS visualization
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ros-humble-rviz2 ros-humble-robot-state-publisher ros-humble-tf2-ros \
    liburdfdom-tools xvfb x11vnc novnc websockify openbox x11-utils \
    libgl1-mesa-dri fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY start-desktop.sh /usr/local/bin/relay-ros2-desktop
ENV DISPLAY=:1 QT_X11_NO_MITSHM=1 LIBGL_ALWAYS_SOFTWARE=1
CMD ["bash", "/usr/local/bin/relay-ros2-desktop"]

# Optional MoveIt IK service; existing headless/view images stay unchanged.
FROM visualization AS moveit
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ros-humble-moveit-ros-move-group ros-humble-moveit-kinematics \
    ros-humble-moveit-planners-ompl \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ros-humble-moveit-simple-controller-manager \
    && rm -rf /var/lib/apt/lists/*

FROM runtime AS headless
