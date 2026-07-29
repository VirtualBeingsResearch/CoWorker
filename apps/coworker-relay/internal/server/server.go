package server

import (
	"context"
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/buildinfo"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/store"
	"github.com/coder/websocket"
)

const (
	protocolVersion = 1
	defaultMaxFrame = 256 << 10
	challengeTTL    = 30 * time.Second
)

type Config struct {
	PublicURL       string
	AdminToken      string
	TrustedProxies  []netip.Prefix
	AuthParallel    int
	ConnectionLimit int
	FrameLimit      int
	MaxFrame        int64
	RelayPrivateKey ed25519.PrivateKey
}

type controlPeer struct {
	instanceID   string
	connectionID string
	conn         *websocket.Conn
	sendMu       sync.Mutex
	heartbeat    sync.Once
	sessionsMu   sync.RWMutex
	sessions     map[sessionID]*desktopPeer
}

func (p *controlPeer) sendText(ctx context.Context, message wireMessage) error {
	p.sendMu.Lock()
	defer p.sendMu.Unlock()
	raw, err := json.Marshal(message)
	if err != nil {
		return err
	}
	return p.conn.Write(ctx, websocket.MessageText, raw)
}

func (p *controlPeer) sendData(ctx context.Context, sessionID sessionID, payload []byte) error {
	frame := make([]byte, 16+len(payload))
	copy(frame[:16], sessionID[:])
	copy(frame[16:], payload)
	p.sendMu.Lock()
	defer p.sendMu.Unlock()
	return p.conn.Write(ctx, websocket.MessageBinary, frame)
}

func (p *controlPeer) startHeartbeat(ctx context.Context) {
	p.heartbeat.Do(func() {
		go func() {
			ticker := time.NewTicker(20 * time.Second)
			defer ticker.Stop()
			for {
				if err := p.sendText(ctx, wireMessage{
					Type: "ping", SentAt: time.Now().UnixNano(),
				}); err != nil {
					return
				}
				select {
				case <-ctx.Done():
					return
				case <-ticker.C:
				}
			}
		}()
	})
}

type desktopPeer struct {
	sessionID sessionID
	conn      *websocket.Conn
	sendMu    sync.Mutex
	frames    atomic.Uint64
	bytes     atomic.Uint64
}

type sessionID [16]byte

func newSessionID() (sessionID, error) {
	var value sessionID
	_, err := rand.Read(value[:])
	return value, err
}

func (id sessionID) String() string { return hex.EncodeToString(id[:]) }

func (p *desktopPeer) send(ctx context.Context, payload []byte) error {
	p.sendMu.Lock()
	defer p.sendMu.Unlock()
	if err := p.conn.Write(ctx, websocket.MessageBinary, payload); err != nil {
		return err
	}
	p.frames.Add(1)
	p.bytes.Add(uint64(len(payload)))
	return nil
}

type wireMessage struct {
	Type              string `json:"type"`
	ProtocolVersion   int    `json:"protocol_version,omitempty"`
	InstanceID        string `json:"instance_id,omitempty"`
	ConnectionID      string `json:"connection_id,omitempty"`
	SessionID         string `json:"session_id,omitempty"`
	PairingID         string `json:"pairing_id,omitempty"`
	Nonce             string `json:"nonce,omitempty"`
	ExpiresAt         int64  `json:"expires_at,omitempty"`
	Epoch             uint64 `json:"epoch,omitempty"`
	PublicKey         string `json:"public_key,omitempty"`
	InstancePublicKey string `json:"instance_public_key,omitempty"`
	RelayPublicKey    string `json:"relay_public_key,omitempty"`
	Proof             string `json:"proof,omitempty"`
	Signature         string `json:"signature,omitempty"`
	SourceIP          string `json:"source_ip,omitempty"`
	PublicOrigin      string `json:"public_origin,omitempty"`
	PublicBaseURL     string `json:"public_base_url,omitempty"`
	Error             string `json:"error,omitempty"`
	SentAt            int64  `json:"sent_at,omitempty"`
}

type windowCounter struct {
	start time.Time
	count int
}

