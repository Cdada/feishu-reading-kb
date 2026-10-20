package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func inboxControl(t *testing.T, filenames ...string) Control {
	c := pilotDownloadControl(t, filenames...)
	filename := c.Download.Filename
	old := c.Root
	c.Root = filepath.Join(filepath.Dir(old), "p11-requests")
	if err := os.Rename(old, c.Root); err != nil {
		t.Fatal(err)
	}
	frozen := map[string]string{}
	for path, digest := range c.Frozen {
		frozen[strings.Replace(path, old, c.Root, 1)] = digest
	}
	c.Frozen = frozen
	c.Session = filepath.Join(c.Root, "approval.local.json")
	c.Reference = "P11"
	raw, _ := os.ReadFile(c.Session)
	var p pilotSession
	json.Unmarshal(raw, &p)
	p.Reference = "P11"
	p.Root = c.Root
	p.Operation = "receive-inbox"
	p.Limits = map[string]int{"auth": 5, "identity": 0, "list": 1, "message": 1, "download": 2, "upload": 0, "send": 0}
	p.Total = 9
	p.Purposes = []string{"service", "custody", "collection", "publication"}
	p.PublishScope = "only_bound_account"
	p.Inbox = &pilotInboxPolicy{Reference: "P19-inbox-v1", Formats: []string{".md", ".pdf"}, Maximum: 4096, MaxFiles: 2}
	raw, _ = json.Marshal(p)
	freezePilot(t, &c, c.Session, raw)
	delete(c.Frozen, filepath.Join(c.Root, filename))
	os.Remove(filepath.Join(c.Root, filename))
	c.Download = &pilotDownload{Reference: inboxDownloadReference, MessageID: "om_synthetic", FileKey: "file_synthetic", Filename: filename, Maximum: 4096}
	return c
}

func changeInboxApproval(t *testing.T, c *Control, change func(*pilotSession)) {
	raw, _ := os.ReadFile(c.Session)
	var p pilotSession
	json.Unmarshal(raw, &p)
	change(&p)
	raw, _ = json.Marshal(p)
	freezePilot(t, c, c.Session, raw)
}

func TestInboxPermissionAndProvenanceBeforeCredentials(t *testing.T) {
	for name, change := range map[string]func(*Control){
		"no-policy":             func(c *Control) { changeInboxApproval(t, c, func(p *pilotSession) { p.Inbox = nil }) },
		"wrong-operation":       func(c *Control) { changeInboxApproval(t, c, func(p *pilotSession) { p.Operation = "intake" }) },
		"larger-byte-limit":     func(c *Control) { c.Download.Maximum++ },
		"too-many-files":        func(c *Control) { changeInboxApproval(t, c, func(p *pilotSession) { p.Inbox.MaxFiles = 7 }) },
		"wrong-download-budget": func(c *Control) { changeInboxApproval(t, c, func(p *pilotSession) { p.Inbox.MaxFiles = 1 }) },
		"unapproved-format": func(c *Control) {
			changeInboxApproval(t, c, func(p *pilotSession) { p.Inbox.Formats = []string{".pdf"} })
		},
		"self-approved-hash": func(c *Control) { c.Download.SHA256 = strings.Repeat("a", 64) },
		"no-list":            func(c *Control) { delete(c.Frozen, filepath.Join(c.Root, "p002-list.response.local")) },
		"no-detail":          func(c *Control) { delete(c.Frozen, filepath.Join(c.Root, "p003-message.response.local")) },
		"other-owner":        func(c *Control) { changeInboxApproval(t, c, func(p *pilotSession) { p.AccountID = "ou_other" }) },
		"other-group":        func(c *Control) { changeInboxApproval(t, c, func(p *pilotSession) { p.ChatID = "oc_other" }) },
	} {
		t.Run(name, func(t *testing.T) {
			c := inboxControl(t)
			change(&c)
			g, e := NewGuard(c, time.Now)
			if e == nil {
				g.Close()
				t.Fatal("invalid inbox accepted")
			}
		})
	}
}

