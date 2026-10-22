# Friction log

Entries use the fields the official rules ask for: the task attempted, the steps taken, expected and actual result, a severity
rating, the workaround used, and an actionable suggestion. They cover friction with the platform and the tools I built on (the
Alexa+ track's requirements, MCP, MCP Apps, the MCP Python SDK 2.2.0). My own bugs and how I found them are in
[ENGINEERING_LOG.md](ENGINEERING_LOG.md), so they don't dilute this list.

Scope: the official FAQ says the Alexa+ developer tools "are in preview and available to select partners only" and that "there's
no way for any participant to connect a self-hosted MCP server to a real Alexa+ device or developer console". Nothing below is
therefore a measured finding about the Alexa+ host. Entries 5 to 7 are questions I could not answer without one.

Severity: **High** = blocked or would ship a wrong result; **Medium** = cost hours or needed a design change; **Low** = minutes.

---

### 1. Tools registered after the server object is built are silently missing
- **Task:** expose Leak Hunt's tools (13 at the time, 14 now), each tied to one MCP Apps screen, from the MCP Python SDK 2.2.0.
- **Steps:** created `Apps()`, constructed `MCPServer(..., extensions=[apps])`, then registered more tools with `@apps.tool(...)`;
  listed tools over Streamable HTTP.
- **Expected:** every registered tool in `tools/list`, or an error at registration.
- **Actual:** the tools registered after construction were absent from `tools/list`, with no warning or error.
- **Severity:** Medium (a tool the model never sees fails silently in production).
- **Workaround:** register every Apps tool first and construct the server last (`addon/server.py`); a test lists the tools over
  HTTP and expects the full set (`tests/test_server_http.py`); the dev host's top bar shows how many tools `tools/list` returned (14).
- **Suggestion:** raise on registration after the extension has been bound, or make the extension's tool list live.

### 2. An empty PKCE `code_challenge` is accepted at `/authorize`
- **Task:** account linking with OAuth 2.1 and PKCE, using the SDK's authorization handlers.
- **Steps:** sent `/authorize` requests with `code_challenge` empty and with 5 characters (`code_challenge_method=S256`).
- **Expected:** `invalid_request` at `/authorize`: an S256 challenge is the 43-character base64url SHA-256 of the verifier
  (RFC 7636, sections 4.2 and 4.3).
- **Actual:** both were accepted. In the SDK source (2.2.0, and upstream `main` as of 17 Oct 2026) the request model declares
  `code_challenge: str` with no length or character check. The token endpoint later fails the exchange, so this is a late,
  confusing error after the person has already consented, not a way around PKCE.
- **Severity:** Low (correctness and error quality; no bypass found).
- **Workaround:** validate in my own provider (43 to 128 characters from the unreserved set) and test that it returns
  `invalid_request` (`addon/oauth.py`, `tests/test_oauth_http.py`).
- **Suggestion:** validate the challenge's length and alphabet in the SDK's request model. (The SDK asks for an issue before any
  outside pull request; I have not filed one.)

### 3. A screen cannot reach its own server, so long tasks have to be polled through the host
- **Task:** keep a screen live for the 2 to 3 minutes of an automatic leak hunt or a bath fill (litres so far, the controller's
  timer, Stop).
- **Steps:** served the screen as an MCP Apps resource with the default content security policy; tried `fetch` to my server.
- **Expected:** a documented way for a screen to follow a long-running task.
- **Actual:** the default `connect-src 'none'` blocks the screen's own `fetch`. The only route is `tools/call` through the host
  over `postMessage`.
- **Severity:** Medium (a design change; whether the Alexa+ host allows a screen to poll for minutes is unknown, see 6).
- **Workaround:** the screen polls with `tools/call` every 1.5 s; the polling tools are visible to `["model","app"]`; it also
  handles `ui/notifications/tool-result`.
- **Suggestion:** document the recommended pattern and limits for long-running tasks in Alexa+ MCP Apps (polling interval and
  duration, or server-initiated updates).

### 4. No Alexa+ surface for participants, so the host's behaviour can only be emulated
- **Task:** watch Leak Hunt's screen and spoken flow on an Alexa+ device or developer console.
- **Steps:** read the hackathon resources and FAQ for any participant access to an Alexa+ test surface.
- **Expected:** a limited simulator or console that loads a self-hosted MCP server.
- **Actual:** none is available to participants (FAQ, quoted above).
- **Severity:** High (every host-dependent behaviour in 5 to 7 is untested).
- **Workaround:** a dev host emulator written from the MCP Apps protocol text (`addon/ui/devhost.html`). It is a real MCP
  client over Streamable HTTP (initialize, tools/list, resources/read, tools/call, shown in its top bar), renders the screen
  in a sandboxed iframe and speaks the `ui/*` messages. It is labelled "Not Alexa+" on every page.
- **Suggestion:** a participant-accessible test console, or a published host capability list plus a conformance server that a
  participant's MCP server can be run against.

### 5. Open: account-linking redirect URIs and the `resource` parameter
- **Task:** configure OAuth for the Alexa+ host.
- **Steps:** looked for the host's redirect URIs and whether it sends the RFC 8707 `resource` parameter.
- **Expected:** both documented for the track.
- **Actual:** not found in the hackathon materials I could read.
- **Severity:** Medium (a wrong guess breaks linking on day one).
- **Workaround:** allowed redirect URIs come from configuration; `resource` is optional and any value other than the server's own
  is rejected. Untested against a real host.
- **Suggestion:** publish the redirect URIs and the exact authorization request the host sends.

### 6. Open: spoken tool calls into an open screen, and how long a screen may poll
- **Task:** "Alexa, turn off kitchen pipe 2" while the Leak Hunt screen is open.
- **Steps:** read the MCP Apps protocol text for how a host forwards a model-initiated tool result to a screen.
- **Expected:** a statement of what the Alexa+ host forwards and how long it keeps a screen alive.
- **Actual:** unknown without a host.
- **Severity:** Medium.
- **Workaround:** the screen handles `ui/notifications/tool-result` and also re-reads state by polling, so either path updates it.
- **Suggestion:** document both behaviours for Alexa+.

### 7. Open: display modes and theme notifications
- **Task:** lay out the screen for the sizes and themes the host uses.
- **Steps:** built for inline and fullscreen, light and dark, from 360 to 1280 px wide; audited them in the emulator.
- **Expected:** the list of display modes, sizes and theme notifications Alexa+ devices send.
- **Actual:** unknown; the emulator follows the protocol text.
- **Severity:** Low.
- **Workaround:** a UI audit at four sizes (360x640, 600x260 inline, 768x480, 1280x800) in both themes, with contrast checks
  (`tools/ui_check.py`).
- **Suggestion:** publish the host context values (display modes, container sizes, platform) per Alexa+ device class.

### 8. The video rule and the FAQ point different ways for the Alexa+ track
- **Task:** plan a demo video that meets the rules for an Alexa+ entry.
- **Steps:** read the official rules and the FAQ on 21 Oct 2026.
- **Expected:** one description of what the video must show.
- **Actual:** the rules say the video "should include footage that shows the Project functioning on the device for which it was
  built"; the FAQ says "there's no way for any participant to connect a self-hosted MCP server to a real Alexa+ device or developer
  console" and describes a web page that "acts as an actual MCP client" as the route.
- **Severity:** Medium (a participant could fear disqualification for following the FAQ).
- **Workaround:** followed the FAQ: the video shows the dev host, a real MCP client over Streamable HTTP, with "Simulated house"
  and "Not Alexa+" on screen throughout.
- **Suggestion:** add one line to the rules for the Alexa+ track saying a labelled simulated host satisfies the video requirement.
