// Package storage manages uploaded files with UUID-based storage. It is the
// Go port of app/services/file_storage.py.
package storage

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/google/uuid"
)

// ErrFileNotFound is returned when a file ID does not exist in storage.
var ErrFileNotFound = errors.New("file not found")

const metadataSuffix = ".meta.json"

// FileMetadata describes a stored file. UploadTime is a Unix timestamp with
// fractional seconds, matching the Python service's JSON representation.
type FileMetadata struct {
	FileID     string  `json:"file_id"`
	Filename   string  `json:"filename"`
	SizeBytes  int64   `json:"size_bytes"`
	UploadTime float64 `json:"upload_time"`
}

// FileStorageService stores file content and metadata side by side in a
// single directory, keyed by UUID.
type FileStorageService struct {
	storageDir string
}

// NewFileStorageService creates the storage directory if needed.
func NewFileStorageService(storageDir string) (*FileStorageService, error) {
	if err := os.MkdirAll(storageDir, 0o755); err != nil {
		return nil, fmt.Errorf("failed to create storage directory: %w", err)
	}
	return &FileStorageService{storageDir: storageDir}, nil
}

func (s *FileStorageService) filePath(fileID string) string {
	return filepath.Join(s.storageDir, fileID)
}

func (s *FileStorageService) metadataPath(fileID string) string {
	return filepath.Join(s.storageDir, fileID+metadataSuffix)
}

// SaveFile stores content under a fresh UUID and returns it.
func (s *FileStorageService) SaveFile(content []byte, filename string) (string, error) {
	fileID := uuid.NewString()

	if err := os.WriteFile(s.filePath(fileID), content, 0o644); err != nil {
		return "", err
	}

	metadata := FileMetadata{
		FileID:     fileID,
		Filename:   filename,
		SizeBytes:  int64(len(content)),
		UploadTime: float64(time.Now().UnixNano()) / 1e9,
	}
	metaBytes, err := json.Marshal(metadata)
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(s.metadataPath(fileID), metaBytes, 0o644); err != nil {
		return "", err
	}

	return fileID, nil
}

// GetFile retrieves file content and metadata by ID. Returns ErrFileNotFound
// when the ID does not exist.
func (s *FileStorageService) GetFile(fileID string) ([]byte, FileMetadata, error) {
	content, err := os.ReadFile(s.filePath(fileID))
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, FileMetadata{}, fmt.Errorf(
				"File with ID '%s' not found: %w", fileID, ErrFileNotFound,
			)
		}
		return nil, FileMetadata{}, err
	}

	metaBytes, err := os.ReadFile(s.metadataPath(fileID))
	if err == nil {
		var metadata FileMetadata
		if err := json.Unmarshal(metaBytes, &metadata); err == nil {
			return content, metadata, nil
		}
	}

	// Fallback for files without (or with corrupt) metadata.
	info, statErr := os.Stat(s.filePath(fileID))
	uploadTime := float64(time.Now().UnixNano()) / 1e9
	if statErr == nil {
		uploadTime = float64(info.ModTime().UnixNano()) / 1e9
	}
	return content, FileMetadata{
		FileID:     fileID,
		Filename:   "unknown",
		SizeBytes:  int64(len(content)),
		UploadTime: uploadTime,
	}, nil
}

// DeleteFile removes a file and its metadata by ID. Returns true if the file
// existed.
func (s *FileStorageService) DeleteFile(fileID string) (bool, error) {
	existed := true
	if err := os.Remove(s.filePath(fileID)); err != nil {
		if !errors.Is(err, fs.ErrNotExist) {
			return false, err
		}
		existed = false
	}
	if err := os.Remove(s.metadataPath(fileID)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return existed, err
	}
	return existed, nil
}

// ListFiles returns metadata for every stored file, skipping unreadable or
// invalid metadata entries.
func (s *FileStorageService) ListFiles() ([]FileMetadata, error) {
	entries, err := os.ReadDir(s.storageDir)
	if err != nil {
		return nil, err
	}

	result := []FileMetadata{}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), metadataSuffix) {
			continue
		}
		metaBytes, err := os.ReadFile(filepath.Join(s.storageDir, entry.Name()))
		if err != nil {
			continue
		}
		var metadata FileMetadata
		if err := json.Unmarshal(metaBytes, &metadata); err != nil {
			continue
		}
		result = append(result, metadata)
	}
	return result, nil
}

// CleanupExpiredFiles removes files older than maxAge and returns the number
// deleted.
func (s *FileStorageService) CleanupExpiredFiles(maxAge time.Duration) (int, error) {
	files, err := s.ListFiles()
	if err != nil {
		return 0, err
	}

	now := float64(time.Now().UnixNano()) / 1e9
	deleted := 0
	for _, metadata := range files {
		if now-metadata.UploadTime <= maxAge.Seconds() {
			continue
		}
		existed, err := s.DeleteFile(metadata.FileID)
		if err != nil {
			continue
		}
		if existed {
			deleted++
		}
	}
	return deleted, nil
}
