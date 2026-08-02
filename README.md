# Insta360 Link — `Insta360WebCamFW.bin` teardown

Target: `Insta360WebCamFW.bin`, 35,741,008 bytes
SHA-256: `e8e7ec61aff0c0b5b273e21971e1d9e62af5ec5c6453e0a0bad7152d5342ebb3`
Product: **Insta360 Link** (gen 1), internal codename `link1`, USB `2E1A:4C01`
Versions: webcam SoC `v1.4.5.8_build1`, gimbal MCU `v1.1.1`, build date 2025-01-20

---

## 1. Container layout

The file is a two-component package with a 200-byte trailer:

```
0x00000000  webcam image     0x021F2FE4   Ambarella multi-partition image
0x021F2FE4  gimbal image     0x00022CA4   Cortex-M app for the gimbal MCU
0x02215C88  manifest         0xC8 (200)
```

Manifest (read from the end of file):

```c
struct entry {              // 84 bytes, count entries, in file order
    uint32_t size;
    char     name[32];      // "webcam", "gimbal"
    char     version[32];   // "v1.4.5.8_build1", "v1.1.1"
    uint8_t  md5[16];       // MD5 of that component's bytes
};
struct trailer {
    struct entry entries[count];
    char     magic[8];      // "WFNIMACW"
    uint64_t count;         // 2
    uint8_t  md5[16];       // MD5 of the whole file except these last 16 bytes
};
```

All three digests were recomputed and match. The `MACW`/`WFNI` halves of the magic
double as the product type word the updater checks (`'MACW'` = WEBCAM,
`'SRNO'` = ONE RS).

### Webcam image (Ambarella)

Three partitions, each preceded by a 0x100-byte header whose first 28 bytes are the
Ambarella `flpart_t` (`magic 0xA324EB90`). All three CRC32s verify.

| # | file off | size | load | contents |
|---|---|---|---|---|
| 0 | 0x000330 | 0x1285AB4 | **0x20000** | ThreadX RTOS application, ARM32 (Ambarella **H22** SoC) |
| 1 | 0x1285EE4 | 0x3AC800 | – | ROMFS: `orccode.bin`, `orcme.bin`, `default_binary.bin` (DSP/ORC microcode) |
| 2 | 0x16327E4 | 0xBC0800 | – | ROMFS: 577 files — NN models, PCM prompts, bitmaps, ISP tuning |

Partition 0 is a single flat image based at `0x20000`; string references are
MOVW/MOVT pairs, not literal pools. Sensor is IMX577 (`imx577*_h22` tuning sets).

### Gimbal image

Cortex-M vector table, **loads at 0x8000** (a 32 KB bootloader occupies 0x0–0x8000 and
is not shipped in this package). MCU identified by the string `INSTA360_GMS586_HC32` —
an HDSC HC32 part; `GMS586` matches the `SENSOR_SERIAL_GMS586` factory option.
595 functions.

---

## 2. Control protocol

### 2.1 USB layout

`2E1A:4C01`, bDeviceClass 0xEF (IAD), 4 interfaces:

| if | class | role |
|---|---|---|
| 0 | 0x0E/0x01 | VideoControl (EP 0x82 interrupt) |
| 1 | 0x0E/0x02 | VideoStreaming — MJPEG + frame-based (H.264), EP 0x81 bulk |
| 2 | 0x01/0x01 | AudioControl |
| 3 | 0x01/0x02 | AudioStreaming, EP 0x83 isoc |

UVC topology: `IT 1 (camera 0x0201) → PU 5 → XU 9 → XU 10 → OT 3`, with `XU 11`
also fed from PU 5.

| Unit | GUID | bmControls | selectors |
|---|---|---|---|
| XU 9 | `{FAF1672D-B71B-4793-8C91-7B1C9B7F95F8}` | `FF FF FF 3F` | 1–30 |
| XU 10 | `{E307E649-4618-A3FF-82FC-2D8B5F216773}` | `3F 00 00 00` | 1–6 (1,2,6 implemented) |
| XU 11 | `{A8BD5DF2-1A98-474E-8DD0-D92672D194FA}` | `1F 00 00 00` | 1–5 |

