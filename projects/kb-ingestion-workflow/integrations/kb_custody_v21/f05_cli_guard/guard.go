package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	transport "github.com/larksuite/cli/extension/transport"
)

type Control struct {
	Reference string            `json:"reference,omitempty"`
	Body      string            `json:"body,omitempty"`
	Mode      string            `json:"mode"`
	Root      string            `json:"root"`
	Profile   string            `json:"profile"`
	AppID     string            `json:"app_id"`
	Kind      string            `json:"kind"`
	Key       string            `json:"key"`
	Path      string            `json:"path"`
	Session   string            `json:"session,omitempty"`
	StartsAt  time.Time         `json:"starts_at"`
	ExpiresAt time.Time         `json:"expires_at"`
	Frozen    map[string]string `json:"frozen"`
	Download  *pilotDownload    `json:"download,omitempty"`
	Preview   *pilotPreview     `json:"preview,omitempty"`
	Notice    *pilotNotice      `json:"notice,omitempty"`
	Progress  *dailyProgress    `json:"progress,omitempty"`
	Card      *dailyCard        `json:"card,omitempty"`
}

type Event struct {
	RetryAfter int                      `json:"retry_after_seconds,omitempty"`
	Type       string                   `json:"type"`
	Key        string                   `json:"key"`
	Kind       string                   `json:"kind"`
	Status     int                      `json:"http_status,omitempty"`
	SHA256     string                   `json:"response_sha256,omitempty"`
	At         time.Time                `json:"at"`
	Download   *pilotDownloadDiagnostic `json:"download_diagnostic,omitempty"`
}

var limits = map[string]int{"auth": 5, "bot": 1, "chat": 1, "members": 1, "message": 2}
var outboxLimits = map[string]int{"auth": 5, "bot": 1, "chat": 1, "members": 1, "send": 1, "message": 1}
var ident = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
var targets = map[string]*regexp.Regexp{
	"bot":     regexp.MustCompile(`^/open-apis/bot/v3/info$`),
	"chat":    regexp.MustCompile(`^/open-apis/im/v1/chats/oc_[A-Za-z0-9_-]+$`),
	"members": regexp.MustCompile(`^/open-apis/im/v1/chats/oc_[A-Za-z0-9_-]+/members\?member_id_type=open_id&page_size=100$`),
	"message": regexp.MustCompile(`^/open-apis/im/v1/messages/om_[A-Za-z0-9_-]+$`),
}

type Guard struct {
	c      Control
	now    func() time.Time
	mu     sync.Mutex
	failed bool
	lock   *os.File
	pilot  *pilotSession
}

func noLinks(p string) error {
	if !filepath.IsAbs(p) || filepath.Clean(p) != p {
		return errors.New("absolute clean path required")
	}
	for q := p; ; q = filepath.Dir(q) {
		s, e := os.Lstat(q)
		if e != nil {
			return e
		}
		if s.Mode()&os.ModeSymlink != 0 {
			return errors.New("symlink forbidden")
		}
		if q == filepath.Dir(q) {
			break
		}
	}
	return nil
}

