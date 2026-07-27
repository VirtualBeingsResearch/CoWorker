package server

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/auth"
	relaycache "github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/cache"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/protocol"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/store"
	"github.com/coder/websocket"
)

const (
	protocolVersion = "1"
	maxRequestBody  = 32 << 20
	maxTunnelFrame  = 48 << 20
)

type Config struct {
	PublicURL        string
	AdminToken       string
	TrustedProxies   []netip.Prefix
	VerifierParallel int
	Cache            *relaycache.Cache
}

type responseEvent struct {
	message protocol.Message
	err     error
}

type tunnel struct {
	instanceID string
	conn       *websocket.Conn
	sendMu     sync.Mutex
	pendingMu  sync.Mutex
	pending    map[string]chan responseEvent
}

func (t *tunnel) send(ctx context.Context, value protocol.Message) error {
	t.sendMu.Lock()
	defer t.sendMu.Unlock()
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return t.conn.Write(ctx, websocket.MessageText, raw)
}

func (t *tunnel) closePending(err error) {
	t.pendingMu.Lock()
	defer t.pendingMu.Unlock()
	for id, ch := range t.pending {
		select {
		case ch <- responseEvent{err: err}:
		default:
		}
		close(ch)
		delete(t.pending, id)
	}
}

type windowCounter struct {
	start time.Time
	count int
}

type Server struct {
	config    Config
	store     *store.Store
	logger    *slog.Logger
	tunnelsMu sync.RWMutex
	tunnels   map[string]*tunnel
	verifySem chan struct{}
	cache     *relaycache.Cache
	anonMu    sync.Mutex
	anonymous map[string]windowCounter
	requests  map[string]windowCounter
}

func New(config Config, database *store.Store, logger *slog.Logger) *Server {
	parallel := config.VerifierParallel
	if parallel <= 0 {
		parallel = 4
	}
	return &Server{
		config:    config,
		store:     database,
		logger:    logger,
		tunnels:   make(map[string]*tunnel),
		verifySem: make(chan struct{}, parallel),
		cache:     config.Cache,
		anonymous: make(map[string]windowCounter),
		requests:  make(map[string]windowCounter),
	}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /_relay/v1/health", s.health)
	mux.HandleFunc("POST /_relay/v1/enroll", s.enroll)
	mux.HandleFunc("GET /_relay/v1/connect", s.connect)
	mux.HandleFunc("POST /_relay/v1/admin/instances", s.adminCreateInstance)
	mux.HandleFunc("GET /_relay/v1/admin/instances", s.adminListInstances)
	mux.HandleFunc("DELETE /_relay/v1/admin/instances/{instance}", s.adminDeleteInstance)
	mux.HandleFunc("PATCH /_relay/v1/admin/instances/{instance}/update-auth", s.adminUpdateAuth)
	mux.HandleFunc("GET /_relay/v1/admin/instances/{instance}/update-stats", s.adminUpdateStats)
	mux.HandleFunc("GET /_relay/v1/admin/bans", s.adminListBans)
	mux.HandleFunc("DELETE /_relay/v1/admin/bans", s.adminRemoveBan)
	mux.HandleFunc("GET /_relay/v1/admin/cache", s.adminCacheStats)
	mux.HandleFunc("DELETE /_relay/v1/admin/cache", s.adminCachePurge)
	mux.HandleFunc("/", s.facade)
	return s.securityHeaders(mux)
}

func (s *Server) securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Cache-Control", "no-store")
		next.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, code string) {
	writeJSON(w, status, map[string]string{"error": code})
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "protocol_version": 1})
}

