package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/config"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/executor"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/storage"
)

// fakeExecutor is a configurable in-memory Executor for API tests, standing
// in for the dummy backends used by the Python integration tests.
type fakeExecutor struct {
	executeFunc   func(opts executor.ExecOptions) (executor.ExecutionResult, error)
	streamFunc    func(opts executor.ExecOptions, emit executor.EmitFunc) (executor.StreamResult, error)
	createFunc    func(ttl int) (executor.SessionInfo, error)
	deleteFunc    func(id string) (bool, error)
	bashFunc      func(id, cmd string) (executor.ExecutionResult, error)
	lastExecOpts  *executor.ExecOptions
	lastSessionID string
}

func intPtr(v int) *int { return &v }

func (f *fakeExecutor) CheckHealth() executor.HealthCheck {
	return executor.HealthCheck{Status: "ok"}
}

func (f *fakeExecutor) ExecutePython(opts executor.ExecOptions) (executor.ExecutionResult, error) {
	f.lastExecOpts = &opts
	if f.executeFunc != nil {
		return f.executeFunc(opts)
	}
	return executor.ExecutionResult{
		Stdout: "hello\n", Stderr: "", ExitCode: intPtr(0), DurationMs: 5,
	}, nil
}

func (f *fakeExecutor) ExecutePythonStreaming(
	opts executor.ExecOptions, emit executor.EmitFunc,
) (executor.StreamResult, error) {
	f.lastExecOpts = &opts
	if f.streamFunc != nil {
		return f.streamFunc(opts, emit)
	}
	emit(executor.StreamChunk{Stream: "stdout", Data: "hello\n"})
	return executor.StreamResult{ExitCode: intPtr(0), DurationMs: 5}, nil
}

func (f *fakeExecutor) CreateSession(
	ttlSeconds int, _ []executor.StagedFile, _ *int, _ *int,
) (executor.SessionInfo, error) {
	if f.createFunc != nil {
		return f.createFunc(ttlSeconds)
	}
	return executor.SessionInfo{
		SessionID: "code-session-abc123", ExpiresAt: 1700000000.5,
	}, nil
}

func (f *fakeExecutor) DeleteSession(sessionID string) (bool, error) {
	f.lastSessionID = sessionID
	if f.deleteFunc != nil {
		return f.deleteFunc(sessionID)
	}
	return true, nil
}

func (f *fakeExecutor) ReapExpiredSessions() (int, error) { return 0, nil }

func (f *fakeExecutor) ExecuteBashInSession(
	sessionID, cmd string, _ int, _ int,
) (executor.ExecutionResult, error) {
	f.lastSessionID = sessionID
	if f.bashFunc != nil {
		return f.bashFunc(sessionID, cmd)
	}
	return executor.ExecutionResult{
		Stdout: "ok\n", ExitCode: intPtr(0), DurationMs: 3,
	}, nil
}

func testConfig() config.Config {
	cfg := config.Load()
	cfg.MaxExecTimeoutMs = 60_000
	cfg.MaxOutputBytes = 1_000_000
	cfg.MaxFileSizeMB = 1
	return cfg
}

func newTestServer(t *testing.T, fake *fakeExecutor) (*httptest.Server, *storage.FileStorageService) {
	t.Helper()
	store, err := storage.NewFileStorageService(t.TempDir())
	if err != nil {
		t.Fatalf("storage: %v", err)
	}
	srv := httptest.NewServer(NewServer(testConfig(), fake, store))
	t.Cleanup(srv.Close)
	return srv, store
}

func postJSON(t *testing.T, url string, body string) *http.Response {
	t.Helper()
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	return resp
}

func decodeBody[T any](t *testing.T, resp *http.Response) T {
	t.Helper()
	defer func() { _ = resp.Body.Close() }()
	var v T
	if err := json.NewDecoder(resp.Body).Decode(&v); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	return v
}

// --- health --------------------------------------------------------------

func TestHealth(t *testing.T) {
	srv, _ := newTestServer(t, &fakeExecutor{})
	resp, err := http.Get(srv.URL + "/health")
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	payload := decodeBody[map[string]any](t, resp)
	if payload["status"] != "ok" {
		t.Errorf("status = %v, want ok", payload["status"])
	}
	if payload["version"] != config.ServiceVersion {
		t.Errorf("version = %v, want %s", payload["version"], config.ServiceVersion)
	}
	if msg, present := payload["message"]; !present || msg != nil {
		t.Errorf("message = %v, want explicit null", msg)
	}
}

