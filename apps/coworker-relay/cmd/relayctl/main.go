package main

import (
	"bytes"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type client struct {
	baseURL string
	token   string
	http    *http.Client
}

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	if os.Args[1] == "init" {
		initDeployment(os.Args[2:])
		return
	}
	loadLocalEnvironment()
	httpClient, err := newHTTPClient()
	if err != nil {
		fatal(err.Error())
	}
	c := client{
		baseURL: strings.TrimRight(os.Getenv("RELAY_URL"), "/"),
		token:   os.Getenv("RELAY_ADMIN_TOKEN"),
		http:    httpClient,
	}
	if c.baseURL == "" {
		fatal("RELAY_URL is required")
	}
	switch os.Args[1] {
	case "health":
		c.print("GET", "/_relay/v1/health", nil, false)
	case "instance":
		c.instance(os.Args[2:])
	case "bans":
		c.bans(os.Args[2:])
	case "cache":
		c.cache(os.Args[2:])
	default:
		usage()
	}
}

func newHTTPClient() (*http.Client, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if path := strings.TrimSpace(os.Getenv("RELAY_CA_CERT")); path != "" {
		certificate, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read RELAY_CA_CERT: %w", err)
		}
		roots, err := x509.SystemCertPool()
		if err != nil || roots == nil {
			roots = x509.NewCertPool()
		}
		if !roots.AppendCertsFromPEM(certificate) {
			return nil, errors.New("RELAY_CA_CERT contains no valid PEM certificates")
		}
		transport.TLSClientConfig = &tls.Config{
			MinVersion: tls.VersionTLS12,
			RootCAs:    roots,
		}
	}
	return &http.Client{Timeout: 20 * time.Second, Transport: transport}, nil
}

type initOptions struct {
	directory    string
	publicURL    string
	externalPort int
	acmeDomain   string
	tlsCert      string
	tlsKey       string
	image        string
	force        bool
}

func initDeployment(args []string) {
	flags := flag.NewFlagSet("init", flag.ExitOnError)
	options := initOptions{}
	flags.StringVar(&options.directory, "dir", "coworker-relay-deploy", "deployment directory")
	flags.StringVar(&options.publicURL, "public-url", "", "public HTTPS URL")
	flags.IntVar(&options.externalPort, "external-port", 8443, "published HTTPS port")
	flags.StringVar(&options.acmeDomain, "acme-domain", "", "ACME certificate domain")
	flags.StringVar(&options.tlsCert, "tls-cert", "", "host path to PEM certificate")
	flags.StringVar(&options.tlsKey, "tls-key", "", "host path to PEM private key")
	flags.StringVar(&options.image, "image", "coworker-relay:latest", "container image")
	flags.BoolVar(&options.force, "force", false, "replace generated files")
	_ = flags.Parse(args)
	adminToken, normalizedURL, err := initialize(options)
	if err != nil {
		fatal(err.Error())
	}
	absolute, _ := filepath.Abs(options.directory)
	fmt.Printf(
		"Relay deployment initialized in %s\nPublic URL: %s\nAdmin token: %s\n\nNext:\n  cd %s\n  docker compose up -d\n  relayctl health\n  relayctl instance create --name home-coworker\n",
		absolute,
		normalizedURL,
		adminToken,
		absolute,
	)
}

