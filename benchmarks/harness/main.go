// Benchmark harness comparing the Python and Go code-interpreter services.
// Runs each server as a native process (no Docker daemon required),
// measuring:
//   - cold-start time to first served request (N runs, first discarded)
//   - CPU time consumed to reach readiness
//   - idle RSS after settle, RSS after load
//   - API-layer latency/throughput on storage endpoints (no executor needed)
//
// Usage:
//
//	(cd code-interpreter && uv sync --locked)
//	(cd code-interpreter-go && CGO_ENABLED=0 go build -trimpath \
//	  -ldflags="-s -w" -o /tmp/code-interpreter-api ./cmd/code-interpreter-api)
//	cd benchmarks/harness && go run .
//
// Environment overrides: PYTHON_SERVICE_BIN, PYTHON_SERVICE_DIR,
// GO_SERVICE_BIN. Defaults assume running from benchmarks/harness.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// abs resolves a path against the harness's own working directory. Relative
// argv[0] paths would otherwise resolve against the child's Dir at exec time.
func abs(path string) string {
	a, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	return a
}

type target struct {
	name string
	argv []string
	dir  string
}

var targets = []target{
	{
		name: "python",
		argv: []string{abs(envOr(
			"PYTHON_SERVICE_BIN", "../../code-interpreter/.venv/bin/code-interpreter-api",
		))},
		dir: abs(envOr("PYTHON_SERVICE_DIR", "../../code-interpreter")),
	},
	{
		name: "go",
		argv: []string{abs(envOr("GO_SERVICE_BIN", "/tmp/code-interpreter-api"))},
		dir:  "/tmp",
	},
}

type proc struct {
	cmd    *exec.Cmd
	port   int
	base   string
	stderr *bytes.Buffer
}

func spawn(t target, port int, storageDir string) (*proc, error) {
	cmd := exec.Command(t.argv[0], t.argv[1:]...)
	cmd.Dir = t.dir
	cmd.Env = append(os.Environ(),
		"HOST=127.0.0.1",
		"PORT="+strconv.Itoa(port),
		"FILE_STORAGE_DIR="+storageDir,
		"EXECUTOR_BACKEND=docker",
		// No daemon in this environment: Go skips DinD bootstrap via the
		// external-daemon branch; Python ignores this variable.
		"DOCKER_HOST=unix:///nonexistent-bench.sock",
		"LOG_LEVEL=WARNING", // suppress access logs on both for fair load tests
	)
	var errBuf bytes.Buffer
	cmd.Stdout = &errBuf
	cmd.Stderr = &errBuf
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return &proc{
		cmd:    cmd,
		port:   port,
		base:   "http://127.0.0.1:" + strconv.Itoa(port),
		stderr: &errBuf,
	}, nil
}

func (p *proc) waitReady(client *http.Client, timeout time.Duration) (time.Duration, error) {
	start := time.Now()
	deadline := start.Add(timeout)
	for time.Now().Before(deadline) {
		resp, err := client.Get(p.base + "/v1/files")
		if err == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if resp.StatusCode == 200 {
				return time.Since(start), nil
			}
		}
		time.Sleep(2 * time.Millisecond)
	}
	return 0, fmt.Errorf("not ready within %s; stderr:\n%s", timeout, p.stderr.String())
}

func (p *proc) kill() {
	_ = p.cmd.Process.Signal(syscall.SIGKILL)
	_, _ = p.cmd.Process.Wait()
}

// procMem returns VmRSS and VmHWM in KiB.
func procMem(pid int) (rss, hwm int64) {
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid))
	if err != nil {
		return 0, 0
	}
	for _, line := range strings.Split(string(data), "\n") {
		if v, ok := strings.CutPrefix(line, "VmRSS:"); ok {
			rss, _ = strconv.ParseInt(strings.Fields(v)[0], 10, 64)
		}
		if v, ok := strings.CutPrefix(line, "VmHWM:"); ok {
			hwm, _ = strconv.ParseInt(strings.Fields(v)[0], 10, 64)
		}
	}
	return rss, hwm
}

// procCPU returns utime+stime in seconds.
func procCPU(pid int) float64 {
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return 0
	}
	// Fields after the comm field (which may contain spaces inside parens).
	idx := strings.LastIndexByte(string(data), ')')
	fields := strings.Fields(string(data)[idx+2:])
	utime, _ := strconv.ParseFloat(fields[11], 64) // field 14 overall
	stime, _ := strconv.ParseFloat(fields[12], 64) // field 15 overall
	return (utime + stime) / 100.0
}

