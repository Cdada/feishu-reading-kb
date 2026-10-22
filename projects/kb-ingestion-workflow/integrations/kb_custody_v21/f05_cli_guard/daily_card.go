package main

// Card 2.0 writes are confined to a receipted bot reply and a fixed renderer.
import (
	"encoding/json"
	"fmt"
	"reflect"
	"regexp"
	"strings"
)

type dailyCard struct {
	ID        string         `json:"card_id"`
	Binding   map[string]any `json:"binding"`
	Title     string         `json:"title"`
	Content   string         `json:"content"`
	Result    string         `json:"result"`
	MessageID string         `json:"message_id"`
	Origin    string         `json:"origin_receipt"`
	Target    string         `json:"target_receipt"`
	Previous  string         `json:"previous_content"`
}

var cardSHA = regexp.MustCompile(`^[a-f0-9]{64}$`)
var cardID = regexp.MustCompile(`^om_[A-Za-z0-9_-]{1,160}$`)

func cardEnabled(c Control, path string) bool {
	var rules struct {
		Cards struct {
			Reference string `json:"reference"`
			Enabled   bool   `json:"enabled"`
		} `json:"cards"`
	}
	return frozenJSON(c, path, &rules) && rules.Cards.Reference == "P40-cards-v1" && rules.Cards.Enabled
}

func cardBinding(v *dailyCard) bool {
	b := v.Binding
	purpose, _ := b["purpose"].(string)
	parent, _ := b["target_message_id"].(string)
	doc, _ := b["document_id"].(string)
	source, _ := b["source_sha256"].(string)
	if !cardSHA.MatchString(v.ID) || !cardID.MatchString(parent) || !cardID.MatchString(doc) || !cardSHA.MatchString(source) ||
		strings.TrimSpace(v.Title) == "" || len(v.Title) > 1024 {
		return false
	}
	if relationPurpose(purpose) {
		return relationBinding(b)
	}
	if purpose == "qa" {
		job, _ := b["job_id"].(string)
		details, _ := b["details"].(string)
		return (len(b) == 6 && b["menu"] == nil || len(b) == 7 && b["menu"] == "time") && cardSHA.MatchString(job) && len(details) > 0 && len(details) <= 12000
	}
	if purpose == "preview" {
		id, _ := b["publication_id"].(string)
		digest, _ := b["pdf_sha256"].(string)
		version, _ := b["preview_version"].(float64)
		return len(b) == 7 && strings.HasPrefix(id, "publication_") && ident.MatchString(id) && cardSHA.MatchString(digest) && version >= 1 && version <= 999999999 && version == float64(int(version))
	}
	id, _ := b["extraction_id"].(string)
	digest, _ := b["report_sha256"].(string)
	return purpose == "images" && len(b) == 6 && strings.HasPrefix(id, "extraction_") && ident.MatchString(id) && cardSHA.MatchString(digest)
}

func plainCardText(text string) map[string]any {
	return map[string]any{"tag": "plain_text", "content": text}
}
func cardJSON(v *dailyCard) map[string]any {
	b := v.Binding
	title := "选择缺图处理方式"
	detail := "请先查看本卡片所回复的缺图说明。继续后仍需单独确认导读入库。"
	commands := []string{"按缺图版本继续", "重新发文件"}
	if b["purpose"] == "preview" {
		title = "确认导读入库"
		detail = fmt.Sprintf("导读第 %.0f 版。请先查看本卡片所回复的导读 PDF，再选择。", b["preview_version"])
		commands = []string{"确认入库", "补充材料后再入库", "暂不入库"}
	}
	if relationPurpose(b["purpose"]) {
		title, detail, commands = relationCard(b)
	}
	if b["purpose"] == "qa" {
		title, detail = "知识库问答", b["details"].(string)
		commands = []string{"查看原文依据", "本周周报", "新问题"}
		if b["menu"] == "time" {
			commands = []string{"按发表时间", "按内容时间", "按入库时间"}
		}
	}
	t := plainCardText(v.Title)
	t["text_size"] = "heading-3"
	elems := []any{map[string]any{"tag": "div", "text": t}, map[string]any{"tag": "div", "text": plainCardText(detail)}}
	if v.Result != "" {
		elems = append(elems, map[string]any{"tag": "div", "text": plainCardText(v.Result)})
	} else {
		for i, command := range commands {
			kind := "default"
			if i == 0 {
				kind = "primary_filled"
			}
			elems = append(elems, map[string]any{"tag": "button", "text": plainCardText(command), "type": kind, "width": "fill",
				"behaviors": []any{map[string]any{"type": "callback", "value": map[string]any{"reference": "P40-card-v1", "card_id": v.ID, "command": command}}}})
		}
	}
	return map[string]any{"schema": "2.0", "config": map[string]any{"width_mode": "default", "update_multi": true, "enable_forward": false},
		"header": map[string]any{"title": plainCardText(title), "template": "blue", "icon": map[string]any{"tag": "standard_icon", "token": "approval_colorful"}},
		"body":   map[string]any{"direction": "vertical", "vertical_spacing": "8px", "elements": elems}}
}

