// Package executor defines the execution-backend abstraction and its shared
// types. It is the Go port of app/services/executor_base.py.
package executor

import (
	"fmt"
	"strings"
)

// EntryKind distinguishes files from directories in a workspace snapshot.
type EntryKind string

const (
	EntryKindFile      EntryKind = "file"
	EntryKindDirectory EntryKind = "directory"
)

// WorkspaceEntry is a single file or directory captured from the execution
// workspace after a run. Content is nil for directories.
type WorkspaceEntry struct {
	Path    string
	Kind    EntryKind
	Content []byte
}

// StagedFile is a file to place into the execution workspace before running.
type StagedFile struct {
	Path    string
	Content []byte
}

// ExecutionResult is the outcome of a completed (or timed-out) execution.
// ExitCode is nil when the run timed out.
type ExecutionResult struct {
	Stdout     string
	Stderr     string
	ExitCode   *int
	TimedOut   bool
	DurationMs int64
	Files      []WorkspaceEntry
}

// StreamChunk is a chunk of output emitted during a streaming execution.
// Stream is either "stdout" or "stderr".
type StreamChunk struct {
	Stream string
	Data   string
}

// StreamResult is the final event of a streaming execution.
type StreamResult struct {
	ExitCode   *int
	TimedOut   bool
	DurationMs int64
	Files      []WorkspaceEntry
}

// HealthCheck is the result of an executor health check. Status is "ok" or
// "error"; Message carries detail for errors.
type HealthCheck struct {
	Status  string
	Message string
}

// SessionInfo identifies a long-lived session. ExpiresAt is a Unix timestamp
// (fractional seconds).
type SessionInfo struct {
	SessionID string
	ExpiresAt float64
}

// Session naming/labeling shared by all backends and the reaper.
const (
	SessionNamePrefix     = "code-session-"
	SessionAppLabel       = "code-interpreter"
	SessionComponentLabel = "session"
	SessionExpiresAtKey   = "code-interpreter.expires-at"
)

// SessionNotFoundError is returned when a session ID does not refer to an
// existing session.
type SessionNotFoundError struct {
	SessionID string
}

func (e *SessionNotFoundError) Error() string {
	return fmt.Sprintf("Session '%s' not found", e.SessionID)
}

// NotImplementedError is returned by backends that do not support an
// operation (streaming, sessions). The API layer maps it to HTTP 501.
type NotImplementedError struct {
	Message string
}

func (e *NotImplementedError) Error() string { return e.Message }

// ValidationError marks caller mistakes (bad file paths, reserved names).
// The API layer maps it to HTTP 422, mirroring Python's ValueError handling.
type ValidationError struct {
	Message string
}

func (e *ValidationError) Error() string { return e.Message }

// ExecOptions carries the parameters of a single Python execution.
type ExecOptions struct {
	Code           string
	Stdin          *string
	TimeoutMs      int
	MaxOutputBytes int
	// Nil means "no limit" for the two resource limits below.
	CPUTimeLimitSec *int
	MemoryLimitMB   *int
	Files           []StagedFile
	// If true, the last line of code will print its value to stdout when it
	// is a bare expression (like Jupyter notebooks or the Python REPL). Only
	// the last line is affected.
	LastLineInteractive bool
}

// EmitFunc receives output chunks during a streaming execution.
type EmitFunc func(StreamChunk)

// Executor is the pluggable execution backend interface.
type Executor interface {
	// CheckHealth reports whether the backend is operational.
	CheckHealth() HealthCheck

	// ExecutePython runs Python code in an isolated environment.
	ExecutePython(opts ExecOptions) (ExecutionResult, error)

	// ExecutePythonStreaming runs Python code, invoking emit for each output
	// chunk as it arrives, and returns the final result.
	ExecutePythonStreaming(opts ExecOptions, emit EmitFunc) (StreamResult, error)

	// CreateSession creates a long-lived execution environment. The session
	// is guaranteed to be torn down at or before ExpiresAt even if this
	// process crashes.
	CreateSession(
		ttlSeconds int,
		files []StagedFile,
		cpuTimeLimitSec *int,
		memoryLimitMB *int,
	) (SessionInfo, error)

	// DeleteSession tears down a session by ID. Returns true if it was found
	// and deleted.
	DeleteSession(sessionID string) (bool, error)

	// ReapExpiredSessions deletes sessions whose TTL has elapsed and returns
	// the number reaped.
	ReapExpiredSessions() (int, error)

	// ExecuteBashInSession runs a bash command inside an existing session.
	// Returns *SessionNotFoundError when the session does not exist. Network
	// restrictions established at session creation remain in force.
	ExecuteBashInSession(
		sessionID string,
		cmd string,
		timeoutMs int,
		maxOutputBytes int,
	) (ExecutionResult, error)
}

// TruncateOutput decodes an output stream as UTF-8 (invalid sequences are
// replaced) and truncates it to maxBytes with a trailing marker.
func TruncateOutput(stream []byte, maxBytes int) string {
	if len(stream) <= maxBytes {
		return strings.ToValidUTF8(string(stream), "�")
	}
	head := stream[:max(0, maxBytes-32)]
	truncated := append(append([]byte{}, head...), []byte("\n...[truncated]")...)
	return strings.ToValidUTF8(string(truncated), "�")
}
