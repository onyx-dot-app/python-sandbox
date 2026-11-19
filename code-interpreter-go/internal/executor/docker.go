package executor

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
	"github.com/docker/docker/errdefs"
	"github.com/docker/docker/pkg/stdcopy"
	units "github.com/docker/go-units"
	"github.com/google/shlex"
	"github.com/google/uuid"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"

	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/config"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/imageref"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/logging"
)

const executorUser = "65532:65532"

// runArgsOptions is the parsed, supported subset of
// PYTHON_EXECUTOR_DOCKER_RUN_ARGS. The Python service passed the raw string
// through to the `docker run` CLI; the Engine API takes structured fields, so
// only these documented flags are accepted.
type runArgsOptions struct {
	labels     map[string]string
	env        []string
	extraHosts []string
	dns        []string
	ulimits    []*units.Ulimit
}

// parseDockerRunArgs parses PYTHON_EXECUTOR_DOCKER_RUN_ARGS. Supported flags:
// --label, --env/-e, --add-host, --dns, --ulimit (each as "--flag value" or
// "--flag=value"). Any other flag is rejected at startup so a silently
// dropped hardening or connectivity flag cannot go unnoticed.
func parseDockerRunArgs(raw string) (runArgsOptions, error) {
	opts := runArgsOptions{labels: map[string]string{}}
	if raw == "" {
		return opts, nil
	}

	tokens, err := shlex.Split(raw)
	if err != nil {
		return opts, fmt.Errorf("invalid PYTHON_EXECUTOR_DOCKER_RUN_ARGS: %w", err)
	}

	for i := 0; i < len(tokens); i++ {
		flag := tokens[i]
		var value string
		if name, inline, ok := strings.Cut(flag, "="); ok {
			flag = name
			value = inline
		} else {
			i++
			if i >= len(tokens) {
				return opts, fmt.Errorf(
					"invalid PYTHON_EXECUTOR_DOCKER_RUN_ARGS: flag %s is missing a value", flag,
				)
			}
			value = tokens[i]
		}

		switch flag {
		case "--label", "-l":
			key, labelValue, _ := strings.Cut(value, "=")
			opts.labels[key] = labelValue
		case "--env", "-e":
			opts.env = append(opts.env, value)
		case "--add-host":
			opts.extraHosts = append(opts.extraHosts, value)
		case "--dns":
			opts.dns = append(opts.dns, value)
		case "--ulimit":
			ulimit, err := units.ParseUlimit(value)
			if err != nil {
				return opts, fmt.Errorf(
					"invalid PYTHON_EXECUTOR_DOCKER_RUN_ARGS ulimit %q: %w", value, err,
				)
			}
			opts.ulimits = append(opts.ulimits, ulimit)
		default:
			return opts, fmt.Errorf(
				"unsupported flag %q in PYTHON_EXECUTOR_DOCKER_RUN_ARGS: supported flags are "+
					"--label, --env, --add-host, --dns, --ulimit", flag,
			)
		}
	}
	return opts, nil
}

// dockerAPI is the subset of the Docker Engine client used by the executor,
// extracted for testability.
type dockerAPI interface {
	Ping(ctx context.Context) (types.Ping, error)
	ImageInspect(
		ctx context.Context, imageID string, opts ...client.ImageInspectOption,
	) (image.InspectResponse, error)
	ImagePull(
		ctx context.Context, refStr string, options image.PullOptions,
	) (io.ReadCloser, error)
	ContainerCreate(
		ctx context.Context,
		config *container.Config,
		hostConfig *container.HostConfig,
		networkingConfig *network.NetworkingConfig,
		platform *ocispec.Platform,
		containerName string,
	) (container.CreateResponse, error)
	ContainerStart(ctx context.Context, containerID string, options container.StartOptions) error
	ContainerKill(ctx context.Context, containerID, signal string) error
	ContainerRemove(ctx context.Context, containerID string, options container.RemoveOptions) error
	ContainerList(
		ctx context.Context, options container.ListOptions,
	) ([]container.Summary, error)
	ContainerExecCreate(
		ctx context.Context, containerID string, options container.ExecOptions,
	) (container.ExecCreateResponse, error)
	ContainerExecAttach(
		ctx context.Context, execID string, config container.ExecAttachOptions,
	) (types.HijackedResponse, error)
	ContainerExecStart(
		ctx context.Context, execID string, config container.ExecStartOptions,
	) error
	ContainerExecInspect(ctx context.Context, execID string) (container.ExecInspect, error)
}