func NewGuard(c Control, now func() time.Time) (*Guard, error) {
	if pilotReference(c.Reference) {
		return newPilotGuard(c, now)
	}
	if c.Session != "" || c.Download != nil || c.Preview != nil || c.Notice != nil || c.Card != nil {
		return nil, errors.New("session is only valid for the bounded Pilot")
	}
	if c.Reference != "" && c.Reference != "F05" && c.Reference != "F06" {
		return nil, errors.New("unknown experiment")
	}
	if !ident.MatchString(c.Profile) || !strings.HasPrefix(c.AppID, "cli_") || !ident.MatchString(c.AppID) {
		return nil, errors.New("fixed profile and application required")
	}
	g := &Guard{c: c, now: now}
	if c.Mode == "profile" {
		return g, nil
	}
	write := c.Reference == "F06" && c.Mode == "send" && c.Kind == "send" && c.Key == "send" &&
		c.Path == "/open-apis/im/v1/messages?receive_id_type=chat_id" && validSendBody(c.Body)
	read := c.Mode == "read" && targets[c.Kind] != nil && targets[c.Kind].MatchString(c.Path) && c.Body == ""
	if !read && !write {
		return nil, errors.New("read-only control required")
	}
	rootName := "f05-requests"
	if c.Reference == "F06" {
		rootName = "f06-requests"
	}
	if (c.Key != "bot" && c.Key != "chat" && c.Key != "members" && c.Key != "notification-0" && c.Key != "notification-1" && !write) ||
		!strings.Contains(c.Root, string(filepath.Separator)+".dev-runtime"+string(filepath.Separator)) ||
		filepath.Base(c.Root) != rootName || len(c.Frozen) == 0 || c.Reference == "F06" && c.Key == "notification-1" {
		return nil, errors.New("fixed one-shot control required")
	}
	want := c.Key
	if strings.HasPrefix(c.Key, "notification-") {
		want = "message"
	}
	if c.Kind != want {
		return nil, errors.New("command category differs")
	}
	if err := noLinks(c.Root); err != nil {
		return nil, err
	}
	if err := g.check(); err != nil {
		return nil, err
	}
	lockPath := filepath.Join(c.Root, "http.lock.local")
	if _, e := os.Lstat(lockPath); e == nil {
		if e = noLinks(lockPath); e != nil {
			return nil, e
		}
	}
	lock, e := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return nil, e
	}
	if e = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		lock.Close()
		return nil, e
	}
	g.lock = lock
	// A killed or completed helper cannot be launched again for this command.
	f, e := os.OpenFile(filepath.Join(c.Root, c.Key+".started.local"), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if e != nil {
		g.Close()
		return nil, e
	}
	if e = f.Sync(); e == nil {
		e = f.Close()
	} else {
		f.Close()
	}
	if e == nil {
		e = syncDir(c.Root)
	}
	if e != nil {
		g.Close()
		return nil, e
	}
	return g, nil
}

func validSendBody(body string) bool {
	var v map[string]string
	if len(body) > 8192 || json.Unmarshal([]byte(body), &v) != nil || len(v) != 4 {
		return false
	}
	var content map[string]string
	return regexp.MustCompile(`^oc_[A-Za-z0-9_-]+$`).MatchString(v["receive_id"]) && v["msg_type"] == "text" &&
		regexp.MustCompile(`^custody-[a-f0-9]{40}$`).MatchString(v["uuid"]) &&
		json.Unmarshal([]byte(v["content"]), &content) == nil && len(content) == 1 &&
		content["text"] == "【V2.1 S6 测试】原始文件已安全进入待归档区，飞书附件可按需清理。"
}

func (g *Guard) budgets() map[string]int {
	if g.pilot != nil {
		return g.pilotLimits()
	}
	if g.c.Reference == "F06" {
		return outboxLimits
	}
	return limits
}

func (g *Guard) Close() {
	if g.lock != nil {
		g.lock.Close()
		g.lock = nil
	}
}

func (g *Guard) Name() string                                             { return "kb-v21-f05-request-guard" }
func (g *Guard) ResolveInterceptor(context.Context) transport.Interceptor { return g }
func (g *Guard) PreRoundTrip(*http.Request) func(*http.Response, error)   { return nil }

func (g *Guard) check() error {
	if g.failed {
		return errors.New("channel stopped")
	}
	n := g.now()
	maximum := 2 * time.Hour
	if g.pilot != nil {
		maximum = 10 * time.Hour
	}
	if !g.c.ExpiresAt.After(g.c.StartsAt) || g.c.ExpiresAt.Sub(g.c.StartsAt) > maximum ||
		n.Before(g.c.StartsAt) || !n.Before(g.c.ExpiresAt) {
		return errors.New("outside approved window")
	}
	for p, want := range g.c.Frozen {
		if err := noLinks(p); err != nil {
			return err
		}
		f, e := os.Open(p)
		if e != nil {
			return e
		}
		h := sha256.New()
		_, e = io.Copy(h, f)
		f.Close()
		if e != nil || hex.EncodeToString(h.Sum(nil)) != want {
			return errors.New("frozen evidence changed")
		}
	}
	// Hashing evidence may cross the deadline. Recheck at the access boundary.
	if g.now().Before(g.c.StartsAt) || !g.now().Before(g.c.ExpiresAt) {
		return errors.New("window expired while checking frozen evidence")
	}
	return nil
}

