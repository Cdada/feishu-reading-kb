package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func mainFixture() *dailyCard {
	return &dailyCard{ID: strings.Repeat("a", 64), Title: "合成归档任务", Binding: map[string]any{
		"purpose": "archive_main", "document_id": "om_source", "target_message_id": "om_source",
		"source_sha256": strings.Repeat("b", 64), "details": "已收到资料，等待解析。", "decision": nil}}
}

func TestArchiveMainExactBindingAndPrevious(t *testing.T) {
	v := mainFixture()
	if !cardBinding(v) {
		t.Fatal("main binding rejected")
	}
	raw, _ := json.Marshal(cardJSON(v))
	v.Previous = string(raw)
	previous := v.Binding
	v.PreviousBinding = previous
	v.Binding = map[string]any{}
	for k, x := range previous {
		v.Binding[k] = x
	}
	v.Binding["details"] = "成功 20/20 页，等待导读。"
	if !mainCardPrevious(v) {
		t.Fatal("valid transition rejected")
	}
	v.Binding["document_id"] = "om_other"
	if mainCardPrevious(v) {
		t.Fatal("cross-document update admitted")
	}
}

func TestArchiveMainActionsRetainExactPreviewIdentity(t *testing.T) {
	v := mainFixture()
	action := map[string]any{"card_id": strings.Repeat("c", 64), "binding": map[string]any{
		"purpose": "preview", "document_id": "om_source", "target_message_id": "om_pdf", "source_sha256": strings.Repeat("d", 64),
		"publication_id": "publication_synthetic", "pdf_sha256": strings.Repeat("e", 64), "preview_version": float64(2)}}
	v.Binding["decision"] = action
	if !cardBinding(v) {
		t.Fatal("valid action rejected")
	}
	raw, _ := json.Marshal(cardJSON(v))
	if !strings.Contains(string(raw), strings.Repeat("c", 64)) || !strings.Contains(string(raw), "确认入库") {
		t.Fatal("immutable action missing")
	}
	action["binding"].(map[string]any)["document_id"] = "om_other"
	if cardBinding(v) {
		t.Fatal("another material decision admitted")
	}
}

func TestArchiveMainRequiresReceiptedOriginalAndPreview(t *testing.T) {
	for _, change := range []string{"valid", "wrong-owner", "wrong-original", "missing-preview", "wrong-preview", "wrong-previous", "no-profile"} {
		t.Run(change, func(t *testing.T) {
			c, p := archiveControl(t)
			raw, _ := os.ReadFile(p.Daily.Rules)
			var rules map[string]any
			json.Unmarshal(raw, &rules)
			if change != "no-profile" {
				rules["archive_cards"] = map[string]any{"reference": "archive-main-v1", "enabled": true}
			}
			raw, _ = json.Marshal(rules)
			freezePilot(t, &c, p.Daily.Rules, raw)
			p.Daily.RulesSHA = c.Frozen[p.Daily.Rules]
			raw, _ = json.Marshal(p)
			freezePilot(t, &c, c.Session, raw)
			owner := p.AccountID
			if change == "wrong-owner" {
				owner = "ou_other"
			}
			original := map[string]any{"message_id": "om_source", "chat_id": p.ChatID, "msg_type": "file", "deleted": false, "updated": false,
				"sender": map[string]any{"id": owner, "id_type": "open_id", "sender_type": "user"}}
			v := mainFixture()
			v.Target = archiveReceipt(t, &c, "original", original)
			if change == "wrong-original" {
				v.Binding["document_id"] = "om_other"
				v.Binding["target_message_id"] = "om_other"
			}
			origin := map[string]any{"message_id": "om_main", "parent_id": "om_source", "chat_id": p.ChatID, "msg_type": "interactive", "deleted": false, "updated": false,
				"sender": map[string]any{"id": p.BotID, "id_type": "open_id", "sender_type": "app"}}
			v.Origin = archiveReceipt(t, &c, "main", origin)
			v.MessageID = "om_main"
			v.PreviousBinding = map[string]any{}
			for k, x := range v.Binding {
				v.PreviousBinding[k] = x
			}
			raw, _ = json.Marshal(cardJSON(v))
			v.Previous = string(raw)
			child := map[string]any{"purpose": "preview", "document_id": v.Binding["document_id"], "target_message_id": "om_pdf", "source_sha256": strings.Repeat("d", 64),
				"publication_id": "publication_synthetic", "pdf_sha256": strings.Repeat("e", 64), "preview_version": float64(1)}
			v.Binding["decision"] = map[string]any{"card_id": strings.Repeat("f", 64), "binding": child}
			if change != "missing-preview" {
				pdfID := "om_pdf"
				if change == "wrong-preview" {
					pdfID = "om_unrelated"
				}
				v.Decision = archiveReceipt(t, &c, "preview", map[string]any{"message_id": pdfID, "chat_id": p.ChatID, "msg_type": "file", "deleted": false, "updated": false,
					"sender": map[string]any{"id": p.BotID, "id_type": "open_id", "sender_type": "app"}})
			}
			if change == "wrong-previous" {
				v.Previous = "{}"
			}
			raw, _ = json.Marshal(cardJSON(v))
			v.Content = string(raw)
			raw, _ = json.Marshal(map[string]string{"content": v.Content})
			c.Body = string(raw)
			c.Card = v
			c.Mode = "card_update"
			c.Kind = "send"
			c.Path = "/open-apis/im/v1/messages/om_main"
			if dailyCardPath(c, p) != (change == "valid") {
				t.Fatalf("unexpected admission for %s", change)
			}
		})
	}
}
