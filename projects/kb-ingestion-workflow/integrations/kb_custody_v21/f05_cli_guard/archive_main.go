package main

import (
	"encoding/json"
	"reflect"
)

func mainCardEnabled(c Control, path string) bool {
	var rules struct {
		Archive struct {
			Reference string `json:"reference"`
			Enabled   bool   `json:"enabled"`
		} `json:"archive_cards"`
	}
	return frozenJSON(c, path, &rules) && rules.Archive.Reference == "archive-main-v1" && rules.Archive.Enabled
}

func mainCardBinding(v *dailyCard) bool {
	b := v.Binding
	details, ok := b["details"].(string)
	if len(b) != 6 || b["document_id"] != b["target_message_id"] || !ok || len(details) == 0 || len(details) > 2048 {
		return false
	}
	if b["decision"] == nil {
		return true
	}
	action, ok := b["decision"].(map[string]any)
	if !ok || len(action) != 2 {
		return false
	}
	id, ok := action["card_id"].(string)
	if !ok || !cardSHA.MatchString(id) {
		return false
	}
	child, ok := action["binding"].(map[string]any)
	if !ok || child["document_id"] != b["document_id"] || child["purpose"] != "preview" && child["purpose"] != "images" {
		return false
	}
	return cardBinding(&dailyCard{ID: id, Title: v.Title, Binding: child})
}

func mainCardDecisionReceipt(c Control, p pilotSession, v *dailyCard) bool {
	if v.Binding["decision"] == nil {
		return v.Decision == ""
	}
	action := v.Binding["decision"].(map[string]any)
	b := action["binding"].(map[string]any)
	kind := "file"
	if b["purpose"] == "images" {
		kind = "text"
	}
	for _, m := range dailyReceiptMessages(c, p, v.Decision) {
		if m["message_id"] == b["target_message_id"] && m["msg_type"] == kind && dailyBotMessage(m, p) {
			return true
		}
	}
	return false
}

func mainCardPrevious(v *dailyCard) bool {
	if v.PreviousBinding == nil {
		return false
	}
	for _, k := range []string{"purpose", "document_id", "target_message_id", "source_sha256"} {
		if v.PreviousBinding[k] != v.Binding[k] {
			return false
		}
	}
	previous := *v
	previous.Binding = v.PreviousBinding
	previous.Result = ""
	if !cardBinding(&previous) {
		return false
	}
	var actual map[string]any
	return json.Unmarshal([]byte(v.Previous), &actual) == nil && reflect.DeepEqual(actual, cardJSON(&previous))
}
