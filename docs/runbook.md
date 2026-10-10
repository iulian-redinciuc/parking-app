# Runbook

What to do when the running system misbehaves: symptoms → checks (commands) → fix. Production paths are used (`/opt/parking`, [deployment.md §10](design/deployment.md#10-provisioning-the-production-machines-t2)); "server" is the API machine, "lot box" the vision host.

> Started in P8.6 with backups and restore; the alerts table is from P8.7, the security check from P8.8, the drills from P8.11. The other sections (stale camera, drifting count, tunnel down, full disk, updates, secret rotation, …) are written in [P8.12](phases/phase-8-hardening.md#p812-runbook-and-readme).

## Alerts

Admin alerts are pushes from the API ([notifications.md §5.1](design/notifications.md#51-admin-alerts-p78-p87)); the open ones are listed in the app under *Alerts → Open issues*. The e-mail from the uptime monitor means the API can't be reached from the internet at all ([deployment.md §11](design/deployment.md#11-monitoring-and-the-external-uptime-check-p87)).

| Alert | Check (on the machine named in the alert: the server, or the lot box for "camera …'s machine") | Fix |
|-------|-------|-----|
| *Disk almost full* (> 85%) | `df -h /opt/parking`; `du -sh /opt/parking/data/*`; `docker system df` | Old images: `docker image prune -a` (only on the production machines, they run nothing else). Lot box: `data/debug/` and `data/recordings/` can be deleted. Server: old `parking.sqlite.before-restore-*` files in `data/db/` |
| *The CPU is hot* (≥ 80 °C) | `vcgencmd measure_temp; vcgencmd get_throttled` (Pi); `docker stats --no-stream` | Fan running and case vents free? Out of the sun? If a worker pins the CPU, lower its `interval`/`imgsz` in `lot.yaml` or `VISION_CPUS` in `.env` |
| *The API restarted* | Nothing if you just updated or rebooted. Otherwise `docker inspect -f '{{.State.OOMKilled}} {{.RestartCount}}' parking-api`; `docker logs --since 30m parking-api`; `uptime` (did the machine reboot?) | Out of memory: raise `API_MEMORY` in `.env`. A crash: the traceback is in the log |
| *The backup failed* | The reason is in the alert and in `cat /opt/parking/data/backups/last-run.json`; then the table under [Backups](#backups) | Fix it and run `./backup.sh`: a good run clears the alert |
| Uptime monitor e-mail (down) | From your own connection: `curl -sS https://<host>/healthz`. Then SSH in: `docker compose -f docker-compose.server.yml ps`, `docker logs --tail 50 parking-web`. No SSH either: the provider's console/status page | Start what is down (`docker compose -f docker-compose.server.yml --profile web up -d`); a VM that is off is started in the provider's console |

## Backups

How they work: [deployment.md §7](design/deployment.md#7-backups). Every night at 03:30 the server writes `data/backups/parking-YYYYMMDD-HHMM.tar.gz` (database, `config/`, reference images; 14 daily + 8 weekly kept) and copies it and `deploy/.env` to the encrypted rclone remote `parking-backup`.

**Check that they run**
```bash
cd /opt/parking/deploy
journalctl -t parking-backup --since "2 days ago"     # "... and .env copied to parking-backup (encrypted)"
./backup.sh list                                      # today's archive here AND on the remote
```

| Symptom | Check | Fix |
|---------|-------|-----|
| Nothing in the journal | `cat /etc/cron.d/parking-backup`; `systemctl is-active cron` | `sudo bash scripts/provision.sh server` (writes the cron entry, installs cron and rclone) |
| `NOT copied off this machine (…)` (exit 3) | The reason is in the brackets: no rclone, no `rclone.conf`, no remote `parking-backup`, or it isn't `crypt` | Set up the remote: [phase guide P8.6](phases/phase-8-hardening.md#p86-backups-and-restore), or put your copy of `rclone.conf` back into `deploy/` (mode 600) |
| `the local backup failed` | `docker ps --filter name=parking-api`; `df -h /opt/parking`; run `docker exec parking-api /app/backend/.venv/bin/parking backup --out /app/data/backups` to see the error | Start the API; free disk space; `database integrity check failed` → restore the last good archive (below) |
| `the upload failed` | `rclone --config rclone.conf lsd parking-backup:` | Storage full, key expired or network down: fix it at the storage provider, then run `./backup.sh` |

**A backup by hand** (before an update or a risky change): `./backup.sh`.

`rclone.conf` holds the storage credentials and the two encryption passwords. **A copy must exist outside the server** (password manager). Without it the remote can't be read by anyone, including you.

## Restore from backup

Use it when the database is damaged or lost, a bad change has to be undone, or the server is being replaced. Everything since the archive was made is lost (at most a day): history rows, new push subscriptions, admin corrections. Live counts recover by themselves as soon as the workers deliver again.

**On the same server** (database damaged, undo a change):
```bash
cd /opt/parking/deploy
./backup.sh list                                  # pick an archive; without a name the newest is used
docker stop parking-api                           # the restore refuses while the API runs
./backup.sh restore parking-YYYYMMDD-HHMM.tar.gz  # fetches it from the remote if it isn't in data/backups/
docker compose -f docker-compose.server.yml --profile web up -d
```

**On a new server** (the old one is gone):
```bash
# 1. provision it like the first one (phase guide P8.2 / P8.3): Docker, firewall, VPN, /opt/parking, cron, rclone
sudo bash provision.sh server --version v0.x.y --peer-key <site key> --public-proxy
cd /opt/parking/deploy
# 2. rclone.conf from your password manager -> /opt/parking/deploy/rclone.conf, then:
chmod 600 rclone.conf
./backup.sh list
./backup.sh env                                   # writes .env (name one if the remote has several)
./backup.sh restore                               # the newest archive; uses the release pinned in .env
# 3. start and check
docker compose -f docker-compose.server.yml --profile web up -d
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/api/status | head -c 300       # the counts at backup time, "stale": true until the workers deliver
curl -fsS "http://127.0.0.1:8000/api/history?zone=total&bucket=hour" | head -c 300
scripts/boot-check.sh server                      # waits until no zone is stale
```
Because `.env` came back with its VAPID keys and tokens, push subscriptions, the admin login and the lot box's `WORKER_TOKEN` keep working. What changes with a new machine: its WireGuard key (run `provision.sh site --peer-key <new server key> --endpoint <new address>` on the lot box) and, with a new public address, the DNS record or the `<IP>.sslip.io` name in `PUBLIC_HOST`, `CORS_ORIGINS` and `PUBLIC_APP_URL`.

**What the restore keeps:** the database it replaces is renamed to `data/db/parking.sqlite.before-restore-<time>`, a replaced config file that differed is kept as `<file>.bak`. To undo a restore: stop the API, move that database back (delete `parking.sqlite-wal` / `-shm` next to it first), start the API. Delete the `before-restore` files once the result is confirmed.

| Symptom | Check | Fix |
|---------|-------|-----|
| `restore failed: checksum mismatch` / `can't read` | The archive is damaged | `./backup.sh restore <an older archive>`; delete the damaged file from `data/backups/` so it's fetched again from the remote |
| `parking-api is running: stop it first` | — | `docker stop parking-api` |
| `Permission denied` in `data/` | `ls -ld /opt/parking/data /opt/parking/config` | They must belong to uid 1000 (the containers' user): `sudo chown -R 1000:1000 /opt/parking/data /opt/parking/config` |
| The API doesn't start after a restore: `Can't locate revision` in `docker logs parking-api` | The archive comes from a **newer** release than the one running | Set `PARKING_VERSION` in `.env` to the release that made it (`app_version` in the archive's `manifest.json`) or newer, then `up -d` again |
| Counts stay stale after the restore | `scripts/boot-check.sh server`; on the lot box `docker compose -f docker-compose.site.yml ps` | The workers can't reach the API: VPN (`ping 10.77.0.1` from the lot box), `WORKER_TOKEN` equal on both machines |

**Restore drill** (do it again after big changes, and once with a real production backup): the commands are in the [phase guide P8.6](phases/phase-8-hardening.md#p86-backups-and-restore); they restore into a scratch folder and start the API without network, so the drill can run on any machine with Docker and rclone without touching a running stack.

## Privacy check

With the quarterly security check ([security-privacy.md §4.1](design/security-privacy.md#41-privacy-deliverables-p89)):

```bash
docker exec parking-api /app/backend/.venv/bin/parking db retention      # server: "retention: ok", exit 0
find /opt/parking/data/debug -type f -mmin +$((24*60+10))                # lot box: prints nothing
grep -E '^(DEBUG_CAPTURE|DEBUG_RETENTION_HOURS)=' /opt/parking/deploy/.env   # lot box: false / 24 unless you are debugging
```

| Symptom | Check | Fix |
|---------|-------|-----|
| `OVERDUE n row(s)` | `docker logs parking-api 2>&1 \| grep -E 'prune\|maintenance job failed' \| tail` (one `prune: …` line a day, at 04:00 lot time) | By hand: `docker exec parking-api /app/backend/.venv/bin/parking db prune`; if the job never logs, restart the API and look at the error |
| Old files in `data/debug/` | `docker ps --filter name=parking-vision` (the workers do the pruning, also with capture off) | Start the worker; a longer `DEBUG_RETENTION_HOURS` left from collecting the validation set goes back to 24 |
| The Privacy screen says "on the signs at the lot" instead of the operator | `curl -s https://<PUBLIC_HOST>/api/lot \| grep -o '"privacy":{[^}]*}'` | Set `PRIVACY_OPERATOR` / `PRIVACY_CONTACT` in the server's `.env`, then `docker compose -f docker-compose.server.yml --profile web up -d` |

When what the system stores changes, the Privacy screen's texts (`frontend/src/i18n/locales/*.json`, `privacy.*`), the table in security-privacy.md §4.1 and the signs change with it.

## Security check

After every change to the public entry, the firewall, `.env` or the Compose files, and once a quarter ([security-privacy.md §2.1](design/security-privacy.md#21-checking-the-controls-p88)):

```bash
cd /opt/parking/deploy && scripts/security-check.sh server      # on the server (site on the lot box)
# from your own machine, in a clone of the repo:
ADMIN_PASSWORD='…' deploy/scripts/security-check.sh outside https://<PUBLIC_HOST> <the lot's public address>
deploy/scripts/security-check.sh repo                           # gitleaks over the full history + Dependabot alerts
```

| Failed check | Fix |
|--------------|-----|
| `.env mode` | `chmod 600 /opt/parking/deploy/.env` |
| `… is the dev machine's` / `… is missing or shorter than 32 random bytes` | New value in `.env` (`openssl rand -hex 32`; password hash: `parking admin hash-password`), the same `WORKER_TOKEN` on both machines, then `docker compose … up -d` |
| `… through the public entry` isn't 404 | The proxy forwards more than `/api/*` and `/healthz`: compare the running `parking-web` with `deploy/Caddyfile` (a tunnel: its ingress rules) |
| `CORS: … is allowed` | `CORS_ORIGINS` in `.env` must be exactly `https://<PUBLIC_HOST>` |
| `publishes port … on every interface` / `open TCP ports besides …` | A `ports:` entry without the loopback or VPN address, or a firewall rule too many: `docker ps`, `sudo ufw status` |
| `gitleaks found something` | **Rotate that secret first**, then remove it from the history ([security-privacy.md §3](design/security-privacy.md#3-public-repo-rules)) |

## Power and network drills

After a change to a machine, the VPN, the lot's network or a camera, and before go-live ([testing.md §9](design/testing.md#9-power-and-network-drills-p811-on-the-production-machines)). From your own machine, in a clone of the repo; start one, wait for `live: pull the plug now`, cut, restore, wait for the verdict, and log in nowhere meanwhile:

```bash
cd backend
uv run python ../scripts/resilience/drill.py power-server https://<PUBLIC_HOST>    # the server off for 1 min
uv run python ../scripts/resilience/drill.py power-site https://<PUBLIC_HOST>      # the lot box off for 1 min
uv run python ../scripts/resilience/drill.py internet https://<PUBLIC_HOST>        # the lot's internet off for 10 min
ADMIN_TOKEN=… uv run python ../scripts/resilience/drill.py camera https://<PUBLIC_HOST> --zone ground   # a camera's cable out for 10 min
```

| Not passed because | Look at |
|--------------------|---------|
| `did not come back by itself` / `back after … s` | On the machine that was cut: `deploy/scripts/boot-check.sh server\|site` says what is still waiting (a container not started: Docker not enabled or `restart:` missing; on the lot box the VPN: `sudo wg show`) |
| `the API was unreachable during the drill` | The server must not depend on the lot: its own logs (`docker logs parking-api`), and whether the drill machine was on the lot's network |
| `other zones went stale too` | Both cameras hang on the same cable, switch port or worker: `docker ps` on the lot box, the switch |
| `no admin alert` | `GET /api/admin/alerts` shows the issue but not `active`: the cut was shorter than the grace time (about 3 min); `available: false`: no VAPID keys |
| `… flow events arrived after the reconnect` | No car crossed the ramp during the cut, or the outbox isn't kept: `ls data/outbox/` on the lot box while the link is down |