func (s *Server) enroll(w http.ResponseWriter, r *http.Request) {
	var input struct {
		PairingCode     string `json:"pairing_code"`
		Verifier        string `json:"verifier"`
		ProtocolVersion int    `json:"protocol_version"`
	}
	if err := decodeJSON(r, &input, 16<<10); err != nil || input.ProtocolVersion != 1 || len(input.Verifier) > 512 {
		writeError(w, http.StatusBadRequest, "invalid_enrollment")
		return
	}
	if err := auth.ValidateArgon2id(input.Verifier); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_enrollment")
		return
	}
	instance, credential, err := s.store.Enroll(input.PairingCode, input.Verifier)
	if err != nil {
		writeError(w, http.StatusUnauthorized, "pairing_rejected")
		return
	}
	s.logger.Info("relay enrollment", "instance_id", instance.ID, "result", "ok")
	writeJSON(w, http.StatusOK, map[string]any{
		"instance_id":         instance.ID,
		"instance_credential": credential,
		"public_base_url":     strings.TrimRight(s.config.PublicURL, "/") + "/i/" + instance.ID,
		"protocol_version":    1,
	})
}

func decodeJSON(r *http.Request, target any, limit int64) error {
	defer r.Body.Close()
	decoder := json.NewDecoder(io.LimitReader(r.Body, limit))
	decoder.DisallowUnknownFields()
	return decoder.Decode(target)
}

func (s *Server) connect(w http.ResponseWriter, r *http.Request) {
	instanceID := r.Header.Get("X-Coworker-Relay-Instance")
	if r.Header.Get("X-Coworker-Relay-Protocol") != protocolVersion {
		writeError(w, http.StatusUpgradeRequired, "protocol_incompatible")
		return
	}
	credential, ok := auth.ParseBearer(r.Header.Get("Authorization"))
	if !ok {
		writeError(w, http.StatusUnauthorized, "instance_auth_required")
		return
	}
	if _, err := s.store.AuthenticateInstance(instanceID, credential); err != nil {
		writeError(w, http.StatusUnauthorized, "instance_auth_invalid")
		return
	}
	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
		CompressionMode: websocket.CompressionDisabled,
	})
	if err != nil {
		return
	}
	conn.SetReadLimit(maxTunnelFrame)
	current := &tunnel{instanceID: instanceID, conn: conn, pending: make(map[string]chan responseEvent)}
	s.tunnelsMu.Lock()
	previous := s.tunnels[instanceID]
	s.tunnels[instanceID] = current
	s.tunnelsMu.Unlock()
	if previous != nil {
		_ = previous.conn.Close(websocket.StatusPolicyViolation, "superseded by a newer connection")
	}
	s.logger.Info("relay tunnel connected", "instance_id", instanceID)
	heartbeatCtx, stopHeartbeat := context.WithCancel(r.Context())
	defer stopHeartbeat()
	go func() {
		ticker := time.NewTicker(20 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-heartbeatCtx.Done():
				return
			case sent := <-ticker.C:
				if err := current.send(heartbeatCtx, protocol.Message{
					Type:   "ping",
					SentAt: float64(sent.UnixNano()) / float64(time.Second),
				}); err != nil {
					_ = current.conn.Close(websocket.StatusInternalError, "heartbeat failed")
					return
				}
			}
		}
	}()
	err = s.readTunnel(r.Context(), current)
	current.closePending(errors.New("instance tunnel closed"))
	s.tunnelsMu.Lock()
	if s.tunnels[instanceID] == current {
		delete(s.tunnels, instanceID)
	}
	s.tunnelsMu.Unlock()
	_ = conn.Close(websocket.StatusNormalClosure, "")
	s.logger.Info("relay tunnel disconnected", "instance_id", instanceID, "error", safeError(err))
}