func TestInboxActualByteReceiptSavedFileAndNoRepeatedKey(t *testing.T) {
	c := inboxControl(t)
	g := guard(t, c)
	after, e := g.PreRoundTripE(get(c))
	if e != nil {
		t.Fatal(e)
	}
	r := response(200, pilotSyntheticFile)
	r.Header.Set("Content-Type", "application/octet-stream")
	r.ContentLength = int64(len(pilotSyntheticFile))
	after(r, nil)
	if g.failed {
		t.Fatal("new file rejected")
	}
	raw, _ := io.ReadAll(r.Body)
	r.Body.Close()
	path := filepath.Join(c.Root, c.Key+".cli.local")
	os.WriteFile(path, raw, 0600)
	if !pilotSavedFile(c, map[string]any{"saved_path": path, "size_bytes": float64(len(raw))}) {
		t.Fatal("saved bytes did not match real receipt")
	}
	os.WriteFile(path, []byte(strings.Repeat("x", len(raw))), 0600)
	if pilotSavedFile(c, map[string]any{"saved_path": path, "size_bytes": float64(len(raw))}) {
		t.Fatal("changed bytes accepted")
	}
	g.Close()
	c.Key = "p005-download"
	if next, e := NewGuard(c, time.Now); e == nil {
		next.Close()
		t.Fatal("same file redownloaded under new key")
	}
}

func TestInboxStreamLimitsAndUnknownDoNotCreateSuccessfulReceipt(t *testing.T) {
	for _, mode := range []string{"empty", "oversize", "length", "partial", "json", "expired"} {
		t.Run(mode, func(t *testing.T) {
			c := inboxControl(t)
			g := guard(t, c)
			after, e := g.PreRoundTripE(get(c))
			if e != nil {
				t.Fatal(e)
			}
			r := response(200, pilotSyntheticFile)
			r.Header.Set("Content-Type", "application/octet-stream")
			r.ContentLength = int64(len(pilotSyntheticFile))
			switch mode {
			case "empty":
				r.Body = io.NopCloser(strings.NewReader(""))
				r.ContentLength = 0
			case "oversize":
				r.Body = io.NopCloser(io.LimitReader(zeroBytes{}, 4098))
				r.ContentLength = -1
			case "length":
				r.ContentLength++
			case "partial":
				r.Body = io.NopCloser(&failedBinary{})
			case "json":
				r.Header.Set("Content-Type", "application/json")
			case "expired":
				g.now = func() time.Time { return c.ExpiresAt }
			}
			after(r, nil)
			r.Body.Close()
			if !g.failed {
				t.Fatal("invalid stream accepted")
			}
			events, eventErr := g.events()
			if eventErr != nil {
				t.Fatal(eventErr)
			}
			last := events[len(events)-1]
			expected := map[string]string{"empty": "byte_limit_rejected", "oversize": "byte_limit_rejected", "length": "content_length_mismatch", "partial": "stream_or_storage_failed", "json": "media_type_rejected", "expired": "frozen_scope_or_window_changed"}[mode]
			if last.Download == nil || !strings.Contains(strings.Join(last.Download.Reasons, ","), expected) {
				t.Fatalf("missing precise stop diagnostic: %+v", last.Download)
			}
			if last.Download.MaximumBytes != 4096 {
				t.Fatal("incorrect diagnostic bound")
			}
			if _, _, ok := inboxReceivedEvidence(c); ok {
				t.Fatal("failed stream promoted")
			}
			info, _ := os.Stat(filepath.Join(c.Root, c.Key+".response.local"))
			if info.Size() > 4097 {
				t.Fatal("unbounded read")
			}
		})
	}
}

func TestDailyInboxPermissionSeparatesOneOriginalFromPreviewDownload(t *testing.T) {
	for _, mode := range []string{"allowed", "old-policy", "one-download", "two-originals", "legacy-sample"} {
		t.Run(mode, func(t *testing.T) {
			c := inboxControl(t)
			changeInboxApproval(t, &c, func(p *pilotSession) {
				p.Operation = "inbox-daily"
				p.Inbox.Reference = "P22-daily-inbox-v1"
				p.Inbox.MaxFiles = 1
				if mode == "old-policy" {
					p.Inbox.Reference = "P19-inbox-v1"
				}
				if mode == "one-download" {
					p.Limits["download"] = 1
					p.Total--
				}
				if mode == "two-originals" {
					p.Inbox.MaxFiles = 2
					p.Limits["download"] = 3
					p.Total++
				}
			})
			if mode == "legacy-sample" {
				sample := filepath.Join(c.Root, "sample.md")
				freezePilot(t, &c, sample, []byte(pilotSyntheticFile))
				c.Download.Reference = ""
				c.Download.Maximum = 0
				c.Download.Sample = sample
				c.Download.SHA256 = c.Frozen[sample]
				c.Download.Size = int64(len(pilotSyntheticFile))
			}
			g, err := NewGuard(c, time.Now)
			if mode == "allowed" {
				if err != nil {
					t.Fatal(err)
				}
				g.Close()
			} else if err == nil {
				g.Close()
				t.Fatal("invalid daily original permission accepted")
			}
		})
	}
}

