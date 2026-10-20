package main

import (
	"encoding/json"
	"net/http"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"
)

func relationControl(t *testing.T, mode string, enabled bool) Control {
	c := cardControl(t, mode)
	c.Card.Binding = map[string]any{"purpose": "relation_candidates", "document_id": "om_original", "source_sha256": strings.Repeat("b", 64), "target_message_id": "om_pdf", "material_id": strings.Repeat("c", 64), "snapshot_sha256": strings.Repeat("d", 64), "revision": float64(0), "details": "合成资料：候选仅供本人判断。", "candidate_count": float64(2)}
	var p pilotSession
	raw, _ := os.ReadFile(c.Session)
	json.Unmarshal(raw, &p)
	var rules map[string]any
	raw, _ = os.ReadFile(p.Daily.Rules)
	json.Unmarshal(raw, &rules)
	if enabled {
		rules["relations"] = map[string]any{"reference": "P42-relations-v1", "enabled": true}
		rules["reading"] = map[string]any{"reference": "P41-reading-v1"}
	}
	raw, _ = json.Marshal(rules)
	freezePilot(t, &c, p.Daily.Rules, raw)
	p.Daily.RulesSHA = c.Frozen[p.Daily.Rules]
	raw, _ = json.Marshal(p)
	freezePilot(t, &c, c.Session, raw)
	if mode == "card_update" {
		initial := *c.Card
		initial.Result = ""
		raw, _ = json.Marshal(cardJSON(&initial))
		c.Card.Previous = string(raw)
	}
	raw, _ = json.Marshal(cardJSON(c.Card))
	c.Card.Content = string(raw)
	if mode != "read" {
		body := map[string]string{"content": c.Card.Content}
		if mode == "send" {
			body["msg_type"] = "interactive"
			body["uuid"] = "card-" + c.Card.ID[:40]
		}
		raw, _ = json.Marshal(body)
		c.Body = string(raw)
	}
	rebindDaily(t, &c)
	return c
}

func TestRelationCardsRequireOptInAndExactReceiptedRequests(t *testing.T) {
	for _, mode := range []string{"send", "read", "card_update"} {
		t.Run(mode, func(t *testing.T) {
			c := relationControl(t, mode, false)
			if g, err := NewGuard(c, time.Now); err == nil {
				g.Close()
				t.Fatal("relations were enabled without P42 authorization")
			}
			c = relationControl(t, mode, true)
			g := guard(t, c)
			defer g.Close()
			method := map[string]string{"send": "POST", "read": "GET", "card_update": "PATCH"}[mode]
			req, _ := http.NewRequest(method, "https://open.feishu.cn"+c.Path, strings.NewReader(c.Body))
			success(t, g, req, `{"code":0,"data":{}}`)
		})
	}
}

func TestRelationCardsRejectUnboundedOrChangedDefinitions(t *testing.T) {
	cases := map[string]func(*dailyCard){
		"six-candidates":   func(v *dailyCard) { v.Binding["candidate_count"] = float64(6) },
		"fractional-count": func(v *dailyCard) { v.Binding["candidate_count"] = 1.5 },
		"extra-field":      func(v *dailyCard) { v.Binding["url"] = "https://example.org" },
		"missing-material": func(v *dailyCard) { delete(v.Binding, "material_id") },
		"unbounded-detail": func(v *dailyCard) { v.Binding["details"] = strings.Repeat("x", 12001) },
		"changed-snapshot": func(v *dailyCard) { v.Binding["snapshot_sha256"] = "not-a-digest" },
		"forged-body":      func(v *dailyCard) { v.Content = `{}` },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			c := relationControl(t, "send", true)
			change(c.Card)
			rebindDaily(t, &c)
			if g, err := NewGuard(c, time.Now); err == nil {
				g.Close()
				t.Fatal("invalid relation card passed")
			}
		})
	}
}

func TestRelationRendererMatchesPythonFixtures(t *testing.T) {
	raw, err := os.ReadFile("../tests/fixtures/p42_cards.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixtures []struct {
		Card     dailyCard      `json:"card"`
		Rendered map[string]any `json:"rendered"`
	}
	if err = json.Unmarshal(raw, &fixtures); err != nil {
		t.Fatal(err)
	}
	for _, fixture := range fixtures {
		if !cardBinding(&fixture.Card) {
			t.Fatal("Python binding was rejected")
		}
		rendered, _ := json.Marshal(cardJSON(&fixture.Card))
		var normalized map[string]any
		json.Unmarshal(rendered, &normalized)
		if !reflect.DeepEqual(normalized, fixture.Rendered) {
			t.Fatal("Python and Go card renderers differ")
		}
	}
}

func TestRelationCardLongCandidatesPassThePhysicalBodyBoundary(t *testing.T) {
	c := relationControl(t, "send", true)
	c.Card.Binding["details"] = strings.Repeat("合成资料", 800)
	c.Card.Binding["candidate_count"] = float64(5)
	raw, _ := json.Marshal(cardJSON(c.Card))
	c.Card.Content = string(raw)
	var body map[string]string
	json.Unmarshal([]byte(c.Body), &body)
	body["content"] = c.Card.Content
	raw, _ = json.Marshal(body)
	c.Body = string(raw)
	if len(c.Body) <= 8192 {
		t.Fatal("fixture must exceed the legacy limit")
	}
	rebindDaily(t, &c)
	g := guard(t, c)
	defer g.Close()
	req, _ := http.NewRequest("POST", "https://open.feishu.cn"+c.Path, strings.NewReader(c.Body))
	success(t, g, req, `{"code":0,"data":{}}`)
}
