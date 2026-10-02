// Package api implements the HTTP layer of the code-interpreter service. It
// is the Go port of app/api/routes.py and the app factory in app/main.py.
package api

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"

	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/config"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/executor"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/logging"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/storage"
)

// Server wires the executor backend and file storage into HTTP handlers.
type Server struct {
	cfg     config.Config
	exec    executor.Executor
	storage *storage.FileStorageService
	log     *slog.Logger
}

// NewServer returns the service's HTTP handler.
func NewServer(
	cfg config.Config,
	exec executor.Executor,
	store *storage.FileStorageService,
) http.Handler {
	s := &Server{
		cfg:     cfg,
		exec:    exec,
		storage: store,
		log:     logging.Named("api"),
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("POST /v1/execute", s.handleExecute)
	mux.HandleFunc("POST /v1/execute/stream", s.handleExecuteStream)
	mux.HandleFunc("POST /v1/files", s.handleUploadFile)
	mux.HandleFunc("GET /v1/files", s.handleListFiles)
	mux.HandleFunc("GET /v1/files/{file_id}", s.handleDownloadFile)
	mux.HandleFunc("DELETE /v1/files/{file_id}", s.handleDeleteFile)
	mux.HandleFunc("POST /v1/sessions", s.handleCreateSession)
	mux.HandleFunc("DELETE /v1/sessions/{session_id}", s.handleDeleteSession)
	mux.HandleFunc("POST /v1/sessions/{session_id}/bash", s.handleSessionBash)
	return mux
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = jsonEncode(w, payload)
}

func writeDetail(w http.ResponseWriter, status int, detail string) {
	writeJSON(w, status, ErrorResponse{Detail: detail})
}

// writeError maps executor/service errors onto the same HTTP statuses the
// Python service uses: validation → 422, missing session → 404, unsupported
// operation → 501, everything else → 500.
func (s *Server) writeError(w http.ResponseWriter, err error) {
	var valErr *validationError
	var execValErr *executor.ValidationError
	var notFoundErr *executor.SessionNotFoundError
	var notImplErr *executor.NotImplementedError

	switch {
	case errors.As(err, &valErr):
		writeDetail(w, http.StatusUnprocessableEntity, valErr.message)
	case errors.As(err, &execValErr):
		writeDetail(w, http.StatusUnprocessableEntity, execValErr.Message)
	case errors.As(err, &notFoundErr):
		writeDetail(w, http.StatusNotFound, notFoundErr.Error())
	case errors.As(err, &notImplErr):
		writeDetail(w, http.StatusNotImplemented, notImplErr.Message)
	default:
		s.log.Error("Internal server error", "error", err.Error())
		writeDetail(w, http.StatusInternalServerError, "Internal Server Error")
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	result := s.exec.CheckHealth()
	var message *string
	if result.Message != "" {
		message = &result.Message
	}
	writeJSON(w, http.StatusOK, HealthResponse{
		Status:  result.Status,
		Message: message,
		Version: config.ServiceVersion,
	})
}

// resolveUploadedFiles resolves uploaded file IDs into content for the
// executor. It returns the staged files and a path→content map used to
// filter unchanged inputs out of the workspace snapshot.
func (s *Server) resolveUploadedFiles(
	files []ExecuteFile,
) ([]executor.StagedFile, map[string][]byte, error) {
	staged := make([]executor.StagedFile, 0, len(files))
	inputFiles := map[string][]byte{}
	for _, file := range files {
		content, _, err := s.storage.GetFile(*file.FileID)
		if err != nil {
			if errors.Is(err, storage.ErrFileNotFound) {
				return nil, nil, &httpError{
					status: http.StatusNotFound,
					detail: fmt.Sprintf(
						"File with ID '%s' not found for path '%s'.", *file.FileID, *file.Path,
					),
				}
			}
			return nil, nil, err
		}
		staged = append(staged, executor.StagedFile{Path: *file.Path, Content: content})
		inputFiles[*file.Path] = content
	}
	return staged, inputFiles, nil
}

// httpError carries an explicit status + detail through handler helpers.
type httpError struct {
	status int
	detail string
}

func (e *httpError) Error() string { return e.detail }

// saveWorkspaceFiles filters and saves new/modified workspace files to
// storage.
func (s *Server) saveWorkspaceFiles(
	entries []executor.WorkspaceEntry,
	inputFiles map[string][]byte,
) []WorkspaceFile {
	workspaceFiles := []WorkspaceFile{}
	for _, entry := range entries {
		if entry.Kind != executor.EntryKindFile || entry.Content == nil {
			continue
		}
		if original, ok := inputFiles[entry.Path]; ok && bytes.Equal(entry.Content, original) {
			continue
		}
		fileID, err := s.storage.SaveFile(entry.Content, entry.Path)
		if err != nil {
			s.log.Warn("Failed to save workspace file", "path", entry.Path, "error", err)
			continue
		}
		workspaceFiles = append(workspaceFiles, WorkspaceFile{
			Path:   entry.Path,
			Kind:   entry.Kind,
			FileID: &fileID,
		})
	}
	return workspaceFiles
}

// parseExecuteRequest decodes, defaults, and validates an execute request,
// including the timeout ceiling shared by both execute routes.
func (s *Server) parseExecuteRequest(r *http.Request) (*ExecuteRequest, error) {
	var req ExecuteRequest
	if err := decodeJSONBody(r.Body, &req); err != nil {
		return nil, err
	}
	if err := req.validate(); err != nil {
		return nil, err
	}
	if *req.TimeoutMs > s.cfg.MaxExecTimeoutMs {
		return nil, &validationError{
			message: fmt.Sprintf(
				"timeout_ms exceeds maximum of %d ms", s.cfg.MaxExecTimeoutMs,
			),
		}
	}
	return &req, nil
}

func (s *Server) execOptions(req *ExecuteRequest, staged []executor.StagedFile) executor.ExecOptions {
	cpu := s.cfg.CPUTimeLimitSec
	mem := s.cfg.MemoryLimitMB
	return executor.ExecOptions{
		Code:                *req.Code,
		Stdin:               req.Stdin,
		TimeoutMs:           *req.TimeoutMs,
		MaxOutputBytes:      s.cfg.MaxOutputBytes,
		CPUTimeLimitSec:     &cpu,
		MemoryLimitMB:       &mem,
		Files:               staged,
		LastLineInteractive: *req.LastLineInteractive,
	}
}

// handleExecute executes provided Python code synchronously within an
// isolated environment.
func (s *Server) handleExecute(w http.ResponseWriter, r *http.Request) {
	req, err := s.parseExecuteRequest(r)
	if err != nil {
		s.writeRequestError(w, err)
		return
	}

	staged, inputFiles, err := s.resolveUploadedFiles(req.Files)
	if err != nil {
		s.writeRequestError(w, err)
		return
	}

	result, err := s.exec.ExecutePython(s.execOptions(req, staged))
	if err != nil {
		s.writeError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, ExecuteResponse{
		Stdout:     result.Stdout,
		Stderr:     result.Stderr,
		ExitCode:   result.ExitCode,
		TimedOut:   result.TimedOut,
		DurationMs: result.DurationMs,
		Files:      s.saveWorkspaceFiles(result.Files, inputFiles),
	})
}

// handleExecuteStream executes Python code with streaming output via
// Server-Sent Events.
func (s *Server) handleExecuteStream(w http.ResponseWriter, r *http.Request) {
	req, err := s.parseExecuteRequest(r)
	if err != nil {
		s.writeRequestError(w, err)
		return
	}

	staged, inputFiles, err := s.resolveUploadedFiles(req.Files)
	if err != nil {
		s.writeRequestError(w, err)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)

	flusher, _ := w.(http.Flusher)
	emit := func(chunk executor.StreamChunk) {
		_, _ = io.WriteString(w, sseFrame("output", StreamOutputEvent{
			Stream: chunk.Stream,
			Data:   chunk.Data,
		}))
		if flusher != nil {
			flusher.Flush()
		}
	}

	result, err := s.exec.ExecutePythonStreaming(s.execOptions(req, staged), emit)
	if err != nil {
		_, _ = io.WriteString(w, sseFrame("error", StreamErrorEvent{Message: err.Error()}))
		if flusher != nil {
			flusher.Flush()
		}
		return
	}

	_, _ = io.WriteString(w, sseFrame("result", StreamResultEvent{
		ExitCode:   result.ExitCode,
		TimedOut:   result.TimedOut,
		DurationMs: result.DurationMs,
		Files:      s.saveWorkspaceFiles(result.Files, inputFiles),
	}))
	if flusher != nil {
		flusher.Flush()
	}
}

// writeRequestError handles pre-execution errors that may carry an explicit
// HTTP status (missing uploaded files) or be validation failures.
func (s *Server) writeRequestError(w http.ResponseWriter, err error) {
	var httpErr *httpError
	if errors.As(err, &httpErr) {
		writeDetail(w, httpErr.status, httpErr.detail)
		return
	}
	s.writeError(w, err)
}

// handleUploadFile uploads a file for later use in code execution.
func (s *Server) handleUploadFile(w http.ResponseWriter, r *http.Request) {
	file, header, err := r.FormFile("file")
	if err != nil {
		writeDetail(w, http.StatusUnprocessableEntity, "A 'file' form field is required.")
		return
	}
	defer func() { _ = file.Close() }()

	maxSizeBytes := int64(s.cfg.MaxFileSizeMB) * 1024 * 1024
	content, err := io.ReadAll(io.LimitReader(file, maxSizeBytes+1))
	if err != nil {
		s.writeError(w, err)
		return
	}
	if int64(len(content)) > maxSizeBytes {
		writeDetail(w, http.StatusRequestEntityTooLarge, fmt.Sprintf(
			"File size exceeds maximum of %d MB", s.cfg.MaxFileSizeMB,
		))
		return
	}

	filename := header.Filename
	if filename == "" {
		filename = "unnamed"
	}
	fileID, err := s.storage.SaveFile(content, filename)
	if err != nil {
		s.writeError(w, err)
		return
	}

	writeJSON(w, http.StatusCreated, UploadFileResponse{
		FileID:    fileID,
		Filename:  filename,
		SizeBytes: int64(len(content)),
	})
}

// handleDownloadFile downloads a previously uploaded file by its ID.
func (s *Server) handleDownloadFile(w http.ResponseWriter, r *http.Request) {
	fileID := r.PathValue("file_id")

	content, metadata, err := s.storage.GetFile(fileID)
	if err != nil {
		if errors.Is(err, storage.ErrFileNotFound) {
			writeDetail(w, http.StatusNotFound, fmt.Sprintf(
				"File with ID '%s' not found", fileID,
			))
			return
		}
		s.writeError(w, err)
		return
	}

	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set(
		"Content-Disposition", fmt.Sprintf("attachment; filename=%q", metadata.Filename),
	)
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(content)
}

// handleListFiles lists all uploaded files with their metadata.
func (s *Server) handleListFiles(w http.ResponseWriter, _ *http.Request) {
	files, err := s.storage.ListFiles()
	if err != nil {
		s.writeError(w, err)
		return
	}

	response := ListFilesResponse{Files: []FileMetadataResponse{}}
	for _, f := range files {
		response.Files = append(response.Files, FileMetadataResponse{
			FileID:     f.FileID,
			Filename:   f.Filename,
			SizeBytes:  f.SizeBytes,
			UploadTime: f.UploadTime,
		})
	}
	writeJSON(w, http.StatusOK, response)
}

// handleDeleteFile deletes a previously uploaded file by its ID.
func (s *Server) handleDeleteFile(w http.ResponseWriter, r *http.Request) {
	fileID := r.PathValue("file_id")

	deleted, err := s.storage.DeleteFile(fileID)
	if err != nil {
		s.writeError(w, err)
		return
	}
	if !deleted {
		writeDetail(w, http.StatusNotFound, fmt.Sprintf("File with ID '%s' not found", fileID))
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// handleCreateSession creates a long-lived code-executor pod/container with
// the given TTL. The session is guaranteed to be torn down at or before the
// TTL expires, even if the API service crashes and restarts.
func (s *Server) handleCreateSession(w http.ResponseWriter, r *http.Request) {
	var req CreateSessionRequest
	if err := decodeJSONBody(r.Body, &req); err != nil {
		s.writeRequestError(w, err)
		return
	}
	if err := req.validate(); err != nil {
		s.writeRequestError(w, err)
		return
	}

	staged, _, err := s.resolveUploadedFiles(req.Files)
	if err != nil {
		s.writeRequestError(w, err)
		return
	}

	cpu := s.cfg.CPUTimeLimitSec
	mem := s.cfg.MemoryLimitMB
	info, err := s.exec.CreateSession(*req.TTLSeconds, staged, &cpu, &mem)
	if err != nil {
		s.writeError(w, err)
		return
	}

	writeJSON(w, http.StatusCreated, CreateSessionResponse{
		SessionID: info.SessionID,
		ExpiresAt: info.ExpiresAt,
	})
}

// handleDeleteSession tears down a session pod/container by ID.
func (s *Server) handleDeleteSession(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")

	deleted, err := s.exec.DeleteSession(sessionID)
	if err != nil {
		s.writeError(w, err)
		return
	}
	if !deleted {
		writeDetail(w, http.StatusNotFound, fmt.Sprintf("Session '%s' not found", sessionID))
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// handleSessionBash runs a bash command inside an existing session.
//
// The session pod has no network access (enforced at session creation), and
// that restriction continues to apply for every command run via this route.
func (s *Server) handleSessionBash(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")

	var req BashExecRequest
	if err := decodeJSONBody(r.Body, &req); err != nil {
		s.writeRequestError(w, err)
		return
	}
	if err := req.validate(); err != nil {
		s.writeRequestError(w, err)
		return
	}
	if *req.TimeoutMs > s.cfg.MaxExecTimeoutMs {
		writeDetail(w, http.StatusUnprocessableEntity, fmt.Sprintf(
			"timeout_ms exceeds maximum of %d ms", s.cfg.MaxExecTimeoutMs,
		))
		return
	}

	result, err := s.exec.ExecuteBashInSession(
		sessionID, *req.Cmd, *req.TimeoutMs, s.cfg.MaxOutputBytes,
	)
	if err != nil {
		s.writeError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, BashExecResponse{
		Stdout:     result.Stdout,
		Stderr:     result.Stderr,
		ExitCode:   result.ExitCode,
		TimedOut:   result.TimedOut,
		DurationMs: result.DurationMs,
	})
}
