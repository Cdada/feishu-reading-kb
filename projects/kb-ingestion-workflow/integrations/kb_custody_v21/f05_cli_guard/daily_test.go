package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func dailyControl(t *testing.T) Control {
	c := noticeControl(t)
	c.Mode, c.Kind, c.Key = "read", "identity", "p001-identity"
	c.Path, c.Body, c.Notice = "/open-apis/bot/v3/info", "", nil
	var p pilotSession
	raw, _ := os.ReadFile(c.Session)
	json.Unmarshal(raw, &p)
	p.ExpiresAt = c.StartsAt.Add(10 * time.Minute)
	c.ExpiresAt = p.ExpiresAt
	p.Limits["send"] = 0
	p.Limits["identity"] = 1
	d := &dailyDelegation{Rules: filepath.Join(c.Root, "rules.local.json"), Reservation: filepath.Join(c.Root, "reservation.local.json")}
	rules := map[string]any{"reference": "P32-standing-v1", "approved": true, "approval_note": "synthetic offline approval",
		"cli_profile": p.Profile, "policy": map[string]any{"app_id": p.AppID, "chat_id": p.ChatID, "account_id": p.AccountID, "bot_id": p.BotID, "paid_model_cny": 0, "retry_attempts": 15}}
	raw, _ = json.Marshal(rules)
	freezePilot(t, &c, d.Rules, raw)
	d.RulesSHA = c.Frozen[d.Rules]
	p.Daily = d
	raw, _ = json.Marshal(p)
	freezePilot(t, &c, c.Session, raw)
	rebindDaily(t, &c)
	return c
}

func rebindDaily(t *testing.T, c *Control) {
	var p pilotSession
	raw, _ := os.ReadFile(c.Session)
	json.Unmarshal(raw, &p)
	for k := range p.Limits {
		p.Limits[k] = 0
	}
	p.Limits["auth"], p.Limits[c.Kind] = 1, 1
	p.StartsAt, p.ExpiresAt = c.StartsAt, c.ExpiresAt
	raw, _ = json.Marshal(p)
	freezePilot(t, c, c.Session, raw)
	body := sha256.Sum256([]byte(c.Body))
	reservation := map[string]any{"reference": "P32-request-v1", "rules_path": p.Daily.Rules, "rules_sha256": p.Daily.RulesSHA,
		"root": c.Root, "operation_key": "synthetic-operation", "attempt": 1, "kind": c.Kind, "mode": c.Mode, "path": c.Path,
		"body_sha256": hex.EncodeToString(body[:]), "maximum_http": 2}
	raw, _ = json.Marshal(reservation)
	freezePilot(t, c, p.Daily.Reservation, raw)
}

func TestDailyStandingParentCheckedBeforeCredentialOrHTTP(t *testing.T) {
	for _, change := range []string{"none", "unapproved", "identity", "paid", "attempt16", "body", "unfrozen-parent"} {
		t.Run(change, func(t *testing.T) {
			c := dailyControl(t)
			var p pilotSession
			raw, _ := os.ReadFile(c.Session)
			json.Unmarshal(raw, &p)
			if change == "unfrozen-parent" {
				delete(c.Frozen, p.Daily.Rules)
			} else if change == "attempt16" || change == "body" {
				var r map[string]any
				raw, _ = os.ReadFile(p.Daily.Reservation)
				json.Unmarshal(raw, &r)
				if change == "attempt16" {
					r["attempt"] = 16
				} else {
					r["body_sha256"] = strings.Repeat("0", 64)
				}
				raw, _ = json.Marshal(r)
				freezePilot(t, &c, p.Daily.Reservation, raw)
			} else if change != "none" {
				var r map[string]any
				raw, _ = os.ReadFile(p.Daily.Rules)
				json.Unmarshal(raw, &r)
				switch change {
				case "unapproved":
					r["approved"] = false
				case "identity":
					r["policy"].(map[string]any)["chat_id"] = "oc_other"
				case "paid":
					r["policy"].(map[string]any)["paid_model_cny"] = 1
				}
				raw, _ = json.Marshal(r)
				freezePilot(t, &c, p.Daily.Rules, raw)
				p.Daily.RulesSHA = c.Frozen[p.Daily.Rules]
				raw, _ = json.Marshal(p)
				freezePilot(t, &c, c.Session, raw)
				rebindDaily(t, &c)
			}
			g, err := NewGuard(c, time.Now)
			if g != nil {
				g.Close()
			}
			if (err == nil) != (change == "none") {
				t.Fatal(change, err)
			}
			if change != "none" {
				if _, err = os.Stat(filepath.Join(c.Root, "http.local.jsonl")); !os.IsNotExist(err) {
					t.Fatal("HTTP evidence unexpectedly created")
				}
			}
		})
	}
}