// --- /v1/execute ----------------------------------------------------------

func TestExecuteReturnsExpectedPayload(t *testing.T) {
	fake := &fakeExecutor{}
	srv, _ := newTestServer(t, fake)

	resp := postJSON(t, srv.URL+"/v1/execute",
		`{"code": "print('hello')", "stdin": null, "timeout_ms": 1000}`)
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	payload := decodeBody[map[string]any](t, resp)
	if payload["stdout"] != "hello\n" {
		t.Errorf("stdout = %v", payload["stdout"])
	}
	if payload["stderr"] != "" {
		t.Errorf("stderr = %v", payload["stderr"])
	}
	if payload["exit_code"] != float64(0) {
		t.Errorf("exit_code = %v", payload["exit_code"])
	}
	if payload["timed_out"] != false {
		t.Errorf("timed_out = %v", payload["timed_out"])
	}
	if _, ok := payload["duration_ms"].(float64); !ok {
		t.Errorf("duration_ms = %v", payload["duration_ms"])
	}
	if files, ok := payload["files"].([]any); !ok || len(files) != 0 {
		t.Errorf("files = %v, want []", payload["files"])
	}

	if fake.lastExecOpts.TimeoutMs != 1000 {
		t.Errorf("executor received timeout %d", fake.lastExecOpts.TimeoutMs)
	}
	if !fake.lastExecOpts.LastLineInteractive {
		t.Error("last_line_interactive should default to true")
	}
}

func TestExecuteValidation(t *testing.T) {
	srv, _ := newTestServer(t, &fakeExecutor{})

	cases := []struct {
		name string
		body string
		want int
	}{
		{"timeout above max", `{"code": "1", "timeout_ms": 60001}`, 422},
		{"timeout below one", `{"code": "1", "timeout_ms": 0}`, 422},
		{"missing code", `{"timeout_ms": 100}`, 422},
		{"code wrong type", `{"code": 42}`, 422},
		{"malformed json", `{"code": `, 422},
		{"defaults applied", `{"code": "1"}`, 200},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp := postJSON(t, srv.URL+"/v1/execute", tc.body)
			defer func() { _ = resp.Body.Close() }()
			if resp.StatusCode != tc.want {
				body, _ := io.ReadAll(resp.Body)
				t.Errorf("status = %d, want %d (body: %s)", resp.StatusCode, tc.want, body)
			}
		})
	}
}

func TestExecuteTimeoutDefaultIs2000(t *testing.T) {
	fake := &fakeExecutor{}
	srv, _ := newTestServer(t, fake)
	resp := postJSON(t, srv.URL+"/v1/execute", `{"code": "1"}`)
	_ = resp.Body.Close()
	if fake.lastExecOpts.TimeoutMs != 2000 {
		t.Errorf("default timeout = %d, want 2000", fake.lastExecOpts.TimeoutMs)
	}
}

func TestExecuteWithUnknownFileID(t *testing.T) {
	srv, _ := newTestServer(t, &fakeExecutor{})
	resp := postJSON(t, srv.URL+"/v1/execute",
		`{"code": "1", "files": [{"path": "in.txt", "file_id": "missing-id"}]}`)
	if resp.StatusCode != 404 {
		t.Fatalf("status = %d, want 404", resp.StatusCode)
	}
	payload := decodeBody[map[string]any](t, resp)
	detail, _ := payload["detail"].(string)
	if !strings.Contains(detail, "missing-id") || !strings.Contains(detail, "in.txt") {
		t.Errorf("detail = %q", detail)
	}
}

func TestExecuteStagesUploadedFiles(t *testing.T) {
	fake := &fakeExecutor{}
	srv, store := newTestServer(t, fake)

	fileID, err := store.SaveFile([]byte("col1,col2\n"), "data.csv")
	if err != nil {
		t.Fatal(err)
	}

	resp := postJSON(t, srv.URL+"/v1/execute", fmt.Sprintf(
		`{"code": "1", "files": [{"path": "input/data.csv", "file_id": %q}]}`, fileID))
	_ = resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if len(fake.lastExecOpts.Files) != 1 {
		t.Fatalf("staged files = %d, want 1", len(fake.lastExecOpts.Files))
	}
	staged := fake.lastExecOpts.Files[0]
	if staged.Path != "input/data.csv" || string(staged.Content) != "col1,col2\n" {
		t.Errorf("staged = %+v", staged)
	}
}