func newClient() *http.Client {
	return &http.Client{
		Transport: &http.Transport{
			MaxIdleConns:        64,
			MaxIdleConnsPerHost: 64,
		},
		Timeout: 30 * time.Second,
	}
}

type startupResult struct {
	ReadyMs []float64
	CPUSec  []float64
}

func benchStartup(t target, runs int, basePort int) (startupResult, error) {
	var res startupResult
	client := newClient()
	for i := 0; i < runs; i++ {
		storage, _ := os.MkdirTemp("", "bench-"+t.name+"-*")
		p, err := spawn(t, basePort+i, storage)
		if err != nil {
			return res, err
		}
		d, err := p.waitReady(client, 60*time.Second)
		if err != nil {
			p.kill()
			return res, fmt.Errorf("%s run %d: %w", t.name, i, err)
		}
		cpu := procCPU(p.cmd.Process.Pid)
		p.kill()
		_ = os.RemoveAll(storage)
		// Discard the first (warmup: page cache, .pyc compilation).
		if i > 0 {
			res.ReadyMs = append(res.ReadyMs, float64(d.Microseconds())/1000.0)
			res.CPUSec = append(res.CPUSec, cpu)
		}
	}
	return res, nil
}

type latencyStats struct {
	RPS float64
	P50 float64
	P90 float64
	P99 float64
	N   int
}

func percentile(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	idx := int(p * float64(len(sorted)-1))
	return sorted[idx]
}

func runLoad(
	client *http.Client,
	concurrency int,
	duration time.Duration,
	do func() error,
) (latencyStats, error) {
	var mu sync.Mutex
	var all []float64
	var firstErr error
	start := time.Now()
	var wg sync.WaitGroup
	for w := 0; w < concurrency; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			var local []float64
			for time.Since(start) < duration {
				t0 := time.Now()
				err := do()
				if err != nil {
					mu.Lock()
					if firstErr == nil {
						firstErr = err
					}
					mu.Unlock()
					return
				}
				local = append(local, float64(time.Since(t0).Microseconds())/1000.0)
			}
			mu.Lock()
			all = append(all, local...)
			mu.Unlock()
		}()
	}
	wg.Wait()
	if firstErr != nil {
		return latencyStats{}, firstErr
	}
	elapsed := time.Since(start).Seconds()
	sort.Float64s(all)
	return latencyStats{
		RPS: float64(len(all)) / elapsed,
		P50: percentile(all, 0.50),
		P90: percentile(all, 0.90),
		P99: percentile(all, 0.99),
		N:   len(all),
	}, nil
}

func getOK(client *http.Client, url string) error {
	resp, err := client.Get(url)
	if err != nil {
		return err
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("GET %s: status %d", url, resp.StatusCode)
	}
	return nil
}

func multipartBody(field, filename string, content []byte) (*bytes.Reader, string) {
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	fw, _ := mw.CreateFormFile(field, filename)
	_, _ = fw.Write(content)
	_ = mw.Close()
	return bytes.NewReader(buf.Bytes()), mw.FormDataContentType()
}