func (s *Server) readTunnel(ctx context.Context, current *tunnel) error {
	for {
		kind, raw, err := current.conn.Read(ctx)
		if err != nil {
			return err
		}
		if kind != websocket.MessageText {
			continue
		}
		var message protocol.Message
		if err := json.Unmarshal(raw, &message); err != nil {
			return errors.New("invalid tunnel frame")
		}
		switch message.Type {
		case "verifier":
			if len(message.Verifier) > 512 || !strings.HasPrefix(message.Verifier, "$argon2id$") {
				return errors.New("invalid verifier frame")
			}
			if err := auth.ValidateArgon2id(message.Verifier); err != nil {
				return errors.New("invalid verifier frame")
			}
			instance, err := s.store.GetInstance(current.instanceID)
			if err != nil {
				return err
			}
			if err := s.store.UpdateVerifier(current.instanceID, message.Verifier, message.Generation); err != nil {
				return err
			}
			if instance.VerifierGeneration != "" && instance.VerifierGeneration != message.Generation {
				current.closePending(errors.New("communication token rotated"))
			}
			if err := current.send(ctx, protocol.Message{
				Type:       "verifier_ack",
				Generation: message.Generation,
			}); err != nil {
				return err
			}
		case "pong":
			continue
		case "response_start", "response_body", "response_error":
			current.pendingMu.Lock()
			ch := current.pending[message.RequestID]
			current.pendingMu.Unlock()
			if ch != nil {
				select {
				case ch <- responseEvent{message: message}:
				case <-ctx.Done():
					return ctx.Err()
				default:
					current.pendingMu.Lock()
					if current.pending[message.RequestID] == ch {
						delete(current.pending, message.RequestID)
						close(ch)
					}
					current.pendingMu.Unlock()
					go s.cancelOverloadedStream(current, message.RequestID)
				}
			}
		default:
			return errors.New("unknown tunnel frame")
		}
	}
}

func (s *Server) cancelOverloadedStream(current *tunnel, requestID string) {
	cancelCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = current.send(cancelCtx, protocol.Message{Type: "cancel", RequestID: requestID})
}

