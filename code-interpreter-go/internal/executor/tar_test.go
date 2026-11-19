package executor

import (
	"bytes"
	"errors"
	"strings"
	"testing"
)

func TestValidateRelativePath(t *testing.T) {
	valid := []struct {
		in   string
		want string
	}{
		{"data.csv", "data.csv"},
		{"dir/data.csv", "dir/data.csv"},
		{"./data.csv", "data.csv"},
		{"a//b", "a/b"},
		{"a/./b", "a/b"},
	}
	for _, tc := range valid {
		got, err := ValidateRelativePath(tc.in)
		if err != nil {
			t.Errorf("ValidateRelativePath(%q) unexpected error: %v", tc.in, err)
			continue
		}
		if got != tc.want {
			t.Errorf("ValidateRelativePath(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}

	invalid := []string{"/abs/path.txt", "../escape.txt", "a/../b", "", ".", "./"}
	for _, in := range invalid {
		if _, err := ValidateRelativePath(in); err == nil {
			t.Errorf("ValidateRelativePath(%q) expected error, got nil", in)
		} else {
			var valErr *ValidationError
			if !errors.As(err, &valErr) {
				t.Errorf("ValidateRelativePath(%q) error type = %T, want *ValidationError", in, err)
			}
		}
	}
}

func TestCreateTarArchiveWithCodeAndFiles(t *testing.T) {
	code := "print('hi')"
	archive, err := CreateTarArchive(
		&code,
		[]StagedFile{
			{Path: "data/input.csv", Content: []byte("a,b\n1,2\n")},
			{Path: "top.txt", Content: []byte("x")},
		},
		false,
		nil,
	)
	if err != nil {
		t.Fatalf("CreateTarArchive: %v", err)
	}

	entries, err := ParseWorkspaceSnapshot(archive)
	if err != nil {
		t.Fatalf("ParseWorkspaceSnapshot: %v", err)
	}

	byPath := map[string]WorkspaceEntry{}
	for _, e := range entries {
		byPath[e.Path] = e
	}

	main, ok := byPath["__main__.py"]
	if !ok {
		t.Fatal("archive missing __main__.py")
	}
	if string(main.Content) != code {
		t.Errorf("__main__.py content = %q, want %q (interactive wrapping disabled)",
			main.Content, code)
	}
	if dir, ok := byPath["data"]; !ok || dir.Kind != EntryKindDirectory {
		t.Errorf("expected directory entry for 'data', got %+v", dir)
	}
	if f, ok := byPath["data/input.csv"]; !ok || !bytes.Equal(f.Content, []byte("a,b\n1,2\n")) {
		t.Errorf("data/input.csv content mismatch: %+v", f)
	}
	if _, ok := byPath["top.txt"]; !ok {
		t.Error("archive missing top.txt")
	}
}

func TestCreateTarArchiveWrapsInteractiveCode(t *testing.T) {
	code := "1 + 1"
	archive, err := CreateTarArchive(&code, nil, true, nil)
	if err != nil {
		t.Fatalf("CreateTarArchive: %v", err)
	}
	entries, err := ParseWorkspaceSnapshot(archive)
	if err != nil {
		t.Fatalf("ParseWorkspaceSnapshot: %v", err)
	}
	if len(entries) != 1 {
		t.Fatalf("expected 1 entry, got %d", len(entries))
	}
	content := string(entries[0].Content)
	if !strings.Contains(content, "ast.parse") || !strings.Contains(content, "1 + 1") {
		t.Errorf("wrapped code missing expected fragments:\n%s", content)
	}
}

func TestCreateTarArchiveRejectsReservedEntrypoint(t *testing.T) {
	code := "print('hi')"
	_, err := CreateTarArchive(
		&code, []StagedFile{{Path: "__main__.py", Content: []byte("evil")}}, true, nil,
	)
	var valErr *ValidationError
	if !errors.As(err, &valErr) {
		t.Fatalf("expected *ValidationError for reserved __main__.py, got %v", err)
	}

	// Without code staged, __main__.py is an ordinary path.
	if _, err := CreateTarArchive(
		nil, []StagedFile{{Path: "__main__.py", Content: []byte("ok")}}, true, nil,
	); err != nil {
		t.Fatalf("expected __main__.py to be allowed without code, got %v", err)
	}
}

func TestParseWorkspaceSnapshotStripsDotSlash(t *testing.T) {
	archive, err := CreateTarArchive(
		nil, []StagedFile{{Path: "./out/result.txt", Content: []byte("42")}}, true, nil,
	)
	if err != nil {
		t.Fatalf("CreateTarArchive: %v", err)
	}
	entries, err := ParseWorkspaceSnapshot(archive)
	if err != nil {
		t.Fatalf("ParseWorkspaceSnapshot: %v", err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Path, "./") || strings.HasPrefix(e.Path, "/") {
			t.Errorf("entry path not cleaned: %q", e.Path)
		}
	}
}

func TestWrapLastLineInteractiveEscapesQuotes(t *testing.T) {
	wrapped := WrapLastLineInteractive(`print('it\'s')`)
	if !strings.Contains(wrapped, `\'`) {
		t.Error("expected single quotes to be escaped in wrapped code")
	}
	if !strings.Contains(wrapped, "compile(interactive, '<stdin>', 'single')") {
		t.Error("wrapped code missing 'single' compile mode")
	}
}
