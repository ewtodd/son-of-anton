package server

// The nudge's environment: a systemd system unit carries no session bus
// address, and desktop notifications only exist on the user's bus. dbusEnv
// fills it in; an inherited one is left alone.

import (
	"reflect"
	"testing"
)

func TestDbusEnvFillsInTheUserBusOnce(t *testing.T) {
	base := []string{"PATH=/bin", "HOME=/home/e-work"}
	want := append(append([]string(nil), base...), "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus")
	if got := dbusEnv(base, 1001); !reflect.DeepEqual(got, want) {
		t.Fatalf("dbusEnv = %v, want the bus appended once", got)
	}

	inherited := []string{"PATH=/bin", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus"}
	if got := dbusEnv(inherited, 1001); !reflect.DeepEqual(got, inherited) {
		t.Fatalf("an inherited bus address must be left alone: %v", got)
	}
}
