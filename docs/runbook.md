# Runbook

What to do when the running system misbehaves, and how to do the routine jobs: symptoms → checks (commands) → fix. Production paths are used (`/opt/parking`, [deployment.md §10](design/deployment.md#10-provisioning-the-production-machines-t2)); "server" is the API machine (the cloud VM), "lot box" the vision host at the lot.

| Something is wrong | Routine jobs | Regular checks |
|--------------------|--------------|----------------|
| [The app can't reach the server](#the-app-shows-cant-reach-the-parking-server) | [Deploy a new version / roll back](#deploy-a-new-version--roll-back) | [Backups](#backups) |
| [A zone is stale / a camera is down](#a-zone-is-stale--a-camera-is-down) | [Restore from backup](#restore-from-backup) | [Privacy check](#privacy-check) |
| [Counts are wrong (occupancy)](#counts-are-wrong-occupancy-camera) | [Rotate a secret](#rotate-a-secret) | [Security check](#security-check) |
| [Counts are drifting (flow)](#counts-are-drifting-flow-camera) | [Add a language / zone / camera](#add-a-language--zone--camera) | [Power and network drills](#power-and-network-drills) |
| [Camera shifted](#camera-shifted) | [Rebuild the dev environment](#rebuild-the-dev-environment) | [Staging week and go-live](#staging-week-and-go-live) |
| [An alert arrived](#alerts) | | |

## Where things are

| | Server (cloud VM) | Lot box (vision host) |
|---|---|---|
| Containers | `parking-api`, `parking-web` (Caddy + the app) | `parking-vision-occupancy` (camera `cam-ground`), `parking-vision-flow` (`cam-ramp`), `parking-autoheal` |
| Compose file (in `/opt/parking/deploy`) | `docker-compose.server.yml`, profile `web` | `docker-compose.site.yml`, profile `flow` |
| Address on the VPN (`wg0`) | `10.77.0.1` (API on port 8000) | `10.77.0.2` (workers' control ports 9000, 9001) |
| `/opt/parking/deploy/.env` | all the API's secrets | the camera URLs, the server's `WORKER_TOKEN` |
| `/opt/parking/config/` | `lot.yaml`, `slots/`, `lines/`: **the same files on both machines** | |
| `/opt/parking/data/` | `db/` (the database), `backups/` | `reference/`, `outbox/`, `debug/`, `recordings/` |

The lot box has no public address. Reach it through the server: `ssh -J <you>@<server> <you>@10.77.0.2`.

The commands below use these two shorthands. Paste the one for the machine you are on first:

```bash
# server
cd /opt/parking/deploy && DC="docker compose -f docker-compose.server.yml --profile web"
# lot box
cd /opt/parking/deploy && DC="docker compose -f docker-compose.site.yml --profile flow"
```

`.env` is read when a container is **created**: after changing it run `$DC up -d` (it recreates what changed). `docker restart` keeps the old values. `lot.yaml` is read when a process starts, so there `docker restart <container>` is enough.

Admin calls from a shell use the static token (on the server; from elsewhere copy it from your password manager):

```bash
ADMIN_TOKEN=$(sed -n 's/^ADMIN_TOKEN=//p' /opt/parking/deploy/.env)
HOST=https://<PUBLIC_HOST>            # on the server itself http://127.0.0.1:8000 works too
```

The `parking` command line on the lot box (for a grab, a recording, an evaluation) runs in the vision image:

```bash
P="docker run --rm --env-file .env -v /opt/parking/config:/app/config -v /opt/parking/data:/app/data -v /opt/parking/models:/app/models -w /app --entrypoint /app/backend/.venv/bin/parking ghcr.io/iulian-redinciuc/parking-vision:$(sed -n 's/^PARKING_VERSION=//p' .env)"
```

## The app shows "Can't reach the parking server"

**Symptoms:** the red banner *Can't reach the parking server* with the header on *Offline*, for everybody. (*You're offline* is the phone's own connection; *Camera data is n min old* is [a stale zone](#a-zone-is-stale--a-camera-is-down): the server answers, the cameras don't.)

**Check**, from a connection that is not the server's or the lot's (a phone on mobile data, your laptop):

```bash
curl -sS -m 10 -o /dev/null -w '%{http_code}\n' https://<PUBLIC_HOST>/healthz     # 200 = the server is fine
curl -sS -m 10 https://<PUBLIC_HOST>/api/status | head -c 300
```

| `curl` says | Cause | Fix |
|-------------|-------|-----|
| `Could not resolve host` | The name doesn't point at the server | A domain: its DNS A record. No domain: the name is `<the server's IPv4 with dashes>.sslip.io`, so a new address means a new `PUBLIC_HOST`, `CORS_ORIGINS` and `PUBLIC_APP_URL` in `.env`, then `$DC up -d` |
| `Connection timed out` / `refused` | The VM is off, or ports 80/443 are closed | The provider's console: is it running? Then SSH in: `sudo ufw status` (80, 443 allowed), `$DC ps` (`parking-web` up), `$DC up -d` |
| A certificate / TLS error | Caddy couldn't get or renew the certificate | `docker logs --tail 50 parking-web`: it needs ports 80 and 443 open and `PUBLIC_HOST` resolving to this machine. Then `docker restart parking-web` |
| `502` | Caddy runs, the API doesn't | `$DC ps`; `docker logs --tail 50 parking-api` (the error is at the end); `$DC up -d`. `Can't locate revision`: the database is from a newer release, see [Restore](#restore-from-backup) |
| `429` | The rate limit (120 requests a minute per address) | Wait a minute. If it's everybody: `docker logs --since 10m parking-web` shows who is hammering |
| `200` for `/healthz`, `503` for `/api/status` | The API is up and has never had data since it started | [A zone is stale](#a-zone-is-stale--a-camera-is-down) |
| `200` for both, the app still can't | The app is opened from another address than the API allows | `grep -E '^(PUBLIC_HOST\|CORS_ORIGINS)=' .env`: `CORS_ORIGINS` must be exactly `https://<PUBLIC_HOST>`. Then `$DC up -d` |

No SSH either: the machine is off or its network is gone, which only the provider's console and status page can show. After any fix: `scripts/boot-check.sh server` waits and prints `ready …` once every container is healthy and no zone is stale. (Its last line, `later than the limit`, and its exit code 1 are about the time since the machine booted: they only matter right after a reboot.)

## A zone is stale / a camera is down

**Symptoms:** a zone says *not live* and the banner *Camera data is n min old* (or *No camera data yet*); the admin alert *Camera … is down* (after about 3 min) or *…: no fresh data* (after 5 min). The app keeps showing the last known numbers.

**1. Which camera, and what does it say?** In the app: *Admin* → the camera list. Or from a shell:

```bash
curl -s $HOST/healthz                                                      # "cameras":{"cam-ground":"down",…}
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" $HOST/api/admin/cameras    # state, issue, last_frame_age_s, last_health_age_s
```

| `state` / `issue` | Meaning | Go to |
|-------------------|---------|-------|
| `unknown`, or `down` with `last_health_age_s` growing past 30 | The API hears nothing from the worker | step 2 |
| `down` / `degraded` with `issue: connect_failed` | The worker runs and reports, but gets no picture from the camera | step 3 |
| `issue: black`, `frozen` or `blurry` | The camera delivers pictures the worker can't use | step 4 |
| `issue: shifted` | The camera moved | [Camera shifted](#camera-shifted) |
| `ok`, but the zone is still stale | Frames arrive, results don't | step 2 (the worker's log) |

**2. The worker and its way to the API** (on the lot box). No SSH to the lot box at all: its power or the lot's internet is out, and somebody has to go and look (power LED, router, cables); it starts and reconnects by itself once both are back.

```bash
$DC ps                                              # every container "Up … (healthy)"
docker logs --tail 50 parking-vision-occupancy      # or parking-vision-flow
ping -c 3 10.77.0.1                                 # the server over the VPN
curl -sS -m 5 http://10.77.0.1:8000/healthz         # the API over the VPN
```

| What you see | Fix |
|--------------|-----|
| The container is missing or `Exited` | `$DC up -d`. If it exits again, the last log lines say why (a field in `lot.yaml`, a missing slot file or model) |
| `Restarting` / `unhealthy` over and over | The worker's loop hangs and `parking-autoheal` keeps restarting it: the log before each restart shows where. Out of memory (`docker inspect -f '{{.State.OOMKilled}}' parking-vision-occupancy`): raise `VISION_MEMORY` / `FLOW_MEMORY` in `.env`, `$DC up -d` |
| `POST /internal/… failed, dropped: ConnectError` / `ConnectTimeout`, `ping` fails | The VPN is down: `sudo wg show` (a `latest handshake` under 3 min is good), `sudo systemctl restart wg-quick@wg0`. Still nothing: the lot's internet (`ping -c 3 1.1.1.1`), or the server (the section above) |
| `ping` works, `curl` doesn't | The API isn't running, or not on the VPN address: on the server `$DC ps` and `grep VPN_BIND_IP .env` (`10.77.0.1`) |
| `failed, dropped: HTTP 401` | `WORKER_TOKEN` differs between the two machines: copy the server's into the lot box's `.env`, `$DC up -d` |
| `failed, dropped: HTTP 422` | The two machines' `lot.yaml` differ (the server doesn't know this camera or its role): make them the same, restart the API and the worker |

**3. The camera** (on the lot box):

```bash
grep -E '^CAM_.*_URL=' .env | sed -E 's#//[^@]*@#//<user:password>@#'      # the camera's address, without the password
ping -c 3 <camera address>
$P grab --camera cam-ground --out data/debug/check.jpg --force             # "… frame from 'cam-ground' …", or why no frame comes
```

| What you see | Fix |
|--------------|-----|
| `ping` fails | Power and cable: the PoE port's light on the switch, the cable, the camera's own LED. Power-cycle the camera (unplug its PoE cable for 10 s). A changed address: give it back its fixed address in the router ([hardware.md](design/hardware.md)) |
| `ping` works, the grab ends with `no frame from … : HTTP 401` (or `403`) | The camera's password isn't the one in `.env`: [Rotate a secret](#rotate-a-secret) |
| `ping` works, the grab ends with a timeout or `HTTP 404` | The stream or snapshot is switched off in the camera, or the path in the URL is wrong: the camera's web page (from the lot box's network), then the URL in `.env` |
| The grab works | The camera is fine now: `docker restart parking-vision-occupancy` and watch its log. Delete `data/debug/check.jpg` |

**4. The picture.** *Admin* → the camera → *New snapshot* shows what the worker sees (also a frame it refused).

| Issue | Usual cause | Fix |
|-------|-------------|-----|
| `black` | Lens covered, no light and no infrared at night | Uncover it; switch the camera's IR / night mode on. A lit lot that is simply dark: lower `health.black_mean_max` for that camera in `lot.yaml` |
| `frozen` | The camera repeats one picture (its firmware hangs) | Power-cycle the camera; update its firmware if it comes back |
| `blurry` | Dirt, water or a spider's web on the lens; focus lost | Clean the lens, refocus. Soft night pictures that are fine to the eye: lower `health.blur_laplacian_min` (how to measure: [vision.md §5](design/vision.md#5-frame-health-parkingvisionhealthpy)) |

A threshold change in `lot.yaml` goes on **both** machines; then `docker restart` the worker.

**5. Confirm.** The zone is live again within a minute of the first good frame (3 consistent readings for a changed space):

```bash
curl -s $HOST/healthz                 # the camera is "ok"
scripts/boot-check.sh server          # on the server: "ready …" = every container healthy, no zone stale
```

The alert sends its own "…is back up" push. A flow zone that was blind while cars drove in or out now has a wrong count: [correct it](#counts-are-drifting-flow-camera).

## Counts are wrong (occupancy camera)

**Symptoms:** the free count of a `slots` zone (ground) differs from what you see at the lot, while the camera is `ok`.

**Check:** *Admin* → the camera → the annotated snapshot. Every space is drawn in its colour for free or taken.

| What the snapshot shows | Cause | Fix |
|-------------------------|-------|-----|
| All shapes sit beside their spaces by the same amount | The camera moved | [Camera shifted](#camera-shifted) |
| One or a few shapes are off, cover a neighbour's car, or a space is missing | The slot file | *Edit parking spaces*: drag the corners onto the **ground** of the space (where the tyres stand), add or delete spaces, *Save* |
| The shapes are right, some spaces are still read wrong (a shadow, a wet patch, a dark car on dark ground) | The scoring | Tune it with the numbers below |
| The picture is right and so is the count, the app shows an older number | A space changes only after 3 readings in a row agree | Wait 15 s. If it stays: [stale](#a-zone-is-stale--a-camera-is-down) |

**After *Save* with the server and the lot box on two machines:** the editor writes `config/slots/<camera>.json` on the **server**, but the worker reads the lot box's copy. Until the copies are the same, the app counts with the new spaces and the worker measures the old ones. Copy the file over and restart the worker, from your own machine:

```bash
scp <you>@<server>:/opt/parking/config/slots/cam-ground.json /tmp/cam-ground.json
scp -J <you>@<server> /tmp/cam-ground.json <you>@10.77.0.2:/opt/parking/config/slots/cam-ground.json
ssh -J <you>@<server> <you>@10.77.0.2 docker restart parking-vision-occupancy
```

(`Permission denied`: the `config/` folders belong to uid 1000, the containers' user; copy as that user or `sudo chown -R 1000:1000 /opt/parking/config` afterwards.)

**Tuning the scoring** (on the lot box, on pictures you counted by hand):

```bash
$P grab --camera cam-ground --out data/debug/now.jpg --force
$P analyze --image data/debug/now.jpg --camera cam-ground --out data/debug/analyze     # now.json (score per space) + now.png
$P analyze --image data/debug/now.jpg --camera cam-ground --out data/debug/analyze --threshold 0.25    # try another threshold
$P evaluate --camera cam-ground --images data/validation/cam-ground --sweep 0.15:0.5:0.05   # the labelled validation set: the best threshold overall
```

Put the value into `occupancy.threshold` of that camera in `lot.yaml` on both machines and `docker restart parking-vision-occupancy`. Don't tune on one picture: a threshold that fixes today's shadow can break the night. The target is ≥ 97% of spaces right on the validation set ([vision.md §10](design/vision.md#10-evaluation-parkingvisionevaluatepy)). Delete the pictures in `data/debug/` afterwards (they can show people and plates).

## Counts are drifting (flow camera)

**Symptoms:** the count of a `flow` zone (underground) moves away from the real number of cars over days; the app shows the zone's number with `≈` (low confidence); the admin alert *…: entry/exit count is off* (more than 3 entries or exits in a day that would have taken it below 0 or above the capacity).

A flow zone only counts cars crossing a line, so every missed or doubled car stays in the number until somebody corrects it.

**Fix first: correct the count.** Count the cars on the level, then *Admin* → the zone's form (number, note), or:

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"occupied": 37, "note": "hand count"}' $HOST/api/admin/zones/underground/correct
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "$HOST/api/admin/corrections?limit=10"     # the log: old → new, when, who
```

Phones show the new number at once. If the level is empty every night, let the app do it: `zones[].reset` in `lot.yaml` (`enabled: true`, `cron: "0 3 * * *"`, `value: 0`; [config.md §1](design/config.md#1-configlotyaml)), on the server, then `docker restart parking-api`.

**Then find out why**, when the corrections are more than a car or two a day:

| Check | Command | Fix |
|-------|---------|-----|
| Was the camera away? Cars that pass while it is down are never counted | *Admin* → cameras; `docker logs --since 24h parking-vision-flow \| grep -ci reconnect` on the lot box | [A camera is down](#a-zone-is-stale--a-camera-is-down) |
| Did events wait on the lot box? | `ls -l /opt/parking/data/outbox/` (empty when everything was delivered) | They are sent when the link is back; nothing to do |
| Are the lines still where the cars drive? | `$P lines-check --camera cam-ramp --image data/debug/ramp.jpg --out data/debug/ramp-lines.jpg` after a `$P grab --camera cam-ramp --out data/debug/ramp.jpg --force` | *Admin* → the camera → *Edit counting lines*, *Save* (two machines: copy `config/lines/cam-ramp.json` to the lot box like a slot file, restart `parking-vision-flow`) |
| How well does it count? | Check the clips, below | |
| Is there a barrier on the zone too? The alert *…: camera and barrier disagree* names both counts for today ([barrier.md §4](design/barrier.md#4-one-source-or-two-api)) | Which one matches a hand count? `docker logs --since 24h parking-barrier \| grep -c ' IN,'` (and `' OUT,'`) against the camera's log | The camera is off: the rows above. The barrier is off: `LOG_LEVEL=DEBUG` shows every edge of its contacts (a loose wire, a relay that chatters: raise `min_gap_ms` / `debounce_ms` in `lot.yaml`). Then correct the count: that also starts the comparison again |

**Check the clips.** Record an hour, count it by hand, compare:

```bash
$P record --camera cam-ramp --minutes 60 --out data/recordings          # on the lot box, at the time of day that drifts
# copy the .mp4 to your own machine (it never goes into the repo), then in a clone of the repo:
python3 -m http.server 8765 --bind 127.0.0.1                             # open http://127.0.0.1:8765/tools/flow-tally/
#   press I / O as each car crosses, export the CSV to data/labels/<clip name>.csv
cd backend && uv run parking evaluate-flow --video ../data/recordings/<clip>.mp4 --camera cam-ramp \
  --debug-video ../out/eval/<clip>.mp4                                   # TP / FP / FN, accuracy, net error
```

The target is ≥ 98% of crossings and a net error of at most 2 cars a day ([vision.md §10](design/vision.md#flow-metrics-per-clip)). The debug video shows each miss: cars counted twice or not at all at the same spot mean the lines (move them apart, away from where cars stop or turn); misses at night mean the picture (lighting, `detector.conf`); tailgating cars merged into one mean `flow.min_track_frames`. Try a value with `--lines`, `--conf` or `--min-track-frames` on the same clip before changing `lot.yaml`. Delete the recordings when done.

## Camera shifted

**Symptoms:** the admin alert *Camera … has shifted*; *Admin* shows the camera `degraded` with *camera moved*; the shapes on the annotated snapshot sit beside the spaces. The worker keeps counting, with shapes that now point at the wrong ground. It never clears by itself.

**Check:** *Admin* → the camera → the annotated snapshot. Compare with the reference picture if in doubt (`/opt/parking/data/reference/<camera>.jpg` on the lot box).

| What happened | Fix |
|---------------|-----|
| The camera was knocked and can be turned back | Turn it until the shapes sit on the spaces again in a *New snapshot*, tighten the mount, then *Save reference frame* |
| It stays where it is now (a new mount, a small permanent nudge) | *Edit parking spaces* (or *Edit counting lines*) → *Move all* and drag every shape onto its space, fix single corners, *Save* (two machines: copy the file to the lot box as in [Counts are wrong](#counts-are-wrong-occupancy-camera)), then *Save reference frame* |
| The shapes are right, the alert is wrong (scaffolding, a parked lorry, snow changed the background) | *Save reference frame* |

*Save reference frame* stores the current picture as the new comparison; from a shell: `curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" $HOST/api/admin/cameras/cam-ground/reference-frame`. The alert clears with the next check (within 5 min). Save it only when the shapes are right: it is what "not shifted" means from then on. Afterwards compare the count with the lot once.

## Deploy a new version / roll back

A version is a release tag (`v0.x.y`): the images in GHCR and the `deploy/` files of that tag ([deployment.md §8](design/deployment.md#8-releases-and-updating)). **Making** a release, from a clone on your own machine: set `version` in `backend/pyproject.toml`, `cd backend && uv lock`, commit, wait for CI on `main` to be green, then `git tag v0.x.y && git push origin v0.x.y` and wait for the *Release* workflow.

**Deploy**, the server first, then the lot box (the API accepts an older worker; unknown fields are ignored both ways):

```bash
cd /opt/parking/deploy
grep '^PARKING_VERSION=' .env                             # note the version running now: it is the roll-back
./backup.sh                                               # server only: a backup from just before
sudo bash scripts/provision.sh server --version v0.x.y    # lot box: site. Copies that release's deploy/ files; .env, rclone.conf and existing config files are kept
sed -i 's/^PARKING_VERSION=.*/PARKING_VERSION=v0.x.y/' .env
$DC pull && $DC up -d
docker logs --tail 30 parking-api                         # server: migrations run at start-up
scripts/boot-check.sh server                              # lot box: site. "ready …" = everything healthy, no zone stale
```

Then open the app: an installed app picks the new version up on its next start (once more if it shows the old one). Read the release notes for anything to add to `.env` or `lot.yaml` (`diff .env.example .env` shows new variables).

**Roll back:** the same commands with the previous tag, on both machines.

| Symptom after a roll-back | Fix |
|---------------------------|-----|
| `Can't locate revision` in `docker logs parking-api` | The newer release changed the database. Restore the backup made before the update: `docker stop parking-api`, `./backup.sh restore <that archive>`, `$DC up -d` ([Restore](#restore-from-backup)); what was recorded since is lost |
| `pull` fails: `manifest unknown` | The tag doesn't exist or its release workflow failed: the *Actions* page. A failed release is fixed with a new tag, tags are never moved |

## Rotate a secret

All secrets are in `/opt/parking/deploy/.env` (mode 600) on the machine that uses them, and in your password manager. Rotate one when it may have leaked, when somebody who knew it leaves, and after a machine is replaced or lost. Edit `.env`, then `$DC up -d` on that machine. The nightly backup copies the server's new `.env` to the encrypted remote; run `./backup.sh` to do it now.

| Secret | How | Consequences |
|--------|-----|--------------|
| **Camera password** (`CAM_*_URL`, lot box) | Change it in the camera's web page first, then in the URL in `.env` (special characters URL-encoded, e.g. `@` → `%40`), `$DC up -d`. Check: `$P grab --camera cam-ground --out data/debug/check.jpg --force` | The camera is `down` between the two steps (the zone goes stale after a minute; a flow camera misses the cars that pass: [correct the count](#counts-are-drifting-flow-camera)). Nothing for the users |
| **Admin password** (`ADMIN_PASSWORD_HASH`, server) | `docker exec -it parking-api /app/backend/.venv/bin/parking admin hash-password`, paste the printed line into `.env` as it is (single quotes included), `$DC up -d` | The old password stops working. Sessions already signed in stay valid until they expire (7 days at most): end them now with the command below |
| **Admin token** (`ADMIN_TOKEN`, server) | `openssl rand -hex 32` into `.env`, `$DC up -d` | Scripts and saved commands holding the old token get `401`. Sign-ins with the password are not affected |
| **Worker token** (`WORKER_TOKEN`, **both** machines) | `openssl rand -hex 32`; the same value into both `.env` files; `$DC up -d` on the server, then at once on the lot box | Between the two, the workers' results are refused (`HTTP 401` in their logs) and the admin snapshots fail. Occupancy readings from that gap are dropped (the next one replaces them); entry/exit events wait in the lot box's outbox and are delivered afterwards, so no count is lost |
| **VAPID keys** (`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, server) | Only if the private key leaked. `docker exec parking-api /app/backend/.venv/bin/parking push vapid-keys`, both lines into `.env`, `$DC up -d` | **Every notification subscription stops working**, drivers' and admins', and nobody is told: each person has to open *Alerts* and tap *Enable notifications* again (the app then replaces the old subscription), and admins switch *Receive admin alerts* back on. Until then no reminders and **no admin alerts**: do your own device first. The dead subscriptions delete themselves as the push services refuse them |
| **Backup storage key / encryption passwords** (`deploy/rclone.conf`, server) | A new access key at the storage provider into `rclone.conf`; `./backup.sh` to check. The two encryption passwords can't be changed for existing archives: for those, start a new remote folder with new passwords and delete the old one after 60 days | A copy of the new `rclone.conf` goes into your password manager, or the backups can't be read by anyone |
| **VPN keys** (`/etc/wireguard/parking.key`, each machine) | On the machine whose key changes: `sudo rm /etc/wireguard/parking.key`, then `sudo bash scripts/provision.sh server --peer-key <the other machine's key>` (lot box: `site … --endpoint <server address>`); it prints the new public key. On the **other** machine run it again with `--peer-key <new key>`. A machine's current public key: `sudo wg show wg0 public-key` | The link is down between the two runs: zones stale, events queued |

End every admin session (after a password change, or when a signed-in device is lost):

```bash
docker exec parking-api /app/backend/.venv/bin/python -c "
import sqlite3; c = sqlite3.connect('/app/data/db/parking.sqlite', timeout=30)
print(c.execute('delete from admin_session').rowcount, 'sessions ended'); c.commit()"
```

A secret that was pushed to the public repository counts as leaked even if the commit is removed: rotate it first ([security-privacy.md §3](design/security-privacy.md#3-public-repo-rules)). After any rotation run the [security check](#security-check).

## Add a language / zone / camera

Each of these is a change in the repository, released and deployed like any [new version](#deploy-a-new-version--roll-back), except for the files in `/opt/parking/config/`, which are edited on the machines.

**A language** (say `ro`; [frontend.md §7](design/frontend.md#7-languages-i18n)):
1. Copy `frontend/src/i18n/locales/en.json` to `ro.json` and translate the values (keys and `{{placeholders}}` stay; plural keys follow the language's own forms, Romanian has `_one`, `_few`, `_other`).
2. Add it to `RESOURCES` in `frontend/src/i18n/index.ts`.
3. Zone names: `name: { en: "Ground", ro: "Parter" }` for each zone in `lot.yaml`, on the server; `docker restart parking-api`.
4. `cd frontend && npm run lint && npm test -- --run`, release, deploy. A phone set to that language gets it by itself; the others fall back to English. Admin alerts stay in English.

**A zone** (a new level or area with its own number; [config.md §1](design/config.md#1-configlotyaml)):
1. Add it under `zones:` in `lot.yaml`: an `id` (lower case, digits, `-`), `name`, `method` (`slots` = a camera sees the spaces, `flow` = a camera counts cars in and out, then `capacity` is required).
2. A zone needs a camera that reports on it: add the zone id to an existing camera's `zones` (one occupancy camera can see spaces of two zones; each slot in its slot file names its zone), or add a camera, below.
3. Same `lot.yaml` on both machines; `docker restart parking-api` on the server and the workers on the lot box. The app shows the new zone by itself; its history starts now.

**A camera:**
1. Mount and network it like the others ([hardware.md §6](design/hardware.md#6-mounting-checklist)): a fixed address, its own strong password, no internet access.
2. Its URL goes into the lot box's `.env` under a new name (`CAM_<NAME>_RTSP_URL=…`), never into `lot.yaml` itself.
3. Add it under `cameras:` in `lot.yaml` on both machines: copy the block of a camera with the same `role`, change `id`, `zones`, `source` (`rtsp:${CAM_<NAME>_RTSP_URL}`), `slots_file` / `lines_file` and `control_url` (`http://10.77.0.2:<the next free port, 9002>`).
4. A worker for it: in `docker-compose.site.yml` copy the `vision-occupancy` (or `vision-flow`) service, give it a new service name and `container_name: parking-vision-<name>`, `command: ["occupancy", "--camera", "<id>"]` and `ports: ["${VPN_BIND_IP}:9002:9000"]`. The file is replaced by the next update, so make the same change in the repository (also in `docker-compose.yml`) and release it.
5. `$DC up -d` on the lot box, `docker restart parking-api` on the server. Check: `$P grab --camera <id> --out data/reference/<id>.jpg`, then *Admin* shows the camera `ok`.
6. Draw its spaces or lines: *Admin* → the camera → *Edit parking spaces* / *Edit counting lines* → *Save* (copy the file to the lot box), then *Save reference frame*. Count by hand once and compare.
7. Each worker needs about 1 CPU core and 1.2 GB of memory (`VISION_*` / `FLOW_*` in `.env`): watch `docker stats --no-stream` and the *CPU is hot* alert for a day ([hardware.md](design/hardware.md)).

## Rebuild the dev environment

For the dev Pi after a reinstall, or any other development machine (Linux or macOS, x86-64 or ARM64). It never touches production, and on the Pi nothing outside the repository's folder and its own `parking*` containers ([deployment.md §1](design/deployment.md#1-development-on-the-raspberry-pi)).

```bash
# 1. tools: uv, Node.js 22, Docker with the compose plugin, gh (README → Prerequisites)
git clone https://github.com/iulian-redinciuc/parking-app.git ~/workspace/parking-app && cd ~/workspace/parking-app
uv tool install pre-commit && pre-commit install          # gitleaks + ruff before every commit

# 2. backend and frontend, with their checks
(cd backend && uv sync --extra vision && uv run pytest -m "not slow" -q && uv run ruff check .)
(cd frontend && npm ci && npm run lint && npm test -- --run)

# 3. the private files git doesn't hold: copy them from the old machine or your own backup
#    data/samples/ground-01.jpg   the sample photo            data/labels/cam-ground.json   its labels
#    deploy/.env                  or make a new one, step 5   models/                       or export again, step 4

# 4. models (only for the detector methods; the dev config's `appearance` method needs none)
(cd backend && uv run parking models export --model yolo11n-seg --imgsz 1280 && uv run parking models export --model yolo11n --imgsz 640)

# 5. the stack: a simulated camera feed from the one photo, then the API and the occupancy worker
(cd backend && uv run parking simulate-feed --camera cam-ground --base data/samples/ground-01.jpg --frames 200 --seed 1 --day)
cd deploy
[ -f .env ] || { cp .env.example .env && chmod 600 .env \
  && sed -i "s/^WORKER_TOKEN=.*/WORKER_TOKEN=$(openssl rand -hex 32)/; s/^DOCKER_GID=.*/DOCKER_GID=$(getent group docker | cut -d: -f3)/" .env; }
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
docker compose ps && curl -s localhost:8000/api/status | head -c 300
```

| What | How |
|------|-----|
| The app against this API | `cd frontend && VITE_API_BASE=http://localhost:8000 npm run dev` (without the variable it runs on mock data) |
| On a phone | `deploy/scripts/dev-public.sh up` (a Cloudflare quick tunnel in its own container, and the GitHub Pages preview pointed at it; needs `gh` signed in); `down` afterwards. The address changes at every start: run `up` again after a reboot |
| Push and the admin screen | In `deploy/.env`: the three `VAPID_*` lines from `uv run parking push vapid-keys`, `ADMIN_PASSWORD_HASH` from `uv run parking admin hash-password`, `ADMIN_TOKEN` from `openssl rand -hex 32`; then `up -d` again. New keys, never production's |
| The agent loop | [tools/agent-loop](../tools/agent-loop/README.md) |
| Remove everything | `cd deploy && docker compose --profile quick --profile flow down -v --rmi local`, then delete the folder |

| Symptom | Fix |
|---------|-----|
| `set DOCKER_GID in .env` | `getent group docker \| cut -d: -f3` into `DOCKER_GID` |
| The worker is `unhealthy`, its log says `no images in data/replay/ground-sim` | Step 5's `simulate-feed` wasn't run (the frames are git-ignored) |
| `simulate-feed` can't find the photo or its labels | Step 3: they are private and not in the repository. Any photo of the lot works after drawing slots and labels for it in [tools/slot-editor](../tools/slot-editor/README.md) |
| Port 8000 is taken | `API_HOST_PORT` in `.env` |
| `Permission denied` in `data/` from a container | The containers run as uid 1000: the folder must belong to that user (`id -u`) |

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

## Staging week and go-live

Once, before the address is shared ([phase guide P8.13](phases/phase-8-hardening.md#p813-staging-run-and-go-live)), and again in short form (two or three days) after a change to a camera, its position or the counting. Production runs for 7 days with only you and a few testers.

**Day 0.** Everything in P8.2–P8.11 is done on these machines. Start the sampler on both machines (commands in the phase guide), count the underground level and correct the app to it (admin page), write the first drift note. Note the version: `grep '^PARKING_VERSION=' /opt/parking/deploy/.env`.

**Every day**, once or twice, at different times of day over the week (a busy hour, dusk, night, rain if there is any):

1. Look at the ground level and at the app at the same moment: free spaces, and on the admin page the camera's picture with the spaces it marks taken.
2. Count the underground level and write the drift note (`$P drift-note --zone underground --true <counted> --api http://10.77.0.1:8000`). **No corrections** during the week.
3. `python3 soak.py report soak.jsonl --days 1` on each machine: read the outages (the verdict complains about the span until day 7), and look at the admin page's alerts.
4. One line in PROGRESS.md → Metrics: date, ground app/real, underground app/real, outages, what the testers reported.

A day is **clean** when all of these hold:

| | Clean | Otherwise |
|---|-------|-----------|
| Ground level | The app's free count is the real one at every look, or off by one space while a car is parking or leaving | [Counts are wrong](#counts-are-wrong-occupancy-camera) |
| Underground level | The error moved by at most 2 cars since the previous day's note | [Counts are drifting](#counts-are-drifting-flow-camera) |
| Outages | None that needed a person: every stale period, restart or unreachable minute in the sampler's report ended by itself | [The app can't reach the server](#the-app-shows-cant-reach-the-parking-server), [A zone is stale](#a-zone-is-stale--a-camera-is-down) |
| Alerts | Every alert that arrived had a real cause, and every real fault raised one | [Alerts](#alerts) |
| Testers | Nothing reported that made the app wrong or unusable (wrong language text or a layout slip is fixed, but doesn't spoil the day) | |

**A day that isn't clean:** fix it, release a new version ([Deploy](#deploy-a-new-version--roll-back)), and the 7 days start again from the day the fix is running; so does the drift test if the fix touched the flow counting or a correction was needed. A fix that changes neither the counting nor how the machines recover (a text, a layout) doesn't restart the week.

**Day 7.** `python3 soak.py report soak.jsonl` on both machines and `$P drift-report --zone underground` on the lot box all say `verdict: PASSED`, and the seven lines in Metrics are clean.

**Go-live**, in this order:

| | Check |
|---|-------|
| 1 | The last version deployed is the one that ran the clean days (or differs only by fixes that didn't restart the week) |
| 2 | [Backups](#backups): last night's copy is at the off-machine destination; the restore drill was done on this server |
| 3 | The uptime monitor is green and its test alert reached you ([Alerts](#alerts)) |
| 4 | [Security check](#security-check) and [Privacy check](#privacy-check) pass; the Privacy screen names the operator and the contact |
| 5 | The signs are up at the lot's entrances ([security-privacy.md §4.1](design/security-privacy.md#41-privacy-deliverables-p89)) **before** the address is shared |
| 6 | Turn the sampler off (`pkill -f 'soak.py sample'` on both machines); anything switched on for the week (debug capture, a longer retention, `LOG_LEVEL=DEBUG`) goes back to normal |
| 7 | Share `https://<PUBLIC_HOST>` (a QR code on the sign works well); tick P8.13 and the exit criteria |

The preview (GitHub Pages, on the dev API or mock data) stays as it is: future changes are tried there first, and reach production only through a release tag.
