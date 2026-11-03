package provider

import (
	"encoding/json"
	"strings"
	"testing"
)

// Only a message with parts changes shape; every other message serializes as
// it always did.
func TestMessageParts(t *testing.T) {
	plain, _ := json.Marshal(Message{Role: "user", Content: "hi"})
	if string(plain) != `{"role":"user","content":"hi"}` {
		t.Fatalf("plain = %s", plain)
	}
	withCalls, _ := json.Marshal(Message{Role: "assistant", ToolCalls: []ToolCall{{ID: "c", Type: "function"}}})
	if !strings.HasPrefix(string(withCalls), `{"role":"assistant","content":"","tool_calls":[`) {
		t.Fatalf("with calls = %s", withCalls)
	}
	parts, _ := json.Marshal(Message{Role: "user", Parts: []ContentPart{
		{Type: "text", Text: "what?"}, {Type: "image_url", ImageURL: &ImageURL{URL: "data:image/png;base64,AA=="}},
	}})
	if string(parts) != `{"role":"user","content":[{"type":"text","text":"what?"},{"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}}]}` {
		t.Fatalf("parts = %s", parts)
	}
}
