package provider_test

import (
	"context"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// maitred names the model that answered in x-maitred-model (under auto, the
// one it routed to); the response carries it.
func TestTheServedModelIsReported(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "a", ServedModel: "radiance/qwen3.8-27b"}, fakellm.Turn{Content: "b"})
	c := client(fake)
	resp, err := c.Complete(context.Background(), basicRequest(), nil)
	if err != nil || resp.ServedModel != "radiance/qwen3.8-27b" {
		t.Fatalf("ServedModel = %q, %v", resp.ServedModel, err)
	}
	if resp, _ := c.Complete(context.Background(), basicRequest(), nil); resp.ServedModel != "" {
		t.Fatalf("no header, no served model: %q", resp.ServedModel)
	}
}
