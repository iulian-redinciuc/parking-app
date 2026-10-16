# Going live: from the PoC to the real thing

The **PoC** is the whole app working end to end in the **dev environment**, with **images standing in for the cameras**. Going live (real cameras, a production environment, real users) is this separate track. **Nothing here blocks the PoC or the agent loop**: these steps start when Iulian decides to, and they need things only he can provide (permission, hardware, accounts).

- Tasks and their PoC status: [../PROGRESS.md](../PROGRESS.md)
- How production is built: [design/deployment.md](design/deployment.md) · Day-to-day operation: [runbook.md](runbook.md)

## 1. Two environments

| | **Dev** (now) | **Prod** (later) |
|---|---|---|
| Purpose | Build, test and demo everything. Stays as the test bed for good | Real users, real counts |
| Machine | The dev Raspberry Pi 5 | Not bought yet. A standalone box at the lot for the cameras ([hardware.md §4.6](design/hardware.md)), and either the same box (T1) or a small cloud VM (T2) for the API ([deployment.md §3](design/deployment.md#3-production-topologies-to-be-chosen)) |
| "Cameras" | Images: the real sample photo, the simulated feed made from it (`parking simulate-feed`), generated images in `samples/generated/`, a synthetic ramp clip | A camera on the box (`picamera:` / `device:`), or network cameras (`snapshot:` / `rtsp:`) if access is ever granted |
| Code and images | Built locally from the checkout (`docker-compose.dev.yml`) | **Released images only** (`ghcr.io/…:v0.x.y`, built by CI for x86-64 and ARM64); never `latest`, never a local build |
| Config | `config/lot.yaml` with `folder:`/`file:`/`video:` sources; `deploy/.env` with dev tokens | Its own `lot.yaml` (camera sources, slot/line files drawn on the real views) and its own `.env` with **fresh secrets** from `deploy/scripts/prod-env.sh`. Nothing secret is ever copied from dev |
| Data | Disposable test data | Its own database, backed up nightly ([deployment.md §7](design/deployment.md#7-backups)) |
| Public address | The preview on GitHub Pages + a temporary quick tunnel (`deploy/scripts/dev-public.sh`) | Its own hostname, HTTPS, the frontend served from the same origin as the API ([deployment.md §5–§6](design/deployment.md#5-public-access-for-the-api)) |
| How changes arrive | Every commit on `main` (the agent loop works here) | A version tag: test on dev → `git tag v0.x.y` → CI builds the images → production pulls that version ([deployment.md §8](design/deployment.md#8-releases-and-updating)); roll back by deploying the previous tag |

**The rule that keeps them apart:** dev and prod share the code and the image format and **nothing else**. Everything that differs is configuration (`lot.yaml`, `.env`, which Compose files), so moving off the Pi is a configuration job, not a rewrite.

### Rehearsal (prod on the dev Pi, before any hardware)
A **production rehearsal** is the production setup started on the dev Pi next to the dev stack: Compose project `parking-rehearsal`, the production Compose files, its own `.env` from `prod-env.sh`, its own data folder, loopback ports only, image feeds as cameras. It is how the PoC checks everything about production that doesn't need the real machines (start-up from released images, migrations, backups and restore, the security check, the load test, restart behaviour), and it is torn down after each check. The dev Pi itself is never rebooted or reconfigured for it.

## 2. Switching from the dev Pi to production

In order. Each step links to where the detail already is.

| # | Step | Who | Detail |
|---|------|-----|--------|
| G1 | **Permission** to point a camera at the car park and run the service; signs; the private GDPR notes | Iulian | [security-privacy.md §4](design/security-privacy.md), [hardware.md §4.6](design/hardware.md) |
| G2 | **Decide the layout:** one box doing everything (T1: cheapest, one machine, public entry through a tunnel) or box + cloud VM (T2: the app stays reachable when the lot's link drops). Ground level only at first, or the ramp camera too | Iulian (the default written down is T2, ground first) | [deployment.md §3](design/deployment.md#3-production-topologies-to-be-chosen) |
| G3 | **Buy** the box (Pi 5, camera, 4G modem + SIM) and, for T2, rent the VM. A domain name | Iulian | [hardware.md §4.5–§4.6](design/hardware.md) |
| G4 | **Provision** each machine: `provision.sh server\|site` (updates, Docker, SSH keys, firewall, VPN) | Iulian runs the script; the loop can do the rest over SSH if it is given access | [deployment.md §10](design/deployment.md#10-provisioning-the-production-machines-t2), phase guide P8.2 |
| G5 | **Release** a version (`git tag v0.x.y`) and write the production `.env` with `prod-env.sh` (fresh secrets) | Loop / Iulian | [deployment.md §8](design/deployment.md#8-releases-and-updating) |
| G6 | **Mount the camera**, switch `cam-ground.source` from the image folder to the camera, grab a frame, **redraw the spaces** on the real view, save the reference image | Iulian mounts; the rest is the admin slot editor | phase guide P4.2–P4.5, [runbook](runbook.md#add-a-language--zone--camera--lot) |
| G7 | **Public entry and frontend:** hostname, HTTPS (tunnel or reverse proxy), the web app served by production | Iulian (DNS/tunnel account), loop | [deployment.md §5–§6](design/deployment.md#5-public-access-for-the-api), P8.3 |
| G8 | **Backups, monitoring, alerts** on the production machines | Loop; Iulian for the storage and uptime-monitor accounts | P8.6, P8.7 |
| G9 | **Measure on the real view:** collect and label ~200 frames, evaluate, tune or train the per-space classifier; health thresholds over a day and a night | Loop + Iulian's eyes on the labels | P4.4, P4.8–P4.10 |
| G10 | **Security and privacy checks, load test, power/network drills** on production | Loop | P8.8–P8.11 |
| G11 | **Staging week**, then share the address | Iulian + testers | [runbook](runbook.md#staging-week-and-go-live), P8.13 |
| G12 | Later: the ramp camera for the underground level (needs a place to mount it), real phones for the notification matrix | Iulian | Phase 5, P6.8 |

**Moving production to different hardware later** (another Pi, a PC, a different cloud): provision the new machine (G4), restore the latest backup onto it ([runbook](runbook.md#restore-from-backup)), deploy the same version with the same `.env` and config, point the hostname at it. The images already exist for both CPU types; only the AI runtime setting may change ([vision.md §11](design/vision.md#11-runtimes-and-performance)).

## 3. Decisions only you can make

Everything else has a working default, written in the decision log. These six don't:

| # | Decision | Default until you decide | Needed by |
|---|----------|--------------------------|-----------|
| D1 | **Permission** to film the car park and run the service, and who is the operator (the name on the privacy page and the signs) | — | G1, before any camera goes up |
| D2 | **Who uses it:** staff only, or anyone? (decides whether the address is shared openly and how many people the notifications must serve) | Staff of the building | G11 |
| D3 | **Layout and budget:** one box at the window doing everything (T1: one machine, the cheapest) or the box plus a small cloud server (T2: also rents a small server; the app stays up when the lot's internet drops) | T2 | G2–G3 |
| D4 | **Underground level:** count it too (needs a place to mount a camera at the ramp, and permission for that) or ground level only | Ground level only | G12 |
| D5 | **Commercial use?** If yes, the AI model's licence (AGPL) is a problem and the detector is swapped for an Apache-licensed one (already prepared) | Non-commercial | Before go-live |
| D6 | **A domain name** (e.g. `parking.example.com`) or the free automatic address (`<server-ip>.sslip.io`) | Free address | G7 |

Small things that are just values: the lot's GPS position and notification radius (go in `.env`), extra UI languages (English now; the signs are generated in Romanian and English).

## 4. What must be proven before go-live

The PoC proved all of this on images and on a rehearsal of production on the dev Pi. These are the checks that only the real setup can pass. The exact commands for each are in [go-live-checks.md](go-live-checks.md).

| | Check | Pass when | Step |
|---|-------|-----------|------|
| C1 | **Accuracy on the real camera:** ~200 real frames from 1–2 weeks (day, dusk, night, rain), labelled, evaluated | ≥ 97% of spaces right; count off by ≤ 1 on ≥ 95% of frames. If not: train the per-space classifier (prepared) | G9 |
| C2 | **The box at the window for a week:** heat behind the glass, reflections at night, 4G drop-outs | No overheating; every outage recovers by itself | G6, G9 |
| C3 | **Real phones:** an Android phone and an iPhone receive notifications | Test notification arrives on both | G7 |
| C4 | **Production basics:** nightly backup + one restore drill, uptime monitor e-mails when the API is down, security check from outside, load test, reboot and power/network-loss drills | All pass as scripted | G8, G10 |
| C5 | **Privacy:** private GDPR notes filled in, signs up at the entrances, privacy page shows the real operator | Done before the camera is switched on | G1, G6 |
| C6 | **Staging week:** 7 clean days with a few testers | Daily app vs. reality checks clean | G11 |
| C7 | *Only with D4:* underground counting: three 1-hour ramp clips tallied by hand, then a week's drift | ≥ 98% of cars counted; drift ≤ 2 cars/day | G12 |

Optional, only if C1 falls short or the licence matters: a fine-tuned detector (needs the C1 frames and a computer with a graphics card, or Google Colab), an AI accelerator for the box.
