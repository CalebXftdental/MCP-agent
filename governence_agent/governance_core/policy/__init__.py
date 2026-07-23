"""Policy Decision Point (PDP) for the Governance Gateway.

Pure, deterministic, no I/O beyond the facts passed in (the PIP). Given a
requester identity, a tool, its args, and the resolved session scope, decide()
returns an allow/deny verdict plus a redaction plan. The gateway (PEP) enforces
the verdict and applies the plan; nothing here talks to a backend or a network.

Modules:
  manifest      -- per-tool metadata: backend, scope requirement, field classifications
  entitlements  -- per-consumer allowed data classifications
  decision      -- decide(): the PDP entrypoint
  redaction     -- apply a redaction plan to a structured result
"""