### 2.2 Standard UVC controls (these work with plain v4l2)

Processing Unit 5 — handler `0x589740`:
brightness(2), contrast(3), power-line-frequency(5), hue(6), saturation(7),
sharpness(8), white-balance-temperature(10), white-balance-temperature-auto(11).

Camera Terminal 1 — handler `0x5894D4`:

| CS | control | maps to |
|---|---|---|
| 6 | `CT_FOCUS_ABSOLUTE` | param 0x0D (manual focus) |
| 8 | `CT_FOCUS_AUTO` | param 0x0C |
| 11 | `CT_ZOOM_ABSOLUTE` | param 0x15 |
| 13 | `CT_PANTILT_ABSOLUTE` | param 0x20 — **value/360**, i.e. arcsec in, 0.1° internally |
| 14 | `CT_PANTILT_RELATIVE` | param 0x21 (direction × speed) |
| 15 | `CT_ROLL_ABSOLUTE` | param 0x20, roll field |

### 2.3 Extension-unit dispatch

Requests are routed by a table of 38 `{unitID, selector, handler}` triples at
`0x011683E0` (dispatcher `FUN_00589A28`). `bRequest`: `0x01` SET_CUR, `0x81` GET_CUR,
`0x85` GET_LEN, `0x82/0x83/0x87` MIN/MAX/DEF.

Handlers translate to an internal parameter space via
`InsApp_Webcam_SetParam(id, buf, arg)` @`0x5848E8` /
`GetParam(id, buf, arg)` @`0x582A8C`.

| XU | Sel | wLen | SET param | GET param | Function |
|---|---|---|---|---|---|
| 9 | 1 | 4 | 0x20,0x0D,0x0C,0x15 | 0x20 | composite state (ptz/focus/af/zoom) |
| 9 | 2 | 0x34 | 0x26,0x27,0x12,0x10 | 0x10,0x11,0x12,0x20,0x15 | **preset get/set** (mode, x, y, z, zoom, whiteboard args) |
| 9 | 3 | 0xAA | – | – | (bulk config blob) |
| 9 | 4 | 0x106 | – | – | (bulk config blob) |
| 9 | 5 | 1 | 0x13 | 0x13 | **gesture enable bitmask** |
| 9 | 6 | 5 | 0x14 | 0x14 | **gesture binding** (5 slots) |
| 9 | 7 | 1 | 0x1E | 0x1E | noise cancellation |
| 9 | 8 | 0x1F6 | – | – | (bulk) |
| 9 | 9 | – | 0x06 | 0x06 | |
| 9 | 10 | 0x81 | – | – | mode string (`uvc`/`photo`/`msc`/`simple`) |
| 9 | 11 | 5 | – | 0x22,0x23 | status readback |
| 9 | 12 | 0x20 | – | – | |
| 9 | 13 | 0x81 | – | – | |
| 9 | 14 | 1 | – | – | |
| 9 | 15 | 0x0C | – | – | |
| 9 | 16 | 0xFF | 0x29 | 0x29 | tone curve |
| 9 | 17 | 1 | – | – | **USB mode switch** — 0=uvc 1=photo 2=msc 3=simple |
| 9 | 18 | 1 | 0x19 | 0x19 | |
| 9 | 19 | 1 | 0x18 | 0x18 | track speed |
| 9 | 20 | 0xF0 | – | 0x16 | |
| 9 | 21 | 8 | 0x17 | – | **set track target (float x, float y)** |
| 9 | 22 | 4 | – | – | |
| 9 | 23 | 0x81 | 0x1F | 0x1F | |
| 9 | 24 | 4 | 0x1A | 0x1A | framing bias (x, y) |
| 9 | 25 | 2 | 0x07,0x21 | 0x07 | **pan/tilt speed** |
| 9 | 26 | 8 | 0x20 | 0x20 | **absolute yaw/pitch/roll** |
| 9 | 27 | 2 | 0x0B,0x0E,0x1C,0x1D,0x28,0x2A,0x2E,0x2F,0x30,0x34 | same | **feature enable multiplexer** |
| 9 | 28 | 0x0A | – | 0x0F | |
| 9 | 29 | 2 | 0x08 | 0x08 | shutter time |
| 9 | 30 | 1 | 0x09 | 0x09 | AE mode |
| 10 | 1 | 8 | – | – | **tracking metadata** |
| 10 | 2 | var | – | – | **tracking raw data stream** |
| 10 | 6 | 1 | 0x35 | 0x35 | AF opt test |
| 11 | 1 | 1 | – | – | |
| 11 | 2 | 1 | 0x10 | 0x10 | **AutoFrame mode** |
| 11 | 3 | 1 | – | – | |
| 11 | 4 | 1 | 0x31 | – | zoom preset — store index |
| 11 | 5 | 1 | 0x32 | – | zoom preset — recall index |

