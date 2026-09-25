# roost

roost is a self-hosted cockpit for coding agents. It lets one developer drive
[Agent Client Protocol](https://agentclientprotocol.com) (ACP) sessions — Claude
Code, Codex, or any other ACP adapter — running on any of their machines, from a
single browser tab or a phone. It also includes an MCP gateway: authorize an
integration once, then choose which hosts and projects are allowed to use it.

roost never proxies agent licences. You bring your own subscriptions and log in
to each agent CLI on your own machines; roost only drives the sessions.

## Status

**Pre-alpha, design phase.** There is no usable code yet. The architecture is
being written down in [`docs/specs/`](docs/specs/) before implementation starts.
Expect everything to change.

## Licence

roost is licensed under the [GNU Affero General Public License v3.0](LICENSE).

