# Dell VEP4600 BMC Fan5 patch

Patch helper and notes for Dell EMC VEP4600 and Dell Edge 3400 BMC firmware `2.30`.

The script patches a Dell BMC `.ima` image offline. It enables Fan5 reporting and control paths in `libipmipdk.so.2.26.0`, adjusts the fan-control ladder, and updates the image checksums needed by AMI YAFU.

## Scope

This was tested against Dell BMC firmware `2.30` for AST2500-based VEP4600 and Edge 3400 systems.

The script is layout-specific. It validates known original or already-patched bytes before modifying each code site. If the image layout or `libipmipdk.so` code differs, it exits instead of patching unknown bytes.

The repository does not include Dell firmware images. Download the original firmware from Dell, extract `VEP4600-BMC-v2.30.ima`, and run the patch locally.

## Legal and safety notes

This project is unofficial and is not affiliated with, endorsed by, or supported by Dell.

The repository includes original tooling and factual notes only. It does not include Dell firmware images, extracted firmware files, Dell binaries, Dell source code, YAFU binaries, or decompiled code.

Use this only on hardware that you own or are authorized to service. Firmware modification can void support agreements, violate license terms, damage hardware, or leave a BMC unbootable. Keep a tested recovery path.

## Patch behavior

The stock firmware reads a one-byte fan-count profile from system EEPROM and stores it in `gFanNum`. On a four-fan platform, the stock policy hides or disables several Fan5 paths.

The patch keeps the detected platform fan count unchanged. It only changes the selected Fan5 gates and side effects:

- Fan5 status can report present when tach and presence are valid.
- Fan5 RPM can report through IPMI.
- all-fan PWM writes include Fan5.
- four-fan initialization no longer sets Fan5 PWM to zero.
- four-fan LED policy no longer forces logical Fan5 LED off.
- fan failure and thermal emergency full-speed paths remain intact.

## Fan curve

Default stock ladder:

```text
Rise:    27 32 36 40 255
Decline: 24 27 32 36 255
PWM:     20 50 65 80 100
```

Default patched ladder:

```text
Rise:    30 35 40 46 255
Decline: 27 32 37 43 255
PWM:     20 30 45 65 100
```

You can provide a custom ladder with `--rise`, `--decline`, and `--pwm`. Keep the final `100` PWM endpoint unless you have validated thermal behavior under load.

## Patch the image

```sh
python3 patch-vep4600-bmc-fan5.py \
  VEP4600-BMC-v2.30.ima \
  -o /tmp/VEP4600-BMC-v2.30.fan5-curve30-yafucrc.ima
```

Known output hash for the default patch:

```text
b9138fa9af014db048ed15abdc5f97009c3790253f659425b03bcb9e889d0cc8
```

Custom ladder example:

```sh
python3 patch-vep4600-bmc-fan5.py \
  VEP4600-BMC-v2.30.ima \
  -o /tmp/VEP4600-BMC-v2.30.fan5-custom.ima \
  --rise '31 36 41 47 255' \
  --decline '28 33 38 44 255' \
  --pwm '20 35 50 70 100'
```

## Flash the image

Run Dell's `Yafuflash` from the host OS with KCS access to the BMC:

```sh
./Yafuflash -kcs -mi /tmp/VEP4600-BMC-v2.30.fan5-curve30-yafucrc.ima

./Yafuflash -non-interactive -cd -d 1 -mse 2 \
  /tmp/VEP4600-BMC-v2.30.fan5-curve30-yafucrc.ima \
  -pipmi
```

This example flashes image 2. Select boot image 2 in the BMC web interface, then reboot the BMC.

If YAFU leaves the BMC in update mode, this reset command was used during testing:

```sh
ipmitool raw 0x06 0x02
```

## Verify

After booting the patched image:

```sh
ipmitool sensor | grep Fan
ipmitool sdr elist all | grep -i fan5
ipmitool raw 0x3a 0xde 0x05 0x03
```

Expected Fan5 SDR output includes a present Fan5 status sensor and a Fan5 RPM reading.

The fan debug command should print the patched ladder:

```text
Rise:    30 35 40 46 255
Decline: 27 32 37 43 255
PWM:     20 30 45 65 100
```

## Notes

- The script uses only the Python standard library.
- The script does not flash the BMC.
- The script does not require SSH, SCP, Redfish, or a BMC shell.
- Runtime bind-mount testing used a BMC shell and TFTP, but that path is not needed for the offline image patch.
- Flashing BMC firmware can leave a device unbootable. Keep a recovery path.
