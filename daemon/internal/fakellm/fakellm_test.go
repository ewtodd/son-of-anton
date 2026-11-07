package fakellm

// The stream check names what it saw (owner, 2026-11-07): the daemon asks for
// a stream on every chat request, so a body that would not parse is not a
// non-streaming request, and the message says which of the two arrived — with
// the path, the body length and the JSON error, so the next occurrence reads
// as its own diagnosis. What the fake accepts does not change: the same
// refusals, the same 400 and body as before.

import (
	"strings"
	"testing"
)

func TestTheStreamCheckNamesItsCause(t *testing.T) {
	// A body that would not parse says it did not parse: the JSON error is
	// in the message, beside the path and the length.
	_, refused := streamCheck(1, "/v1/chat/completions", []byte("{oops"))
	if !strings.Contains(refused, "request 1 to /v1/chat/completions (5 bytes)") ||
		!strings.Contains(refused, "did not parse") || !strings.Contains(refused, "invalid character") {
		t.Fatalf("an unparsed body is not named as its own cause: %q", refused)
	}
	// A body that parses but does not stream keeps the plain wording, with
	// the path and the length beside it.
	_, refused = streamCheck(2, "/v1/chat/completions", []byte(`{"model":"m"}`))
	if !strings.Contains(refused, "request 2 to /v1/chat/completions (13 bytes)") ||
		!strings.Contains(refused, "did not ask for a stream") {
		t.Fatalf("a non-streaming request is not named as its own cause: %q", refused)
	}
	// A streaming request passes, model in hand for the chunks.
	model, refused := streamCheck(3, "/v1/chat/completions", []byte(`{"model":"m","stream":true}`))
	if refused != "" || model != "m" {
		t.Fatalf("a streaming request was refused (%q) or lost its model (%q)", refused, model)
	}
}
