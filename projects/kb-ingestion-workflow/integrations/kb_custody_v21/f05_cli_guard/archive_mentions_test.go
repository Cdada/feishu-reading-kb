package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
)

func TestArchiveEventFiltersScopeMentionAndCommand(t *testing.T) {
	raw := `{"schema":"2.0","header":{"event_id":"event_synthetic","event_type":"im.message.receive_v1","app_id":"cli_synthetic","create_time":"1000000"},"event":{"sender":{"sender_id":{"open_id":"ou_owner"},"sender_type":"user","tenant_key":"private-tenant"},"message":{"message_id":"om_cmd","chat_id":"oc_synthetic","chat_type":"group","message_type":"text","content":"{\"text\":\"@_user_1 归档\"}","mentions":[{"key":"@_user_1","id":{"open_id":"ou_bot"},"name":"private-name"}]}}}`
	for _, change := range []string{"valid", "bare", "wrong-app", "wrong-chat", "wrong-owner", "wrong-bot", "fake-at", "wrong-command", "double-mention", "nil-header"} {
		t.Run(change, func(t *testing.T) {
			var ev larkim.P2MessageReceiveV1
			if err := json.Unmarshal([]byte(raw), &ev); err != nil {
				t.Fatal(err)
			}
			set := func(p **string, value string) { *p = &value }
			switch change {
			case "bare":
				set(&ev.Event.Message.Content, `{"text":"@_user_1"}`)
			case "wrong-app":
				ev.EventV2Base.Header.AppID = "cli_other"
			case "wrong-chat":
				set(&ev.Event.Message.ChatId, "oc_other")
			case "wrong-owner":
				set(&ev.Event.Sender.SenderId.OpenId, "ou_other")
			case "wrong-bot":
				set(&ev.Event.Message.Mentions[0].Id.OpenId, "ou_other")
			case "fake-at":
				set(&ev.Event.Message.Content, `{"text":"@机器人 归档"}`)
			case "wrong-command":
				set(&ev.Event.Message.Content, `{"text":"@_user_1 确认入库"}`)
			case "double-mention":
				ev.Event.Message.Mentions = append(ev.Event.Message.Mentions, ev.Event.Message.Mentions[0])
			case "nil-header":
				ev.EventV2Base.Header = nil
			}
			value := archiveEventPayload(&ev, "cli_synthetic", "oc_synthetic", "ou_owner", "ou_bot")
			if (value != nil) != (change == "valid" || change == "bare") {
				t.Fatalf("%s: %v", change, value)
			}
			encoded, _ := json.Marshal(value)
			if strings.Contains(string(encoded), "private-") {
				t.Fatal("private event fields retained")
			}
		})
	}
}

func archiveControl(t *testing.T) (Control, pilotSession) {
	c := dailyControl(t)
	var p pilotSession
	raw, _ := os.ReadFile(c.Session)
	json.Unmarshal(raw, &p)
	var rules map[string]any
	raw, _ = os.ReadFile(p.Daily.Rules)
	json.Unmarshal(raw, &rules)
	rules["cards"] = map[string]any{"reference": "P40-cards-v1", "enabled": true}
	rules["archive_mentions"] = map[string]any{"reference": "P46-archive-mention-v1", "enabled": true}
	raw, _ = json.Marshal(rules)
	freezePilot(t, &c, p.Daily.Rules, raw)
	p.Daily.RulesSHA = c.Frozen[p.Daily.Rules]
	raw, _ = json.Marshal(p)
	freezePilot(t, &c, c.Session, raw)
	rebindDaily(t, &c)
	root := filepath.Join(c.Root, "card-events")
	directory := filepath.Join(root, "synthetic")
	os.MkdirAll(directory, 0700)
	sessionPath := filepath.Join(directory, "listener.local.json")
	session := Control{Reference: "P40-events-v1", Mode: "events", AppID: p.AppID, Profile: p.Profile, Session: p.Daily.Rules, Root: root, Frozen: map[string]string{p.Daily.Rules: c.Frozen[p.Daily.Rules]}}
	raw, _ = json.Marshal(session)
	freezePilot(t, &c, sessionPath, raw)
	proof := map[string]any{"reference": "P46-archive-mention-v1", "app_id": p.AppID, "listener_session": sessionPath, "event": map[string]string{
		"type": "im.message.receive_v1", "event_id": "event_synthetic", "timestamp": "1000000", "operator_id": p.AccountID, "chat_id": p.ChatID, "message_id": "om_cmd"}}
	raw, _ = json.Marshal(proof)
	freezePilot(t, &c, filepath.Join(directory, "synthetic.archive-event.local.json"), raw)
	return c, p
}

func archiveNative(p pilotSession) map[string]any {
	return map[string]any{"message_id": "om_cmd", "parent_id": "om_file", "chat_id": p.ChatID, "msg_type": "text", "deleted": false, "updated": false,
		"sender": map[string]any{"id": p.AccountID, "id_type": "open_id", "sender_type": "user"},
		"body":   map[string]any{"content": `{"text":"@_user_1 归档"}`}, "mentions": []any{map[string]any{"id": p.BotID, "id_type": "open_id", "key": "@_user_1"}}}
}

