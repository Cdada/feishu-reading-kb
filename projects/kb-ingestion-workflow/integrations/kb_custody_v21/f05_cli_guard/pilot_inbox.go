package main

// A separately approved prospective inbox discovers bytes from successful
// transport receipts. It cannot borrow the old preselected-sample permission.
import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"mime"
	"os"
	"path/filepath"
	"strings"
)

const inboxDownloadReference = "P19-inbox-download-v1"

type pilotInboxPolicy struct {
	Reference string   `json:"reference"`
	Formats   []string `json:"formats"`
	Maximum   int64    `json:"max_asset_bytes"`
	MaxFiles  int      `json:"max_files"`
}

func validPilotDownloadBinding(c Control, p pilotSession) bool {
	d := c.Download
	if d.Reference == "" {
		if p.Operation == "inbox-daily" || p.Operation == "inbox-service" || p.Operation == "receive-inbox" {
			return false
		}
		if d.Maximum != 0 || d.Size < 1 || d.Size > pilotFileLimit || len(d.SHA256) != 64 ||
			c.Frozen[d.Sample] != d.SHA256 || filepath.Base(d.Sample) != d.Filename {
			return false
		}
		info, err := os.Stat(d.Sample)
		return err == nil && info.Mode().IsRegular() && info.Size() == d.Size
	}
	policy := p.Inbox
	daily := p.Operation == "inbox-daily"
	service := p.Operation == "inbox-service"
	ref, extra := "P19-inbox-v1", 0
	if daily {
		ref, extra = "P22-daily-inbox-v1", 1
	}
	if service && policy != nil {
		ref, extra = "P23-service-inbox-v1", policy.MaxFiles
	}
	if d.Reference != inboxDownloadReference || c.Reference != "P11" || (p.Operation != "receive-inbox" && !daily && !service) ||
		policy == nil || policy.Reference != ref || policy.Maximum < 1 || policy.Maximum > pilotFileLimit ||
		policy.MaxFiles < 1 || policy.MaxFiles > 6 || (daily && policy.MaxFiles != 1) || (service && policy.MaxFiles > 3) || p.Limits["download"] != policy.MaxFiles+extra ||
		d.Sample != "" || d.SHA256 != "" || d.Size != 0 || d.Maximum != policy.Maximum ||
		len(d.Filename) < 1 || len(d.Filename) > 1024 || strings.ContainsAny(d.Filename, "/\\\x00\r\n") {
		return false
	}
	seen := map[string]bool{}
	allowed := false
	for _, ext := range policy.Formats {
		if seen[ext] {
			return false
		}
		seen[ext] = true
		switch ext {
		case ".md", ".txt", ".pdf", ".webarchive", ".zip":
		default:
			return false
		}
		if strings.ToLower(filepath.Ext(d.Filename)) == ext {
			allowed = true
		}
	}
	if !allowed {
		return false
	}
	// V1 only admits a complete single-page scan. A truncated page cannot be
	// treated as a complete batch or grant downloads for the visible subset.
	for path, digest := range c.Frozen {
		_, eligible := receiptControl(c, p, path)
		if !eligible || !strings.HasSuffix(path, "-list.response.local") {
			continue
		}
		raw, err := os.ReadFile(path)
		if err != nil || len(raw) > 1024*1024 {
			continue
		}
		hash := sha256.Sum256(raw)
		if hex.EncodeToString(hash[:]) != digest {
			continue
		}
		var v struct {
			Data struct {
				More  *bool `json:"has_more"`
				Items []struct {
					ID string `json:"message_id"`
				} `json:"items"`
			} `json:"data"`
		}
		if json.Unmarshal(raw, &v) != nil || v.Data.More == nil || (*v.Data.More && p.Daily == nil) || len(v.Data.Items) > 50 {
			continue
		}
		for _, m := range v.Data.Items {
			if m.ID == d.MessageID {
				local := c
				if p.Daily == nil {
					local.Frozen = map[string]string{path: digest}
				}
				if knownPilotMessage(local, p, d.MessageID) {
					return true
				}
			}
		}
	}
	return false
}

func downloadMaximum(d *pilotDownload) int64 {
	if d.Reference == inboxDownloadReference {
		return d.Maximum
	}
	return pilotFileLimit
}

func inboxMediaType(value string) bool {
	media, _, err := mime.ParseMediaType(value)
	if err != nil {
		return false
	}
	switch media {
	case "application/octet-stream", "application/pdf", "text/plain", "text/markdown", "application/x-webarchive", "application/x-safari-webarchive", "application/zip", "application/x-zip-compressed":
		return true
	}
	return false
}

func inboxReceivedEvidence(c Control) (int64, string, bool) {
	path := filepath.Join(c.Root, c.Key+".response.local")
	if noLinks(path) != nil {
		return 0, "", false
	}
	f, err := os.Open(path)
	if err != nil {
		return 0, "", false
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, io.LimitReader(f, downloadMaximum(c.Download)+1))
	if err != nil || n < 1 || n > downloadMaximum(c.Download) {
		return 0, "", false
	}
	digest := hex.EncodeToString(h.Sum(nil))
	events, err := (&Guard{c: c}).events()
	if err != nil {
		return 0, "", false
	}
	found := 0
	for _, e := range events {
		if e.Key == c.Key+":download" && e.Type == "complete" && e.Kind == "download" && e.Status == 200 && e.SHA256 == digest {
			found++
		}
	}
	return n, digest, found == 1
}
