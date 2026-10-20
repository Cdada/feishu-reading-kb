package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func retainDailyProof(t *testing.T, c *Control, path string) {
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	freezePilot(t, c, path, raw)
	journal := filepath.Join(c.Root, "http.local.jsonl")
	raw, err = os.ReadFile(journal)
	if err != nil {
		t.Fatal(err)
	}
	freezePilot(t, c, journal, raw)
}

func TestDailyReconciledPreviewRequiresBoundRealHistoryAndExactPDF(t *testing.T) {
	for _, mode := range []string{"good", "readable", "foreign-chat", "foreign-bot", "filename"} {
		t.Run(mode, func(t *testing.T) {
			upload := dailyControl(t)
			upload.Mode, upload.Kind, upload.Key, upload.Path = "upload", "upload", "p001-upload", "/open-apis/im/v1/files"
			pdf := filepath.Join(upload.Root, "candidate.pdf")
			freezePilot(t, &upload, pdf, []byte(syntheticPDF))
			pid := "publication_" + strings.Repeat("b", 32)
			upload.Preview = &pilotPreview{PublicationID: pid, PDFPath: pdf, SHA256: upload.Frozen[pdf], Size: int64(len(syntheticPDF)), Filename: "preview-" + pid + ".pdf"}
			if mode == "readable" {
				upload.Preview.Filename = "导读_合成论文_第3版.pdf"
			}
			rebindDaily(t, &upload)
			g := guard(t, upload)
			success(t, g, uploadRequest(t, upload, syntheticPDF), `{"code":0,"data":{"file_key":"file_upload"}}`)
			g.Close()
			upload.Preview.UploadReceipt = filepath.Join(upload.Root, upload.Key+".response.local")
			retainDailyProof(t, &upload, upload.Preview.UploadReceipt)

			listed := pilotList(dailyControl(t))
			rebindDaily(t, &listed)
			var sent map[string]any
			json.Unmarshal([]byte(previewSent(upload)), &sent)
			item := sent["data"].(map[string]any)
			if mode == "foreign-chat" {
				item["chat_id"] = "oc_other"
			}
			if mode == "foreign-bot" {
				item["sender"].(map[string]any)["id"] = "cli_other"
			}
			if mode == "filename" {
				item["body"].(map[string]any)["content"] = `{"file_key":"file_message","file_name":"other.pdf"}`
			}
			raw, _ := json.Marshal(map[string]any{"code": 0, "data": map[string]any{"has_more": false, "items": []any{item}}})
			g = guard(t, listed)
			success(t, g, get(listed), string(raw))
			g.Close()
			history := filepath.Join(listed.Root, listed.Key+".response.local")
			retainDailyProof(t, &listed, history)

			download := dailyControl(t)
			for path, digest := range upload.Frozen {
				download.Frozen[path] = digest
			}
			for path, digest := range listed.Frozen {
				download.Frozen[path] = digest
			}
			v := *upload.Preview
			v.SendReceipt = history
			download.Preview = &v
			download.Mode, download.Kind, download.Key = "read", "download", "p001-download"
			download.Path = "/open-apis/im/v1/messages/om_preview/resources/file_message?type=file"
			download.Download = &pilotDownload{MessageID: "om_preview", FileKey: "file_message", Filename: v.Filename, Sample: v.PDFPath, SHA256: v.SHA256, Size: v.Size}
			rebindDaily(t, &download)
			g, err := NewGuard(download, time.Now)
			if (err == nil) != (mode == "good" || mode == "readable") {
				if g != nil {
					g.Close()
				}
				t.Fatal(mode, err)
			}
			if g != nil {
				defer g.Close()
				done, e := g.PreRoundTripE(get(download))
				if e != nil {
					t.Fatal(e)
				}
				resp := response(200, syntheticPDF)
				resp.ContentLength = int64(len(syntheticPDF))
				done(resp, nil)
				if g.failed {
					t.Fatal("exact reconciled PDF rejected")
				}
			}
		})
	}
}

func TestReadablePreviewNamesRejectUnsafePathsAndRequireDailyScope(t *testing.T) {
	for _, name := range []string{"导读_../逃逸_第1版.pdf", "导读_标题\n_第1版.pdf", "导读_标题\u202e_第1版.pdf",
		"导读_标题_第0版.pdf", "导读_标题_第1版.exe", "导读_" + strings.Repeat("长", 100) + "_第1版.pdf"} {
		t.Run(name, func(t *testing.T) {
			c := dailyControl(t)
			var p pilotSession
			if !frozenJSON(c, c.Session, &p) {
				t.Fatal("missing daily session")
			}
			v := &pilotPreview{PublicationID: "publication_" + strings.Repeat("b", 32), Filename: name}
			if validPreviewFilename(v, p) {
				t.Fatal("unsafe label accepted")
			}
		})
	}
	v := &pilotPreview{PublicationID: "publication_" + strings.Repeat("b", 32), Filename: "导读_合成论文_第1版.pdf"}
	if validPreviewFilename(v, pilotSession{}) {
		t.Fatal("legacy approval expanded")
	}
}
