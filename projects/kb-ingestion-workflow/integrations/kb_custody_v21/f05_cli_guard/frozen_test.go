package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func onlyFrozen(c Control) string {
	for p := range c.Frozen {
		return p
	}
	panic("missing fixture")
}

func TestCachedFrozenEvidenceRejectsChanges(t *testing.T) {
	for _, scenario := range []string{"same_size_restored_mtime", "replacement", "symlink", "parent_symlink", "missing", "digest"} {
		t.Run(scenario, func(t *testing.T) {
			c := control(t)
			p := onlyFrozen(c)
			g := guard(t, c)
			info, err := os.Stat(p)
			if err != nil {
				t.Fatal(err)
			}
			switch scenario {
			case "same_size_restored_mtime":
				// Keep the inode, size and modification timestamp; ctime still changes.
				time.Sleep(2 * time.Millisecond)
				if err = os.WriteFile(p, []byte("different"), 0600); err != nil {
					t.Fatal(err)
				}
				if err = os.Chtimes(p, info.ModTime(), info.ModTime()); err != nil {
					t.Fatal(err)
				}
			case "replacement":
				other := p + ".replacement"
				if err = os.WriteFile(other, []byte("different"), 0600); err != nil {
					t.Fatal(err)
				}
				if err = os.Chtimes(other, info.ModTime(), info.ModTime()); err != nil {
					t.Fatal(err)
				}
				if err = os.Rename(other, p); err != nil {
					t.Fatal(err)
				}
			case "symlink":
				other := p + ".moved"
				if err = os.Rename(p, other); err != nil {
					t.Fatal(err)
				}
				if err = os.Symlink(other, p); err != nil {
					t.Fatal(err)
				}
			case "parent_symlink":
				parent := filepath.Dir(p)
				other := parent + ".moved"
				if err = os.Rename(parent, other); err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { os.Remove(parent); os.Rename(other, parent) })
				if err = os.Symlink(other, parent); err != nil {
					t.Fatal(err)
				}
			case "missing":
				if err = os.Remove(p); err != nil {
					t.Fatal(err)
				}
			case "digest":
				c.Frozen[p] = hex.EncodeToString(make([]byte, 32))
			}
			if err = g.checkCached(); err == nil {
				t.Fatal("changed evidence passed local checkpoint")
			}
		})
	}
}

func TestPhysicalAccessRehashesEvenMatchingCachedStamp(t *testing.T) {
	c := control(t)
	p := onlyFrozen(c)
	g := guard(t, c)
	if err := os.WriteFile(p, []byte("different"), 0600); err != nil {
		t.Fatal(err)
	}
	var info unix.Stat_t
	if err := unix.Lstat(p, &info); err != nil {
		t.Fatal(err)
	}
	// Deliberately forge the in-memory hit: the physical boundary must still
	// read the bytes instead of trusting this local optimization.
	g.verified[p] = verifiedFile{stamp(info), c.Frozen[p]}
	if _, err := g.PreRoundTripE(auth(c)); err == nil {
		t.Fatal("physical request trusted cache")
	}
	if events, err := g.events(); err != nil || len(events) != 0 {
		t.Fatalf("request admitted: %v %v", events, err)
	}
}

func TestCachedFrozenCheckStillEnforcesWindow(t *testing.T) {
	c := control(t)
	g := guard(t, c)
	if err := g.checkCached(); err != nil {
		t.Fatal(err)
	}
	g.now = func() time.Time { return c.ExpiresAt }
	if err := g.checkCached(); err == nil {
		t.Fatal("cached evidence bypassed deadline")
	}
}

func TestChangedEvidenceDuringResponseCannotComplete(t *testing.T) {
	c := control(t)
	g := guard(t, c)
	// Pilot responses use the same local checkpoint before recording success.
	post, err := g.PreRoundTripE(get(c))
	if err != nil {
		t.Fatal(err)
	}
	g.pilot = &pilotSession{}
	if err = os.WriteFile(onlyFrozen(c), []byte("different"), 0600); err != nil {
		t.Fatal(err)
	}
	post(response(200, `{"code":0}`), nil)
	if !g.failed {
		t.Fatal("changed response evidence passed")
	}
	events, err := g.events()
	if err != nil || len(events) != 2 || events[1].Type != "stop" {
		t.Fatalf("invalid completion: %v %v", events, err)
	}
}

func TestFrozenCheckpointAllowsUnchangedContentWithNewMetadata(t *testing.T) {
	c := control(t)
	g := guard(t, c)
	p := onlyFrozen(c)
	future := time.Now().Add(time.Minute)
	if err := os.Chtimes(p, future, future); err != nil {
		t.Fatal(err)
	}
	if err := g.checkCached(); err != nil {
		t.Fatal(err)
	}
	if err := g.check(); err != nil {
		t.Fatal(err)
	}
}

func BenchmarkFrozenCheckpoints(b *testing.B) {
	root, err := filepath.EvalSymlinks(b.TempDir())
	if err != nil {
		b.Fatal(err)
	}
	frozen := map[string]string{}
	body := bytes.Repeat([]byte("synthetic frozen evidence\n"), 16384)
	digest := sha256.Sum256(body)
	for i := 0; i < 64; i++ {
		p := filepath.Join(root, fmt.Sprintf("synthetic-%03d", i))
		if err = os.WriteFile(p, body, 0600); err != nil {
			b.Fatal(err)
		}
		frozen[p] = hex.EncodeToString(digest[:])
	}
	c := Control{StartsAt: time.Now().Add(-time.Minute), ExpiresAt: time.Now().Add(time.Hour), Frozen: frozen}
	for _, cached := range []bool{false, true} {
		b.Run(fmt.Sprintf("local_reuse=%t", cached), func(b *testing.B) {
			g := &Guard{c: c, now: time.Now}
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				// Original one-shot path: setup, local preflight, credentials, auth HTTP,
				// auth result, business HTTP, business result, local CLI result.
				for _, physical := range []bool{true, false, false, true, false, true, false, false} {
					var err error
					if cached && !physical {
						err = g.checkCached()
					} else {
						err = g.check()
					}
					if err != nil {
						b.Fatal(err)
					}
				}
			}
		})
	}
}