type Server struct {
	config           Config
	store            *store.Store
	logger           *slog.Logger
	relayPublicKey   ed25519.PublicKey
	controlsMu       sync.RWMutex
	controls         map[string]*controlPeer
	authSem          chan struct{}
	limitsMu         sync.Mutex
	connectionLimits map[string]windowCounter
	frameLimits      map[string]windowCounter
	connectionLimit  int
	frameLimit       int
	maxFrame         int64
	draining         atomic.Bool
	authFailures     atomic.Uint64
	banTotal         atomic.Uint64
	controlTotal     atomic.Uint64
	desktopTotal     atomic.Uint64
	bytesForwarded   atomic.Uint64
}

func New(config Config, database *store.Store, logger *slog.Logger) *Server {
	parallel := config.AuthParallel
	if parallel <= 0 {
		parallel = 32
	}
	connectionLimit := config.ConnectionLimit
	if connectionLimit <= 0 {
		connectionLimit = 60
	}
	frameLimit := config.FrameLimit
	if frameLimit <= 0 {
		frameLimit = 2400
	}
	maxFrame := config.MaxFrame
	if maxFrame <= 0 {
		maxFrame = defaultMaxFrame
	}
	publicKey := config.RelayPrivateKey.Public().(ed25519.PublicKey)
	return &Server{
		config: config, store: database, logger: logger,
		relayPublicKey:   append(ed25519.PublicKey(nil), publicKey...),
		controls:         make(map[string]*controlPeer),
		authSem:          make(chan struct{}, parallel),
		connectionLimits: make(map[string]windowCounter),
		frameLimits:      make(map[string]windowCounter),
		connectionLimit:  connectionLimit, frameLimit: frameLimit, maxFrame: maxFrame,
	}
}

func (s *Server) PublicHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("GET /_relay/v1/pair", s.pair)
	mux.HandleFunc("GET /_relay/v1/coworker", s.connectCoworker)
	mux.HandleFunc("GET /i/{instance}/_relay/v1/connect", s.connectDesktop)
	return s.securityHeaders(mux)
}

func (s *Server) AdminHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("POST /_relay/v1/admin/instances", s.adminCreateInstance)
	mux.HandleFunc("GET /_relay/v1/admin/instances", s.adminListInstances)
	mux.HandleFunc("DELETE /_relay/v1/admin/instances/{instance}", s.adminDeleteInstance)
	mux.HandleFunc("GET /_relay/v1/admin/bans", s.adminListBans)
	mux.HandleFunc("DELETE /_relay/v1/admin/bans", s.adminRemoveBan)
	mux.HandleFunc("GET /_relay/v1/admin/backup", s.adminBackup)
	mux.HandleFunc("POST /_relay/v1/admin/gc", s.adminGarbageCollect)
	mux.HandleFunc("GET /_relay/v1/admin/metrics", s.adminMetrics)
	return s.securityHeaders(mux)
}

// Handler is retained for focused tests. Production serves the public and
// loopback-only administrative handlers on different listeners.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.Handle("/", s.PublicHandler())
	mux.Handle("/_relay/v1/admin/", s.AdminHandler())
	return mux
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
	if s.draining.Load() {
		writeError(w, http.StatusServiceUnavailable, "draining")
		return
	}
	if err := s.store.Check(); err != nil {
		writeError(w, http.StatusServiceUnavailable, "database_unavailable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ready", "protocol_version": protocolVersion, "e2ee": true,
		"build": buildinfo.Values(),
	})
}

func randomBytes(size int) ([]byte, error) {
	raw := make([]byte, size)
	_, err := rand.Read(raw)
	return raw, err
}

func randomEncoded(size int) (string, error) {
	raw, err := randomBytes(size)
	if err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

func decodeKey(value string) (ed25519.PublicKey, error) {
	raw, err := base64.RawURLEncoding.DecodeString(value)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, errors.New("invalid Ed25519 public key")
	}
	return ed25519.PublicKey(raw), nil
}

func decodeSignature(value string) ([]byte, error) {
	raw, err := base64.RawURLEncoding.DecodeString(value)
	if err != nil || len(raw) != ed25519.SignatureSize {
		return nil, errors.New("invalid Ed25519 signature")
	}
	return raw, nil
}

