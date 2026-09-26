// Package logging configures process-wide structured logging. It is the Go
// port of app/logging_config.py: a single handler on the default logger, with
// either a plain human-readable format or single-line JSON for container log
// aggregators.
package logging

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sync"

	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/config"
)

// LoggerKey is the attribute used to carry the logger name (the analogue of
// Python's logging.getLogger(__name__)).
const LoggerKey = "logger"

// Named returns a logger carrying a component name, mirroring the per-module
// loggers of the Python service.
func Named(name string) *slog.Logger {
	return slog.Default().With(slog.String(LoggerKey, name))
}

// resolveLevel maps a Python-style level name to a slog level. It returns
// (level, wasValid); unknown names fall back to INFO so a typo'd,
// operator-supplied LOG_LEVEL degrades gracefully instead of crashing the
// service at startup.
func resolveLevel(name string) (slog.Level, bool) {
	switch name {
	case "DEBUG":
		return slog.LevelDebug, true
	case "INFO":
		return slog.LevelInfo, true
	case "WARNING", "WARN":
		return slog.LevelWarn, true
	case "ERROR":
		return slog.LevelError, true
	case "CRITICAL", "FATAL":
		return slog.LevelError + 4, true
	}
	return slog.LevelInfo, false
}

// levelName renders slog levels using Python's level names so log output stays
// consistent across the two implementations.
func levelName(l slog.Level) string {
	switch {
	case l >= slog.LevelError+4:
		return "CRITICAL"
	case l >= slog.LevelError:
		return "ERROR"
	case l >= slog.LevelWarn:
		return "WARNING"
	case l >= slog.LevelInfo:
		return "INFO"
	default:
		return "DEBUG"
	}
}

// Setup installs the configured handler on slog's default logger and returns
// the resolved level. Idempotent: the default logger is replaced, not
// stacked, so repeated calls do not duplicate output.
func Setup(cfg config.Config) slog.Level {
	level, levelValid := resolveLevel(cfg.LogLevel)

	var handler slog.Handler
	if cfg.JSONLogging() {
		handler = NewJSONHandler(os.Stderr, level)
	} else {
		handler = NewPlainHandler(os.Stderr, level)
	}
	slog.SetDefault(slog.New(handler))

	if !levelValid {
		slog.Warn(fmt.Sprintf("Unknown LOG_LEVEL %q; falling back to INFO", cfg.LogLevel))
	}
	return level
}

// NewJSONHandler emits structured single-line JSON records with the same
// field names as the Python service (timestamp, level, logger, filename,
// lineno, message).
func NewJSONHandler(w io.Writer, level slog.Level) slog.Handler {
	return slog.NewJSONHandler(w, &slog.HandlerOptions{
		Level:     level,
		AddSource: true,
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if len(groups) > 0 {
				return a
			}
			switch a.Key {
			case slog.TimeKey:
				a.Key = "timestamp"
				a.Value = slog.StringValue(a.Value.Time().Format("2006-01-02T15:04:05-0700"))
			case slog.LevelKey:
				a.Key = "level"
				a.Value = slog.StringValue(levelName(a.Value.Any().(slog.Level)))
			case slog.MessageKey:
				a.Key = "message"
			case slog.SourceKey:
				if src, ok := a.Value.Any().(*slog.Source); ok {
					// An empty-keyed group is inlined by the handler, which
					// yields discrete top-level filename/lineno fields.
					return slog.Attr{Key: "", Value: slog.GroupValue(
						slog.String("filename", filepath.Base(src.File)),
						slog.Int("lineno", src.Line),
					)}
				}
			}
			return a
		},
	})
}

// NewPlainHandler renders records as
// "2006-01-02 15:04:05,000 - logger - LEVEL - message", matching Python's
// "%(asctime)s - %(name)s - %(levelname)s - %(message)s" format.
func NewPlainHandler(w io.Writer, level slog.Level) slog.Handler {
	return &plainHandler{w: w, level: level, mu: &sync.Mutex{}}
}

type plainHandler struct {
	w     io.Writer
	level slog.Level
	attrs []slog.Attr
	mu    *sync.Mutex
}

func (h *plainHandler) Enabled(_ context.Context, level slog.Level) bool {
	return level >= h.level
}

func (h *plainHandler) Handle(_ context.Context, r slog.Record) error {
	name := "app"
	extras := ""
	collect := func(a slog.Attr) bool {
		if a.Key == LoggerKey {
			name = a.Value.String()
		} else {
			extras += fmt.Sprintf(" %s=%v", a.Key, a.Value.Any())
		}
		return true
	}
	for _, a := range h.attrs {
		collect(a)
	}
	r.Attrs(collect)

	ts := r.Time.Format("2006-01-02 15:04:05,000")
	h.mu.Lock()
	defer h.mu.Unlock()
	_, err := fmt.Fprintf(
		h.w, "%s - %s - %s - %s%s\n", ts, name, levelName(r.Level), r.Message, extras,
	)
	return err
}

func (h *plainHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	merged := make([]slog.Attr, 0, len(h.attrs)+len(attrs))
	merged = append(merged, h.attrs...)
	merged = append(merged, attrs...)
	return &plainHandler{w: h.w, level: h.level, attrs: merged, mu: h.mu}
}

func (h *plainHandler) WithGroup(_ string) slog.Handler {
	// Groups are not used by this service; flatten them.
	return h
}
