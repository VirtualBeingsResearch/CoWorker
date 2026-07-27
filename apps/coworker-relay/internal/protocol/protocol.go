package protocol

type Header [2]string

type Message struct {
	Type             string   `json:"type"`
	RequestID        string   `json:"request_id,omitempty"`
	Method           string   `json:"method,omitempty"`
	Path             string   `json:"path,omitempty"`
	RawPath          string   `json:"raw_path,omitempty"`
	Query            string   `json:"query,omitempty"`
	Headers          []Header `json:"headers,omitempty"`
	RelayHeaderStart int      `json:"relay_header_start,omitempty"`
	Body             string   `json:"body,omitempty"`
	ClientIP         string   `json:"client_ip,omitempty"`
	Status           int      `json:"status,omitempty"`
	More             bool     `json:"more,omitempty"`
	Error            string   `json:"error,omitempty"`
	Verifier         string   `json:"verifier,omitempty"`
	Generation       string   `json:"generation,omitempty"`
	SentAt           float64  `json:"sent_at,omitempty"`
}
