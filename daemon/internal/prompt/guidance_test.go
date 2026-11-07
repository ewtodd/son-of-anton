package prompt_test

// The guidance's rules describe what the model actually receives (the
// guidance is imported into this package): a background receipt is carried
// inside a message — a stamped session puts the send time first, so the
// receipt never starts it.

import (
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
)

func TestTheReceiptRuleFitsWhatArrives(t *testing.T) {
	if strings.Contains(prompt.Guidance, "starts with `[background receipt]`") {
		t.Fatal("the guidance says a receipt starts the message; a stamped session puts the send time first")
	}
	if !strings.Contains(prompt.Guidance, "carries `[background receipt]`") {
		t.Fatal("the guidance must say a background receipt is carried in the message")
	}
}
