// Package bootstrap prepares the Docker environment before the service
// starts. It is the Go port of entrypoint.sh: it decides between
// Docker-out-of-Docker (a mounted socket), Docker-in-Docker (starting a
// nested dockerd, which requires --privileged), and no Docker at all, so the
// image needs no shell entrypoint.
package bootstrap

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/docker/docker/client"
	"golang.org/x/sys/unix"

	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/config"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/logging"
)

const (
	dockerSocketPath   = "/var/run/docker.sock"
	dockerdLogPath     = "/var/log/dockerd.log"
	cgroupRoot         = "/sys/fs/cgroup"
	daemonReadyRetries = 30
	daemonReadyDelay   = 1 * time.Second
)

// Mode is the Docker setup decision for this process.
type Mode int

const (
	// ModeSkipKubernetes: the Kubernetes backend is configured; Docker is
	// not used at all.
	ModeSkipKubernetes Mode = iota
	// ModeExternalDaemon: DOCKER_HOST points at a daemon elsewhere; nothing
	// to set up locally.
	ModeExternalDaemon
	// ModeSocketFound: the host's socket is mounted (Docker-out-of-Docker).
	ModeSocketFound
	// ModeStartDaemon: no socket but we have privileges — start a nested
	// dockerd (Docker-in-Docker).
	ModeStartDaemon
	// ModeNoDocker: no socket and no privileges; warn and continue so
	// health checks surface the misconfiguration.
	ModeNoDocker
)

// DecideMode picks the Docker setup mode from the environment, mirroring the
// branch structure of entrypoint.sh (plus an explicit DOCKER_HOST short
// circuit the script lacked).
func DecideMode(executorBackend string, dockerHostSet, socketExists, varRunWritable bool) Mode {
	if strings.ToLower(executorBackend) == "kubernetes" {
		return ModeSkipKubernetes
	}
	if dockerHostSet {
		return ModeExternalDaemon
	}
	if socketExists {
		return ModeSocketFound
	}
	if varRunWritable {
		return ModeStartDaemon
	}
	return ModeNoDocker
}

// socketExists reports whether path exists and is a unix socket (the -S test
// in entrypoint.sh).
func socketExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode()&os.ModeSocket != 0
}

// BootstrapDockerEnvironment applies the decided mode. When it starts a
// nested dockerd it returns a stop function that signals the daemon;
// otherwise the returned function is a no-op. The only error case is a
// Docker-in-Docker daemon that fails to start or become ready.
func BootstrapDockerEnvironment(cfg config.Config) (func(), error) {
	log := logging.Named("bootstrap")
	noop := func() {}

	mode := DecideMode(
		cfg.ExecutorBackend,
		os.Getenv("DOCKER_HOST") != "",
		socketExists(dockerSocketPath),
		unix.Access("/var/run", unix.W_OK) == nil,
	)

	switch mode {
	case ModeSkipKubernetes:
		log.Info("Using Kubernetes executor backend - skipping Docker setup")
		return noop, nil
	case ModeExternalDaemon:
		log.Info("DOCKER_HOST is set - using the configured external Docker daemon")
		return noop, nil
	case ModeSocketFound:
		log.Info("Docker socket found at " + dockerSocketPath +
			" - using Docker-out-of-Docker mode")
		return noop, nil
	case ModeNoDocker:
		log.Warn("No Docker socket found and insufficient privileges for Docker-in-Docker")
		log.Warn("This container needs either:")
		log.Warn("  1. Docker-out-of-Docker: -v /var/run/docker.sock:/var/run/docker.sock --user root")
		log.Warn("  2. Docker-in-Docker: --privileged")
		return noop, nil
	}

	log.Info("No Docker socket found but running with privileges - enabling Docker-in-Docker mode")
	return startDockerd()
}

