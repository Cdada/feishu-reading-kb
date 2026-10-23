package main

// N10 reuses the pinned official CLI, credential store and physical transport
// journal. Pilot writes require the separately bound exact PDF contract.
import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/larksuite/cli/extension/credential"
)

var pilotBaseLimits = map[string]int{"auth": 10, "identity": 6, "list": 12, "message": 6, "download": 6, "upload": 4, "send": 6}
var pilotKey = regexp.MustCompile(`^p[0-9]{3}-(identity|list|message|download|upload|send)$`)

type pilotSession struct {
	Daily        *dailyDelegation  `json:"daily_parent,omitempty"`
	Reference    string            `json:"reference"`
	Operation    string            `json:"operation"`
	Inbox        *pilotInboxPolicy `json:"inbox"`
	Approved     bool              `json:"approved"`
	Root         string            `json:"root"`
	AppID        string            `json:"app_id"`
	ChatID       string            `json:"chat_id"`
	AccountID    string            `json:"account_id"`
	BotID        string            `json:"bot_id"`
	Profile      string            `json:"cli_profile"`
	StartsAt     time.Time         `json:"starts_at"`
	ExpiresAt    time.Time         `json:"expires_at"`
	Limits       map[string]int    `json:"limits"`
	Total        int               `json:"maximum_total"`
	Purposes     []string          `json:"notice_purposes"`
	Retries      int               `json:"automatic_retries"`
	Redirects    int               `json:"redirects"`
	PublishScope string            `json:"publish_scope"`
}

func pilotReference(reference string) bool { return reference == "N10" || reference == "P11" }

func validExecutionBudget(p pilotSession) bool {
	if len(p.Limits) != len(pilotBaseLimits) || p.Retries != 0 || p.Redirects != 0 || p.PublishScope != "only_bound_account" ||
		strings.Join(p.Purposes, ",") != "service,custody,collection,publication" {
		return false
	}
	total := 0
	for k := range pilotBaseLimits {
		n, ok := p.Limits[k]
		if !ok || n < 0 || n > 100 {
			return false
		}
		total += n
	}
	return total > 0 && total <= 100 && p.Total == total
}

