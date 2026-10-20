package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const syntheticPDF = "%PDF-1.4\nSynthetic exact preview bytes.\n%%EOF\n"

func pilotUploadControl(t *testing.T) Control {
	c := pilotControl(t)
	c.Mode = "upload"
	c.Kind = "upload"
	c.Key = "p010-upload"
	c.Path = "/open-apis/im/v1/files"
	path := filepath.Join(c.Root, "candidate.pdf")
	freezePilot(t, &c, path, []byte(syntheticPDF))
	id := "publication_" + strings.Repeat("a", 32)
	c.Preview = &pilotPreview{PublicationID: id, PDFPath: path, SHA256: c.Frozen[path], Size: int64(len(syntheticPDF)), Filename: "preview-" + id + ".pdf"}
	return c
}
func uploadRequest(t *testing.T, c Control, content string) *http.Request {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	w.WriteField("file_type", "pdf")
	w.WriteField("file_name", c.Preview.Filename)
	part, e := w.CreateFormFile("file", filepath.Base(c.Preview.PDFPath))
	if e != nil {
		t.Fatal(e)
	}
	io.WriteString(part, content)
	w.Close()
	r, e := http.NewRequest("POST", "https://open.feishu.cn"+c.Path, bytes.NewReader(buf.Bytes()))
	if e != nil {
		t.Fatal(e)
	}
	r.Header.Set("Content-Type", w.FormDataContentType())
	return r
}
func freezeReceipt(t *testing.T, c *Control, raw string) string {
	path := filepath.Join(c.Root, c.Key+".response.local")
	h := sha256.Sum256([]byte(raw))
	c.Frozen[path] = hex.EncodeToString(h[:])
	return path
}
func pilotSendControl(t *testing.T) Control {
	c := pilotUploadControl(t)
	g := guard(t, c)
	raw := `{"code":0,"data":{"file_key":"file_upload"}}`
	success(t, g, uploadRequest(t, c, syntheticPDF), raw)
	g.Close()
	c.Preview.UploadReceipt = freezeReceipt(t, &c, raw)
	c.Mode = "send"
	c.Kind = "send"
	c.Key = "p011-send"
	c.Path = "/open-apis/im/v1/messages?receive_id_type=chat_id"
	hash := sha256.Sum256([]byte(c.AppID + ":oc_synthetic:" + c.Preview.PublicationID))
	body, _ := json.Marshal(map[string]string{"receive_id": "oc_synthetic", "msg_type": "file", "content": `{"file_key":"file_upload"}`, "uuid": "preview-" + hex.EncodeToString(hash[:])[:40]})
	c.Body = string(body)
	return c
}
func previewSent(c Control) string {
	body, _ := json.Marshal(map[string]string{"file_key": "file_message", "file_name": c.Preview.Filename})
	raw, _ := json.Marshal(map[string]any{"code": 0, "data": map[string]any{"message_id": "om_preview", "chat_id": "oc_synthetic", "msg_type": "file", "deleted": false, "updated": false,
		"sender": map[string]string{"id": "cli_synthetic", "id_type": "app_id", "sender_type": "app"}, "body": map[string]string{"content": string(body)}}})
	return string(raw)
}
func pilotPreviewDownload(t *testing.T) Control {
	c := pilotSendControl(t)
	g := guard(t, c)
	r, _ := http.NewRequest("POST", "https://open.feishu.cn"+c.Path, strings.NewReader(c.Body))
	raw := previewSent(c)
	success(t, g, r, raw)
	g.Close()
	c.Preview.SendReceipt = freezeReceipt(t, &c, raw)
	c.Key = "p012-download"
	c.Kind = "download"
	c.Mode = "read"
	c.Body = ""
	c.Path = "/open-apis/im/v1/messages/om_preview/resources/file_message?type=file"
	v := c.Preview
	c.Download = &pilotDownload{MessageID: "om_preview", FileKey: "file_message", Filename: v.Filename, Sample: v.PDFPath, SHA256: v.SHA256, Size: v.Size}
	return c
}

