package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

const previewLimit int64 = 30 * 1000 * 1000

type pilotPreview struct {
	PublicationID string `json:"publication_id"`
	PDFPath       string `json:"pdf_path"`
	SHA256        string `json:"sha256"`
	Size          int64  `json:"size_bytes"`
	Filename      string `json:"filename"`
	UploadReceipt string `json:"upload_receipt,omitempty"`
	SendReceipt   string `json:"send_receipt,omitempty"`
}

// Read only responses that the same HTTP ledger completed and hashed. A path or
// caller-selected key alone never establishes resource ownership.
func pilotReceipt(c Control, p pilotSession, path, kind string) map[string]any {
	origin, eligible := receiptControl(c, p, path)
	if !eligible || !strings.HasSuffix(path, "-"+kind+".response.local") || c.Frozen[path] == "" {
		return nil
	}
	events, err := (&Guard{c: origin, pilot: &p}).events()
	if err != nil {
		return nil
	}
	key := strings.TrimSuffix(filepath.Base(path), ".response.local") + ":" + kind
	marker := filepath.Join(origin.Root, c.Preview.PublicationID+"-"+kind+".started.local")
	if noLinks(marker) != nil {
		return nil
	}
	rawMarker, err := os.ReadFile(marker)
	var binding map[string]string
	if err != nil || len(rawMarker) > 1024 || json.Unmarshal(rawMarker, &binding) != nil || len(binding) != 2 ||
		binding["key"]+":"+kind != key || binding["pdf_sha256"] != c.Preview.SHA256 {
		return nil
	}
	starts, ends := 0, 0
	for _, e := range events {
		if e.Key != key {
			continue
		}
		if e.Kind != kind {
			return nil
		}
		if e.Type == "intent" {
			starts++
		} else if e.Type == "complete" && e.Status == 200 && e.SHA256 == c.Frozen[path] {
			ends++
		} else {
			return nil
		}
	}
	if starts != 1 || ends != 1 || noLinks(path) != nil {
		return nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, 1024*1024+1))
	h := sha256.Sum256(raw)
	if err != nil || len(raw) > 1024*1024 || hex.EncodeToString(h[:]) != c.Frozen[path] {
		return nil
	}
	var v struct {
		Code *int           `json:"code"`
		Data map[string]any `json:"data"`
	}
	if json.Unmarshal(raw, &v) != nil || v.Code == nil || *v.Code != 0 {
		return nil
	}
	return v.Data
}

func validPreviewFilename(v *pilotPreview, p pilotSession) bool {
	if v.Filename == "preview-"+v.PublicationID+".pdf" {
		return true // Resume existing requests with their exact original name.
	}
	if p.Daily == nil || len(v.Filename) > 240 || !utf8.ValidString(v.Filename) ||
		!regexp.MustCompile(`^导读_.+_第[1-9][0-9]{0,8}版[.]pdf$`).MatchString(v.Filename) {
		return false
	}
	for _, r := range v.Filename {
		if strings.ContainsRune(`/\:*?"<>|`, r) || unicode.IsControl(r) || unicode.In(r, unicode.Cf) {
			return false
		}
	}
	return true
}