// DockerExecutor runs code inside ephemeral Docker containers with no network
// access, talking directly to the Docker Engine API (no docker CLI binary
// required). It is the Go port of app/services/executor_docker.py.
type DockerExecutor struct {
	cli     dockerAPI
	image   string
	network string
	extra   runArgsOptions
	log     *slog.Logger
}

// NewDockerExecutor builds an Engine API client from the environment
// (DOCKER_HOST etc., defaulting to the local socket) and validates the
// configured run-args subset.
func NewDockerExecutor(cfg config.Config) (*DockerExecutor, error) {
	extra, err := parseDockerRunArgs(cfg.DockerRunArgs)
	if err != nil {
		return nil, err
	}
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return nil, fmt.Errorf("failed to create Docker client: %w", err)
	}
	return &DockerExecutor{
		cli:     cli,
		image:   cfg.DockerImage,
		network: cfg.DockerNetwork,
		extra:   extra,
		log:     logging.Named("executor.docker"),
	}, nil
}

// CheckHealth verifies the Docker daemon is reachable and the executor image
// is available locally.
func (d *DockerExecutor) CheckHealth() HealthCheck {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := d.cli.Ping(ctx); err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			return HealthCheck{Status: "error", Message: "Docker daemon not responding"}
		}
		return HealthCheck{
			Status:  "error",
			Message: "Docker daemon not reachable: " + err.Error(),
		}
	}

	imageWithTag := imageref.Normalize(d.image)
	imgCtx, cancelImg := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancelImg()
	if _, err := d.cli.ImageInspect(imgCtx, imageWithTag); err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			return HealthCheck{
				Status:  "error",
				Message: "Timeout checking image " + imageWithTag,
			}
		}
		return HealthCheck{
			Status:  "error",
			Message: "Executor image " + imageWithTag + " not available locally",
		}
	}

	return HealthCheck{Status: "ok"}
}

// containerSpecs builds the create-container configuration, the SDK analogue
// of the Python service's `docker run` argument list.
//
// sleepSeconds controls how long the container's idle `sleep` lasts; callers
// must ensure it exceeds their work duration. labels are attached for later
// filtering (e.g. by the session reaper).
func (d *DockerExecutor) containerSpecs(
	cpuTimeLimitSec *int,
	memoryLimitMB *int,
	sleepSeconds int,
	labels map[string]string,
) (*container.Config, *container.HostConfig) {
	mergedLabels := map[string]string{}
	for k, v := range labels {
		mergedLabels[k] = v
	}
	for k, v := range d.extra.labels {
		mergedLabels[k] = v
	}

	env := []string{
		"PYTHONUNBUFFERED=1",
		"PYTHONDONTWRITEBYTECODE=1",
		"PYTHONIOENCODING=utf-8",
		"MPLCONFIGDIR=/tmp/matplotlib",
	}
	env = append(env, d.extra.env...)

	cfg := &container.Config{
		Image:      d.image,
		Cmd:        []string{"sleep", strconv.Itoa(sleepSeconds)},
		WorkingDir: "/workspace",
		Env:        env,
		Labels:     mergedLabels,
	}

	pidsLimit := int64(64)
	resources := container.Resources{
		PidsLimit: &pidsLimit,
		Ulimits:   append([]*units.Ulimit{}, d.extra.ulimits...),
	}
	if cpuTimeLimitSec != nil {
		cpuLimit := int64(max(*cpuTimeLimitSec, 1))
		resources.Ulimits = append(resources.Ulimits, &units.Ulimit{
			Name: "cpu", Soft: cpuLimit, Hard: cpuLimit,
		})
	}
	if memoryLimitMB != nil {
		memoryBytes := int64(max(*memoryLimitMB, 16)) * 1024 * 1024
		resources.Memory = memoryBytes
		resources.MemorySwap = memoryBytes
	}

	// We need CAP_CHOWN to set up the workspace, but drop privileges for
	// execution (exec runs as the unprivileged executor user).
	hostCfg := &container.HostConfig{
		AutoRemove:  true,
		NetworkMode: container.NetworkMode(d.network),
		// Use the host cgroup namespace to avoid cgroup v2 issues in DinD.
		CgroupnsMode: container.CgroupnsModeHost,
		SecurityOpt:  []string{"no-new-privileges"},
		CapDrop:      []string{"ALL"},
		CapAdd:       []string{"CHOWN"},
		Tmpfs: map[string]string{
			"/tmp": "rw,size=64m",
			// Create the workspace as a tmpfs owned by the executor user.
			"/workspace": "rw,uid=65532,gid=65532",
		},
		Resources:  resources,
		ExtraHosts: append([]string{}, d.extra.extraHosts...),
		DNS:        append([]string{}, d.extra.dns...),
	}

	return cfg, hostCfg
}

