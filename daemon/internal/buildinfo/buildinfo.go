// Package buildinfo holds the one version both commands report.
package buildinfo

// Version is the release, a date (YY.MM.DD). The Nix build sets it with
// -ldflags "-X .../internal/buildinfo.Version=26.11.03"; a plain go build
// leaves it "dev".
var Version = "dev"