func upload(client *http.Client, base string, content []byte) (string, error) {
	body, ctype := multipartBody("file", "bench.bin", content)
	resp, err := client.Post(base+"/v1/files", ctype, body)
	if err != nil {
		return "", err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != 201 {
		return "", fmt.Errorf("upload status %d", resp.StatusCode)
	}
	var payload struct {
		FileID string `json:"file_id"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return "", err
	}
	return payload.FileID, nil
}

type residentResult struct {
	IdleRSSKiB     int64
	IdleHWMKiB     int64
	AfterLoadRSS   int64
	AfterLoadHWM   int64
	Download1      latencyStats
	Download16     latencyStats
	List16         latencyStats
	Upload4        latencyStats
	ExecuteReject1 latencyStats // POST /v1/execute with over-limit timeout: full parse+validate path, 422
}

func benchResident(t target, port int) (residentResult, error) {
	var res residentResult
	storage, _ := os.MkdirTemp("", "bench-res-"+t.name+"-*")
	defer func() { _ = os.RemoveAll(storage) }()

	p, err := spawn(t, port, storage)
	if err != nil {
		return res, err
	}
	defer p.kill()

	client := newClient()
	if _, err := p.waitReady(client, 60*time.Second); err != nil {
		return res, err
	}

	time.Sleep(3 * time.Second)
	res.IdleRSSKiB, res.IdleHWMKiB = procMem(p.cmd.Process.Pid)

	content := bytes.Repeat([]byte("x"), 100*1024) // 100 KiB
	fileID, err := upload(client, p.base, content)
	if err != nil {
		return res, err
	}
	dlURL := p.base + "/v1/files/" + fileID
	listURL := p.base + "/v1/files"

	warm, err := runLoad(client, 4, 2*time.Second, func() error { return getOK(client, dlURL) })
	_ = warm
	if err != nil {
		return res, err
	}

	if res.Download1, err = runLoad(client, 1, 5*time.Second, func() error {
		return getOK(client, dlURL)
	}); err != nil {
		return res, err
	}
	if res.Download16, err = runLoad(client, 16, 5*time.Second, func() error {
		return getOK(client, dlURL)
	}); err != nil {
		return res, err
	}
	if res.List16, err = runLoad(client, 16, 5*time.Second, func() error {
		return getOK(client, listURL)
	}); err != nil {
		return res, err
	}

	// Full request-validation path without an executor: timeout_ms above the
	// configured maximum returns 422 on both implementations.
	execBody := []byte(`{"code": "print('hi')", "timeout_ms": 99999999}`)
	if res.ExecuteReject1, err = runLoad(client, 1, 3*time.Second, func() error {
		resp, err := client.Post(
			p.base+"/v1/execute", "application/json", bytes.NewReader(execBody),
		)
		if err != nil {
			return err
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		_ = resp.Body.Close()
		if resp.StatusCode != 422 {
			return fmt.Errorf("execute status %d", resp.StatusCode)
		}
		return nil
	}); err != nil {
		return res, err
	}

	if res.Upload4, err = runLoad(client, 4, 3*time.Second, func() error {
		_, err := upload(client, p.base, content)
		return err
	}); err != nil {
		return res, err
	}

	time.Sleep(1 * time.Second)
	res.AfterLoadRSS, res.AfterLoadHWM = procMem(p.cmd.Process.Pid)
	return res, nil
}

func stats(v []float64) (min, med, max float64) {
	if len(v) == 0 {
		return
	}
	s := append([]float64{}, v...)
	sort.Float64s(s)
	return s[0], s[len(s)/2], s[len(s)-1]
}

func main() {
	fmt.Printf("host: %d CPUs\n\n", runtime.NumCPU())
	for i, t := range targets {
		fmt.Printf("=== %s ===\n", t.name)

		su, err := benchStartup(t, 11, 19000+i*100)
		if err != nil {
			fmt.Printf("startup bench failed: %v\n", err)
			os.Exit(1)
		}
		minMs, medMs, maxMs := stats(su.ReadyMs)
		minC, medC, maxC := stats(su.CPUSec)
		fmt.Printf("startup_to_ready_ms: min=%.1f median=%.1f max=%.1f (n=%d)\n",
			minMs, medMs, maxMs, len(su.ReadyMs))
		fmt.Printf("startup_cpu_sec:     min=%.2f median=%.2f max=%.2f\n", minC, medC, maxC)

		res, err := benchResident(t, 19900+i)
		if err != nil {
			fmt.Printf("resident bench failed: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("idle_rss_mib:        %.1f (hwm %.1f)\n",
			float64(res.IdleRSSKiB)/1024, float64(res.IdleHWMKiB)/1024)
		fmt.Printf("after_load_rss_mib:  %.1f (hwm %.1f)\n",
			float64(res.AfterLoadRSS)/1024, float64(res.AfterLoadHWM)/1024)
		pr := func(name string, s latencyStats) {
			fmt.Printf("%-22s rps=%8.0f p50=%6.2fms p90=%6.2fms p99=%6.2fms (n=%d)\n",
				name, s.RPS, s.P50, s.P90, s.P99, s.N)
		}
		pr("download_100k_c1:", res.Download1)
		pr("download_100k_c16:", res.Download16)
		pr("list_files_c16:", res.List16)
		pr("execute_validate_c1:", res.ExecuteReject1)
		pr("upload_100k_c4:", res.Upload4)
		fmt.Println()
	}
}