func TestPilotPreviewFullByteChainAndNewInvocationCannotResend(t *testing.T) {
	c := pilotPreviewDownload(t)
	g := guard(t, c)
	after, err := g.PreRoundTripE(get(c))
	if err != nil {
		t.Fatal(err)
	}
	resp := response(200, syntheticPDF)
	resp.ContentLength = int64(len(syntheticPDF))
	after(resp, nil)
	resp.Body.Close()
	if g.failed {
		t.Fatal("exact preview download rejected")
	}
	g.Close()
	raw, e := os.ReadFile(filepath.Join(c.Root, c.Key+".response.local"))
	if e != nil || string(raw) != syntheticPDF {
		t.Fatal("different downloaded PDF", e)
	}
	c.Kind = "send"
	c.Mode = "send"
	c.Key = "p099-send"
	c.Download = nil
	c.Preview.SendReceipt = ""
	c.Path = "/open-apis/im/v1/messages?receive_id_type=chat_id"
	hash := sha256.Sum256([]byte(c.AppID + ":oc_synthetic:" + c.Preview.PublicationID))
	body, _ := json.Marshal(map[string]string{"receive_id": "oc_synthetic", "msg_type": "file", "content": `{"file_key":"file_upload"}`, "uuid": "preview-" + hex.EncodeToString(hash[:])[:40]})
	c.Body = string(body)
	if g, e := NewGuard(c, time.Now); e == nil {
		g.Close()
		t.Fatal("same candidate resent under another invocation")
	}
}
func TestPilotPreviewScopeAndReceiptRejectBeforeCredential(t *testing.T) {
	for _, mode := range []string{"scope", "key", "receipt", "pdf", "size", "id", "source", "download-key", "sender", "updated"} {
		t.Run(mode, func(t *testing.T) {
			c := pilotSendControl(t)
			switch mode {
			case "scope":
				c.Body = strings.ReplaceAll(c.Body, "oc_synthetic", "oc_other")
			case "key":
				c.Body = strings.ReplaceAll(c.Body, "file_upload", "file_other")
			case "receipt":
				delete(c.Frozen, c.Preview.UploadReceipt)
			case "pdf":
				os.WriteFile(c.Preview.PDFPath, []byte("changed"), 0600)
			case "size":
				c.Preview.Size++
			case "id":
				c.Preview.PublicationID = "publication_" + strings.Repeat("b", 32)
				c.Preview.Filename = "preview-" + c.Preview.PublicationID + ".pdf"
			case "source":
				delete(c.Frozen, c.Preview.PDFPath)
			default:
				c = pilotPreviewDownload(t)
				if mode == "download-key" {
					c.Download.FileKey = "file_upload"
					c.Path = strings.ReplaceAll(c.Path, "file_message", "file_upload")
				} else {
					raw, _ := os.ReadFile(c.Preview.SendReceipt)
					if mode == "sender" {
						raw = bytes.ReplaceAll(raw, []byte("cli_synthetic"), []byte("cli_other"))
					} else {
						raw = bytes.ReplaceAll(raw, []byte(`"updated":false`), []byte(`"updated":true`))
					}
					freezePilot(t, &c, c.Preview.SendReceipt, raw)
				}
			}
			if g, e := NewGuard(c, time.Now); e == nil {
				g.Close()
				t.Fatal("invalid preview permitted")
			}
		})
	}
}
func TestPilotPreviewMultipartRejectsDifferentBytesBeforeHTTP(t *testing.T) {
	c := pilotUploadControl(t)
	g := guard(t, c)
	if _, e := g.PreRoundTripE(uploadRequest(t, c, strings.Repeat("x", len(syntheticPDF)))); e == nil {
		t.Fatal("wrong multipart accepted")
	}
	events, e := g.events()
	if e != nil || len(events) != 0 {
		t.Fatal("HTTP reserved for wrong bytes", e)
	}
}

func TestPilotPreviewUploadUsesSharedCategoryBudget(t *testing.T) {
	c := pilotUploadControl(t)
	g := guard(t, c)
	for _, key := range []string{"p001-upload:upload", "p002-upload:upload", "p003-upload:upload", "p004-upload:upload"} {
		if e := g.append(Event{Type: "intent", Key: key, Kind: "upload"}); e != nil {
			t.Fatal(e)
		}
		if e := g.append(Event{Type: "complete", Key: key, Kind: "upload", Status: 200}); e != nil {
			t.Fatal(e)
		}
	}
	if _, e := g.PreRoundTripE(uploadRequest(t, c, syntheticPDF)); e == nil {
		t.Fatal("exhausted upload category allowed")
	}
	events, _ := g.events()
	if len(events) != 8 {
		t.Fatal("unexpected extra physical intent")
	}
}
