// Package config holds environment-based settings for the code-interpreter
// service. It is the Go port of app/app_configs.py.
package config

import (
	"os"
	"strconv"
	"strings"
)

// ServiceVersion is the semver of the running service. Clients can compare
// against a required minimum to detect whether new functionality is available.
const ServiceVersion = "0.4.4"

// Config carries every runtime setting the service reads from the environment.
type Config struct {
	// Executor backend selection ("docker" or "kubernetes").
	ExecutorBackend string

	// Docker executor configuration. The daemon connection comes from the
	// standard DOCKER_HOST / DOCKER_API_VERSION / DOCKER_CERT_PATH environment
	// variables, defaulting to the local socket.
	DockerImage   string
	DockerRunArgs string
	// Docker network for spawned executor containers. Defaults to "none" (no
	// network access) for maximum isolation. Set to a Docker network name
	// (e.g. "onyx_default", "traefik") to allow executor containers to reach
	// services on that network.
	DockerNetwork string

	// Kubernetes executor configuration.
	KubernetesNamespace      string
	KubernetesImage          string
	KubernetesServiceAccount string
	// When true, executor pods run a privileged (NET_ADMIN) init container
	// that uses iptables to drop all outbound traffic before the executor
	// container starts. This avoids the race where a pod can reach the
	// network before the CNI enforces a NetworkPolicy. Environments whose CNI
	// applies NetworkPolicies without that race (or that disallow NET_ADMIN)
	// can set this to false and rely on a NetworkPolicy.
	KubernetesNetAdminLockdown bool

	// Execution limits.
	MaxExecTimeoutMs int
	MaxOutputBytes   int
	CPUTimeLimitSec  int
	MemoryLimitMB    int

	// API server configuration.
	Host string
	Port int

	// Logging configuration. LogLevel controls verbosity (e.g. DEBUG, INFO,
	// WARNING). LogFormat selects the output style: "plain" (default
	// human-readable text) or "json" (structured single-line JSON suitable
	// for container log aggregators).
	LogLevel  string
	LogFormat string

	// File storage configuration.
	FileStorageDir string
	MaxFileSizeMB  int
	FileTTLSec     int
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envIntOr(key string, fallback int) int {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func envBoolDefaultTrue(key string) bool {
	v := strings.ToLower(envOr(key, "true"))
	switch v {
	case "false", "0", "no":
		return false
	}
	return true
}

// Load reads every setting from the environment, applying the same defaults
// as the Python service.
func Load() Config {
	return Config{
		ExecutorBackend: envOr("EXECUTOR_BACKEND", "docker"),

		DockerImage:   envOr("PYTHON_EXECUTOR_DOCKER_IMAGE", "onyxdotapp/python-executor-sci"),
		DockerRunArgs: envOr("PYTHON_EXECUTOR_DOCKER_RUN_ARGS", ""),
		DockerNetwork: envOr("PYTHON_EXECUTOR_DOCKER_NETWORK", "none"),

		KubernetesNamespace:        envOr("KUBERNETES_EXECUTOR_NAMESPACE", "default"),
		KubernetesImage:            envOr("KUBERNETES_EXECUTOR_IMAGE", "onyxdotapp/python-executor-sci"),
		KubernetesServiceAccount:   envOr("KUBERNETES_EXECUTOR_SERVICE_ACCOUNT", ""),
		KubernetesNetAdminLockdown: envBoolDefaultTrue("KUBERNETES_EXECUTOR_NET_ADMIN_LOCKDOWN"),

		MaxExecTimeoutMs: envIntOr("MAX_EXEC_TIMEOUT_MS", 60_000),
		MaxOutputBytes:   envIntOr("MAX_OUTPUT_BYTES", 1_000_000),
		CPUTimeLimitSec:  envIntOr("CPU_TIME_LIMIT_SEC", 5),
		MemoryLimitMB:    envIntOr("MEMORY_LIMIT_MB", 256),

		Host: envOr("HOST", "0.0.0.0"),
		Port: envIntOr("PORT", 8000),

		LogLevel:  strings.ToUpper(envOr("LOG_LEVEL", "INFO")),
		LogFormat: strings.ToLower(envOr("LOG_FORMAT", "plain")),

		FileStorageDir: envOr("FILE_STORAGE_DIR", "/tmp/code-interpreter-files"),
		MaxFileSizeMB:  envIntOr("MAX_FILE_SIZE_MB", 100),
		FileTTLSec:     envIntOr("FILE_TTL_SEC", 3600),
	}
}

// JSONLogging reports whether structured JSON logging is enabled.
func (c Config) JSONLogging() bool {
	return c.LogFormat == "json"
}
