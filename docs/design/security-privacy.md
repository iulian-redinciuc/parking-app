# Security and privacy

## 1. What we protect

| Asset | Why it matters |
|-------|----------------|
| Camera streams | Show people and licence plates |
| Admin access | Could falsify counts or reconfigure cameras |
| Count integrity | Wrong numbers send people to a full lot |
| Push subscriptions | Could be abused to spam users |
| Secrets (`.env`) | Camera passwords, VAPID private key, tunnel token |
| The dev Pi | It also runs other software, which this app must never touch ([deployment.md §1](deployment.md#1-development-on-the-raspberry-pi)) |
| Production machines | Public-facing; must stay patched and minimal |

## 2. Threats and controls

| Threat | Control |
|--------|---------|
| Fake counts posted to the API | `/internal/*` requires `WORKER_TOKEN` (constant-time check) **and** is not forwarded by the tunnel; workers and API share a private Docker network ([deployment.md §5](deployment.md#5-public-access-for-the-api)) |
| Someone on the internet reaches cameras | Cameras on their own VLAN with no internet. No port forwards. Only the vision workers can reach them |
| Admin brute force | Argon2 password hash, 5 attempts / 15 min / IP, session tokens expire after 7 days, `ADMIN_TOKEN` ≥ 32 random bytes |
| Token theft via XSS | React escapes output by default. No `dangerouslySetInnerHTML`. Strict CSP `<meta>` in the built `index.html` (`default-src 'self'; connect-src 'self' <API>; img-src 'self' data: blob: <API>; object-src 'none'; base-uri 'self'; form-action 'self'`; written by `vite.config.ts` from `VITE_API_BASE`, so no inline script or style, no `eval`, nothing from another origin; not in `vite dev`). The production proxy adds the header `Content-Security-Policy: frame-ancestors 'none'`, which a `<meta>` can't carry. Admin token kept in `sessionStorage`, not `localStorage` |
| CSRF | Bearer tokens instead of cookies, so there's nothing for a browser to send automatically |
| API abuse / DoS | Rate limits (`limits`, per IP; [api.md §7](api.md#7-cross-cutting)), Cloudflare in front (Option A), SSE queues bounded per client, a cap on concurrent SSE clients (e.g. 1000) |
| Push spam through our API | Rate limits on subscribe/test. Pushes only ever go to subscriptions the browser created. Content is generated server-side only |
| Secrets leaked to the public repo | `.env`, `data/`, `models/` in `.gitignore`. **gitleaks** pre-commit hook + CI job (both use `.gitleaks.toml`: default rules plus a strict AWS key-ID rule that also catches `…EXAMPLE` keys). GitHub secret scanning and push protection switched on |
| Compromised dependency | Dependabot (pip, npm, actions weekly; docker added with the Dockerfiles in P2.10) and its alerts on the repository. Tools that only CI runs and that drag in advisories stay out of the lockfile (Lighthouse CI runs through `npx` at a fixed version). Lockfiles (`uv.lock`, `package-lock.json`). Images pinned to major versions |
| A compromise spreads to other software on the same machine | Containers run as non-root with a read-only root filesystem, no capabilities and `no-new-privileges` ([deployment.md §4.1](deployment.md#41-hardening-p84)), internal networks, read-only mounts where possible, resource limits. Only the API is exposed (via the public entry). Dev and production use **different secrets** |
| The watchdog's Docker socket (root on the host for whoever can use it) | Only `parking-autoheal` mounts it: no network at all, read-only, no capabilities, non-root with the socket's group, exact image version, and it only restarts containers labelled `parking.autoheal=true` ([deployment.md §4.2](deployment.md#42-watchdog-for-stuck-workers-p85)) |

### 2.1 Checking the controls (P8.8)

`deploy/scripts/security-check.sh` checks a deployment against the table above; it only reads (the login check posts wrong passwords) and exits 1 when a check fails. Commands: [phase guide P8.8](../phases/phase-8-hardening.md#p88-security-review).

| Mode | Run it | What it checks |
|------|--------|----------------|
| `outside https://<PUBLIC_HOST> [address …]` | From a machine outside (a laptop, the dev Pi) | `/internal/*`, `/control/*`, `/docs`, `/openapi.json` are 404 through the public entry; `Origin: https://evil.example` gets no `Access-Control-Allow-Origin` (GET and preflight); the page has the CSP `<meta>`; HSTS, `nosniff` and `frame-ancestors` headers; `http://` redirects; admin routes are 401 without / with a wrong token; with `ADMIN_PASSWORD` set: login, the token expires in 7 days, logout revokes it; the 6th wrong password is 429 (locks that IP out for 15 min; `SKIP_LOGIN_LIMIT=1`); `nmap -p-` of the host and of each extra address finds only `ALLOWED_PORTS` (22, 80, 443) |
| `server` / `site` | On the machine, in `deploy/` | `.env` is mode 600 and yours; `WORKER_TOKEN`, `ADMIN_TOKEN`, `ADMIN_PASSWORD_HASH`, `VAPID_PRIVATE_KEY` (site: `WORKER_TOKEN`) are set and at least 32 random bytes long; `LOG_LEVEL` isn't `DEBUG`, `DEBUG_CAPTURE` is off; `CORS_ORIGINS` is exactly `https://<PUBLIC_HOST>`; `/internal/*` (server) and `/control/*` (site) answer 401 without the worker token; no container of the project publishes a port on every interface except `parking-web`'s 80/443 |
| `fingerprints` | On the dev machine | Prints the sha256 of each secret in `.env`. Copy the output to the production machine and pass it as `DEV_FINGERPRINTS=<file>`: `server` / `site` then fail on any secret that is the dev one |
| `repo` | In a clone, with `gitleaks` and `gh` | `gitleaks detect --log-opts=--all` (the full history) is clean; no open high/critical Dependabot alert |

Not scripted: that the cameras can't be reached from outside their VLAN (a `nc -z <camera> 554` from another network segment, P4.2).

## 3. Public-repo rules

The repo `iulian-redinciuc/parking-app` is **public**. Before every commit:

| Never commit | Put it in |
|--------------|-----------|
| Camera images, video, screenshots of the camera view | `data/` (git-ignored) |
| Ground-truth labels tied to real images | `data/labels/` |
| Camera URLs, usernames, passwords, IPs | `deploy/.env` |
| VAPID private key, admin hash/token, worker token, tunnel token | `deploy/.env` |
| Exact lot coordinates (if you consider them private) | `deploy/.env` (`LOT_LAT`/`LOT_LON`) |
| Model weights | `models/` (git-ignored; reproducible via `parking models export`) |

Allowed: slot/line JSON (just pixel coordinates), synthetic test images, config without secrets.

If something slips through: **rotate the secret first**, then remove it from history (`git filter-repo`) and force-push. Assume anything pushed was copied.

## 4. Privacy and GDPR checklist

If the lot is in the EU/EEA (or the UK), filming people and vehicles is personal-data processing under GDPR. This is a checklist, not legal advice. Check your national data-protection authority's CCTV guidance.

- [ ] **Purpose** written down: "counting free parking spaces". Nothing else (no identification, no tracking of individuals over time).
- [ ] **Lawful basis** identified (usually legitimate interest), with a short balancing note.
- [ ] **Data minimisation:** frames processed in memory and discarded. Only counts and slot states stored. Track IDs are ephemeral and never linked to identities. No licence-plate recognition.
- [ ] **No public video:** the public API returns numbers only. Snapshots are admin-only.
- [ ] **Debug captures** off by default (`DEBUG_CAPTURE=false`). When on, full frames + observation JSON go to `data/debug/<camera>/<date>/` on the vision host only (never the API, never the repo) and are auto-deleted after `DEBUG_RETENTION_HOURS` (24 h; the pruning also runs with capture off). Turn it on only while debugging or collecting the validation set, and back off afterwards. Datasets kept longer get faces and plates blurred.
- [ ] **Signage** at the lot: who operates the cameras, purpose, contact.
- [ ] **Camera views** don't cover neighbouring private property, windows or public streets beyond what's necessary (use privacy masks in the camera settings).
- [ ] **DPIA** considered: likely needed for systematic monitoring of a publicly accessible area at scale. For a small private lot, document why it isn't.
- [ ] **App users:** location is processed on the device only. Push subscriptions are pseudonymous (no names or emails). Privacy page in the app explains this (§4.1).
- [ ] **Retention** periods ([data-model.md §4](data-model.md#4-retention-and-rollups-apscheduler-jobs-in-the-api)) implemented and verified (`parking db retention`, §4.1).
- [ ] **Access:** only you (admin) can see camera images. Document who has admin.

### 4.1 Privacy deliverables (P8.9)

| Deliverable | Where | State |
|-------------|-------|-------|
| Privacy screen | `#/privacy` in the app ([frontend.md §2.5](frontend.md#25-privacy-privacy-phase-8)), linked from the footer of every screen and from the Alerts screen. The operator's name and contact come from `PRIVACY_OPERATOR` / `PRIVACY_CONTACT` in the API's `.env` | Built; the two values are set on the production server |
| Retention check | `parking db retention` ([data-model.md §4](data-model.md#4-retention-and-rollups-apscheduler-jobs-in-the-api)): the oldest row of every table against its period, exit 1 when the prune job isn't deleting. Debug captures: `find data/debug -type f -mmin +$((24*60+10))` on the vision host prints nothing (with the default `DEBUG_RETENTION_HOURS=24`) | Built; run on production after it has been up for more than 90 days, and at each quarterly security check ([runbook](../runbook.md#privacy-check)) |
| Signage | The text below, printed and put up at each entrance, before the cameras are switched on | The operator does it |
| Purpose, lawful basis with the balancing note, DPIA decision, who has admin | The operator's own notes, **kept private, not in this repo**. The headings to fill in: purpose; lawful basis and why the interest outweighs the intrusion (what was done to keep it small: this section's checklist); whether a DPIA is needed and why (not); camera positions and what each sees, privacy masks; who has admin access; where the data is (the lot box, the server and its hosting provider with its region, the backup storage) and the retention periods; how a request from a person is answered | The operator does it |

**What the system really keeps** (the Privacy screen says the same; change both together):

| Data | Where | Kept |
|------|-------|------|
| Camera frames | Memory of the vision host | Not kept. Never sent to the API (T1/T2) |
| Reference picture, one per camera (shift detection) | `data/reference/` on the vision host (saved there by the worker, also when asked from the admin page); in the backups only when the API runs on the same machine (T1) | Until replaced |
| Admin snapshot (the current frame, admin only) | Fetched live from the worker | Not stored |
| Debug captures (off by default) | `data/debug/` on the vision host | `DEBUG_RETENTION_HOURS` (24 h) |
| Slot and zone states, flow events (a time and a direction, a track number that means nothing outside the worker's run) | Database | 90 days; minute averages 30 days; hourly averages kept |
| Push subscription: the browser's push address and keys, notification settings, time zone, language | Database | Until the device unsubscribes, or the push service reports the address gone (or 5 failed sends in a row) |
| Notification log | Database | 30 days |
| Admin sessions: token hash, IP address, browser name | Database | Until they expire (7 days), deleted by the next prune |
| IP addresses of app users | The API's access log (container log, 3 × 10 MB, overwritten); the rate limiter (memory). The production proxy (Caddy) writes no access log | Days at most |
| Location of app users | The phone only | Never sent |

**T3 only:** if production ever moves to T3, video travels from the cameras to the cloud machine over the encrypted VPN and is analysed there in memory; this table, the screen's *The cameras at the lot* texts and the sign must then say so. The chosen layout is T2, where no picture leaves the lot.

**Sign text** (A4 or larger, at each entrance, in the local language; a camera pictogram on top):

> **Camera in use: counting free parking spaces**
> The cameras count free and taken spaces. Pictures are analysed on site and discarded: no recording, no number-plate reading, no identification of people.
> Operator: *name, address*
> Contact: *e-mail or phone*
> Details and your rights: *https://<PUBLIC_HOST>/#/privacy*
