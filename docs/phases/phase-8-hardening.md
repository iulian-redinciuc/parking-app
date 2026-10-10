# Phase 8: Production deployment and hardening

**Goal:** move from the dev Pi to the **production environment** chosen in P4.1, and make it run unattended, recover by itself, and be safe and documented.
**Runs on:** production machines (topology T1, T2 or T3 from [deployment.md §3](../design/deployment.md#3-production-topologies-to-be-chosen)). The dev Pi stays the test machine.
**Specs used:** [deployment.md](../design/deployment.md), [hardware.md §4](../design/hardware.md#4-compute-hardware), [security-privacy.md](../design/security-privacy.md), [testing.md](../design/testing.md), [data-model.md §5](../design/data-model.md#5-backups).

## Order
```
P8.1 release images ─> P8.2 provision ─> P8.3 public entry + frontend ─> P8.4–P8.12 hardening ─> P8.13 staging run + go-live
```

---

## P8.1: Release pipeline (multi-arch images)
**Files:** `.github/workflows/release.yml`, `deploy/scripts/release-notes.sh`, `.github/workflows/ci.yml` (`images` job)

**Steps**
1. On tags `v*`: `docker/setup-qemu-action` + `docker/setup-buildx-action` → build `api` and `vision` for `linux/amd64,linux/arm64` → push to `ghcr.io/iulian-redinciuc/parking-api:<tag>` and `parking-vision:<tag>` (plus `:latest`, except for pre-release tags). The tag has to match `version` in `backend/pyproject.toml`.
2. If the production vision host is NVIDIA: also build `parking-vision:<tag>-cuda` for that platform only. (Not needed: P4.1 chose a Raspberry Pi 5.)
3. Pull and start the pushed images on an x86 and an ARM runner (`verify` job).
4. Write release notes from the commit messages (task IDs make this easy): `deploy/scripts/release-notes.sh <tag>`, published as the GitHub release.
5. In CI (`ci.yml`, job `images`): build both images on both CPU types without pushing ([testing.md §5](../design/testing.md#5-ci-githubworkflowsciyml)).

Full description: [deployment.md §8](../design/deployment.md#8-releases-and-updating).

```bash
git tag v0.1.0 && git push origin v0.1.0        # → Actions: Release
gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
docker buildx imagetools inspect ghcr.io/iulian-redinciuc/parking-vision:v0.1.0   # linux/amd64 + linux/arm64
docker pull ghcr.io/iulian-redinciuc/parking-api:v0.1.0                           # on the dev Pi and on an x86 machine
docker pull ghcr.io/iulian-redinciuc/parking-vision:v0.1.0
```

**Done when:** tagging `v0.1.0` produces both images for both CPU types in GHCR, and `docker pull` works on the dev Pi and on an x86 machine.

## P8.2: Provision the production machines
**Steps**
1. Set up the machine(s) for the chosen topology: Linux with automatic security updates, Docker, a firewall that denies everything inbound except what the topology needs, SSH with keys only.
2. **T2 only:** set up the private VPN between the lot box and the API server ([deployment.md §9](../design/deployment.md#9-workers-and-api-on-different-machines-t2)).
3. Create `/opt/parking/` with `deploy/`, `config/`, `models/`, `data/`. Create a **new production `.env`** with fresh secrets. Never reuse the dev tokens, keys or passwords.
4. On the vision host: `parking models export --runtime <runtime for this machine>` ([vision.md §11](../design/vision.md#11-runtimes-and-performance)).
5. **Re-measure on production hardware:** `parking benchmark`, `parking evaluate` on the validation set, `parking evaluate-flow` on the test clips. Re-tune `imgsz`, thresholds and CPU limits in the production config if the numbers differ from the dev Pi. Record them in PROGRESS.md → Metrics, labelled "production".

**Commands (T2, chosen in P4.1).** Scripts and addresses: [deployment.md §9–§10](../design/deployment.md#10-provisioning-the-production-machines-t2). On each machine, logged in with an SSH key:
```bash
# both machines (server = the cloud VM, site = the Pi at the lot); the first run prints the WireGuard public key
curl -fsSLO https://raw.githubusercontent.com/iulian-redinciuc/parking-app/v0.x.y/deploy/scripts/provision.sh
sudo bash provision.sh server --version v0.x.y                      # on the lot box: site
sudo bash provision.sh server --version v0.x.y --peer-key <site key>
sudo bash provision.sh site --version v0.x.y --peer-key <server key> --endpoint <server public IP>
ping -c 3 10.77.0.1                                                 # from the lot box: the VPN is up

# server: new secrets, then the API
cd /opt/parking/deploy
PARKING_VERSION=v0.x.y scripts/prod-env.sh server                   # then fill in what it lists
PARKING_VERSION=v0.x.y docker compose -f docker-compose.server.yml up -d
curl -fsS http://127.0.0.1:8000/healthz

# lot box: the server's WORKER_TOKEN, the same lot.yaml (control_url → http://10.77.0.2:9000 / :9001), camera URLs
cd /opt/parking/deploy
PARKING_VERSION=v0.x.y WORKER_TOKEN=<from the server's .env> scripts/prod-env.sh site
P="docker run --rm --env-file .env -v /opt/parking/config:/app/config -v /opt/parking/data:/app/data -v /opt/parking/models:/app/models -w /app --entrypoint /app/backend/.venv/bin/parking ghcr.io/iulian-redinciuc/parking-vision:v0.x.y"
$P models export --model yolo11n --imgsz 640 --runtime ncnn --out models        # step 4 (the runtime for this machine)
$P benchmark --image data/reference/cam-ground.jpg --camera cam-ground --out data/benchmarks   # step 5
$P evaluate --camera cam-ground --images data/validation/cam-ground             # P4.8's validation set, ≥ 97%
$P evaluate-flow --video data/recordings/<clip>.mp4 --camera cam-ramp           # P5.9's clips, ≥ 98%; fps as in P5.10
PARKING_VERSION=v0.x.y docker compose -f docker-compose.site.yml --profile flow up -d
```
The API server runs no AI, so step 5 is measured on the lot box only; on the server check `docker stats parking-api` (the load test is P8.10).

**Done when:** `PARKING_VERSION=<tag> docker compose up -d` runs on the production machine(s), and accuracy and speed meet the targets on that hardware.

## P8.3: Production public entry and frontend hosting
**Steps**
1. Public HTTPS entry for the API ([deployment.md §5](../design/deployment.md#5-public-access-for-the-api)): a tunnel, or Caddy on a public-IP server. Production hostname, separate from the dev one.
2. Decide production frontend hosting ([deployment.md §6](../design/deployment.md#6-frontend-hosting)): keep GitHub Pages (custom domain optional), another static host, or served by the production reverse proxy. Build with that host's `VITE_BASE` and `VITE_API_BASE`.
3. Production `CORS_ORIGINS`, `PUBLIC_APP_URL`, and **production VAPID keys**. Push subscriptions made against the preview don't carry over: testers re-enable notifications once.
4. Keep the GitHub Pages preview pointed at the dev API (or `mock`) for future testing.

**Chosen (P8.3):** Caddy on the cloud VM (`parking-web`, Option B) as the one public listener, also serving the frontend at the same origin; hostname = a domain if there is one, otherwise `<VM IPv4 with dashes>.sslip.io`. Details: [deployment.md §5 Option B](../design/deployment.md#option-b-reverse-proxy-on-a-machine-with-a-public-ip-t2t3-server), [§6](../design/deployment.md#6-frontend-hosting).

**Files:** `deploy/Caddyfile`, `frontend/Dockerfile` (image `parking-web`), `deploy/docker-compose.server.yml` (service `web`, profile `web`), `deploy/scripts/prod-env.sh` (`PUBLIC_HOST`), `release.yml` / `ci.yml` (the image).

**Commands** (on the server, after P8.2; needs a release that has the `parking-web` image, i.e. a tag after `v0.1.0`):
```bash
sudo bash provision.sh server --public-proxy                  # opens 80, 443/tcp, 443/udp
cd /opt/parking/deploy
# new .env: add PUBLIC_HOST to P8.2's command; an existing .env: set PUBLIC_HOST, CORS_ORIGINS, PUBLIC_APP_URL by hand
PARKING_VERSION=v0.x.y PUBLIC_HOST=203-0-113-7.sslip.io scripts/prod-env.sh server    # or parking.<your domain>
docker compose -f docker-compose.server.yml --profile web up -d
docker compose -f docker-compose.server.yml logs -f web      # "certificate obtained successfully"
H=https://203-0-113-7.sslip.io
curl -fsS $H/healthz && curl -fsS $H/api/status | head -c 200
curl -s -o /dev/null -w '%{http_code}\n' $H/internal/observations   # 404
curl -sN --max-time 5 $H/api/stream | head -n 5               # events arrive at once
```
Then on a phone over mobile data: open `https://<PUBLIC_HOST>/`, install it, enable notifications, *Send test notification* ([notifications.md §6.2](../design/notifications.md#62-how-to-run-it-on-a-phone)). The VAPID keys are the production ones `prod-env.sh` generated (step 3). The Pages preview stays on the dev API / `mock` (step 4, nothing to change).

**Done when:** the production URL works on a phone over mobile data, and push works from production.

## P8.4: Compose hardening
**Steps**
1. Every service: `restart: unless-stopped`, a healthcheck, `read_only: true` where possible (with `tmpfs: /tmp`), `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, non-root user.
2. Resource limits tuned to the production measurements (P8.2), set in the production `.env`.
3. Pin image tags (e.g. `cloudflare/cloudflared:<version>`, `PARKING_VERSION=v0.x.y`; never `latest` in production). Let Dependabot propose updates.
4. Docker starts on boot. The stack comes back after a reboot.

Settings, limits and the Caddy details: [deployment.md §4.1](../design/deployment.md#41-hardening-p84). **Files:** the four `deploy/docker-compose*.yml`, `frontend/Dockerfile` (Caddy as uid 1000), `deploy/.env.example` (`*_CPUS` / `*_MEMORY`), `.github/dependabot.yml`, `deploy/scripts/boot-check.sh`.

**Commands** (on each production machine, after P8.2/P8.3; needs a release tagged after this task, because the non-root `parking-web` image and the scripts come with it):
```bash
cd /opt/parking/deploy
docker compose -f docker-compose.server.yml --profile web up -d      # lot box: -f docker-compose.site.yml --profile flow
docker inspect -f '{{.Name}} ro={{.HostConfig.ReadonlyRootfs}} cap_drop={{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} user={{.Config.User}} {{.HostConfig.RestartPolicy.Name}}' $(docker ps -q --filter name=parking-)
docker stats --no-stream                                             # step 2: set *_CPUS / *_MEMORY in .env from this (and P8.10)
systemctl is-enabled docker                                          # step 4: enabled

sudo reboot                                                          # both machines; then log in again and start nothing
scripts/boot-check.sh site                                           # lot box: "ready <n> s after boot"
scripts/boot-check.sh server                                         # server: also waits for /api/status without stale zones
```
`boot-check.sh server ground` checks only the named zones (before Camera A exists). Then open the app on a phone: live numbers, no *Stale* badge.

**Done when:** after rebooting the production machine(s), the app shows live data again within 3 minutes with no manual steps.

## P8.5: Watchdog for stuck workers
**Steps**
1. Workers write a heartbeat file (`/tmp/heartbeat`) after each successful loop; the Docker healthcheck fails if it's older than 3 × the interval.
2. Docker doesn't restart *unhealthy* containers by itself. Add a `parking-autoheal` container (`willfarrell/autoheal`, limited to containers labelled `autoheal=true`), or a tiny cron script: `docker ps --filter health=unhealthy --filter name=parking- -q | xargs -r docker restart`.
3. The API exposes a `restarts` count per worker in `/healthz` (from health messages).

How it works and why: [deployment.md §4.2](../design/deployment.md#42-watchdog-for-stuck-workers-p85). **Files:** `backend/parking/workers/heartbeat.py` (the file + `python -m parking.workers.heartbeat`), `workers/base.py` (`alive()`, `SIGUSR1` freeze, `started_at`), `api/ingest.py` + `routes/public.py` (`restarts`), `backend/Dockerfile` (`HEARTBEAT_FILE`), `deploy/docker-compose.yml` + `docker-compose.site.yml` (worker healthcheck, label, `autoheal`), `deploy/.env.example` + `prod-env.sh` (`DOCKER_GID`).

As built: the limit is `max(3 × interval, 20 s)`; the label is `parking.autoheal=true` (namespaced, so nothing else on a shared machine matches); the debug command is `SIGUSR1`. An existing `.env` needs one new line: `echo "DOCKER_GID=$(getent group docker | cut -d: -f3)" >> .env`.

**Commands** (any machine that runs workers; in production with a release tagged after this task, lot box: add `-f docker-compose.site.yml`):
```bash
cd deploy
docker compose ps                                              # parking-autoheal and the workers "healthy"
docker compose kill -s USR1 vision-occupancy                   # freeze the loop (log: "SIGUSR1: loop frozen")
watch -n 2 docker compose ps vision-occupancy                  # unhealthy after ~40 s, "Up … seconds" again within ~1 min
docker logs --tail 3 parking-autoheal                          # "found to be unhealthy - Restarting container now"
curl -s localhost:8000/healthz                                 # on the API machine: "restarts":{"cam-ground":1,…}
```

**Done when:** freezing a worker (`docker compose pause vision-occupancy` doesn't count; use a debug command that blocks the loop) gets it restarted automatically within ~1 min.

## P8.6: Backups and restore
**Files:** CLI `parking backup`, `deploy/backup.sh`, crontab entry

**Steps**
1. `parking backup --out DIR` uses the SQLite online backup API, adds `config/` and the reference images, and writes `parking-YYYYMMDD-HHMM.tar.gz`.
2. Cron at 03:30 on the API machine runs it in the API container; rotate 14 daily + 8 weekly copies.
3. Off-machine copy with `rclone` (object storage or another machine). **Encrypt** (rclone crypt), because it contains config and coordinates.
4. Also back up the production `.env` (VAPID keys!) to the encrypted remote.
5. **Restore drill:** on a spare machine (the dev Pi works), restore and start; check `/api/status` and history.

Format, rotation, the nightly job and the restore: [deployment.md §7](../design/deployment.md#7-backups). **Files:** `backend/parking/db/backup.py`, `parking backup` / `parking restore` (`cli.py`), `deploy/backup.sh` (`run` | `list` | `env` | `restore`), `deploy/scripts/provision.sh` (server: `cron`, `rclone`, `/etc/cron.d/parking-backup`), `.gitignore` (`deploy/rclone.conf`), [runbook.md](../runbook.md).

As built: rotation is part of `parking backup`; the cron job runs `deploy/backup.sh` on the host (it needs `docker` and `rclone`, which the hardened API container has neither of); the `.env` goes to the remote as its own file, not into the archive; archive times are UTC, the cron time is the machine's clock.

**Commands** (on the API machine, after P8.2; needs a release tagged after this task). Step 1 is once per machine; pick **one** storage (object storage or another machine):
```bash
cd /opt/parking/deploy
sudo bash scripts/provision.sh server            # again: installs cron + rclone and /etc/cron.d/parking-backup
umask 077; touch rclone.conf
RC="rclone --config rclone.conf"
# 1a. S3-compatible object storage (Backblaze B2, Hetzner Object Storage, Cloudflare R2, ...): a private bucket + a key for it
$RC config create parking-store s3 provider=Other endpoint=<endpoint> access_key_id=<id> secret_access_key=<key>
STORE=parking-store:<bucket>/parking
# 1b. or another machine / a storage box over SFTP (an SSH key for it in ~/.ssh)
$RC config create parking-store sftp host=<host> user=<user> key_file=~/.ssh/id_ed25519
STORE=parking-store:parking
# then the encrypted remote on top of it, with new random passwords
$RC config create parking-backup crypt remote=$STORE password="$(openssl rand -base64 32)" password2="$(openssl rand -base64 32)" --obscure
$RC listremotes --long                           # parking-backup: crypt
# copy rclone.conf into your password manager NOW: without it the backups can't be read

./backup.sh                                      # archive + .env -> the remote; exit 0
./backup.sh list
journalctl -t parking-backup --since yesterday   # the nightly runs (03:30)
```
**Restore drill** on a spare machine with Docker and rclone (the dev Pi: a scratch folder, not the repo's `data/`), given only `rclone.conf` — the same steps as [runbook.md → Restore from backup](../runbook.md#restore-from-backup):
```bash
mkdir -p /tmp/parking-drill/deploy && cd /tmp/parking-drill/deploy       # rclone.conf goes here
B=<path to>/deploy/backup.sh
export BACKUP_ROOT=/tmp/parking-drill BACKUP_ENV_FILE=$PWD/.env BACKUP_RCLONE_CONFIG=$PWD/rclone.conf \
       BACKUP_API_CONTAINER=parking-drill-api BACKUP_IMAGE=ghcr.io/iulian-redinciuc/parking-api:v0.x.y
$B list && $B env && $B restore
docker run -d --name parking-drill-api --network none --read-only --tmpfs /tmp --user 1000:1000 --env-file .env \
  -v /tmp/parking-drill/config:/app/config -v /tmp/parking-drill/data:/app/data $BACKUP_IMAGE
Q='import urllib.request as u; print(u.urlopen("http://localhost:8000/api/status").read()[:300]); print(u.urlopen("http://localhost:8000/api/history?zone=total&bucket=hour").read()[:300])'
docker exec parking-drill-api python -c "$Q"      # the counts at backup time (stale: true) and the history points
docker rm -f parking-drill-api && rm -rf /tmp/parking-drill
```

**Done when:** the restore drill succeeds, and is documented in the runbook.

*Status (P8.6):* the drill passed on the dev Pi with a backup of the **dev** stack through a `crypt` remote on a local folder (numbers in PROGRESS.md → Metrics). Still open, because they need the production API machine (P8.2) and a storage destination only Iulian can provide: the cron entry on that machine, the real off-machine remote, the production `.env` on it, and the drill with a production backup.

## P8.7: Monitoring and alerts
**Steps**
1. Admin push alerts (P7.8) cover cameras and staleness. Add: disk > 85%, CPU temperature high (on machines that report it), API restarted, backup failed.
2. External uptime check: a free monitor (e.g. UptimeRobot / Healthchecks.io) on `https://<api-host>/healthz` every 5 min, emailing you. This catches "the whole machine or its internet is down", which the machine can't report itself.

How it works: [notifications.md §5.1](../design/notifications.md#51-admin-alerts-p78-p87) (the alerts) and [deployment.md §11](../design/deployment.md#11-monitoring-and-the-external-uptime-check-p87) (the two layers). **Files:** `backend/parking/core/system.py` (disk %, CPU temperature), `messages.py` + `workers/base.py` (`disk_pct`, `cpu_temp_c` in the health message), `push/admin_alerts.py` (`disk`, `cpu_temp`, `api_restarted`, `backup_failed`), `deploy/backup.sh` (`data/backups/last-run.json`), the Alerts screen's issue texts, [runbook.md → Alerts](../runbook.md#alerts).

As built: step 1 needs nothing set up beyond *Receive admin alerts* on your phone (Alerts screen, logged in as admin); the lot box's disk and temperature travel in the workers' health messages. Step 2 is an account only you can create; the monitor is a **keyword** monitor so a proxy error page with status 200 doesn't count as up.

**Commands** (production, after P8.3; needs a release tagged after this task on both machines):
```bash
# 1. the admin alerts: a restart is the easy one to see (push "Admin: The API restarted" within ~15 s)
cd /opt/parking/deploy && docker compose -f docker-compose.server.yml restart api
cat ../data/backups/last-run.json                      # after P8.6: {"ts": …, "status": "ok", "message": ""}

# 2. the external check, once, at https://uptimerobot.com (free account, your e-mail):
#    + New monitor → Keyword → URL https://<PUBLIC_HOST>/healthz → keyword "status":"ok" → "exists"
#    → interval 5 minutes → alert contact: your e-mail (down + up)
curl -s https://<PUBLIC_HOST>/healthz | grep -o '"status":"ok"'      # what the monitor looks for

# 3. the check: stop the API, note the time, wait for the e-mail (< 10 min), start it again
docker compose -f docker-compose.server.yml stop api; date
docker compose -f docker-compose.server.yml --profile web up -d      # the "up" e-mail follows
```

**Done when:** stopping the production API (or cutting its network) produces an external alert email within 10 min.

*Status (P8.7):* step 1 is done and tested (dev Pi; the probe also checked inside the hardened API image). Still open, because they need the production API with its public URL (P8.2/P8.3) and a monitoring account only Iulian can create: the monitor itself and the stop-the-API check.

## P8.8: Security review
Go through [security-privacy.md §2](../design/security-privacy.md#2-threats-and-controls) line by line on the **production** setup and tick each control as verified:
- [ ] `/internal/*` returns 401 without `WORKER_TOKEN` and 404 through the public entry; workers' `/control/*` unreachable from outside
- [ ] Production secrets differ from dev; `.env` readable only by its owner (`chmod 600`)
- [ ] Cameras unreachable from outside the camera VLAN (except the vision host)
- [ ] Login rate limit works; tokens expire; logout revokes
- [ ] CSP meta present; no `dangerouslySetInnerHTML`; the admin token is in sessionStorage
- [ ] CORS rejects other origins (`curl -H "Origin: https://evil.example" -I …`)
- [ ] gitleaks scan of the **full history** clean (`gitleaks detect --log-opts="--all"`)
- [ ] Dependabot alerts at zero high/critical
- [ ] From outside: only the public HTTPS entry answers (`nmap` the production address(es))

**Files:** `deploy/scripts/security-check.sh` (the checks, [security-privacy.md §2.1](../design/security-privacy.md#21-checking-the-controls-p88)), `frontend/vite.config.ts` (the CSP `<meta>`), `frontend/e2e/csp.spec.ts`, `deploy/Caddyfile` (`frame-ancestors`), [runbook.md → Security check](../runbook.md#security-check).

**Commands** (production, after P8.3; needs a release tagged after this task):
```bash
# on the dev Pi, once: what the dev secrets look like (hashes only), copied to both machines
cd deploy && scripts/security-check.sh fingerprints > /tmp/dev.fp && scp /tmp/dev.fp <server>:/tmp/ && scp /tmp/dev.fp <lot box>:/tmp/
# on the server and on the lot box
cd /opt/parking/deploy && DEV_FINGERPRINTS=/tmp/dev.fp scripts/security-check.sh server      # lot box: site
# from outside (the dev Pi or a laptop, not over the VPN); the extra address is the lot's public one
ADMIN_PASSWORD='…' deploy/scripts/security-check.sh outside https://<PUBLIC_HOST> <lot public address>
# the repository (needs gitleaks and gh)
deploy/scripts/security-check.sh repo
# cameras: from a device on the lot's normal network (not the camera VLAN, not the lot box)
nc -vz -w 3 <camera address> 554; nc -vz -w 3 <camera address> 80       # both must fail
```

**Done when:** every item is ticked, and the results are noted in PROGRESS.md.

*Status (P8.8):* the review found three gaps, fixed here: the CSP `<meta>` was specified but never built into the page; Dependabot **alerts** are switched off on the repository (a setting only Iulian changes; the lockfiles were audited by hand instead: `pip-audit` and `npm audit` clean); the dev `.env` was mode 664. Checked on the dev Pi, on a throwaway API + `parking-web` pair with fresh secrets: every `outside` check except the port-80 redirect and the port scan (neither exists there), and every `server` check; gitleaks over the full history is clean. The boxes above stay unticked: they are about the production machines (P8.2/P8.3) and the camera VLAN (P4.2), which don't exist yet.

## P8.9: Privacy deliverables
**Steps**
1. Work through the [GDPR checklist](../design/security-privacy.md#4-privacy-and-gdpr-checklist); keep the notes (purpose, lawful basis, DPIA decision) in a private place, not this public repo.
2. Signage at the lot.
3. The privacy screen in the app (`#/privacy`), linked from the footer and the Alerts screen.
4. Verify the retention jobs actually delete data (query the oldest rows).
5. **T3 only:** document that video travels to the cloud machine (encrypted VPN), and how long nothing is kept there.

**Files:** `frontend/src/screens/PrivacyScreen.tsx`, `frontend/src/components/Footer.tsx`, `frontend/e2e/privacy.spec.ts`, `backend/parking/db/rollups.py` (`retention_report`), `parking db retention`, `PRIVACY_OPERATOR` / `PRIVACY_CONTACT` → `privacy` in `GET /api/lot`; what each deliverable is and the sign text: [security-privacy.md §4.1](../design/security-privacy.md#41-privacy-deliverables-p89).

**Commands** (production, after P8.3; needs a release tagged after this task):
```bash
# on the server: who runs the cameras, for the Privacy screen
cd /opt/parking/deploy && nano .env       # PRIVACY_OPERATOR=…  PRIVACY_CONTACT=…
docker compose -f docker-compose.server.yml --profile web up -d
curl -s https://<PUBLIC_HOST>/api/lot | grep -o '"privacy":{[^}]*}'
# step 4, on the server: nothing is kept past its period (exit 1 and OVERDUE lines otherwise)
docker exec parking-api /app/backend/.venv/bin/parking db retention
# step 4, on the lot box: no debug capture older than DEBUG_RETENTION_HOURS (prints nothing)
find /opt/parking/data/debug -type f -mmin +$((24*60+10))
```

**Done when:** the checklist is complete, the signage is up, and the privacy screen is live.

*Status (P8.9):* steps 3 and 4 are built and tested: the Privacy screen with its two links, and `parking db retention`, checked on a copy of the dev Pi's database (ok as it is; 16 planted rows 95–200 days old reported as overdue, gone after `parking db prune`). Step 5 doesn't apply (T2: no picture leaves the lot; the note for T3 is in security-privacy.md §4.1). Still open, because only the operator can do them: the private notes of step 1 (a draft with the technical facts filled in is on the dev Pi, `out/privacy/gdpr-notes.md`, git-ignored), the signs (step 2), and the screen live on the production URL with the operator's name and contact (P8.3).

## P8.10: Load test
**Files:** `scripts/load/sse.py` (what it measures and the verdict's rules: [testing.md §8](../design/testing.md#8-load-test-p810-against-the-public-entry)), `backend/tests/unit/test_load_sse.py`, `backend/tests/integration/test_load_sse.py`

**Steps:** 500 concurrent SSE clients against the **production** public entry for 10 min (outside peak hours), while counts change. Measure the delivery delay (server `updated_at` vs client receive time), API memory and CPU.

As built: the clients are opened 100 a minute (the API's limit of 120 requests a minute per address stays on, and all 500 come from one address), so a run takes about 16 min: 5 min connecting, 10 min measuring. The script only listens: the counts have to change by themselves (at least 10 changes in the 10 min), so pick a quiet time when cars still move, not the night.

**Commands** (from the dev Pi or a laptop, not on the server; after P8.3):
```bash
cd ~/workspace/parking-app/backend
# <server> = the SSH name of the cloud VM: docker stats for parking-api is read there
uv run python ../scripts/load/sse.py https://<PUBLIC_HOST> --ssh <server> --out ../out/load/production.json
echo $?                                   # 0 = passed (prints PASSED, or NOT PASSED with the reasons)
```

**Done when:** p95 delivery < 2 s, no errors, API memory stable.

*Status (P8.10):* the script is built and tested. Still open, because it needs the production public entry (P8.2/P8.3): the run itself. Dev Pi figures are in PROGRESS.md → Metrics; they say the software holds 500 clients, not that the production VM and its network do.

## P8.11: Power and network resilience
**Files:** `scripts/resilience/drill.py` (what it watches and the verdict's rules: [testing.md §9](../design/testing.md#9-power-and-network-drills-p811-on-the-production-machines)), `backend/tests/unit/test_resilience_drill.py`

**Steps**
1. Cut power to each production machine for 1 min → everything comes back by itself; the app shows stale, then live.
2. Cut the lot's internet for 10 min → **T2:** counting continues on site and flow events arrive after reconnect (outbox); **T1:** the app is unreachable, then recovers by itself.
3. Unplug one camera for 10 min → its zone shows stale; the other zones stay live; an admin alert fires; it recovers by itself.
4. Optional UPS for the vision host, switch and router; test a 5 min outage.

As built: the drill script watches the public entry and gives the verdict; one person at the lot (and, for the server, in the cloud provider's console: *power off*, wait, *power on*) does the cutting and nothing else. Between the cut and the verdict nobody logs in to a machine.

**Commands** (from the dev Pi or a laptop on another network than the lot's; after P8.3, with the cameras live). Start one, wait for `live: pull the plug now`, cut for the time given, restore, wait for `PASSED`:
```bash
cd ~/workspace/parking-app/backend
drill() { uv run python ../scripts/resilience/drill.py "$@" --out ../out/drill/$1.json; }
drill power-server https://<PUBLIC_HOST>                  # 1. the cloud VM: power off 1 min, power on
drill power-site   https://<PUBLIC_HOST>                  # 1. the lot box: pull its power plug 1 min
drill internet     https://<PUBLIC_HOST>                  # 2. the lot's router/modem off 10 min; drive a car over
                                                          #    the ramp meanwhile (no flow camera yet: --min-flow 0)
export ADMIN_TOKEN=…                                      # 3. from the server's .env, for reading the alerts
drill camera https://<PUBLIC_HOST> --zone ground          # 3. Camera B's cable out 10 min; then again with
drill camera https://<PUBLIC_HOST> --zone underground     #    Camera A's (also check the push on the admin phone)
drill power-site https://<PUBLIC_HOST> --fault-wait 360   # 4. only with a UPS: mains off 5 min, expected is
                                                          #    "the fault was never seen" (nothing noticed)
```
Topology T1 (everything on one machine at the lot): `power-server` for the power cut and `internet-t1` for the internet cut.

**Done when:** all scenarios pass with no manual help.

*Status (P8.11):* the script is built and tested, and the four scenarios passed on the dev Pi against a throwaway two-zone stack (containers killed and started again, workers taken off the network, a camera's frames removed; figures in PROGRESS.md → Metrics). That says the software recovers by itself, not that the production machines, the VPN and the cameras do. Still open, because it needs the production machines, the lot's network and the cameras (P8.2/P8.3, P4, P5): the drills themselves.

## P8.12: Runbook and README
**Files:** `docs/runbook.md`, `README.md`

Runbook sections, each with symptoms → checks (commands) → fix:
- App shows "Can't reach the server"
- A zone is stale / a camera is down
- Counts are wrong (occupancy) → recalibrate
- Counts drifting (flow) → correct, check the clips
- Camera shifted
- Deploy a new version / roll back
- Restore from backup
- Rotate a secret (camera password, admin password, worker token, VAPID keys: the consequences)
- Add a new language / zone / camera
- Rebuild the dev environment on the Pi (or another dev machine)

**Done when:** someone other than you could follow the runbook to fix a stale camera.

*Status (P8.12):* written ([runbook](../runbook.md)); the README has a *Production* section pointing to it. The stale-camera section was walked through on the dev Pi against a throwaway API + worker (worker stopped, wrong token, API unreachable, camera unreachable, then the fix). The steps that need the production machines (VPN, public entry, provisioning, updates) are written from the specs and get their first real run in P8.13. One thing to know with two machines: a slot or line file saved in the admin editor is written on the server and has to be copied to the lot box (runbook → *Counts are wrong*).

## P8.13: Staging run and go-live
**Steps**
1. Run production for **7 days** with only you and a few testers, comparing against reality daily (as in P4.11/P5.11).
2. Fix anything found and release a new version (P8.1 flow).
3. Go live: share the production URL, put up signage, keep the preview for testing future changes.

**Done when:** 7 clean days, then the production URL is shared.

---

## Exit criteria
- [ ] Running on the production machine(s) from released, version-pinned images
- [ ] Accuracy and speed targets met **on production hardware**
- [ ] Survives a reboot, power cut, internet outage and camera outage with no manual help
- [ ] Backups off the production machine, restore drill done
- [ ] Security and privacy checklists complete
- [ ] External uptime monitoring active
- [ ] Runbook complete; 7-day staging run passed
