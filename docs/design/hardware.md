# Hardware

## 1. Sample images for Phase 1 (what to send)

| # | Image | Why |
|---|-------|-----|
| 1 | **Normal day**, some spaces free, some taken | The main test |
| 2 | **Busy / full** | Bootstrapping slots, occlusion |
| 3 | **Nearly empty** | Makes sure empty spaces aren't "seen" as taken |
| 4 | **Night** (if possible) | Low light / IR behaviour |
| 5 | **Rain or strong shadows** (if possible) | Robustness |

Requirements:
- All from the **same position and angle** (as if from the final fixed camera).
- **Original file**, full resolution (≥ 1920×1080 ideally). Not a screenshot, not forwarded through WhatsApp/Messenger (they compress heavily).
- Shot from **high up** if you can (window, upper floor, pole), since that's what the camera will see.
- These stay in `data/samples/` on the development machine and are **never committed** (the repo is public).

## 2. Cameras

### Must-haves (both cameras)
- PoE (one cable for power and data), wired Ethernet.
- **RTSP** with a main stream and a **sub-stream**, ONVIF. An HTTP **snapshot** URL is a big plus for the occupancy camera.
- **WDR** (wide dynamic range) for strong sun and shadows, **IR night vision** or good low-light sensitivity.
- IP66/IP67 if outdoors. Vandal-resistant (IK10) if within reach.
- Local access without a cloud account.

### Occupancy camera (Camera B)
- ≥ 4 MP (2560×1440). 4K helps for big lots.
- Field of view chosen to cover **all** ground-level spaces. A 2.8 mm lens is about 100° horizontal; 4 mm is about 80° for a farther, narrower view.
- **Mount high**: ≥ 4–6 m (building wall, pole), looking down. The steeper the view, the less cars hide each other. Aim for at least ~20–30° downward angle to the farthest space.
- Rule of thumb: the **farthest car should be ≥ 60 px wide** in the full-resolution image.

### Flow camera (Camera A)
- 2–4 MP is enough. The sub-stream (640×360 or 704×576) is what gets analysed.
- **Side-on to the lane**, about 3 m high, 30–45° down. Avoid looking straight into headlights.
- The whole lane width visible, plus ~3 m before and after the counting lines, so tracks are established before the crossing.
- Underground: make sure the ramp is lit, or the camera's IR covers it.

### Examples (categories, not endorsements; check current models and prices)
- Consumer/prosumer PoE cameras with RTSP: Reolink, TP-Link VIGI, Hikvision, Dahua/Amcrest.
- **UniFi Protect** cameras only make sense if you have a Protect console (UDM Pro / UNVR / Cloud Key Gen2+). Protect can expose RTSP streams. A UniFi *Network* controller alone doesn't record cameras.

## 3. Network
- PoE switch (802.3af/at), budget ≥ 15 W per camera with IR.
- Put cameras on a **separate VLAN** with no internet access. Only the vision host may reach them.
- Give cameras **static IPs** (or DHCP reservations). Change default passwords; disable cloud/P2P features; update firmware.
- Test from the vision host: `ffprobe -rtsp_transport tcp "rtsp://user:pass@IP:554/..."` and `curl -o test.jpg "http://user:pass@IP/snapshot..."`. `deploy/scripts/camera-check.sh` does both from the host and from inside the vision container for the URLs in `deploy/.env` (P4.2).
- Rehearsed on the dev Pi with `deploy/docker-compose.fake-camera.yml`: an image feed as a camera with a login on an `internal` Docker network, which has no route to the internet and which only the occupancy worker joins.
- The standalone box (§4.6) has no camera network: the camera is on the box, and the box's 4G link is outbound only (nothing forwarded to it).

## 4. Compute hardware

### 4.1 Development machine: Raspberry Pi 5 (already have)
Used only to build and test. Recommended: an **active cooler**, because long benchmark and evaluation runs throttle a bare Pi 5. Nothing else is needed for development.