func archiveReceipt(t *testing.T, c *Control, key string, item map[string]any) string {
	path := filepath.Join(c.Root, key+"-message.response.local")
	raw, _ := json.Marshal(map[string]any{"code": 0, "data": map[string]any{"items": []any{item}}})
	freezePilot(t, c, path, raw)
	journal := filepath.Join(c.Root, "http.local.jsonl")
	old, _ := os.ReadFile(journal)
	for _, ev := range []map[string]any{{"type": "intent", "kind": "message", "key": key + "-message:message"}, {"type": "complete", "kind": "message", "key": key + "-message:message", "http_status": 200, "response_sha256": c.Frozen[path]}} {
		raw, _ = json.Marshal(ev)
		old = append(old, append(raw, '\n')...)
	}
	freezePilot(t, c, journal, old)
	return path
}

func TestArchiveEventOnlyGrantsExactCommandRead(t *testing.T) {
	c, p := archiveControl(t)
	if !knownPilotMessage(c, p, "om_cmd") {
		t.Fatal("bound event not accepted")
	}
	for _, id := range []string{"om_file", "om_other", "../../secrets"} {
		if knownPilotMessage(c, p, id) {
			t.Fatal("event widened read", id)
		}
	}
	for path := range c.Frozen {
		if filepath.Base(path) == "listener.local.json" {
			delete(c.Frozen, path)
		}
	}
	if knownPilotMessage(c, p, "om_cmd") {
		t.Fatal("unbound listener accepted")
	}
}

func TestArchiveParentAndDownloadRequirePhysicalOwnerCommandAndFile(t *testing.T) {
	for _, change := range []string{"valid", "wrong-parent", "wrong-owner", "wrong-bot", "edited", "no-journal", "no-file", "wrong-key", "no-profile"} {
		t.Run(change, func(t *testing.T) {
			c, p := archiveControl(t)
			item := archiveNative(p)
			switch change {
			case "wrong-parent":
				item["parent_id"] = "om_other"
			case "wrong-owner":
				item["sender"].(map[string]any)["id"] = "ou_other"
			case "wrong-bot":
				item["mentions"].([]any)[0].(map[string]any)["id"] = "ou_other"
			case "edited":
				item["updated"] = true
			}
			archiveReceipt(t, &c, "command", item)
			if change != "no-file" {
				file := map[string]any{"message_id": "om_file", "chat_id": p.ChatID, "msg_type": "file", "deleted": false, "updated": false,
					"sender": map[string]any{"id": p.AccountID, "id_type": "open_id", "sender_type": "user"},
					"body":   map[string]any{"content": `{"file_key":"file_synthetic","file_name":"synthetic.md"}`}}
				archiveReceipt(t, &c, "file", file)
			}
			if change == "no-journal" {
				os.Remove(filepath.Join(c.Root, "http.local.jsonl"))
			}
			if change == "no-profile" {
				delete(c.Frozen, p.Daily.Rules)
			}
			c.Download = &pilotDownload{Reference: inboxDownloadReference, MessageID: "om_file", FileKey: "file_synthetic", Filename: "synthetic.md", Maximum: 4096}
			if change == "wrong-key" {
				c.Download.FileKey = "file_other"
			}
			c.Path = "/open-apis/im/v1/messages/om_file/resources/" + c.Download.FileKey + "?type=file"
			p.Operation = "receive-inbox"
			p.Limits["download"] = 1
			p.Inbox = &pilotInboxPolicy{Reference: "P19-inbox-v1", Formats: []string{".md"}, Maximum: 4096, MaxFiles: 1}
			if knownPilotDownload(c, p) != (change == "valid") {
				t.Fatal("download binding mismatch", change)
			}
		})
	}
}

func TestArchiveStatusCardRequiresCommandReceiptAndFixedRenderer(t *testing.T) {
	c, p := archiveControl(t)
	receipt := archiveReceipt(t, &c, "command", archiveNative(p))
	v := &dailyCard{ID: strings.Repeat("a", 64), Title: "synthetic.md", Target: receipt, Binding: map[string]any{
		"purpose": "archive_status", "document_id": "om_cmd", "source_sha256": strings.Repeat("b", 64), "target_message_id": "om_cmd", "details": "已收到归档请求。"}}
	c.Card = v
	c.Mode = "send"
	c.Kind = "send"
	c.Path = "/open-apis/im/v1/messages/om_cmd/reply"
	raw, _ := json.Marshal(cardJSON(v))
	v.Content = string(raw)
	raw, _ = json.Marshal(map[string]string{"msg_type": "interactive", "content": v.Content, "uuid": "card-" + v.ID[:40]})
	c.Body = string(raw)
	if !dailyCardPath(c, p) {
		t.Fatal("valid archive status card rejected")
	}
	if strings.Contains(v.Content, "button") {
		t.Fatal("status unexpectedly authorizes a choice")
	}
	v.Binding["target_message_id"] = "om_other"
	if dailyCardPath(c, p) {
		t.Fatal("foreign card target accepted")
	}
}