### 2.4 Internal parameter IDs (`SetParam`/`GetParam`)

Recovered from the switch at `0x584924` (SET) and `0x582AB4`/`0x582C14` (GET).
Where a value is persisted, the `INSUTIL_CFG2_*` key is given.

| id | meaning | persisted key |
|---|---|---|
| 0x06 | – | |
| 0x07 | pan/tilt speed | |
| 0x08 | shutter/exposure time | |
| 0x09 | AE mode | |
| 0x0B | video flip | 0x2049 |
| 0x0C | autofocus enable | 0x204A |
| 0x0D | manual focus position | |
| 0x0E | HDR | 0x204B |
| 0x10 | framing mode (0,1,2,4,5,6) | |
| 0x12 | | |
| 0x13 | gesture-group enable bitmask (5 bits) | 0x204C |
| 0x14 | gesture binding table | |
| 0x15 | zoom | |
| 0x17 | track target (x, y floats) | |
| 0x18 | track speed | 0x204D |
| 0x19 | | 0x204E |
| 0x1A | framing bias (int16 x, int16 y; `0x7FFF` = reset) | |
| 0x1C | AI zoom enable | 0x204F |
| 0x1D | algorithm (AI) master enable | 0x2050 |
| 0x1E | audio noise cancellation | 0x2051 |
| 0x20 | **PTZ absolute** — int16 yaw, int16 pitch, int16 roll (0.1° units) | |
| 0x21 | **PTZ velocity** — int16 x_speed, y_speed (×25 before TX) | |
| 0x24 | video rotate | |
| 0x25 | video roll | |
| 0x26 | PTZ preset restore | |
| 0x27 | preset zoom | |
| 0x29 | tone curve (0x200 bytes) | 0x2060/0x2061 |
| 0x2A | vertical (portrait) mode | 0x205F |
| 0x2E | touch-gesture enable | 0x2062 |
| 0x2F | drag enable | 0x2063 |
| 0x30 | stream-on option | 0x2067 |
| 0x31 | store zoom preset (0–5) | 0x2068 (6 × 0x33 B) |
| 0x32 | recall zoom preset (0–5) | |
| 0x33 | upside-down enable | |
| 0x34 | `en_gl_ver` | 0x206A |
| 0x35 | AF opt test | |

Additional per-image IQ parameters (`0x2053`–`0x2061`) are driven by a separate
message task at `0x583878`: local IQ, brightness, contrast, EV, ISO, exposure
time/mode, flicker, manual focus, curve.

### 2.5 The other protocol in the image

The binary also carries Insta360's generic camera protocol, `INSPROTO_UCD_MSG_*`,
implemented with **protobuf-c**. The descriptors are intact, so the complete schema
was recovered: **121 messages and 88 enums**, emitted to
[`insta360.proto`](insta360.proto). This is the shared Insta360 camera codebase
(ONE R / X-series lineage) and covers capture, options, file transfer, BT/WiFi,
factory tests and `PtzCtrlInfo`. On the Link the primary host surface is the UVC XU
path above; the UCD protocol is reachable over the AmbaLink/"simple" USB class and
the factory command set, not over UVC.

---

## 3. Gestures

Pipeline: Insta360 `bva` vision library (MNN inference + OpenCV), sources at
`vendors/Insta360/bva/src/trackerv2/amba/`.

Models loaded at gesture init (from ROMFS partition 2):

