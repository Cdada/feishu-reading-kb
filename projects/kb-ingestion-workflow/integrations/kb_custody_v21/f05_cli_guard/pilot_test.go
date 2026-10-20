package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func freezePilot(t *testing.T, c *Control, path string, body []byte) {
	t.Helper()
	if e := os.WriteFile(path, body, 0600); e != nil {
		t.Fatal(e)
	}
	h := sha256.Sum256(body)
	c.Frozen[path] = hex.EncodeToString(h[:])
}
func pilotControl(t *testing.T) Control {
	t.Helper()
	c := control(t)
	c.Reference = "N10"
	c.Root = filepath.Join(filepath.Dir(c.Root), "n10-requests")
	if e := os.Mkdir(c.Root, 0700); e != nil {
		t.Fatal(e)
	}
	c.Key = "p001-identity"
	c.Kind = "identity"
	c.Session = filepath.Join(c.Root, "approval.local.json")
	c.ExpiresAt = c.StartsAt.Add(10 * time.Hour)
	p := pilotSession{Reference: "N10", Approved: true, Root: c.Root, Profile: c.Profile, AppID: c.AppID,
		ChatID: "oc_synthetic", AccountID: "ou_synthetic_user", BotID: "ou_synthetic_bot", StartsAt: c.StartsAt, ExpiresAt: c.ExpiresAt}
	b, _ := json.Marshal(p)
	freezePilot(t, &c, c.Session, b)
	return c
}
func pilotList(c Control) Control {
	c.Key = "p002-list"
	c.Kind = "list"
	q := url.Values{"container_id": {"oc_synthetic"}, "container_id_type": {"chat"}, "page_size": {"50"},
		"sort_type": {"ByCreateTimeAsc"}, "start_time": {strconv.FormatInt(c.StartsAt.Add(-time.Hour).Unix(), 10)},
		"end_time": {strconv.FormatInt(c.StartsAt.Unix(), 10)}}
	c.Path = "/open-apis/im/v1/messages?" + q.Encode()
	return c
}

func TestPilotScopeAndWindowRejectBeforeRequests(t *testing.T) {
	for name, change := range map[string]func(*Control){
		"foreign-app":   func(c *Control) { c.AppID = "cli_other" },
		"window-change": func(c *Control) { c.ExpiresAt = c.ExpiresAt.Add(time.Second) },
		"foreign-chat":  func(c *Control) { c.Path = strings.ReplaceAll(c.Path, "oc_synthetic", "oc_other") },
		"extra-query":   func(c *Control) { c.Path += "&user_id_type=user_id" },
		"excess-page":   func(c *Control) { c.Path = strings.ReplaceAll(c.Path, "page_size=50", "page_size=100") },
		"older-history": func(c *Control) {
			u, _ := url.Parse(c.Path)
			q := u.Query()
			q.Set("start_time", strconv.FormatInt(c.StartsAt.Add(-25*time.Hour).Unix(), 10))
			u.RawQuery = q.Encode()
			c.Path = u.String()
		},
		"send": func(c *Control) { c.Mode = "send" },
		"forged-id": func(c *Control) {
			c.Kind = "message"
			c.Key = "p003-message"
			c.Path = "/open-apis/im/v1/messages/om_unknown"
		},
	} {
		t.Run(name, func(t *testing.T) {
			c := pilotList(pilotControl(t))
			change(&c)
			if g, e := NewGuard(c, time.Now); e == nil {
				g.Close()
				t.Fatal("scope accepted")
			}
			if _, e := os.Stat(filepath.Join(c.Root, "http.local.jsonl")); !os.IsNotExist(e) {
				t.Fatal("HTTP intent created")
			}
		})
	}
}

func TestPilotExactMessageRequiresActualBoundListReceipt(t *testing.T) {
	for _, mode := range []string{"success", "no-journal", "wrong-hash", "foreign-sender", "foreign-chat"} {
		t.Run(mode, func(t *testing.T) {
			c := pilotList(pilotControl(t))
			g := guard(t, c)
			body := `{"code":0,"data":{"items":[{"message_id":"om_synthetic","chat_id":"oc_synthetic","sender":{"id":"ou_synthetic_user","sender_type":"user","id_type":"open_id"}}]}}`
			if mode == "foreign-sender" {
				body = strings.ReplaceAll(body, "ou_synthetic_user", "ou_other")
			}
			if mode == "foreign-chat" {
				body = strings.ReplaceAll(body, "oc_synthetic", "oc_other")
			}
			path := filepath.Join(c.Root, c.Key+".response.local")
			if mode == "no-journal" {
				freezePilot(t, &c, path, []byte(body))
			} else {
				success(t, g, get(c), body)
				h := sha256.Sum256([]byte(body))
				c.Frozen[path] = hex.EncodeToString(h[:])
			}
			if mode == "wrong-hash" {
				freezePilot(t, &c, path, []byte(body+" "))
			}
			g.Close()
			c.Key = "p003-message"
			c.Kind = "message"
			c.Path = "/open-apis/im/v1/messages/om_synthetic"
			next, e := NewGuard(c, time.Now)
			if next != nil {
				next.Close()
			}
			if (e == nil) != (mode == "success") {
				t.Fatalf("mode %s: %v", mode, e)
			}
		})
	}
}

