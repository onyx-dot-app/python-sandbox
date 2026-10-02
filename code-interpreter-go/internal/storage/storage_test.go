package storage

import (
	"bytes"
	"errors"
	"testing"
	"time"
)

func newTestStorage(t *testing.T) *FileStorageService {
	t.Helper()
	s, err := NewFileStorageService(t.TempDir())
	if err != nil {
		t.Fatalf("NewFileStorageService: %v", err)
	}
	return s
}

func TestSaveAndGetFile(t *testing.T) {
	s := newTestStorage(t)
	content := []byte("hello world")

	fileID, err := s.SaveFile(content, "greeting.txt")
	if err != nil {
		t.Fatalf("SaveFile: %v", err)
	}

	got, metadata, err := s.GetFile(fileID)
	if err != nil {
		t.Fatalf("GetFile: %v", err)
	}
	if !bytes.Equal(got, content) {
		t.Errorf("content = %q, want %q", got, content)
	}
	if metadata.Filename != "greeting.txt" {
		t.Errorf("filename = %q, want greeting.txt", metadata.Filename)
	}
	if metadata.SizeBytes != int64(len(content)) {
		t.Errorf("size = %d, want %d", metadata.SizeBytes, len(content))
	}
	if metadata.UploadTime <= 0 {
		t.Errorf("upload_time = %f, want > 0", metadata.UploadTime)
	}
}

func TestSaveBinaryFileIntegrity(t *testing.T) {
	s := newTestStorage(t)
	content := make([]byte, 4096)
	for i := range content {
		content[i] = byte(i % 251)
	}

	fileID, err := s.SaveFile(content, "blob.bin")
	if err != nil {
		t.Fatalf("SaveFile: %v", err)
	}
	got, _, err := s.GetFile(fileID)
	if err != nil {
		t.Fatalf("GetFile: %v", err)
	}
	if !bytes.Equal(got, content) {
		t.Error("binary content corrupted on round-trip")
	}
}

func TestGetFileNotFound(t *testing.T) {
	s := newTestStorage(t)
	_, _, err := s.GetFile("nonexistent-id")
	if !errors.Is(err, ErrFileNotFound) {
		t.Errorf("err = %v, want ErrFileNotFound", err)
	}
}

func TestDeleteFile(t *testing.T) {
	s := newTestStorage(t)
	fileID, err := s.SaveFile([]byte("x"), "f.txt")
	if err != nil {
		t.Fatalf("SaveFile: %v", err)
	}

	deleted, err := s.DeleteFile(fileID)
	if err != nil || !deleted {
		t.Fatalf("DeleteFile = (%v, %v), want (true, nil)", deleted, err)
	}
	if _, _, err := s.GetFile(fileID); !errors.Is(err, ErrFileNotFound) {
		t.Error("file still readable after delete")
	}

	deleted, err = s.DeleteFile(fileID)
	if err != nil || deleted {
		t.Errorf("second DeleteFile = (%v, %v), want (false, nil)", deleted, err)
	}
}

func TestListFiles(t *testing.T) {
	s := newTestStorage(t)
	if files, err := s.ListFiles(); err != nil || len(files) != 0 {
		t.Fatalf("empty ListFiles = (%v, %v)", files, err)
	}

	id1, _ := s.SaveFile([]byte("a"), "a.txt")
	id2, _ := s.SaveFile([]byte("bb"), "b.txt")

	files, err := s.ListFiles()
	if err != nil {
		t.Fatalf("ListFiles: %v", err)
	}
	if len(files) != 2 {
		t.Fatalf("len = %d, want 2", len(files))
	}
	seen := map[string]bool{}
	for _, f := range files {
		seen[f.FileID] = true
	}
	if !seen[id1] || !seen[id2] {
		t.Errorf("ListFiles missing ids: %v", seen)
	}
}

func TestCleanupExpiredFiles(t *testing.T) {
	s := newTestStorage(t)
	_, _ = s.SaveFile([]byte("old"), "old.txt")

	// Nothing is older than an hour.
	deleted, err := s.CleanupExpiredFiles(time.Hour)
	if err != nil || deleted != 0 {
		t.Fatalf("CleanupExpiredFiles(1h) = (%d, %v), want (0, nil)", deleted, err)
	}

	// With a zero max age everything already stored has expired.
	time.Sleep(10 * time.Millisecond)
	deleted, err = s.CleanupExpiredFiles(0)
	if err != nil || deleted != 1 {
		t.Fatalf("CleanupExpiredFiles(0) = (%d, %v), want (1, nil)", deleted, err)
	}

	files, _ := s.ListFiles()
	if len(files) != 0 {
		t.Errorf("files remain after cleanup: %v", files)
	}
}
