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

**Done when:** after rebooting the production machine(s), the app shows live data again within 3 minutes with no manual steps.

## P8.5: Watchdog for stuck workers
**Steps**
1. Workers write a heartbeat file (`/tmp/heartbeat`) after each successful loop; the Docker healthcheck fails if it's older than 3 × the interval.
2. Docker doesn't restart *unhealthy* containers by itself. Add a `parking-autoheal` container (`willfarrell/autoheal`, limited to containers labelled `autoheal=true`), or a tiny cron script: `docker ps --filter health=unhealthy --filter name=parking- -q | xargs -r docker restart`.
3. The API exposes a `restarts` count per worker in `/healthz` (from health messages).

**Done when:** freezing a worker (`docker compose pause vision-occupancy` doesn't count; use a debug command that blocks the loop) gets it restarted automatically within ~1 min.

## P8.6: Backups and restore
**Files:** CLI `parking backup`, `deploy/backup.sh`, crontab entry

**Steps**
1. `parking backup --out DIR` uses the SQLite online backup API, adds `config/` and the reference images, and writes `parking-YYYYMMDD-HHMM.tar.gz`.
2. Cron at 03:30 on the API machine runs it in the API container; rotate 14 daily + 8 weekly copies.
3. Off-machine copy with `rclone` (object storage or another machine). **Encrypt** (rclone crypt), because it contains config and coordinates.
4. Also back up the production `.env` (VAPID keys!) to the encrypted remote.
5. **Restore drill:** on a spare machine (the dev Pi works), restore and start; check `/api/status` and history.

**Done when:** the restore drill succeeds, and is documented in the runbook.

## P8.7: Monitoring and alerts
**Steps**
1. Admin push alerts (P7.8) cover cameras and staleness. Add: disk > 85%, CPU temperature high (on machines that report it), API restarted, backup failed.
2. External uptime check: a free monitor (e.g. UptimeRobot / Healthchecks.io) on `https://<api-host>/healthz` every 5 min, emailing you. This catches "the whole machine or its internet is down", which the machine can't report itself.

**Done when:** stopping the production API (or cutting its network) produces an external alert email within 10 min.

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

**Done when:** every item is ticked, and the results are noted in PROGRESS.md.

## P8.9: Privacy deliverables
**Steps**
1. Work through the [GDPR checklist](../design/security-privacy.md#4-privacy-and-gdpr-checklist); keep the notes (purpose, lawful basis, DPIA decision) in a private place, not this public repo.
2. Signage at the lot.
3. The privacy screen in the app (`#/privacy`), linked from the footer and the Alerts screen.
4. Verify the retention jobs actually delete data (query the oldest rows).
5. **T3 only:** document that video travels to the cloud machine (encrypted VPN), and how long nothing is kept there.

**Done when:** the checklist is complete, the signage is up, and the privacy screen is live.

## P8.10: Load test
**Files:** `scripts/load/sse.py`

**Steps:** 500 concurrent SSE clients against the **production** public entry for 10 min (outside peak hours), while counts change. Measure the delivery delay (server `updated_at` vs client receive time), API memory and CPU.

**Done when:** p95 delivery < 2 s, no errors, API memory stable.

## P8.11: Power and network resilience
**Steps**
1. Cut power to each production machine for 1 min → everything comes back by itself; the app shows stale, then live.
2. Cut the lot's internet for 10 min → **T2:** counting continues on site and flow events arrive after reconnect (outbox); **T1:** the app is unreachable, then recovers by itself.
3. Unplug one camera for 10 min → its zone shows stale; the other zones stay live; an admin alert fires; it recovers by itself.
4. Optional UPS for the vision host, switch and router; test a 5 min outage.

**Done when:** all scenarios pass with no manual help.

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
