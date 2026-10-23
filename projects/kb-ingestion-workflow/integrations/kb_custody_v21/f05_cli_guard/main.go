// A fixed read-only entry around the official CLI. No secret export or proxy.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/larksuite/cli/cmd"
	"github.com/larksuite/cli/extension/credential"
	transport "github.com/larksuite/cli/extension/transport"
)

func main() {
	if len(os.Args) == 4 && os.Args[1] == "--card-listener" {
		os.Exit(runCardListener(os.Args[2], os.Args[3]))
	}
	os.Exit(run())
}

func readControl(reader io.Reader) (Control, error) {
	var c Control
	// The full source inventory now exceeds 64 KiB. Keep one bounded envelope,
	// including every source hash; do not silently truncate its frozen set.
	raw, err := io.ReadAll(io.LimitReader(reader, 1024*1024+1))
	if err != nil || len(raw) > 1024*1024 {
		return c, fmt.Errorf("control limit")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var extra any
	if dec.Decode(&c) != nil || dec.Decode(&extra) != io.EOF {
		return c, fmt.Errorf("invalid control")
	}
	return c, nil
}

func run() int {
	c, err := readControl(os.Stdin)
	if err != nil {
		fmt.Println(`{"ok":false,"error":"invalid control"}`)
		return 1
	}
	g, err := NewGuard(c, time.Now)
	if err != nil {
		fmt.Println(`{"ok":false,"error":"guard setup rejected"}`)
		return 1
	}
	defer g.Close()
	ok := executeCLI(c, g)
	json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": ok, "mode": c.Mode, "credential_exported": false})
	if !ok {
		return 1
	}
	return 0
}

func cliArgs(c Control) []string {
	if c.Mode == "profile" {
		return []string{"f05-cli-guard", "config", "show", "--profile", c.Profile}
	}
	path, _, hasQuery := strings.Cut(c.Path, "?")
	if c.Mode == "upload" {
		fields, _ := json.Marshal(map[string]string{"file_name": c.Preview.Filename, "file_type": "pdf"})
		return []string{"f05-cli-guard", "api", "POST", path, "--as", "bot", "--profile", c.Profile,
			"--format", "json", "--data", string(fields), "--file", "file=" + filepath.Base(c.Preview.PDFPath)}
	}
	if c.Mode == "card_update" {
		return []string{"f05-cli-guard", "api", "PATCH", path, "--as", "bot", "--profile", c.Profile, "--format", "json", "--data", c.Body}
	}
	if c.Card != nil && c.Mode == "send" {
		return []string{"f05-cli-guard", "api", "POST", path, "--as", "bot", "--profile", c.Profile, "--format", "json", "--data", c.Body}
	}
	if c.Mode == "update" {
		return []string{"f05-cli-guard", "api", "PUT", path, "--as", "bot", "--profile", c.Profile,
			"--format", "json", "--data", c.Body}
	}
	if c.Mode == "send" {
		return []string{"f05-cli-guard", "api", "POST", path, "--as", "bot", "--profile", c.Profile,
			"--format", "json", "--params", `{"receive_id_type":"chat_id"}`, "--data", c.Body}
	}
	args := []string{"f05-cli-guard", "api", "GET", path, "--as", "bot", "--profile", c.Profile, "--format", "json"}
	if hasQuery {
		if pilotReference(c.Reference) {
			u, _ := url.Parse(c.Path) // newPilotGuard already validated the complete path.
			params := map[string]string{}
			for k, values := range u.Query() {
				params[k] = values[0]
			}
			encoded, _ := json.Marshal(params)
			args = append(args, "--params", string(encoded))
		} else {
			args = append(args, "--params", `{"member_id_type":"open_id","page_size":100}`)
		}
	}
	if pilotReference(c.Reference) && c.Kind == "download" {
		// Raw API performs one bounded GET. The convenience download shortcut
		// enables multipart/retries and is deliberately not used in this window.
		args = append(args, "--output", filepath.Join(c.Root, c.Key+".cli.local"))
	}
	return args
}

func executeCLI(c Control, g *Guard, opts ...cmd.BuildOption) bool {
	if pilotReference(c.Reference) && (c.Download != nil || c.Mode == "upload") {
		// Keep the official file allowlist: the private request directory is
		// this one-shot process's cwd, on both macOS and Linux test runners.
		old, err := os.Getwd()
		cwd := c.Root
		if c.Mode == "upload" {
			cwd = filepath.Dir(c.Preview.PDFPath)
		}
		if err != nil || noLinks(cwd) != nil || os.Chdir(cwd) != nil {
			return false
		}
		defer os.Chdir(old)
	}
	if g.pilot != nil {
		if g.checkCached() != nil {
			return false
		}
		credential.Register(pilotCredentialGate{g})
	}
	transport.Register(g)
	// Keep the standard TLS validation, keychain, profile and risk controls.
	// No env-credential provider is imported. No global config is changed.
	os.Setenv("LARKSUITE_CLI_NO_UPDATE_NOTIFIER", "1")
	os.Setenv("LARKSUITE_CLI_NO_SKILLS_NOTIFIER", "1")
	os.Setenv("GODEBUG", "http2client=0")
	os.Args = cliArgs(c)
	var out cappedBuffer
	// Errors may include provider data. Never echo them to the operator.
	code := cmd.ExecuteWithOptions(append(opts, cmd.WithIO(bytes.NewReader(nil), &out, io.Discard))...)
	var obj map[string]any
	err := json.Unmarshal(out.Bytes(), &obj)
	ok := code == 0 && err == nil && !out.over && !g.failed
	if c.Mode == "profile" {
		ok = ok && obj["profile"] == c.Profile && obj["appId"] == c.AppID && obj["brand"] == "feishu"
	} else if pilotReference(c.Reference) && c.Download != nil {
		// Pinned CLI v1.0.97 raw binary output is saved_path/size_bytes,
		// not the JSON API success envelope. Verify its actual file as well.
		ok = ok && pilotSavedFile(c, obj)
	} else {
		ok = ok && obj["ok"] == true && obj["identity"] == "bot" && !g.failed
	}
	if g.pilot != nil {
		ok = ok && g.checkCached() == nil
	}
	return ok
}

type cappedBuffer struct {
	bytes.Buffer
	over bool
}

func (b *cappedBuffer) Write(p []byte) (int, error) {
	if b.Len()+len(p) > 1024*1024 {
		b.over = true
		return 0, fmt.Errorf("output limit")
	}
	return b.Buffer.Write(p)
}
