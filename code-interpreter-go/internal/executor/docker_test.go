package executor

import (
	"archive/tar"
	"bytes"
	"log/slog"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/config"
)

func newTestDockerExecutor(t *testing.T, runArgs string) *DockerExecutor {
	t.Helper()
	extra, err := parseDockerRunArgs(runArgs)
	if err != nil {
		t.Fatalf("parseDockerRunArgs(%q): %v", runArgs, err)
	}
	return &DockerExecutor{
		image:   "onyxdotapp/python-executor-sci",
		network: "none",
		extra:   extra,
		log:     slog.Default(),
	}
}

func TestContainerSpecsBasics(t *testing.T) {
	d := newTestDockerExecutor(t, "")
	cpu, mem := 5, 256
	cfg, hostCfg := d.containerSpecs(&cpu, &mem, 12, nil)

	if !slices.Equal(cfg.Cmd, []string{"sleep", "12"}) {
		t.Errorf("cmd = %v", cfg.Cmd)
	}
	if cfg.WorkingDir != "/workspace" || cfg.Image != d.image {
		t.Errorf("config = %+v", cfg)
	}
	if !slices.Contains(cfg.Env, "PYTHONUNBUFFERED=1") ||
		!slices.Contains(cfg.Env, "MPLCONFIGDIR=/tmp/matplotlib") {
		t.Errorf("env = %v", cfg.Env)
	}

	if !hostCfg.AutoRemove {
		t.Error("AutoRemove not set")
	}
	if string(hostCfg.NetworkMode) != "none" {
		t.Errorf("network = %s", hostCfg.NetworkMode)
	}
	if string(hostCfg.CgroupnsMode) != "host" {
		t.Errorf("cgroupns = %s", hostCfg.CgroupnsMode)
	}
	if !slices.Contains(hostCfg.SecurityOpt, "no-new-privileges") {
		t.Errorf("security opts = %v", hostCfg.SecurityOpt)
	}
	if !slices.Contains([]string(hostCfg.CapDrop), "ALL") ||
		!slices.Contains([]string(hostCfg.CapAdd), "CHOWN") {
		t.Errorf("caps = drop %v add %v", hostCfg.CapDrop, hostCfg.CapAdd)
	}
	if hostCfg.Tmpfs["/workspace"] != "rw,uid=65532,gid=65532" ||
		hostCfg.Tmpfs["/tmp"] != "rw,size=64m" {
		t.Errorf("tmpfs = %v", hostCfg.Tmpfs)
	}
	if hostCfg.Resources.PidsLimit == nil || *hostCfg.Resources.PidsLimit != 64 {
		t.Errorf("pids limit = %v", hostCfg.Resources.PidsLimit)
	}
	if hostCfg.Resources.Memory != 256*1024*1024 ||
		hostCfg.Resources.MemorySwap != 256*1024*1024 {
		t.Errorf("memory = %d swap %d", hostCfg.Resources.Memory, hostCfg.Resources.MemorySwap)
	}

	var foundCPU bool
	for _, u := range hostCfg.Resources.Ulimits {
		if u.Name == "cpu" && u.Soft == 5 && u.Hard == 5 {
			foundCPU = true
		}
	}
	if !foundCPU {
		t.Errorf("cpu ulimit missing: %v", hostCfg.Resources.Ulimits)
	}
}

func TestContainerSpecsClampsLimits(t *testing.T) {
	d := newTestDockerExecutor(t, "")
	cpu, mem := 0, 4
	_, hostCfg := d.containerSpecs(&cpu, &mem, 1, nil)

	if hostCfg.Resources.Memory != 16*1024*1024 {
		t.Errorf("memory not clamped to 16 MiB: %d", hostCfg.Resources.Memory)
	}
	for _, u := range hostCfg.Resources.Ulimits {
		if u.Name == "cpu" && (u.Soft != 1 || u.Hard != 1) {
			t.Errorf("cpu ulimit not clamped: %+v", u)
		}
	}
}

func TestContainerSpecsOmitsLimitsWhenNil(t *testing.T) {
	d := newTestDockerExecutor(t, "")
	_, hostCfg := d.containerSpecs(nil, nil, 1, nil)

	if hostCfg.Resources.Memory != 0 {
		t.Errorf("memory set despite nil: %d", hostCfg.Resources.Memory)
	}
	for _, u := range hostCfg.Resources.Ulimits {
		if u.Name == "cpu" {
			t.Errorf("cpu ulimit set despite nil: %+v", u)
		}
	}
}

func TestContainerSpecsLabels(t *testing.T) {
	d := newTestDockerExecutor(t, "")
	cfg, _ := d.containerSpecs(nil, nil, 60, map[string]string{
		"app":       SessionAppLabel,
		"component": SessionComponentLabel,
	})
	if cfg.Labels["app"] != "code-interpreter" || cfg.Labels["component"] != "session" {
		t.Errorf("labels = %v", cfg.Labels)
	}
}

