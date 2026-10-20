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
