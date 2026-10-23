package main

// This private adapter embeds the pinned CLI credential/transport factory and
// its pinned SDK. It owns one connection, with no shared CLI bus or credential
// export. Each bootstrap/upgrade is granted by the existing Python ledger.
import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	extcredential "github.com/larksuite/cli/extension/credential"
	exttransport "github.com/larksuite/cli/extension/transport"
	"github.com/larksuite/cli/internal/cmdutil"
	"github.com/larksuite/cli/internal/core"
	"github.com/larksuite/oapi-sdk-go/v3/event/dispatcher"
	"github.com/larksuite/oapi-sdk-go/v3/event/dispatcher/callback"
	larkws "github.com/larksuite/oapi-sdk-go/v3/ws"
)

type eventReply struct {
	Sequence int  `json:"sequence"`
	OK       bool `json:"ok"`
}
type eventGate struct {
	base        *Guard
	chat, owner string
	bot         string
	ctx         context.Context
	cancel      context.CancelFunc
	output      io.Writer
	input       chan eventReply
	rpcMu       sync.Mutex
	sequence    int
	wsMu        sync.Mutex
	expectedWS  string
	ready       atomic.Bool
}

func (g *eventGate) rpc(value map[string]any) error {
	g.rpcMu.Lock()
	defer g.rpcMu.Unlock()
	if g.ctx.Err() != nil || !time.Now().Before(g.base.c.ExpiresAt) {
		return errors.New("event session stopped")
	}
	g.sequence++
	value["sequence"] = g.sequence
	output := g.output
	if output == nil {
		output = os.Stdout
	}
	if json.NewEncoder(output).Encode(value) != nil {
		g.cancel()
		return errors.New("event output failed")
	}
	select {
	case reply := <-g.input:
		if reply.Sequence == g.sequence && reply.OK {
			return nil
		}
	case <-g.ctx.Done():
	case <-time.After(3 * time.Second):
	}
	g.cancel()
	return errors.New("event parent did not durably approve")
}

func (g *eventGate) Name() string                                                { return "kb-v21-owned-card-events" }
func (g *eventGate) ResolveInterceptor(context.Context) exttransport.Interceptor { return g }
func (g *eventGate) PreRoundTrip(*http.Request) func(*http.Response, error)      { return nil }
func (g *eventGate) ResolveAccount(context.Context) (*extcredential.Account, error) {
	if g.base.check() != nil || g.rpc(map[string]any{"type": "check"}) != nil {
		return nil, &extcredential.BlockError{Provider: g.Name(), Reason: "event authorization unavailable"}
	}
	return nil, nil
}
func (g *eventGate) ResolveToken(context.Context, extcredential.TokenSpec) (*extcredential.Token, error) {
	// The listener never needs a tenant/user token. Card reads use one-shot CLI.
	return nil, &extcredential.BlockError{Provider: g.Name(), Reason: "tokens are not used by the card listener"}
}

func (g *eventGate) PreRoundTripE(req *http.Request) (func(*http.Response, error), error) {
	reject := func() (func(*http.Response, error), error) {
		g.cancel()
		return nil, errors.New("event HTTP rejected before network")
	}
	if g.base.check() != nil || req.URL == nil || req.URL.String() != "https://open.feishu.cn/callback/ws/endpoint" ||
		req.Method != "POST" || req.Response != nil || req.GetBody == nil {
		return reject()
	}
	copyBody, err := req.GetBody()
	if err != nil {
		return reject()
	}
	raw, err := io.ReadAll(io.LimitReader(copyBody, 8193))
	copyBody.Close()
	var body map[string]string
	if err != nil || len(raw) > 8192 || json.Unmarshal(raw, &body) != nil || len(body) != 3 || body["AppID"] != g.base.c.AppID || body["AppSecret"] == "" || body["ClientAssertion"] != "" {
		return reject()
	}
	// Never persist credential payloads or token-bearing bootstrap URLs.
	if g.rpc(map[string]any{"type": "reserve", "kind": "event_bootstrap", "path": "/callback/ws/endpoint"}) != nil {
		return reject()
	}
	req.Close = true
	req.Header.Set("Connection", "close")
	return func(resp *http.Response, callErr error) {
		if callErr != nil || resp == nil {
			g.cancel()
			return
		}
		raw, err := io.ReadAll(io.LimitReader(resp.Body, 65537))
		resp.Body.Close()
		resp.Body = io.NopCloser(bytes.NewReader(raw))
		var value larkws.EndpointResp
		if err != nil || len(raw) > 65536 || resp.StatusCode != 200 || json.Unmarshal(raw, &value) != nil || value.Code != 0 || value.Data == nil {
			g.cancel()
			return
		}
		u, err := url.Parse(value.Data.Url)
		if err != nil || u.Scheme != "wss" || u.User != nil || u.Fragment != "" || u.Port() != "" && u.Port() != "443" || !strings.HasSuffix(u.Hostname(), ".feishu.cn") {
			g.cancel()
			resp.StatusCode = 599
			return
		}
		g.wsMu.Lock()
		g.expectedWS = value.Data.Url
		g.wsMu.Unlock()
		if g.rpc(map[string]any{"type": "http_complete", "kind": "event_bootstrap", "status": resp.StatusCode}) != nil {
			resp.StatusCode = 599
		}
	}, nil
}

