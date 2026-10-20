package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	exttransport "github.com/larksuite/cli/extension/transport"
	"github.com/larksuite/cli/internal/cmdutil"
	"github.com/larksuite/cli/internal/core"
	"github.com/larksuite/oapi-sdk-go/v3/event/dispatcher/callback"
	larkws "github.com/larksuite/oapi-sdk-go/v3/ws"
)

func syntheticGate(t *testing.T) (*eventGate, *bytes.Buffer) {
	c := dailyControl(t)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	output := &bytes.Buffer{}
	g := &eventGate{base: &Guard{c: c, now: time.Now}, ctx: ctx, cancel: cancel, input: make(chan eventReply, 8), output: output,
		chat: "oc_synthetic", owner: "ou_synthetic"}
	for i := 1; i <= 8; i++ {
		g.input <- eventReply{Sequence: i, OK: true}
	}
	return g, output
}

func TestCardListenerMetersBootstrapAndBindsOneUpgradeWithoutCredentialOutput(t *testing.T) {
	g, out := syntheticGate(t)
	body := `{"AppID":"` + g.base.c.AppID + `","AppSecret":"synthetic-secret","ClientAssertion":""}`
	req, _ := http.NewRequest("POST", "https://open.feishu.cn/callback/ws/endpoint", strings.NewReader(body))
	post, err := g.PreRoundTripE(req)
	if err != nil {
		t.Fatal(err)
	}
	endpoint := "wss://synthetic.feishu.cn/ws?ticket=synthetic-ticket"
	raw, _ := json.Marshal(map[string]any{"code": 0, "data": map[string]any{"URL": endpoint}})
	resp := &http.Response{StatusCode: 200, Body: io.NopCloser(bytes.NewReader(raw))}
	post(resp, nil)
	if g.ctx.Err() != nil {
		t.Fatal("valid bootstrap rejected")
	}
	dial := g.dialer(&websocket.Dialer{})
	upgrade, _ := http.NewRequest("GET", strings.Replace(endpoint, "wss:", "https:", 1), nil)
	if _, err = dial.Proxy(upgrade); err != nil {
		t.Fatal(err)
	}
	g.Info(context.Background(), "connected to "+endpoint)
	if !g.ready.Load() {
		t.Fatal("missing ready acknowledgement")
	}
	if _, err = dial.Proxy(upgrade); err == nil {
		t.Fatal("upgrade reservation reused")
	}
	saved := out.String()
	if strings.Contains(saved, "synthetic-secret") || strings.Contains(saved, "synthetic-ticket") || strings.Contains(saved, "wss:") {
		t.Fatal("credential payload entered ledger")
	}
	if strings.Count(saved, `"type":"reserve"`) != 2 || !strings.Contains(saved, `"type":"ready"`) {
		t.Fatal(saved)
	}
}

