package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const pilotSyntheticFile = "# Synthetic sample\n\nOffline file download only.\n"

func pilotDownloadControl(t *testing.T, filenames ...string) Control {
	filename := "sample.md"
	if len(filenames) == 1 {
		filename = filenames[0]
	}
	t.Helper()
	c := pilotList(pilotControl(t))
	for _, kind := range []string{"list", "message"} {
		if kind == "message" {
			c.Key, c.Kind, c.Path = "p003-message", "message", "/open-apis/im/v1/messages/om_synthetic"
		}
		g := guard(t, c)
		body := `{"code":0,"data":{"has_more":false,"items":[{"message_id":"om_synthetic","chat_id":"oc_synthetic","msg_type":"file","deleted":false,"sender":{"id":"ou_synthetic_user","sender_type":"user","id_type":"open_id"},"body":{"content":"{\"file_key\":\"file_synthetic\",\"file_name\":\"sample.md\"}"}}]}}`
		body = strings.ReplaceAll(body, "sample.md", filename)
		success(t, g, get(c), body)
		g.Close()
		h := sha256.Sum256([]byte(body))
		c.Frozen[filepath.Join(c.Root, c.Key+".response.local")] = hex.EncodeToString(h[:])
	}
	sample := filepath.Join(c.Root, filename)
	freezePilot(t, &c, sample, []byte(pilotSyntheticFile))
	c.Key, c.Kind = "p004-download", "download"
	c.Path = "/open-apis/im/v1/messages/om_synthetic/resources/file_synthetic?type=file"
	c.Download = &pilotDownload{MessageID: "om_synthetic", FileKey: "file_synthetic", Filename: filename,
		Sample: sample, SHA256: c.Frozen[sample], Size: int64(len(pilotSyntheticFile))}
	return c
}

func TestPilotDownloadRequiresFrozenSampleAndRawMessageProvenance(t *testing.T) {
	for name, change := range map[string]func(*Control){
		"other-key": func(c *Control) {
			c.Download.FileKey = "file_other"
			c.Path = strings.ReplaceAll(c.Path, "file_synthetic", "file_other")
		},
		"other-message": func(c *Control) {
			c.Download.MessageID = "om_other"
			c.Path = strings.ReplaceAll(c.Path, "om_synthetic", "om_other")
		},
		"other-name":      func(c *Control) { c.Download.Filename = "other.md" },
		"unfrozen-sample": func(c *Control) { delete(c.Frozen, c.Download.Sample) },
		"wrong-size":      func(c *Control) { c.Download.Size++ },
		"over-limit":      func(c *Control) { c.Download.Size = pilotFileLimit + 1 },
		"no-origin":       func(c *Control) { delete(c.Frozen, filepath.Join(c.Root, "p003-message.response.local")) },
		"profile-mode":    func(c *Control) { c.Mode = "profile" },
		"bad-origin": func(c *Control) {
			freezePilot(t, c, filepath.Join(c.Root, "p003-message.response.local"), []byte(`{}`))
		},
		"existing-output": func(c *Control) { os.WriteFile(filepath.Join(c.Root, c.Key+".cli.local"), []byte("preserve"), 0600) },
	} {
		t.Run(name, func(t *testing.T) {
			c := pilotDownloadControl(t)
			change(&c)
			if g, e := NewGuard(c, time.Now); e == nil {
				g.Close()
				t.Fatal("invalid download accepted")
			}
			if _, e := os.Stat(filepath.Join(c.Root, c.Key+".started.local")); !os.IsNotExist(e) {
				t.Fatal("download started")
			}
		})
	}
}

type failedBinary struct{ sent bool }

func (r *failedBinary) Read(p []byte) (int, error) {
	if !r.sent {
		r.sent = true
		return copy(p, []byte(pilotSyntheticFile[:10])), nil
	}
	return 0, io.ErrUnexpectedEOF
}

func TestPilotDownloadByteLimitLengthHashAndInterruptedStream(t *testing.T) {
	for _, mode := range []string{"ok", "hash", "length", "partial", "over-limit", "json-error", "expired"} {
		t.Run(mode, func(t *testing.T) {
			c := pilotDownloadControl(t)
			g := guard(t, c)
			after, e := g.PreRoundTripE(get(c))
			if e != nil {
				t.Fatal(e)
			}
			body := pilotSyntheticFile
			if mode == "hash" {
				body = strings.Repeat("x", len(body))
			}
			if mode == "json-error" {
				body = `{"code":999,"msg":"synthetic"}`
			}
			r := response(200, body)
			r.Header.Set("Content-Type", "application/octet-stream")
			r.ContentLength = int64(len(body))
			if mode == "length" {
				r.ContentLength++
			}
			if mode == "partial" {
				r.Body = io.NopCloser(&failedBinary{})
			}
			if mode == "over-limit" {
				r.Body = io.NopCloser(io.LimitReader(zeroBytes{}, pilotFileLimit+2))
				r.ContentLength = -1
			}
			if mode == "expired" {
				g.now = func() time.Time { return c.ExpiresAt }
			}
			after(r, nil)
			if (mode == "ok") == g.failed {
				t.Fatalf("unexpected outcome %s", mode)
			}
			events, e := g.events()
			if e != nil {
				t.Fatal(e)
			}
			last := events[len(events)-1]
			if (last.Type == "complete") != (mode == "ok") || last.Kind != "download" {
				t.Fatal(last)
			}
			if mode == "ok" {
				raw, _ := io.ReadAll(r.Body)
				if !bytes.Equal(raw, []byte(pilotSyntheticFile)) {
					t.Fatal("binary changed")
				}
			}
			r.Body.Close()
			info, _ := os.Stat(filepath.Join(c.Root, c.Key+".response.local"))
			if info.Size() > pilotFileLimit+1 {
				t.Fatal("unbounded copy")
			}
			if _, e := g.PreRoundTripE(get(c)); e == nil {
				t.Fatal("automatic repeated download")
			}
		})
	}
}

type zeroBytes struct{}

func (zeroBytes) Read(p []byte) (int, error) { clear(p); return len(p), nil }