func (g *Guard) events() ([]Event, error) {
	p := filepath.Join(g.c.Root, "http.local.jsonl")
	if _, e := os.Lstat(p); os.IsNotExist(e) {
		return nil, nil
	}
	if e := noLinks(p); e != nil {
		return nil, e
	}
	info, e := os.Stat(p)
	if e != nil || info.Size() > 65536 {
		return nil, errors.New("journal size rejected")
	}
	f, e := os.Open(p)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	var events []Event
	scan := bufio.NewScanner(io.LimitReader(f, 65537))
	for scan.Scan() {
		var v Event
		if e = json.Unmarshal(scan.Bytes(), &v); e != nil {
			return nil, e
		}
		events = append(events, v)
		maximum := 40
		// Receipt-only readers have no live pilot guard. Their frozen control
		// still identifies the Pilot journal and its bounded 100 HTTP attempts.
		if pilotReference(g.c.Reference) {
			maximum = 200
		}
		if len(events) > maximum {
			return nil, errors.New("journal limit")
		}
	}
	return events, scan.Err()
}

func syncDir(p string) error {
	f, e := os.Open(p)
	if e != nil {
		return e
	}
	defer f.Close()
	return f.Sync()
}

func (g *Guard) append(v Event) error {
	v.At = g.now()
	p := filepath.Join(g.c.Root, "http.local.jsonl")
	if _, e := os.Lstat(p); e == nil {
		if e = noLinks(p); e != nil {
			return e
		}
	}
	f, e := os.OpenFile(p, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0600)
	if e != nil {
		return e
	}
	e = json.NewEncoder(f).Encode(v)
	if e == nil {
		e = f.Sync()
	}
	closeErr := f.Close()
	if e != nil {
		return e
	}
	if closeErr != nil {
		return closeErr
	}
	return syncDir(g.c.Root)
}

func (g *Guard) reserve(kind string) error {
	limits := g.budgets()
	events, e := g.events()
	if e != nil {
		return e
	}
	started, complete := map[string]string{}, map[string]bool{}
	count := map[string]int{}
	for _, v := range events {
		if limits[v.Kind] == 0 {
			return errors.New("unknown request category")
		}
		switch v.Type {
		case "intent":
			if started[v.Key] != "" {
				return errors.New("duplicate intent")
			}
			started[v.Key] = v.Kind
			count[v.Kind]++
		case "complete":
			if complete[v.Key] || started[v.Key] != v.Kind || v.Status != 200 {
				return errors.New("invalid completion")
			}
			complete[v.Key] = true
		default:
			return errors.New("prior request stopped")
		}
	}
	key := g.c.Key + ":" + kind
	if len(started) != len(complete) || started[key] != "" || count[kind] >= limits[kind] {
		return errors.New("pending, repeated or exhausted request")
	}
	return g.append(Event{Type: "intent", Key: key, Kind: kind})
}

