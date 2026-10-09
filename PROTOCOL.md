# Orkli TermoWifi local protocol

Reverse-engineered from two sources:

- **App**: the decompiled Android app *Orkli iDomus* 3.5.001
  (`com.orkli.orkliapp`, built on the Ingenium BUSing SDK). Class references
  point to `com.ingenium.*`.
- **Capture**: traffic observed on a real installation (TermoWifi TW501B +
  ControlKlima CK1080, 3 TermoLite zones + bathrooms).

Each section says which source backs it. Anything marked *unconfirmed* is a
best guess.

## Transport

| Endpoint | Use | Source |
|----------|-----|--------|
| TermoWifi, TCP **12345** | Raw 7-byte frames (below). Used by this integration. | Capture |
| `192.168.4.1` UDP 12000 / TCP 12345 | AP mode setup, same 7-byte frames with `CMD 0x8C` | App (`ConfigureWifiActivityStep3`) |
| EthBus gateway, TCP 12347 | App to Ingenium gateway: `FF FF DST_HI DST_LO CMD D1 D2` out, 9-byte frames in | App (`Comunicacion`) |
| Orkli cloud | Same packets as 12347, prefixed with an `EE EE <id>` login frame | App (`Comunicacion`) |

The app never talks to port 12345 in normal use: it goes through the cloud. The
packet contents (`CMD`, `D1`, `D2`) are the same on every transport, which is
how the register map below was obtained.

## Frame

```
 byte   0      1       2       3     4    5    6
       3B    DEST   ORIGIN    CMD   D1   D2   CHK
```

- `3B`: start byte.
- `DEST`, `ORIGIN`: 1-byte bus addresses. TermoWifi (zone controller) is
  `0x01`; the app/gateway is `0xFE`.
- `CHK = (DEST + ORIGIN + CMD + D1 + D2) & 0xFF` (App, `ConfigureWifiActivityStep3`).

Frames are not length-prefixed and may arrive split or merged in TCP reads.
Resynchronize by looking for `3B` and validating `CHK`.

| CMD | Meaning |
|-----|---------|
| `0x04` | Register write (to the TermoWifi) or status report (from it) |
| `0x01` | ACK. The app never uses its `D1`/`D2` to update state. |
| `0x8C` | Device configuration (WiFi SSID/password upload). **Do not send.** |

Frame shapes seen on the TermoWifi (Capture):

| Frame | Meaning |
|-------|---------|
| `3B 01 FE 04 D1 D2 CHK` | Client to TermoWifi: write register `D1` with `D2` |
| `3B 01 01 04 D1 D2 CHK` | TermoWifi status report (`DEST == ORIGIN == 01`) |
| `3B FE 01 01 D1 D2 CHK` | TermoWifi ACK to the client |

Frames with `ORIGIN = FE` (client writes) can also be received on the socket;
they are commands, not status, and must be ignored.

## Registers (`D1`)

### Per zone

Zone `z` is 0-based (0..7; TermoLite zone "1" is `z = 0`). Each zone has four
registers at `D1 = z * 4 + r` (App, `IngUnidadAucore`):

| r | D1 for z = 0..7 | Name (app) | Content |
|---|-----------------|------------|---------|
| 0 | `00 04 08 0C 10 14 18 1C` | `estado` | On/off |
| 1 | `01 05 09 0D 11 15 19 1D` | `modoFuncionamiento` | Mode, fan speed, flags |
| 2 | `02 06 0A 0E 12 16 1A 1E` | `consigna` | Setpoint |
| 3 | `03 07 0B 0F 13 17 1B 1F` | `ambiente` | Room temperature |

Humidity is at `D1 = 0x64 + z` (100..107).

### Global

| D1 | Meaning | Source |
|----|---------|--------|
| `0x23` (35) | Write `0` = ask for a full status dump. The app sends it when the thermostat screen opens. | App (`UnidadAucoreActivity`) |
| `0x32` (50) | Dew point / fancoil alarm: `0` none, `1`, `2` (two alarm icons) | App |
| `0x35` (53) | Available modes bitmask: bit `n` set means mode `n` (1..6) is allowed. Non-zero means an Orkli unit. | App |

## Value encoding

### Setpoint (`r = 2`)

```
°C = v / 2          v = round(°C * 2)
```

- Range 15.0..35.0 °C (`v` = 30..70), step 0.5 °C. The app clamps to this range.
- `v = 0`: no data, so the zone does not exist (used for discovery).