func sign(privateKey ed25519.PrivateKey, payload string) string {
	return base64.RawURLEncoding.EncodeToString(ed25519.Sign(privateKey, []byte(payload)))
}

func verify(publicKey ed25519.PublicKey, payload, signature string) bool {
	raw, err := decodeSignature(signature)
	return err == nil && ed25519.Verify(publicKey, []byte(payload), raw)
}

func challengePayload(kind, instanceID, connectionID, nonce string, epoch uint64, expires int64) string {
	return strings.Join([]string{
		"coworker-relay-v1", kind, instanceID, connectionID, nonce,
		strconv.FormatUint(epoch, 10), strconv.FormatInt(expires, 10),
	}, "\n")
}

func keySyncPayload(instanceID, connectionID, publicKey string, epoch uint64) string {
	return strings.Join([]string{
		"coworker-relay-v1", "auth-key", instanceID, connectionID,
		strconv.FormatUint(epoch, 10), publicKey,
	}, "\n")
}

func sessionPayload(kind, instanceID, connectionID, sessionID, sourceIP, origin string) string {
	return strings.Join([]string{
		"coworker-relay-v1", kind, instanceID, connectionID,
		sessionID, sourceIP, origin,
	}, "\n")
}

func (s *Server) accept(w http.ResponseWriter, r *http.Request) (*websocket.Conn, error) {
	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
		CompressionMode: websocket.CompressionDisabled,
		OriginPatterns:  []string{"*"},
	})
	if err == nil {
		conn.SetReadLimit(s.maxFrame + 64<<10)
	}
	return conn, err
}

func readText(ctx context.Context, conn *websocket.Conn, target *wireMessage) error {
	kind, raw, err := conn.Read(ctx)
	if err != nil {
		return err
	}
	if kind != websocket.MessageText || len(raw) > 64<<10 {
		return errors.New("invalid control frame")
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	return decoder.Decode(target)
}

func writeText(ctx context.Context, conn *websocket.Conn, value wireMessage) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return conn.Write(ctx, websocket.MessageText, raw)
}

func (s *Server) pair(w http.ResponseWriter, r *http.Request) {
	ip := s.clientIP(r)
	if !s.allowWindow(s.connectionLimits, "pair:"+ip, s.connectionLimit) {
		writeError(w, http.StatusTooManyRequests, "rate_limited")
		return
	}
	conn, err := s.accept(w, r)
	if err != nil {
		return
	}
	defer conn.Close(websocket.StatusNormalClosure, "")
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	nonce, err := randomEncoded(32)
	if err != nil {
		return
	}
	if err := writeText(ctx, conn, wireMessage{
		Type: "pair_challenge", ProtocolVersion: protocolVersion, Nonce: nonce,
		RelayPublicKey: base64.RawURLEncoding.EncodeToString(s.relayPublicKey),
	}); err != nil {
		return
	}
	var proof wireMessage
	if err := readText(ctx, conn, &proof); err != nil ||
		proof.Type != "pair_proof" || proof.PairingID == "" {
		_ = writeText(ctx, conn, wireMessage{Type: "error", Error: "pairing_invalid"})
		return
	}
	pairing, err := s.store.GetPairing(proof.PairingID)
	if err != nil {
		_ = writeText(ctx, conn, wireMessage{Type: "error", Error: "pairing_rejected"})
		return
	}
	if _, err := decodeKey(proof.InstancePublicKey); err != nil {
		_ = writeText(ctx, conn, wireMessage{Type: "error", Error: "pairing_invalid"})
		return
	}
	mac := hmac.New(sha256.New, []byte(pairing.Secret))
	_, _ = mac.Write([]byte(strings.Join([]string{
		"coworker-relay-v1", "pair", proof.PairingID, nonce, proof.InstancePublicKey,
	}, "\n")))
	expected := mac.Sum(nil)
	actual, decodeErr := base64.RawURLEncoding.DecodeString(proof.Proof)
	if decodeErr != nil || !hmac.Equal(actual, expected) {
		_ = writeText(ctx, conn, wireMessage{Type: "error", Error: "pairing_rejected"})
		return
	}
	instance, err := s.store.CompletePairing(proof.PairingID, proof.InstancePublicKey)
	if err != nil {
		_ = writeText(ctx, conn, wireMessage{Type: "error", Error: "pairing_rejected"})
		return
	}
	payload := strings.Join([]string{
		"coworker-relay-v1", "pair-ok", instance.ID, nonce,
		proof.InstancePublicKey, base64.RawURLEncoding.EncodeToString(s.relayPublicKey),
	}, "\n")
	_ = writeText(ctx, conn, wireMessage{
		Type: "pair_ok", ProtocolVersion: protocolVersion, InstanceID: instance.ID,
		RelayPublicKey: base64.RawURLEncoding.EncodeToString(s.relayPublicKey),
		PublicBaseURL:  strings.TrimRight(s.config.PublicURL, "/") + "/i/" + instance.ID,
		Signature:      sign(s.config.RelayPrivateKey, payload),
	})
	s.logger.Info("relay instance paired", "instance_id", instance.ID, "source_ip", ip)
}

