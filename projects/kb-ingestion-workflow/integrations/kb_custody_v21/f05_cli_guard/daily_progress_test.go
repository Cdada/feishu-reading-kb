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

func progressControl(t *testing.T) Control {
	old := pilotList(dailyControl(t))
	rebindDaily(t, &old)
	g := guard(t, old)
	body := `{"code":0,"data":{"has_more":false,"items":[{"message_id":"om_progress","chat_id":"oc_synthetic","msg_type":"text","deleted":false,"updated":false,"sender":{"id":"ou_synthetic_bot","id_type":"open_id","sender_type":"app"},"body":{"content":"{\"text\":\"PDF 处理进度\\n0%\"}"}}]}}`
	success(t, g, get(old), body)
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
	rules["pdf"] = map[string]any{"progress": true}
	raw, _ = json.Marshal(rules)
	freezePilot(t, &c, p.Daily.Rules, raw)
	p.Daily.RulesSHA = c.Frozen[p.Daily.Rules]
	raw, _ = json.Marshal(p)
	freezePilot(t, &c, c.Session, raw)
	c.Mode, c.Kind, c.Key = "update", "send", "p001-send"
	c.Path = "/open-apis/im/v1/messages/om_progress"
	c.Body = `{"msg_type":"text","content":"{\"text\":\"PDF 处理进度\\n10%\"}"}`
	c.Progress = &dailyProgress{MessageID: "om_progress", OriginReceipt: receipt, InitialText: "PDF 处理进度\n0%", PreviousText: "PDF 处理进度\n0%"}
	rebindDaily(t, &c)
	return c
}

func TestDailyProgressOnlyOwnedFrozenTextCanBeUpdated(t *testing.T) {
	for name, change := range map[string]func(*Control){
		"valid":           func(c *Control) {},
		"unbound-target":  func(c *Control) { c.Progress.MessageID = "om_other"; c.Path = "/open-apis/im/v1/messages/om_other" },
		"changed-origin":  func(c *Control) { c.Progress.InitialText = "PDF 处理进度\nother" },
		"missing-receipt": func(c *Control) { delete(c.Frozen, c.Progress.OriginReceipt) },
		"missing-journal": func(c *Control) {
			delete(c.Frozen, filepath.Join(filepath.Dir(c.Progress.OriginReceipt), "http.local.jsonl"))
		},
		"unrelated-body":      func(c *Control) { c.Body = `{"msg_type":"text","content":"{\"text\":\"other\"}"}` },
		"card-body":           func(c *Control) { c.Body = strings.Replace(c.Body, `"msg_type":"text"`, `"msg_type":"interactive"`, 1) },
		"extra-receiver":      func(c *Control) { c.Body = strings.Replace(c.Body, "{", `{"receive_id":"oc_other",`, 1) },
		"query":               func(c *Control) { c.Path += "?other=1" },
		"send-mode":           func(c *Control) { c.Mode = "send" },
		"no-progress-binding": func(c *Control) { c.Progress = nil },
	} {
		t.Run(name, func(t *testing.T) {
			c := progressControl(t)
			change(&c)
			rebindDaily(t, &c)
			g, err := NewGuard(c, time.Now)
			if g != nil {
				g.Close()
			}
			if (err == nil) != (name == "valid") {
				t.Fatal(name, err)
			}
		})
	}
}

func TestDailyProgressRequiresExplicitStandingOptIn(t *testing.T) {
	c := progressControl(t)
	var p pilotSession
	raw, _ := os.ReadFile(c.Session)
	json.Unmarshal(raw, &p)
	var rules map[string]any
	raw, _ = os.ReadFile(p.Daily.Rules)
	json.Unmarshal(raw, &rules)
	delete(rules, "pdf")
	raw, _ = json.Marshal(rules)
	freezePilot(t, &c, p.Daily.Rules, raw)
	p.Daily.RulesSHA = c.Frozen[p.Daily.Rules]
	raw, _ = json.Marshal(p)
	freezePilot(t, &c, c.Session, raw)
	rebindDaily(t, &c)
	if g, err := NewGuard(c, time.Now); err == nil {
		g.Close()
		t.Fatal("update without approved progress profile")
	}
}

func TestDailyProgressPhysicalPUTAndExactBody(t *testing.T) {
	for _, mode := range []string{"valid", "post", "changed-body", "wrong-host"} {
		t.Run(mode, func(t *testing.T) {
			c := progressControl(t)
			g := guard(t, c)
			defer g.Close()
			method, url, body := "PUT", "https://open.feishu.cn"+c.Path, c.Body
			if mode == "post" {
				method = "POST"
			}
			if mode == "changed-body" {
				body = strings.Replace(body, "10%", "20%", 1)
			}
			if mode == "wrong-host" {
				url = "https://example.com" + c.Path
			}
			r, _ := http.NewRequest(method, url, strings.NewReader(body))
			if mode == "valid" {
				success(t, g, r, `{"code":0,"data":{}}`)
				args := strings.Join(cliArgs(c), " ")
				if !strings.Contains(args, "api PUT /open-apis/im/v1/messages/om_progress") || strings.Contains(args, "receive_id_type") {
					t.Fatal(args)
				}
			} else if _, err := g.PreRoundTripE(r); err == nil {
				t.Fatal("invalid update reached HTTP")
			}
		})
	}
}
