// Command code-interpreter-api runs the code-interpreter HTTP service. It is
// the Go port of app/main.py.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/api"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/bootstrap"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/config"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/executor"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/logging"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/storage"
)

const sessionReaperInterval = 30 * time.Second

// reapExpiredSessionsOnce runs a single reap pass via the configured
// executor. Failures are logged, never fatal.
func reapExpiredSessionsOnce(exec executor.Executor, log *slog.Logger) {
	count, err := exec.ReapExpiredSessions()
	if err != nil {
		log.Warn("Session reaper pass failed", "error", err.Error())
		return
	}
	if count > 0 {
		log.Info(fmt.Sprintf("Reaped %d expired session(s)", count))
	}
}

// sessionReaperLoop periodically deletes sessions whose TTL has elapsed,
// until ctx is cancelled.
func sessionReaperLoop(ctx context.Context, exec executor.Executor, log *slog.Logger) {
	ticker := time.NewTicker(sessionReaperInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			reapExpiredSessionsOnce(exec, log)
		}
	}
}

func run() error {
	cfg := config.Load()
	logging.Setup(cfg)
	log := logging.Named("main")

	// Prepare the Docker environment (Docker-in-Docker daemon if required)
	// before anything talks to the socket. This replaces entrypoint.sh.
	stopDockerd, err := bootstrap.BootstrapDockerEnvironment(cfg)
	if err != nil {
		return err
	}
	defer stopDockerd()

	// Startup: ensure the Docker executor image is available before accepting
	// requests.
	if strings.ToLower(cfg.ExecutorBackend) == "docker" {
		log.Info("Ensuring Docker executor image is available...")
		if err := executor.EnsureDockerImageAvailable(cfg); err != nil {
			return err
		}
		log.Info("Docker executor image is ready")
	}

	exec, err := executor.New(cfg)
	if err != nil {
		return err
	}

	store, err := storage.NewFileStorageService(cfg.FileStorageDir)
	if err != nil {
		return err
	}

	// Reap any sessions whose TTL elapsed while the service was down, then
	// keep reaping in the background.
	reapExpiredSessionsOnce(exec, log)
	reaperCtx, stopReaper := context.WithCancel(context.Background())
	defer stopReaper()
	go sessionReaperLoop(reaperCtx, exec, log)

	server := &http.Server{
		Addr:    fmt.Sprintf("%s:%d", cfg.Host, cfg.Port),
		Handler: api.NewServer(cfg, exec, store),
	}

	shutdownDone := make(chan struct{})
	go func() {
		defer close(shutdownDone)
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
		<-sigCh
		log.Info("Shutting down...")
		stopReaper()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()

	log.Info(fmt.Sprintf("Serving on http://%s:%d", cfg.Host, cfg.Port))
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	<-shutdownDone
	return nil
}

func main() {
	if err := run(); err != nil {
		slog.Error(err.Error())
		os.Exit(1)
	}
}
