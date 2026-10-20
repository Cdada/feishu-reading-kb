package main

// P32 derives one immutable, at-most-two-HTTP child from approved standing rules.
// Existing P11/N10 approvals keep their original restrictions. Cross-batch
// receipts are admitted only with the original frozen approval and HTTP journal.
import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
)

type dailyDelegation struct {
	Rules       string `json:"rules_path"`
	RulesSHA    string `json:"rules_sha256"`
	Reservation string `json:"reservation_path"`
}

func frozenJSON(c Control, path string, target any) bool {
	if c.Frozen[path] == "" || noLinks(path) != nil {
		return false
	}
	raw, err := os.ReadFile(path)
	if err != nil || len(raw) > 1024*1024 {
		return false
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]) == c.Frozen[path] && json.Unmarshal(raw, target) == nil
}

func validDailyDelegation(c Control, p pilotSession) bool {
	if p.Daily == nil {
		return true
	}
	d := p.Daily
	if c.Reference != "P11" || c.Frozen[d.Rules] != d.RulesSHA ||
		!strings.Contains(d.Rules, string(filepath.Separator)+".dev-runtime"+string(filepath.Separator)) {
		return false
	}
	var rules struct {
		Reference string `json:"reference"`
		Approved  bool   `json:"approved"`
		Note      string `json:"approval_note"`
		Profile   string `json:"cli_profile"`
		Policy    struct {
			App     string `json:"app_id"`
			Chat    string `json:"chat_id"`
			Account string `json:"account_id"`
			Bot     string `json:"bot_id"`
			Paid    int    `json:"paid_model_cny"`
			Retries int    `json:"retry_attempts"`
		} `json:"policy"`
	}
	if !frozenJSON(c, d.Rules, &rules) || rules.Reference != "P32-standing-v1" || !rules.Approved ||
		strings.TrimSpace(rules.Note) == "" || rules.Profile != p.Profile || rules.Policy.App != p.AppID ||
		rules.Policy.Chat != p.ChatID || rules.Policy.Account != p.AccountID || rules.Policy.Bot != p.BotID ||
		rules.Policy.Paid != 0 || rules.Policy.Retries < 1 || rules.Policy.Retries > 15 {
		return false
	}
	var reservation struct {
		Reference string `json:"reference"`
		Rules     string `json:"rules_path"`
		SHA       string `json:"rules_sha256"`
		Root      string `json:"root"`
		Key       string `json:"operation_key"`
		Attempt   int    `json:"attempt"`
		Kind      string `json:"kind"`
		Mode      string `json:"mode"`
		Path      string `json:"path"`
		BodySHA   string `json:"body_sha256"`
		Maximum   int    `json:"maximum_http"`
	}
	body := sha256.Sum256([]byte(c.Body))
	if !frozenJSON(c, d.Reservation, &reservation) || reservation.Reference != "P32-request-v1" ||
		reservation.Rules != d.Rules || reservation.SHA != d.RulesSHA || reservation.Root != c.Root ||
		reservation.Key == "" || reservation.Attempt < 1 || reservation.Attempt > rules.Policy.Retries ||
		reservation.Kind != c.Kind || reservation.Mode != c.Mode || reservation.Path != c.Path ||
		reservation.BodySHA != hex.EncodeToString(body[:]) || reservation.Maximum != 2 {
		return false
	}
	if p.Limits["auth"] != 1 || p.Limits[c.Kind] != 1 || p.Total != 2 {
		return false
	}
	return true
}

func receiptControl(c Control, p pilotSession, path string) (Control, bool) {
	directory := filepath.Dir(path)
	if directory == c.Root {
		return c, true
	}
	if p.Daily == nil || !validDailyDelegation(c, p) {
		return c, false
	}
	previous := filepath.Join(directory, "approval.local.json")
	journal := filepath.Join(directory, "http.local.jsonl")
	var old pilotSession
	if !frozenJSON(c, previous, &old) || !old.Approved || old.Reference != "P11" || old.Root != directory ||
		old.AppID != p.AppID || old.ChatID != p.ChatID || old.AccountID != p.AccountID || old.BotID != p.BotID ||
		old.Profile != p.Profile || c.Frozen[journal] == "" || noLinks(journal) != nil {
		return c, false
	}
	raw, err := os.ReadFile(journal)
	sum := sha256.Sum256(raw)
	if err != nil || len(raw) > 65536 || hex.EncodeToString(sum[:]) != c.Frozen[journal] {
		return c, false
	}
	origin := c
	origin.Root = directory
	return origin, true
}