func (s *Server) connectCoworker(w http.ResponseWriter, r *http.Request) {
	instanceID := r.URL.Query().Get("instance_id")
	ip := s.clientIP(r)
	if !s.allowWindow(s.connectionLimits, "coworker:"+ip, s.connectionLimit) {
		writeError(w, http.StatusTooManyRequests, "rate_limited")
		return
	}
	instance, err := s.store.GetInstance(instanceID)
	if err != nil || instance.InstancePublicKey == "" {
		writeError(w, http.StatusNotFound, "not_found")
		return
	}
	publicKey, err := decodeKey(instance.InstancePublicKey)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "instance_invalid")
		return
	}
	conn, err := s.accept(w, r)
	if err != nil {
		return
	}
	defer conn.Close(websocket.StatusNormalClosure, "")
	connectionID, _ := randomEncoded(16)
	nonce, _ := randomEncoded(32)
	expires := time.Now().Add(challengeTTL).Unix()
	payload := challengePayload(
		"control", instanceID, connectionID, nonce, instance.AuthEpoch, expires,
	)
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	if err := writeText(ctx, conn, wireMessage{
		Type: "control_challenge", ProtocolVersion: protocolVersion,
		InstanceID: instanceID, ConnectionID: connectionID, Nonce: nonce,
		Epoch: instance.AuthEpoch, ExpiresAt: expires,
		RelayPublicKey: base64.RawURLEncoding.EncodeToString(s.relayPublicKey),
		Signature:      sign(s.config.RelayPrivateKey, payload),
	}); err != nil {
		return
	}
	var proof wireMessage
	if err := readText(ctx, conn, &proof); err != nil ||
		proof.Type != "control_proof" || proof.ConnectionID != connectionID ||
		!verify(publicKey, payload, proof.Signature) {
		s.authFailures.Add(1)
		_ = writeText(ctx, conn, wireMessage{Type: "error", Error: "instance_auth_invalid"})
		return
	}
	cancel()
	peer := &controlPeer{
		instanceID: instanceID, connectionID: connectionID,
		conn: conn, sessions: make(map[sessionID]*desktopPeer),
	}
	s.controlsMu.Lock()
	previous := s.controls[instanceID]
	s.controls[instanceID] = peer
	s.controlsMu.Unlock()
	if previous != nil {
		_ = previous.conn.Close(websocket.StatusPolicyViolation, "superseded")
	}
	_ = s.store.TouchInstance(instanceID)
	s.controlTotal.Add(1)
	_ = peer.sendText(r.Context(), wireMessage{
		Type: "control_ready", ProtocolVersion: protocolVersion,
		ConnectionID: connectionID, Epoch: instance.AuthEpoch,
	})
	s.logger.Info("relay coworker control connected", "instance_id", instanceID)
	err = s.readCoworker(r.Context(), peer, publicKey, connectionID)
	s.controlsMu.Lock()
	if s.controls[instanceID] == peer {
		delete(s.controls, instanceID)
	}
	s.controlsMu.Unlock()
	peer.sessionsMu.Lock()
	for _, desktop := range peer.sessions {
		_ = desktop.conn.Close(websocket.StatusServiceRestart, "coworker disconnected")
	}
	peer.sessions = make(map[sessionID]*desktopPeer)
	peer.sessionsMu.Unlock()
	s.logger.Info(
		"relay coworker control disconnected",
		"instance_id", instanceID, "error", safeError(err),
	)
}

