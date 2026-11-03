// Package signal serves Signal for soa-d: signal-cli's HTTP client, the rules
// for which events become messages, Markdown to Signal styles, and the lock
// that keeps two gateways from using one account.
package signal

import (
	"strings"
)

// Config is how an instance uses Signal.
type Config struct {
	URL, Account   string
	Senders        []string // numbers or UUIDs; "*" is anyone
	Groups         []string // group ids; "*" is any
	DMMode         string   // "allow" | "ignore"
	RequireMention bool
	PerUser        bool // group sessions per sender
}

// ConfigFromEnv reads the SIGNAL_* settings through get.
func ConfigFromEnv(get func(string) string) Config {
	c := Config{
		URL:            strings.TrimRight(strings.TrimSpace(get("SIGNAL_HTTP_URL")), "/"),
		Account:        strings.TrimSpace(get("SIGNAL_ACCOUNT")),
		Senders:        splitList(get("SIGNAL_ALLOWED_USERS")),
		Groups:         splitList(get("SIGNAL_GROUP_ALLOWED_USERS")),
		DMMode:         "allow",
		RequireMention: Truthy(get("SIGNAL_REQUIRE_MENTION")),
		PerUser:        true,
	}
	if strings.ToLower(strings.TrimSpace(get("SIGNAL_DM_MODE"))) == "ignore" {
		c.DMMode = "ignore"
	}
	return c
}

// Truthy reads the true/1/yes/on spelling of a boolean setting.
func Truthy(s string) bool {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "true", "1", "yes", "on":
		return true
	}
	return false
}

func splitList(s string) []string {
	var out []string
	for _, f := range strings.Split(s, ",") {
		if f = strings.TrimSpace(f); f != "" {
			out = append(out, f)
		}
	}
	return out
}

// listed says whether id is in an allow list; "*" allows any.
func listed(list []string, id string) bool {
	for _, x := range list {
		if x == "*" || (id != "" && x == id) {
			return true
		}
	}
	return false
}
