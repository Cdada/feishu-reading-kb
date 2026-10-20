package main

import (
	"bytes"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/larksuite/cli/cmd"
)

// Synthetic keychain and RoundTripper exercise the pinned official command and
// auth stack without any socket or access to the operator's stored credentials.
type syntheticKeychain struct{ reads int }

func (k *syntheticKeychain) Get(service, account string) (string, error) {
	k.reads++
	if !strings.Contains(account, "cli_synthetic") {
		return "", errors.New("unexpected synthetic account")
	}
	return "synthetic-secret", nil
}
func (*syntheticKeychain) Set(string, string, string) error {
	return errors.New("keychain writes forbidden")
}
func (*syntheticKeychain) Remove(string, string) error {
	return errors.New("keychain deletes forbidden")
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestOfficialCLIAuthQueryRedirectAndErrorChain(t *testing.T) {
	mode := os.Getenv("F05_SYNTHETIC_CLI")
	if mode == "" {
		for _, m := range []string{"success", "profile", "redirect", "auth-error", "http-error", "timeout", "send-success", "send-redirect", "send-http-error", "send-timeout", "pilot-list", "pilot-profile", "pilot-expired-credential", "pilot-download", "pilot-download-inbox", "pilot-download-redirect", "pilot-download-error", "pilot-download-timeout",
			"preview-upload-success", "preview-upload-redirect", "preview-upload-http-error", "preview-upload-timeout",
			"preview-send-success", "preview-send-redirect", "preview-send-http-error", "preview-send-timeout", "preview-download-success", "notice-send-success", "notice-send-redirect", "notice-send-http-error", "notice-send-timeout"} {
			t.Run(m, func(t *testing.T) {
				child := exec.Command(os.Args[0], "-test.run=^TestOfficialCLIAuthQueryRedirectAndErrorChain$")
				child.Env = append(os.Environ(), "F05_SYNTHETIC_CLI="+m)
				out, e := child.CombinedOutput()
				if e != nil {
					t.Fatalf("offline child %v: %s", e, out)
				}
			})
		}
		return
	}
	c := control(t)
	c.Key = "members"
	c.Kind = "members"
	c.Path = "/open-apis/im/v1/chats/oc_synthetic/members?member_id_type=open_id&page_size=100"
	if mode == "profile" {
		c.Mode = "profile"
	}
	if strings.HasPrefix(mode, "send-") {
		c = sendControl(t)
	}
	if strings.HasPrefix(mode, "pilot-") {
		c = pilotList(pilotControl(t))
		if mode == "pilot-profile" {
			c.Mode = "profile"
		}
	}
	if strings.HasPrefix(mode, "pilot-download") {
		c = pilotDownloadControl(t)
	}
	if mode == "pilot-download-inbox" {
		c = inboxControl(t)
	}
	if strings.HasPrefix(mode, "preview-upload-") {
		c = pilotUploadControl(t)
	}
	if strings.HasPrefix(mode, "preview-send-") {
		c = pilotSendControl(t)
	}
	if mode == "preview-download-success" {
		c = pilotPreviewDownload(t)
	}
	if strings.HasPrefix(mode, "notice-send-") {
		c = noticeControl(t)
	}
	g := guard(t, c)
	if mode == "pilot-expired-credential" {
		n := 0
		g.now = func() time.Time {
			n++
			if n >= 5 {
				return c.ExpiresAt
			}
			return c.StartsAt.Add(time.Minute)
		}
	}
	dir := t.TempDir()
	t.Setenv("LARKSUITE_CLI_CONFIG_DIR", dir)
	for _, name := range []string{"LARK_CLI_NO_PROXY", "LARKSUITE_CLI_PROFILE", "LARKSUITE_CLI_WORKSPACE", "LARKSUITE_CLI_APP_ID", "LARKSUITE_CLI_APP_SECRET", "LARKSUITE_CLI_USER_ACCESS_TOKEN", "LARKSUITE_CLI_TENANT_ACCESS_TOKEN"} {
		t.Setenv(name, "")
	}
	config := `{"apps":[{"name":"synthetic","appId":"cli_synthetic","appSecret":{"source":"keychain","id":"appsecret:cli_synthetic"},"brand":"feishu","users":[]}]}`
	if e := os.WriteFile(filepath.Join(dir, "config.json"), []byte(config), 0600); e != nil {
		t.Fatal(e)
	}
	physical := 0
	old := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = old })
	http.DefaultTransport = roundTripFunc(func(r *http.Request) (*http.Response, error) {
		physical++
		events, e := g.events()
		if e != nil || len(events) == 0 || events[len(events)-1].Type != "intent" {
			t.Fatal("HTTP before durable intent", e)
		}
		if r.URL.String() == "https://accounts.feishu.cn/oauth/v3/token" {
			if mode == "auth-error" {
				return response(200, `{"code":999,"error":"synthetic"}`), nil
			}
			return response(200, `{"access_token":"synthetic-token","expires_in":7200}`), nil
		}
		if r.URL.String() != "https://open.feishu.cn"+c.Path {
			t.Fatal("wrong target", r.URL)
		}
		if r.Header.Get("Authorization") != "Bearer synthetic-token" {
			t.Fatal("official token not applied")
		}
		if strings.HasPrefix(mode, "preview-") {
			if strings.HasSuffix(mode, "-redirect") {
				v := response(302, `{}`)
				v.Header.Set("Location", "https://open.feishu.cn"+c.Path)
				return v, nil
			}
			if strings.HasSuffix(mode, "-http-error") {
				return response(500, `{"code":999}`), nil
			}
			if strings.HasSuffix(mode, "-timeout") {
				return nil, errors.New("synthetic unknown preview request")
			}
			if c.Kind == "upload" {
				if !g.pilotUploadBody(r) {
					t.Fatal("official multipart body differs")
				}
				return response(200, `{"code":0,"data":{"file_key":"file_upload"}}`), nil
			}
			if c.Kind == "send" {
				return response(200, previewSent(c)), nil
			}
			v := response(200, syntheticPDF)
			v.Header.Set("Content-Type", "application/octet-stream")
			v.ContentLength = int64(len(syntheticPDF))
			return v, nil
		}
		if strings.HasPrefix(mode, "pilot-download") {
			switch mode {
			case "pilot-download-redirect":
				v := response(302, `{}`)
				v.Header.Set("Location", "https://open.feishu.cn"+c.Path)
				return v, nil
			case "pilot-download-error":
				return response(500, `{"code":999}`), nil
			case "pilot-download-timeout":
				return nil, errors.New("synthetic interrupted download")
			}
			v := response(200, pilotSyntheticFile)
			v.Header.Set("Content-Type", "application/octet-stream")
			v.ContentLength = int64(len(pilotSyntheticFile))
			return v, nil
		}
		if strings.HasPrefix(mode, "send-") {
			b, e := io.ReadAll(r.Body)
			if e != nil || r.Method != "POST" || !validSendBody(string(b)) {
				t.Fatal("official send command lost fixed body", e)
			}
		}
		if strings.HasPrefix(mode, "notice-send-") {
			b, e := io.ReadAll(r.Body)
			if e != nil || string(b) != c.Body {
				t.Fatal("notice body differs", e)
			}
		}
		switch strings.TrimPrefix(strings.TrimPrefix(mode, "notice-"), "send-") {
		case "redirect":
			v := response(302, `{}`)
			v.Header.Set("Location", "https://open.feishu.cn"+c.Path)
			return v, nil
		case "http-error":
			return response(500, `{"code":999}`), nil
		case "timeout":
			return nil, errors.New("synthetic timeout")
		}
		return response(200, `{"code":0,"data":{"has_more":false,"items":[]}}`), nil
	})
	keychain := &syntheticKeychain{}
	ok := executeCLI(c, g, cmd.WithKeychain(keychain))
	if ok != (mode == "success" || mode == "profile" || mode == "send-success" || mode == "notice-send-success" || mode == "pilot-list" || mode == "pilot-profile" || mode == "pilot-download" || mode == "pilot-download-inbox" || strings.HasPrefix(mode, "preview-") && strings.HasSuffix(mode, "-success")) {
		t.Fatalf("unexpected CLI result mode=%s ok=%v requests=%d", mode, ok, physical)
	}
	want := 2
	if mode == "profile" || mode == "pilot-profile" || mode == "pilot-expired-credential" {
		want = 0
	}
	if mode == "auth-error" {
		want = 1
	}
	if physical != want {
		t.Fatalf("hidden retries or missing call: got %d want %d", physical, want)
	}
	if mode == "pilot-download" || mode == "pilot-download-inbox" {
		raw, e := os.ReadFile(filepath.Join(c.Root, c.Key+".cli.local"))
		if e != nil || string(raw) != pilotSyntheticFile {
			t.Fatal("official raw API did not save exact bytes", e)
		}
	}
	if mode == "pilot-expired-credential" && keychain.reads != 0 {
		t.Fatal("expired during evidence check but read stored credential")
	}
	if mode != "profile" && mode != "pilot-profile" && mode != "pilot-expired-credential" && keychain.reads == 0 {
		t.Fatal("stored credential route not exercised")
	}
	files, _ := os.ReadDir(c.Root)
	for _, f := range files {
		b, _ := os.ReadFile(filepath.Join(c.Root, f.Name()))
		if bytes.Contains(b, []byte("synthetic-secret")) || bytes.Contains(b, []byte("synthetic-token")) {
			t.Fatal("credential persisted")
		}
	}
}
