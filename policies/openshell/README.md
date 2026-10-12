# OpenShell outer-cage deployment profile (F-034)

Defense-in-depth: the OpenShell sandbox wraps the whole Protocol-X Tool MCP
container with kernel-enforced policy — Landlock filesystem allowlist limited
to the workspace, a seccomp syscall filter, and an OPA egress policy allowing
only Nebius Token Factory + PyPI. Inside the cage, per-call rlimit workers
(F-003) remain the inner sandbox.

**The product is fully functional without OpenShell** (AC-F034-03): the
standard Docker hardened profile (`deploy/docker/`) + rlimits is the baseline.
This profile is strictly additive for hosts with OpenShell installed.

## Files

- `profile.yaml` — cage definition (Landlock, seccomp, OPA, privacy router)
- `egress.rego` — OPA egress policy (Token Factory + PyPI only)
- `smoke.sh` — smoke test: run the container under the cage and assert
  kernel-level write denial outside the workspace

## Usage

```bash
# 1. Build the standard hardened image
docker build -f deploy/docker/Dockerfile -t protocol-x-tool-mcp:latest .

# 2. Run under the OpenShell cage (requires openshell CLI on a Linux host)
openshell run --profile policies/openshell/profile.yaml \
  -- protocol-x-tool-mcp:latest

# 3. Smoke-test kernel enforcement
policies/openshell/smoke.sh
```

## Verification mapping

- AC-F034-01: `smoke.sh` attempts a write outside the workspace with the
  application's own workspace check bypassed; the kernel (Landlock) rejects it.
- AC-F034-02: `egress.rego` denies every destination except Token Factory and
  PyPI at the policy layer.
- AC-F034-03: hosts without OpenShell run the standard Docker profile; all
  v1.0 Must acceptance criteria pass unchanged.