func (s *Server) readCoworker(
	ctx context.Context,
	peer *controlPeer,
	instanceKey ed25519.PublicKey,
	connectionID string,
) error {
	for {
		kind, raw, err := peer.conn.Read(ctx)
		if err != nil {
			return err
		}
		switch kind {
		case websocket.MessageText:
			if len(raw) > 64<<10 {
				return errors.New("control frame too large")
			}
			var message wireMessage
			if err := json.Unmarshal(raw, &message); err != nil {
				return errors.New("invalid control frame")
			}
			switch message.Type {
			case "auth_key":
				if message.ConnectionID != connectionID ||
					!verify(
						instanceKey,
						keySyncPayload(
							peer.instanceID, connectionID, message.PublicKey, message.Epoch,
						),
						message.Signature,
					) {
					return errors.New("invalid authentication key update")
				}
				if _, err := decodeKey(message.PublicKey); err != nil {
					return err
				}
				if err := s.store.UpdateAuthKey(
					peer.instanceID, message.PublicKey, message.Epoch,
				); err != nil {
					instance, getErr := s.store.GetInstance(peer.instanceID)
					if getErr != nil || instance.AuthEpoch != message.Epoch ||
						instance.AuthPublicKey != message.PublicKey {
						return err
					}
				}
				if err := peer.sendText(ctx, wireMessage{
					Type: "auth_key_ack", Epoch: message.Epoch,
				}); err != nil {
					return err
				}
				peer.startHeartbeat(ctx)
			case "pong":
				continue
			default:
				return errors.New("unknown control frame")
			}
		case websocket.MessageBinary:
			if len(raw) < 17 || int64(len(raw)-16) > s.maxFrame {
				return errors.New("invalid data frame")
			}
			var sessionID sessionID
			copy(sessionID[:], raw[:16])
			peer.sessionsMu.RLock()
			desktop := peer.sessions[sessionID]
			peer.sessionsMu.RUnlock()
			if desktop != nil {
				if err := desktop.send(ctx, raw[16:]); err != nil {
					_ = desktop.conn.Close(websocket.StatusInternalError, "forward failed")
				}
				s.bytesForwarded.Add(uint64(len(raw) - 16))
			}
		default:
			return errors.New("unsupported websocket frame")
		}
	}
}

