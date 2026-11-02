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
FROM visualization AS moveit-upstream
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ros-humble-moveit-ros-move-group ros-humble-moveit-kinematics \
    ros-humble-moveit-planners-ompl \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ros-humble-moveit-simple-controller-manager \
    && rm -rf /var/lib/apt/lists/*

FROM moveit-upstream AS moveit
# Keep the SDK libraries intact; rebuild only the pinned upstream entry point.
COPY move_group_lifecycle /opt/relay-moveit-src
ADD --checksum=sha256:e5149cf2f3fd8d8006e33373b70f435bf3edec69b2744ea492bbcfba34f9a5e6 https://raw.githubusercontent.com/moveit/moveit2/2.5.10/moveit_ros/move_group/src/move_group.cpp /opt/relay-moveit-src/upstream_move_group.cpp
RUN python3 /opt/relay-moveit-src/prepare_upstream.py /opt/relay-moveit-src \
    && bash -c 'source /opt/ros/humble/setup.bash && cmake -S /opt/relay-moveit-src -B /opt/relay-moveit-build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/relay-moveit && cmake --build /opt/relay-moveit-build -j2 && cmake --install /opt/relay-moveit-build'
ENV AMENT_PREFIX_PATH=/opt/relay-moveit

FROM runtime AS headless
