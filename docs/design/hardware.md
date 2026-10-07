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
- These stay in `data/samples/` on the Pi and are **never committed** (the repo is public).

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
- Put cameras on a **separate VLAN** with no internet access. Only the Pi (or the edge box) may reach them.
- Give cameras **static IPs** (or DHCP reservations). Change default passwords; disable cloud/P2P features; update firmware.
- Test from the Pi: `ffprobe -rtsp_transport tcp "rtsp://user:pass@IP:554/..."` and `curl -o test.jpg "http://user:pass@IP/snapshot..."`.

## 4. Raspberry Pi 5 accessories
| Item | Why | Needed? |
|------|-----|---------|
| **Active cooler** | Sustained inference throttles a bare Pi 5 | Yes |
| Official 27 W USB-C PSU | Stability under load | Yes |
| NVMe (already have) | DB, recordings | ✅ |
| **AI HAT+** (Hailo-8L 13 TOPS or Hailo-8 26 TOPS) | Only if the flow camera can't keep ≥ 8 fps on the CPU | Decide in Phase 5 |
| UPS / power bank with pass-through | Survive short power cuts | Phase 8, optional |

## 5. Edge box bill of materials (only if the lot isn't on the Pi's network)
- Raspberry Pi 5 (4–8 GB), active cooler, PSU, 128 GB+ NVMe or high-endurance SD.
- PoE switch, weatherproof enclosure if outdoors.
- 4G/5G router with a data SIM if there's no wired internet (vision results only need a few MB per day; remote admin snapshots add a little).
- Optional: UPS.

## 6. Mounting checklist
- [ ] Camera positions agree with the layout option (A/B/C) chosen in PLAN.md.
- [ ] Occupancy view covers every ground space, with no space hidden behind a pillar or tree.
- [ ] Flow view covers the whole lane, side-on, with space before and after the lines.
- [ ] Mounts are rigid (a camera that sways in wind triggers shift detection).
- [ ] Cable runs protected. Drip loops at the camera.
- [ ] CCTV signage installed (see [security-privacy.md](security-privacy.md#4-privacy-and-gdpr-checklist)).