- `det_hand_08976b01.INT8.mnn` — hand detector
- `det_head_dc4d29bf.INT8.mnn` — head detector
- `cls_gesture_2M_3ed5b674.FP32.mnn` — gesture classifier (output tensor `probs`)
- `det_person_24M_008109ee.FP32.mnn` — person detector (used by the tracker)

Runtime is split across dedicated ThreadX tasks: `AppGesture task`,
`webcam ges event task`, `webcam person task`, `webcam zoom task`, `webcam ptz task`,
`webcam snap task`, `webcam wboard detect task`, `webcam rc scan task`,
`webcam ai state monitor task`.

The bva entry points are `WebcamGetGestureEvents` (debounced events),
`GetGestureCls` (raw classification), `GetRealTimeHandInfo`, `WebcamGetHeadList`,
`WebcamPersonDetect`, `WebcamTrackerPersonBox`.

**Enable/disable** — XU9 selector 5, one byte, → param 0x13. The byte is a 5-bit mask;
`FUN_00595C8C(group, on)` walks a table at `0xB50D38` mapping group → gesture id(s):

| bit | group | gesture ids |
|---|---|---|
| 0 | 1 | 0x1F |
| 1 | 2 | 0x01 |
| 2 | 3 | 0x15, 0x16, 0x17 |
| 3 | 4 | 0x0B, 0x0C |
| 4 | 5 | (unmapped) |

**Binding** — XU9 selector 6, five bytes, → param 0x14 (`FUN_0058292C`). Five slots;
each byte is 0–2 and is remapped through `{3,1,2,1}` to an action code before
`FUN_00595DD8(gesture_id, action)`.

Recognized actions surface as message-manager events
(`GUI_PROMPT_HIGHTLIGHT_POINT`, `..._FINGER`, `..._SLOWDOWN_START/END`) with audio
cues `finger_point.pcm`, `shutter_9times.pcm`. Gesture-triggered modes include
single-target tracking (`Do Single Track`), zoom (`Entry Desktop`, `zoom_target`),
whiteboard (`Entry White Board`, tag search with luminance-thresholded corner tags)
and DeskView.

---

## 4. Debug functionality over USB

Yes, and quite a lot of it.

1. **Serial console over USB CDC-ACM.** The `AmbaShell` command tree includes
   `t app test usbdbg [start | 2uart | 2usb]`, which re-routes the debug console
   between the physical UART and USB. The CDC-ACM descriptor set is present in the
   image (`4255:0052`, strings `Amba`, `Amba cdcacm class`, `Ambarella UART`), driven
   by `ApplibUsbCdcAcmMulti` — log strings include *"switch to CDC_ACM mode for
   instance %d"* and *"Broken Shell starts."*
2. **USB class switching.** `t app test chg_usbmode [msc|amage|rs232]`, and from the
   host side **XU 9 selector 17** sets the mode directly: `0=uvc, 1=photo, 2=msc,
   3=simple`. Value 4 writes persistent config `0x2064` and then behaves as `msc`.
   That is a host-reachable path into mass-storage mode.
3. **Ambarella iTuner protocol** over the `amage` class (`USB HDLR` module): dump/load
   ISP registers, colour-correction tables, FPN and vignette maps, save raw frames and
   JPEGs, set exposure directly.
4. **Factory command set** — `INSPROTO_UCD_MSG_FACTORY_CMD_*`: `LED_TEST`,
   `MOTOR_TEST`, `GYROSCOPE_TEST`, `USB_SPEED_TEST`, `SCRIPT_JSON_UPLOAD`,
   `SCRIPT_RUN`, `PTZ_CTRL_SET_OPTION`/`GET_OPTION`, plus vignette/BLC/BPC data save.
   Gated on `A:\is_factory_mode` existing.
5. **Log surfaces**: print ring buffer, `A:\factory.log`, `C:\temperature.log`,
   `INSAPP_CMD_READ_CPU_EXCEPTION_LOG`.

The shell itself exposes a large tree (`t app test …`) covering resolution, bitrate,
encode mode, EIS, raw capture, key/jack injection, memory statistics and
`erase_sd0`. Whether the CDC path is enabled in a shipping unit without first
issuing `usbdbg` is the open question — see §7.

---

## 5. Suppressing the activity LED

**Over the documented control surface: no. From modified firmware: yes, trivially.**