func pilotPreviewPath(c Control, p pilotSession) bool {
	v := c.Preview
	if v == nil || !regexp.MustCompile(`^publication_[a-f0-9]{32}$`).MatchString(v.PublicationID) ||
		!validPreviewFilename(v, p) || v.Size < 1 || v.Size > previewLimit ||
		len(v.SHA256) != 64 || c.Frozen[v.PDFPath] != v.SHA256 || noLinks(v.PDFPath) != nil {
		return false
	}
	stat, err := os.Stat(v.PDFPath)
	if err != nil || !stat.Mode().IsRegular() || stat.Size() != v.Size {
		return false
	}
	if c.Mode == "upload" {
		return c.Kind == "upload" && c.Path == "/open-apis/im/v1/files" && c.Download == nil && v.UploadReceipt == "" && v.SendReceipt == ""
	}
	uploaded := pilotReceipt(c, p, v.UploadReceipt, "upload")
	key, _ := uploaded["file_key"].(string)
	if !strings.HasPrefix(key, "file_") || !ident.MatchString(key) {
		return false
	}
	if c.Mode == "send" {
		var body, content map[string]string
		hash := sha256.Sum256([]byte(p.AppID + ":" + p.ChatID + ":" + v.PublicationID))
		return c.Kind == "send" && c.Path == "/open-apis/im/v1/messages?receive_id_type=chat_id" && c.Download == nil && v.SendReceipt == "" &&
			len(c.Body) <= 8192 && json.Unmarshal([]byte(c.Body), &body) == nil && len(body) == 4 && body["receive_id"] == p.ChatID &&
			body["msg_type"] == "file" && body["uuid"] == "preview-"+hex.EncodeToString(hash[:])[:40] &&
			json.Unmarshal([]byte(body["content"]), &content) == nil && len(content) == 1 && content["file_key"] == key
	}
	if c.Mode != "read" || c.Kind != "download" || c.Download == nil || c.Body != "" {
		return false
	}
	m := pilotReceipt(c, p, v.SendReceipt, "send")
	if m == nil {
		m = dailyReconciledPreview(c, p)
	}
	sender, ok := m["sender"].(map[string]any)
	if !ok {
		return false
	}
	mid, ok := m["message_id"].(string)
	if !ok || !strings.HasPrefix(mid, "om_") || !ident.MatchString(mid) {
		return false
	}
	if m["chat_id"] != p.ChatID || m["msg_type"] != "file" || m["deleted"] != false || (m["updated"] != nil && m["updated"] != false) ||
		sender["sender_type"] != "app" || !(sender["id_type"] == "app_id" && sender["id"] == p.AppID || sender["id_type"] == "open_id" && sender["id"] == p.BotID) {
		return false
	}
	body, ok := m["body"].(map[string]any)
	if !ok {
		return false
	}
	text, ok := body["content"].(string)
	if !ok || len(text) > 65536 {
		return false
	}
	var content map[string]any
	if json.Unmarshal([]byte(text), &content) != nil {
		return false
	}
	resource, ok := content["file_key"].(string)
	if !ok || !strings.HasPrefix(resource, "file_") || !ident.MatchString(resource) || content["file_name"] != v.Filename {
		return false
	}
	d := c.Download
	return d.MessageID == mid && d.FileKey == resource && d.Filename == v.Filename && d.Sample == v.PDFPath && d.SHA256 == v.SHA256 && d.Size == v.Size &&
		c.Path == "/open-apis/im/v1/messages/"+mid+"/resources/"+resource+"?type=file"
}

// Inspect the actual official SDK multipart body, including its exact PDF bytes,
// before allowing the POST. Never construct a second upload or HTTP client.
func (g *Guard) pilotUploadBody(req *http.Request) bool {
	if g.pilot == nil || g.c.Preview == nil || req.Method != "POST" || req.URL.Host != "open.feishu.cn" || req.URL.RequestURI() != g.c.Path || req.GetBody == nil {
		return false
	}
	body, err := req.GetBody()
	if err != nil {
		return false
	}
	defer body.Close()
	typ, params, err := mime.ParseMediaType(req.Header.Get("Content-Type"))
	if err != nil || typ != "multipart/form-data" || params["boundary"] == "" {
		return false
	}
	reader := multipart.NewReader(io.LimitReader(body, previewLimit+65536), params["boundary"])
	seen := map[string]bool{}
	v := g.c.Preview
	for {
		part, err := reader.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			return false
		}
		name := part.FormName()
		if seen[name] {
			return false
		}
		seen[name] = true
		if name == "file" {
			if part.FileName() != filepath.Base(v.PDFPath) {
				return false
			}
			h := sha256.New()
			n, err := io.Copy(h, io.LimitReader(part, previewLimit+1))
			if err != nil || n != v.Size || hex.EncodeToString(h.Sum(nil)) != v.SHA256 {
				return false
			}
		} else {
			raw, err := io.ReadAll(io.LimitReader(part, 513))
			if err != nil || len(raw) > 512 || part.FileName() != "" {
				return false
			}
			if !(name == "file_name" && string(raw) == v.Filename || name == "file_type" && string(raw) == "pdf") {
				return false
			}
		}
	}
	return len(seen) == 3 && seen["file"] && seen["file_name"] && seen["file_type"]
}