func (s *Server) facade(w http.ResponseWriter, r *http.Request) {
	instanceID, path, ok := splitInstancePath(r.URL.Path)
	if !ok {
		writeError(w, http.StatusNotFound, "not_found")
		return
	}
	instance, err := s.store.GetInstance(instanceID)
	if err != nil {
		writeError(w, http.StatusNotFound, "instance_not_found")
		return
	}
	category, allowed := routeCategory(r.Method, path)
	if !allowed {
		writeError(w, http.StatusNotFound, "not_found")
		return
	}
	ip := s.clientIP(r)
	requestID := newRequestID()
	w.Header().Set("X-Coworker-Relay-Request-Id", requestID)
	if !s.allowRequest(instanceID, ip) {
		s.logAccess(instanceID, ip, category, "rate_limited", requestID)
		w.Header().Set("Retry-After", "60")
		writeError(w, http.StatusTooManyRequests, "rate_limited")
		return
	}
	if ban, active, _ := s.store.ActiveBan(instanceID, ip, time.Now().UTC()); active {
		s.logAccess(instanceID, ip, category, "banned", requestID)
		w.Header().Set("Retry-After", auth.RetryAfterSeconds(ban.Until.Unix(), time.Now().Unix()))
		writeError(w, http.StatusTooManyRequests, "temporarily_banned")
		return
	}
	authorization, hasAuthorization := r.Header["Authorization"]
	rawAuthorization := ""
	if len(authorization) > 0 {
		rawAuthorization = authorization[0]
	}
	if category == "status" && !hasAuthorization {
		if !s.allowAnonymous(instanceID, ip) {
			s.logAccess(instanceID, ip, category, "rate_limited", requestID)
			writeError(w, http.StatusTooManyRequests, "rate_limited")
			return
		}
		s.logAccess(instanceID, ip, category, "anonymous", requestID)
		writeJSON(w, http.StatusOK, map[string]any{})
		return
	}
	requiresAuth := category != "update" || instance.UpdateAuthMode == "required"
	if !hasAuthorization {
		if requiresAuth {
			if !s.allowAnonymous(instanceID, ip) {
				s.logAccess(instanceID, ip, category, "rate_limited", requestID)
				writeError(w, http.StatusTooManyRequests, "rate_limited")
				return
			}
			s.logAccess(instanceID, ip, category, "missing", requestID)
			writeError(w, http.StatusUnauthorized, "bearer_required")
			return
		}
		if !s.allowAnonymous(instanceID, ip) {
			s.logAccess(instanceID, ip, category, "rate_limited", requestID)
			writeError(w, http.StatusTooManyRequests, "rate_limited")
			return
		}
	} else {
		token, validFormat := auth.ParseBearer(rawAuthorization)
		valid := validFormat && s.verify(instance.Verifier, token)
		if !valid {
			banned, until, _ := s.store.RecordFailure(instanceID, ip, time.Now().UTC())
			result := "invalid"
			if banned {
				result = "banned"
				w.Header().Set("Retry-After", auth.RetryAfterSeconds(until.Unix(), time.Now().Unix()))
				writeError(w, http.StatusTooManyRequests, "temporarily_banned")
			} else {
				writeError(w, http.StatusUnauthorized, "bearer_invalid")
			}
			s.logAccess(instanceID, ip, category, result, requestID)
			return
		}
	}
	authResult := "authenticated"
	if !hasAuthorization {
		authResult = "anonymous"
	}
	s.logAccess(instanceID, ip, category, authResult, requestID)
	cacheKey := ""
	var unlock func()
	if category == "update" && strings.HasPrefix(path, "/api/desktop-updates/assets/") && s.cache != nil {
		cacheKey = relaycache.Key(instanceID, path+"?"+r.URL.RawQuery)
		unlock = s.cache.Lock(cacheKey)
		defer unlock()
		if entry, found := s.cache.Get(cacheKey); found {
			s.serveCached(w, r, entry)
			return
		}
		if r.Header.Get("Range") != "" {
			cacheKey = ""
		}
	}
	if category == "update" {
		if version := updateCheckVersion(path); version != "" {
			if err := s.store.RecordUpdateCheck(instanceID, version, hasAuthorization, time.Now().UTC()); err != nil {
				s.logger.Warn("relay update statistics failed", "instance_id", instanceID, "error", safeError(err))
			}
		}
	}
	s.forward(w, r, instanceID, path, ip, cacheKey, requestID)
}

func (s *Server) logAccess(instanceID, ip, category, result, requestID string) {
	s.logger.Info(
		"relay access",
		"instance_id", instanceID,
		"source_ip", ip,
		"route", category,
		"auth_result", result,
		"request_id", requestID,
	)
}

func splitInstancePath(path string) (string, string, bool) {
	if !strings.HasPrefix(path, "/i/") {
		return "", "", false
	}
	rest := strings.TrimPrefix(path, "/i/")
	index := strings.IndexByte(rest, '/')
	if index < 1 {
		return "", "", false
	}
	instanceID, forwarded := rest[:index], rest[index:]
	if !strings.HasPrefix(instanceID, "cw_") || strings.Contains(forwarded, "..") {
		return "", "", false
	}
	return instanceID, forwarded, true
}

func routeCategory(method, path string) (string, bool) {
	switch {
	case method == http.MethodGet && path == "/status":
		return "status", true
	case method == http.MethodPost && path == "/messages":
		return "message", true
	case method == http.MethodGet && strings.HasPrefix(path, "/sse/"):
		return "sse", true
	case (method == http.MethodGet || method == http.MethodPost) && path == "/api/communicate/register":
		return "registration", true
	case method == http.MethodDelete && strings.HasPrefix(path, "/api/communicate/register/"):
		return "registration", true
	case method == http.MethodGet && allowedUpdatePath(path):
		return "update", true
	default:
		return "", false
	}
}

