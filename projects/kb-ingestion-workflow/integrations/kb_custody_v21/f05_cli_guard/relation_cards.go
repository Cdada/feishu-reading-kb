package main

// These commands and render fields mirror relation_cards.py. Physical writes
// additionally require an explicitly enabled P42 profile in the frozen rules.
import "strings"

var relationCommands = map[string][]string{
	"relation_candidates": {"候选 1", "候选 2", "候选 3", "候选 4", "候选 5", "结束关联"},
	"relation_type":       {"本篇是解读，候选是原文", "候选是解读，本篇是原文", "本篇是摘要或摘录，候选是原文", "候选是摘要或摘录，本篇是原文", "本篇是修订版，候选是旧版", "候选是修订版，本篇是旧版", "仅主题相关", "取消本次关联"},
	"relation_manage":     {"更正关联", "撤销关联"},
}

func relationPurpose(v any) bool {
	purpose, _ := v.(string)
	_, ok := relationCommands[purpose]
	return ok
}

func relationEnabled(c Control, path string) bool {
	var rules struct {
		Relations struct {
			Reference string `json:"reference"`
			Enabled   bool   `json:"enabled"`
		} `json:"relations"`
		Reading struct {
			Reference string `json:"reference"`
		} `json:"reading"`
	}
	return frozenJSON(c, path, &rules) && rules.Relations.Reference == "P42-relations-v1" && rules.Relations.Enabled && rules.Reading.Reference == "P41-reading-v1"
}

func relationBinding(b map[string]any) bool {
	if len(b) != 9 {
		return false
	}
	for _, field := range []string{"material_id", "snapshot_sha256"} {
		value, _ := b[field].(string)
		if !cardSHA.MatchString(value) {
			return false
		}
	}
	detail, _ := b["details"].(string)
	revision, ok := b["revision"].(float64)
	if strings.TrimSpace(detail) == "" || len(detail) > 12000 || !ok || revision < 0 || revision > 999999999 || revision != float64(int(revision)) {
		return false
	}
	if b["purpose"] == "relation_candidates" {
		n, ok := b["candidate_count"].(float64)
		return ok && n >= 0 && n <= 5 && n == float64(int(n))
	}
	pair, _ := b["pair_id"].(string)
	return cardSHA.MatchString(pair)
}

func relationCard(b map[string]any) (string, string, []string) {
	purpose := b["purpose"].(string)
	title := map[string]string{"relation_candidates": "选择可能关联的资料", "relation_type": "确认资料关系", "relation_manage": "管理已确认关联"}[purpose]
	commands := relationCommands[purpose]
	if purpose == "relation_candidates" {
		n := int(b["candidate_count"].(float64))
		commands = append([]string{}, commands[:n]...)
		if n > 0 {
			commands = append(commands, "结束关联")
		}
	}
	return title, b["details"].(string), commands
}

func knownCardCommand(command string) bool {
	for _, value := range []string{"确认入库", "暂不入库", "补充材料后再入库", "按缺图版本继续", "重新发文件"} {
		if value == command {
			return true
		}
	}
	for _, commands := range relationCommands {
		for _, value := range commands {
			if value == command {
				return true
			}
		}
	}
	return false
}
