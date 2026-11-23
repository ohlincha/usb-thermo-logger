# USB Thermo Logger

Live display, plot and CSV logging for **PCsensor TEMPer-compatible** USB thermometers and hygrometers, on
Windows, macOS and Linux. Pure Python standard library (Tk GUI); no drivers or packages to install.

*TEMPer is a trademark of PCsensor. This project is independent and not affiliated with or endorsed by
PCsensor; the name is used only to describe compatible hardware.*

| USB ID | Device | Firmware (USB product string) | Reads |
|---|---|---|---|
| 0c45:7401 | TEMPer | TEMPerV1.4 | temperature |
| 0c45:7402 | TEMPerHUM | TEMPERHUM1V1.0 | temperature, relative humidity |
| 0c45:7403 | TH1000iso thermocouple interface | TH1000isoV1.5 | internal (cold-junction) temperature; probe inputs reported raw |

Several sensors can be read at once. Linux lists 0c45:7403 as "Foot Switch": that ID is shared with PCsensor
foot pedals.

## Install

Installers are attached to each [release](https://github.com/ohlincha/usb-thermo-logger/releases):

- **Windows:** `ThermoLogger.exe` (GUI) and `thermo-logger-cli.exe` (command line). Uses Windows' own HID driver.
- **macOS:** `UsbThermoLogger-macOS.dmg`: drag *USB Thermo Logger* to Applications. The app is not notarised: the
  first time, right-click it and choose **Open** (macOS 15: System Settings → Privacy & Security → Open Anyway).
- **Linux (Debian/Ubuntu):** `sudo apt install ./usb-thermo-logger_<version>_all.deb` (installs a udev rule, so
  no root is needed: any user logged in at the computer can read the sensors, and over SSH members of the
  `plugdev` group; replug the sensors afterwards). Other distributions: run `thermo_logger_gui.py` with Python 3
  and Tk, and install the rule: `sudo cp 70-usb-thermo-logger.rules /etc/udev/rules.d/ && sudo udevadm control --reload`.

## Use

The window shows every connected sensor with its latest values and reply bytes, and plots temperature (left
axis) and humidity (right axis, dashed) over time. Set the reading interval, the plot span, and press
**Log to CSV…** to write every reading to a file (`time, sensor, temperature_C, humidity_pct, raw`).
Sensors are told apart by their USB port.

**Names and offsets:** double-click a sensor to give it a name and a temperature or humidity offset (saved per
model and USB port). *Match temperature to* sets the offset so the sensor reads the same as a reference sensor
right now; let both settle side by side first. The plain TEMPer (0c45:7401) has its sensor inside the stick next
to its microcontroller and warms itself by a few °C (ours: about +3.5 °C after a few minutes in a USB hub):
a short USB extension cable away from the computer reduces this; an offset corrects the rest.

Command line:

```
thermo-logger-cli list                    # sensors found, with their keys and offsets
thermo-logger-cli read --raw              # one reading of each, with the reply bytes
thermo-logger-cli log -i 10 -o log.csv    # every 10 s until Ctrl-C
thermo-logger-cli set 7401@1-7.2 --name Bench --temperature-offset -3.5
```

## Protocol

The sensors have two HID interfaces: a keyboard (for the "type the value" mode) and a command interface. A
reading is the output report `01 80 33 01 00 00 00 00` on the command interface; the 8-byte reply holds
big-endian values from byte 2:

- **TEMPer (V1.4):** bytes 2–3 = temperature × 256 (signed).
- **TEMPerHUM (V1.0):** bytes 2–3 = temperature × 100; bytes 4–5 = Sensirion SHT1x 12-bit humidity ticks,
  RH = −2.0468 + 0.0367·n − 1.5955×10⁻⁶·n², with the SHT1x temperature correction (checked against a reference
  hygrometer).
- **TH1000iso (V1.5):** bytes 2–3 = internal temperature × 256; bytes 4–5 and 6–7 = the two thermocouple
  inputs, `0f ff` without a probe. The probe scale is not confirmed yet; readings with a probe are welcome.

## License

MIT
