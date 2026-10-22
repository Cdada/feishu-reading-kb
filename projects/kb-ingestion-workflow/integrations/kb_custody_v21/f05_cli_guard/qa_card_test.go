package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"
)

func qaControl(t *testing.T, mode string, enabled bool, targetType string) Control {
	c := cardControlTarget(t, mode, targetType)
	var p pilotSession
	raw, _ := os.ReadFile(c.Session)
	json.Unmarshal(raw, &p)
	var rules map[string]any
	raw, _ = os.ReadFile(p.Daily.Rules)
	json.Unmarshal(raw, &rules)
	if enabled {
		rules["qa"] = map[string]any{"reference": "P43-qa-v1", "enabled": true}
	}
	raw, _ = json.Marshal(rules)
	freezePilot(t, &c, p.Daily.Rules, raw)
	p.Daily.RulesSHA = c.Frozen[p.Daily.Rules]
	raw, _ = json.Marshal(p)
	freezePilot(t, &c, c.Session, raw)
	c.Card.Binding = map[string]any{"purpose": "qa", "document_id": "om_question", "source_sha256": strings.Repeat("b", 64),
		"target_message_id": "om_pdf", "job_id": strings.Repeat("c", 64), "details": "合成问答操作"}
	if mode == "card_update" {
		c.Card.Result = ""
		raw, _ = json.Marshal(cardJSON(c.Card))
		c.Card.Previous = string(raw)
		c.Card.Result = "已收到，请查看新的问答结果。"
	}
	raw, _ = json.Marshal(cardJSON(c.Card))
	c.Card.Content = string(raw)
	body := map[string]string{"content": c.Card.Content}
	if mode == "send" {
		body["msg_type"] = "interactive"
		body["uuid"] = "card-" + c.Card.ID[:40]
	}
	raw, _ = json.Marshal(body)
	c.Body = string(raw)
	if mode == "read" {
		c.Body = ""
	}
	rebindDaily(t, &c)
	return c
}

func TestQAReadSendAndUpdateRequireExplicitProfile(t *testing.T) {
	for _, mode := range []string{"send", "read", "card_update"} {
		for _, enabled := range []bool{true, false} {
			t.Run(mode+map[bool]string{true: "-enabled", false: "-disabled"}[enabled], func(t *testing.T) {
				c := qaControl(t, mode, enabled, "text")
				g, err := NewGuard(c, time.Now)
				if g != nil {
					g.Close()
				}
				if (err == nil) != enabled {
					t.Fatalf("profile gate mismatch: %v", err)
				}
			})
		}
	}
}

func TestQARejectsUnboundDocumentTargetsAndCommands(t *testing.T) {
	c := qaControl(t, "send", true, "file")
	if g, err := NewGuard(c, time.Now); err == nil {
		g.Close()
		t.Fatal("QA must reply to the receipted text result")
	}
	for _, command := range []string{"查看原文依据", "本周周报", "新问题"} {
		if !knownCardCommand(command) {
			t.Fatal("QA callback missing from listener", command)
		}
	}
	if knownCardCommand("run an arbitrary command") {
		t.Fatal("arbitrary callback accepted")
	}
}

func TestQATimeMenuCommandsAreFixed(t *testing.T) {
	c := qaControl(t, "read", true, "text")
	c.Card.Binding["menu"] = "time"
	if !cardBinding(c.Card) {
		t.Fatal("time menu binding rejected")
	}
	raw, _ := json.Marshal(cardJSON(c.Card))
	for _, command := range []string{"按发表时间", "按内容时间", "按入库时间"} {
		if !strings.Contains(string(raw), command) || !knownCardCommand(command) {
			t.Fatal("time command not rendered or admitted", command)
		}
	}
	if strings.Contains(string(raw), "本周周报") {
		t.Fatal("mixed time and normal menu")
	}
	c.Card.Binding["menu"] = "custom"
	if cardBinding(c.Card) {
		t.Fatal("arbitrary menu accepted")
	}
}

func TestQAWeeklyMenuHasExplicitSynthesisChoice(t *testing.T) {
	c := qaControl(t, "read", true, "text")
	c.Card.Binding["menu"] = "weekly"
	if !cardBinding(c.Card) {
		t.Fatal("weekly menu rejected")
	}
	raw, _ := json.Marshal(cardJSON(c.Card))
	if !knownCardCommand("周报解读") || !strings.Contains(string(raw), "周报解读") {
		t.Fatal("weekly synthesis choice missing")
	}
	if strings.Contains(string(raw), "按发表时间") {
		t.Fatal("wrong menu")
	}
}

func TestQAOriginalMenuDoesNotChangeLegacyCard(t *testing.T) {
	c := qaControl(t, "read", true, "text")
	raw, _ := json.Marshal(cardJSON(c.Card))
	if strings.Contains(string(raw), "取阅原件") {
		t.Fatal("legacy card changed")
	}
	c.Card.Binding["menu"] = "result"
	if !cardBinding(c.Card) {
		t.Fatal("result menu rejected")
	}
	raw, _ = json.Marshal(cardJSON(c.Card))
	if !knownCardCommand("取阅原件") || !strings.Contains(string(raw), "取阅原件") {
		t.Fatal("original choice missing")
	}
}
