package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func sendControl(t *testing.T) Control {
	c := control(t)
	c.Reference, c.Mode, c.Key, c.Kind = "F06", "send", "send", "send"
	c.Root = filepath.Join(filepath.Dir(c.Root), "f06-requests")
	if e := os.Mkdir(c.Root, 0700); e != nil {
		t.Fatal(e)
	}
	c.Path = "/open-apis/im/v1/messages?receive_id_type=chat_id"
	c.Body = `{"receive_id":"oc_synthetic","msg_type":"text","uuid":"custody-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","content":"{\"text\":\"【V2.1 S6 测试】原始文件已安全进入待归档区，飞书附件可按需清理。\"}"}`
	return c
}

func TestF06OneFrozenSendConsumesSeparateAuthAndCannotRepeat(t *testing.T) {
	c := sendControl(t)
	g := guard(t, c)
	success(t, g, auth(c), `{"access_token":"synthetic-token"}`)
	r, _ := http.NewRequest("POST", "https://open.feishu.cn"+c.Path, strings.NewReader(c.Body))
	success(t, g, r, `{"code":0,"data":{"message_id":"om_synthetic"}}`)
	if events, e := g.events(); e != nil || len(events) != 4 || events[2].Kind != "send" {
		t.Fatal("wrong actual request accounting", events, e)
	}
	if _, e := g.PreRoundTripE(r); e == nil {
		t.Fatal("repeat allowed")
	}
	g.Close()
	if restarted, e := NewGuard(c, time.Now); e == nil {
		restarted.Close()
		t.Fatal("restarted send allowed")
	}
}

func TestF06RejectsBodyChangesAndDoesNotEnableF05Writes(t *testing.T) {
	for _, mode := range []string{"F05", "wrong-target", "wrong-text", "new-key", "extra-field", "wrong-method"} {
		t.Run(mode, func(t *testing.T) {
			c := sendControl(t)
			if mode == "F05" {
				c.Reference = "F05"
				if g, e := NewGuard(c, time.Now); e == nil {
					g.Close()
					t.Fatal("F05 write allowed")
				}
				return
			}
			g := guard(t, c)
			body := c.Body
			method := "POST"
			switch mode {
			case "wrong-target":
				body = strings.Replace(body, "oc_synthetic", "oc_other", 1)
			case "wrong-text":
				body = strings.Replace(body, "安全", "改变", 1)
			case "new-key":
				body = strings.Replace(body, "custody-a", "custody-b", 1)
			case "extra-field":
				body = strings.Replace(body, `{"receive_id"`, `{"extra":"value","receive_id"`, 1)
			case "wrong-method":
				method = "PUT"
			}
			r, _ := http.NewRequest(method, "https://open.feishu.cn"+c.Path, strings.NewReader(body))
			if _, e := g.PreRoundTripE(r); e == nil {
				t.Fatal("changed request allowed")
			}
			if events, e := g.events(); e != nil || len(events) != 0 {
				t.Fatal("invalid request touched budget", events, e)
			}
		})
	}
}