func allowedUpdatePath(path string) bool {
	const prefix = "/api/desktop-updates/"
	if !strings.HasPrefix(path, prefix) {
		return false
	}
	rest := strings.TrimPrefix(path, prefix)
	if strings.HasPrefix(rest, "assets/") || strings.HasPrefix(rest, "feed/v1/") {
		return true
	}
	if strings.HasPrefix(rest, "releases") || strings.HasPrefix(rest, "statistics") {
		return false
	}
	return len(strings.Split(rest, "/")) == 3
}

func updateCheckVersion(path string) string {
	const prefix = "/api/desktop-updates/"
	rest := strings.TrimPrefix(path, prefix)
	if rest == path || strings.HasPrefix(rest, "assets/") || strings.HasPrefix(rest, "feed/") {
		return ""
	}
	parts := strings.Split(rest, "/")
	if len(parts) == 3 {
		return parts[2]
	}
	return ""
}

func (s *Server) verify(verifier, token string) bool {
	if verifier == "" {
		return false
	}
	select {
	case s.verifySem <- struct{}{}:
		defer func() { <-s.verifySem }()
	case <-time.After(2 * time.Second):
		return false
	}
	ok, err := auth.VerifyArgon2id(verifier, token)
	return err == nil && ok
}

func (s *Server) allowAnonymous(instanceID, ip string) bool {
	return s.allowWindow(s.anonymous, instanceID+"\x00"+ip, 60)
}

func (s *Server) allowRequest(instanceID, ip string) bool {
	return s.allowWindow(s.requests, instanceID+"\x00"+ip, 600)
}

func (s *Server) allowWindow(
	windows map[string]windowCounter,
	key string,
	limit int,
) bool {
	now := time.Now()
	s.anonMu.Lock()
	defer s.anonMu.Unlock()
	if len(windows) > 10_000 {
		for candidate, window := range windows {
			if now.Sub(window.start) >= time.Minute {
				delete(windows, candidate)
			}
		}
	}
	window := windows[key]
	if now.Sub(window.start) >= time.Minute {
		window = windowCounter{start: now}
	}
	window.count++
	windows[key] = window
	return window.count <= limit
}

