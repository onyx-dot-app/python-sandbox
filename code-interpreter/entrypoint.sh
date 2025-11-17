#!/bin/bash
set -e

# Function to start Docker daemon in Docker-in-Docker mode
start_dockerd() {
    echo "Starting Docker daemon for Docker-in-Docker mode..."

    # enable cgroup v2 nesting (run as root, before dockerd)
    if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
        mkdir -p /sys/fs/cgroup/init

        # move all current processes out of the root cgroup
        while read -r pid; do
            echo "$pid" > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true
        done < /sys/fs/cgroup/cgroup.procs

        # turn on all available controllers for children
        controllers=$(cat /sys/fs/cgroup/cgroup.controllers)
        controllers="+${controllers// / +}"
        echo "$controllers" > /sys/fs/cgroup/cgroup.subtree_control
    fi

    # Start Docker daemon in the background
    dockerd \
        --host=unix:///var/run/docker.sock \
        --storage-driver=vfs \
        > /var/log/dockerd.log 2>&1 &

    # Wait for Docker daemon to be ready
    echo "Waiting for Docker daemon to be ready..."
    max_attempts=30
    attempt=0
    while ! docker info > /dev/null 2>&1; do
        attempt=$((attempt + 1))
        if [ $attempt -ge $max_attempts ]; then
            echo "ERROR: Docker daemon failed to start within timeout"
            cat /var/log/dockerd.log
            exit 1
        fi
        sleep 1
    done

    echo "Docker daemon is ready"
}

# Check if we're running in privileged mode (for DinD)
# If /var/run/docker.sock doesn't exist and we have privileges, start dockerd
if [ ! -S /var/run/docker.sock ]; then
    if [ -w /var/run ]; then
        echo "No Docker socket found but running with privileges - enabling Docker-in-Docker mode"
        start_dockerd
    else
        echo "WARNING: No Docker socket found and insufficient privileges for Docker-in-Docker"
        echo "This container needs either:"
        echo "  1. Docker-out-of-Docker: -v /var/run/docker.sock:/var/run/docker.sock --user root"
        echo "  2. Docker-in-Docker: --privileged"
    fi
else
    echo "Docker socket found at /var/run/docker.sock - using Docker-out-of-Docker mode"
fi

# Execute the main command
exec "$@"
