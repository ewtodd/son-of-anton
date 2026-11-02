package tools

import "os"

// openNull returns an *os.File for /dev/null (the tools' empty stdin).
func openNull() *os.File {
	f, err := os.OpenFile(os.DevNull, os.O_RDONLY, 0)
	if err != nil {
		return nil
	}
	return f
}