The indicator is an RGB LED owned by the **gimbal MCU**, not the SoC. The SoC drives
it with UART command **0x50** carrying a 12-byte payload copied verbatim out of a
16-entry table at `0x01192070` (16 bytes per entry):

```
FUN_00590A44(mode):   InsApp_PTZ_UartSend(0x50, &table[mode*0x10], 0x0C, 0, 0x80)
```

Entry layout is three channels of `{level, brightness, period_u16}` plus a `u16`
duration:

```
mode  0: 00 64 e8 03 | 00 64 e8 03 | 00 64 e8 03 | 0000    <- all channels 0 = OFF
mode  4: 00 64 e8 03 | 00 64 e8 03 | ff 64 e8 03 | 0000
mode  7: ff 32 fa 00 | a5 32 fa 00 | 00 32 fa 00 | 03e8    <- amber, 250 ms blink
mode 12: ff 64 e8 03 | ff 64 e8 03 | ff 64 e8 03 | 0000    <- white
```

Mode selection happens entirely inside `AppPtz`: the LED task `FUN_00590AD4` pops
mode numbers from a message queue fed by `FUN_005909B4`, whose callers are internal
state machines (streaming on/off, charging, firmware update → mode 9, gesture events,
errors). **No XU selector and no `SetParam` id reaches the LED**, so a host cannot
turn it off through UVC.

However there is no hardware interlock at all — the LED is not wired to the sensor or
the encoder, it is a table lookup plus a UART frame. Any of the following in modified
firmware suppresses it completely:

- zero the 16 bytes of whichever table entries are used while streaming, or
- patch `FUN_00590A44` to always index entry 0, or
- stub `FUN_005909B4` so no mode is ever queued.

Mode 0 already exists as a valid "all off" pattern, so no new MCU behaviour is
needed. Given §6, loading such firmware requires no key material.

(Separately, `enter ptz privacy mode` exists and physically parks the gimbal — that is
the shipped privacy mechanism, and it is visible rather than covert.)

---

## 6. PTZ / servo controllability

Two-axis mechanical gimbal (yaw + pitch) with digital roll, driven by the HC32 MCU.
The SoC↔MCU link is a CRC-checked UART frame protocol; the MCU's command dispatcher
(`FUN_000208D0`) accepts opcodes `0x03`–`0x09`, `0x30`–`0x3D`, plus `0x50` (LED) and
`0xF0`–`0xF5` (OTA).

Motion is command **0x38** with a 6-byte payload and a sub-mode byte:

| sub-mode | behaviour |
|---|---|
| 1 | relative — adds the delta to the current target |
| 2 | absolute — sets the target angle, with wrap/limit handling around ±180° |
| 3 | rate — velocity command |

Host-reachable paths, in order of convenience:

1. **Standard UVC pan/tilt.** `CT_PANTILT_ABSOLUTE` (selector 13) and
   `CT_PANTILT_RELATIVE` (14), plus `CT_ROLL_ABSOLUTE` (15) and `CT_ZOOM_ABSOLUTE`
   (11). The absolute value arrives in UVC arcseconds and is divided by 360, giving
   0.1° resolution internally. This means `v4l2-ctl --set-ctrl pan_absolute=…` /
   `tilt_absolute=…` drives the servos with no vendor tooling.
2. **XU 9 selector 26** (and 23), 8 bytes → param 0x20: `int16 yaw, int16 pitch,
   int16 roll` in 0.1° units, sent as sub-mode 2 (absolute).
3. **XU 9 selector 25**, 2 bytes → param 0x21: pan/tilt speed. The RTOS multiplies
   each by 25 before issuing sub-mode 3, and inverts sign when the gimbal orientation
   flag reports inverted mounting.
4. **XU 9 selector 2**, 0x34 bytes: full preset structure — mode, x, y, z, zoom, plus
   nine whiteboard float arguments. `0x0E1A` in a field means "leave unchanged";
   `0x7FFF` is the reset/no-op sentinel used for bias.
5. Six stored zoom presets (`0x25FA2E4`, 6 × 0x33 bytes) via params 0x31/0x32 or
   XU 11 selectors 4/5.

