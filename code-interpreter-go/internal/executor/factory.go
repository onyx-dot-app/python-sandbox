package executor

import (
	"fmt"
	"strings"

	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/config"
)

// New returns the executor selected by cfg.ExecutorBackend ("docker" or
// "kubernetes"). It is the Go port of app/services/executor_factory.py.
func New(cfg config.Config) (Executor, error) {
	switch strings.ToLower(cfg.ExecutorBackend) {
	case "docker":
		return NewDockerExecutor(cfg)
	case "kubernetes":
		return NewKubernetesExecutor(cfg)
	default:
		return nil, fmt.Errorf("Unknown executor backend: %s", strings.ToLower(cfg.ExecutorBackend))
	}
}