func dailyHistoryOrigin(t *testing.T) (Control, string) {
	c := pilotList(dailyControl(t))
	rebindDaily(t, &c)
	g := guard(t, c)
	body := `{"code":0,"data":{"has_more":false,"items":[{"message_id":"om_known_bot","thread_id":"omt_known","chat_id":"oc_synthetic","deleted":false,"sender":{"id":"ou_synthetic_bot","id_type":"open_id","sender_type":"app"}}]}}`
	success(t, g, get(c), body)
	g.Close()
	path := filepath.Join(c.Root, c.Key+".response.local")
	raw, _ := os.ReadFile(path)
	freezePilot(t, &c, path, raw)
	journal := filepath.Join(c.Root, "http.local.jsonl")
	raw, _ = os.ReadFile(journal)
	freezePilot(t, &c, journal, raw)
	return c, path
}

func TestDailyForeignReceiptAndKnownThreadRemainScoped(t *testing.T) {
	for _, mode := range []string{"thread", "bot-detail", "unknown-thread", "thread-time-range", "missing-origin-approval", "changed-origin-journal"} {
		t.Run(mode, func(t *testing.T) {
			old, _ := dailyHistoryOrigin(t)
			c := dailyControl(t)
			for path, hash := range old.Frozen {
				c.Frozen[path] = hash
			}
			c.Kind, c.Key = "list", "p001-list"
			q := url.Values{"container_id_type": {"thread"}, "container_id": {"omt_known"}, "page_size": {"50"}, "sort_type": {"ByCreateTimeAsc"}}
			if mode == "unknown-thread" {
				q.Set("container_id", "omt_other")
			}
			if mode == "thread-time-range" {
				q.Set("start_time", "1")
			}
			c.Path = "/open-apis/im/v1/messages?" + q.Encode()
			if mode == "bot-detail" {
				c.Kind, c.Key, c.Path = "message", "p001-message", "/open-apis/im/v1/messages/om_known_bot"
			}
			if mode == "missing-origin-approval" {
				delete(c.Frozen, old.Session)
			}
			if mode == "changed-origin-journal" {
				c.Frozen[filepath.Join(old.Root, "http.local.jsonl")] = strings.Repeat("a", 64)
			}
			rebindDaily(t, &c)
			g, err := NewGuard(c, time.Now)
			if g != nil {
				g.Close()
			}
			if (err == nil) != (mode == "thread" || mode == "bot-detail") {
				t.Fatal(mode, err)
			}
		})
	}
}

func TestDailyHistorySevenDaysAndRateLimitWait(t *testing.T) {
	c := pilotList(dailyControl(t))
	u, _ := url.Parse(c.Path)
	q := u.Query()
	q.Set("start_time", strconv.FormatInt(c.StartsAt.Add(-6*24*time.Hour).Unix(), 10))
	u.RawQuery = q.Encode()
	c.Path = u.String()
	rebindDaily(t, &c)
	g := guard(t, c)
	defer g.Close()
	done, err := g.PreRoundTripE(get(c))
	if err != nil {
		t.Fatal(err)
	}
	reply := response(429, `{"code":99991400}`)
	reply.Header.Set("x-ogw-ratelimit-reset", "123")
	done(reply, nil)
	events, err := g.events()
	if err != nil || len(events) != 2 || events[1].RetryAfter != 123 || events[1].Type != "stop" {
		t.Fatal(events, err)
	}
}