func TestDailyPreviewSlotCannotReceiveAnotherOriginal(t *testing.T) {
	c := inboxControl(t)
	changeInboxApproval(t, &c, func(p *pilotSession) {
		p.Operation = "inbox-daily"
		p.Inbox.Reference = "P22-daily-inbox-v1"
		p.Inbox.MaxFiles = 1
	})
	// A different already attempted original consumes the original slot even
	// when no successful HTTP receipt exists yet; the PDF slot is not a retry.
	if err := os.WriteFile(filepath.Join(c.Root, "inbox-"+strings.Repeat("b", 64)+".started.local"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	if g, err := NewGuard(c, time.Now); err == nil {
		g.Close()
		t.Fatal("preview slot admitted another original")
	}
}

func TestServiceOriginalAndPreviewBudgetsRemainSeparate(t *testing.T) {
	c := inboxControl(t)
	changeInboxApproval(t, &c, func(p *pilotSession) {
		p.Operation = "inbox-service"
		p.Inbox.Reference = "P23-service-inbox-v1"
		p.Inbox.MaxFiles = 2
		p.Limits["download"] = 4
		p.Total = 11
	})
	g, err := NewGuard(c, time.Now)
	if err != nil {
		t.Fatal(err)
	}
	g.Close()
}

func TestServiceCannotBorrowOldGrantOrReceiveTooManyOriginals(t *testing.T) {
	for _, mode := range []string{"wrong-reference", "wrong-budget", "too-many", "preview-slot"} {
		t.Run(mode, func(t *testing.T) {
			c := inboxControl(t)
			changeInboxApproval(t, &c, func(p *pilotSession) {
				p.Operation = "inbox-service"
				p.Inbox.Reference = "P23-service-inbox-v1"
				p.Inbox.MaxFiles = 2
				p.Limits["download"] = 4
				p.Total = 11
				if mode == "wrong-reference" {
					p.Inbox.Reference = "P19-inbox-v1"
				}
				if mode == "wrong-budget" {
					p.Limits["download"] = 2
					p.Total = 9
				}
				if mode == "too-many" {
					p.Inbox.MaxFiles = 4
					p.Limits["download"] = 8
					p.Total = 15
				}
			})
			if mode == "preview-slot" {
				for _, digit := range []string{"a", "b"} {
					if err := os.WriteFile(filepath.Join(c.Root, "inbox-"+strings.Repeat(digit, 64)+".started.local"), nil, 0600); err != nil {
						t.Fatal(err)
					}
				}
			}
			if g, err := NewGuard(c, time.Now); err == nil {
				g.Close()
				t.Fatal("invalid service original accepted")
			}
		})
	}
}

func TestInboxSavedFileAfterManyPilotRequests(t *testing.T) {
	c := inboxControl(t)
	changeInboxApproval(t, &c, func(p *pilotSession) {
		p.Operation = "inbox-service"
		p.Inbox.Reference = "P23-service-inbox-v1"
		p.Limits = map[string]int{"auth": 30, "identity": 3, "list": 10, "message": 2, "download": 4, "upload": 2, "send": 9}
		p.Total = 60
	})
	g := guard(t, c)
	defer g.Close()
	// The preceding list/detail and 25 completed auth calls put this
	// successful file receipt at 56 events, as in the second real material.
	for i := 0; i < 25; i++ {
		key := fmt.Sprintf("synthetic-%03d:auth", i)
		if err := g.append(Event{Type: "intent", Key: key, Kind: "auth"}); err != nil {
			t.Fatal(err)
		}
		if err := g.append(Event{Type: "complete", Key: key, Kind: "auth", Status: 200}); err != nil {
			t.Fatal(err)
		}
	}
	after, err := g.PreRoundTripE(get(c))
	if err != nil {
		t.Fatal(err)
	}
	response := response(200, pilotSyntheticFile)
	response.Header.Set("Content-Type", "application/octet-stream")
	response.ContentLength = int64(len(pilotSyntheticFile))
	after(response, nil)
	raw, err := io.ReadAll(response.Body)
	response.Body.Close()
	if err != nil || g.failed {
		t.Fatal("bounded transport failed", err)
	}
	path := filepath.Join(c.Root, c.Key+".cli.local")
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	events, err := g.events()
	if err != nil || len(events) != 56 {
		t.Fatal("unexpected fixture event count", len(events), err)
	}
	if !pilotSavedFile(c, map[string]any{"saved_path": path, "size_bytes": float64(len(raw))}) {
		t.Fatal("successful later inbox receipt rejected by legacy event ceiling")
	}
}

func TestEventReaderKeepsReferenceSpecificBoundsWithoutRuntimeGuard(t *testing.T) {
	for _, test := range []struct {
		name, reference string
		events          int
		allowed         bool
	}{
		{"legacy-at-limit", "F05", 40, true}, {"legacy-over-limit", "F05", 41, false},
		{"pilot-at-limit", "P11", 200, true}, {"pilot-over-limit", "P11", 201, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			root, err := filepath.EvalSymlinks(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			file, err := os.Create(filepath.Join(root, "http.local.jsonl"))
			if err != nil {
				t.Fatal(err)
			}
			for i := 0; i < test.events; i++ {
				if err := json.NewEncoder(file).Encode(Event{Type: "intent", Key: fmt.Sprint(i), Kind: "auth"}); err != nil {
					t.Fatal(err)
				}
			}
			if err := file.Close(); err != nil {
				t.Fatal(err)
			}
			_, err = (&Guard{c: Control{Root: root, Reference: test.reference}}).events()
			if (err == nil) != test.allowed {
				t.Fatal("incorrect reference-specific bound", err)
			}
		})
	}
}

// New ZIP support remains prospective: an old MD-only grant cannot admit it.
func TestMarkdownBundleRequiresExplicitInboxFormat(t *testing.T) {
	for _, allowed := range []bool{false, true} {
		t.Run(fmt.Sprint(allowed), func(t *testing.T) {
			c := inboxControl(t, "sample.zip")
			if allowed {
				changeInboxApproval(t, &c, func(p *pilotSession) { p.Inbox.Formats = []string{".md", ".zip", ".webarchive"} })
			}
			g, err := NewGuard(c, time.Now)
			if allowed && err != nil {
				t.Fatal(err)
			}
			if !allowed && err == nil {
				g.Close()
				t.Fatal("old grant accepted new format")
			}
			if g != nil {
				g.Close()
			}
		})
	}
}

func TestMarkdownBundleDownloadMediaTypes(t *testing.T) {
	for _, media := range []string{"application/zip", "application/x-zip-compressed", "application/octet-stream"} {
		t.Run(media, func(t *testing.T) {
			c := inboxControl(t, "sample.zip")
			changeInboxApproval(t, &c, func(p *pilotSession) { p.Inbox.Formats = []string{".md", ".zip", ".webarchive"} })
			g := guard(t, c)
			defer g.Close()
			after, err := g.PreRoundTripE(get(c))
			if err != nil {
				t.Fatal(err)
			}
			r := response(200, pilotSyntheticFile)
			r.Header.Set("Content-Type", media)
			r.ContentLength = int64(len(pilotSyntheticFile))
			after(r, nil)
			r.Body.Close()
			if g.failed {
				t.Fatal("approved ZIP transport rejected")
			}
			if _, _, ok := inboxReceivedEvidence(c); !ok {
				t.Fatal("missing byte receipt")
			}
		})
	}
}

// Transport admission preserves byte receipts; WebArchive parsing is checked downstream.
func TestWebarchiveDownloadMediaTypes(t *testing.T) {
	for _, tc := range []struct {
		media    string
		accepted bool
	}{
		{"application/x-safari-webarchive", true},
		{"application/x-safari-webarchive; charset=binary", true},
		{"application/x-webarchive", true},
		{"application/octet-stream", true},
		{"application/json", false},
		{"text/html", false},
		{"application/x-safari-webarchivex", false},
		{"application/x-safari-webarchive; invalid", false},
	} {
		t.Run(tc.media, func(t *testing.T) {
			c := inboxControl(t, "sample.webarchive")
			changeInboxApproval(t, &c, func(p *pilotSession) { p.Inbox.Formats = []string{".webarchive"} })
			g := guard(t, c)
			defer g.Close()
			after, err := g.PreRoundTripE(get(c))
			if err != nil {
				t.Fatal(err)
			}
			r := response(200, pilotSyntheticFile)
			r.Header.Set("Content-Type", tc.media)
			r.ContentLength = int64(len(pilotSyntheticFile))
			after(r, nil)
			r.Body.Close()
			if g.failed == tc.accepted {
				t.Fatalf("unexpected transport result for %q: failed=%v", tc.media, g.failed)
			}
			_, _, received := inboxReceivedEvidence(c)
			if received != tc.accepted {
				t.Fatalf("receipt eligibility for %q: got=%v", tc.media, received)
			}
		})
	}
}

func TestWebarchiveRequiresExplicitInboxFormat(t *testing.T) {
	c := inboxControl(t, "sample.webarchive")
	if g, err := NewGuard(c, time.Now); err == nil {
		g.Close()
		t.Fatal("existing MD/PDF permission admitted a WebArchive")
	}
}