func (g *eventGate) dialer(original *websocket.Dialer) *websocket.Dialer {
	d := *original
	prior := d.Proxy
	d.Proxy = func(req *http.Request) (*url.URL, error) {
		if g.base.check() != nil || req.URL == nil {
			return nil, errors.New("event dial rejected")
		}
		u := *req.URL
		u.Scheme = "wss"
		g.wsMu.Lock()
		expected := g.expectedWS
		g.expectedWS = ""
		g.wsMu.Unlock()
		if expected == "" || u.String() != expected {
			g.cancel()
			return nil, errors.New("unbound event endpoint")
		}
		if err := g.rpc(map[string]any{"type": "reserve", "kind": "event_upgrade", "path": "websocket"}); err != nil {
			return nil, err
		}
		if prior != nil {
			return prior(req)
		}
		return nil, nil
	}
	return &d
}

func (g *eventGate) Debug(context.Context, ...interface{}) {}
func (g *eventGate) Info(_ context.Context, args ...interface{}) {
	// SDK logging can contain a connection URL; only emit fixed status values.
	if len(args) > 0 && strings.Contains(fmt.Sprint(args[0]), "connected to") && g.ready.CompareAndSwap(false, true) {
		if g.rpc(map[string]any{"type": "ready"}) != nil {
			g.cancel()
		}
	}
}
func (g *eventGate) Warn(context.Context, ...interface{})  { g.cancel() }
func (g *eventGate) Error(context.Context, ...interface{}) { g.cancel() }

func eventPayload(ev *callback.CardActionTriggerEvent, app, chat, owner string) (map[string]any, error) {
	if ev == nil || ev.EventV2Base == nil || ev.EventV2Base.Header == nil || ev.EventV2Base.Header.AppID != app || ev.EventV2Base.Header.EventType != "card.action.trigger" ||
		ev.EventV2Base.Header.EventID == "" || len(ev.EventV2Base.Header.EventID) > 160 || !ident.MatchString(ev.EventV2Base.Header.EventID) || ev.Event == nil {
		return nil, errors.New("unbound callback header")
	}
	v := ev.Event
	if v.Operator == nil || v.Operator.OpenID != owner || v.Context == nil || v.Context.OpenChatID != chat ||
		!cardID.MatchString(v.Context.OpenMessageID) || v.Host != "im_message" || v.Action == nil || v.Action.Tag != "button" ||
		len(v.Action.FormValue) != 0 || v.Action.InputValue != "" || v.Action.Option != "" || len(v.Action.Options) != 0 {
		return nil, errors.New("unsupported owner action")
	}
	a := v.Action.Value
	cid, _ := a["card_id"].(string)
	command, _ := a["command"].(string)
	if len(a) != 3 || a["reference"] != "P40-card-v1" || !cardSHA.MatchString(cid) ||
		!knownCardCommand(command) {
		return nil, errors.New("unregistered card action")
	}
	value, _ := json.Marshal(a)
	return map[string]any{"type": "card.action.trigger", "event_id": ev.EventV2Base.Header.EventID, "timestamp": ev.EventV2Base.Header.CreateTime,
		"operator_id": owner, "chat_id": chat, "host": "im_message", "message_id": v.Context.OpenMessageID,
		"action_tag": "button", "action_value": string(value)}, nil
}

func (g *eventGate) handle(_ context.Context, ev *callback.CardActionTriggerEvent) (*callback.CardActionTriggerResponse, error) {
	value, err := eventPayload(ev, g.base.c.AppID, g.chat, g.owner)
	if err != nil {
		return &callback.CardActionTriggerResponse{Toast: &callback.Toast{Type: "warning", Content: "此操作不属于当前资料确认，请查看最新导读。"}}, nil
	}
	if err = g.rpc(map[string]any{"type": "event", "app_id": g.base.c.AppID, "event": value}); err != nil {
		return nil, err
	}
	return &callback.CardActionTriggerResponse{Toast: &callback.Toast{Type: "info", Content: "已收到操作，正在核对；处理结果稍后更新。"}}, nil
}

