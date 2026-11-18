package recall

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// The note that started the investigation: a markdown log whose watched list
// sits under headings, with a long mangled tail glued onto one line.
const movieLog = `# Horror Movies Seen
Log of horror movies Ethan has seen.

## Watched
(Rewatch counts as of 2026-09-26.)

### Roughly high school era
- Cabin in the Woods (2011, Drew Goddard)
- It Follows (2014, David Robert Mitchell)
- Don't Breathe (2016, Fede Álvarez)
- A Quiet Place (2018, John Krasinski)
- Sinister (2012, Scott Derrickson) — **watched many times, loves it**
- The Conjuring 2 (2016, James Wan)
- Insidious 2 (2013, James Wan)
- Hereditary (2018, Ari Aster) — **watched many times, loves it**
- The Babadook (2014, Jennifer Kent) — **seen multiple times**
- SAW (2004, James Wan) — **the first one is GOOD**
- From Dusk Till Dawn (1996, Robert Rodriguez)

### Undergrad
- Mother! (2017, Darren Aronofsky) — **hated it**
- The Witch (2015, Robert Eggers) — **didn't care for it**
- REC (2007, Jaume Balagueró & Paco Plaza) — the Spanish original
- V/H/S (2012, various)
- The Taking of Deborah Logan (2014, Adam Robitel)
- The Autopsy of Jane Doe (2016, André Øvredal) — **watched twice**
- As Above, So Below (2014, Geoff Bowman)
- I Trapped the Devil (2019, Josh Lobo)
- Hush (2015, Mike Flanagan)
- The Strangers (2008, Bryan Bertino)
- False Positive (2021, David F. Sanberg) — **"fucking garbage" (hated)**
- 1BR (2019, D.M.W. Griffith)
- The Ritual (2017, David Bruckner) — **watched twice, "solid"**
- Gerald's Game (2017, Mike Flanagan)
- Perfect Blue (1997, Satoshi Kon) — anime; "if that counts"
- The Bye Bye Man (2017, Stacy Title)
- Men (2022, Alex Garland)
- The Descent (2005, Neil Marshall)
- The Green Room (2015, Jeremy Saulnier) — **pretty good**

### Grad school
- X (2022, Ti West)
- Texas Chainsaw Massacre (2022, David Blue Garcia)
- The Thing (1982, John Carpenter) — **10/10 all-time high**
- The Hills Have Eyes (2006, Marky Rothchild) — the remake
- Sinners (2025, Ryan Coogler) — **great**
- Barbarian (2022, Zach Cregger)
- Weapons (2025, Zach Cregger)
- Resident Evil (2026, Zach Cregger) — the reboot
- Backrooms (2026, Kane Parsons) — weird but fun

## Taste profile (read of the list)
**Strong A24 lean** (confirmed by Ethan): X, Men (multiple times), Backrooms, The Substance lineage.
- Hereditary (many times) + Sinister (many times) = his two pillars.

## Recommendations picked (revised 2026-09-26)
All fresh (not on his list).
- **Talk to Me** (2023) — **WATCHED 09-26**.
- **Get Out** (2017) — **WATCHED 09-28**.
- **The Exorcist** (1973) — **PICKED for the weekend**.
`

func TestChunkTextNeverStartsMidWord(t *testing.T) {
	chunks := chunkText(movieLog, 0)
	if len(chunks) < 3 {
		t.Fatalf("expected several chunks, got %d", len(chunks))
	}
	for i, c := range chunks {
		// The regression: the old chunking cut a window right after "Ge" in
		// "Gerald's Game", so a chunk started "rald's Game (2017, ...)" and the
		// model "fixed" the file. Line-atomic chunking must never start there.
		if strings.HasPrefix(c.text, "rald") {
			t.Fatalf("chunk %d starts mid-word: %q", i, c.text[:40])
		}
		if strings.Contains(c.text, "rald") && !strings.Contains(c.text, "Gerald's Game") {
			t.Fatalf("chunk %d shows a broken Gerald's Game: %q", i, c.text)
		}
	}
	// Every source line must appear whole in some chunk: no line is split.
	for _, line := range normalizeLines(movieLog) {
		if strings.TrimSpace(line) == "" {
			continue
		}
		found := false
		for _, c := range chunks {
			for _, cl := range normalizeLines(c.text) {
				if strings.TrimSpace(cl) == strings.TrimSpace(line) {
					found = true
					break
				}
			}
			if found {
				break
			}
		}
		if !found {
			t.Fatalf("a source line is split or missing: %q", line)
		}
	}
}

func TestChunkSections(t *testing.T) {
	chunks := chunkText(movieLog, 0)
	var sawUndergrad, sawRecs bool
	for _, c := range chunks {
		if strings.Contains(c.section, "## Watched") && strings.Contains(c.section, "### Undergrad") {
			sawUndergrad = true
		}
		if strings.Contains(c.section, "## Recommendations picked") {
			sawRecs = true
		}
	}
	if !sawUndergrad || !sawRecs {
		t.Fatalf("sections not attached: undergrad=%v recs=%v; chunks:\n%+v", sawUndergrad, sawRecs, chunks)
	}
}

func TestChunkLongMangledLine(t *testing.T) {
	// One line holding several sections glued with literal "\n", the state the
	// real log was in: the line must be split at word boundaries, and a piece
	// must never start mid-word.
	text := "# Horror Movies Seen ## Watched - Gerald's Game (2017, Mike Flanagan) - Perfect Blue (1997, Satoshi Kon)"
	for i := 0; i < 40; i++ {
		text += " - The Green Room (2015, Jeremy Saulnier) — **pretty good**"
	}
	chunks := chunkText(text, 0)
	if len(chunks) < 3 {
		t.Fatalf("a long line should come back as word-boundary pieces, got %d", len(chunks))
	}
	for _, c := range chunks {
		if strings.HasPrefix(c.text, "erald") || strings.HasPrefix(c.text, "ald") {
			t.Fatalf("piece starts mid-word: %q", c.text[:30])
		}
	}
}

func TestNoteHitsExpandToTheirSection(t *testing.T) {
	fake := fakellm.New(t)
	home := t.TempDir()
	dir := filepath.Join(home, "notes/gateway/movies")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "log.md"), []byte(movieLog), 0o600); err != nil {
		t.Fatal(err)
	}
	x, err := Open(home, Config{BaseURL: fake.URL, Model: "embed", TopK: 5, MinScore: 0.1,
		SearchTimeout: 3 * time.Second, FailureCooldown: time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	defer x.Close()
	got := x.Recall(context.Background(), "gateway", "Gerald's Game (2017, Mike Flanagan) movie list")
	// The whole watched list comes back with a section label, not a fragment.
	if !strings.Contains(got, "movies/log.md › # Horror Movies Seen › ## Watched") {
		t.Fatalf("no section label for the watched list:\n%s", got)
	}
	if !strings.Contains(got, "From Dusk Till Dawn (1996") || !strings.Contains(got, "Backrooms (2026") || !strings.Contains(got, "Gerald's Game (2017") {
		t.Fatalf("the watched section is incomplete:\n%s", got)
	}
}