func initialize(options initOptions) (string, string, error) {
	if options.externalPort < 1 || options.externalPort > 65535 {
		return "", "", errors.New("--external-port must be between 1 and 65535")
	}
	parsed, normalizedURL, err := normalizePublicURL(options.publicURL, options.externalPort)
	if err != nil {
		return "", "", err
	}
	if options.image == "" || strings.ContainsAny(options.image, "\r\n") {
		return "", "", errors.New("--image must not be empty or contain newlines")
	}
	pemMode := options.tlsCert != "" || options.tlsKey != ""
	if pemMode && (options.tlsCert == "" || options.tlsKey == "") {
		return "", "", errors.New("--tls-cert and --tls-key must be provided together")
	}
	if pemMode && options.acmeDomain != "" {
		return "", "", errors.New("choose either ACME or PEM certificate mode")
	}
	if !pemMode {
		if options.acmeDomain == "" {
			options.acmeDomain = parsed.Hostname()
		}
		if net.ParseIP(options.acmeDomain) != nil {
			return "", "", errors.New("an IP address requires --tls-cert and --tls-key")
		}
	}
	directory, err := filepath.Abs(options.directory)
	if err != nil {
		return "", "", err
	}
	envPath := filepath.Join(directory, ".env")
	composePath := filepath.Join(directory, "compose.yaml")
	gitignorePath := filepath.Join(directory, ".gitignore")
	if !options.force {
		for _, path := range []string{envPath, composePath, gitignorePath} {
			if _, err := os.Stat(path); err == nil {
				return "", "", fmt.Errorf("%s already exists; use --force to replace generated files", path)
			} else if !errors.Is(err, os.ErrNotExist) {
				return "", "", err
			}
		}
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return "", "", err
	}
	adminToken, err := randomAdminToken()
	if err != nil {
		return "", "", err
	}
	env := strings.Join([]string{
		"RELAY_PUBLIC_URL=" + normalizedURL,
		"RELAY_URL=" + normalizedURL,
		"RELAY_ADMIN_TOKEN=" + adminToken,
		"RELAY_LISTEN=:8443",
		"RELAY_DATABASE=/var/lib/coworker-relay/relay.db",
		"RELAY_CACHE_DIR=/var/lib/coworker-relay/cache",
		"RELAY_CACHE_MAX_BYTES=4294967296",
		"RELAY_EXTERNAL_PORT=" + strconv.Itoa(options.externalPort),
		"RELAY_IMAGE=" + options.image,
	}, "\n") + "\n"
	composeVolumes := "      - relay-data:/var/lib/coworker-relay\n"
	ports := "      - \"${RELAY_EXTERNAL_PORT}:8443\"\n"
	if pemMode {
		cert, certErr := filepath.Abs(options.tlsCert)
		if certErr != nil {
			return "", "", certErr
		}
		key, keyErr := filepath.Abs(options.tlsKey)
		if keyErr != nil {
			return "", "", keyErr
		}
		env += "RELAY_TLS_CERT=/run/tls/fullchain.pem\nRELAY_TLS_KEY=/run/tls/privkey.pem\n"
		composeVolumes += "      - " + strconv.Quote(cert+":/run/tls/fullchain.pem:ro") + "\n"
		composeVolumes += "      - " + strconv.Quote(key+":/run/tls/privkey.pem:ro") + "\n"
	} else {
		env += "RELAY_ACME_DOMAIN=" + options.acmeDomain + "\n"
		env += "RELAY_ACME_HTTP_LISTEN=:8080\n"
		env += "RELAY_ACME_CACHE=/var/lib/coworker-relay/acme\n"
		ports += "      - \"80:8080\"\n"
	}
	compose := "services:\n" +
		"  relay:\n" +
		"    image: ${RELAY_IMAGE}\n" +
		"    restart: unless-stopped\n" +
		"    env_file:\n" +
		"      - .env\n" +
		"    ports:\n" + ports +
		"    volumes:\n" + composeVolumes +
		"    read_only: true\n" +
		"    tmpfs:\n" +
		"      - /tmp\n\n" +
		"volumes:\n" +
		"  relay-data:\n"
	flag := os.O_WRONLY | os.O_CREATE
	if options.force {
		flag |= os.O_TRUNC
	} else {
		flag |= os.O_EXCL
	}
	if err := writeFile(envPath, []byte(env), 0o600, flag); err != nil {
		return "", "", err
	}
	if err := writeFile(composePath, []byte(compose), 0o644, flag); err != nil {
		return "", "", err
	}
	if err := writeFile(gitignorePath, []byte(".env\n"), 0o644, flag); err != nil {
		return "", "", err
	}
	return adminToken, normalizedURL, nil
}

func normalizePublicURL(raw string, externalPort int) (*url.URL, string, error) {
	parsed, err := url.Parse(strings.TrimSpace(strings.TrimRight(raw, "/")))
	if err != nil ||
		parsed.Scheme != "https" ||
		parsed.Hostname() == "" ||
		parsed.User != nil ||
		parsed.Path != "" ||
		parsed.RawQuery != "" ||
		parsed.Fragment != "" {
		return nil, "", errors.New("--public-url must be an HTTPS origin")
	}
	if parsed.Port() == "" && externalPort != 443 {
		parsed.Host = net.JoinHostPort(parsed.Hostname(), strconv.Itoa(externalPort))
	} else if parsed.Port() != "" && parsed.Port() != strconv.Itoa(externalPort) {
		return nil, "", errors.New("--public-url port must match --external-port")
	}
	return parsed, parsed.String(), nil
}

func randomAdminToken() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

func writeFile(path string, body []byte, mode os.FileMode, flag int) error {
	file, err := os.OpenFile(path, flag, mode)
	if err != nil {
		return err
	}
	if err := file.Chmod(mode); err != nil {
		_ = file.Close()
		return err
	}
	if _, err := file.Write(body); err != nil {
		_ = file.Close()
		return err
	}
	return file.Close()
}

func loadLocalEnvironment() {
	path := os.Getenv("RELAY_CONFIG")
	if path == "" {
		path = ".env"
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return
	}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, value, ok := strings.Cut(line, "=")
		name = strings.TrimSpace(name)
		if ok && os.Getenv(name) == "" {
			_ = os.Setenv(name, value)
		}
	}
}

