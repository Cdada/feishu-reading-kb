package main

// N10 only downloads named, frozen local samples from the bound person's file
// messages. Raw API keeps the official credential/transport stack, without the
// convenience helper's multipart requests or automatic retries.
import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

const pilotFileLimit int64 = 32 * 1024 * 1024

type pilotDownload struct {
	Reference string `json:"reference,omitempty"`
	Maximum   int64  `json:"max_asset_bytes,omitempty"`
	MessageID string `json:"message_id"`
	FileKey   string `json:"file_key"`
	Filename  string `json:"filename"`
	Sample    string `json:"sample_path"`
	SHA256    string `json:"sha256"`
	Size      int64  `json:"size_bytes"`
}

func knownPilotDownload(c Control, p pilotSession) bool {
	d := c.Download
	if d == nil || !strings.HasPrefix(d.MessageID, "om_") || !ident.MatchString(d.MessageID) ||
		!strings.HasPrefix(d.FileKey, "file_") || !ident.MatchString(d.FileKey) ||
		!validPilotDownloadBinding(c, p) || strings.ContainsAny(d.Filename, "/\\") ||
		c.Path != "/open-apis/im/v1/messages/"+d.MessageID+"/resources/"+d.FileKey+"?type=file" ||
		!knownPilotMessage(c, p, d.MessageID) {
		return false
	}
	switch strings.ToLower(filepath.Ext(d.Filename)) {
	case ".md", ".txt", ".pdf", ".webarchive":
	case ".zip", ".docx", ".xlsx":
		if d.Reference != inboxDownloadReference {
			return false
		}
	default:
		return false
	}
	for path, digest := range c.Frozen {
		origin, eligible := receiptControl(c, p, path)
		if !eligible || !strings.HasSuffix(path, "-message.response.local") {
			continue
		}
		events, err := (&Guard{c: origin, pilot: &p}).events()
		if err != nil {
			continue
		}
		key := strings.TrimSuffix(filepath.Base(path), ".response.local") + ":message"
		intents, completions := 0, 0
		for _, e := range events {
			if e.Key != key {
				continue
			}
			if e.Kind != "message" {
				return false
			}
			if e.Type == "intent" {
				intents++
			} else if e.Type == "complete" && e.Status == 200 && e.SHA256 == digest {
				completions++
			} else {
				return false
			}
		}
		if intents != 1 || completions != 1 {
			continue
		}
		raw, err := os.ReadFile(path)
		if err != nil || len(raw) > 1024*1024 {
			continue
		}
		h := sha256.Sum256(raw)
		if hex.EncodeToString(h[:]) != digest {
			continue
		}
		var v struct {
			Code *int `json:"code"`
			Data struct {
				Items []struct {
					ID      string `json:"message_id"`
					Chat    string `json:"chat_id"`
					Type    string `json:"msg_type"`
					Deleted *bool  `json:"deleted"`
					Updated *bool  `json:"updated"`
					Sender  struct {
						ID     string `json:"id"`
						Type   string `json:"sender_type"`
						IDType string `json:"id_type"`
					} `json:"sender"`
					Body struct {
						Content string `json:"content"`
					} `json:"body"`
				} `json:"items"`
			} `json:"data"`
		}
		if json.Unmarshal(raw, &v) != nil || v.Code == nil || *v.Code != 0 || len(v.Data.Items) != 1 {
			continue
		}
		m := v.Data.Items[0]
		var body struct {
			Key  string `json:"file_key"`
			Name string `json:"file_name"`
		}
		if (d.Reference != inboxDownloadReference || m.Updated == nil || !*m.Updated) && m.ID == d.MessageID && m.Chat == p.ChatID && m.Type == "file" && m.Deleted != nil && !*m.Deleted &&
			m.Sender.ID == p.AccountID && m.Sender.Type == "user" && m.Sender.IDType == "open_id" &&
			json.Unmarshal([]byte(m.Body.Content), &body) == nil && body.Key == d.FileKey && body.Name == d.Filename {
			return true
		}
	}
	return false
}

func (g *Guard) receivePilotFile(resp *http.Response, event Event) {
	// The response is copied in bounded chunks to private evidence, then re-opened
	// for the official CLI. No 32 MiB duplicate buffer in this guard.
	path := filepath.Join(g.c.Root, g.c.Key+".response.local")
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	h := sha256.New()
	var n int64
	if err == nil {
		n, err = io.Copy(io.MultiWriter(f, h), io.LimitReader(resp.Body, downloadMaximum(g.c.Download)+1))
		if err == nil {
			err = f.Sync()
		}
		if closeErr := f.Close(); err == nil {
			err = closeErr
		}
		if err == nil {
			err = syncDir(g.c.Root)
		}
	}
	resp.Body.Close()
	resp.Body = io.NopCloser(strings.NewReader(""))
	event.SHA256 = hex.EncodeToString(h.Sum(nil))
	d := g.c.Download
	diagnostic := newPilotDownloadDiagnostic(resp, n, downloadMaximum(d), err, g.check())
	valid := len(diagnostic.Reasons) == 0
	if d.Reference == inboxDownloadReference {
		// An API JSON error must not become a newly discovered original file.
		if !inboxMediaType(resp.Header.Get("Content-Type")) {
			diagnostic.Reasons = append(diagnostic.Reasons, "media_type_rejected")
			valid = false
		}
	} else {
		if n != d.Size || event.SHA256 != d.SHA256 {
			diagnostic.Reasons = append(diagnostic.Reasons, "sample_mismatch")
			valid = false
		}
	}
	if valid {
		var stream *os.File
		stream, err = os.Open(path)
		valid = err == nil
		if err != nil {
			diagnostic.Reasons = append(diagnostic.Reasons, "saved_file_open_failed")
		}
		if valid {
			resp.Body = stream
		}
	}
	if valid {
		event.Type = "complete"
	} else {
		g.failed = true
		event.Download = diagnostic
	}
	if g.append(event) != nil {
		g.failed = true
	}
}

func pilotSavedFile(c Control, obj map[string]any) bool {
	path := filepath.Join(c.Root, c.Key+".cli.local")
	size, digest := c.Download.Size, c.Download.SHA256
	if c.Download.Reference == inboxDownloadReference {
		var ok bool
		size, digest, ok = inboxReceivedEvidence(c)
		if !ok {
			return false
		}
	}
	if obj["saved_path"] != path || obj["size_bytes"] != float64(size) || noLinks(path) != nil {
		return false
	}
	f, e := os.Open(path)
	if e != nil {
		return false
	}
	defer f.Close()
	h := sha256.New()
	n, e := io.Copy(h, io.LimitReader(f, pilotFileLimit+1))
	return e == nil && n == size && hex.EncodeToString(h.Sum(nil)) == digest
}