So: absolute positioning, velocity control, roll, zoom and presets are all exposed,
and the mechanically-limited range is enforced by the MCU rather than by the host
interface.

Ranges read from a live unit (§8):

| control | min | max | step | in degrees |
|---|---|---|---|---|
| `pan_absolute` | −522000 | +522000 | 3600 | **±145°**, 1° granularity |
| `tilt_absolute` | −324000 | +360000 | 3600 | **−90° … +100°**, 1° granularity |
| `zoom_absolute` | 100 | 400 | 1 | 1.0× – 4.0× |
| `focus_absolute` | 0 | 100 | 1 | – |

Values are UVC arcseconds; the firmware divides by 360, so the internal resolution is
0.1° even though the descriptor advertises 1° steps. Roll is advertised in the
Camera Terminal `bmControls` (absolute and relative) but `uvcvideo` does not map it
to a v4l2 control — it is reachable via a raw UVC control transfer or XU 9/26.

---

## 7. Anti-tamper

### Update path A — in-band (Link Controller / UCD)

The image is written to `A:\Insta360WebCamFW.bin`, then `AppFWUpdate` runs
`FUN_0059FEE0`:

1. Stream the file through MD5, **excluding the final 16 bytes**.
2. Compare against those final 16 bytes. Mismatch → `"FW File MD5 Check Fail."`
3. Parse the trailer; require the type word to be `'MACW'` (WEBCAM) or `'SRNO'`
   (ONE RS), else `"Project ONERS fw type is not right."`
4. Dispatch per component: `webcam` → SoC flash; `gimbal` → PTZ MCU OTA over UART.

That is the entire authentication. **There is no signature, no public key, no
certificate, and no anti-rollback.** Searching the whole 19 MB image finds no RSA,
ECDSA, SHA-2 or HMAC verification in the update path — only MD5, the Ambarella
per-partition CRC32s, and the ASCII type magic. The image is not encrypted either;
everything above was read in the clear.

Consequence: producing an accepted firmware is a matter of editing bytes, fixing the
three Ambarella CRC32s and the component/whole-file MD5s, and re-appending the
trailer. The `WFNIMACW` trailer format is fully documented in §1.

### Update path B — USB mass-storage boot mode

Entering MSC exposes the internal volume; the host drops the same
`Insta360WebCamFW.bin` on it and the device runs **the identical `AppFWUpdate` code**
on the next boot. There is no additional or stronger check on this path — the
`upgrade from merge package` / `upgrade from standalone package` logic in
`FUN_005A0204` is shared. MSC mode is reachable both by the button combination and,
notably, from the host at runtime via **XU 9 selector 17** (`uvc set usb mode: 2`),
which needs nothing more than the ability to issue a UVC extension-unit SET_CUR.

### Gimbal MCU OTA

The PTZ update (`FUN_005A8710`) is a stateful UART protocol (`0xF0` reset, `0xF1`
handshake, `0xF2` info, `0xF3` 256-byte data blocks, `0xF4` finalise, `0xF5` reboot).
The info block carries `image size`, `image crc32` and `image checksum` — integrity
only, no signature. A version comparison exists (`ptz no need ota`) but I found no
downgrade prevention. The MCU's own bootloader (flash 0x0–0x8000) is not shipped in
this package, so whatever check *it* performs is out of scope of this artifact.

### What this artifact cannot tell us

Ambarella H22 supports RSA-verified secure boot enforced by BST/BLD. `amboot` is
**not part of this package** — partition 0 is the application image. So I can say
definitively that the application-level updater performs no cryptographic
verification, but I cannot rule out a SoC-level secure-boot chain on the actual
flash from this file alone. If secure boot were enabled and the RTOS image itself
were signature-checked by the bootloader, replacing partition 0 would fail at boot
even though the updater accepts the package. That distinction is worth resolving
before treating the LED-suppression scenario in §5 as practically reachable.

Other hardening observed: a hardware watchdog, `A:\is_factory_mode` as the gate for
factory commands, and battery-level checks before starting an update.

---

## 8. Live verification

