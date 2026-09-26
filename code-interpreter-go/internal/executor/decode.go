package executor

import (
	"strings"
	"sync"
	"unicode/utf8"
)

// incrementalUTF8Decoder decodes a byte stream into valid UTF-8 text across
// arbitrary chunk boundaries. Bytes that could be the start of a rune split
// across chunks are held back until the rune completes; invalid sequences are
// replaced with U+FFFD. It is the analogue of Python's incremental UTF-8
// decoder with errors="replace".
type incrementalUTF8Decoder struct {
	pending []byte
}

// Decode appends p to any held-back bytes and returns the longest decodable
// prefix as valid UTF-8 text. When final is true, everything (including an
// incomplete trailing rune) is flushed with replacement characters.
func (d *incrementalUTF8Decoder) Decode(p []byte, final bool) string {
	d.pending = append(d.pending, p...)

	keep := 0
	if !final {
		// Scan at most the last UTFMax-1 bytes for an incomplete rune start.
		limit := len(d.pending) - (utf8.UTFMax - 1)
		for i := len(d.pending) - 1; i >= 0 && i >= limit; i-- {
			b := d.pending[i]
			if b < 0x80 {
				break // ASCII byte: everything up to the end is complete.
			}
			if b >= 0xC0 {
				// Start byte of a multibyte rune: hold it back only if the
				// rune is still incomplete.
				if !utf8.FullRune(d.pending[i:]) {
					keep = len(d.pending) - i
				}
				break
			}
			// Continuation byte: keep scanning backwards.
		}
	}

	emit := d.pending[:len(d.pending)-keep]
	rest := append([]byte(nil), d.pending[len(d.pending)-keep:]...)
	d.pending = rest

	if len(emit) == 0 {
		return ""
	}
	return strings.ToValidUTF8(string(emit), "�")
}

// streamTracker enforces a per-stream output cap while incrementally decoding
// chunks, mirroring Python's _StreamTracker.
type streamTracker struct {
	stream    string
	decoder   incrementalUTF8Decoder
	bytesSent int
	maxBytes  int
}

func newStreamTracker(stream string, maxBytes int) *streamTracker {
	return &streamTracker{stream: stream, maxBytes: maxBytes}
}

// decodeChunk decodes a raw chunk and returns a StreamChunk if within limits.
// Bytes beyond the cap are counted but never decoded or emitted.
func (t *streamTracker) decodeChunk(data []byte) *StreamChunk {
	var chunk *StreamChunk
	if t.bytesSent < t.maxBytes {
		allowed := t.maxBytes - t.bytesSent
		if allowed > len(data) {
			allowed = len(data)
		}
		if text := t.decoder.Decode(data[:allowed], false); text != "" {
			chunk = &StreamChunk{Stream: t.stream, Data: text}
		}
	}
	t.bytesSent += len(data)
	return chunk
}

// flush drains the decoder and returns a final chunk if any bytes remain.
func (t *streamTracker) flush() *StreamChunk {
	if text := t.decoder.Decode(nil, true); text != "" {
		return &StreamChunk{Stream: t.stream, Data: text}
	}
	return nil
}

// trackerWriter adapts a streamTracker + emit callback into an io.Writer for
// backend stream-copy goroutines. mu serializes emits across the stdout and
// stderr writers.
type trackerWriter struct {
	tracker *streamTracker
	emit    EmitFunc
	mu      *sync.Mutex
}

func (w *trackerWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if chunk := w.tracker.decodeChunk(p); chunk != nil {
		w.emit(*chunk)
	}
	return len(p), nil
}