func TestPilotExtensionRetainsSpentBudgetAndPendingProtection(t *testing.T) {
	c := pilotList(pilotControl(t))
	g := guard(t, c)
	for i := 0; i < 12; i++ {
		k := fmt.Sprintf("old%d:list", i)
		if e := g.append(Event{Type: "intent", Key: k, Kind: "list"}); e != nil {
			t.Fatal(e)
		}
		if e := g.append(Event{Type: "complete", Key: k, Kind: "list", Status: 200}); e != nil {
			t.Fatal(e)
		}
	}
	if g.budgets()["list"] != 12 {
		t.Fatal("expanded without evidence")
	}
	if e := g.reserve("list"); e == nil {
		t.Fatal("borrowed unused category")
	}
	p := filepath.Join(c.Root, "extension.local.json")
	b, _ := json.Marshal(map[string]any{"reference": "N10", "reason": "Synthetic forecast needs more than 50 physical calls", "maximum_total": 100, "prior_attempts": 12, "approval_sha256": c.Frozen[c.Session]})
	freezePilot(t, &c, p, b)
	if g.budgets()["list"] != 24 {
		t.Fatal("approved extension not applied")
	}
	if e := g.reserve("list"); e != nil {
		t.Fatal(e)
	}
	// A new key and an extended ceiling cannot hide an unfinished physical call.
	g.c.Key = "p004-list"
	if e := g.reserve("list"); e == nil {
		t.Fatal("extension retried pending call")
	}
	ev, _ := g.events()
	n := 0
	for _, e := range ev {
		if e.Type == "intent" {
			n++
		}
	}
	if n != 13 {
		t.Fatal("budget history reset", n)
	}
}

func TestPilotFullHundredCallJournalAndHardCeiling(t *testing.T) {
	c := pilotControl(t)
	g := guard(t, c)
	for kind, cap := range pilotBaseLimits {
		for i := 0; i < cap; i++ {
			k := fmt.Sprintf("base%d:%s", i, kind)
			g.append(Event{Type: "intent", Key: k, Kind: kind})
			g.append(Event{Type: "complete", Key: k, Kind: kind, Status: 200})
		}
	}
	b, _ := json.Marshal(map[string]any{"reference": "N10", "reason": "All fifty used; finish existing scope", "maximum_total": 100, "prior_attempts": 50, "approval_sha256": c.Frozen[c.Session]})
	freezePilot(t, &c, filepath.Join(c.Root, "extension.local.json"), b)
	for kind, cap := range pilotBaseLimits {
		for i := 0; i < cap; i++ {
			g.c.Key = fmt.Sprintf("extra%d-%s", i, kind)
			if e := g.reserve(kind); e != nil {
				t.Fatal(e)
			}
			g.append(Event{Type: "complete", Key: g.c.Key + ":" + kind, Kind: kind, Status: 200})
		}
	}
	ev, e := g.events()
	if e != nil || len(ev) != 200 {
		t.Fatal("hundred-call ledger rejected", len(ev), e)
	}
	for kind := range pilotBaseLimits {
		if e := g.reserve(kind); e == nil {
			t.Fatal("ceiling exceeded", kind)
		}
	}
}

func TestDeadlineCrossingDuringHashOrReservationNeverPermitsNetwork(t *testing.T) {
	for _, stage := range []string{"hash", "reservation"} {
		t.Run(stage, func(t *testing.T) {
			c := pilotControl(t)
			g := guard(t, c)
			calls := 0
			g.now = func() time.Time {
				calls++
				cut := 2
				if stage == "reservation" {
					cut = 5
				}
				if calls >= cut {
					return c.ExpiresAt
				}
				return c.StartsAt.Add(time.Minute)
			}
			if _, e := g.PreRoundTripE(get(c)); e == nil {
				t.Fatal("expired physical call allowed")
			}
			ev, _ := g.events()
			want := 0
			if stage == "reservation" {
				want = 1
			}
			if len(ev) != want {
				t.Fatalf("stage=%s events=%d", stage, len(ev))
			}
		})
	}
}

func TestPilotProcessDeathCannotRestartOrResetUnderNewKey(t *testing.T) {
	c := pilotControl(t)
	b, _ := json.Marshal(c)
	child := exec.Command(os.Args[0], "-test.run=^TestHardExitAfterIntentCannotResetBudget$")
	child.Env = append(os.Environ(), "F05_SYNTHETIC_CRASH="+string(b))
	err := child.Run()
	var stopped *exec.ExitError
	if !errors.As(err, &stopped) || stopped.ExitCode() != 77 {
		t.Fatal("unexpected child exit", err)
	}
	if g, e := NewGuard(c, time.Now); e == nil {
		g.Close()
		t.Fatal("restarted killed command")
	}
	c.Key = "p002-identity"
	g := guard(t, c)
	if _, e := g.PreRoundTripE(get(c)); e == nil {
		t.Fatal("new key hid unresolved physical attempt")
	}
	events, e := g.events()
	if e != nil || len(events) != 1 || events[0].Type != "intent" {
		t.Fatal("death evidence changed", events, e)
	}
}

func TestFullSourceControlInventoryIsBoundedAndNeverTruncated(t *testing.T) {
	c := pilotControl(t)
	for i := 0; i < 300; i++ {
		c.Frozen["/synthetic/"+strings.Repeat("source", 30)+fmt.Sprint(i)] = strings.Repeat("a", 64)
	}
	b, _ := json.Marshal(c)
	if len(b) <= 65536 {
		t.Fatal("test does not exercise larger inventory")
	}
	got, e := readControl(bytes.NewReader(b))
	if e != nil || len(got.Frozen) != len(c.Frozen) {
		t.Fatal("complete source binding lost", e)
	}
	for _, bad := range [][]byte{append(b, []byte(` {}`)...), []byte(`{"unknown":1}`), append(b, bytes.Repeat([]byte(" "), 1024*1024)...)} {
		if _, e := readControl(bytes.NewReader(bad)); e == nil {
			t.Fatal("invalid or over-limit envelope accepted")
		}
	}
}
