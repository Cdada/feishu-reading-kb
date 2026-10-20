package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func cardControl(t *testing.T, mode string) Control {
	return cardControlTarget(t, mode, "file")
}

func cardControlTarget(t *testing.T, mode, targetType string) Control {
	return cardControlTarget(t, mode, "file")
}

func cardControlTarget(t *testing.T, mode, targetType string) Control {
	old := pilotList(dailyControl(t))
	rebindDaily(t, &old)
	g := guard(t, old)
	success(t, g, get(old), `{"code":0,"data":{"has_more":false,"items":[{"message_id":"om_pdf","chat_id":"oc_synthetic","msg_type":"`+targetType+`","deleted":false,"updated":false,"sender":{"id":"ou_synthetic_bot","id_type":"open_id","sender_type":"app"}},{"message_id":"om_card","parent_id":"om_pdf","chat_id":"oc_synthetic","msg_type":"interactive","deleted":false,"updated":false,"sender":{"id":"ou_synthetic_bot","id_type":"open_id","sender_type":"app"}}]}}`)
	g.Close()
	receipt := filepath.Join(old.Root, old.Key+".response.local")
	for _, path := range []string{receipt, filepath.Join(old.Root, "http.local.jsonl")} {
		raw, _ := os.ReadFile(path)
		freezePilot(t, &old, path, raw)
	}
	c := dailyControl(t)
	for path, hash := range old.Frozen {
		c.Frozen[path] = hash
	}
	var p pilotSession
	raw, _ := os.ReadFile(c.Session)
	json.Unmarshal(raw, &p)
	var rules map[string]any
	raw, _ = os.ReadFile(p.Daily.Rules)
	json.Unmarshal(raw, &rules)
	rules["cards"] = map[string]any{"reference": "P40-cards-v1", "enabled": true}
	raw, _ = json.Marshal(rules)
	freezePilot(t, &c, p.Daily.Rules, raw)
	p.Daily.RulesSHA = c.Frozen[p.Daily.Rules]
	raw, _ = json.Marshal(p)
	freezePilot(t, &c, c.Session, raw)
	c.Card = &dailyCard{ID: strings.Repeat("a", 64), Title: "合成测试资料", Target: receipt,
		Binding: map[string]any{"purpose": "preview", "document_id": "om_original", "source_sha256": strings.Repeat("b", 64),
			"target_message_id": "om_pdf", "publication_id": "publication_synthetic", "pdf_sha256": strings.Repeat("c", 64), "preview_version": float64(1)}}
	c.Mode, c.Kind, c.Key = mode, "send", "p001-send"
	c.Path = "/open-apis/im/v1/messages/om_pdf/reply"
	if mode != "send" {
		c.Card.MessageID, c.Card.Origin = "om_card", receipt
		c.Path = "/open-apis/im/v1/messages/om_card"
	}
	if mode == "card_update" {
		raw, _ = json.Marshal(cardJSON(c.Card))
		c.Card.Previous = string(raw)
		c.Card.Result = "已确认入库。"
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
		c.Kind, c.Key, c.Body = "message", "p001-message", ""
		c.Path += "?card_msg_content_type=user_card_content"
	}
	rebindDaily(t, &c)
	return c
}

func TestDailyCardExactRepliesReadbackAndPATCH(t *testing.T) {
	for _, mode := range []string{"send", "read", "card_update"} {
		t.Run(mode, func(t *testing.T) {
			c := cardControl(t, mode)
			g := guard(t, c)
			defer g.Close()
			method := "POST"
			if mode == "read" {
				method = "GET"
			}
			if mode == "card_update" {
				method = "PATCH"
			}
			r, _ := http.NewRequest(method, "https://open.feishu.cn"+c.Path, strings.NewReader(c.Body))
			success(t, g, r, `{"code":0,"data":{}}`)
			args := strings.Join(cliArgs(c), " ")
			if !strings.Contains(args, "api "+method+" ") || strings.Contains(args, "receive_id_type") {
				t.Fatal(args)
			}
		})
	}
}

func TestDailyCardRejectsWidenedScopeOrUnreceiptedOrigin(t *testing.T) {
	cases := map[string]func(*Control){
		"foreign-parent":     func(c *Control) { c.Card.Binding["target_message_id"] = "om_other" },
		"foreign-card":       func(c *Control) { c.Card.MessageID = "om_other"; c.Path = "/open-apis/im/v1/messages/om_other" },
		"missing-target":     func(c *Control) { delete(c.Frozen, c.Card.Target) },
		"missing-journal":    func(c *Control) { delete(c.Frozen, filepath.Join(filepath.Dir(c.Card.Target), "http.local.jsonl")) },
		"arbitrary-body":     func(c *Control) { c.Body = `{"content":"{}"}` },
		"forged-renderer":    func(c *Control) { c.Card.Content = `{"schema":"2.0","body":{"elements":[]}}` },
		"extra-query":        func(c *Control) { c.Path += "?unexpected=true" },
		"unbound-update":     func(c *Control) { c.Card = nil },
		"fractional-version": func(c *Control) { c.Card.Binding["preview_version"] = 1.5 },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			c := cardControl(t, "card_update")
			change(&c)
			rebindDaily(t, &c)
			if g, err := NewGuard(c, time.Now); err == nil {
				g.Close()
				t.Fatal("unapproved card passed")
			}
		})
	}
}

func TestDailyCardRequiresExplicitStandingOptIn(t *testing.T) {
	c := cardControl(t, "send")
	var p pilotSession
	raw, _ := os.ReadFile(c.Session)
	json.Unmarshal(raw, &p)
	var rules map[string]any
	raw, _ = os.ReadFile(p.Daily.Rules)
	json.Unmarshal(raw, &rules)
	delete(rules, "cards")
	raw, _ = json.Marshal(rules)
	freezePilot(t, &c, p.Daily.Rules, raw)
	p.Daily.RulesSHA = c.Frozen[p.Daily.Rules]
	raw, _ = json.Marshal(p)
	freezePilot(t, &c, c.Session, raw)
	rebindDaily(t, &c)
	if g, err := NewGuard(c, time.Now); err == nil {
		g.Close()
		t.Fatal("card profile was not required")
	}
}

func TestDailyCardPhysicalMethodBodyAndHostAreExact(t *testing.T) {
	for _, bad := range []string{"method", "body", "host"} {
		t.Run(bad, func(t *testing.T) {
			c := cardControl(t, "card_update")
			g := guard(t, c)
			defer g.Close()
			method, url, body := "PATCH", "https://open.feishu.cn"+c.Path, c.Body
			if bad == "method" {
				method = "PUT"
			}
			if bad == "body" {
				body = "{}"
			}
			if bad == "host" {
				url = "https://example.org" + c.Path
			}
			r, _ := http.NewRequest(method, url, strings.NewReader(body))
			if _, err := g.PreRoundTripE(r); err == nil {
				t.Fatal("changed physical request passed")
			}
		})
	}
}