func TestParseDockerRunArgs(t *testing.T) {
	opts, err := parseDockerRunArgs(
		`--label team=infra --env "FOO=bar baz" --add-host example.com:10.0.0.1 ` +
			`--dns 1.1.1.1 --ulimit nofile=1024:2048`,
	)
	if err != nil {
		t.Fatal(err)
	}
	if opts.labels["team"] != "infra" {
		t.Errorf("labels = %v", opts.labels)
	}
	if !slices.Contains(opts.env, "FOO=bar baz") {
		t.Errorf("env = %v", opts.env)
	}
	if !slices.Contains(opts.extraHosts, "example.com:10.0.0.1") {
		t.Errorf("extraHosts = %v", opts.extraHosts)
	}
	if !slices.Contains(opts.dns, "1.1.1.1") {
		t.Errorf("dns = %v", opts.dns)
	}
	if len(opts.ulimits) != 1 || opts.ulimits[0].Name != "nofile" ||
		opts.ulimits[0].Soft != 1024 || opts.ulimits[0].Hard != 2048 {
		t.Errorf("ulimits = %+v", opts.ulimits)
	}
}

func TestParseDockerRunArgsEqualsForm(t *testing.T) {
	opts, err := parseDockerRunArgs(`--env=FOO=bar --label=a=b`)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(opts.env, "FOO=bar") || opts.labels["a"] != "b" {
		t.Errorf("opts = %+v", opts)
	}
}

func TestParseDockerRunArgsRejectsUnsupported(t *testing.T) {
	for _, raw := range []string{
		"--privileged true",
		"--network host",
		"--volume /:/host",
		"--env",
	} {
		if _, err := parseDockerRunArgs(raw); err == nil {
			t.Errorf("parseDockerRunArgs(%q) expected error", raw)
		}
	}
}

func TestParseDockerRunArgsEmpty(t *testing.T) {
	opts, err := parseDockerRunArgs("")
	if err != nil {
		t.Fatal(err)
	}
	if len(opts.env) != 0 || len(opts.labels) != 0 {
		t.Errorf("opts = %+v", opts)
	}
}

func TestParseWorkspaceSnapshotFromInContainerTar(t *testing.T) {
	// Simulate the archive produced by `tar -c -C /workspace .` inside the
	// container: "./"-rooted entries.
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, hdr := range []*tar.Header{
		{Name: "./", Typeflag: tar.TypeDir, Mode: 0o755},
		{Name: "./out", Typeflag: tar.TypeDir, Mode: 0o755},
		{Name: "./out/result.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 2},
	} {
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatal(err)
		}
		if hdr.Typeflag == tar.TypeReg {
			if _, err := tw.Write([]byte("42")); err != nil {
				t.Fatal(err)
			}
		}
	}
	_ = tw.Close()

	entries, err := ParseWorkspaceSnapshot(buf.Bytes())
	if err != nil {
		t.Fatal(err)
	}
	byPath := map[string]WorkspaceEntry{}
	for _, e := range entries {
		byPath[e.Path] = e
	}
	if len(entries) != 2 {
		t.Fatalf("entries = %+v", entries)
	}
	if e, ok := byPath["out"]; !ok || e.Kind != EntryKindDirectory {
		t.Errorf("out dir entry = %+v", e)
	}
	if e, ok := byPath["out/result.txt"]; !ok || string(e.Content) != "42" {
		t.Errorf("out/result.txt = %+v", e)
	}
}

// TestDockerExecutorEndToEnd exercises a real docker daemon. It is skipped
// unless docker (and the executor image) are available, mirroring the Python
// e2e tests that require a running environment.
func TestDockerExecutorEndToEnd(t *testing.T) {
	if os.Getenv("CODE_INTERPRETER_GO_E2E") == "" {
		t.Skip("set CODE_INTERPRETER_GO_E2E=1 to run docker end-to-end tests")
	}

	cfg := config.Load()
	d, err := NewDockerExecutor(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if health := d.CheckHealth(); health.Status != "ok" {
		t.Skipf("docker backend not healthy: %s", health.Message)
	}

	stdin := "fed via stdin"
	result, err := d.ExecutePython(ExecOptions{
		Code:                "import sys\nprint('hello from go port')\nprint(sys.stdin.read())",
		Stdin:               &stdin,
		TimeoutMs:           30_000,
		MaxOutputBytes:      1_000_000,
		LastLineInteractive: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(result.Stdout, "hello from go port") ||
		!strings.Contains(result.Stdout, "fed via stdin") {
		t.Errorf("stdout = %q", result.Stdout)
	}
	if result.ExitCode == nil || *result.ExitCode != 0 {
		t.Errorf("exit code = %v", result.ExitCode)
	}
}
