# Direct Android update site

The FastAPI application serves the installer page at `/app/`, a validated
manifest at `/app/update.json`, and only versioned APK names under
`/app/downloads/`. The page keeps its download button hidden until a valid
manifest exists. There is no placeholder APK and no GitHub download link.

Set `BYMEVPN_UPDATE_ROOT` to the service-side directory containing
`update.json` and `downloads/`. For container deployments, mount only the
SFTP publication directory read-only into the API container, for example:

```yaml
volumes:
  - /srv/bymevpn-updates/app:/app/update_files:ro
environment:
  BYMEVPN_UPDATE_ROOT: /app/update_files
```

The first-install page and logo are packaged with the backend. The public
files are read-only to the API process. The manifest is never long-cached;
versioned APKs use immutable one-year caching and an Android APK MIME type.

## Restricted SFTP account

Use a dedicated account such as `bymevpn-upload`. Its chroot root must be
owned by root and not writable by the account. Only the `app/` child and
`app/downloads/` should be writable. Do not add the account to the bot or
Docker groups, and do not grant it a shell. A reviewed OpenSSH `Match User`
block should enforce `ChrootDirectory /srv/bymevpn-updates`,
`ForceCommand internal-sftp`, public-key-only authentication, and disable TCP
forwarding, X11 forwarding, and tunnels. Store its authorized key outside the
chroot under a root-owned file. Validate the effective configuration with
`sshd -t` before reloading SSH; keep the current administrative session open
until a second restricted SFTP login is confirmed.

The account should see only `app/update.json` and `app/downloads/`. The API
container should mount `/srv/bymevpn-updates/app` read-only. Never mount the
bot checkout, `.env`, database, VPN configuration, or Docker socket into the
upload account's view.

## Release credentials

The Android repository's tagged release workflow expects these repository
secrets for signing:

- `KEYSTORE_BASE64`
- `KEYSTORE_PASSWORD`
- `KEY_ALIAS`
- `KEY_PASSWORD`

Automatic SFTP publication additionally requires these repository secrets:

- `UPDATE_SSH_HOST`
- `UPDATE_SSH_USER`
- `UPDATE_SSH_KEY`
- `UPDATE_SSH_KNOWN_HOSTS`

Set repository variable `UPDATE_DEPLOY_ENABLED=true` only after the restricted
account and a signed test release are verified. Pin `UPDATE_SSH_KNOWN_HOSTS`
from the host key obtained through the provider console; compare its
ED25519 SHA-256 fingerprint before saving it. Never use `ssh-keyscan` output
as a trust decision by itself.

The public page reads version and download URL from this validated manifest. Include `published_at` (YYYY-MM-DD), `apk_size_bytes`, `min_android_version`, and `signing_certificate_sha256` alongside the required `version_name`, `version_code`, `download_url`, `sha256`, and `release_notes`. The API checks that the reported size and SHA-256 match the APK. Obtain the signing certificate fingerprint from the signed APK with the Android release tooling; never guess or copy it from a different signing key. These fields are optional for backward compatibility, but the page only displays metadata supplied by the current release manifest.

The publishing job writes a versioned APK first, then writes a temporary
manifest and renames it into place. Do not enable the deployment variable
until a real signed release has passed checksum verification and an upgrade
from an installed release has been tested. The API-side integration tests use
temporary APK bytes only; they are not a signed Android release or a live
SFTP test.