Checked against a connected unit (`2E1A:4C01`, `/dev/video2`) using read-only
queries: `lsusb -v`, `v4l2-ctl --list-ctrls-menus`, and `UVCIOC_CTRL_QUERY` with
`GET_LEN` / `GET_INFO` / `GET_CUR` only. Nothing was written to the device.

Confirmed:

- All three XU GUIDs match the values derived from the image, byte for byte, as do
  `bmControls` (`FF FF FF 3F` / `3F 00 00 00` / `1F 00 00 00`) and the source-ID
  chain.
- The Camera Terminal advertises exactly the control set the handler at `0x5894D4`
  implements: Focus (Absolute), Focus Auto, Zoom (Absolute), PanTilt (Absolute),
  PanTilt (Relative), Roll (Absolute), Roll (Relative).
- **`GET_LEN` matched the statically-predicted `wLength` for all 34 registered
  selectors** (XU9 1–30, XU10 1/6, XU11 1–5) with no exceptions.
- `GET_INFO` corroborates the direction analysis: XU 9/11 and 9/20 report `0x01`
  (GET only); XU 9/14, 11/4 and 11/5 report `0x02` (SET only) and reject `GET_CUR`
  with `EBADRQC` — consistent with 11/4 and 11/5 being *store preset* / *recall
  preset* write triggers.
- XU 9/17 reads back `0x00` = `uvc`, matching the mode-name table.
- XU 9/2 returns a 0x34-byte structure whose byte 0 is the mode and whose `int16` at
  offset 0x32 is the zoom field, as laid out in §2.3.
- XU 9/26 returns two little-endian `int32` angles in arcseconds that track the
  `pan_absolute` / `tilt_absolute` v4l2 values, confirming the arcsecond→0.1°
  conversion.
- XU 9/5 (gesture enable mask) read back `0x00` and XU 9/6 (bindings) all zeros on
  this unit — gestures disabled in its current configuration.

Several selectors expose device identity in plaintext to any process that can open
the video node: XU 9/12 returns the 14-character unit serial, XU 9/3 returns a
second serial/model string, and XU 9/13 and 9/23 each return a 40-hex-character
identifier. Actual values are not reproduced here since this directory is committed.

One discrepancy worth noting: XU 10 selectors 3, 4 and 5 answer `GET_LEN` with `0x0A`
even though the dispatch table at `0x011683E0` has no entries for them — the
Ambarella UVC class layer appears to answer generically below the vendor table.

## Files

- [`extract_partitions.py`](extract_partitions.py) — splits the package into its four
  components and verifies all six checksums (3× Ambarella CRC32, 2× component MD5,
  1× whole-file MD5)
- [`xu_static_map.py`](xu_static_map.py) — rebuilds the extension-unit table in §2.3
  from the RTOS image
- [`xu_probe.py`](xu_probe.py) — reads `GET_LEN` / `GET_INFO` / `GET_CUR` from a live
  device to check that map (read-only; issues no `SET_CUR`)
- [`insta360.proto`](insta360.proto) — 121 messages, 88 enums recovered from the
  embedded protobuf-c descriptors
- [`romfs_assets.txt`](romfs_assets.txt) — full 577-entry ROMFS listing

The firmware image itself is not included here; it is distributed by the vendor.
Point the scripts at your own copy:

```sh
./extract_partitions.py Insta360WebCamFW.bin out/
./xu_static_map.py out/p0_rtos.bin
./xu_probe.py /dev/videoN          # requires a connected Link
```

The version analysed is `v1.4.5.8_build1`, SHA-256
`e8e7ec61aff0c0b5b273e21971e1d9e62af5ec5c6453e0a0bad7152d5342ebb3`. Addresses in
this document are absolute in the RTOS image's own address space (base `0x20000`);
subtract `0x20000` for file offsets into `p0_rtos.bin`.

## Scope and caveats

This is interoperability and security analysis of a device I own. No vendor code is
redistributed — `insta360.proto` is a schema reconstructed from descriptor metadata,
not vendor source.

The firmware-authentication weakness in §7 has **not** been reported to Insta360 as
of publication. Note the open question at the end of that section: `amboot` is not
part of this package, so whether the SoC enforces a signed boot chain underneath the
application-level updater is unresolved, and the practical exploitability of §5 and
§7 depends on that answer.
