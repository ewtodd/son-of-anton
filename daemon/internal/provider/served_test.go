package provider_test

import (
	"context"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
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

// OnServed hears the served model from the headers, before any of the
// stream, and is not called when the endpoint names none.
func TestOnServedHearsTheHeadersFirst(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "a", ServedModel: "radiance/qwen3.8-27b"}, fakellm.Turn{Content: "b"})
	c := client(fake)
	var order []string
	req := basicRequest()
	req.OnServed = func(m string) { order = append(order, "served "+m) }
	if _, err := c.Complete(context.Background(), req, func(provider.Delta) { order = append(order, "delta") }); err != nil {
		t.Fatal(err)
	}
	if len(order) < 2 || order[0] != "served radiance/qwen3.8-27b" || order[1] != "delta" {
		t.Fatalf("order = %v", order)
	}
	order = nil
	if _, err := c.Complete(context.Background(), req, nil); err != nil || len(order) != 0 {
		t.Fatalf("no header, no call: %v, %v", order, err)
	}
}