func (s *Server) connectDesktop(w http.ResponseWriter, r *http.Request) {
	instanceID := r.PathValue("instance")
	ip := s.clientIP(r)
	now := time.Now().UTC()
	if ban, active, _ := s.store.ActiveBan(instanceID, ip, now); active {
		w.Header().Set("Retry-After", strconv.Itoa(max(1, int(time.Until(ban.Until).Seconds()))))
		writeError(w, http.StatusTooManyRequests, "banned")
		return
	}
	if !s.allowWindow(s.connectionLimits, instanceID+"\x00"+ip, s.connectionLimit) {
		w.Header().Set("Retry-After", "60")
		writeError(w, http.StatusTooManyRequests, "rate_limited")
		return
	}
	instance, err := s.store.GetInstance(instanceID)
	if err != nil || instance.AuthPublicKey == "" {
		writeError(w, http.StatusNotFound, "not_found")
		return
	}
	s.controlsMu.RLock()
	control := s.controls[instanceID]
	s.controlsMu.RUnlock()
	if control == nil {
		writeError(w, http.StatusServiceUnavailable, "instance_offline")
		return
	}
	authKey, err := decodeKey(instance.AuthPublicKey)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "instance_invalid")
		return
	}
	conn, err := s.accept(w, r)
	if err != nil {
		return
	}
	defer conn.Close(websocket.StatusNormalClosure, "")
	connectionID, _ := randomEncoded(16)
	nonce, _ := randomEncoded(32)
	expires := time.Now().Add(challengeTTL).Unix()
	payload := challengePayload(
		"desktop", instanceID, connectionID, nonce, instance.AuthEpoch, expires,
	)
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	if err := writeText(ctx, conn, wireMessage{
		Type: "auth_challenge", ProtocolVersion: protocolVersion,
		InstanceID: instanceID, ConnectionID: connectionID, Nonce: nonce,
		Epoch: instance.AuthEpoch, ExpiresAt: expires,
	}); err != nil {
		return
	}
	var proof wireMessage
	if err := readText(ctx, conn, &proof); err != nil ||
		proof.Type != "auth_proof" || proof.ConnectionID != connectionID {
		s.recordFailure(instanceID, ip)
		return
	}
	select {
	case s.authSem <- struct{}{}:
		defer func() { <-s.authSem }()
	default:
		_ = writeText(ctx, conn, wireMessage{Type: "error", Error: "auth_busy"})
		return
	}
	if !verify(authKey, payload, proof.Signature) {
		s.recordFailure(instanceID, ip)
		_ = writeText(ctx, conn, wireMessage{Type: "error", Error: "auth_invalid"})
		return
	}
	cancel()
	sessionID, err := newSessionID()
	if err != nil {
		return
	}
	desktop := &desktopPeer{sessionID: sessionID, conn: conn}
	control.sessionsMu.Lock()
	control.sessions[sessionID] = desktop
	control.sessionsMu.Unlock()
	defer func() {
		control.sessionsMu.Lock()
		delete(control.sessions, sessionID)
		control.sessionsMu.Unlock()
		closePayload := sessionPayload(
			"session-close", instanceID, control.connectionID,
			sessionID.String(), ip, s.config.PublicURL,
		)
		closeContext, closeCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer closeCancel()
		_ = control.sendText(closeContext, wireMessage{
			Type: "session_close", InstanceID: instanceID,
			ConnectionID: control.connectionID, SessionID: sessionID.String(),
			SourceIP: ip, PublicOrigin: s.config.PublicURL,
			Signature: sign(s.config.RelayPrivateKey, closePayload),
		})
		if err := s.store.AddTraffic(
			instanceID, 1, desktop.frames.Load(), desktop.bytes.Load(),
		); err != nil {
			s.logger.Error("persist relay traffic aggregate", "error", err)
		}
	}()
	openPayload := sessionPayload(
		"session-open", instanceID, control.connectionID,
		sessionID.String(), ip, s.config.PublicURL,
	)
	if err := control.sendText(r.Context(), wireMessage{
		Type: "session_open", ProtocolVersion: protocolVersion,
		InstanceID: instanceID, ConnectionID: control.connectionID,
		SessionID: sessionID.String(), SourceIP: ip, PublicOrigin: s.config.PublicURL,
		Signature: sign(s.config.RelayPrivateKey, openPayload),
	}); err != nil {
		return
	}
	if err := writeText(r.Context(), conn, wireMessage{
		Type: "auth_ok", ProtocolVersion: protocolVersion,
		SessionID: sessionID.String(), Epoch: instance.AuthEpoch,
	}); err != nil {
		return
	}
	s.desktopTotal.Add(1)
	for {
		kind, raw, err := conn.Read(r.Context())
		if err != nil {
			return
		}
		if kind != websocket.MessageBinary || int64(len(raw)) > s.maxFrame {
			_ = conn.Close(websocket.StatusPolicyViolation, "invalid data frame")
			return
		}
		if !s.allowWindow(s.frameLimits, instanceID+"\x00"+ip, s.frameLimit) {
			_ = conn.Close(websocket.StatusPolicyViolation, "frame rate exceeded")
			return
		}
		if err := control.sendData(r.Context(), sessionID, raw); err != nil {
			return
		}
		desktop.frames.Add(1)
		desktop.bytes.Add(uint64(len(raw)))
		s.bytesForwarded.Add(uint64(len(raw)))
	}
}

func (s *Server) recordFailure(instanceID, ip string) {
	s.authFailures.Add(1)
	banned, until, err := s.store.RecordFailure(instanceID, ip, time.Now().UTC())
	if err != nil {
		s.logger.Error("record relay authentication failure", "error", err)
		return
	}
	if banned {
		s.banTotal.Add(1)
		s.logger.Warn(
			"relay source banned", "instance_id", instanceID,
			"source_ip", ip, "until", until,
		)
	}
}

