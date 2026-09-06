# Security and demo boundaries

RelayOps ships local synthetic services and intentionally public demo credentials. Compose publishes services only on `127.0.0.1`. Do not expose the demo LDAP, monitoring controls, or mock application APIs to the internet.

Keep secrets in environment variables or ignored local configuration. Never commit `.env`, `config.yaml`, database files, logs, or exported documents. Replace the signing key and demo identity setup before any nonlocal use. Model and SMTP calls use the destinations you explicitly configure.

Before committing, run `python scripts/check_public_tree.py` and review the staged diff. The lightweight check is an additional guard, not a guarantee that all confidential data or secrets are detected. Review source, fixtures, documentation, images, and generated bundles together.

If you find sensitive content, do not reproduce it in a public issue. Use GitHub private vulnerability reporting if enabled. If credentials were exposed, revoke them before attempting repository-history cleanup.
