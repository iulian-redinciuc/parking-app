# Phase 8: Hardening for production

**Goal:** the system runs unattended, recovers by itself, and is safe and documented.
**Specs used:** [deployment.md](../design/deployment.md), [security-privacy.md](../design/security-privacy.md), [testing.md](../design/testing.md), [data-model.md §5](../design/data-model.md#5-backups).

---

## P8.1: Compose hardening
**Steps**
1. Every service: `restart: unless-stopped`, a healthcheck, `read_only: true` where possible (with `tmpfs: /tmp`), `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, non-root user.
2. Resource limits tuned to the Phase 4–5 measurements.
3. Pin image tags (e.g. `cloudflare/cloudflared:<version>`, `python:3.12.x-slim-bookworm`). Let Dependabot propose updates.
4. Docker starts on boot (`systemctl is-enabled docker`). The stack comes back after `sudo reboot`.

**Done when:** after `sudo reboot`, the app shows live data again within 3 minutes with no manual steps.

## P8.2: Watchdog for stuck workers
**Steps**
1. Workers write a heartbeat file (`/tmp/heartbeat`) after each successful loop; the Docker healthcheck fails if it's older than 3 × the interval.
2. Docker doesn't restart *unhealthy* containers by itself. Add `willfarrell/autoheal` (or a tiny cron script: `docker ps --filter health=unhealthy -q | xargs -r docker restart`).
3. The API exposes a `restarts` count per worker in `/healthz` (from health messages).

**Done when:** freezing a worker (`docker compose pause vision-occupancy` doesn't count; use a debug command that blocks the loop) gets it restarted automatically within ~1 min.

## P8.3: Backups and restore
**Files:** CLI `parking backup`, `deploy/backup.sh`, crontab entry

**Steps**
1. `parking backup --out DIR` uses the SQLite online backup API, adds `config/` and the reference images, and writes `parking-YYYYMMDD-HHMM.tar.gz`.
2. Host cron at 03:30 runs it in the API container; rotate 14 daily + 8 weekly copies.
3. Off-Pi copy with `rclone` (Google Drive/OneDrive/another machine). **Encrypt** (rclone crypt), because it contains config and coordinates.
4. Also back up `deploy/.env` (VAPID keys!) to the encrypted remote.
5. **Restore drill:** on a spare folder/Pi, restore and start; check `/api/status` and history.

**Done when:** the restore drill succeeds, and is documented in the runbook.

## P8.4: Monitoring and alerts
**Steps**
1. Admin push alerts (P7.8) cover cameras and staleness. Add: disk > 85%, CPU temp > 80 °C sustained 10 min, API restarted, backup failed.
2. External uptime check: a free monitor (e.g. UptimeRobot / Healthchecks.io) on `https://<api>/healthz` every 5 min, emailing you. This catches "the whole Pi or internet is down", which the Pi can't report itself.

**Done when:** pulling the Pi's network cable produces an external alert email within 10 min.

## P8.5: Security review
Go through [security-privacy.md §2](../design/security-privacy.md#2-threats-and-controls) line by line and tick each control as verified:
- [ ] `/internal/*` returns 401 without `WORKER_TOKEN` and 404 through the tunnel
- [ ] `docker ps` / `docker network ls`: only `parking-*` containers and `parking_*` networks belong to this app; no other project's containers were changed
- [ ] Cameras unreachable from outside the camera VLAN (except the Pi)
- [ ] Login rate limit works; tokens expire; logout revokes
- [ ] CSP meta present; no `dangerouslySetInnerHTML`; the admin token is in sessionStorage
- [ ] CORS rejects other origins (`curl -H "Origin: https://evil.example" -I …`)
- [ ] gitleaks scan of the **full history** clean (`gitleaks detect --log-opts="--all"`)
- [ ] Dependabot alerts at zero high/critical
- [ ] Only the API is reachable from outside; `nmap` from outside your network shows nothing

**Done when:** every item is ticked, and the results are noted in PROGRESS.md.

## P8.6: Privacy deliverables
**Steps**
1. Work through the [GDPR checklist](../design/security-privacy.md#4-privacy-and-gdpr-checklist); keep the notes (purpose, lawful basis, DPIA decision) in a private place, not this public repo.
2. Signage at the lot.
3. The privacy screen in the app (`#/privacy`), linked from the footer and the Alerts screen.
4. Verify the retention jobs actually delete data (query the oldest rows).

**Done when:** the checklist is complete, the signage is up, and the privacy screen is live.

## P8.7: Load test
**Files:** `scripts/load/sse.py`

**Steps:** 500 concurrent SSE clients through the tunnel for 10 min, while the replay feed changes every 10 s. Measure the delivery delay (server `updated_at` vs client receive time), API memory and CPU.

**Done when:** p95 delivery < 2 s, no errors, API memory stable.

## P8.8: Power and network resilience
**Steps**
1. Pull the power for 1 min → everything comes back by itself; the app shows stale, then live.
2. Unplug the internet for 10 min → the local pipeline keeps counting; after reconnect, the tunnel recovers and phones resync.
3. Unplug one camera for 10 min → its zone shows stale; the other zones stay live; an admin alert fires; it recovers by itself.
4. Optional UPS for the Pi + PoE switch; test a 5 min outage.

**Done when:** all scenarios pass with no manual help.

## P8.9: Runbook and README
**Files:** `docs/runbook.md`, `README.md`

Runbook sections, each with symptoms → checks (commands) → fix:
- App shows "Can't reach the server"
- A zone is stale / a camera is down
- Counts are wrong (occupancy) → recalibrate
- Counts drifting (flow) → correct, check the clips
- Camera shifted
- Restore from backup
- Rotate a secret (camera password, admin password, VAPID keys: the consequences)
- Update the software
- Add a new language / zone / camera

**Done when:** someone other than you could follow the runbook to fix a stale camera.

---

## Exit criteria
- [ ] Survives a reboot, power cut, internet outage and camera outage with no manual help
- [ ] Backups off-Pi, restore drill done
- [ ] Security and privacy checklists complete
- [ ] External uptime monitoring active
- [ ] Runbook complete