func TestExecuteSavesNewWorkspaceFilesOnly(t *testing.T) {
	fake := &fakeExecutor{}
	srv, store := newTestServer(t, fake)

	inputContent := []byte("unchanged")
	fileID, _ := store.SaveFile(inputContent, "in.txt")

	fake.executeFunc = func(opts executor.ExecOptions) (executor.ExecutionResult, error) {
		return executor.ExecutionResult{
			Stdout: "", ExitCode: intPtr(0), DurationMs: 1,
			Files: []executor.WorkspaceEntry{
				{Path: "outdir", Kind: executor.EntryKindDirectory},
				{Path: "in.txt", Kind: executor.EntryKindFile, Content: inputContent},
				{Path: "out.txt", Kind: executor.EntryKindFile, Content: []byte("fresh")},
			},
		}, nil
	}

	resp := postJSON(t, srv.URL+"/v1/execute", fmt.Sprintf(
		`{"code": "1", "files": [{"path": "in.txt", "file_id": %q}]}`, fileID))
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	payload := decodeBody[ExecuteResponse](t, resp)

	if len(payload.Files) != 1 {
		t.Fatalf("files = %+v, want only the new out.txt", payload.Files)
	}
	saved := payload.Files[0]
	if saved.Path != "out.txt" || saved.Kind != executor.EntryKindFile || saved.FileID == nil {
		t.Fatalf("saved = %+v", saved)
	}

	content, _, err := store.GetFile(*saved.FileID)
	if err != nil || string(content) != "fresh" {
		t.Errorf("stored output file = %q, %v", content, err)
	}
}

// --- /v1/execute/stream ----------------------------------------------------

type sseEvent struct {
	event string
	data  map[string]any
}

func parseSSE(t *testing.T, raw string) []sseEvent {
	t.Helper()
	var events []sseEvent
	var current string
	var data string
	for _, line := range strings.Split(raw, "\n") {
		switch {
		case strings.HasPrefix(line, "event: "):
			current = line[7:]
		case strings.HasPrefix(line, "data: "):
			data = line[6:]
		case line == "" && current != "":
			var payload map[string]any
			if err := json.Unmarshal([]byte(data), &payload); err != nil {
				t.Fatalf("bad SSE data %q: %v", data, err)
			}
			events = append(events, sseEvent{event: current, data: payload})
			current, data = "", ""
		}
	}
	return events
}

func TestStreamingBasicOutput(t *testing.T) {
	srv, _ := newTestServer(t, &fakeExecutor{})

	resp := postJSON(t, srv.URL+"/v1/execute/stream",
		`{"code": "print('hello')", "timeout_ms": 5000}`)
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); !strings.Contains(ct, "text/event-stream") {
		t.Fatalf("content-type = %q", ct)
	}

	body, _ := io.ReadAll(resp.Body)
	events := parseSSE(t, string(body))

	var outputs, results []sseEvent
	for _, e := range events {
		switch e.event {
		case "output":
			outputs = append(outputs, e)
		case "result":
			results = append(results, e)
		}
	}
	if len(results) != 1 {
		t.Fatalf("result events = %d, want 1", len(results))
	}
	result := results[0].data
	if result["exit_code"] != float64(0) || result["timed_out"] != false {
		t.Errorf("result = %v", result)
	}

	var stdout string
	for _, e := range outputs {
		if e.data["stream"] == "stdout" {
			stdout += e.data["data"].(string)
		}
	}
	if stdout != "hello\n" {
		t.Errorf("stdout = %q", stdout)
	}
}

func TestStreamingEmitsErrorEvent(t *testing.T) {
	fake := &fakeExecutor{
		streamFunc: func(_ executor.ExecOptions, emit executor.EmitFunc) (executor.StreamResult, error) {
			emit(executor.StreamChunk{Stream: "stdout", Data: "partial"})
			return executor.StreamResult{}, fmt.Errorf("backend exploded")
		},
	}
	srv, _ := newTestServer(t, fake)

	resp := postJSON(t, srv.URL+"/v1/execute/stream", `{"code": "1"}`)
	defer func() { _ = resp.Body.Close() }()
	body, _ := io.ReadAll(resp.Body)
	events := parseSSE(t, string(body))

	var sawError bool
	for _, e := range events {
		if e.event == "error" {
			sawError = true
			if !strings.Contains(e.data["message"].(string), "backend exploded") {
				t.Errorf("error message = %v", e.data["message"])
			}
		}
		if e.event == "result" {
			t.Error("result event must not follow an error")
		}
	}
	if !sawError {
		t.Error("no error event emitted")
	}
}