### 4.2 Production vision host (at the lot; topologies T1/T2)
Chosen in P4.1, once the topology is decided ([deployment.md §3](deployment.md#3-production-topologies-to-be-chosen)). The default is **another Raspberry Pi 5** (the same kind as the dev Pi). The other rows are fallbacks only if a Pi turns out too slow in P5.10:

| Option | AI runtime | Notes |
|--------|-----------|-------|
| Raspberry Pi 5 (8 GB) + active cooler, optionally **AI HAT+** (Hailo-8L 13 TOPS / Hailo-8 26 TOPS) | NCNN (CPU) / Hailo | Same CPU type as the dev Pi, so the fewest surprises. Add the AI HAT+ if the flow camera needs more speed |
| **Intel N100/N150 mini PC** (8–16 GB RAM, SSD) | OpenVINO | x86, usually faster than a Pi on CPU alone; fanless models exist |
| **NVIDIA Jetson Orin Nano** | CUDA / TensorRT | Most AI headroom (several cameras, higher fps); needs the CUDA image variant |

Pick by measuring: record clips from the real cameras (P5.2), then run `parking benchmark` and `parking evaluate-flow` on the candidate machine before buying more than one.

### 4.3 Production API server (topologies T2/T3)
- **T2:** a small cloud VM or any always-on server: 1–2 vCPU, 1–2 GB RAM, ~20 GB disk, Linux with Docker. The API doesn't run AI, so it needs very little.
- **Chosen in P8.2 (to order):** a small shared-CPU cloud VM in an EU region, **2 vCPU / 2–4 GB RAM / ≥ 20 GB SSD, a public IPv4 address, Debian 12** (e.g. Hetzner Cloud's smallest shared instance, CX22 class; DigitalOcean, OVH or Scaleway equivalents are fine; x86-64 or ARM64, the images exist for both). Check the current price (a few euros a month). Set up with `deploy/scripts/provision.sh server` ([deployment.md §10](deployment.md#10-provisioning-the-production-machines-t2)).
- **T3:** a VM big enough to also run vision for every camera (benchmark first), or one with a GPU.

### 4.4 Site extras (T1/T2)
- PoE switch (see §3), a weatherproof enclosure if outdoors.
- A 4G/5G router with a data SIM if there's no wired internet. T2 needs only a few MB per day plus admin snapshots; **T1** also serves all public app traffic from the lot.
- Optional: a UPS for the vision host, switch and router.

### 4.5 Chosen in P4.1 (to order)
Topology **T2** ([deployment.md §3](deployment.md#3-production-topologies-to-be-chosen)): a Raspberry Pi 5 at the lot runs the vision workers; the API runs on a small cloud VM (ordered in P8.2, not now). Since 2026-10-10 the lot's cameras and network can't be used, so the ground camera is the **standalone box at a window** of §4.6; the PoE camera below is kept for the day wired access is granted. Any equivalent model with the same must-haves is fine.

| Item | Choice | Why |
|------|--------|-----|
| Vision host | **Raspberry Pi 5 8 GB** + official Active Cooler + official 27 W USB-C PSU + **M.2 HAT+ with a 256 GB NVMe SSD** (boot from it) + a case that fits both. No AI HAT+ yet | Same CPU type as the dev Pi, so the dev Pi's numbers apply (P1.10: occupancy 158 ms, `yolo11n` @ 640 83 ms; P4.1: 0.63 s for a 12 MP frame). The SSD avoids SD-card wear from logs and the flow outbox. The AI HAT+ is decided in P5.10 only if the flow camera needs it |
| Camera B (occupancy, ground) | **Raspberry Pi Camera Module 3 Wide** (Sony IMX708, 12 MP 4608×2592, 102° × 67°, autofocus, fixed at infinity with `focus=0`) on the Pi's CSI port, 300 mm ribbon cable. **Module 3 (standard, 66° × 41°)** instead if the farthest space is more than ~35 m away. Fallback: a 4K USB webcam (`device:`) | No network camera, PoE or cabling through the building (§4.6). The wide lens covers both rows from close up; 12 MP keeps the farthest space far above the size the scorer needs (sizing below). `picamera:` source (P4.12) |
| Lens and mount | At the window the sample photo was taken from (open question #4): **≥ 5 m above the lot** (2nd floor or higher), looking down at **≥ 45°** (near top-down is fine) over both rows; lens close to the pane with a dark hood against reflections; no IR (it reflects off the glass); a rigid bracket or a stand fixed to the sill, not a suction mount | Phase 1 showed that a steep, near top-down view has no occlusion and the appearance scorer reached 34/34 on it; the YOLO detector needs an angled view, so it's the fallback, not the target. A camera that moves triggers shift detection (P4.6) |
| Internet at the lot | A 4G/5G USB modem or small router with a data SIM (§4.6) | T2 sends only a few MB per day |
| Optional | A small UPS for the Pi and the modem | §4.4 |
| If wired access is granted | **Reolink RLC-811A** (8 MP, PoE, motorized 2.7–13.5 mm varifocal, RTSP + ONVIF + HTTP snapshot, IR, WDR, IP66; alternative Hikvision DS-2CD2646G2-IZS) + **TP-Link TL-SG1005P** PoE switch (≥ 30 W budget) + outdoor Cat6 with RJ45 glands; Camera A (Phase 5) a 4 MP PoE fixed-lens camera such as the Reolink RLC-510A, 4 mm | A motorized varifocal frames both rows from wherever it ends up; an HTTP snapshot is what the occupancy worker prefers (P4.3) |

**Sizing the resolution (P4.1 PoC check on images).** The appearance scorer was run on the whole replay feed (284 frames, 4828 spaces: simulated, real and generated) shrunk step by step: it stays at **100% down to 1/16 of the sample's size**, where a space's short side is **13–30 px**, and the first misses appear at 1/25 (8–19 px: 99.6%). Shrinking a clean photo is kinder than a real lens behind glass with noise and compression, so the target for the real camera keeps the §2 rule of thumb, **≥ 60 px across the farthest space**, about 4× that margin. For a space 2.5 m wide at distance *d* the Module 3 Wide gives about 4608 × 2.5 / (2 · tan 51° · *d*) ≈ **4660 / *d* px** in the middle of the frame (the standard Module 3: ≈ 8860 / *d*), and roughly half that at the edges of the wide lens: ≥ 60 px up to ~35 m from the window (~70 m with the standard module). At 12 MP the dev Pi (the vision host's hardware) needs **0.63 s per frame** (93 ms JPEG decode + 536 ms scoring, 17 spaces), about 13% of one core at one frame every 5 s; `?width=2304&height=1296` (the sensor's binned mode) is a quarter of that if needed.

### 4.6 Default from 2026-10-10: standalone box (no access to the lot's cameras or network)
The lot's existing cameras and the building's network can't be used. So the default is one self-contained box at a **window overlooking the lot** (like the spot the sample photo was taken from), replacing a wired PoE camera and switch for the ground level (the choice of P4.1, §4.5):

| Item | Choice | Why |
|------|--------|-----|
| Computer | Raspberry Pi 5 8 GB + Active Cooler + 27 W PSU + SSD (§4.5) | Runs the vision worker next to the camera; frames never leave it |
| Camera | **Raspberry Pi Camera Module 3 Wide** (12 MP, autofocus) on the Pi, or a 4K USB webcam; behind glass: lens close to the pane, a dark hood against reflections, no IR | No network camera, no PoE, no cabling through the building (`picamera:` / `device:` source, P4.12) |
| Internet | A 4G/5G USB modem or small router with a data SIM | Outbound only, a few MB per day (numbers, not video); nothing touches the building's network |
| Power | One mains socket | |
| First try | An old phone at the window as a temporary camera (an IP-camera app gives `rtsp:`/`snapshot:` over the phone's own hotspot) | Costs nothing; proves the view before buying |

**How the camera is read** (P4.12; formats in [config.md → Source URI formats](config.md#source-uri-formats), containers in [deployment.md §4.3](deployment.md#43-a-camera-plugged-into-the-vision-host-p412)):

| Camera | `source` in `lot.yaml` | Notes | Tested |
|--------|------------------------|-------|--------|
| Raspberry Pi Camera Module 3 / 3 Wide (CSI ribbon cable) | `picamera:0?width=4608&height=2592&focus=0` | Full 12 MP at 2 frames a second (the default) through `rpicam-vid`. `focus=0` fixes the lens at infinity: behind glass, autofocus tends to lock onto the pane. Other Pi modules (HQ, v2, Module 3 NoIR) work the same way at their own resolutions; without autofocus, leave `focus` out | Not yet (no module on the dev Pi) |
| 4K USB webcam (UVC) | `device:/dev/video0?width=3840&height=2160&fourcc=MJPG&fps=1` | `MJPG` is needed for 4K over USB; `fps=1` decodes one frame a second and throws the rest away undecoded. List what a camera offers with `v4l2-ctl -d /dev/video0 --list-formats-ext`; turn its autofocus off there too if it hunts (`v4l2-ctl -c focus_automatic_continuous=0 -c focus_absolute=0`) | Not yet (no webcam on the dev Pi) |
| Old phone with an IP-camera app (first try) | `snapshot:http://…` or `rtsp:rtsp://…` | Already supported (P4.3) | With a local HTTP server and a mocked stream only |

First check on the real box, before anything else: `parking grab --camera cam-ground --out out/grab.jpg` (it prints the frame's size, or why there is none: device missing, no permission, busy, no camera detected), then look at the picture: every space visible, in focus, no reflection of the room. Update the *Tested* column with the model and the resolution that worked.

The PoE cameras at the end of §4.5 stay the choice **if** wired access is ever granted. The underground level (Camera A at the ramp) has no window, so it waits for that access, or the app runs **ground level only** at first.

**Permission is still needed** from the lot's owner / building management to film the car park, even from inside (security-privacy.md §4): the points in its favour are that nothing connects to their network, frames are processed in memory and never stored or sent, and only counts leave the box.

### 4.7 Barrier contacts (P9.3)

Only where the lot has a barrier or loop detectors, and its owner allows a connection ([barrier.md](barrier.md)). Nothing to buy for the app itself beyond wire and, if needed, an isolator.

- **What to ask the barrier's installer for:** potential-free (dry) relay contacts that close once per vehicle: one for the entry lane and one for the exit lane (`pulse` mode), or the presence outputs of the two loops of a single lane (`pair` mode). Most controllers and two-channel loop detectors have spare relay outputs for this.
- **Wiring:** each contact between a GPIO pin of the vision box and a GND pin of the same header; the pin's internal pull-up does the rest (`active=low`, the default). Example: entry contact on GPIO17 (pin 11) and GND (pin 9), exit contact on GPIO27 (pin 13) and GND (pin 14) → `source: "gpio:/dev/gpiochip0?in=17&out=27"`.
- **Never connect a voltage to the header.** The pins take 3.3 V at most; a barrier's 12/24 V signal destroys the Pi. An output that carries a voltage (or a cable longer than a few metres, or one that runs next to the barrier's motor) goes through an optocoupler module or a small relay, whose output side is again a dry contact.
- Twisted pair or shielded cable; keep the contacts' cable away from mains. A stuck or chattering relay is handled in software (`debounce_ms`, `min_gap_ms`).
- Don't use GPIO pins that a HAT on the box already uses (`gpioinfo` shows the lines in use; the worker also says so when a line is busy).

## 6. Mounting checklist
- [ ] Camera positions agree with the layout option (A/B/C) chosen in PLAN.md.
- [ ] Occupancy view covers every ground space, with no space hidden behind a pillar or tree.
- [ ] Flow view covers the whole lane, side-on, with space before and after the lines.
- [ ] Mounts are rigid (a camera that sways in wind triggers shift detection).
- [ ] Cable runs protected. Drip loops at the camera.
- [ ] CCTV signage installed (see [security-privacy.md](security-privacy.md#4-privacy-and-gdpr-checklist)).
