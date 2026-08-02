# Insta360 Link firmware teardown

## The finding

**The camera accepts unsigned firmware, and a local program can install it without
help from the user.**

The firmware package has no signature, no certificate, and no public key. The camera
verifies an MD5 digest and a 4-character type word. Both are values that anybody can
compute. Nothing else protects the update path.

To prove the consequence, I changed one data table in the firmware. The new firmware
never turns on the recording indicator. I installed it on a camera I own. The camera
now records video and the indicator light stays off. A stock image restores the light.
Each direction takes one command and about 30 seconds.

The camera also has no hardware interlock between the sensor and the indicator. The
indicator is a table lookup and a UART frame, so firmware alone controls it.

Two host paths install firmware. Both start with a UVC extension-unit request, which
any program that opens `/dev/videoN` can send:

| Path | Transfer method | Extra permission | User action |
|---|---|---|---|
| Mass storage (§4) | mount the exposed volume | mount a removable volume | replug the camera |
| Vendor class (§5) | bulk transfer to a vendor interface | write access to `/dev/bus/usb` | **none** |

The vendor-class path is the complete one. It uploads the firmware and reboots the
camera with no physical contact.

This is analysis of a device I own. See [Scope](#13-scope).

---

## 1. Target

| | |
|---|---|
| File | `Insta360WebCamFW.bin`, 35,741,008 bytes |
| SHA-256 | `e8e7ec61aff0c0b5b273e21971e1d9e62af5ec5c6453e0a0bad7152d5342ebb3` |
| Product | Insta360 Link (gen 1), codename `link1`, USB `2E1A:4C01` |
| Versions | webcam SoC `v1.4.5.8_build1`, gimbal MCU `v1.1.1`, built 2025-01-20 |

The camera reports its own version as `v1.4.5_build1`. The package declares
`v1.4.5.8_build1`. The two formats differ, which is relevant in §3.

Addresses in this document are absolute in the address space of the RTOS image, which
is based at `0x20000`. To get a file offset into `p0_rtos.bin`, subtract `0x20000`.

---

## 2. Package format

The file holds two components and a 200-byte trailer:

```
0x00000000  webcam image     0x021F2FE4   Ambarella multi-partition image
0x021F2FE4  gimbal image     0x00022CA4   Cortex-M application for the gimbal MCU
0x02215C88  trailer          0xC8 (200)
```

Read the trailer from the end of the file:

```c
struct entry {              // 84 bytes, `count` entries, in file order
    uint32_t size;
    char     name[32];      // "webcam", "gimbal"
    char     version[32];   // "v1.4.5.8_build1", "v1.1.1"
    uint8_t  md5[16];       // MD5 of the bytes of that component
};
struct trailer {
    struct entry entries[count];
    char     magic[8];      // "WFNIMACW"
    uint64_t count;         // 2
    uint8_t  md5[16];       // MD5 of the whole file, less these last 16 bytes
};
```

The two halves of the magic are also the product type word that the updater reads.
`MACW` means WEBCAM and `SRNO` means ONE RS.

### 2.1 Webcam image

The webcam component starts with a 0x230-byte outer header. Three partitions follow.
A 0x100-byte header precedes each partition. The first 28 bytes of that header are the
Ambarella `flpart_t` structure, with magic `0xA324EB90`.

| # | file offset | size | load address | contents |
|---|---|---|---|---|
| 0 | 0x000330 | 0x1285AB4 | **0x20000** | ThreadX RTOS application, ARM32 (Ambarella H22 SoC) |
| 1 | 0x1285EE4 | 0x3AC800 | – | ROMFS: `orccode.bin`, `orcme.bin`, `default_binary.bin` (DSP/ORC microcode) |
| 2 | 0x16327E4 | 0xBC0800 | – | ROMFS: 577 files, including NN models, PCM prompts, bitmaps and ISP tuning |

The outer header holds a second set of CRC32 values over the same data:

```c
struct outer {                  // 0x230 bytes, at the start of the webcam component
    uint8_t  rsv0[0x20];
    uint32_t magic;             // +0x20  0x8732DFE6
    uint32_t crc32;             // +0x24  CRC32 of the whole partition region
    uint32_t rsv1[2];
    struct { uint32_t size;     // +0x30 + 8n  size of partition n, with its 0x100 header
             uint32_t crc_reg;  // +0x34 + 8n  see below
    } part[];
    /* the remainder is a DRAM layout table and holds no integrity values */
};
```

`crc_reg` is not a finished CRC. It is the un-finalized CRC32 register, which equals
`~crc32(region[:end of partition n])`. The packer keeps one running register across
all three partitions. It stores that register un-finalized after each partition, and
writes the finalized value once into `+0x24`. For this reason `part[2].crc_reg` and
`crc32` cover identical bytes but hold opposite values.

`flpart_t.flag` is also derived. It always equals `img_len * 32`.

The package holds ten integrity values in total: three `flpart_t` CRC32 values, three
`crc_reg` values, the outer `crc32`, two component MD5 digests, and the whole-file
MD5 digest. All ten verify on the vendor image.
[`fwpack.py`](fwpack.py) recomputes all ten. Its `roundtrip` command rebuilds the
vendor file byte for byte.

This container is not new. I derived it from the image, but the same format is
already public for the Insta360 GO and X3 lines, which use the same Ambarella
lineage. See §12.1.

Partition 0 is one flat image based at `0x20000`. String references use MOVW/MOVT
pairs instead of literal pools. The sensor is an IMX577, which matches the
`imx577*_h22` tuning sets.

### 2.2 Gimbal image

The gimbal image starts with a Cortex-M vector table and loads at `0x8000`. A 32 KB
bootloader occupies `0x0` to `0x8000` and is not part of this package. The string
`INSTA360_GMS586_HC32` identifies the MCU as an HDSC HC32 part. `GMS586` matches the
`SENSOR_SERIAL_GMS586` factory option. The image holds 595 functions.

---

## 3. Update path

The camera looks for `A:\Insta360WebCamFW.bin`. `FUN_005A05A0` runs once per boot from
AppMsgMgr message `0xB0` and calls `FUN_005A0204` with a drive letter. The camera
skips the search only when that letter is `'Z'`. Command `0x8005` calls
`FUN_005A0204('A')` directly, but no USB transport reaches that command (§10.1).

`FUN_0059FEE0` then does the whole of the authentication:

1. It reads the file through MD5, and excludes the final 16 bytes.
2. It compares the result against those final 16 bytes. A mismatch gives
   `"FW File MD5 Check Fail."`
3. It reads the trailer and requires the type word `MACW` or `SRNO`. Any other value
   gives `"Project ONERS fw type is not right."`
4. It sends each component to its handler.

**There is no signature, no public key, no certificate, and no anti-rollback.** A
search of the whole 19 MB image finds no RSA, ECDSA, SHA-2 or HMAC code in the update
path. The package is not encrypted. To build firmware that the camera accepts, edit
the bytes, recompute the ten integrity values, and rebuild the trailer.

### 3.1 Component dispatch

`FUN_005A0204` reads a two-entry module table at `0x012287A8`. The stride is 0x2C
bytes: `char name[0x20]`, then an upgrade function at `+0x20`, then a version-compare
function at `+0x24`.

| module | upgrade | version compare |
|---|---|---|
| `webcam` | `0x5A0628` | `0x5A06B0` |
| `gimbal` | `0x5A0760` | none |

The camera accepts a merge package (`count == 2`) and a standalone package
(`count == 1`). It selects the handler by the component name string. A webcam-only
package therefore never touches the gimbal MCU. `fwpack.py standalone` builds one.

### 3.2 The version compare does nothing

`FUN_005A06B0` formats the running version, writes two log lines, and calls `strcmp`.
It then discards the result:

```
005a0750  blx  strcmp
005a0754  mov  r0, #1        <- the return value is overwritten
```

`FUN_0059FC48` skips a module only when this function returns 0. The function always
returns 1, so the camera never skips the webcam module. An identical or older version
installs normally. The two version strings also use different formats (§1), so the
comparison can never match. The gimbal has no compare function at all.

### 3.3 The application does not write the flash

`FUN_005A0628` only moves the system to state 5. The write happens in `FUN_0014F04C`
case 1, which calls `FUN_00943CF0(0, 5000)` and then `FUN_00945534`. That function
rewrites the 2 KB Ambarella partition table, with a CRC32 at `+0x4CC` over a table at
`0xA86AF8`, and reboots.

`amboot` programs the partitions on the next boot. `amboot` is not part of this
package, so this file cannot describe what `amboot` verifies. Section 5.4 answers that
question with a hardware test instead.

### 3.4 Gimbal MCU OTA

The PTZ update (`FUN_005A8710`) is a stateful UART protocol: `0xF0` reset, `0xF1`
handshake, `0xF2` info, `0xF3` 256-byte data blocks, `0xF4` finalize, `0xF5` reboot.
The info block carries an image size, a CRC32 and a checksum. It carries no signature.
A version comparison exists and logs `ptz no need ota`, but nothing prevents a
downgrade. The bootloader of the MCU is not part of this package.

### 3.5 Mode guard

`FUN_005A0204` reads the current USB mode name first. If that name is `"msc"`
(`0x00A30A54`) or `"simple"` (`0x00DB7484+4`), it stops and logs
`"Currently in special mode :%s, do not check for upgrade firmware"`.

The camera therefore installs firmware only from `uvc` or `photo` mode. A host must
write the file in one mode and install it from another. Section 5.3 shows how one
request satisfies both conditions.

---

## 4. Install over mass storage

XU 9 selector 17 sets the USB mode. The handler is `FUN_00587468`. The byte indexes a
four-entry table at `0xB4E3E8`: `uvc`, `photo`, `msc`, `simple`. The handler sends the
selected entry as message `0x1E`. Value 4 first writes configuration key `0x2064` and
then becomes value 2.

CAUTION: The handler does not test the range of the byte. Values of 5 and more index
past the 0x50-byte table. Send only 0 to 3.

In `msc` mode the camera drops its Insta360 identity. It enumerates as `070A:4026`,
"AmbarellaInc / A9 Platform". A host filter on `2E1A` does not find it. The volume has
the label `INSTA360`. It is 95 MB of FAT and holds `DCIM`, `LOG` and `snap`. This is
drive `A:`.

To install firmware over this path:

1. Set XU 9 selector 17 to 2.
2. Mount the volume.
3. Write `Insta360WebCamFW.bin` to the root of the volume.
4. Unmount the volume.
5. Replug the camera.

[`fwinstall.py`](fwinstall.py) with `--via msc` does steps 1 to 4. Step 5 is manual,
because `msc` mode has no control channel. The vendor-class path in §5 has no such
limit and is the default for that reason.

---

## 5. Install over the vendor class

Mode 3 (`simple`) is an Insta360 vendor class, built from `libusb_simple.a`. It holds
a file-transfer interface. Over this interface the whole update runs with no action
from the user.

### 5.1 Interface

In `simple` mode the camera enumerates as **4255:1234**. It has one vendor interface,
class `0xFF`/`0xFF`, with four bulk endpoints. Endpoints `0x01` and `0x82` are the
file-transfer pair, driven by the tasks `insta_port_01_task` and `insta_port_82_task`.
Endpoints `0x04` and `0x83` are unexplored. The class allocates a 6 MB
`jsonRespondBuffer`, so a JSON command surface exists somewhere in it.

The control requests have the shape of UVC requests but are not UVC:

```
bmRequestType  0x41 OUT / 0xC1 IN     (vendor, interface)
bRequest       0x01 SET_CUR, 0x81 GET_CUR, 0x85 GET_LEN
wValue         the control selector, directly (UVC would use CS<<8)
wIndex         ignored
```

| CS | handler | GET_LEN | function |
|---|---|---|---|
| 1 | `SimpleClass_Cs_UsbSpeedTest` | 1 | speed test |
| 2 | – | 1 | USB mode switch, with the same table as XU 9/17 |
| 4 | `SimpleClass_Cs_FileUpload` | 0x89 | host writes a file to the camera |
| 5 | – | 0xE1 | camera sends a file to the host |
| 6 | `SimpleClass_Cs_TakePicture` | 0x81 | still capture |

The camera logs a rejected selector, which is how I identified the `wValue` form:

```
insta_simple_vendor_request(): request_in=0x85, request_tye=0xc1, request_value=0x100, ...
insta_simple_cs_handle(): Don't support Control Selector 0x100
```

A `CS<<8` request is rejected and does not write the shared response buffer. The next
read therefore returns the length of the previous selector.

### 5.2 Upload

CS 4 is 137 bytes: `{u32 total, u32 written, u8 state, char name[128]}`. Write it with
`state = 1` to arm an upload. Then send the file to bulk endpoint `0x01`. Then read CS
4 until `state` becomes 2. A `state` of `0x70` means a size error and `0x71` means a
write error.

CAUTION: `SimpleClass_WriteFile` opens the target file with mode `"a+"`, which
appends. An upload over an existing file joins the two files. The updater deletes the
package after it installs it, so a normal cycle is clean. An interrupted upload leaves
a file that corrupts the next upload. The MD5 test then rejects the result, so the
camera does not install it. [`fwinstall.py`](fwinstall.py) reads the slot state before
it arms an upload.

### 5.3 CS 2 reboots the camera

CS 2 does more than change the USB mode. It restarts the camera:

```
<AppFacMain>  Execute msg_dev:0 cmd:30
< UtilCFG>    Set Config mode:0 id:14 data:0x637675      <- "uvc"
<AppOptions>  InsApp_Options_SystemMode_Set mode = uvc
              force shutdown
```

This behavior completes the attack. The mode guard (§3.5) blocks an install while the
camera is in `simple` mode. CS 2 leaves `simple` mode and reboots in one request, so
one request meets both conditions.

The full sequence:

```
XU 9/17 = 3            switch to simple mode
CS 4 SET_CUR           arm the upload {size, 0, 1, "A:\Insta360WebCamFW.bin"}
bulk OUT 0x01          send the package   (35.7 MB at about 1.6 MB/s, about 22 s)
CS 4 GET_CUR           read until state == 2
CS 2 SET_CUR = 0       force shutdown, then reboot
                       the next boot installs the package, reboots, returns as uvc
```

The whole cycle takes about 30 seconds. Nobody touches the camera.

WARNING: CS 2 after an upload commits to the install. There is no confirmation step.

### 5.4 Hardware results

I ran this sequence in both directions on a live camera:

- Stock firmware to patched firmware. The indicator went dark.
- Patched firmware to stock firmware. The indicator returned.

The camera installed a partition 0 that I modified. Therefore `amboot` does not
verify a signature over the application partition, and the H22 secure boot is either
off or not applied to that partition. This answers the question that §3.3 leaves open.

The log of the camera records the same sequence for a modified package as for the
vendor package:

```
<AppFWUpdate>  file A:\Insta360WebCamFW.bin exist.
<AppFWUpdate>  Camera type is WEBCAM
<UtilFWUpdate> File webcam MD5 check PASS.
<UtilFWUpdate> Match Module: webcam
<AppFWUpdate>  Going to upgrade amba soc.
               fwupdate start,reboot now...
```

The updater deletes the package after it installs it. The next boot logs
`No FW File: A:\Insta360WebCamFW.bin`.

One side effect has no known cause: after one install the `A:\LOG` directory was empty
and its numbering restarted at 1. About twenty earlier boot logs were gone. Other
installs kept the log history.

---

## 6. The indicator LED

The gimbal MCU owns the RGB indicator, not the SoC. The SoC sends UART command `0x50`
with a 12-byte payload. The payload is a direct copy from a 16-entry table at
`0x01192070`, where each entry is 16 bytes:

```
FUN_00590A44(mode):   InsApp_PTZ_UartSend(0x50, &table[mode*0x10], 0x0C, 0, 0x80)
```

Each entry holds three channels of `{level, brightness, period_u16}` and one `u16`
duration:

```
mode  0: 00 64 e8 03 | 00 64 e8 03 | 00 64 e8 03 | 0000    <- all channels 0, so OFF
mode  4: 00 64 e8 03 | 00 64 e8 03 | ff 64 e8 03 | 0000
mode  7: ff 32 fa 00 | a5 32 fa 00 | 00 32 fa 00 | 03e8    <- amber, 250 ms blink
mode  9: 00 32 d0 07 | 00 32 d0 07 | ff 32 d0 07 | 0000    <- firmware update
mode 12: ff 64 e8 03 | ff 64 e8 03 | ff 64 e8 03 | 0000    <- white
```

`AppPtz` selects the mode. The LED task `FUN_00590AD4` reads mode numbers from a queue
that `FUN_005909B4` fills. The callers of `FUN_005909B4` are internal state machines
for streaming, charging, firmware update, gesture events and errors. No XU selector
and no `SetParam` identifier reaches the LED, so a host cannot turn it off through
UVC.

The camera has no hardware interlock. The LED is not wired to the sensor or to the
encoder. Modified firmware can suppress it in three ways:

- Overwrite the table entries that streaming uses.
- Patch `FUN_00590A44` to always index entry 0.
- Stub `FUN_005909B4` so that no mode ever enters the queue.

`fwpack.py patch-led` uses the first method, in the most conservative form. It copies
the 16 bytes of mode 0 over the other fifteen entries. It changes no code and no
length. The bytes it writes are bytes that the MCU already receives in normal
operation, so nothing new reaches the MCU. The `--keep-update-blink` option keeps mode
9, so a later firmware update still shows progress.

The patched image differs from the vendor file in 158 bytes:

- the LED table entries
- the four outer CRC words
- the `flpart_t` CRC32 of partition 0
- the two MD5 digests in the trailer

No other byte changes.

On hardware the indicator stayed dark at idle and through a 30 fps 1080p MJPEG capture
with the sensor live.

The camera also has a `enter ptz privacy mode` function, which parks the gimbal. That
is the privacy method the vendor ships, and it is visible rather than silent.

Indicator suppression is an established class of result on other hardware (§12.3). The
difference here is cost. Earlier work on this class had to reverse a boot ROM and
build a way to reflash a microcontroller. This camera needed neither, because the
update path of the vendor accepts a modified image.

---

## 7. USB control protocol

### 7.1 USB layout

`2E1A:4C01`, bDeviceClass 0xEF (IAD), four interfaces:

| if | class | role |
|---|---|---|
| 0 | 0x0E/0x01 | VideoControl (EP 0x82 interrupt) |
| 1 | 0x0E/0x02 | VideoStreaming, MJPEG and frame-based H.264, EP 0x81 bulk |
| 2 | 0x01/0x01 | AudioControl |
| 3 | 0x01/0x02 | AudioStreaming, EP 0x83 isochronous |

UVC topology: `IT 1 (camera 0x0201) → PU 5 → XU 9 → XU 10 → OT 3`. `XU 11` also takes
its input from PU 5.

| Unit | GUID | bmControls | selectors |
|---|---|---|---|
| XU 9 | `{FAF1672D-B71B-4793-8C91-7B1C9B7F95F8}` | `FF FF FF 3F` | 1–30 |
| XU 10 | `{E307E649-4618-A3FF-82FC-2D8B5F216773}` | `3F 00 00 00` | 1–6 (1, 2, 6 implemented) |
| XU 11 | `{A8BD5DF2-1A98-474E-8DD0-D92672D194FA}` | `1F 00 00 00` | 1–5 |

### 7.2 Standard UVC controls

These controls work with plain v4l2.

Processing Unit 5, handler `0x589740`: brightness(2), contrast(3),
power-line-frequency(5), hue(6), saturation(7), sharpness(8),
white-balance-temperature(10), white-balance-temperature-auto(11).

Camera Terminal 1, handler `0x5894D4`:

| CS | control | parameter |
|---|---|---|
| 6 | `CT_FOCUS_ABSOLUTE` | 0x0D (manual focus) |
| 8 | `CT_FOCUS_AUTO` | 0x0C |
| 11 | `CT_ZOOM_ABSOLUTE` | 0x15 |
| 13 | `CT_PANTILT_ABSOLUTE` | 0x20, value/360, so arcseconds in and 0.1° internally |
| 14 | `CT_PANTILT_RELATIVE` | 0x21 (direction × speed) |
| 15 | `CT_ROLL_ABSOLUTE` | 0x20, roll field |

### 7.3 Extension-unit dispatch

Existing Linux projects for this camera already use XU unit 9 and three of its
selectors, found by USB capture (§12.2). The table below comes from the dispatch table
in the image instead, and covers all 34 registered selectors.

A table of 38 `{unitID, selector, handler}` triples at `0x011683E0` routes the
requests. The dispatcher is `FUN_00589A28`. `bRequest` values are `0x01` SET_CUR,
`0x81` GET_CUR, `0x85` GET_LEN, and `0x82`/`0x83`/`0x87` for MIN/MAX/DEF.

Handlers translate to an internal parameter space through
`InsApp_Webcam_SetParam(id, buf, arg)` at `0x5848E8` and `GetParam(id, buf, arg)` at
`0x582A8C`.

| XU | Sel | wLen | SET param | GET param | Function |
|---|---|---|---|---|---|
| 9 | 1 | 4 | 0x20,0x0D,0x0C,0x15 | 0x20 | composite state (ptz/focus/af/zoom) |
| 9 | 2 | 0x34 | 0x26,0x27,0x12,0x10 | 0x10,0x11,0x12,0x20,0x15 | preset get/set |
| 9 | 3 | 0xAA | – | – | bulk configuration blob |
| 9 | 4 | 0x106 | – | – | bulk configuration blob |
| 9 | 5 | 1 | 0x13 | 0x13 | gesture enable bitmask |
| 9 | 6 | 5 | 0x14 | 0x14 | gesture binding, 5 slots |
| 9 | 7 | 1 | 0x1E | 0x1E | noise cancellation |
| 9 | 8 | 0x1F6 | – | – | bulk |
| 9 | 9 | – | 0x06 | 0x06 | |
| 9 | 10 | 0x81 | – | – | mode string |
| 9 | 11 | 5 | – | 0x22,0x23 | status readback |
| 9 | 12 | 0x20 | – | – | unit serial |
| 9 | 13 | 0x81 | – | – | identifier |
| 9 | 14 | 1 | – | – | |
| 9 | 15 | 0x0C | – | – | |
| 9 | 16 | 0xFF | 0x29 | 0x29 | tone curve |
| 9 | 17 | 1 | – | – | **USB mode switch**, 0=uvc 1=photo 2=msc 3=simple |
| 9 | 18 | 1 | 0x19 | 0x19 | |
| 9 | 19 | 1 | 0x18 | 0x18 | track speed |
| 9 | 20 | 0xF0 | – | 0x16 | |
| 9 | 21 | 8 | 0x17 | – | set track target (float x, float y) |
| 9 | 22 | 4 | – | – | |
| 9 | 23 | 0x81 | 0x1F | 0x1F | |
| 9 | 24 | 4 | 0x1A | 0x1A | framing bias (x, y) |
| 9 | 25 | 2 | 0x07,0x21 | 0x07 | pan/tilt speed |
| 9 | 26 | 8 | 0x20 | 0x20 | absolute yaw/pitch/roll |
| 9 | 27 | 2 | 0x0B,0x0E,0x1C,0x1D,0x28,0x2A,0x2E,0x2F,0x30,0x34 | same | feature enable multiplexer |
| 9 | 28 | 0x0A | – | 0x0F | |
| 9 | 29 | 2 | 0x08 | 0x08 | shutter time |
| 9 | 30 | 1 | 0x09 | 0x09 | AE mode |
| 10 | 1 | 8 | – | – | tracking metadata |
| 10 | 2 | var | – | – | tracking raw data stream |
| 10 | 6 | 1 | 0x35 | 0x35 | AF opt test |
| 11 | 1 | 1 | – | – | |
| 11 | 2 | 1 | 0x10 | 0x10 | AutoFrame mode |
| 11 | 3 | 1 | – | – | |
| 11 | 4 | 1 | 0x31 | – | zoom preset, store index |
| 11 | 5 | 1 | 0x32 | – | zoom preset, recall index |

### 7.4 Internal parameter IDs

These come from the switch at `0x584924` (SET) and `0x582AB4`/`0x582C14` (GET). Where
the camera stores a value, the `INSUTIL_CFG2_*` key follows.

| id | meaning | stored key |
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
| 0x1A | framing bias (int16 x, int16 y, `0x7FFF` resets) | |
| 0x1C | AI zoom enable | 0x204F |
| 0x1D | algorithm (AI) master enable | 0x2050 |
| 0x1E | audio noise cancellation | 0x2051 |
| 0x20 | PTZ absolute: int16 yaw, int16 pitch, int16 roll, in 0.1° units | |
| 0x21 | PTZ velocity: int16 x_speed, y_speed, multiplied by 25 before transmit | |
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

A separate message task at `0x583878` drives the per-image IQ parameters `0x2053` to
`0x2061`. These parameters are local IQ, brightness, contrast, EV, ISO, exposure time,
exposure mode, flicker, manual focus and curve.

### 7.5 Hardware verification of this map

I read a connected unit with `lsusb -v`, `v4l2-ctl --list-ctrls-menus`, and
`UVCIOC_CTRL_QUERY` with `GET_LEN`, `GET_INFO` and `GET_CUR`. These queries write
nothing.

- The three XU GUIDs match the values from the image, byte for byte. So do the
  `bmControls` values and the source-ID chain.
- The Camera Terminal advertises the exact control set that the handler at `0x5894D4`
  implements.
- `GET_LEN` matched the predicted `wLength` for all 34 registered selectors, with no
  exceptions.
- `GET_INFO` agrees with the direction analysis. XU 9/11 and 9/20 report `0x01` (GET
  only). XU 9/14, 11/4 and 11/5 report `0x02` (SET only) and reject `GET_CUR` with
  `EBADRQC`.
- XU 9/26 returns two little-endian `int32` angles in arcseconds that track the
  `pan_absolute` and `tilt_absolute` v4l2 values.

XU 10 selectors 3, 4 and 5 answer `GET_LEN` with `0x0A`, although the dispatch table
at `0x011683E0` has no entries for them. The Ambarella UVC class layer appears to
answer below the vendor table.

### 7.6 Identifiers that any local program can read

Several selectors return device identity in plain text to any process that opens the
video node. XU 9/12 returns the 14-character unit serial. XU 9/3 returns a second
serial and model string. XU 9/13 and XU 9/23 each return a 40-character hexadecimal
identifier. This document does not reproduce the values.

---

## 8. PTZ and servo control

The camera has a two-axis mechanical gimbal for yaw and pitch, and digital roll. The
HC32 MCU drives it. The link between the SoC and the MCU is a UART frame protocol with
a CRC. The command dispatcher of the MCU (`FUN_000208D0`) accepts opcodes `0x03` to
`0x09`, `0x30` to `0x3D`, `0x50` for the LED, and `0xF0` to `0xF5` for OTA.

Motion is command `0x38` with a 6-byte payload and a sub-mode byte:

| sub-mode | behavior |
|---|---|
| 1 | relative, adds the delta to the current target |
| 2 | absolute, sets the target angle and handles the limits near ±180° |
| 3 | rate, a velocity command |

Host paths, in order of convenience:

1. Standard UVC pan and tilt. `CT_PANTILT_ABSOLUTE` (selector 13),
   `CT_PANTILT_RELATIVE` (14), `CT_ROLL_ABSOLUTE` (15) and `CT_ZOOM_ABSOLUTE` (11).
   The camera divides the arcsecond value by 360, which gives 0.1° internally. So
   `v4l2-ctl --set-ctrl pan_absolute=…` drives the servos with no vendor tool.
2. XU 9 selector 26 (and 23), 8 bytes to parameter 0x20: `int16 yaw, int16 pitch,
   int16 roll` in 0.1° units, sent as sub-mode 2.
3. XU 9 selector 25, 2 bytes to parameter 0x21: pan and tilt speed. The RTOS
   multiplies each value by 25 and issues sub-mode 3. It inverts the sign when the
   orientation flag reports inverted mounting.
4. XU 9 selector 2, 0x34 bytes: the full preset structure. `0x0E1A` in a field means
   no change. `0x7FFF` is the reset value for bias.
5. Six stored zoom presets at `0x25FA2E4`, 6 × 0x33 bytes, through parameters
   0x31/0x32 or XU 11 selectors 4/5.

Ranges read from a live unit:

| control | min | max | step | in degrees |
|---|---|---|---|---|
| `pan_absolute` | −522000 | +522000 | 3600 | ±145°, 1° granularity |
| `tilt_absolute` | −324000 | +360000 | 3600 | −90° to +100°, 1° granularity |
| `zoom_absolute` | 100 | 400 | 1 | 1.0× to 4.0× |
| `focus_absolute` | 0 | 100 | 1 | – |

The values are UVC arcseconds. The firmware divides by 360, so the internal resolution
is 0.1° although the descriptor advertises 1° steps. The Camera Terminal advertises
roll, but `uvcvideo` does not map it to a v4l2 control. Roll is reachable through a raw
UVC control transfer or XU 9/26.

---

## 9. Gestures

The pipeline is the Insta360 `bva` vision library, which uses MNN inference and
OpenCV. The sources are at `vendors/Insta360/bva/src/trackerv2/amba/`.

Gesture init loads these models from ROMFS partition 2:

- `det_hand_08976b01.INT8.mnn`, the hand detector
- `det_head_dc4d29bf.INT8.mnn`, the head detector
- `cls_gesture_2M_3ed5b674.FP32.mnn`, the gesture classifier, output tensor `probs`
- `det_person_24M_008109ee.FP32.mnn`, the person detector used by the tracker

The runtime uses dedicated ThreadX tasks: `AppGesture task`, `webcam ges event task`,
`webcam person task`, `webcam zoom task`, `webcam ptz task`, `webcam snap task`,
`webcam wboard detect task`, `webcam rc scan task`, `webcam ai state monitor task`.

The bva entry points are `WebcamGetGestureEvents` for debounced events, `GetGestureCls`
for raw classification, `GetRealTimeHandInfo`, `WebcamGetHeadList`,
`WebcamPersonDetect` and `WebcamTrackerPersonBox`.

XU 9 selector 5 enables gestures with one byte, which goes to parameter 0x13. The byte
is a 5-bit mask. `FUN_00595C8C(group, on)` reads a table at `0xB50D38` that maps a
group to gesture identifiers:

| bit | group | gesture ids |
|---|---|---|
| 0 | 1 | 0x1F |
| 1 | 2 | 0x01 |
| 2 | 3 | 0x15, 0x16, 0x17 |
| 3 | 4 | 0x0B, 0x0C |
| 4 | 5 | (unmapped) |

XU 9 selector 6 sets the bindings with five bytes, which go to parameter 0x14
(`FUN_0058292C`). There are five slots. Each byte is 0 to 2 and passes through the map
`{3,1,2,1}` to an action code, before `FUN_00595DD8(gesture_id, action)`.

Recognized actions appear as message-manager events `GUI_PROMPT_HIGHTLIGHT_POINT`,
`GUI_PROMPT_HIGHTLIGHT_FINGER` and `GUI_PROMPT_SLOWDOWN_START/END`, with the audio
cues `finger_point.pcm` and `shutter_9times.pcm`. Gesture modes include single-target
tracking (`Do Single Track`), zoom (`Entry Desktop`, `zoom_target`), whiteboard
(`Entry White Board`, which searches for corner tags by a luminance threshold) and
DeskView.

---

## 10. Other host surfaces

### 10.1 The protobuf protocol

The binary carries the generic Insta360 camera protocol, `INSPROTO_UCD_MSG_*`, built
with protobuf-c. The descriptors are intact, so I recovered the complete schema: 121
messages and 88 enums, written to [`insta360.proto`](insta360.proto). It is the shared
Insta360 camera codebase from the ONE R and X-series line. It covers capture, options,
file transfer, Bluetooth, WiFi, factory tests and `PtzCtrlInfo`.

No UCD message is reachable over USB on the Link. The dispatcher `FUN_005BD9FC`
handles messages such as `INSPROTO_UCD_MSG_HOST_CMD_REBOOT_CAMERA`, but no USB
transport reaches it. The vendor class in §5 carries file transfer and JSON, not
protobuf. UCD belongs to the AmbaLink, Bluetooth and WiFi side, which this hardware
does not expose.

### 10.2 Debug surfaces

1. **Serial console over USB CDC-ACM.** The `AmbaShell` command tree includes
   `t app test usbdbg [start | 2uart | 2usb]`, which moves the debug console between
   the physical UART and USB. The CDC-ACM descriptor set is in the image as
   `4255:0052`, with the strings `Amba`, `Amba cdcacm class` and `Ambarella UART`.
   `ApplibUsbCdcAcmMulti` drives it.
2. **USB class switching.** `t app test chg_usbmode [msc|amage|rs232]`, and XU 9
   selector 17 from the host (§4).
3. **Ambarella iTuner protocol** over the `amage` class, module `USB HDLR`. It can
   read and write ISP registers, color-correction tables, FPN maps and vignette maps.
   It can also save raw frames and JPEG files, and set the exposure.
4. **Factory command set**, `INSPROTO_UCD_MSG_FACTORY_CMD_*`: `LED_TEST`,
   `MOTOR_TEST`, `GYROSCOPE_TEST`, `USB_SPEED_TEST`, `SCRIPT_JSON_UPLOAD`,
   `SCRIPT_RUN`, `PTZ_CTRL_SET_OPTION` and `GET_OPTION`, and vignette, BLC and BPC
   data save. The file `A:\is_factory_mode` gates these commands.
5. **Log surfaces**: a print ring buffer, `A:\factory.log`, `C:\temperature.log`, and
   `INSAPP_CMD_READ_CPU_EXCEPTION_LOG`.

The shell tree (`t app test …`) covers resolution, bitrate, encode mode, EIS, raw
capture, key and jack injection, memory statistics, and `erase_sd0`. I did not
establish whether the CDC path is active in a shipping unit before a `usbdbg` command.

Other hardening in the image: a hardware watchdog, `A:\is_factory_mode` as the gate
for factory commands, and a battery-level test before an update starts.

---

## 11. Files

| File | Purpose |
|---|---|
| [`fwpack.py`](fwpack.py) | Parse, verify, patch and rebuild the package. Recomputes all ten integrity values. |
| [`fwinstall.py`](fwinstall.py) | Install firmware onto a camera. `--via simple` (default, §5) needs no user action. `--via msc` (§4) needs a manual replug. |
| [`xu_static_map.py`](xu_static_map.py) | Rebuild the extension-unit table in §7.3 from the RTOS image. |
| [`xu_probe.py`](xu_probe.py) | Read `GET_LEN`, `GET_INFO` and `GET_CUR` from a live camera. Writes nothing. |
| [`insta360.proto`](insta360.proto) | 121 messages and 88 enums from the protobuf-c descriptors. |
| [`romfs_assets.txt`](romfs_assets.txt) | The 577-entry ROMFS listing. |

`fwpack.py` writes nothing to a camera. `fwinstall.py` writes only with `--yes`. Its
`status` command never writes.

This directory does not hold the firmware image. The vendor distributes it. Point the
tools at your own copy:

```sh
./fwpack.py verify    Insta360WebCamFW.bin
./fwpack.py roundtrip Insta360WebCamFW.bin        # must report byte-identical
./fwpack.py patch-led Insta360WebCamFW.bin noled.bin --keep-update-blink
./fwinstall.py install /dev/videoN noled.bin --yes  # needs root for raw USB
./xu_probe.py /dev/videoN                         # needs a connected camera
```

Raw USB needs write access to `/dev/bus/usb/*`. Run as root, or add a udev rule for
`2E1A:4C01`, `4255:1234` and `070A:4026`. The camera uses a different USB identity in
each mode, so a rule must list all three.

---

## 12. Prior work

### 12.1 Package format

The container in §2 is already public for other Insta360 cameras. I derived it from
the image before I found this work, so §2 is an independent rediscovery and not a new
result.

- [enekochan/insta360-go-firmware-tool](https://github.com/enekochan/insta360-go-firmware-tool)
  documents the format for the GO 2, GO 3 and GO 3S, which use the same Ambarella H22
  SoC. Its firmware-structure document gives:
    - the 560-byte header and the magic at `0x20`
    - the CRC32 at `0x24`
    - the section table at `0x30`, as a length and an **inverted running CRC32**
    - the 256-byte section headers with magic `0x90EB24A3`
    - the MD5 over all preceding data
    - the footer entries of size, name, version and MD5

  The tool repacks firmware that those cameras accept. That document prints the magic
  values in the opposite byte order to this one.
- [nneonneo/Insta360-X3-Firmware-Tools](https://github.com/nneonneo/Insta360-X3-Firmware-Tools)
  unpacks and repacks Insta360 X3 firmware and notes that the format resembles other
  Ambarella devices and the GO structure.

Neither project mentions the Link. Neither documents a signature check, which agrees
with §3.

### 12.2 Control protocol

- [dtinth](https://dt.in.th/Insta360LinkControllerWebSocketProtocol) reverse
  engineered the WebSocket protocol of the Link Controller desktop application. That
  work is at the application layer and does not cover USB.
- [vrwallace/Insta360-Link-1-and-2-Controller-for-Linux](https://github.com/vrwallace/Insta360-Link-1-and-2-Controller-for-Linux)
  and [EdenCoder/insta360-linux](https://github.com/EdenCoder/insta360-linux) control
  the camera from Linux. Both use XU unit 9, which agrees with §7.1. Both use
  selectors 2, 19 and 14, found from the work of dtinth plus USB capture and
  experiment.

Neither project covers firmware, USB mode switching, mass storage, the vendor class,
or the indicator LED.

### 12.3 Indicator suppression on other hardware

- [iSeeYou: Disabling the MacBook Webcam Indicator LED](https://www.usenix.org/conference/usenixsecurity14/technical-sessions/presentation/brocker),
  Brocker and Checkoway, USENIX Security 2014. Reprograms the iSight microcontroller.
- [Lights Out](https://github.com/xairy/lights-out), Konovalov, POC 2024. Turns off
  the webcam indicator of the ThinkPad X230. The LED is on GPIO B1 of a Ricoh R5U8710
  at XDATA address `0x80`. That work had to leak and reverse the boot ROM of the
  controller and build a way to reflash an 8051 over USB.

### 12.4 What this document adds

I found no published firmware analysis of the Link, and no CVE for it. The parts that
appear to be new are:

- the `simple` vendor class of the Link: `4255:1234`, control selectors 1, 2, 4, 5
  and 6, the `wValue = CS` form, and control selector 2 as a reboot primitive (§5)
- the unattended install that follows from it, on any Insta360 camera (§5.3)
- the LED path of the Link: the table at `0x01192070` and UART command `0x50` to the
  gimbal MCU (§6)
- the full extension-unit map and the internal parameter space (§7.3, §7.4)
- the mode guard and the inert version compare (§3.2, §3.5)
- the hardware result that `amboot` does not verify partition 0 on this camera (§5.4)

## 13. Scope

This is interoperability and security analysis of a device I own. This directory
holds no vendor code. `insta360.proto` is a schema rebuilt from descriptor metadata,
not vendor source.

As of publication I did not report this weakness to Insta360.