func dailyCardPath(c Control, p pilotSession) bool {
	v := c.Card
	if v == nil || p.Daily == nil || c.Reference != "P11" || c.Progress != nil || c.Notice != nil || c.Preview != nil || c.Download != nil ||
		!cardEnabled(c, p.Daily.Rules) || !cardBinding(v) || len(v.Content) > 32768 || len(v.Result) > 2048 {
		return false
	}
	if relationPurpose(v.Binding["purpose"]) {
		if !relationEnabled(c, p.Daily.Rules) {
			return false
		}
	} else if v.Binding["purpose"] == "qa" {
		var rules struct {
			QA struct {
				Reference string `json:"reference"`
				Enabled   bool   `json:"enabled"`
			} `json:"qa"`
		}
		if !frozenJSON(c, p.Daily.Rules, &rules) || rules.QA.Reference != "P43-qa-v1" || !rules.QA.Enabled {
			return false
		}
	} else if len(v.Content) > 7000 {
		return false
	}
	var content map[string]any
	if json.Unmarshal([]byte(v.Content), &content) != nil || !reflect.DeepEqual(content, cardJSON(v)) {
		return false
	}
	parent := v.Binding["target_message_id"].(string)
	targetOK := false
	for _, m := range dailyReceiptMessages(c, p, v.Target) {
		if m["message_id"] == parent && dailyBotMessage(m, p) &&
			((v.Binding["purpose"] == "preview" || relationPurpose(v.Binding["purpose"])) && m["msg_type"] == "file" || (v.Binding["purpose"] == "images" || v.Binding["purpose"] == "qa") && m["msg_type"] == "text") {
			targetOK = true
		}
	}
	if !targetOK {
		return false
	}
	if c.Mode == "send" {
		var body map[string]string
		return c.Kind == "send" && v.MessageID == "" && v.Origin == "" && v.Result == "" && v.Previous == "" &&
			c.Path == "/open-apis/im/v1/messages/"+parent+"/reply" && json.Unmarshal([]byte(c.Body), &body) == nil && len(body) == 3 &&
			body["msg_type"] == "interactive" && body["content"] == v.Content && body["uuid"] == "card-"+v.ID[:40]
	}
	if !cardID.MatchString(v.MessageID) {
		return false
	}
	owned := false
	for _, m := range dailyReceiptMessages(c, p, v.Origin) {
		if m["message_id"] == v.MessageID && m["parent_id"] == parent && m["msg_type"] == "interactive" && dailyBotMessage(m, p) {
			owned = true
		}
	}
	if !owned {
		return false
	}
	if c.Mode == "read" {
		return c.Kind == "message" && c.Body == "" && c.Path == "/open-apis/im/v1/messages/"+v.MessageID+"?card_msg_content_type=user_card_content"
	}
	var body map[string]string
	initial := *v
	initial.Result = ""
	var previous map[string]any
	return c.Mode == "card_update" && c.Kind == "send" && strings.TrimSpace(v.Result) != "" &&
		c.Path == "/open-apis/im/v1/messages/"+v.MessageID && json.Unmarshal([]byte(c.Body), &body) == nil && len(body) == 1 && body["content"] == v.Content &&
		json.Unmarshal([]byte(v.Previous), &previous) == nil && reflect.DeepEqual(previous, cardJSON(&initial))
}