func TestCardListenerRejectsWrongBootstrapAndUnboundDialBeforeNetwork(t *testing.T) {
	for _, bad := range []string{"method", "host", "app", "extra", "endpoint"} {
		t.Run(bad, func(t *testing.T) {
			g, _ := syntheticGate(t)
			body := `{"AppID":"` + g.base.c.AppID + `","AppSecret":"synthetic","ClientAssertion":""}`
			method, url := "POST", "https://open.feishu.cn/callback/ws/endpoint"
			if bad == "method" {
				method = "GET"
			}
			if bad == "host" {
				url = "https://example.org/callback/ws/endpoint"
			}
			if bad == "app" {
				body = strings.Replace(body, g.base.c.AppID, "cli_other", 1)
			}
			if bad == "extra" {
				body = strings.Replace(body, "{", `{"extra":"x",`, 1)
			}
			req, _ := http.NewRequest(method, url, strings.NewReader(body))
			post, err := g.PreRoundTripE(req)
			if bad != "endpoint" {
				if err == nil {
					t.Fatal("invalid bootstrap accepted")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			post(&http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"code":0,"data":{"URL":"wss://example.org/"}}`))}, nil)
			if g.ctx.Err() == nil {
				t.Fatal("foreign endpoint accepted")
			}
		})
	}
}

func TestCardListenerOnlyAdmitsExactOwnerButtonsAndDropsCallbackToken(t *testing.T) {
	raw := `{"schema":"2.0","header":{"event_id":"synthetic_event","event_type":"card.action.trigger","app_id":"cli_synthetic","create_time":"100000"},"event":{"operator":{"open_id":"ou_synthetic"},"token":"synthetic-callback-token","host":"im_message","context":{"open_chat_id":"oc_synthetic","open_message_id":"om_card"},"action":{"tag":"button","value":{"reference":"P40-card-v1","card_id":"` + strings.Repeat("a", 64) + `","command":"确认入库"}}}}`
	for _, bad := range []string{"valid", "owner", "chat", "app", "form", "command", "missing-header"} {
		t.Run(bad, func(t *testing.T) {
			var ev callback.CardActionTriggerEvent
			if err := json.Unmarshal([]byte(raw), &ev); err != nil {
				t.Fatal(err)
			}
			switch bad {
			case "owner":
				ev.Event.Operator.OpenID = "ou_other"
			case "chat":
				ev.Event.Context.OpenChatID = "oc_other"
			case "app":
				ev.EventV2Base.Header.AppID = "cli_other"
			case "form":
				ev.Event.Action.FormValue = map[string]interface{}{"x": "y"}
			case "command":
				ev.Event.Action.Value["command"] = "arbitrary"
			case "missing-header":
				ev.EventV2Base.Header = nil
			}
			value, err := eventPayload(&ev, "cli_synthetic", "oc_synthetic", "ou_synthetic")
			if (err == nil) != (bad == "valid") {
				t.Fatal(bad, err)
			}
			encoded, _ := json.Marshal(value)
			if strings.Contains(string(encoded), "synthetic-callback-token") {
				t.Fatal("callback token persisted")
			}
		})
	}
}

type bootstrapOfflineTransport func(*http.Request) (*http.Response, error)

func (f bootstrapOfflineTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestPinnedCLITransportBridgeInterceptsActualSDKBootstrap(t *testing.T) {
	// The SDK captures DefaultClient at package initialization. Exercise its
	// real Start path with an in-memory transport; no socket is opened.
	g, out := syntheticGate(t)
	prior := exttransport.GetProvider()
	exttransport.Register(g)
	defer exttransport.Register(prior)
	oldTransport, oldRedirect := http.DefaultClient.Transport, http.DefaultClient.CheckRedirect
	defer func() { http.DefaultClient.Transport = oldTransport; http.DefaultClient.CheckRedirect = oldRedirect }()
	calls := 0
	http.DefaultClient.Transport = bootstrapOfflineTransport(func(r *http.Request) (*http.Response, error) {
		calls++
		if !strings.Contains(out.String(), `"kind":"event_bootstrap"`) {
			t.Fatal("SDK reached transport before durable grant")
		}
		// A provider failure terminates Start before any WebSocket attempt.
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"code":100004,"msg":"synthetic stop"}`)), Request: r}, nil
	})
	cmdutil.NewDefault(cmdutil.NewIOStreams(bytes.NewReader(nil), io.Discard, io.Discard), cmdutil.InvocationContext{Profile: g.base.c.Profile, ProfileSource: core.ProfileFromFlag})
	client := larkws.NewClient(g.base.c.AppID, "synthetic-secret", larkws.WithAutoReconnect(false), larkws.WithLogger(g))
	if err := client.Start(g.ctx); err == nil {
		t.Fatal("expected synthetic bootstrap failure")
	}
	if calls != 1 || !strings.Contains(out.String(), `"type":"reserve"`) {
		t.Fatal("pinned bridge missed SDK bootstrap")
	}
}