func (c client) cache(args []string) {
	if len(args) != 1 {
		usage()
	}
	switch args[0] {
	case "inspect":
		c.print("GET", "/_relay/v1/admin/cache", nil, true)
	case "purge":
		c.print("DELETE", "/_relay/v1/admin/cache", nil, true)
	default:
		usage()
	}
}

func (c client) instance(args []string) {
	if len(args) == 0 {
		usage()
	}
	switch args[0] {
	case "create":
		flags := flag.NewFlagSet("instance create", flag.ExitOnError)
		name := flags.String("name", "", "display name")
		_ = flags.Parse(args[1:])
		c.print("POST", "/_relay/v1/admin/instances", map[string]string{"name": *name}, true)
	case "list":
		c.print("GET", "/_relay/v1/admin/instances", nil, true)
	case "revoke":
		if len(args) != 2 {
			fatal("usage: relayctl instance revoke <instance_id>")
		}
		c.print("DELETE", "/_relay/v1/admin/instances/"+url.PathEscape(args[1]), nil, true)
	case "update-auth":
		if len(args) != 3 || (args[2] != "optional" && args[2] != "required") {
			fatal("usage: relayctl instance update-auth <instance_id> <optional|required>")
		}
		path := "/_relay/v1/admin/instances/" + url.PathEscape(args[1]) + "/update-auth"
		c.print("PATCH", path, map[string]string{"mode": args[2]}, true)
	case "update-stats":
		if len(args) != 2 {
			fatal("usage: relayctl instance update-stats <instance_id>")
		}
		path := "/_relay/v1/admin/instances/" + url.PathEscape(args[1]) + "/update-stats"
		c.print("GET", path, nil, true)
	default:
		usage()
	}
}

func (c client) bans(args []string) {
	if len(args) == 0 {
		usage()
	}
	switch args[0] {
	case "list":
		flags := flag.NewFlagSet("bans list", flag.ExitOnError)
		instance := flags.String("instance", "", "instance ID")
		_ = flags.Parse(args[1:])
		path := "/_relay/v1/admin/bans"
		if *instance != "" {
			path += "?instance=" + url.QueryEscape(*instance)
		}
		c.print("GET", path, nil, true)
	case "remove":
		flags := flag.NewFlagSet("bans remove", flag.ExitOnError)
		instance := flags.String("instance", "", "instance ID")
		ip := flags.String("ip", "", "source IP")
		reason := flags.String("reason", "", "audit reason")
		_ = flags.Parse(args[1:])
		if *instance == "" || *ip == "" || strings.TrimSpace(*reason) == "" {
			fatal("--instance, --ip and --reason are required")
		}
		query := url.Values{"instance": {*instance}, "ip": {*ip}, "reason": {*reason}}
		c.print("DELETE", "/_relay/v1/admin/bans?"+query.Encode(), nil, true)
	default:
		usage()
	}
}

func (c client) print(method, path string, body any, admin bool) {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			fatal(err.Error())
		}
		reader = bytes.NewReader(raw)
	}
	request, err := http.NewRequest(method, c.baseURL+path, reader)
	if err != nil {
		fatal(err.Error())
	}
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	if admin {
		if c.token == "" {
			fatal("RELAY_ADMIN_TOKEN is required")
		}
		request.Header.Set("Authorization", "Bearer "+c.token)
	}
	response, err := c.http.Do(request)
	if err != nil {
		fatal(err.Error())
	}
	defer response.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		fatal(fmt.Sprintf("%s: %s", response.Status, strings.TrimSpace(string(raw))))
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		fmt.Println("ok")
		return
	}
	var pretty bytes.Buffer
	if json.Indent(&pretty, raw, "", "  ") == nil {
		fmt.Println(pretty.String())
	} else {
		fmt.Println(string(raw))
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  relayctl init --public-url <https-origin> [--dir <directory>]
                [--external-port 8443]
                [--acme-domain <domain> | --tls-cert <path> --tls-key <path>]
  relayctl health
  relayctl instance create --name <name>
  relayctl instance list
  relayctl instance revoke <instance_id>
  relayctl instance update-auth <instance_id> <optional|required>
  relayctl instance update-stats <instance_id>
  relayctl bans list [--instance <instance_id>]
  relayctl bans remove --instance <instance_id> --ip <ip> --reason <reason>
  relayctl cache inspect
  relayctl cache purge

configuration:
  RELAY_URL=https://relay.example.com:8443
  RELAY_ADMIN_TOKEN=<shared administrator token>
  RELAY_CA_CERT=/path/to/private-ca.pem
  RELAY_CONFIG=.env  # optional; defaults to ./.env`)
	os.Exit(2)
}

func fatal(message string) {
	fmt.Fprintln(os.Stderr, "relayctl:", message)
	os.Exit(1)
}
