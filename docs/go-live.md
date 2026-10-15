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

## 3. Live checks moved out of the tasks

Each task's check that needs the real camera, the lot or the production machines is listed here by task ID when the task is closed in PoC mode: what was checked in the PoC, and what remains to be checked for real. Tick them during G6–G12.

<!-- The agent loop appends one "### Pn.m …" entry per task below. -->

### P2.13 Generated images as the ground camera
PoC: all 12 generated images show the reference view and are in the dev replay feed with labels; the appearance scorer reads 204 of 204 spaces right on them (after a fix for darker, yellower light) and the API's ground count follows the feed. Generated and simulated frames are not evidence of accuracy on a real camera.
- [ ] On the real camera's view: the scorer on real frames in different light (morning, evening sun, overcast, wet pavement, night), which is what the colour-cast fix is for. Covered by the validation set and evaluation of P4.8–P4.9 (step G9); needs the camera mounted (G6).
- [ ] Switch `cam-ground.source` from `folder:data/replay/ground-feed` to the camera, redraw the spaces and save a new `reference_image` and `reference_empty` for that view (G6); the generated images and their labels then only serve the dev environment.

### P6.8 Device test matrix
PoC: every expected cell of the matrix (notifications.md §6) passes live on emulated devices on the dev Pi (Pixel 7, iPhone 13 installed / not installed, desktop), with real pushes from the API through FCM to Chromium. The iPhone rows emulate Safari's user agent on Chromium, so they prove the app's iPhone paths, not Safari or Apple's push service.
- [ ] **Android phone, Chrome:** run notifications.md §6.2 steps 1–4 against the public HTTPS API (needs G7 and the frontend built for it); note Android and Chrome versions in §6.1 and PROGRESS.md. Also check that a first *Enable* on a fresh install delivers the test push (on the dev Pi's headless Chromium an endpoint that arrived after a slow first registration sometimes got 410 from FCM), and watch `notification_log` for `failed` / 410 during the staging week (G11).
- [ ] **iPhone (iOS ≥ 16.4), Safari, installed to the Home Screen:** §6.2 steps 1–4 (Apple's push service, `web.push.apple.com`); permission prompt only after the tap; the badge and notification look. Needs an iPhone (Iulian or a tester).
- [ ] **iPhone, Safari, not installed:** §6.2 step 5 (install hint, no button; Tier 1 banner).
- [ ] **Desktop Firefox** (Mozilla's push service): §6.2 steps 1, 3, 4; any desktop with Firefox.
- [ ] **Tier 1 for real:** drive or walk into the radius with the app open (phase 6 exit criterion "a drive test"); `LOT_LAT`/`LOT_LON` of the real lot in the production `.env`.

### P4.1 Production layout, camera and vision host
PoC: the choices are recorded (T2; Raspberry Pi Camera Module 3 Wide on a Raspberry Pi 5 8 GB + SSD at the sample photo's window, hardware.md §4.5), and on images the scorer needs far fewer pixels than the camera gives: 100% on the replay feed down to spaces 13–30 px across (the target for the real camera is ≥ 60 px), and a 12 MP frame takes 0.63 s on the dev Pi, the vision host's hardware. Shrunk photos are not a real lens behind glass.
- [ ] **Confirm the window:** height above the lot (≥ 5 m), that both rows are in view, the distance to the farthest space (Module 3 Wide up to ~35 m, else the standard Module 3), power socket at the window, 4G signal there. Needs Iulian on site, and permission (G1).
- [ ] **Order** the shopping list of hardware.md §4.5 (Pi 5 8 GB, Active Cooler, 27 W PSU, M.2 HAT+ + 256 GB NVMe SSD, case, Camera Module 3 Wide + 300 mm cable, 4G modem + data SIM, optional UPS). Cheapest first step: an old phone at the window as the camera (§4.6) to prove the view before buying. Needs Iulian (payment), step G3.
- [ ] **On the real camera:** `parking grab --camera cam-ground`, measure the farthest space's width in px (≥ 60), check focus and reflections behind the glass; then P4.5 and P4.8–P4.9 on its frames (G6, G9).

### P4.2 Install and network
PoC: in a production rehearsal on the dev Pi (site Compose file, released `parking-vision:v0.1.0`), `deploy/scripts/camera-check.sh` reached a fake network camera (the ground image feed as RTSP H.264 1932×2576 + an HTTP snapshot, behind a login, on an isolated Docker "camera VLAN") from the host's network and from inside the vision container: 4 of 4 checks passed, and it failed correctly on a wrong password and a stopped camera. That network had no route to the internet and only the worker could reach the camera. No real camera, router or 4G link was involved.
- [ ] **Standalone box (the default):** mount it at the window per [hardware.md §6](design/hardware.md#6-mounting-checklist); before fixing it, `parking grab --camera cam-ground` (deployment.md §4.3) and check that every space is visible, sharp and without reflections. The box's 4G link: outbound only, nothing forwarded to it; `provision.sh site` sets the firewall. Needs Iulian on site with the hardware (G3, G6).
- [ ] **Only for a network camera** (an old phone as the first try, or the PoE camera with wired access): camera VLAN with "camera → internet: block", "vision host → camera: allow" on the lot's router; DHCP reservation; a new strong password, firmware update, cloud/P2P/UPnP off, privacy masks over neighbouring windows and the street; main stream native/2560×1440 H.264, sub-stream 640×360, WDR, NTP + timezone. Needs access to the lot's network (not granted as of 2026-10-10).
- [ ] Put the camera URLs in the box's `deploy/.env` and run `./scripts/camera-check.sh -f docker-compose.site.yml`: every line PASS. From a device on the lot's normal network, `nc -z <camera> 554` must fail (phase 8 security checklist).

### P4.3 Snapshot and RTSP sources
PoC: in a production rehearsal on the dev Pi, the occupancy worker read a fake network camera (the ground image feed at 1932×2576 behind a login) through `snapshot:` for 21 min and through `rtsp:` for 33 min, sampling every 2 s, with the camera taken away for 2 min (once and twice): memory stayed flat (snapshot 171 → 168 MB, RTSP within 195–268 MB and lower at the end), the thread count never changed, health went `degraded` → `down` while it was gone and back to `ok` within ~1–2 min of its return, without a container restart. No real camera, cable or camera module was involved, and no run was an hour long.
- [ ] **Standalone box (the default, `picamera:` source):** with the box at the window, run the worker 1 h with the sampler on the box (`python3 backend/scripts/soak.py sample --containers parking-vision-occupancy --interval 60 --duration 3600`): RSS without an upward trend, no restart, an observation every interval in the server's `/healthz`. Then the camera gone for 2 min: power the box off, take the ribbon cable out (never with power on), power on for 2 min (`cam-ground` `down`), power off, cable back, power on (`ok` again). Needs Iulian on site with the hardware (G3, G6).
- [ ] **Network camera (only with wired access, or an old phone as a test camera):** `snapshot:` (preferred) in `lot.yaml`, worker 1 h with the sampler as above: RSS flat, observations every interval; then unplug the camera's cable (or turn the phone's camera app off) for 2 min: `down` in `/healthz` and the admin screen, back to `ok` within ~1 min of plugging it in; repeat with `rtsp:` if it will be the fallback.

