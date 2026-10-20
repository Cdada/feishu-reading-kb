package main

// Updates are limited to this bot's previously receipted progress text.
// This is deliberately not an arbitrary-message editing capability.
import (
	"encoding/json"
	"strings"
)

type dailyProgress struct {
	MessageID     string `json:"message_id"`
	OriginReceipt string `json:"origin_receipt"`
	InitialText   string `json:"initial_text"`
	PreviousText  string `json:"previous_text"`
}

func dailyProgressPath(c Control, p pilotSession) bool {
	v := c.Progress
	if v == nil || p.Daily == nil || c.Reference != "P11" || c.Mode != "update" || c.Kind != "send" ||
		c.Notice != nil || c.Preview != nil || c.Download != nil ||
		!strings.HasPrefix(v.MessageID, "om_") || !ident.MatchString(v.MessageID) ||
		c.Path != "/open-apis/im/v1/messages/"+v.MessageID ||
		!strings.HasPrefix(v.InitialText, "PDF 处理进度\n") || len(v.InitialText) > 6000 ||
		!strings.HasPrefix(v.PreviousText, "PDF 处理进度\n") || len(v.PreviousText) > 6000 {
		return false
	}
	var rules struct {
		PDF struct {
			Progress bool `json:"progress"`
		} `json:"pdf"`
	}
	if !frozenJSON(c, p.Daily.Rules, &rules) || !rules.PDF.Progress {
		return false
	}
	owned := false
	for _, m := range dailyReceiptMessages(c, p, v.OriginReceipt) {
		if m["message_id"] != v.MessageID || m["msg_type"] != "text" || !dailyBotMessage(m, p) {
			continue
		}
		body, _ := m["body"].(map[string]any)
		raw, _ := body["content"].(string)
		var content map[string]string
		if json.Unmarshal([]byte(raw), &content) == nil && len(content) == 1 && content["text"] == v.InitialText {
			owned = true
		}
	}
	if !owned {
		return false
	}
	var body, content map[string]string
	return len(c.Body) <= 8192 && json.Unmarshal([]byte(c.Body), &body) == nil && len(body) == 2 &&
		body["msg_type"] == "text" && json.Unmarshal([]byte(body["content"]), &content) == nil && len(content) == 1 &&
		strings.HasPrefix(content["text"], "PDF 处理进度\n") && len(content["text"]) <= 6000
}
