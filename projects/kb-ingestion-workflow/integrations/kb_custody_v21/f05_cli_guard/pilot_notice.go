package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

type pilotNotice struct {
	ID       string `json:"notification_id"`
	PlanPath string `json:"plan_path"`
}

// The product checker constructs and freezes this exact plan from a persisted
// service/outbox/report item. The physical guard enforces scope and actual body;
// it never infers a successful send or authorizes an unknown lookup.
func pilotNoticePath(c Control, p pilotSession) bool {
	n := c.Notice
	if c.Reference != "P11" || n == nil || c.Preview != nil || c.Download != nil ||
		c.Mode != "send" || c.Kind != "send" ||
		c.Path != "/open-apis/im/v1/messages?receive_id_type=chat_id" ||
		!regexp.MustCompile(`^([a-f0-9]{64}|notification_[a-f0-9]{32})$`).MatchString(n.ID) ||
		n.PlanPath != filepath.Join(c.Root, n.ID+".notice.local.json") || c.Frozen[n.PlanPath] == "" ||
		noLinks(n.PlanPath) != nil {
		return false
	}
	raw, err := os.ReadFile(n.PlanPath)
	if err != nil || len(raw) > 10000 {
		return false
	}
	h := sha256.Sum256(raw)
	if hex.EncodeToString(h[:]) != c.Frozen[n.PlanPath] {
		return false
	}
	var plan map[string]string
	if json.Unmarshal(raw, &plan) != nil || len(plan) != 8 || plan["reference"] != "P11" ||
		plan["notification_id"] != n.ID || plan["app_id"] != p.AppID ||
		plan["account_id"] != p.AccountID || plan["target_chat_id"] != p.ChatID ||
		!regexp.MustCompile(`^[A-Za-z0-9_-]{1,50}$`).MatchString(plan["provider_request_key"]) ||
		strings.TrimSpace(plan["payload"]) == "" || len(plan["payload"]) > 6000 {
		return false
	}
	allowed := false
	for _, purpose := range p.Purposes {
		if purpose == plan["purpose"] {
			allowed = true
		}
	}
	if !allowed {
		return false
	}
	var body, content map[string]string
	return len(c.Body) <= 8192 && json.Unmarshal([]byte(c.Body), &body) == nil && len(body) == 4 &&
		body["receive_id"] == p.ChatID && body["msg_type"] == "text" && body["uuid"] == plan["provider_request_key"] &&
		json.Unmarshal([]byte(body["content"]), &content) == nil && len(content) == 1 && content["text"] == plan["payload"]
}
