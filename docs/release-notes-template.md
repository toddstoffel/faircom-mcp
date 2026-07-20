# Release Notes Template

## FairCom MCP <version>
Date: <YYYY-MM-DD>

## Summary
- <one-line release summary>

## Added
- <new capability>

## Changed
- <behavioral change>

## Fixed
- <bug fix>

## Security
- <policy/default hardening updates>

## Packaging and Deployment
- Container image: `<image:tag>`
- Python artifacts: wheel and sdist
- Linux packages: DEB and RPM
- Service artifacts: systemd unit, env template, logrotate, sysusers/tmpfiles

## Compatibility
- Python: >=3.11
- FairCom JSON API: <validated versions>
- Linux distro matrix: see `docs/support-matrix.md`

## Upgrade Notes
1. Review env changes in `/etc/faircom-mcp/faircom-mcp.env`.
2. Restart service after upgrade.
3. Run health probe and one read-only tool smoke test.

## Known Issues
- <known issue>

## Validation
- Lint: pass
- Typecheck: pass
- Unit/integration tests: pass
- Edge integration tests: <result>
- Package build (DEB/RPM): pass
