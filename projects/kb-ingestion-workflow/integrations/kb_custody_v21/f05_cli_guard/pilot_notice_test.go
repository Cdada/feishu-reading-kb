package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func noticeControl(t *testing.T) Control {
	c := pilotControl(t)
	c.Reference = "P11"
	c.Root = filepath.Join(filepath.Dir(c.Root), "p11-requests")
	if e := os.Mkdir(c.Root, 0700); e != nil {
		t.Fatal(e)
	}
	c.Session = filepath.Join(c.Root, "approval.local.json")
	p := pilotSession{Reference: "P11", Approved: true, Root: c.Root, AppID: c.AppID, Profile: c.Profile,
		ChatID: "oc_synthetic", AccountID: "ou_synthetic_user", BotID: "ou_synthetic_bot",
		StartsAt: c.StartsAt, ExpiresAt: c.ExpiresAt, Limits: map[string]int{"auth": 1, "identity": 0, "list": 0, "message": 0, "download": 0, "upload": 0, "send": 1},
		Total: 2, Purposes: []string{"service", "custody", "collection", "publication"}, PublishScope: "only_bound_account"}
	raw, _ := json.Marshal(p)
	freezePilot(t, &c, c.Session, raw)
	c.Mode, c.Kind, c.Key = "send", "send", "p001-send"
	c.Path = "/open-apis/im/v1/messages?receive_id_type=chat_id"
	nid := strings.Repeat("a", 64)
	c.Notice = &pilotNotice{ID: nid, PlanPath: filepath.Join(c.Root, nid+".notice.local.json")}
	plan := map[string]string{"reference": "P11", "purpose": "service", "app_id": p.AppID, "account_id": p.AccountID,
		"target_chat_id": p.ChatID, "notification_id": nid, "provider_request_key": "pilot-svc-" + nid[:40], "payload": "合成暂停提醒"}
	raw, _ = json.Marshal(plan)
	freezePilot(t, &c, c.Notice.PlanPath, raw)
	content, _ := json.Marshal(map[string]string{"text": plan["payload"]})
	raw, _ = json.Marshal(map[string]string{"receive_id": p.ChatID, "msg_type": "text", "uuid": plan["provider_request_key"], "content": string(content)})
	c.Body = string(raw)
	return c
}

func noticeRequest(c Control) *http.Request {
	r, _ := http.NewRequest("POST", "https://open.feishu.cn"+c.Path, strings.NewReader(c.Body))
	return r
}

func TestPilotNoticeAuthSendBudgetAndStableIdentity(t *testing.T) {
	for _, kind := range []string{"service", "custody", "publication"} {
		t.Run(kind, func(t *testing.T) {
			c := noticeControl(t)
			if kind == "publication" {
				raw, _ := os.ReadFile(c.Notice.PlanPath)
				var plan map[string]string
				json.Unmarshal(raw, &plan)
				plan["purpose"] = kind
				raw, _ = json.Marshal(plan)
				freezePilot(t, &c, c.Notice.PlanPath, raw)
			}
			if kind == "custody" {
				raw, _ := os.ReadFile(c.Notice.PlanPath)
				var plan map[string]string
				json.Unmarshal(raw, &plan)
				nid := "notification_" + strings.Repeat("a", 32)
				plan["notification_id"] = nid
				plan["purpose"] = "custody"
				c.Notice = &pilotNotice{ID: nid, PlanPath: filepath.Join(c.Root, nid+".notice.local.json")}
				raw, _ = json.Marshal(plan)
				freezePilot(t, &c, c.Notice.PlanPath, raw)
			}
			g := guard(t, c)
			success(t, g, auth(c), `{"access_token":"synthetic-token"}`)
			success(t, g, noticeRequest(c), `{"code":0,"data":{"message_id":"om_synthetic"}}`)
			events, err := g.events()
			if err != nil || len(events) != 4 || events[0].Kind != "auth" || events[2].Kind != "send" {
				t.Fatal(events, err)
			}
			g.Close()
			c.Key = "p002-send"
			if next, e := NewGuard(c, time.Now); e == nil {
				next.Close()
				t.Fatal("same notice accepted under another key")
			}

		})
	}
}

