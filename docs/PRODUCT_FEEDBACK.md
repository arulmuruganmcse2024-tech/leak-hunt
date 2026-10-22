# Product feedback

The rules ask, for each tool, API or SDK used: which, and for what; what worked; what needs work; how onboarding went; and
whether I would build with them again. This page answers those five questions. It covers only what I actually used.

**Scope note.** The official FAQ says participants cannot get the gated Alexa+ developer tools (category SDK, MCP toolkit, CLI,
web simulator) and that a self-hosted MCP server demonstrated with your own front end is the intended route. So nothing here is a
finding about the Alexa+ host: the screen was tested against an emulator I wrote from the MCP Apps protocol text, and the
questions I could not answer are in [FRICTION_LOG.md](FRICTION_LOG.md) as open items.

## 1. What I used, and for what

| Tool / API / SDK | Used for |
| --- | --- |
| MCP Python SDK 2.2.0 | The self-hosted server: tool definitions, Streamable HTTP transport, the OAuth provider protocol and `AuthSettings`, custom routes |
| MCP specification 2025-11-25 (Streamable HTTP) | The wire protocol; the tests speak it directly (initialize, tools/list, tools/call) |
| MCP Apps extension (`ui://` resources, JSON-RPC over `postMessage`) | The on-screen console: next step, valve map, live flow trace, Stop |
| OAuth 2.1 with PKCE (S256), RFC 7636 | Account linking, implemented server-side against the SDK's provider protocol |
| Playwright (Chromium) | An automated audit of the screen: touch-only completion, target sizes, contrast, light and dark, spoken/displayed parity |
| Claude Code (AI coding assistant) | Drafting code, tests and documents; every figure in `docs/` comes from a command in [VERIFICATION.md](VERIFICATION.md) |

## 2. What worked well

* **Declaring a UI resource and tool visibility is concise.** `@apps.tool` with a `resource_uri`, and `visibility=["model","app"]`
  for the tools the screen polls, took a few lines and behaved as described.
* **`CallToolResult` passes `structuredContent` and `_meta` straight through**, so one result can be spoken (`content`), drawn
  (`structuredContent`) and linked to its screen (`_meta.ui.resourceUri`) without a second channel.
* **The OAuth provider protocol is clear.** Implementing it and wiring `AuthSettings` was small and testable over real HTTP.
* **Streamable HTTP is easy to test with plain `urllib`**, which let the whole server be exercised in the suite without a host.
* **`custom_route`** made an evidence page and a health check trivial to add next to `/mcp`.

## 3. What needs work

| Area | What cost time | Suggestion |
| --- | --- | --- |
| `Apps` / `@apps.tool` | Tools declared after `MCPServer(...)` is built silently do not appear in `tools/list` | Raise an error, or document the ordering next to `extensions=[apps]` |
| Initialize and `extensions` | The `extensions` capability is advertised only on the newer wire version, so a 2025-11-25 client never sees it | State which wire versions carry it |
| OAuth `/authorize` | An empty or short `code_challenge` was accepted | Validate RFC 7636 length and alphabet by default |
| MCP Apps CSP | `connect-src 'none'` by default means a screen cannot `fetch` its own server, so it polls through `tools/call` | Document the polling pattern, or offer a server-to-screen push so a long task does not need a polling loop |
| `custom_route` | Content negotiation on `Accept` is easy to get wrong for a page that browsers and clients both open | An example of a route that serves HTML and JSON |
| Tools that move something physical | The rule "pass `consent=true` only after the customer said yes" lives in server instructions, which a model can still get wrong; the annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) cannot say "closes a valve" | A host-attested confirmation the tool can check, and an annotation for physical effects |
| Interrupting a long task | "Stop" must reach a tool while another call may be running; I made `stop_leak_hunt` idempotent and told the model to call it before asking anything | Treat voice interruption as a first-class host feature |

None of this relied on the host. Control exists only if the operator configures a pipe hub, every close during a hunt is a lease
the hub enforces, and the add-on never addresses the main safety valve ([PIPE_HUB.md](PIPE_HUB.md)).

## 4. Onboarding: zero to hello world

Installing the SDK was a single `pip install "mcp==2.2.0"`. The first working tool was quick; the first working *screen* took
longer because of the registration-order and capability behaviour above. The npm registry was not reachable from my build
environment, so the TypeScript SDK was not an option and I did not compare the two. `python3 -I -m addon.server` fails because `-I`
drops the working directory from `sys.path`; I added a small `serve.py`. Without access to the Alexa+ toolkit I could not measure
the path from hello-world to a real device, so I make no claim about it.

## 5. Would I build with these again?

**Yes**, with the caveats in section 3. The MCP server side is well-shaped for a tool that has to be testable without its host.
What I could not judge is the host side, which this competition does not let participants reach.
