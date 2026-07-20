# Project Structure

- `src/faircom_mcp/transports`: transport adapters (HTTP/SSE/STDIO)
- `src/faircom_mcp/api`: FairCom JSON API client and adapter layer
- `src/faircom_mcp/tools`: tool handlers and schemas
- `src/faircom_mcp/security`: auth, policy, and write controls
- `tests/unit`: fast deterministic tests
- `tests/integration`: end-to-end and transport tests
- `packaging/systemd`: service unit and environment templates
- `packaging/logrotate`: log rotation policy
- `packaging/sysusers.d`: Linux service user/group definitions
- `packaging/tmpfiles.d`: runtime/state directory lifecycle definitions
- `packaging/rpm`: RPM spec and packaging notes
- `packaging/deb`: DEB metadata and maintainer scripts