func TestPilotNoticeRejectsUnboundScopeContentAndOldWindow(t *testing.T) {
	for name, change := range map[string]func(*Control){
		"N10":          func(c *Control) { c.Reference = "N10" },
		"foreign-chat": func(c *Control) { c.Body = strings.ReplaceAll(c.Body, "oc_synthetic", "oc_foreign") },
		"altered-body": func(c *Control) { c.Body = strings.ReplaceAll(c.Body, "合成", "修改") },
		"unbound-plan": func(c *Control) { delete(c.Frozen, c.Notice.PlanPath) },
		"alternate-purpose": func(c *Control) {
			raw, _ := os.ReadFile(c.Notice.PlanPath)
			freezePilot(t, c, c.Notice.PlanPath, []byte(strings.ReplaceAll(string(raw), "service", "unapproved")))
		},
		"negative-budget": func(c *Control) {
			raw, _ := os.ReadFile(c.Session)
			var p pilotSession
			json.Unmarshal(raw, &p)
			p.Limits["send"] = -1
			p.Total = 0
			raw, _ = json.Marshal(p)
			freezePilot(t, c, c.Session, raw)
		},
		"source-changed": func(c *Control) { os.WriteFile(c.Notice.PlanPath, []byte("{}"), 0600) },
		"mixed-preview":  func(c *Control) { c.Preview = &pilotPreview{} },
	} {
		t.Run(name, func(t *testing.T) {
			c := noticeControl(t)
			change(&c)
			if g, e := NewGuard(c, time.Now); e == nil {
				g.Close()
				t.Fatal("invalid plan accepted")
			}
			if _, e := os.Stat(filepath.Join(c.Root, "http.local.jsonl")); !os.IsNotExist(e) {
				t.Fatal("unexpected physical request")
			}
		})
	}
}

func TestPilotNoticePhysicalBodyExpiryAndBudgetNeverRetry(t *testing.T) {
	for _, mode := range []string{"body", "window", "budget"} {
		t.Run(mode, func(t *testing.T) {
			c := noticeControl(t)
			if mode == "budget" {
				raw, _ := os.ReadFile(c.Session)
				var p pilotSession
				json.Unmarshal(raw, &p)
				p.Limits["send"] = 0
				p.Total = 1
				raw, _ = json.Marshal(p)
				freezePilot(t, &c, c.Session, raw)
			}
			g := guard(t, c)
			if mode == "body" {
				c.Body = strings.ReplaceAll(c.Body, "合成", "修改")
			}
			if mode == "window" {
				g.now = func() time.Time { return c.ExpiresAt }
			}
			if _, e := g.PreRoundTripE(noticeRequest(c)); e == nil {
				t.Fatal("invalid physical request accepted")
			}
			events, _ := g.events()
			if len(events) != 0 {
				t.Fatal("request reserved")
			}
		})
	}
}

func TestPilotNoticeHardExitAfterPhysicalSendDoesNotSendAgain(t *testing.T) {
	if raw := os.Getenv("P11_SYNTHETIC_CRASH"); raw != "" {
		var c Control
		if json.Unmarshal([]byte(raw), &c) != nil {
			os.Exit(91)
		}
		g, e := NewGuard(c, time.Now)
		if e != nil {
			os.Exit(92)
		}
		if _, e = g.PreRoundTripE(noticeRequest(c)); e != nil {
			os.Exit(93)
		}
		// Existing transport fault-injection seam: the remote side accepted the
		// request, but this process dies before journaling its response.
		if os.WriteFile(filepath.Join(c.Root, "synthetic-remote-accepted"), []byte("one"), 0600) != nil {
			os.Exit(94)
		}
		os.Exit(77)
	}
	c := noticeControl(t)
	raw, _ := json.Marshal(c)
	child := exec.Command(os.Args[0], "-test.run=^TestPilotNoticeHardExitAfterPhysicalSendDoesNotSendAgain$")
	child.Env = append(os.Environ(), "P11_SYNTHETIC_CRASH="+string(raw))
	err := child.Run()
	var stopped *exec.ExitError
	if !errors.As(err, &stopped) || stopped.ExitCode() != 77 {
		t.Fatal("unexpected child", err)
	}
	for _, key := range []string{"p001-send", "p002-send"} {
		c.Key = key
		if g, e := NewGuard(c, time.Now); e == nil {
			g.Close()
			t.Fatal("unknown write restarted")
		}
	}
	events, e := (&Guard{c: c}).events()
	if e != nil || len(events) != 1 || events[0].Type != "intent" {
		t.Fatal(events, e)
	}
	if raw, e = os.ReadFile(filepath.Join(c.Root, "synthetic-remote-accepted")); e != nil || string(raw) != "one" {
		t.Fatal("remote fixture changed")
	}
}