// startContainer creates and starts an executor container.
func (d *DockerExecutor) startContainer(
	name string,
	cfg *container.Config,
	hostCfg *container.HostConfig,
) error {
	ctx := context.Background()
	if _, err := d.cli.ContainerCreate(ctx, cfg, hostCfg, nil, nil, name); err != nil {
		return fmt.Errorf("Failed to start container: %v", err)
	}
	if err := d.cli.ContainerStart(ctx, name, container.StartOptions{}); err != nil {
		return fmt.Errorf("Failed to start container: %v", err)
	}
	return nil
}

// killContainer force-kills a container, ignoring failures (it may already be
// gone; AutoRemove cleans it up afterwards).
func (d *DockerExecutor) killContainer(name string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = d.cli.ContainerKill(ctx, name, "KILL")
}

// killProcessInContainer best-effort SIGKILLs all processes with the given
// name inside the container (as root, to ensure we can kill it).
func (d *DockerExecutor) killProcessInContainer(containerName, processName string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	resp, err := d.cli.ContainerExecCreate(ctx, containerName, container.ExecOptions{
		Cmd: []string{"pkill", "-9", processName},
	})
	if err != nil {
		return
	}
	_ = d.cli.ContainerExecStart(ctx, resp.ID, container.ExecStartOptions{Detach: true})
}

// execParams describes one exec-in-container invocation.
type execParams struct {
	container string
	cmd       []string
	user      string
	stdin     io.Reader
	stdout    io.Writer
	stderr    io.Writer
	// timeout of 0 means wait indefinitely.
	timeout time.Duration
	// killProcess, when non-empty, is pkill'ed inside the container if the
	// timeout elapses.
	killProcess string
}

// isMissingContainerErr reports whether an Engine API error means the target
// container is gone or stopped — the typed replacement for the Python
// service's stderr-string heuristic.
func isMissingContainerErr(err error) bool {
	return errdefs.IsNotFound(err) || errdefs.IsConflict(err)
}