func (g *Guard) PreRoundTripE(req *http.Request) (func(*http.Response, error), error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	reject := func() (func(*http.Response, error), error) {
		g.failed = true
		return nil, errors.New("F05 request rejected before network")
	}
	if (g.c.Mode != "read" && g.c.Mode != "send" && g.c.Mode != "upload" && g.c.Mode != "update" && g.c.Mode != "card_update") || g.check() != nil || req.URL == nil || req.URL.Scheme != "https" ||
		req.URL.User != nil || req.URL.Fragment != "" || req.Response != nil {
		return reject()
	}
	kind := g.c.Kind
	if req.Method == "POST" && req.URL.String() == "https://accounts.feishu.cn/oauth/v3/token" {
		// Read a replayable copy only; never log credentials or an auth payload hash.
		if req.GetBody == nil {
			return reject()
		}
		b, e := req.GetBody()
		if e != nil {
			return reject()
		}
		raw, e := io.ReadAll(io.LimitReader(b, 8193))
		b.Close()
		if e != nil || len(raw) > 8192 {
			return reject()
		}
		form, e := url.ParseQuery(string(raw))
		if e != nil || len(form) != 3 || len(form["client_id"]) != 1 || len(form["client_secret"]) != 1 ||
			len(form["grant_type"]) != 1 || form.Get("client_id") != g.c.AppID ||
			form.Get("grant_type") != "client_credentials" || form.Get("client_secret") == "" {
			return reject()
		}
		kind = "auth"
	} else if g.c.Mode == "upload" {
		if !g.pilotUploadBody(req) {
			return reject()
		}
	} else if g.c.Mode == "send" || g.c.Mode == "update" || g.c.Mode == "card_update" {
		method := "POST"
		if g.c.Mode == "update" {
			method = "PUT"
		}
		if g.c.Mode == "card_update" {
			method = "PATCH"
		}
		if req.Method != method || req.URL.Host != "open.feishu.cn" || req.URL.RequestURI() != g.c.Path || req.GetBody == nil {
			return reject()
		}
		b, e := req.GetBody()
		if e != nil {
			return reject()
		}
		bodyLimit := int64(8192)
		if g.c.Card != nil && relationPurpose(g.c.Card.Binding["purpose"]) {
			// The frozen P42 renderer allows five bounded candidate descriptions.
			// Keep the legacy limit for all other writes and all credential calls.
			bodyLimit = 65536
		}
		raw, e := io.ReadAll(io.LimitReader(b, bodyLimit+1))
		b.Close()
		var got, want map[string]string
		if e != nil || int64(len(raw)) > bodyLimit || json.Unmarshal(raw, &got) != nil || json.Unmarshal([]byte(g.c.Body), &want) != nil || len(got) != len(want) {
			return reject()
		}
		for k, v := range want {
			if got[k] != v {
				return reject()
			}
		}
	} else if req.Method != "GET" || req.URL.Host != "open.feishu.cn" || req.URL.RequestURI() != g.c.Path ||
		req.Body != nil && req.Body != http.NoBody {
		return reject()
	}
	if e := g.reserve(kind); e != nil {
		return reject()
	}
	// Durable journaling also takes time. An expired reservation remains pending,
	// but must never authorize a physical request after the deadline.
	if g.now().Before(g.c.StartsAt) || !g.now().Before(g.c.ExpiresAt) {
		return reject()
	}
	// Fresh HTTP/1 connections: no pooled-connection replay below this hook.
	req.Close = true
	req.Header.Set("Connection", "close")
	key := g.c.Key + ":" + kind
	return func(resp *http.Response, callErr error) {
		g.mu.Lock()
		defer g.mu.Unlock()
		v := Event{Type: "stop", Key: key, Kind: kind}
		if resp != nil {
			v.Status = resp.StatusCode
			if resp.StatusCode == http.StatusTooManyRequests {
				// Feishu provides seconds until the next permitted request.
				raw := resp.Header.Get("x-ogw-ratelimit-reset")
				if raw == "" {
					raw = resp.Header.Get("Retry-After")
				}
				if n, err := strconv.Atoi(raw); err == nil && n >= 0 && n <= 86400 {
					v.RetryAfter = n
				}
			}
		}
		if callErr != nil || resp == nil || resp.Body == nil {
			g.failed = true
			g.append(v)
			return
		}
		if g.pilot != nil && kind == "download" {
			g.receivePilotFile(resp, v)
			return
		}
		raw, e := io.ReadAll(io.LimitReader(resp.Body, 1024*1024+1))
		resp.Body.Close()
		resp.Body = io.NopCloser(bytes.NewReader(raw))
		valid := e == nil && len(raw) <= 1024*1024 && resp.StatusCode == 200
		var data map[string]any
		valid = valid && json.Unmarshal(raw, &data) == nil
		if kind == "auth" {
			token, ok := data["access_token"].(string)
			valid = valid && ok && token != "" && (data["code"] == nil || data["code"] == float64(0))
		} else {
			valid = valid && data["code"] == float64(0)
			sum := sha256.Sum256(raw)
			v.SHA256 = hex.EncodeToString(sum[:])
			p := filepath.Join(g.c.Root, g.c.Key+".response.local")
			f, err := os.OpenFile(p, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
			if err == nil {
				_, err = f.Write(raw)
				if err == nil {
					err = f.Sync()
				}
				f.Close()
				if err == nil {
					err = syncDir(g.c.Root)
				}
			}
			valid = valid && err == nil
		}
		if g.pilot != nil {
			valid = valid && g.check() == nil
		}
		if valid {
			v.Type = "complete"
		} else {
			g.failed = true
		}
		if g.append(v) != nil {
			g.failed = true
		}
	}, nil
}

var _ transport.AbortableInterceptor = (*Guard)(nil)
