package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func control(t *testing.T) Control {
	t.Helper()
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	root := filepath.Join(base, ".dev-runtime", "f05-requests")
	if err = os.MkdirAll(root, 0700); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(base, "frozen.synthetic")
	if err = os.WriteFile(p, []byte("synthetic"), 0600); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("synthetic"))
	return Control{Mode: "read", Root: root, Profile: "synthetic", AppID: "cli_synthetic",
		Kind: "bot", Key: "bot", Path: "/open-apis/bot/v3/info", StartsAt: time.Now().Add(-time.Minute),
		ExpiresAt: time.Now().Add(time.Hour), Frozen: map[string]string{p: hex.EncodeToString(sum[:])}}
}
func guard(t *testing.T, c Control) *Guard {
	t.Helper()
	g, e := NewGuard(c, time.Now)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(g.Close)
	return g
}
func get(c Control) *http.Request {
	r, _ := http.NewRequest("GET", "https://open.feishu.cn"+c.Path, nil)
	return r
}
func auth(c Control) *http.Request {
	f := url.Values{"client_id": {c.AppID}, "client_secret": {"synthetic-secret"}, "grant_type": {"client_credentials"}}
	r, _ := http.NewRequest("POST", "https://accounts.feishu.cn/oauth/v3/token", strings.NewReader(f.Encode()))
	return r
}
func response(status int, body string) *http.Response {
	return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}
}
func success(t *testing.T, g *Guard, r *http.Request, body string) {
	t.Helper()
	post, e := g.PreRoundTripE(r)
	if e != nil {
		t.Fatal(e)
	}
	if !r.Close || r.Header.Get("Connection") != "close" {
		t.Fatal("connection replay protection absent")
	}
	post(response(200, body), nil)
	if g.failed {
		t.Fatal("unexpected failure")
	}
}
func TestAuthAndReadCountSeparatelyWithoutSavingCredentials(t *testing.T) {
	c := control(t)
	g := guard(t, c)
	success(t, g, auth(c), `{"access_token":"synthetic-token"}`)
	success(t, g, get(c), `{"code":0,"bot":{"open_id":"ou_synthetic_bot"}}`)
	events, e := g.events()
	if e != nil || len(events) != 4 || events[0].Kind != "auth" || events[2].Kind != "bot" {
		t.Fatalf("events: %v %v", events, e)
	}
	files, _ := os.ReadDir(c.Root)
	for _, f := range files {
		b, _ := os.ReadFile(filepath.Join(c.Root, f.Name()))
		if bytes.Contains(b, []byte("synthetic-secret")) || bytes.Contains(b, []byte("synthetic-token")) {
			t.Fatal("credential persisted")
		}
	}
	if _, e = g.PreRoundTripE(get(c)); e == nil {
		t.Fatal("repeated read allowed")
	}
}
func TestRedirectAndHTTPFailuresStopBeforeAnyFollowup(t *testing.T) {
	for _, status := range []int{301, 302, 307, 429, 500} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			c := control(t)
			g := guard(t, c)
			post, e := g.PreRoundTripE(get(c))
			if e != nil {
				t.Fatal(e)
			}
			post(response(status, `{"code":0}`), nil)
			follow := get(c)
			follow.Response = response(status, "")
			if _, e = g.PreRoundTripE(follow); e == nil {
				t.Fatal("followup allowed")
			}
			events, _ := g.events()
			if len(events) != 2 || events[1].Type != "stop" {
				t.Fatal(events)
			}
		})
	}
}
func TestTimeoutMalformedAndAPIFailureCannotBecomeSuccess(t *testing.T) {
	for _, body := range []string{`{"code":999}`, `broken`, strings.Repeat("x", 1024*1024+1), "timeout"} {
		t.Run(body[:min(len(body), 12)], func(t *testing.T) {
			c := control(t)
			g := guard(t, c)
			post, e := g.PreRoundTripE(get(c))
			if e != nil {
				t.Fatal(e)
			}
			if body == "timeout" {
				post(nil, errors.New("synthetic timeout"))
			} else {
				post(response(200, body), nil)
			}
			if _, e = g.PreRoundTripE(get(c)); e == nil {
				t.Fatal("retry allowed")
			}
			events, _ := g.events()
			if len(events) != 2 || events[1].Type != "stop" {
				t.Fatal(events)
			}
		})
	}
}
func TestProfileAndForeignTargetsAreDeniedBeforeIntent(t *testing.T) {
	for _, change := range []func(*http.Request){
		func(r *http.Request) { r.Method = "PUT" }, func(r *http.Request) { r.URL.Host = "example.invalid" },
		func(r *http.Request) { r.URL.Path += "/other" }, func(r *http.Request) { r.URL.RawQuery = "page_token=other" },
		func(r *http.Request) { r.Response = response(302, "") },
	} {
		c := control(t)
		g := guard(t, c)
		r := get(c)
		change(r)
		if _, e := g.PreRoundTripE(r); e == nil {
			t.Fatal("foreign request allowed")
		}
		ev, _ := g.events()
		if len(ev) != 0 {
			t.Fatal(ev)
		}
	}
	c := control(t)
	c.Mode = "profile"
	g := guard(t, c)
	if _, e := g.PreRoundTripE(auth(c)); e == nil {
		t.Fatal("profile used network")
	}
	c = control(t)
	g = guard(t, c)
	wrong := c
	wrong.AppID = "cli_other"
	if _, e := g.PreRoundTripE(auth(wrong)); e == nil {
		t.Fatal("foreign credential allowed")
	}
}
func TestExpiredOrChangedEvidenceStopsBeforeNetworking(t *testing.T) {
	c := control(t)
	g := guard(t, c)
	g.now = func() time.Time { return c.ExpiresAt }
	if _, e := g.PreRoundTripE(get(c)); e == nil {
		t.Fatal("expired allowed")
	}
	c = control(t)
	g = guard(t, c)
	for p := range c.Frozen {
		if e := os.WriteFile(p, []byte("changed"), 0600); e != nil {
			t.Fatal(e)
		}
	}
	if _, e := g.PreRoundTripE(get(c)); e == nil {
		t.Fatal("changed evidence allowed")
	}
}
func TestConcurrentAndCompletedHelperCannotRestart(t *testing.T) {
	c := control(t)
	g := guard(t, c)
	other := c
	other.Key = "chat"
	other.Kind = "chat"
	other.Path = "/open-apis/im/v1/chats/oc_synthetic"
	if x, e := NewGuard(other, time.Now); e == nil {
		x.Close()
		t.Fatal("concurrent helper allowed")
	}
	success(t, g, get(c), `{"code":0}`)
	g.Close()
	if x, e := NewGuard(c, time.Now); e == nil {
		x.Close()
		t.Fatal("restart allowed")
	}
}
func TestCategoryBudgetCannotBorrowAndUnresolvedBlocks(t *testing.T) {
	c := control(t)
	g := guard(t, c)
	for i := 0; i < 5; i++ {
		k := string(rune('a'+i)) + ":auth"
		if e := g.append(Event{Type: "intent", Key: k, Kind: "auth"}); e != nil {
			t.Fatal(e)
		}
		if e := g.append(Event{Type: "complete", Key: k, Kind: "auth", Status: 200}); e != nil {
			t.Fatal(e)
		}
	}
	if _, e := g.PreRoundTripE(auth(c)); e == nil {
		t.Fatal("auth budget exceeded")
	}
	c = control(t)
	g = guard(t, c)
	if e := g.append(Event{Type: "intent", Key: "previous:message", Kind: "message"}); e != nil {
		t.Fatal(e)
	}
	if _, e := g.PreRoundTripE(get(c)); e == nil {
		t.Fatal("unresolved allowed")
	}
}
func TestHardExitAfterIntentCannotResetBudget(t *testing.T) {
	if raw := os.Getenv("F05_SYNTHETIC_CRASH"); raw != "" {
		var c Control
		if json.Unmarshal([]byte(raw), &c) != nil {
			os.Exit(70)
		}
		g, e := NewGuard(c, time.Now)
		if e != nil {
			os.Exit(71)
		}
		if _, e = g.PreRoundTripE(get(c)); e != nil {
			os.Exit(72)
		}
		os.Exit(77)
	}
	c := control(t)
	b, _ := json.Marshal(c)
	cmd := exec.Command(os.Args[0], "-test.run=^TestHardExitAfterIntentCannotResetBudget$")
	cmd.Env = append(os.Environ(), "F05_SYNTHETIC_CRASH="+string(b))
	e := cmd.Run()
	var status *exec.ExitError
	if !errors.As(e, &status) || status.ExitCode() != 77 {
		t.Fatalf("child: %v", e)
	}
	if g, e := NewGuard(c, time.Now); e == nil {
		g.Close()
		t.Fatal("dead helper restarted")
	}
	c.Key = "chat"
	c.Kind = "chat"
	c.Path = "/open-apis/im/v1/chats/oc_synthetic"
	g := guard(t, c)
	if _, e = g.PreRoundTripE(get(c)); e == nil {
		t.Fatal("unresolved prior allowed")
	}
}
