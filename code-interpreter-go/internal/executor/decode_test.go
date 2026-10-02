package executor

import (
	"strings"
	"testing"
)

func TestIncrementalUTF8DecoderSplitRune(t *testing.T) {
	// "héllo" with the é (0xC3 0xA9) split across two chunks.
	var d incrementalUTF8Decoder
	part1 := d.Decode([]byte{'h', 0xC3}, false)
	if part1 != "h" {
		t.Errorf("first chunk = %q, want %q (0xC3 must be held back)", part1, "h")
	}
	part2 := d.Decode([]byte{0xA9, 'l', 'l', 'o'}, false)
	if part2 != "éllo" {
		t.Errorf("second chunk = %q, want %q", part2, "éllo")
	}
	if tail := d.Decode(nil, true); tail != "" {
		t.Errorf("flush = %q, want empty", tail)
	}
}

func TestIncrementalUTF8DecoderInvalidBytes(t *testing.T) {
	var d incrementalUTF8Decoder
	out := d.Decode([]byte{0xFF, 'a'}, false)
	if !strings.Contains(out, "�") || !strings.Contains(out, "a") {
		t.Errorf("invalid byte not replaced: %q", out)
	}
}

func TestIncrementalUTF8DecoderFlushIncomplete(t *testing.T) {
	var d incrementalUTF8Decoder
	if out := d.Decode([]byte{0xE2, 0x82}, false); out != "" {
		t.Errorf("incomplete rune emitted early: %q", out)
	}
	// Final flush must emit the truncated rune as replacement char(s).
	if out := d.Decode(nil, true); !strings.Contains(out, "�") {
		t.Errorf("flush of incomplete rune = %q, want replacement char", out)
	}
}

func TestStreamTrackerCapsOutput(t *testing.T) {
	tr := newStreamTracker("stdout", 5)

	chunk := tr.decodeChunk([]byte("abc"))
	if chunk == nil || chunk.Data != "abc" {
		t.Fatalf("first chunk = %+v, want abc", chunk)
	}

	// Only 2 more bytes are allowed; the rest must be counted but dropped.
	chunk = tr.decodeChunk([]byte("defgh"))
	if chunk == nil || chunk.Data != "de" {
		t.Fatalf("second chunk = %+v, want de", chunk)
	}

	if chunk = tr.decodeChunk([]byte("ijk")); chunk != nil {
		t.Fatalf("after cap, chunk = %+v, want nil", chunk)
	}
	if tr.bytesSent != 11 {
		t.Errorf("bytesSent = %d, want 11 (dropped bytes still counted)", tr.bytesSent)
	}
}

func TestTruncateOutput(t *testing.T) {
	if got := TruncateOutput([]byte("hello"), 100); got != "hello" {
		t.Errorf("under limit = %q", got)
	}

	long := strings.Repeat("x", 200)
	got := TruncateOutput([]byte(long), 100)
	if !strings.HasSuffix(got, "\n...[truncated]") {
		t.Errorf("truncated output missing marker: %q", got)
	}
	// head is max(0, 100-32) = 68 bytes, plus the 15-byte marker.
	if len(got) != 68+len("\n...[truncated]") {
		t.Errorf("truncated length = %d, want %d", len(got), 68+len("\n...[truncated]"))
	}

	// Tiny limit: head is empty, only the marker remains.
	if got := TruncateOutput([]byte(long), 10); got != "\n...[truncated]" {
		t.Errorf("tiny limit = %q", got)
	}

	// Invalid UTF-8 replaced.
	if got := TruncateOutput([]byte{0xFF, 'a'}, 100); !strings.Contains(got, "�") {
		t.Errorf("invalid UTF-8 not replaced: %q", got)
	}
}