| v (hex) | v | °C |
|---------|---|----|
| `1E` | 30 | 15.0 |
| `28` | 40 | 20.0 |
| `29` | 41 | 20.5 |
| `2A` | 42 | 21.0 |
| `2C` | 44 | 22.0 |
| `46` | 70 | 35.0 |

### Room temperature (`r = 3`)

```
°C = (162 - v) / 2      v = 162 - °C * 2
```

The scale is **inverted**: a higher `v` means a colder room. The step is 0.5 °C.

- `v = 0`: no data. Writing `0` to this register is a read request.
- `v < 62` (above 50 °C): the app shows it as no data.
- `v > 162`: below 0 °C (`v = 255` is -46.5 °C).

| v (hex) | v | °C |
|---------|---|----|
| `3E` | 62 | 50.0 |
| `76` | 118 | 22.0 |
| `78` | 120 | 21.0 |
| `79` | 121 | 20.5 |
| `7A` | 122 | 20.0 |
| `A2` | 162 | 0.0 |

### Humidity (`D1 = 0x64 + z`)

```
% = v / 2.55        v = % * 2.55
```

- Range 0..100 % (`v` = 0..255). The resolution is about 0.4 %.
- `v = 0` and `v = 255`: no data. Not every zone has a humidity sensor; the
  TermoWifi (living room) does.

| v (hex) | v | % |
|---------|---|---|
| `66` | 102 | 40.0 |
| `80` | 128 | 50.2 |
| `99` | 153 | 60.0 |

### State (`r = 0`)

| Bit | Meaning |
|-----|---------|
| 0 | On |
| 1 | Meaning *unconfirmed*; the app keeps it when turning off |

The app treats `(v & 3) == 3` as on and normalizes `(v & 3) == 1` to `3`, so
on is effectively bit 0. To write it, the app turns on with `v | 3` and turns
off with `v & 2`, which usually gives `03` and `02`.

### Mode (`r = 1`)

| Bits | Meaning |
|------|---------|
| 0..3 | Mode (table below) |
| 4..7 | Fan speed: `0x00`, `0x10`, `0x20`, `0x30`, `0x40` (fancoil units only) |
| 6, 7 | Also read as dew point flags: `v >> 7 == 1` means forced, `v >> 6 == 1` means error. This overlaps with fan speed `0x40`; *unconfirmed*. |

Odd modes are cooling and even modes are heating:

| Mode | Function | Counterpart |
|------|----------|-------------|
| 1 | Cool, air (fancoil) | 4 |
| 2 | Heat, floor | 3 |
| 3 | Cool, floor | 2 |
| 4 | Heat, air | 1 |
| 5 | Cool, floor + air | 6 |
| 6 | Heat, floor + air | 5 |

The app changes only bits 0..3 and keeps the fan speed. Old Masterclima
gateways (id starting with `PP`) swap modes 2 and 4. That variant does not
apply to the TermoWifi and is out of scope.

## Sequences

### Zone discovery

From `ConfigureTermowifiUserActivity.generateTermowifiInstal()`: make two passes
over zones 0..7, skip zones already found, and for each remaining zone send a
read request and wait 3 s:

```
3B 01 FE 04 (z*4+3) 00 CHK
```

A zone exists when it answers with a status report for register `z*4+2`
(setpoint) with a non-zero value.

### Read a zone

Send `z*4+3 = 0`. The TermoWifi reports the zone registers with
`3B 01 01 04 …` frames.

### Change setpoint

Write `z*4+2 = °C * 2`. After a slider change in a TermoWifi installation the
app also sends `z*4+3 = 0` to read the zone back.

## Worked examples

All values are hex.

| Frame | Decoded |
|-------|---------|
| `3B 01 FE 04 23 00 26` | Ask for a full status dump |
| `3B 01 FE 04 07 00 0A` | Read zone 1 (`07` = 1·4+3) |
| `3B 01 FE 04 06 2B 34` | Zone 1 setpoint = 21.5 °C (`2B` = 43) |
| `3B 01 FE 04 04 03 0A` | Zone 1 on |
| `3B 01 FE 04 04 02 09` | Zone 1 off |
| `3B 01 FE 04 05 02 0A` | Zone 1 mode heat, floor |
| `3B 01 01 04 07 78 85` | Report: zone 1 room temperature 21.0 °C (`78` = 120) |
| `3B 01 01 04 65 80 EB` | Report: zone 1 humidity 50.2 % (`80` = 128) |
| `3B FE 01 01 06 2B 31` | ACK of the setpoint write (`D1`/`D2` echo the write, Capture) |
