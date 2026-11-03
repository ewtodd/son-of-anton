package server

import "time"

// SetNow makes the server's clock f, for tests; the returned func restores
// the clock it replaced.
func SetNow(f func() time.Time) (restore func()) {
	old := setClock(f)
	return func() { setClock(old) }
}
