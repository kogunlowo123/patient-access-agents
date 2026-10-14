# ADR-0004: MCP in stateless JSON mode inside the API; A2A answered synchronously

- Status: Accepted
- Date: 2026-10-14

## Context

The service must expose its tools over MCP (official Python SDK) and accept
A2A `message/send`. It runs as several replicas behind a load balancer and
already authenticates every request with a bearer token. The installed SDK
is mcp 2.x, where FastMCP is now `MCPServer`.

## Decision

- Mount `MCPServer.streamable_http_app(stateless_http=True,
  json_response=True)` routes at `/mcp` in the FastAPI app and run its
  session manager in the app lifespan. Stateless mode needs no session
  affinity and no shared session store.
- Authenticate each tool call from the HTTP `Authorization` header with the
  same authenticator as REST; DNS-rebinding protection, intended for
  unauthenticated localhost servers, is disabled explicitly.
- A2A `message/send` runs the workflow synchronously and returns a `Task`
  whose state reflects the case (`completed`, `input-required`, `working`,
  `rejected`, `failed`). Streaming and push notifications are not offered;
  long waits are human approvals, which callers poll with `case-status`.

## Consequences

- MCP and A2A share the policy, audit and projections of REST.
- No server-initiated MCP messages (sampling, elicitation) are used.
- `tasks/get` and streaming can be added later behind the same card.
