"""Human authentication for the dashboard (distinct from machine API-key auth).

Dashboard operators log in with a username + password and receive a short-lived,
HMAC-signed session cookie; the gateway's dashboard/admin routes verify that
cookie and the principal's role. Machines (MCP consumers) never use any of this --
they authenticate with API keys at the edge.

Modules:
  passwords  -- scrypt password hashing/verification (stdlib; no build deps)
  session    -- issue/verify signed session tokens (HMAC-SHA256, with expiry)
  lockout    -- in-memory brute-force limiter for the login route
"""
