package api

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/executor"
)

// Session and bash defaults, mirroring app/models/schemas.py.
const (
	DefaultSessionTTLSec = 15 * 60
	MaxSessionTTLSec     = 24 * 60 * 60
	DefaultBashTimeoutMs = 30_000
	defaultExecTimeoutMs = 2000
)

// ExecuteFile references a previously uploaded file to stage in the
// execution workspace.
type ExecuteFile struct {
	Path   *string `json:"path"`
	FileID *string `json:"file_id"`
}

// ExecuteRequest is the body of POST /v1/execute and /v1/execute/stream.
// Pointer fields distinguish "absent" from zero values so defaults can be
// applied after decoding.
type ExecuteRequest struct {
	Code                *string       `json:"code"`
	Stdin               *string       `json:"stdin"`
	TimeoutMs           *int          `json:"timeout_ms"`
	LastLineInteractive *bool         `json:"last_line_interactive"`
	Files               []ExecuteFile `json:"files"`
}

// validationError is mapped to HTTP 422 by the handlers, mirroring
// FastAPI/Pydantic request validation.
type validationError struct{ message string }

func (e *validationError) Error() string { return e.message }

// decodeJSONBody decodes a request body, translating malformed JSON and type
// mismatches into validation errors.
func decodeJSONBody(body io.Reader, dst any) error {
	data, err := io.ReadAll(body)
	if err != nil {
		return &validationError{message: "Failed to read request body"}
	}
	if err := json.Unmarshal(data, dst); err != nil {
		return &validationError{message: "Invalid request body: " + err.Error()}
	}
	return nil
}

// validate applies defaults and enforces the same constraints as the Pydantic
// model (code required, timeout_ms >= 1).
func (r *ExecuteRequest) validate() error {
	if r.Code == nil {
		return &validationError{message: "Field 'code' is required."}
	}
	if r.TimeoutMs == nil {
		v := defaultExecTimeoutMs
		r.TimeoutMs = &v
	}
	if *r.TimeoutMs < 1 {
		return &validationError{message: "timeout_ms must be greater than or equal to 1"}
	}
	if r.LastLineInteractive == nil {
		v := true
		r.LastLineInteractive = &v
	}
	for _, f := range r.Files {
		if f.Path == nil || f.FileID == nil {
			return &validationError{
				message: "Each file must include both 'path' and 'file_id'.",
			}
		}
	}
	return nil
}

// WorkspaceFile is a saved output file in an execute response.
type WorkspaceFile struct {
	Path   string             `json:"path"`
	Kind   executor.EntryKind `json:"kind"`
	FileID *string            `json:"file_id"`
}

// ExecuteResponse is the body of a successful POST /v1/execute.
type ExecuteResponse struct {
	Stdout     string          `json:"stdout"`
	Stderr     string          `json:"stderr"`
	ExitCode   *int            `json:"exit_code"`
	TimedOut   bool            `json:"timed_out"`
	DurationMs int64           `json:"duration_ms"`
	Files      []WorkspaceFile `json:"files"`
}

// StreamOutputEvent is the payload of "output" SSE events.
type StreamOutputEvent struct {
	Stream string `json:"stream"`
	Data   string `json:"data"`
}

// StreamResultEvent is the payload of the final "result" SSE event.
type StreamResultEvent struct {
	ExitCode   *int            `json:"exit_code"`
	TimedOut   bool            `json:"timed_out"`
	DurationMs int64           `json:"duration_ms"`
	Files      []WorkspaceFile `json:"files"`
}

// StreamErrorEvent is the payload of "error" SSE events.
type StreamErrorEvent struct {
	Message string `json:"message"`
}

// jsonEncode writes v as JSON to w.
func jsonEncode(w io.Writer, v any) error {
	return json.NewEncoder(w).Encode(v)
}

// sseFrame formats a fully-formed SSE frame for an event payload.
func sseFrame(event string, payload any) string {
	data, err := json.Marshal(payload)
	if err != nil {
		data = []byte("{}")
	}
	return fmt.Sprintf("event: %s\ndata: %s\n\n", event, data)
}

// UploadFileResponse is the body of a successful POST /v1/files.
type UploadFileResponse struct {
	FileID    string `json:"file_id"`
	Filename  string `json:"filename"`
	SizeBytes int64  `json:"size_bytes"`
}

// FileMetadataResponse describes one stored file in a list response.
type FileMetadataResponse struct {
	FileID     string  `json:"file_id"`
	Filename   string  `json:"filename"`
	SizeBytes  int64   `json:"size_bytes"`
	UploadTime float64 `json:"upload_time"`
}

// ListFilesResponse is the body of GET /v1/files.
type ListFilesResponse struct {
	Files []FileMetadataResponse `json:"files"`
}

// HealthResponse is the body of GET /health.
type HealthResponse struct {
	Status  string  `json:"status"`
	Message *string `json:"message"`
	Version string  `json:"version"`
}

// CreateSessionRequest is the body of POST /v1/sessions.
type CreateSessionRequest struct {
	Files      []ExecuteFile `json:"files"`
	TTLSeconds *int          `json:"ttl_seconds"`
}

// validate applies defaults and enforces 1 <= ttl_seconds <= MaxSessionTTLSec.
func (r *CreateSessionRequest) validate() error {
	if r.TTLSeconds == nil {
		v := DefaultSessionTTLSec
		r.TTLSeconds = &v
	}
	if *r.TTLSeconds < 1 {
		return &validationError{message: "ttl_seconds must be greater than or equal to 1"}
	}
	if *r.TTLSeconds > MaxSessionTTLSec {
		return &validationError{
			message: fmt.Sprintf(
				"ttl_seconds must be less than or equal to %d", MaxSessionTTLSec,
			),
		}
	}
	for _, f := range r.Files {
		if f.Path == nil || f.FileID == nil {
			return &validationError{
				message: "Each file must include both 'path' and 'file_id'.",
			}
		}
	}
	return nil
}

// CreateSessionResponse is the body of a successful POST /v1/sessions.
type CreateSessionResponse struct {
	SessionID string  `json:"session_id"`
	ExpiresAt float64 `json:"expires_at"`
}

// BashExecRequest is the body of POST /v1/sessions/{session_id}/bash.
type BashExecRequest struct {
	Cmd       *string `json:"cmd"`
	TimeoutMs *int    `json:"timeout_ms"`
}

// validate applies defaults and enforces cmd presence and timeout_ms >= 1.
func (r *BashExecRequest) validate() error {
	if r.Cmd == nil {
		return &validationError{message: "Field 'cmd' is required."}
	}
	if r.TimeoutMs == nil {
		v := DefaultBashTimeoutMs
		r.TimeoutMs = &v
	}
	if *r.TimeoutMs < 1 {
		return &validationError{message: "timeout_ms must be greater than or equal to 1"}
	}
	return nil
}

// BashExecResponse is the body of a successful bash execution.
type BashExecResponse struct {
	Stdout     string `json:"stdout"`
	Stderr     string `json:"stderr"`
	ExitCode   *int   `json:"exit_code"`
	TimedOut   bool   `json:"timed_out"`
	DurationMs int64  `json:"duration_ms"`
}

// ErrorResponse mirrors FastAPI's {"detail": ...} error body.
type ErrorResponse struct {
	Detail string `json:"detail"`
}
