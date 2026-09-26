// Tests for imageref.Normalize, ported from
// tests/integration_tests/test_image_ref.py.
//
// These exercise every shape of Docker image reference we expect operators to
// set in PYTHON_EXECUTOR_DOCKER_IMAGE: bare repositories (":latest" must be
// appended), tagged references and digests (must be unchanged), and
// registry-with-port variants where a ":" before the last "/" is a port
// separator, not a tag separator.
package imageref

import "testing"

func TestNormalize(t *testing.T) {
	digest := "onyxdotapp/python-executor-sci" +
		"@sha256:462c2fb0ed8998b75418d7a3f9d7fb75f61ce4c4605a1468436d5af09b9971b8"

	cases := []struct {
		name string
		in   string
		want string
	}{
		{"bare repo gets latest", "python-executor-sci", "python-executor-sci:latest"},
		{
			"namespaced bare repo gets latest",
			"onyxdotapp/python-executor-sci",
			"onyxdotapp/python-executor-sci:latest",
		},
		{"registry bare repo gets latest", "ghcr.io/owner/repo", "ghcr.io/owner/repo:latest"},
		{"tagged reference unchanged", "python-executor-sci:0.4.0", "python-executor-sci:0.4.0"},
		{
			"namespaced tagged unchanged",
			"onyxdotapp/python-executor-sci:0.4.0",
			"onyxdotapp/python-executor-sci:0.4.0",
		},
		{"registry tagged unchanged", "ghcr.io/owner/repo:v1", "ghcr.io/owner/repo:v1"},
		{"digest reference unchanged", digest, digest},
		{
			"registry port is not a tag",
			"registry.io:443/owner/repo",
			"registry.io:443/owner/repo:latest",
		},
		{
			"registry port with tag unchanged",
			"registry.io:443/owner/repo:v2",
			"registry.io:443/owner/repo:v2",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Normalize(tc.in); got != tc.want {
				t.Errorf("Normalize(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}
