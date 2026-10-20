package main

import (
	"mime"
	"net/http"
)

// No request URLs, authorization headers, cookies or file contents are logged.
// These facts explain a stop; they can never promote it to a successful receipt.
type pilotDownloadDiagnostic struct {
	ReceivedBytes int64    `json:"received_bytes"`
	DeclaredBytes int64    `json:"declared_bytes"`
	MaximumBytes  int64    `json:"maximum_bytes"`
	MediaType     string   `json:"media_type"`
	Reasons       []string `json:"reasons"`
}

func newPilotDownloadDiagnostic(resp *http.Response, n, maximum int64, ioError, scopeError error) *pilotDownloadDiagnostic {
	media, _, err := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if err != nil || len(media) > 128 {
		media = "invalid_or_missing"
	}
	d := &pilotDownloadDiagnostic{n, resp.ContentLength, maximum, media, []string{}}
	if ioError != nil {
		d.Reasons = append(d.Reasons, "stream_or_storage_failed")
	}
	if resp.StatusCode != http.StatusOK {
		d.Reasons = append(d.Reasons, "http_status_rejected")
	}
	if n < 1 || n > maximum {
		d.Reasons = append(d.Reasons, "byte_limit_rejected")
	}
	if resp.ContentLength >= 0 && resp.ContentLength != n {
		d.Reasons = append(d.Reasons, "content_length_mismatch")
	}
	if scopeError != nil {
		d.Reasons = append(d.Reasons, "frozen_scope_or_window_changed")
	}
	return d
}