// execInContainer runs a command via the exec API, streaming demultiplexed
// stdout/stderr into the provided writers. Returns the exit code (nil when
// the run timed out or the daemon could not report one) and whether the
// timeout elapsed.
func (d *DockerExecutor) execInContainer(p execParams) (exitCode *int, timedOut bool, err error) {
	ctx := context.Background()

	createResp, err := d.cli.ContainerExecCreate(ctx, p.container, container.ExecOptions{
		User:         p.user,
		AttachStdin:  p.stdin != nil,
		AttachStdout: true,
		AttachStderr: true,
		Cmd:          p.cmd,
	})
	if err != nil {
		return nil, false, err
	}

	attach, err := d.cli.ContainerExecAttach(ctx, createResp.ID, container.ExecAttachOptions{})
	if err != nil {
		return nil, false, err
	}
	defer attach.Close()

	// Feed stdin from its own goroutine (mirroring communicate()) so a child
	// that never reads cannot deadlock the copy loop, then half-close to
	// signal EOF.
	if p.stdin != nil {
		go func() {
			_, _ = io.Copy(attach.Conn, p.stdin)
			_ = attach.CloseWrite()
		}()
	} else {
		_ = attach.CloseWrite()
	}

	stdout := p.stdout
	if stdout == nil {
		stdout = io.Discard
	}
	stderr := p.stderr
	if stderr == nil {
		stderr = io.Discard
	}

	done := make(chan error, 1)
	go func() {
		_, copyErr := stdcopy.StdCopy(stdout, stderr, attach.Reader)
		done <- copyErr
	}()

	var timer <-chan time.Time
	if p.timeout > 0 {
		t := time.NewTimer(p.timeout)
		defer t.Stop()
		timer = t.C
	}

	select {
	case <-done:
	case <-timer:
		timedOut = true
		if p.killProcess != "" {
			d.killProcessInContainer(p.container, p.killProcess)
		}
		// Closing the attach connection unblocks the copy goroutine; wait for
		// it so the writers are never touched after we return.
		attach.Close()
		<-done
	}

	if timedOut {
		return nil, true, nil
	}

	// The exec process may not be reaped the instant the stream closes; poll
	// briefly until the daemon reports completion.
	inspectDeadline := time.Now().Add(2 * time.Second)
	for {
		inspect, inspectErr := d.cli.ContainerExecInspect(ctx, createResp.ID)
		if inspectErr != nil {
			return nil, false, inspectErr
		}
		if !inspect.Running {
			code := inspect.ExitCode
			return &code, false, nil
		}
		if time.Now().After(inspectDeadline) {
			return nil, false, nil
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// uploadTarToContainer streams a tar archive into the container workspace.
//
// The workspace is a tmpfs mount, which exists only in the container's mount
// namespace — the Engine's CopyToContainer API writes through the rootfs and
// would be invisible there — so extraction runs inside the container.
func (d *DockerExecutor) uploadTarToContainer(containerName string, tarArchive []byte) error {
	var stderr bytes.Buffer
	exitCode, _, err := d.execInContainer(execParams{
		container: containerName,
		cmd:       []string{"tar", "-x", "-C", "/workspace"},
		user:      executorUser,
		stdin:     bytes.NewReader(tarArchive),
		stderr:    &stderr,
	})
	if err != nil {
		return fmt.Errorf("Failed to extract files: %v", err)
	}
	if exitCode == nil || *exitCode != 0 {
		return fmt.Errorf(
			"Failed to extract files: %s", strings.ToValidUTF8(stderr.String(), "�"),
		)
	}
	return nil
}

// extractWorkspaceSnapshot extracts files from the container workspace after
// execution using in-container tar (see uploadTarToContainer for why).
// Failures yield an empty snapshot, never an error.
func (d *DockerExecutor) extractWorkspaceSnapshot(containerName string) []WorkspaceEntry {
	var stdout bytes.Buffer
	exitCode, _, err := d.execInContainer(execParams{
		container: containerName,
		cmd:       []string{"tar", "-c", "--exclude=__main__.py", "-C", "/workspace", "."},
		stdout:    &stdout,
		timeout:   10 * time.Second,
	})
	if err != nil || exitCode == nil || *exitCode != 0 {
		return nil
	}
	entries, err := ParseWorkspaceSnapshot(stdout.Bytes())
	if err != nil {
		return nil
	}
	return entries
}

// prepareContainer creates an ephemeral executor container and stages the
// code + files. The returned cleanup function kills the container and must be
// deferred by the caller.
func (d *DockerExecutor) prepareContainer(opts ExecOptions) (string, func(), error) {
	containerName := "code-exec-" + strings.ReplaceAll(uuid.NewString(), "-", "")

	cfg, hostCfg := d.containerSpecs(
		opts.CPUTimeLimitSec, opts.MemoryLimitMB, opts.TimeoutMs/1000+10, nil,
	)
	if err := d.startContainer(containerName, cfg, hostCfg); err != nil {
		return "", func() {}, err
	}
	cleanup := func() { d.killContainer(containerName) }

	tarArchive, err := CreateTarArchive(&opts.Code, opts.Files, opts.LastLineInteractive, nil)
	if err != nil {
		cleanup()
		return "", func() {}, err
	}
	if err := d.uploadTarToContainer(containerName, tarArchive); err != nil {
		cleanup()
		return "", func() {}, err
	}
	return containerName, cleanup, nil
}

// ExecutePython executes Python code inside an ephemeral Docker container.
func (d *DockerExecutor) ExecutePython(opts ExecOptions) (ExecutionResult, error) {
	containerName, cleanup, err := d.prepareContainer(opts)
	if err != nil {
		return ExecutionResult{}, err
	}
	defer cleanup()

	d.log.Debug("Executing code", "code", opts.Code)

	var stdin io.Reader
	if opts.Stdin != nil {
		stdin = strings.NewReader(*opts.Stdin)
	}
	var stdoutBuf, stderrBuf bytes.Buffer

	start := time.Now()
	exitCode, timedOut, err := d.execInContainer(execParams{
		container:   containerName,
		cmd:         []string{"python", "/workspace/__main__.py"},
		user:        executorUser,
		stdin:       stdin,
		stdout:      &stdoutBuf,
		stderr:      &stderrBuf,
		timeout:     time.Duration(opts.TimeoutMs) * time.Millisecond,
		killProcess: "python",
	})
	if err != nil {
		return ExecutionResult{}, fmt.Errorf("Failed to execute code: %v", err)
	}

	workspaceSnapshot := d.extractWorkspaceSnapshot(containerName)
	durationMs := time.Since(start).Milliseconds()

	stdout := TruncateOutput(stdoutBuf.Bytes(), opts.MaxOutputBytes)
	d.log.Debug("execution stdout", "stdout", stdout)
	stderr := TruncateOutput(stderrBuf.Bytes(), opts.MaxOutputBytes)
	d.log.Debug("execution stderr", "stderr", stderr)

	return ExecutionResult{
		Stdout:     stdout,
		Stderr:     stderr,
		ExitCode:   exitCode,
		TimedOut:   timedOut,
		DurationMs: durationMs,
		Files:      workspaceSnapshot,
	}, nil
}

// ExecutePythonStreaming executes Python code, emitting output chunks as they
// arrive, and returns the final result.
func (d *DockerExecutor) ExecutePythonStreaming(
	opts ExecOptions,
	emit EmitFunc,
) (StreamResult, error) {
	containerName, cleanup, err := d.prepareContainer(opts)
	if err != nil {
		return StreamResult{}, err
	}
	defer cleanup()

	var stdin io.Reader
	if opts.Stdin != nil {
		stdin = strings.NewReader(*opts.Stdin)
	}

	var mu sync.Mutex
	stdoutWriter := &trackerWriter{
		tracker: newStreamTracker("stdout", opts.MaxOutputBytes), emit: emit, mu: &mu,
	}
	stderrWriter := &trackerWriter{
		tracker: newStreamTracker("stderr", opts.MaxOutputBytes), emit: emit, mu: &mu,
	}

	start := time.Now()
	exitCode, timedOut, err := d.execInContainer(execParams{
		container:   containerName,
		cmd:         []string{"python", "/workspace/__main__.py"},
		user:        executorUser,
		stdin:       stdin,
		stdout:      stdoutWriter,
		stderr:      stderrWriter,
		timeout:     time.Duration(opts.TimeoutMs) * time.Millisecond,
		killProcess: "python",
	})
	if err != nil {
		return StreamResult{}, fmt.Errorf("Failed to execute code: %v", err)
	}

	for _, w := range []*trackerWriter{stdoutWriter, stderrWriter} {
		mu.Lock()
		if chunk := w.tracker.flush(); chunk != nil {
			emit(*chunk)
		}
		mu.Unlock()
	}

	workspaceSnapshot := d.extractWorkspaceSnapshot(containerName)
	durationMs := time.Since(start).Milliseconds()

	return StreamResult{
		ExitCode:   exitCode,
		TimedOut:   timedOut,
		DurationMs: durationMs,
		Files:      workspaceSnapshot,
	}, nil
}

// CreateSession starts a long-lived session container whose idle `sleep`
// enforces the TTL even if this process crashes.
func (d *DockerExecutor) CreateSession(
	ttlSeconds int,
	files []StagedFile,
	cpuTimeLimitSec *int,
	memoryLimitMB *int,
) (SessionInfo, error) {
	containerName := SessionNamePrefix + strings.ReplaceAll(uuid.NewString(), "-", "")
	expiresAt := float64(time.Now().UnixNano())/1e9 + float64(ttlSeconds)

	cfg, hostCfg := d.containerSpecs(
		cpuTimeLimitSec, memoryLimitMB, ttlSeconds,
		map[string]string{
			"app":               SessionAppLabel,
			"component":         SessionComponentLabel,
			SessionExpiresAtKey: strconv.FormatFloat(expiresAt, 'f', -1, 64),
		},
	)
	if err := d.startContainer(containerName, cfg, hostCfg); err != nil {
		return SessionInfo{}, fmt.Errorf("Failed to start session container: %v", err)
	}

	if len(files) > 0 {
		tarArchive, err := CreateTarArchive(nil, files, true, nil)
		if err != nil {
			d.killContainer(containerName)
			return SessionInfo{}, err
		}
		if err := d.uploadTarToContainer(containerName, tarArchive); err != nil {
			d.killContainer(containerName)
			return SessionInfo{}, err
		}
	}

	d.log.Info("Created session container",
		"session_id", containerName, "expires_at", expiresAt)
	return SessionInfo{SessionID: containerName, ExpiresAt: expiresAt}, nil
}

// DeleteSession force-removes a session container by ID. Returns false when
// the container does not exist.
func (d *DockerExecutor) DeleteSession(sessionID string) (bool, error) {
	if !strings.HasPrefix(sessionID, SessionNamePrefix) {
		return false, nil
	}
	err := d.cli.ContainerRemove(
		context.Background(), sessionID, container.RemoveOptions{Force: true},
	)
	if err != nil {
		if errdefs.IsNotFound(err) {
			return false, nil
		}
		return false, fmt.Errorf("Failed to delete session %s: %v", sessionID, err)
	}
	return true, nil
}

// ReapExpiredSessions deletes session containers whose expires-at label has
// elapsed. Returns the number reaped.
func (d *DockerExecutor) ReapExpiredSessions() (int, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	containers, err := d.cli.ContainerList(ctx, container.ListOptions{
		All: true,
		Filters: filters.NewArgs(
			filters.Arg("label", "app="+SessionAppLabel),
			filters.Arg("label", "component="+SessionComponentLabel),
		),
	})
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			d.log.Warn("Timed out listing session containers for reap")
		} else {
			d.log.Warn("Failed to list session containers", "error", err.Error())
		}
		return 0, nil
	}

	now := float64(time.Now().UnixNano()) / 1e9
	reaped := 0
	for _, c := range containers {
		name := c.ID
		if len(c.Names) > 0 {
			name = strings.TrimPrefix(c.Names[0], "/")
		}
		expiresStr := c.Labels[SessionExpiresAtKey]
		if name == "" || expiresStr == "" {
			continue
		}
		expiresAt, err := strconv.ParseFloat(expiresStr, 64)
		if err != nil {
			continue
		}
		if expiresAt >= now {
			continue
		}
		rmErr := d.cli.ContainerRemove(
			context.Background(), name, container.RemoveOptions{Force: true},
		)
		if rmErr == nil || errdefs.IsNotFound(rmErr) {
			reaped++
			d.log.Info("Reaped expired session container", "name", name)
		} else {
			d.log.Warn("Failed to reap session container",
				"name", name, "error", rmErr.Error())
		}
	}
	return reaped, nil
}

// ExecuteBashInSession runs a bash command inside an existing session
// container.
//
// The container was created with the "none" network at session-create time
// and that network namespace is what the exec inherits — no additional
// settings are needed (or accepted) for exec.
func (d *DockerExecutor) ExecuteBashInSession(
	sessionID string,
	cmd string,
	timeoutMs int,
	maxOutputBytes int,
) (ExecutionResult, error) {
	if !strings.HasPrefix(sessionID, SessionNamePrefix) {
		return ExecutionResult{}, &SessionNotFoundError{SessionID: sessionID}
	}

	var stdoutBuf, stderrBuf bytes.Buffer
	start := time.Now()

	// Kill bash inside the container on timeout; pkill matches all bash procs
	// in the container — acceptable since the agent runs commands
	// sequentially.
	exitCode, timedOut, err := d.execInContainer(execParams{
		container:   sessionID,
		cmd:         []string{"bash", "-c", cmd},
		user:        executorUser,
		stdout:      &stdoutBuf,
		stderr:      &stderrBuf,
		timeout:     time.Duration(timeoutMs) * time.Millisecond,
		killProcess: "bash",
	})
	if err != nil {
		if isMissingContainerErr(err) {
			return ExecutionResult{}, &SessionNotFoundError{SessionID: sessionID}
		}
		return ExecutionResult{}, fmt.Errorf("Failed to execute bash command: %v", err)
	}

	durationMs := time.Since(start).Milliseconds()
	return ExecutionResult{
		Stdout:     TruncateOutput(stdoutBuf.Bytes(), maxOutputBytes),
		Stderr:     TruncateOutput(stderrBuf.Bytes(), maxOutputBytes),
		ExitCode:   exitCode,
		TimedOut:   timedOut,
		DurationMs: durationMs,
	}, nil
}

// EnsureDockerImageAvailable checks that the executor image exists locally
// and attempts to pull it if not. It runs during application startup so the
// image is ready before the service accepts requests. An unreachable daemon
// is a warning, not an error, so Kubernetes-only images can share the binary.
func EnsureDockerImageAvailable(cfg config.Config) error {
	log := logging.Named("main")

	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return fmt.Errorf("failed to create Docker client: %w", err)
	}
	defer func() { _ = cli.Close() }()

	pingCtx, cancelPing := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancelPing()
	if _, err := cli.Ping(pingCtx); err != nil {
		log.Warn("Docker daemon not reachable, skipping image check")
		return nil
	}

	imageWithTag := imageref.Normalize(cfg.DockerImage)

	log.Info("Checking for Docker image: " + imageWithTag)
	checkCtx, cancelCheck := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancelCheck()
	if _, err := cli.ImageInspect(checkCtx, imageWithTag); err == nil {
		log.Info("Docker image " + imageWithTag + " is already available locally")
		return nil
	}

	log.Info("Docker image " + imageWithTag + " not found locally, attempting to pull...")
	pullCtx, cancelPull := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancelPull()
	reader, err := cli.ImagePull(pullCtx, imageWithTag, image.PullOptions{})
	if err == nil {
		_, err = io.Copy(io.Discard, reader)
		_ = reader.Close()
	}
	if pullCtx.Err() == context.DeadlineExceeded {
		return fmt.Errorf(
			"Timeout while pulling Docker image %s. "+
				"This may indicate network issues or the image is very large.",
			imageWithTag,
		)
	}
	if err != nil {
		log.Error("Failed to pull " + imageWithTag + ": " + err.Error())
		return fmt.Errorf(
			"Docker executor image %s is not available locally "+
				"and could not be pulled. Error: %v",
			imageWithTag, err,
		)
	}
	log.Info("Successfully pulled " + imageWithTag)
	return nil
}
