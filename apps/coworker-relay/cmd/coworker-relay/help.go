package main

import (
	"fmt"
	"io"
	"strings"
)

var helpTopics = map[string]string{
	"": `Run and administer the self-hosted Coworker Relay

Usage:
  coworker-relay [--config <path>] <command> [options]
  coworker-relay help [command [subcommand]]
  coworker-relay <command> [subcommand] --help

Commands:
  serve       Start the public WebSocket and local administration services
  init        Create a Compose deployment (interactive with no options)
  health      Check the local Relay administration endpoint
  version     Print build information
  instance    Create, list, and revoke Coworker instances
  bans        Inspect and remove source-IP bans
  backup      Download a consistent database backup
  restore     Restore a backup while Relay is stopped
  gc          Run database garbage collection
  metrics     Show opaque-transport operational metrics
  help        Show command help

Configuration:
  RELAY_PUBLIC_URL=http://relay.example.com:8443
  RELAY_ADMIN_URL=http://127.0.0.1:8444
  RELAY_ADMIN_TOKEN=<local administrator token>
  RELAY_CONFIG=.env  # optional; defaults to ./.env

Global options:
  --config <path>    Read Relay configuration from an explicit file
`,
	"serve": `Start Coworker Relay

Usage:
  coworker-relay serve

The public listener defaults to :8443 and carries WebSocket traffic whose
contents are end-to-end encrypted between Desktop and Coworker. The
administration listener defaults to 127.0.0.1:8444 and must not be published.
`,
	"init": `Initialize a self-hosted Coworker Relay deployment

Usage:
  coworker-relay init
  coworker-relay init --public-url <http-or-https-origin> [options]

Options:
  --dir <directory>       Deployment directory (default: coworker-relay-deploy)
  --public-url <origin>   Example: http://203.0.113.10:8443
  --external-port <port>  Published WebSocket port (default: 8443)
  --admin-port <port>     Host-loopback admin port (default: 8444)
  --image <reference>     Relay container image
  --force                 Replace previously generated files

No certificate or port 80 is required. Operators may independently place a
trusted HTTPS/WSS reverse proxy in front of the public listener.
`,
	"health": `Check Relay availability

Usage:
  coworker-relay health

Reads RELAY_ADMIN_URL from the current directory's .env file and contacts only
the loopback administration endpoint.
`,
	"version": `Print coworker-relay version information

Usage:
  coworker-relay version
  coworker-relay --version
`,
	"instance": `Create and manage Coworker instances

Usage:
  coworker-relay instance create [--name <name>]
  coworker-relay instance list
  coworker-relay instance revoke <instance_id>

Create returns a 10-minute, single-use, high-entropy pairing code.
`,
	"instance create": `Create a Coworker instance

Usage:
  coworker-relay instance create [--name <name>]
`,
	"instance list": `List Coworker instances

Usage:
  coworker-relay instance list
`,
	"instance revoke": `Permanently revoke a Coworker instance

Usage:
  coworker-relay instance revoke <instance_id>
`,
	"bans": `Inspect or remove authentication bans

Usage:
  coworker-relay bans list [--instance <instance_id>]
  coworker-relay bans remove --instance <instance_id> --ip <ip> --reason <reason>
`,
	"bans list": `List active authentication bans

Usage:
  coworker-relay bans list [--instance <instance_id>]
`,
	"bans remove": `Remove an authentication ban with an audit reason

Usage:
  coworker-relay bans remove --instance <instance_id> --ip <ip> --reason <reason>
`,
	"backup": `Download a consistent Relay database backup

Usage:
  coworker-relay backup [--output <path>]
`,
	"restore": `Restore a Relay database while Relay is stopped

Usage:
  coworker-relay restore --from <backup.db> --database <relay.db> [--force]

Databases with a non-E2EE-v1 schema require backup and reinitialization.
`,
	"gc": `Run Relay database garbage collection

Usage:
  coworker-relay gc
`,
	"metrics": `Show Relay metadata-only operational metrics

Usage:
  coworker-relay metrics
`,
	"help": `Show coworker-relay help

Usage:
  coworker-relay help [command [subcommand]]
`,
}

var helpSubcommands = map[string]map[string]struct{}{
	"instance": {"create": {}, "list": {}, "revoke": {}},
	"bans":     {"list": {}, "remove": {}},
}

func handleHelp(args []string, output io.Writer) (bool, error) {
	if len(args) == 0 {
		fmt.Fprint(output, helpTopics[""])
		return true, nil
	}
	if args[0] == "help" {
		topic := strings.Join(args[1:], " ")
		text, ok := helpTopics[topic]
		if !ok {
			return true, fmt.Errorf("unknown help topic %q", topic)
		}
		fmt.Fprint(output, text)
		return true, nil
	}
	for index, arg := range args {
		if arg != "-h" && arg != "--help" {
			continue
		}
		topicParts := args[:index]
		if len(topicParts) > 2 {
			topicParts = topicParts[:2]
		}
		topic := strings.Join(topicParts, " ")
		text, ok := helpTopics[topic]
		if !ok {
			return true, fmt.Errorf("unknown help topic %q", topic)
		}
		fmt.Fprint(output, text)
		return true, nil
	}
	return false, nil
}