func (s *Server) allowWindow(
	values map[string]windowCounter,
	key string,
	limit int,
) bool {
	s.limitsMu.Lock()
	defer s.limitsMu.Unlock()
	now := time.Now()
	value := values[key]
	if value.start.IsZero() || now.Sub(value.start) >= time.Minute {
		values[key] = windowCounter{start: now, count: 1}
		return true
	}
	if value.count >= limit {
		return false
	}
	value.count++
	values[key] = value
	if len(values) > 100_000 {
		for currentKey, current := range values {
			if now.Sub(current.start) >= time.Minute {
				delete(values, currentKey)
			}
		}
	}
	return true
}

func (s *Server) Drain() { s.draining.Store(true) }

func (s *Server) CloseTunnels() {
	s.controlsMu.Lock()
	defer s.controlsMu.Unlock()
	for _, peer := range s.controls {
		_ = peer.conn.Close(websocket.StatusGoingAway, "relay shutting down")
	}
	s.controls = make(map[string]*controlPeer)
}

func (s *Server) requireAdmin(w http.ResponseWriter, r *http.Request) bool {
	header := r.Header.Get("Authorization")
	if !strings.HasPrefix(header, "Bearer ") ||
		subtle.ConstantTimeCompare(
			[]byte(strings.TrimPrefix(header, "Bearer ")),
			[]byte(s.config.AdminToken),
		) != 1 {
		writeError(w, http.StatusUnauthorized, "admin_auth_invalid")
		return false
	}
	return true
}

func decodeJSON(r *http.Request, target any, limit int64) error {
	defer r.Body.Close()
	decoder := json.NewDecoder(io.LimitReader(r.Body, limit))
	decoder.DisallowUnknownFields()
	return decoder.Decode(target)
}

