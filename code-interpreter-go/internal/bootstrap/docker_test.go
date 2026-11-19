package bootstrap

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDecideMode(t *testing.T) {
	cases := []struct {
		name           string
		backend        string
		dockerHostSet  bool
		socketExists   bool
		varRunWritable bool
		want           Mode
	}{
		{"kubernetes backend skips docker", "kubernetes", false, false, false, ModeSkipKubernetes},
		{"kubernetes wins over socket", "kubernetes", true, true, true, ModeSkipKubernetes},
		{"docker host set uses external daemon", "docker", true, false, true, ModeExternalDaemon},
		{"mounted socket is DooD", "docker", false, true, true, ModeSocketFound},
		{"privileged without socket is DinD", "docker", false, false, true, ModeStartDaemon},
		{"no socket no privileges warns", "docker", false, false, false, ModeNoDocker},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := DecideMode(tc.backend, tc.dockerHostSet, tc.socketExists, tc.varRunWritable)
			if got != tc.want {
				t.Errorf("DecideMode = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestSocketExists(t *testing.T) {
	if socketExists(filepath.Join(t.TempDir(), "nope.sock")) {
		t.Error("nonexistent path reported as socket")
	}

	// A regular file is not a socket.
	regular := filepath.Join(t.TempDir(), "regular")
	if err := os.WriteFile(regular, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if socketExists(regular) {
		t.Error("regular file reported as socket")
	}
}