func (s *Server) forward(
	w http.ResponseWriter,
	r *http.Request,
	instanceID, path, ip, cacheKey, requestID string,
) {
	s.tunnelsMu.RLock()
	current := s.tunnels[instanceID]
	s.tunnelsMu.RUnlock()
	if current == nil {
		writeError(w, http.StatusServiceUnavailable, "instance_offline")
		return
	}
	controller := http.NewResponseController(w)
	_ = controller.SetReadDeadline(time.Now().Add(30 * time.Second))
	body, err := io.ReadAll(io.LimitReader(r.Body, maxRequestBody+1))
	_ = controller.SetReadDeadline(time.Time{})
	if err != nil || len(body) > maxRequestBody {
		writeError(w, http.StatusRequestEntityTooLarge, "request_too_large")
		return
	}
	headers := orderedHeaders(r.Header)
	relayHeaderStart := len(headers)
	originalTarget := r.RequestURI
	headers = append(headers,
		protocol.Header{"X-Coworker-Relay", "v1"},
		protocol.Header{"X-Coworker-Relay-Instance", instanceID},
		protocol.Header{"X-Coworker-Relay-Request-Id", requestID},
		protocol.Header{"X-Coworker-Relay-Original-URL", strings.TrimRight(s.config.PublicURL, "/") + originalTarget},
		protocol.Header{"X-Coworker-Relay-Original-Target", originalTarget},
		protocol.Header{"Forwarded", "for=" + forwardedIP(ip) + ";proto=https;host=" + s.publicHost()},
	)
	rawPath := path
	if escaped := r.URL.EscapedPath(); escaped != "" {
		prefix := "/i/" + instanceID
		rawPath = strings.TrimPrefix(escaped, prefix)
	}
	message := protocol.Message{
		Type:             "request",
		RequestID:        requestID,
		Method:           r.Method,
		Path:             path,
		RawPath:          rawPath,
		Query:            r.URL.RawQuery,
		Headers:          headers,
		RelayHeaderStart: relayHeaderStart,
		Body:             base64.StdEncoding.EncodeToString(body),
		ClientIP:         ip,
	}
	ch := make(chan responseEvent, 32)
	current.pendingMu.Lock()
	current.pending[requestID] = ch
	current.pendingMu.Unlock()
	defer func() {
		current.pendingMu.Lock()
		delete(current.pending, requestID)
		current.pendingMu.Unlock()
	}()
	sendCtx, cancelSend := context.WithTimeout(r.Context(), 15*time.Second)
	err = current.send(sendCtx, message)
	cancelSend()
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "instance_offline")
		return
	}
	started := false
	var cacheWriter *relaycache.Writer
	abortCache := func() {
		if cacheWriter != nil {
			cacheWriter.Abort()
			cacheWriter = nil
		}
	}
	defer abortCache()
	for {
		select {
		case <-r.Context().Done():
			cancelCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			_ = current.send(cancelCtx, protocol.Message{Type: "cancel", RequestID: requestID})
			cancel()
			return
		case event, open := <-ch:
			if !open || event.err != nil {
				if !started {
					writeError(w, http.StatusBadGateway, "tunnel_closed")
				}
				return
			}
			switch event.message.Type {
			case "response_start":
				for _, header := range event.message.Headers {
					if !hopByHopHeader(header[0]) {
						w.Header().Add(header[0], header[1])
					}
				}
				w.Header().Set("X-Coworker-Relay-Request-Id", requestID)
				w.WriteHeader(event.message.Status)
				started = true
				if cacheKey != "" && event.message.Status == http.StatusOK && s.cache != nil {
					cacheHeaders := make([]relaycache.Header, 0, len(event.message.Headers))
					for _, header := range event.message.Headers {
						if !hopByHopHeader(header[0]) && !strings.EqualFold(header[0], "Set-Cookie") {
							cacheHeaders = append(cacheHeaders, relaycache.Header(header))
						}
					}
					cacheWriter, _ = s.cache.Begin(cacheKey, event.message.Status, cacheHeaders)
				}
				if flusher, ok := w.(http.Flusher); ok {
					flusher.Flush()
				}
			case "response_body":
				chunk, decodeErr := base64.StdEncoding.DecodeString(event.message.Body)
				if decodeErr != nil {
					return
				}
				if len(chunk) > 0 {
					_ = controller.SetWriteDeadline(time.Now().Add(30 * time.Second))
					if _, writeErr := w.Write(chunk); writeErr != nil {
						return
					}
					_ = controller.SetWriteDeadline(time.Time{})
					if cacheWriter != nil {
						if writeErr := cacheWriter.Write(chunk); writeErr != nil {
							abortCache()
						}
					}
					if flusher, ok := w.(http.Flusher); ok {
						flusher.Flush()
					}
				}
				if !event.message.More {
					if cacheWriter != nil {
						if commitErr := cacheWriter.Commit(); commitErr != nil {
							s.logger.Warn("relay cache commit failed", "request_id", requestID, "error", safeError(commitErr))
						}
						cacheWriter = nil
					}
					return
				}
			case "response_error":
				if !started {
					writeError(w, http.StatusBadGateway, "coworker_request_failed")
				}
				return
			}
		case <-time.After(30 * time.Second):
			if !started {
				writeError(w, http.StatusGatewayTimeout, "coworker_timeout")
				return
			}
		}
	}
}

func (s *Server) publicHost() string {
	parsed, _ := url.Parse(s.config.PublicURL)
	return parsed.Host
}