func TestStreamingValidationRejectedBeforeStream(t *testing.T) {
	srv, _ := newTestServer(t, &fakeExecutor{})
	resp := postJSON(t, srv.URL+"/v1/execute/stream", `{"code": "1", "timeout_ms": 999999}`)
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != 422 {
		t.Errorf("status = %d, want 422", resp.StatusCode)
	}
}

// --- /v1/files ---------------------------------------------------------------

func uploadFile(t *testing.T, url, filename string, content []byte) *http.Response {
	t.Helper()
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	fw, err := mw.CreateFormFile("file", filename)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fw.Write(content); err != nil {
		t.Fatal(err)
	}
	_ = mw.Close()

	resp, err := http.Post(url+"/v1/files", mw.FormDataContentType(), &buf)
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

func TestFileUploadDownloadRoundTrip(t *testing.T) {
	srv, _ := newTestServer(t, &fakeExecutor{})
	content := []byte{0x00, 0x01, 0xFF, 0xFE, 'a', 'b'}

	resp := uploadFile(t, srv.URL, "blob.bin", content)
	if resp.StatusCode != 201 {
		t.Fatalf("upload status = %d, want 201", resp.StatusCode)
	}
	uploaded := decodeBody[UploadFileResponse](t, resp)
	if uploaded.Filename != "blob.bin" || uploaded.SizeBytes != int64(len(content)) {
		t.Errorf("upload payload = %+v", uploaded)
	}

	dl, err := http.Get(srv.URL + "/v1/files/" + uploaded.FileID)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = dl.Body.Close() }()
	if dl.StatusCode != 200 {
		t.Fatalf("download status = %d", dl.StatusCode)
	}
	if cd := dl.Header.Get("Content-Disposition"); !strings.Contains(cd, "blob.bin") {
		t.Errorf("content-disposition = %q", cd)
	}
	got, _ := io.ReadAll(dl.Body)
	if !bytes.Equal(got, content) {
		t.Error("binary content corrupted through upload/download")
	}
}

func TestFileUploadTooLarge(t *testing.T) {
	srv, _ := newTestServer(t, &fakeExecutor{}) // MaxFileSizeMB = 1
	big := bytes.Repeat([]byte("x"), 1024*1024+1)
	resp := uploadFile(t, srv.URL, "big.bin", big)
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != 413 {
		t.Errorf("status = %d, want 413", resp.StatusCode)
	}
}

func TestFileListAndDelete(t *testing.T) {
	srv, _ := newTestServer(t, &fakeExecutor{})

	resp := uploadFile(t, srv.URL, "a.txt", []byte("a"))
	uploaded := decodeBody[UploadFileResponse](t, resp)

	listResp, err := http.Get(srv.URL + "/v1/files")
	if err != nil {
		t.Fatal(err)
	}
	listed := decodeBody[ListFilesResponse](t, listResp)
	if len(listed.Files) != 1 || listed.Files[0].FileID != uploaded.FileID {
		t.Errorf("list = %+v", listed)
	}

	req, _ := http.NewRequest(http.MethodDelete, srv.URL+"/v1/files/"+uploaded.FileID, nil)
	delResp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = delResp.Body.Close()
	if delResp.StatusCode != 204 {
		t.Errorf("delete status = %d, want 204", delResp.StatusCode)
	}

	req, _ = http.NewRequest(http.MethodDelete, srv.URL+"/v1/files/"+uploaded.FileID, nil)
	delResp, _ = http.DefaultClient.Do(req)
	_ = delResp.Body.Close()
	if delResp.StatusCode != 404 {
		t.Errorf("second delete status = %d, want 404", delResp.StatusCode)
	}

	dl, _ := http.Get(srv.URL + "/v1/files/" + uploaded.FileID)
	_ = dl.Body.Close()
	if dl.StatusCode != 404 {
		t.Errorf("download after delete = %d, want 404", dl.StatusCode)
	}
}

// --- /v1/sessions -------------------------------------------------------------

