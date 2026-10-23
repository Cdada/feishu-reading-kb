package main

// Mention events only grant a read of that exact message. Download admission
// additionally requires a physical read of its direct parent and file metadata.
import (
	"context"
	"encoding/json"
	"path/filepath"
	"regexp"
	"strings"
	"unicode"

	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
)

var archiveMentionKey = regexp.MustCompile(`^@_user_[0-9]+$`)
var archiveTime = regexp.MustCompile(`^[0-9]{1,20}$`)

func archiveMentionEnabled(c Control, path string) bool {
	var v struct {
		Profile struct {
			Reference string `json:"reference"`
			Enabled   bool   `json:"enabled"`
		} `json:"archive_mentions"`
	}
	return frozenJSON(c, path, &v) && v.Profile.Reference == "P46-archive-mention-v1" && v.Profile.Enabled && cardEnabled(c, path)
}

func archiveText(raw, key string) bool {
	if len(raw) > 4096 || !archiveMentionKey.MatchString(key) {
		return false
	}
	var body map[string]string
	if json.Unmarshal([]byte(raw), &body) != nil || len(body) != 1 {
		return false
	}
	text, ok := body["text"]
	if !ok {
		return false
	}
	text = strings.TrimSpace(text)
	if text == key {
		return true
	}
	if !strings.HasPrefix(text, key) {
		return false
	}
	rest := []rune(strings.TrimPrefix(text, key))
	return len(rest) > 0 && unicode.IsSpace(rest[0]) && strings.TrimSpace(string(rest)) == "归档"
}

func archiveNativeCommand(m map[string]any, p pilotSession) bool {
	s, _ := m["sender"].(map[string]any)
	body, _ := m["body"].(map[string]any)
	raw, _ := body["content"].(string)
	mentions, _ := m["mentions"].([]any)
	// Newly sent mentions can carry updated=true; revalidate their current body.
	_, updateFlagValid := m["updated"].(bool)
	if m["chat_id"] != p.ChatID || m["msg_type"] != "text" || m["deleted"] != false || !updateFlagValid ||
		s["id"] != p.AccountID || s["id_type"] != "open_id" || s["sender_type"] != "user" || len(mentions) != 1 {
		return false
	}
	mention, _ := mentions[0].(map[string]any)
	key, _ := mention["key"].(string)
	return mention["id"] == p.BotID && mention["id_type"] == "open_id" && archiveText(raw, key)
}

func stringValue(v *string) string {
	if v == nil {
		return ""
	}
	return *v
}

func archiveEventPayload(ev *larkim.P2MessageReceiveV1, app, chat, owner, bot string) map[string]any {
	if ev == nil || ev.EventV2Base == nil || ev.EventV2Base.Header == nil || ev.EventV2Base.Header.AppID != app || ev.EventV2Base.Header.EventType != "im.message.receive_v1" ||
		!ident.MatchString(ev.EventV2Base.Header.EventID) || len(ev.EventV2Base.Header.EventID) > 160 || !archiveTime.MatchString(ev.EventV2Base.Header.CreateTime) ||
		ev.Event == nil || ev.Event.Sender == nil || ev.Event.Sender.SenderId == nil || ev.Event.Message == nil {
		return nil
	}
	s, m := ev.Event.Sender, ev.Event.Message
	if stringValue(s.SenderType) != "user" || stringValue(s.SenderId.OpenId) != owner || stringValue(m.ChatId) != chat ||
		stringValue(m.ChatType) != "group" || stringValue(m.MessageType) != "text" || !cardID.MatchString(stringValue(m.MessageId)) ||
		len(m.Mentions) != 1 || m.Mentions[0] == nil || m.Mentions[0].Id == nil {
		return nil
	}
	mention := m.Mentions[0]
	if stringValue(mention.Id.OpenId) != bot || !archiveText(stringValue(m.Content), stringValue(mention.Key)) {
		return nil
	}
	return map[string]any{"type": "im.message.receive_v1", "event_id": ev.EventV2Base.Header.EventID, "timestamp": ev.EventV2Base.Header.CreateTime,
		"operator_id": owner, "chat_id": chat, "message_id": stringValue(m.MessageId)}
}

func (g *eventGate) handleArchiveMessage(_ context.Context, ev *larkim.P2MessageReceiveV1) error {
	value := archiveEventPayload(ev, g.base.c.AppID, g.chat, g.owner, g.bot)
	if value == nil {
		return nil
	}
	return g.rpc(map[string]any{"type": "event", "app_id": g.base.c.AppID, "event": value})
}

func knownArchiveMessage(c Control, p pilotSession, id string) bool {
	if p.Daily == nil || !archiveMentionEnabled(c, p.Daily.Rules) || !cardID.MatchString(id) {
		return false
	}
	for path := range c.Frozen {
		if !strings.HasSuffix(path, ".archive-event.local.json") {
			continue
		}
		var proof struct {
			Reference string            `json:"reference"`
			App       string            `json:"app_id"`
			Session   string            `json:"listener_session"`
			Event     map[string]string `json:"event"`
		}
		if !frozenJSON(c, path, &proof) || proof.Reference != "P46-archive-mention-v1" || proof.App != p.AppID || len(proof.Event) != 6 {
			continue
		}
		e := proof.Event
		if e["type"] != "im.message.receive_v1" || e["operator_id"] != p.AccountID || e["chat_id"] != p.ChatID || e["message_id"] != id ||
			!ident.MatchString(e["event_id"]) || len(e["event_id"]) > 160 || !archiveTime.MatchString(e["timestamp"]) {
			continue
		}
		var session Control
		if !frozenJSON(c, proof.Session, &session) || session.Reference != "P40-events-v1" || session.Mode != "events" ||
			session.AppID != p.AppID || session.Profile != p.Profile || session.Session != p.Daily.Rules ||
			session.Frozen[p.Daily.Rules] != c.Frozen[p.Daily.Rules] || filepath.Base(proof.Session) != "listener.local.json" ||
			filepath.Dir(path) != filepath.Dir(proof.Session) || filepath.Dir(filepath.Dir(path)) != session.Root ||
			filepath.Base(session.Root) != "card-events" {
			continue
		}
		return true
	}
	return false
}

func knownArchiveParent(c Control, p pilotSession, id string) bool {
	if p.Daily == nil || !archiveMentionEnabled(c, p.Daily.Rules) || !cardID.MatchString(id) {
		return false
	}
	for path := range c.Frozen {
		if !strings.HasSuffix(path, "-message.response.local") {
			continue
		}
		for _, m := range dailyReceiptMessages(c, p, path) {
			if m["parent_id"] == id && archiveNativeCommand(m, p) {
				return true
			}
		}
	}
	return false
}