func (s *Server) serveCached(w http.ResponseWriter, r *http.Request, entry relaycache.Entry) {
	file, err := os.Open(entry.Path)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "cache_read_failed")
		return
	}
	defer file.Close()
	for _, header := range entry.Metadata.Headers {
		if !strings.EqualFold(header[0], "Content-Length") && !strings.EqualFold(header[0], "Accept-Ranges") {
			w.Header().Add(header[0], header[1])
		}
	}
	w.Header().Set("X-Coworker-Relay-Cache", "hit")
	http.ServeContent(w, r, filepath.Base(entry.Path), entry.Metadata.CreatedAt, file)
}

func orderedHeaders(header http.Header) []protocol.Header {
	names := make([]string, 0, len(header))
	for name := range header {
		names = append(names, name)
	}
	sort.Strings(names)
	values := make([]protocol.Header, 0, len(header))
	for _, name := range names {
		for _, value := range header.Values(name) {
			values = append(values, protocol.Header{name, value})
		}
	}
	return values
}

func hopByHopHeader(name string) bool {
	switch strings.ToLower(name) {
	case "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade":
		return true
	default:
		return false
	}
}

func forwardedIP(ip string) string {
	if strings.Contains(ip, ":") {
		return `"` + "[" + ip + "]" + `"`
	}
	return ip
}

func newRequestID() string {
	raw := make([]byte, 16)
	_, _ = rand.Read(raw)
	return hex.EncodeToString(raw)
}

func (s *Server) clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	remote, err := netip.ParseAddr(strings.Trim(host, "[]"))
	if err != nil {
		return host
	}
	trusted := false
	for _, prefix := range s.config.TrustedProxies {
		if prefix.Contains(remote) {
			trusted = true
			break
		}
	}
	if trusted {
		if forwarded := strings.TrimSpace(strings.Split(r.Header.Get("X-Forwarded-For"), ",")[0]); forwarded != "" {
			if parsed, err := netip.ParseAddr(strings.Trim(forwarded, "[]")); err == nil {
				return parsed.String()
			}
		}
	}
	return remote.String()
}

func (s *Server) requireAdmin(w http.ResponseWriter, r *http.Request) bool {
	token, ok := auth.ParseBearer(r.Header.Get("Authorization"))
	if !ok || s.config.AdminToken == "" {
		writeError(w, http.StatusUnauthorized, "admin_auth_required")
		return false
	}
	left, right := sha256.Sum256([]byte(token)), sha256.Sum256([]byte(s.config.AdminToken))
	if subtle.ConstantTimeCompare(left[:], right[:]) != 1 {
		writeError(w, http.StatusForbidden, "admin_auth_invalid")
		return false
	}
	return true
}

func (s *Server) adminCreateInstance(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	var input struct {
		Name string `json:"name"`
	}
	if err := decodeJSON(r, &input, 8<<10); err != nil || len(input.Name) > 120 {
		writeError(w, http.StatusBadRequest, "invalid_instance")
		return
	}
	instance, pairingCode, err := s.store.CreateInstance(input.Name)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "store_failed")
		return
	}
	s.logger.Info("relay instance created", "instance_id", instance.ID)
	writeJSON(w, http.StatusCreated, map[string]any{
		"instance":     publicInstance(instance, s.isOnline(instance.ID)),
		"pairing_code": pairingCode,
		"expires_in":   600,
		"relay_url":    s.config.PublicURL,
	})
}

func (s *Server) adminListInstances(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	instances, err := s.store.ListInstances()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "store_failed")
		return
	}
	result := make([]map[string]any, 0, len(instances))
	for _, instance := range instances {
		result = append(result, publicInstance(instance, s.isOnline(instance.ID)))
	}
	writeJSON(w, http.StatusOK, map[string]any{"instances": result})
}