// startDockerd launches a nested Docker daemon and waits for it to become
// ready, mirroring entrypoint.sh's start_dockerd.
func startDockerd() (func(), error) {
	log := logging.Named("bootstrap")
	log.Info("Starting Docker daemon for Docker-in-Docker mode...")

	// Enable cgroup v2 nesting before dockerd starts.
	if err := enableCgroupNesting(); err != nil {
		log.Warn("Failed to enable cgroup v2 nesting", "error", err.Error())
	}

	logFile, err := os.OpenFile(dockerdLogPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("failed to open dockerd log file: %w", err)
	}

	cmd := exec.Command(
		"dockerd",
		"--host=unix://"+dockerSocketPath,
		"--storage-driver=vfs",
	)
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		_ = logFile.Close()
		return nil, fmt.Errorf("failed to start dockerd: %w", err)
	}
	_ = logFile.Close()

	// Reap dockerd if it exits so it cannot linger as a zombie under PID 1.
	daemonExited := make(chan error, 1)
	go func() { daemonExited <- cmd.Wait() }()

	stop := func() { _ = cmd.Process.Signal(syscall.SIGTERM) }

	log.Info("Waiting for Docker daemon to be ready...")
	if err := waitForDaemonReady(daemonExited); err != nil {
		stop()
		return nil, err
	}

	log.Info("Docker daemon is ready")
	return stop, nil
}

// waitForDaemonReady pings the daemon socket until it responds, the retry
// budget is exhausted, or the daemon process exits.
func waitForDaemonReady(daemonExited <-chan error) error {
	cli, err := client.NewClientWithOpts(
		client.WithHost("unix://"+dockerSocketPath),
		client.WithAPIVersionNegotiation(),
	)
	if err != nil {
		return fmt.Errorf("failed to create Docker client: %w", err)
	}
	defer func() { _ = cli.Close() }()

	for attempt := 0; attempt < daemonReadyRetries; attempt++ {
		select {
		case waitErr := <-daemonExited:
			return fmt.Errorf(
				"dockerd exited before becoming ready: %v\n%s", waitErr, dockerdLogTail(),
			)
		case <-time.After(daemonReadyDelay):
		}

		ctx, cancel := context.WithTimeout(context.Background(), daemonReadyDelay)
		_, pingErr := cli.Ping(ctx)
		cancel()
		if pingErr == nil {
			return nil
		}
	}

	return errors.New(
		"ERROR: Docker daemon failed to start within timeout\n" + dockerdLogTail(),
	)
}

// dockerdLogTail returns the last part of the dockerd log for error
// messages, standing in for entrypoint.sh's `cat /var/log/dockerd.log`.
func dockerdLogTail() string {
	data, err := os.ReadFile(dockerdLogPath)
	if err != nil {
		return "(dockerd log unavailable: " + err.Error() + ")"
	}
	const tailBytes = 4096
	if len(data) > tailBytes {
		data = data[len(data)-tailBytes:]
	}
	return string(data)
}

// enableCgroupNesting moves all processes out of the root cgroup and turns
// on all controllers for children, the cgroup v2 dance from entrypoint.sh
// required before running a nested dockerd.
func enableCgroupNesting() error {
	controllersPath := filepath.Join(cgroupRoot, "cgroup.controllers")
	controllersRaw, err := os.ReadFile(controllersPath)
	if err != nil {
		// Not cgroup v2; nothing to do (the script's `if [ -f ... ]` guard).
		return nil
	}

	initDir := filepath.Join(cgroupRoot, "init")
	if err := os.MkdirAll(initDir, 0o755); err != nil {
		return err
	}

	// Move all current processes out of the root cgroup. Individual writes
	// may fail for kernel threads or already-exited pids; ignore them like
	// the script's `|| true`.
	procsRaw, err := os.ReadFile(filepath.Join(cgroupRoot, "cgroup.procs"))
	if err != nil {
		return err
	}
	initProcs := filepath.Join(initDir, "cgroup.procs")
	for _, pid := range strings.Fields(string(procsRaw)) {
		_ = os.WriteFile(initProcs, []byte(pid), 0o644)
	}

	// Turn on all available controllers for children.
	controllers := strings.Fields(string(controllersRaw))
	for i, c := range controllers {
		controllers[i] = "+" + c
	}
	return os.WriteFile(
		filepath.Join(cgroupRoot, "cgroup.subtree_control"),
		[]byte(strings.Join(controllers, " ")),
		0o644,
	)
}