func (s *Server) adminCreateInstance(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	var input struct {
		Name string `json:"name"`
	}
	if err := decodeJSON(r, &input, 8<<10); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request")
		return
	}
	instance, pairingCode, err := s.store.CreateInstance(input.Name)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "store_failed")
		return
	}
	s.logger.Info("relay instance created", "instance_id", instance.ID, "name", instance.Name)
	_ = s.store.RecordAudit(store.AuditEvent{
		Action: "instance.create", InstanceID: instance.ID,
		Reason: strings.TrimSpace(input.Name),
	})
	writeJSON(w, http.StatusCreated, map[string]any{
		"instance": instance, "pairing_code": pairingCode,
		"pairing_expires_in_seconds": 600, "relay_url": s.config.PublicURL,
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
	type item struct {
		store.Instance
		Online bool `json:"online"`
	}
	result := make([]item, 0, len(instances))
	for _, instance := range instances {
		result = append(result, item{Instance: instance, Online: s.isOnline(instance.ID)})
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
	s.controlsMu.Lock()
	if peer := s.controls[id]; peer != nil {
		_ = peer.conn.Close(websocket.StatusPolicyViolation, "instance revoked")
		delete(s.controls, id)
	}
	s.controlsMu.Unlock()
	s.logger.Warn("relay instance revoked", "instance_id", id)
	_ = s.store.RecordAudit(store.AuditEvent{
		Action: "instance.revoke", InstanceID: id,
	})
	writeJSON(w, http.StatusOK, map[string]bool{"deleted": true})
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
	instanceID := r.URL.Query().Get("instance")
	ip := r.URL.Query().Get("ip")
	reason := strings.TrimSpace(r.URL.Query().Get("reason"))
	if instanceID == "" || net.ParseIP(ip) == nil || reason == "" {
		writeError(w, http.StatusBadRequest, "instance_ip_and_reason_required")
		return
	}
	if err := s.store.RemoveBanWithAudit(instanceID, ip, reason); err != nil {
		writeError(w, http.StatusInternalServerError, "store_failed")
		return
	}
	s.logger.Warn(
		"relay ban removed", "instance_id", instanceID,
		"source_ip", ip, "reason", reason,
	)
	writeJSON(w, http.StatusOK, map[string]bool{"removed": true})
}

func (s *Server) adminBackup(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Disposition", "attachment; filename=coworker-relay.db")
	if err := s.store.Backup(w); err != nil {
		s.logger.Error("relay backup failed", "error", err)
	}
}

func (s *Server) adminGarbageCollect(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	removed, err := s.store.GarbageCollect(time.Now().UTC())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "store_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"removed": removed})
}

func (s *Server) adminMetrics(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	s.controlsMu.RLock()
	online := len(s.controls)
	s.controlsMu.RUnlock()
	persisted, _ := s.store.TrafficTotals()
	writeJSON(w, http.StatusOK, map[string]any{
		"online_instances":      online,
		"auth_failures":         s.authFailures.Load(),
		"bans":                  s.banTotal.Load(),
		"coworker_connections":  s.controlTotal.Load(),
		"desktop_connections":   s.desktopTotal.Load(),
		"bytes_forwarded":       s.bytesForwarded.Load(),
		"persisted_connections": persisted.Connections,
		"persisted_frames":      persisted.Frames,
		"persisted_bytes":       persisted.Bytes,
	})
}

func (s *Server) isOnline(id string) bool {
	s.controlsMu.RLock()
	defer s.controlsMu.RUnlock()
	return s.controls[id] != nil
}

func (s *Server) clientIP(r *http.Request) string {
	directHost, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		directHost = r.RemoteAddr
	}
	direct, err := netip.ParseAddr(strings.Trim(directHost, "[]"))
	if err != nil {
		return directHost
	}
	direct = direct.Unmap()
	if !s.isTrustedProxy(direct) {
		return direct.String()
	}
	chain := make([]netip.Addr, 0, 8)
	for _, value := range strings.Split(r.Header.Get("X-Forwarded-For"), ",") {
		if candidate, err := netip.ParseAddr(strings.TrimSpace(value)); err == nil {
			chain = append(chain, candidate.Unmap())
		}
	}
	for index := len(chain) - 1; index >= 0; index-- {
		if !s.isTrustedProxy(chain[index]) {
			return chain[index].String()
		}
	}
	if len(chain) > 0 {
		return chain[0].String()
	}
	return direct.String()
}

func (s *Server) isTrustedProxy(address netip.Addr) bool {
	for _, prefix := range s.config.TrustedProxies {
		if prefix.Contains(address) {
			return true
		}
	}
	return false
}

func safeError(err error) string {
	if err == nil {
		return ""
	}
	value := err.Error()
	if len(value) > 300 {
		return value[:300]
	}
	return value
}

func ParseTrustedProxies(raw string) ([]netip.Prefix, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, nil
	}
	var result []netip.Prefix
	for _, item := range strings.Split(raw, ",") {
		prefix, err := netip.ParsePrefix(strings.TrimSpace(item))
		if err != nil {
			return nil, err
		}
		result = append(result, prefix)
	}
	return result, nil
}

func ValidatePublicURL(raw string) (string, error) {
	value := strings.TrimRight(strings.TrimSpace(raw), "/")
	parsed, err := url.Parse(value)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") ||
		parsed.Hostname() == "" || parsed.User != nil ||
		(parsed.Path != "" && parsed.Path != "/") ||
		parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", errors.New("RELAY_PUBLIC_URL must be an absolute HTTP(S) origin")
	}
	return value, nil
}

func LoadOrCreateSigningKey(path string) (ed25519.PrivateKey, error) {
	if raw, err := os.ReadFile(path); err == nil {
		if err := os.Chmod(path, 0o600); err != nil {
			return nil, fmt.Errorf("secure Relay signing key permissions: %w", err)
		}
		decoded, err := base64.RawURLEncoding.DecodeString(strings.TrimSpace(string(raw)))
		if err != nil || len(decoded) != ed25519.PrivateKeySize {
			return nil, errors.New("invalid Relay signing key file")
		}
		return ed25519.PrivateKey(decoded), nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	encoded := base64.RawURLEncoding.EncodeToString(privateKey) + "\n"
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return nil, err
	}
	if _, err := io.WriteString(file, encoded); err != nil {
		_ = file.Close()
		return nil, err
	}
	if err := file.Close(); err != nil {
		return nil, err
	}
	return privateKey, nil
}
