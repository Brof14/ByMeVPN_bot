# Production runtime hardening

The bot containers run as UID/GID `10001`, with a read-only root filesystem,
no Linux capabilities, and `no-new-privileges`. `/app/data` remains the only
persistent writable application mount; `/tmp` is a size-limited tmpfs.

Before deployment, grant group `10001` read access to the mounted `.env`, TLS
private keys, and mobile entitlement signing key. Keep their owner as root and
use mode `0440`. Make the top level of the shared bot data directory owned by
`10001:10001` and mode `0750`; keep the Redis subdirectory and its contents
owned by Redis. The container needs read/write access to SQLite, its WAL files,
and the Telegram media cache.

The primary service settings are in `docker-compose.yml`. For the secondary
service, include `deploy/secondary-runtime.override.yml` last in its existing
Compose file list and pin both services to the same release image. Do not put
credentials in Compose `environment` mappings; load them from a root-owned
environment file that is not committed.

The services use host networking. Both webhook listeners bind ports above
1024, so dropping all Linux capabilities does not prevent binding. Keep the
existing network mode during this hardening change; replacing host networking
requires a separate port and routing migration.

Rollback consists of restoring the previous image pins and Compose files,
recreating both bot containers as root with a writable root filesystem, and
restoring the saved ownership/mode values. Do not alter the Redis data mount.
