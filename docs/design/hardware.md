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
- Test from the vision host: `ffprobe -rtsp_transport tcp "rtsp://user:pass@IP:554/..."` and `curl -o test.jpg "http://user:pass@IP/snapshot..."`.

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
Topology **T2** ([deployment.md §3](deployment.md#3-production-topologies-to-be-chosen)): a Raspberry Pi 5 at the lot runs the vision workers; the API runs on a small cloud VM (ordered in P8.2, not now). Any equivalent model with the same must-haves (§2) is fine.

| Item | Choice | Why |
|------|--------|-----|
| Camera B (occupancy) | **Reolink RLC-811A**: 8 MP (3840×2160), PoE, motorized 5× varifocal 2.7–13.5 mm, RTSP + ONVIF + HTTP snapshot, IR + spotlight, WDR, IP66. Alternative: Hikvision DS-2CD2646G2-IZS (4 MP, 2.8–12 mm motorized, IK10) | The final position isn't surveyed, so a **motorized varifocal** frames both rows from wherever it ends up (the fixed 2.8 mm/4 mm choice of §2 can't be corrected after mounting). 8 MP keeps the farthest car well above 60 px; the worker can still ask for 2560×1440 (P4.2). An HTTP snapshot URL is what the occupancy worker uses (P4.3) |
| Camera B mount | Where the sample photo was taken (open question #4): **≥ 5 m high** on the building wall/window, looking down at **≥ 45°** (near top-down is fine), covering both rows of ground spaces. Wall or pole bracket, no swaying | Phase 1 showed that a steep, near top-down view has no occlusion and the appearance scorer reached 34/34 on it; the YOLO detector needs an angled view, so it's the fallback, not the target |
| Vision host | **Raspberry Pi 5 8 GB** + official Active Cooler + official 27 W USB-C PSU + **M.2 HAT+ with a 256 GB NVMe SSD** (boot from it) + a case that fits both. No AI HAT+ yet | Same CPU type as the dev Pi (no surprises, NCNN numbers from P1.10 apply: occupancy 158 ms, `yolo11n` @ 640 83 ms). The SSD avoids SD-card wear from logs and the flow outbox. The AI HAT+ is decided in P5.10 only if the flow camera needs it |
| PoE switch | **TP-Link TL-SG1005P** (5 ports, 4 × PoE 802.3af/at, 65 W) or any switch with ≥ 4 PoE ports and ≥ 30 W budget | Camera B + Camera A (Phase 5) + uplink + the Pi |
| Cabling | Outdoor-rated Cat6 for each camera run, RJ45 waterproof glands | §6 |
| Internet at the lot | The existing wired line if there is one; otherwise a 4G router with a data SIM | T2 sends only a few MB per day |
| Optional | A small UPS for the Pi, switch and router | §4.4 |

Camera A (flow, Phase 5) can be ordered at the same time to save a trip: a 4 MP PoE fixed-lens camera from the same brand (e.g. Reolink RLC-510A, 4 mm), placed per §2 in P5.1.

### 4.6 Default from 2026-10-10: standalone box (no access to the lot's cameras or network)
The lot's existing cameras and the building's network can't be used. So the default is one self-contained box at a **window overlooking the lot** (like the spot the sample photo was taken from), replacing the wired PoE camera and switch of §4.5 for the ground level:

| Item | Choice | Why |
|------|--------|-----|
| Computer | Raspberry Pi 5 8 GB + Active Cooler + 27 W PSU + SSD (as §4.5) | Runs the vision worker next to the camera; frames never leave it |
| Camera | **Raspberry Pi Camera Module 3 Wide** (12 MP, autofocus) on the Pi, or a 4K USB webcam; behind glass: lens close to the pane, a dark hood against reflections, no IR | No network camera, no PoE, no cabling through the building (`picamera:` / `device:` source, P4.12) |
| Internet | A 4G/5G USB modem or small router with a data SIM | Outbound only, a few MB per day (numbers, not video); nothing touches the building's network |
| Power | One mains socket | |
| First try | An old phone at the window as a temporary camera (an IP-camera app gives `rtsp:`/`snapshot:` over the phone's own hotspot) | Costs nothing; proves the view before buying |

The PoE cameras of §4.5 stay the choice **if** wired access is ever granted. The underground level (Camera A at the ramp) has no window, so it waits for that access, or the app runs **ground level only** at first.

**Permission is still needed** from the lot's owner / building management to film the car park, even from inside (security-privacy.md §4): the points in its favour are that nothing connects to their network, frames are processed in memory and never stored or sent, and only counts leave the box.

## 6. Mounting checklist
- [ ] Camera positions agree with the layout option (A/B/C) chosen in PLAN.md.
- [ ] Occupancy view covers every ground space, with no space hidden behind a pillar or tree.
- [ ] Flow view covers the whole lane, side-on, with space before and after the lines.
- [ ] Mounts are rigid (a camera that sways in wind triggers shift detection).
- [ ] Cable runs protected. Drip loops at the camera.
- [ ] CCTV signage installed (see [security-privacy.md](security-privacy.md#4-privacy-and-gdpr-checklist)).
