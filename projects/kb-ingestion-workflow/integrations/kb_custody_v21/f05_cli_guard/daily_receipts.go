package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
)

// A reconciliation result retains its real list/detail receipt. It is never
// relabelled as a successful POST or written into an old physical journal.
func dailyReceiptData(c Control, p pilotSession, path, kind string) map[string]any {
	if p.Daily == nil || !strings.HasSuffix(path, "-"+kind+".response.local") {
		return nil
	}
	origin, ok := receiptControl(c, p, path)
	if !ok {
		return nil
	}
	events, err := (&Guard{c: origin, pilot: &p}).events()
	if err != nil {
		return nil
	}
	key := strings.TrimSuffix(filepath.Base(path), ".response.local") + ":" + kind
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
	raw, err := os.ReadFile(path)
	sum := sha256.Sum256(raw)
	if err != nil || len(raw) > 1024*1024 || hex.EncodeToString(sum[:]) != c.Frozen[path] {
		return nil
	}
	var value struct {
		Code *int           `json:"code"`
		Data map[string]any `json:"data"`
	}
	if json.Unmarshal(raw, &value) != nil || value.Code == nil || *value.Code != 0 {
		return nil
	}
	return value.Data
}

func dailyReceiptMessages(c Control, p pilotSession, path string) []map[string]any {
	for _, kind := range []string{"list", "message", "send"} {
		if !strings.HasSuffix(path, "-"+kind+".response.local") {
			continue
		}
		data := dailyReceiptData(c, p, path, kind)
		if data == nil {
			return nil
		}
		if kind == "send" {
			return []map[string]any{data}
		}
		values, _ := data["items"].([]any)
		var result []map[string]any
		for _, v := range values {
			if m, ok := v.(map[string]any); ok {
				result = append(result, m)
			}
		}
		return result
	}
	return nil
}

func dailyBotMessage(m map[string]any, p pilotSession) bool {
	sender, _ := m["sender"].(map[string]any)
	return m["chat_id"] == p.ChatID && m["deleted"] == false && sender["sender_type"] == "app" &&
		(sender["id_type"] == "app_id" && sender["id"] == p.AppID || sender["id_type"] == "open_id" && sender["id"] == p.BotID)
}

func knownDailyBotMessage(c Control, p pilotSession, id string) bool {
	if p.Daily == nil {
		return false
	}
	for path := range c.Frozen {
		// A previous direct read alone must not invent an initial message ID.
		if !strings.HasSuffix(path, "-list.response.local") && !strings.HasSuffix(path, "-send.response.local") {
			continue
		}
		for _, m := range dailyReceiptMessages(c, p, path) {
			if m["message_id"] == id && dailyBotMessage(m, p) {
				return true
			}
		}
	}
	return false
}

func knownDailyThread(c Control, p pilotSession, id string) bool {
	if p.Daily == nil || !strings.HasPrefix(id, "omt_") || !ident.MatchString(id) {
		return false
	}
	for path := range c.Frozen {
		for _, m := range dailyReceiptMessages(c, p, path) {
			if m["thread_id"] == id && dailyBotMessage(m, p) {
				return true
			}
		}
	}
	return false
}

func dailyReconciledPreview(c Control, p pilotSession) map[string]any {
	v := c.Preview
	if p.Daily == nil || v == nil || c.Download == nil {
		return nil
	}
	for _, m := range dailyReceiptMessages(c, p, v.SendReceipt) {
		if m["message_id"] == c.Download.MessageID && dailyBotMessage(m, p) {
			return m
		}
	}
	return nil
}