func TestCreateSession(t *testing.T) {
	srv, _ := newTestServer(t, &fakeExecutor{})

	resp := postJSON(t, srv.URL+"/v1/sessions", `{"ttl_seconds": 300}`)
	if resp.StatusCode != 201 {
		t.Fatalf("status = %d, want 201", resp.StatusCode)
	}
	payload := decodeBody[CreateSessionResponse](t, resp)
	if payload.SessionID != "code-session-abc123" || payload.ExpiresAt != 1700000000.5 {
		t.Errorf("payload = %+v", payload)
	}
}

func TestCreateSessionTTLValidation(t *testing.T) {
	srv, _ := newTestServer(t, &fakeExecutor{})
	for _, body := range []string{
		`{"ttl_seconds": 0}`,
		`{"ttl_seconds": 86401}`,
	} {
		resp := postJSON(t, srv.URL+"/v1/sessions", body)
		_ = resp.Body.Close()
		if resp.StatusCode != 422 {
			t.Errorf("body %s: status = %d, want 422", body, resp.StatusCode)
		}
	}
}

func TestCreateSessionNotImplemented(t *testing.T) {
	fake := &fakeExecutor{
		createFunc: func(int) (executor.SessionInfo, error) {
			return executor.SessionInfo{}, &executor.NotImplementedError{
				Message: "fakeExecutor does not support sessions",
			}
		},
	}
	srv, _ := newTestServer(t, fake)
	resp := postJSON(t, srv.URL+"/v1/sessions", `{}`)
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != 501 {
		t.Errorf("status = %d, want 501", resp.StatusCode)
	}
}

func TestDeleteSession(t *testing.T) {
	fake := &fakeExecutor{}
	srv, _ := newTestServer(t, fake)

	req, _ := http.NewRequest(http.MethodDelete, srv.URL+"/v1/sessions/code-session-abc123", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != 204 {
		t.Errorf("status = %d, want 204", resp.StatusCode)
	}
	if fake.lastSessionID != "code-session-abc123" {
		t.Errorf("executor got session %q", fake.lastSessionID)
	}

	fake.deleteFunc = func(string) (bool, error) { return false, nil }
	req, _ = http.NewRequest(http.MethodDelete, srv.URL+"/v1/sessions/code-session-missing", nil)
	resp, _ = http.DefaultClient.Do(req)
	_ = resp.Body.Close()
	if resp.StatusCode != 404 {
		t.Errorf("missing session status = %d, want 404", resp.StatusCode)
	}
}

// --- /v1/sessions/{id}/bash -----------------------------------------------------

func TestSessionBash(t *testing.T) {
	srv, _ := newTestServer(t, &fakeExecutor{})

	resp := postJSON(t, srv.URL+"/v1/sessions/code-session-abc123/bash", `{"cmd": "echo ok"}`)
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	payload := decodeBody[BashExecResponse](t, resp)
	if payload.Stdout != "ok\n" || payload.ExitCode == nil || *payload.ExitCode != 0 {
		t.Errorf("payload = %+v", payload)
	}
}

func TestSessionBashValidationAndErrors(t *testing.T) {
	fake := &fakeExecutor{}
	srv, _ := newTestServer(t, fake)
	url := srv.URL + "/v1/sessions/code-session-abc123/bash"

	resp := postJSON(t, url, `{"cmd": "echo", "timeout_ms": 60001}`)
	_ = resp.Body.Close()
	if resp.StatusCode != 422 {
		t.Errorf("timeout above max: status = %d, want 422", resp.StatusCode)
	}

	resp = postJSON(t, url, `{"timeout_ms": 100}`)
	_ = resp.Body.Close()
	if resp.StatusCode != 422 {
		t.Errorf("missing cmd: status = %d, want 422", resp.StatusCode)
	}

	fake.bashFunc = func(id, _ string) (executor.ExecutionResult, error) {
		return executor.ExecutionResult{}, &executor.SessionNotFoundError{SessionID: id}
	}
	resp = postJSON(t, url, `{"cmd": "echo"}`)
	if resp.StatusCode != 404 {
		t.Fatalf("missing session: status = %d, want 404", resp.StatusCode)
	}
	payload := decodeBody[map[string]any](t, resp)
	if detail, _ := payload["detail"].(string); !strings.Contains(detail, "not found") {
		t.Errorf("detail = %v", payload["detail"])
	}
}