func newPilotGuard(c Control, clock func() time.Time) (*Guard, error) {
	rootName := "n10-requests"
	if c.Reference == "P11" {
		rootName = "p11-requests"
	}
	if (c.Mode != "read" && c.Mode != "profile" && c.Mode != "upload" && c.Mode != "send" && c.Mode != "update" && c.Mode != "card_update") ||
		(c.Mode != "send" && c.Mode != "update" && c.Mode != "card_update" && c.Body != "") || !ident.MatchString(c.Profile) || !pilotKey.MatchString(c.Key) ||
		!strings.HasSuffix(c.Key, "-"+c.Kind) || filepath.Base(c.Root) != rootName ||
		!strings.Contains(c.Root, string(filepath.Separator)+".dev-runtime"+string(filepath.Separator)) ||
		c.Session != filepath.Join(c.Root, "approval.local.json") || c.Frozen[c.Session] == "" {
		return nil, errors.New("bounded Pilot session required")
	}
	if noLinks(c.Session) != nil {
		return nil, errors.New("invalid Pilot approval path")
	}
	raw, err := os.ReadFile(c.Session)
	if err != nil || len(raw) > 65536 {
		return nil, errors.New("Pilot approval unavailable")
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != c.Frozen[c.Session] {
		return nil, errors.New("Pilot approval changed")
	}
	var p pilotSession
	if json.Unmarshal(raw, &p) != nil || !p.Approved || p.Reference != c.Reference || p.Root != c.Root ||
		(c.Reference == "P11" && !validExecutionBudget(p)) ||
		p.Profile != c.Profile || p.AppID != c.AppID || !strings.HasPrefix(p.AppID, "cli_") || !ident.MatchString(p.AppID) ||
		!strings.HasPrefix(p.ChatID, "oc_") || !ident.MatchString(p.ChatID) ||
		!strings.HasPrefix(p.AccountID, "ou_") || !ident.MatchString(p.AccountID) ||
		!strings.HasPrefix(p.BotID, "ou_") || !ident.MatchString(p.BotID) ||
		!p.StartsAt.Equal(c.StartsAt) || !p.ExpiresAt.Equal(c.ExpiresAt) {
		return nil, errors.New("Pilot identity or window differs")
	}
	if !validDailyDelegation(c, p) {
		return nil, errors.New("standing-rule delegation rejected")
	}
	g := &Guard{c: c, now: clock, pilot: &p}
	if err = g.check(); err != nil {
		return nil, err
	}
	if !pilotPath(c, p) {
		return nil, errors.New("Pilot read outside approved scope")
	}
	if c.Kind == "download" {
		if _, err = os.Lstat(filepath.Join(c.Root, c.Key+".cli.local")); !os.IsNotExist(err) {
			return nil, errors.New("download output already exists")
		}
	}
	lockPath := filepath.Join(c.Root, "http.lock.local")
	if _, err = os.Lstat(lockPath); err == nil && noLinks(lockPath) != nil {
		return nil, errors.New("invalid lock")
	}
	lock, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		return nil, err
	}
	g.lock = lock
	if c.Download != nil && c.Download.Reference == inboxDownloadReference {
		// The preview download has a separate slot in a daily run. Count original
		// markers under the shared physical lock so it cannot admit a second file.
		entries, e := os.ReadDir(c.Root)
		if e != nil {
			g.Close()
			return nil, e
		}
		count := 0
		for _, entry := range entries {
			if strings.HasPrefix(entry.Name(), "inbox-") && strings.HasSuffix(entry.Name(), ".started.local") {
				count++
			}
		}
		if p.Inbox == nil || count >= p.Inbox.MaxFiles {
			g.Close()
			return nil, errors.New("original inbox material slots exhausted")
		}
		// Same file cannot acquire another physical download under a new key.
		h := sha256.Sum256([]byte(c.Download.MessageID + ":" + c.Download.FileKey))
		marker := filepath.Join(c.Root, "inbox-"+hex.EncodeToString(h[:])+".started.local")
		f, e := os.OpenFile(marker, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if e != nil {
			g.Close()
			return nil, e
		}
		e = f.Sync()
		closeErr := f.Close()
		if e == nil {
			e = closeErr
		}
		if e == nil {
			e = syncDir(c.Root)
		}
		if e != nil {
			g.Close()
			return nil, e
		}
	}
	if c.Kind == "upload" || c.Kind == "send" {
		// Candidate identity, not a new invocation key, prevents duplicate writes.
		marker := ""
		binding := map[string]string{"key": c.Key}
		if c.Card != nil {
			marker = "card-" + c.Card.ID + "-" + c.Mode + ".started.local"
			h := sha256.Sum256([]byte(c.Body))
			binding["body_sha256"] = hex.EncodeToString(h[:])
		} else if c.Progress != nil {
			marker = "progress-" + c.Progress.MessageID + "-update.started.local"
			h := sha256.Sum256([]byte(c.Body))
			binding["body_sha256"] = hex.EncodeToString(h[:])
		} else if c.Notice != nil {
			marker = "notice-" + c.Notice.ID + "-send.started.local"
			binding["notice_sha256"] = c.Frozen[c.Notice.PlanPath]
		} else {
			marker = c.Preview.PublicationID + "-" + c.Kind + ".started.local"
			binding["pdf_sha256"] = c.Preview.SHA256
		}
		f, e := os.OpenFile(filepath.Join(c.Root, marker), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if e != nil {
			g.Close()
			return nil, e
		}
		e = json.NewEncoder(f).Encode(binding)
		if e == nil {
			e = f.Sync()
		}
		closeErr := f.Close()
		if e == nil {
			e = closeErr
		}
		if e == nil {
			e = syncDir(c.Root)
		}
		if e != nil {
			g.Close()
			return nil, e
		}
	}
	if c.Mode == "profile" {
		return g, nil
	}
	// An interrupted/finished command cannot be restarted, even under a new process.
	f, err := os.OpenFile(filepath.Join(c.Root, c.Key+".started.local"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		g.Close()
		return nil, err
	}
	err = f.Sync()
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		err = syncDir(c.Root)
	}
	if err != nil {
		g.Close()
		return nil, err
	}
	return g, nil
}

func pilotPath(c Control, p pilotSession) bool {
	if c.Card != nil || c.Mode == "card_update" {
		return dailyCardPath(c, p)
	}
	if c.Progress != nil || c.Mode == "update" {
		return dailyProgressPath(c, p)
	}
	if c.Notice != nil {
		return pilotNoticePath(c, p)
	}
	if c.Preview != nil {
		return pilotPreviewPath(c, p)
	}
	if c.Mode != "read" && c.Mode != "profile" {
		return false
	}
	if (c.Kind == "download") != (c.Download != nil) || c.Kind == "download" && c.Mode != "read" {
		return false
	}
	u, err := url.Parse(c.Path)
	if err != nil || u.IsAbs() || u.Host != "" || u.User != nil || u.Fragment != "" || u.EscapedPath() != u.Path {
		return false
	}
	q, err := url.ParseQuery(u.RawQuery)
	if err != nil || q.Encode() != u.RawQuery {
		return false
	}
	for _, v := range q {
		if len(v) != 1 {
			return false
		}
	}
	chat := "/open-apis/im/v1/chats/" + p.ChatID
	switch c.Kind {
	case "download":
		return knownPilotDownload(c, p)
	case "identity":
		return c.Path == "/open-apis/bot/v3/info" || c.Path == chat ||
			c.Path == chat+"/members?member_id_type=open_id&page_size=100"
	case "list":
		if p.Daily != nil && q.Get("container_id_type") == "thread" {
			if u.Path != "/open-apis/im/v1/messages" || (len(q) != 4 && len(q) != 5) ||
				q.Get("sort_type") != "ByCreateTimeAsc" || !knownDailyThread(c, p, q.Get("container_id")) {
				return false
			}
			for k := range q {
				if k != "container_id_type" && k != "container_id" && k != "page_size" && k != "sort_type" && k != "page_token" {
					return false
				}
			}
			size, err := strconv.Atoi(q.Get("page_size"))
			return err == nil && size >= 1 && size <= 50 && (len(q) == 4 || len(q.Get("page_token")) > 0 && len(q.Get("page_token")) <= 4096)
		}
		size, sizeErr := strconv.Atoi(q.Get("page_size"))
		validSize := q.Get("page_size") == "50" || p.Daily != nil && sizeErr == nil && size >= 1 && size <= 50
		if u.Path != "/open-apis/im/v1/messages" || (len(q) != 6 && len(q) != 7) ||
			q.Get("container_id_type") != "chat" || q.Get("container_id") != p.ChatID ||
			!validSize || q.Get("sort_type") != "ByCreateTimeAsc" {
			return false
		}
		for k := range q {
			if k != "container_id_type" && k != "container_id" && k != "page_size" && k != "sort_type" && k != "start_time" && k != "end_time" && k != "page_token" {
				return false
			}
		}
		a, e1 := strconv.ParseInt(q.Get("start_time"), 10, 64)
		b, e2 := strconv.ParseInt(q.Get("end_time"), 10, 64)
		lookback := 24 * time.Hour
		if p.Daily != nil {
			lookback = 7 * 24 * time.Hour
		}
		return e1 == nil && e2 == nil && a >= p.StartsAt.Add(-lookback).Unix() && a < b && b <= p.ExpiresAt.Unix() &&
			(len(q) == 6 || len(q.Get("page_token")) > 0 && len(q.Get("page_token")) <= 4096)
	case "message":
		if len(q) != 0 || !strings.HasPrefix(u.Path, "/open-apis/im/v1/messages/") {
			return false
		}
		messageID := strings.TrimPrefix(u.Path, "/open-apis/im/v1/messages/")
		return strings.HasPrefix(messageID, "om_") && ident.MatchString(messageID) && knownPilotMessage(c, p, messageID)
	}
	return false
}

// An exact-ID read must originate in a successful, hash-bound list response from
// the fixed group and person. Caller-supplied IDs alone are not sufficient.
func knownPilotMessage(c Control, p pilotSession, id string) bool {
	if knownDailyBotMessage(c, p, id) || knownArchiveMessage(c, p, id) || knownArchiveParent(c, p, id) {
		return true
	}
	for path, digest := range c.Frozen {
		if !strings.HasSuffix(filepath.Base(path), "-list.response.local") {
			continue
		}
		origin, eligible := receiptControl(c, p, path)
		if !eligible {
			continue
		}
		events, err := (&Guard{c: origin, pilot: &p}).events()
		if err != nil {
			continue
		}
		key := strings.TrimSuffix(filepath.Base(path), ".response.local") + ":list"
		intents, completions := 0, 0
		for _, ev := range events {
			if ev.Key != key {
				continue
			}
			if ev.Kind != "list" {
				return false
			}
			switch ev.Type {
			case "intent":
				intents++
			case "complete":
				if ev.Status != 200 || ev.SHA256 != digest {
					return false
				}
				completions++
			default:
				return false
			}
		}
		if intents != 1 || completions != 1 {
			continue
		}
		raw, e := os.ReadFile(path)
		if e != nil || len(raw) > 1024*1024 {
			continue
		}
		h := sha256.Sum256(raw)
		if hex.EncodeToString(h[:]) != digest {
			continue
		}
		var v struct {
			Code *int `json:"code"`
			Data struct {
				Items []struct {
					ID     string `json:"message_id"`
					Chat   string `json:"chat_id"`
					Sender struct {
						ID     string `json:"id"`
						Type   string `json:"sender_type"`
						IDType string `json:"id_type"`
					} `json:"sender"`
				} `json:"items"`
			} `json:"data"`
		}
		if json.Unmarshal(raw, &v) != nil || v.Code == nil || *v.Code != 0 {
			continue
		}
		for _, m := range v.Data.Items {
			if m.ID == id && m.Chat == p.ChatID && m.Sender.ID == p.AccountID && m.Sender.Type == "user" && m.Sender.IDType == "open_id" {
				return true
			}
		}
	}
	return false
}

// The official extension chain runs this before the default account provider
// opens its stored app secret. Returning nil retains the official store; this
// hook never resolves or copies credentials itself. HTTP has its own boundary.
type pilotCredentialGate struct{ g *Guard }

func (p pilotCredentialGate) Name() string  { return "kb-pilot-credential-window" }
func (p pilotCredentialGate) Priority() int { return -100 }
func (p pilotCredentialGate) ResolveAccount(context.Context) (*credential.Account, error) {
	if err := p.g.check(); err != nil {
		return nil, &credential.BlockError{Provider: p.Name(), Reason: "approved window or evidence changed"}
	}
	return nil, nil
}
func (p pilotCredentialGate) ResolveToken(context.Context, credential.TokenSpec) (*credential.Token, error) {
	return nil, &credential.BlockError{Provider: p.Name(), Reason: "use the selected official credential source"}
}

func (g *Guard) pilotLimits() map[string]int {
	limits := map[string]int{}
	if g.pilot.Reference == "P11" {
		for k, v := range g.pilot.Limits {
			limits[k] = v
		}
		return limits // A new session never inherits N10's extension.
	}
	for k, v := range pilotBaseLimits {
		limits[k] = v
	}
	// Expansion is a separately persisted, frozen decision. It does not clear the
	// journal or permit retries. A malformed extension grants no additional calls.
	path := filepath.Join(g.c.Root, "extension.local.json")
	if g.c.Frozen[path] == "" {
		return limits
	}
	raw, e := os.ReadFile(path)
	if e != nil || len(raw) > 16384 {
		return limits
	}
	h := sha256.Sum256(raw)
	if hex.EncodeToString(h[:]) != g.c.Frozen[path] {
		return limits
	}
	var v struct {
		Reference   string `json:"reference"`
		Reason      string `json:"reason"`
		ApprovalSHA string `json:"approval_sha256"`
		Total       int    `json:"maximum_total"`
		Prior       int    `json:"prior_attempts"`
	}
	if json.Unmarshal(raw, &v) != nil || v.Reference != "N10" || strings.TrimSpace(v.Reason) == "" || v.Total != 100 || v.Prior < 1 || v.Prior > 50 || v.ApprovalSHA != g.c.Frozen[g.c.Session] {
		return limits
	}
	events, e := g.events()
	if e != nil {
		return limits
	}
	n := 0
	for _, ev := range events {
		if ev.Type == "intent" {
			n++
		}
	}
	if n < v.Prior {
		return limits
	}
	for k, v := range limits {
		limits[k] = 2 * v
	}
	return limits
}
