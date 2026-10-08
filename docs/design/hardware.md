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
- **T3:** a VM big enough to also run vision for every camera (benchmark first), or one with a GPU.

### 4.4 Site extras (T1/T2)
- PoE switch (see §3), a weatherproof enclosure if outdoors.
- A 4G/5G router with a data SIM if there's no wired internet. T2 needs only a few MB per day plus admin snapshots; **T1** also serves all public app traffic from the lot.
- Optional: a UPS for the vision host, switch and router.

## 6. Mounting checklist
- [ ] Camera positions agree with the layout option (A/B/C) chosen in PLAN.md.
- [ ] Occupancy view covers every ground space, with no space hidden behind a pillar or tree.
- [ ] Flow view covers the whole lane, side-on, with space before and after the lines.
- [ ] Mounts are rigid (a camera that sways in wind triggers shift detection).
- [ ] Cable runs protected. Drip loops at the camera.
- [ ] CCTV signage installed (see [security-privacy.md](security-privacy.md#4-privacy-and-gdpr-checklist)).
