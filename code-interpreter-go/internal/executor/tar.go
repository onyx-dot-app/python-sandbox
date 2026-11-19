package executor

import (
	"archive/tar"
	"bytes"
	"errors"
	"io"
	"strings"
)

// EntrypointFileName is the reserved name for the staged user program.
const EntrypointFileName = "__main__.py"

// ValidateRelativePath sanitizes a workspace-relative path: it must not be
// absolute, must not contain "..", and must be non-empty after removing "."
// and empty segments. Returns the cleaned path joined with "/".
func ValidateRelativePath(pathStr string) (string, error) {
	if strings.HasPrefix(pathStr, "/") {
		return "", &ValidationError{Message: "File paths must be relative."}
	}

	var sanitized []string
	for _, part := range strings.Split(pathStr, "/") {
		if part == "" || part == "." {
			continue
		}
		if part == ".." {
			return "", &ValidationError{Message: "File paths must not contain '..'."}
		}
		sanitized = append(sanitized, part)
	}

	if len(sanitized) == 0 {
		return "", &ValidationError{Message: "File path must not be empty."}
	}

	return strings.Join(sanitized, "/"), nil
}

// TarOwner optionally assigns a uid/gid to archive entries (used by the
// Kubernetes backend, whose extraction runs without a chown-capable tar).
type TarOwner struct {
	UID int
	GID int
}

// CreateTarArchive builds a tar archive optionally containing an entrypoint
// and staged files.
//
// If code is non-nil it is written as __main__.py at the archive root; when
// lastLineInteractive is also true the code is wrapped so the last line
// prints its value if it is a bare expression. Parent directories of staged
// files are materialized as explicit directory entries.
func CreateTarArchive(
	code *string,
	files []StagedFile,
	lastLineInteractive bool,
	owner *TarOwner,
) ([]byte, error) {
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)

	applyOwner := func(h *tar.Header) {
		if owner != nil {
			h.Uid = owner.UID
			h.Gid = owner.GID
		}
	}

	if code != nil {
		codeToExecute := *code
		if lastLineInteractive {
			codeToExecute = WrapLastLineInteractive(codeToExecute)
		}
		hdr := &tar.Header{
			Name: EntrypointFileName,
			Mode: 0o644,
			Size: int64(len(codeToExecute)),
		}
		applyOwner(hdr)
		if err := tw.WriteHeader(hdr); err != nil {
			return nil, err
		}
		if _, err := tw.Write([]byte(codeToExecute)); err != nil {
			return nil, err
		}
	}

	createdDirs := map[string]bool{}
	for _, file := range files {
		validated, err := ValidateRelativePath(file.Path)
		if err != nil {
			return nil, err
		}
		if code != nil && validated == EntrypointFileName {
			return nil, &ValidationError{
				Message: "File path '__main__.py' is reserved for the execution entrypoint.",
			}
		}

		parts := strings.Split(validated, "/")
		for i := 1; i < len(parts); i++ {
			dirPath := strings.Join(parts[:i], "/")
			if createdDirs[dirPath] {
				continue
			}
			hdr := &tar.Header{
				Name:     dirPath + "/",
				Typeflag: tar.TypeDir,
				Mode:     0o755,
			}
			applyOwner(hdr)
			if err := tw.WriteHeader(hdr); err != nil {
				return nil, err
			}
			createdDirs[dirPath] = true
		}

		hdr := &tar.Header{
			Name: validated,
			Mode: 0o644,
			Size: int64(len(file.Content)),
		}
		applyOwner(hdr)
		if err := tw.WriteHeader(hdr); err != nil {
			return nil, err
		}
		if _, err := tw.Write(file.Content); err != nil {
			return nil, err
		}
	}

	if err := tw.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// ParseWorkspaceSnapshot reads a tar archive produced by `tar -c -C
// /workspace .` and returns the workspace entries it contains. The root "."
// entry is skipped and leading "./" prefixes are removed.
func ParseWorkspaceSnapshot(tarData []byte) ([]WorkspaceEntry, error) {
	tr := tar.NewReader(bytes.NewReader(tarData))
	var entries []WorkspaceEntry

	for {
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, err
		}

		if hdr.Name == "." || hdr.Name == "./" {
			continue
		}
		cleanPath := strings.TrimLeft(hdr.Name, "./")
		if cleanPath == "" {
			continue
		}
		cleanPath = strings.TrimSuffix(cleanPath, "/")

		switch hdr.Typeflag {
		case tar.TypeDir:
			entries = append(entries, WorkspaceEntry{
				Path: cleanPath,
				Kind: EntryKindDirectory,
			})
		case tar.TypeReg:
			content, err := io.ReadAll(tr)
			if err != nil {
				return nil, err
			}
			entries = append(entries, WorkspaceEntry{
				Path:    cleanPath,
				Kind:    EntryKindFile,
				Content: content,
			})
		}
	}

	return entries, nil
}