func (s *Server) adminDeleteInstance(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	id := r.PathValue("instance")
	if err := s.store.DeleteInstance(id); err != nil {
		writeError(w, http.StatusInternalServerError, "store_failed")
		return
	}
	s.tunnelsMu.RLock()
	current := s.tunnels[id]
	s.tunnelsMu.RUnlock()
	if current != nil {
		_ = current.conn.Close(websocket.StatusPolicyViolation, "instance revoked")
	}
	s.logger.Info("relay instance revoked", "instance_id", id)
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) adminUpdateAuth(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	var input struct {
		Mode string `json:"mode"`
	}
	if err := decodeJSON(r, &input, 8<<10); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_update_auth_mode")
		return
	}
	id := r.PathValue("instance")
	if err := s.store.SetUpdateAuthMode(id, input.Mode); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_update_auth_mode")
		return
	}
	s.logger.Info("relay update auth changed", "instance_id", id, "mode", input.Mode)
	writeJSON(w, http.StatusOK, map[string]string{"instance_id": id, "mode": input.Mode})
}

func (s *Server) adminUpdateStats(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	stats, err := s.store.UpdateStatistics(r.PathValue("instance"), time.Now().UTC())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "store_failed")
		return
	}
	writeJSON(w, http.StatusOK, stats)
}

func (s *Server) adminListBans(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	bans, err := s.store.ListBans(r.URL.Query().Get("instance"))
	if err != nil {
		writeError(w, http.StatusInternalServerError, "store_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"bans": bans})
}

func (s *Server) adminRemoveBan(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	instanceID, ip := r.URL.Query().Get("instance"), r.URL.Query().Get("ip")
	reason := strings.TrimSpace(r.URL.Query().Get("reason"))
	if instanceID == "" || ip == "" || reason == "" || len(reason) > 500 {
		writeError(w, http.StatusBadRequest, "instance_ip_reason_required")
		return
	}
	if err := s.store.RemoveBan(instanceID, ip); err != nil {
		writeError(w, http.StatusInternalServerError, "store_failed")
		return
	}
	s.logger.Info("relay ban removed", "instance_id", instanceID, "source_ip", ip, "reason", reason)
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) adminCacheStats(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	if s.cache == nil {
		writeJSON(w, http.StatusOK, map[string]int64{"entries": 0, "bytes": 0, "max_bytes": 0})
		return
	}
	stats, err := s.cache.Stats()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "cache_read_failed")
		return
	}
	writeJSON(w, http.StatusOK, stats)
}

func (s *Server) adminCachePurge(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	if s.cache != nil {
		if err := s.cache.Purge(); err != nil {
			writeError(w, http.StatusInternalServerError, "cache_purge_failed")
			return
		}
	}
	s.logger.Info("relay cache purged")
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) isOnline(id string) bool {
	s.tunnelsMu.RLock()
	defer s.tunnelsMu.RUnlock()
	return s.tunnels[id] != nil
}

func publicInstance(instance store.Instance, online bool) map[string]any {
	return map[string]any{
		"id":                  instance.ID,
		"name":                instance.Name,
		"online":              online,
		"enrolled":            instance.CredentialHash != "",
		"update_auth_mode":    instance.UpdateAuthMode,
		"created_at":          instance.CreatedAt,
		"last_connected_at":   instance.LastConnectedAt,
		"verifier_generation": instance.VerifierGeneration,
	}
}

func safeError(err error) string {
	if err == nil {
		return ""
	}
	return strings.Split(err.Error(), "\n")[0]
}

func ParseTrustedProxies(raw string) ([]netip.Prefix, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, nil
	}
	var result []netip.Prefix
	for _, value := range strings.Split(raw, ",") {
		prefix, err := netip.ParsePrefix(strings.TrimSpace(value))
		if err != nil {
			return nil, err
		}
		result = append(result, prefix)
	}
	return result, nil
}

func ValidatePublicURL(raw string) (string, error) {
	parsed, err := url.Parse(strings.TrimRight(raw, "/"))
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || parsed.Path != "" {
		return "", errors.New("RELAY_PUBLIC_URL must be an HTTPS origin without credentials, path, query, or fragment")
	}
	return parsed.String(), nil
}