func runCardListener(path, digest string) int {
	if noLinks(path) != nil || !cardSHA.MatchString(digest) {
		return 1
	}
	raw, err := os.ReadFile(path)
	sum := sha256.Sum256(raw)
	if err != nil || len(raw) > 1024*1024 || hex.EncodeToString(sum[:]) != digest {
		return 1
	}
	c, err := readControl(bytes.NewReader(raw))
	if err != nil || c.Reference != "P40-events-v1" || c.Mode != "events" || c.Kind != "message" || c.Body != "" || c.Card != nil ||
		c.ExpiresAt.Sub(c.StartsAt) > 10*time.Minute || c.Session == "" || filepath.Base(c.Root) != "card-events" ||
		noLinks(c.Root) != nil || !strings.Contains(c.Root, string(filepath.Separator)+".dev-runtime"+string(filepath.Separator)) {
		return 1
	}
	base := &Guard{c: c, now: time.Now}
	var rules struct {
		Reference string `json:"reference"`
		Approved  bool   `json:"approved"`
		Profile   string `json:"cli_profile"`
		Policy    struct {
			App   string `json:"app_id"`
			Chat  string `json:"chat_id"`
			Owner string `json:"account_id"`
			Bot   string `json:"bot_id"`
		} `json:"policy"`
	}
	if base.check() != nil || !cardEnabled(c, c.Session) || !frozenJSON(c, c.Session, &rules) || rules.Reference != "P32-standing-v1" || !rules.Approved ||
		rules.Profile != c.Profile || rules.Policy.App != c.AppID || !strings.HasPrefix(rules.Policy.Chat, "oc_") || !ident.MatchString(rules.Policy.Chat) ||
		!strings.HasPrefix(rules.Policy.Owner, "ou_") || !ident.MatchString(rules.Policy.Owner) {
		return 1
	}
	marker, err := os.OpenFile(path+".started", os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return 1
	}
	marker.Close()
	ctx, cancel := context.WithDeadline(context.Background(), c.ExpiresAt)
	defer cancel()
	g := &eventGate{base: base, ctx: ctx, cancel: cancel, input: make(chan eventReply), chat: rules.Policy.Chat, owner: rules.Policy.Owner, bot: rules.Policy.Bot}
	go func() {
		scanner := bufio.NewScanner(os.Stdin)
		scanner.Buffer(make([]byte, 4096), 65536)
		for scanner.Scan() {
			var reply eventReply
			if json.Unmarshal(scanner.Bytes(), &reply) != nil {
				break
			}
			select {
			case g.input <- reply:
			case <-ctx.Done():
				return
			}
		}
		cancel()
	}()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	go func() {
		select {
		case <-signals:
			cancel()
		case <-ctx.Done():
		}
	}()
	extcredential.Register(g)
	exttransport.Register(g)
	f := cmdutil.NewDefault(cmdutil.NewIOStreams(bytes.NewReader(nil), io.Discard, io.Discard), cmdutil.InvocationContext{Profile: c.Profile, ProfileSource: core.ProfileFromFlag})
	if f.CheckStrictMode(ctx, core.AsBot) != nil {
		return 1
	}
	cfg, err := f.Config()
	if err != nil || cfg.AppID != c.AppID || string(cfg.Brand) != "feishu" || cfg.AppSecret == "" || cfg.ProfileName != c.Profile {
		return 1
	}
	websocket.DefaultDialer = g.dialer(websocket.DefaultDialer)
	handler := dispatcher.NewEventDispatcher("", "").OnP2CardActionTrigger(g.handle)
	if archiveMentionEnabled(c, c.Session) {
		handler.OnP2MessageReceiveV1(g.handleArchiveMessage)
	}
	client := larkws.NewClient(cfg.AppID, cfg.AppSecret, larkws.WithEventHandler(handler), larkws.WithAutoReconnect(false), larkws.WithLogger(g))
	go func() {
		if client.Start(ctx) != nil {
			cancel()
		}
	}()
	<-ctx.Done()
	if g.ready.Load() && time.Now().After(c.ExpiresAt.Add(-time.Second)) {
		return 0
	}
	return 1
}
