package server

import "github.com/ewtodd/son-of-anton/daemon/internal/provider"

func usageObj(u provider.Usage) map[string]any {
	return map[string]any{"prompt_tokens": u.PromptTokens, "completion_tokens": u.CompletionTokens,
		"reasoning_tokens": u.ReasoningTokens, "cached_tokens": u.CachedTokens}
}
