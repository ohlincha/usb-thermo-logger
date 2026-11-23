#!/usr/bin/env python3
"""USB Thermo Logger: reader and logger for PCsensor TEMPer-compatible USB thermometers / hygrometers
(stdlib only). TEMPer is a trademark of PCsensor; this project is not affiliated with PCsensor.

Supported (USB vendor 0c45):
  7401  TEMPer        temperature
  7402  TEMPerHUM     temperature + relative humidity (firmware TEMPERHUM1V1.x)
  7403  TH1000iso     thermocouple interface: internal temperature; probe inputs reported raw (unconfirmed)

The sensors are HID devices with two interfaces (a keyboard, used for the "type the value" mode, and a
second one for commands). A reading is the 8-byte command 01 80 33 01 00 00 00 00 sent as an output report
on the second interface; the 8-byte reply holds big-endian values from byte 2.

Usage:
  python3 thermo_logger.py list
  python3 thermo_logger.py read                 # one reading of every sensor
  python3 thermo_logger.py log -i 10 -o log.csv # every 10 s until Ctrl-C
"""

import argparse
import csv
import datetime
import json
import os
import sys
import time

__version__ = "0.2.2"

VID = 0x0C45
CMD_READ = bytes([0x01, 0x80, 0x33, 0x01, 0x00, 0x00, 0x00, 0x00])

MODELS = {
    0x7401: "TEMPer",
    0x7402: "TEMPerHUM",
    0x7403: "TH1000 thermocouple",
}


class HidError(Exception):
    pass


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------

def _s16(b, i):
    return int.from_bytes(b[i:i + 2], "big", signed=True)


def _u16(b, i):
    return int.from_bytes(b[i:i + 2], "big", signed=False)


def sht1x_humidity(raw, temp_c):
    """Sensirion SHT1x 12-bit relative humidity with temperature compensation (datasheet coefficients)."""
    rh_lin = -2.0468 + 0.0367 * raw - 1.5955e-6 * raw * raw
    rh = (temp_c - 25.0) * (0.01 + 0.00008 * raw) + rh_lin
    return max(0.0, min(100.0, rh))


def decode(pid, reply):
    """Returns a dict of quantities ({"temperature": °C, "humidity": %RH}) from an 8-byte reply."""
    if len(reply) < 6:
        raise HidError(f"short reply: {reply.hex(' ')}")
    if pid == 0x7402:
        # TEMPERHUM1V1.0: temperature in 1/100 °C, humidity as SHT1x 12-bit ticks (checked against a reference
        # hygrometer: 727 ticks = 23.8 %RH at 25.5 °C)
        t = _s16(reply, 2) / 100.0
        return {"temperature": t, "humidity": round(sht1x_humidity(_u16(reply, 4), t), 2)}
    if pid == 0x7401:
        # TEMPer1 (TEMPerV1.x): temperature in 1/256 °C
        return {"temperature": round(_s16(reply, 2) / 256.0, 2)}
    if pid == 0x7403:
        # TH1000iso (thermocouple interface): bytes 2-3 its internal (cold-junction) temperature in 1/256 °C;
        # bytes 4-5 and 6-7 the two thermocouple inputs, 0x0fff when no probe is connected. The probe scale is
        # not confirmed yet (no thermocouple at hand): connected inputs are reported raw.
        q = {"temperature": round(_s16(reply, 2) / 256.0, 2)}
        for k, i in (("tc1_raw", 4), ("tc2_raw", 6)):
            if len(reply) >= i + 2 and _u16(reply, i) != 0x0FFF:
                q[k] = _u16(reply, i)
        return q
    return {}


# --------------------------------------------------------------------------
# HID back ends (command interface only)
# --------------------------------------------------------------------------

class LinuxHid:
    """Kernel hidraw interface (/dev/hidrawN)."""

    @staticmethod
    def enumerate():
        base = "/sys/class/hidraw"
        found = []
        if not os.path.isdir(base):
            return found
        for name in sorted(os.listdir(base)):
            dev = os.path.join(base, name, "device")
            try:
                with open(os.path.join(dev, "uevent")) as fh:
                    info = dict(line.strip().split("=", 1) for line in fh if "=" in line)
            except OSError:
                continue
            parts = info.get("HID_ID", "").split(":")
            if len(parts) != 3 or int(parts[1], 16) != VID:
                continue
            pid = int(parts[2], 16)
            # .../1-7.2:1.1/0003:0C45:7402.002B : the USB interface number is after the last '.' of "1-7.2:1.1"
            usbif = os.path.basename(os.path.dirname(os.path.realpath(dev)))
            ifnum = usbif.rsplit(".", 1)[-1] if ":" in usbif else "?"
            if ifnum != "1":
                continue                        # interface 0 is the keyboard
            found.append({"path": "/dev/" + name, "pid": pid, "name": info.get("HID_NAME", ""),
                          "location": usbif.split(":")[0]})
        return found

    def __init__(self, path):
        try:
            self.fd = os.open(path, os.O_RDWR)
        except PermissionError:
            raise HidError(
                f"Permission denied opening {path}.\n"
                "Install the udev rule once (see README):\n"
                "  sudo cp 70-usb-thermo-logger.rules /etc/udev/rules.d/ && sudo udevadm control --reload\n"
                "then unplug/replug the sensor.")
        self.path = path

    def transfer(self, cmd, timeout=1.5):
        import select
        while select.select([self.fd], [], [], 0)[0]:   # drop stale replies
            os.read(self.fd, 64)
        os.write(self.fd, b"\x00" + cmd)                 # report ID 0 (unnumbered reports)
        if not select.select([self.fd], [], [], timeout)[0]:
            raise HidError("no reply")
        data = os.read(self.fd, 64)
        if not data:
            raise HidError("device disconnected")
        return data

    def close(self):
        os.close(self.fd)


class WindowsHid:
    """hid.dll / setupapi.dll through ctypes (Windows' own HID driver; nothing to install)."""

    @staticmethod
    def enumerate():
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                        ("d4", ctypes.c_ubyte * 8)]

        class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("guid", GUID), ("flags", wintypes.DWORD),
                        ("reserved", ctypes.c_void_p)]

        class HIDD_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Size", wintypes.ULONG), ("VendorID", wintypes.USHORT),
                        ("ProductID", wintypes.USHORT), ("VersionNumber", wintypes.USHORT)]

        hid = ctypes.WinDLL("hid")
        setupapi = ctypes.WinDLL("setupapi")
        k32 = ctypes.WinDLL("kernel32")
        setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND,
                                                  wintypes.DWORD]
        setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                         wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
        setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]

        guid = GUID()
        hid.HidD_GetHidGuid(ctypes.byref(guid))
        info = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x02 | 0x10)
        found = []
        idx = 0
        while True:
            ifd = SP_DEVICE_INTERFACE_DATA()
            ifd.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(guid), idx, ctypes.byref(ifd)):
                break
            idx += 1
            need = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value + 4)
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(ifd), buf, need, None, None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            if "mi_01" not in path.lower():
                continue                        # interface 0 is the keyboard (opened exclusively by Windows)
            h = k32.CreateFileW(path, 0, 3, None, 3, 0, None)
            if h in (None, wintypes.HANDLE(-1).value):
                continue
            attr = HIDD_ATTRIBUTES()
            attr.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
            ok = hid.HidD_GetAttributes(h, ctypes.byref(attr))
            k32.CloseHandle(h)
            if ok and attr.VendorID == VID:
                # the instance part of the path ("...&mi_01#7&abc&0&0000#...") tells sensors apart
                loc = path.split("#")[2] if path.count("#") >= 2 else path
                found.append({"path": path, "pid": attr.ProductID, "name": MODELS.get(attr.ProductID, ""),
                              "location": loc})
        setupapi.SetupDiDestroyDeviceInfoList(info)
        return found

    def __init__(self, path):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        k32 = self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        for fn in ("ReadFile", "WriteFile"):
            getattr(k32, fn).argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                            wintypes.BOOL]
        k32.CancelIo.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        FILE_FLAG_OVERLAPPED = 0x40000000
        h = k32.CreateFileW(path, 0xC0000000, 3, None, 3, FILE_FLAG_OVERLAPPED, None)
        if h in (None, wintypes.HANDLE(-1).value):
            raise HidError(f"cannot open {path} (error {ctypes.get_last_error()})")
        self.h = h
        self.in_len, self.out_len = self._report_lengths()

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]
        self.OVERLAPPED = OVERLAPPED

    def _report_lengths(self):
        ctypes = self.ctypes
        hid = ctypes.WinDLL("hid")
        pp = ctypes.c_void_p()
        hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        if not hid.HidD_GetPreparsedData(self.h, ctypes.byref(pp)):
            return 9, 9
        caps = (ctypes.c_ushort * 64)()  # HIDP_CAPS: Usage, UsagePage, InputReportByteLength, OutputReport...
        hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        hid.HidP_GetCaps(pp, caps)
        hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
        hid.HidD_FreePreparsedData(pp)
        return caps[2] or 9, caps[3] or 9

    def _io(self, fn, buf, n, timeout):
        ctypes, wintypes, k32 = self.ctypes, self.wintypes, self.k32
        ov = self.OVERLAPPED()
        ov.hEvent = k32.CreateEventW(None, True, False, None)
        done = wintypes.DWORD()
        try:
            if not fn(self.h, buf, n, None, ctypes.byref(ov)):
                if ctypes.get_last_error() != 997:          # ERROR_IO_PENDING
                    raise HidError(f"I/O failed (error {ctypes.get_last_error()}) - device disconnected?")
                if k32.WaitForSingleObject(ov.hEvent, int(timeout * 1000)) != 0:
                    k32.CancelIo(self.h)
                    raise HidError("no reply")
            if not k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(done), True):
                raise HidError(f"I/O failed (error {ctypes.get_last_error()})")
            return done.value
        finally:
            k32.CloseHandle(ov.hEvent)

    def transfer(self, cmd, timeout=1.5):
        ctypes = self.ctypes
        out = ctypes.create_string_buffer(b"\x00" + cmd + b"\x00" * max(0, self.out_len - 1 - len(cmd)),
                                          self.out_len)
        self._io(self.k32.WriteFile, out, self.out_len, timeout)
        buf = ctypes.create_string_buffer(self.in_len)
        n = self._io(self.k32.ReadFile, buf, self.in_len, timeout)
        return buf.raw[1:n]                                  # without the report-ID byte

    def close(self):
        self.k32.CloseHandle(self.h)


class MacHid:
    """IOKit's IOHIDManager through ctypes."""

    _lib = None

    @classmethod
    def _libs(cls):
        if cls._lib is None:
            import ctypes
            from ctypes import c_void_p, c_int32, c_long, c_char_p, c_uint32, c_double, c_bool
            iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            sig = {
                (cf, "CFStringCreateWithCString"): (c_void_p, [c_void_p, c_char_p, c_uint32]),
                (cf, "CFNumberGetValue"): (c_bool, [c_void_p, c_long, c_void_p]),
                (cf, "CFSetGetCount"): (c_long, [c_void_p]),
                (cf, "CFSetGetValues"): (None, [c_void_p, c_void_p]),
                (cf, "CFRelease"): (None, [c_void_p]),
                (cf, "CFRetain"): (c_void_p, [c_void_p]),
                (cf, "CFRunLoopGetCurrent"): (c_void_p, []),
                (cf, "CFRunLoopRunInMode"): (c_int32, [c_void_p, c_double, c_bool]),
                (iokit, "IOHIDManagerCreate"): (c_void_p, [c_void_p, c_uint32]),
                (iokit, "IOHIDManagerSetDeviceMatching"): (None, [c_void_p, c_void_p]),
                (iokit, "IOHIDManagerCopyDevices"): (c_void_p, [c_void_p]),
                (iokit, "IOHIDDeviceGetProperty"): (c_void_p, [c_void_p, c_void_p]),
                (iokit, "IOHIDDeviceOpen"): (c_int32, [c_void_p, c_uint32]),
                (iokit, "IOHIDDeviceClose"): (c_int32, [c_void_p, c_uint32]),
                (iokit, "IOHIDDeviceSetReport"): (c_int32, [c_void_p, c_int32, c_long, c_char_p, c_long]),
                (iokit, "IOHIDDeviceScheduleWithRunLoop"): (None, [c_void_p, c_void_p, c_void_p]),
                (iokit, "IOHIDDeviceUnscheduleFromRunLoop"): (None, [c_void_p, c_void_p, c_void_p]),
                (iokit, "IOHIDDeviceRegisterInputReportCallback"): (
                    None, [c_void_p, c_void_p, c_long, c_void_p, c_void_p]),
            }
            for (lib, name), (res, args) in sig.items():
                fn = getattr(lib, name)
                fn.restype, fn.argtypes = res, args
            cls._lib = (ctypes, iokit, cf)
        return cls._lib

    @classmethod
    def _int_prop(cls, dev, key):
        ctypes, iokit, cf = cls._libs()
        k = cf.CFStringCreateWithCString(None, key.encode(), 0x08000100)
        ref = iokit.IOHIDDeviceGetProperty(dev, k)
        cf.CFRelease(k)
        if not ref:
            return None
        v = ctypes.c_int32()
        cf.CFNumberGetValue(ref, 3, ctypes.byref(v))
        return v.value

    @classmethod
    def _devices(cls):
        ctypes, iokit, cf = cls._libs()
        mgr = iokit.IOHIDManagerCreate(None, 0)
        iokit.IOHIDManagerSetDeviceMatching(mgr, None)
        devset = iokit.IOHIDManagerCopyDevices(mgr)
        devs = []
        if devset:
            n = cf.CFSetGetCount(devset)
            arr = (ctypes.c_void_p * n)()
            cf.CFSetGetValues(devset, arr)
            for d in arr:
                if cls._int_prop(d, "VendorID") != VID:
                    continue
                if (cls._int_prop(d, "PrimaryUsagePage"), cls._int_prop(d, "PrimaryUsage")) == (1, 6):
                    continue                    # the keyboard interface
                loc = cls._int_prop(d, "LocationID") or 0
                devs.append(({"path": f"usb-location-{loc:08x}", "pid": cls._int_prop(d, "ProductID"),
                              "name": MODELS.get(cls._int_prop(d, "ProductID"), ""),
                              "location": f"{loc:08x}"}, cf.CFRetain(d)))
        return mgr, devs

    @classmethod
    def enumerate(cls):
        return [info for info, _ in cls._devices()[1]]

    def __init__(self, path):
        import queue
        ctypes, iokit, cf = self._libs()
        self.mgr, devs = self._devices()
        match = [d for info, d in devs if info["path"] == path]
        if not match:
            raise HidError(f"device {path} not found")
        self.dev = match[0]
        if iokit.IOHIDDeviceOpen(self.dev, 0) != 0:
            raise HidError("IOHIDDeviceOpen failed (check System Settings > Privacy & Security > Input Monitoring)")
        self.q = queue.Queue()
        self._buf = (ctypes.c_uint8 * 64)()
        CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32,
                              ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8), ctypes.c_long)

        def on_report(ctx, result, sender, rtype, rid, report, length):
            self.q.put(bytes(report[:length]))

        self._cb = CB(on_report)
        self._scheduled = False

    def transfer(self, cmd, timeout=1.5):
        import queue
        ctypes, iokit, cf = self._libs()
        if not self._scheduled:                 # on the thread that runs the run loop
            iokit.IOHIDDeviceRegisterInputReportCallback(self.dev, self._buf, 64, self._cb, None)
            self._mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
            iokit.IOHIDDeviceScheduleWithRunLoop(self.dev, cf.CFRunLoopGetCurrent(), self._mode)
            self._scheduled = True
        while not self.q.empty():
            self.q.get_nowait()
        if iokit.IOHIDDeviceSetReport(self.dev, 1, 0, cmd, len(cmd)) != 0:   # kIOHIDReportTypeOutput
            raise HidError("write failed - device disconnected?")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                return self.q.get_nowait()
            except queue.Empty:
                cf.CFRunLoopRunInMode(self._mode, 0.05, True)
        raise HidError("no reply")

    def close(self):
        _, iokit, _ = self._libs()
        iokit.IOHIDDeviceClose(self.dev, 0)


def backend():
    if sys.platform.startswith("linux"):
        return LinuxHid
    if sys.platform == "win32":
        return WindowsHid
    if sys.platform == "darwin":
        return MacHid
    raise HidError(f"unsupported platform {sys.platform}")


# --------------------------------------------------------------------------
# Settings: per-sensor name and offsets, keyed by model and USB port (the sensors have no serial number)
# --------------------------------------------------------------------------

APP_ID = "usb-thermo-logger"


def config_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_ID)


class Settings:
    def __init__(self, path=None):
        self.path = path or os.path.join(config_dir(), "sensors.json")
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}

    def get(self, key):
        d = self.data.get(key, {})
        return {"name": d.get("name", ""), "temperature_offset": float(d.get("temperature_offset", 0.0)),
                "humidity_offset": float(d.get("humidity_offset", 0.0))}

    def set(self, key, **values):
        d = self.data.setdefault(key, {})
        d.update(values)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------
# Sensors
# --------------------------------------------------------------------------

class Sensor:
    def __init__(self, info, settings=None):
        self.info = info
        self.pid = info["pid"]
        self.model = MODELS.get(self.pid, f"0c45:{self.pid:04x}")
        self.key = f"{self.pid:04x}@{info['location']}"      # settings key: model and USB port
        self.settings = settings
        self.hid = backend()(info["path"])
        self.last_raw = b""
        self.last_uncorrected = {}

    @property
    def config(self):
        if self.settings:
            return self.settings.get(self.key)
        return {"name": "", "temperature_offset": 0.0, "humidity_offset": 0.0}

    @property
    def label(self):
        name = self.config["name"] if self.settings else ""
        return name or f"{self.model} ({self.info['location']})"

    def read(self):
        """One reading with the sensor's offsets applied; keeps the reply (last_raw) and the uncorrected values."""
        reply = self.hid.transfer(CMD_READ)
        self.last_raw = reply
        q = decode(self.pid, reply)
        self.last_uncorrected = dict(q)
        if self.settings:
            c = self.config
            if "temperature" in q and c["temperature_offset"]:
                q["temperature"] = round(q["temperature"] + c["temperature_offset"], 2)
            if "humidity" in q and c["humidity_offset"]:
                q["humidity"] = round(max(0.0, min(100.0, q["humidity"] + c["humidity_offset"])), 2)
        return q

    def close(self):
        self.hid.close()


def find_sensors():
    return backend().enumerate()


def open_all(settings=None):
    sensors, errors = [], []
    settings = settings if settings is not None else Settings()
    for info in find_sensors():
        try:
            sensors.append(Sensor(info, settings))
        except HidError as e:
            errors.append(str(e))
    return sensors, errors


UNITS = {"temperature": "°C", "humidity": "%RH"}


def fmt(key, value):
    if key in UNITS:
        return f"{key} {value:.2f} {UNITS[key]}"
    return f"{key} {value}"


def iso_time(t):
    return datetime.datetime.fromtimestamp(t).isoformat(timespec="seconds")


def main(argv=None):
    ap = argparse.ArgumentParser(description="USB Thermo Logger: reader/logger for TEMPer-compatible USB sensors")
    ap.add_argument("--version", action="version", version=f"thermo-logger {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="list sensors")
    r = sub.add_parser("read", help="one reading of every sensor")
    r.add_argument("--raw", action="store_true", help="also print the reply bytes")
    o = sub.add_parser("set", help="name a sensor or set its offsets (see 'list' for the keys)")
    o.add_argument("key", help="sensor key from 'list', e.g. 7401@1-7.2")
    o.add_argument("--name")
    o.add_argument("--temperature-offset", type=float, help="°C added to the reading")
    o.add_argument("--humidity-offset", type=float, help="%%RH added to the reading")
    lg = sub.add_parser("log", help="log every sensor until Ctrl-C")
    lg.add_argument("-i", "--interval", type=float, default=10.0, help="seconds between readings (default 10)")
    lg.add_argument("-o", "--output", help="CSV file (default: stdout)")
    a = ap.parse_args(argv)
    cmd = a.cmd or "read"

    if cmd == "list":
        found = find_sensors()
        st = Settings()
        for info in found:
            key = f"{info['pid']:04x}@{info['location']}"
            c = st.get(key)
            extra = "".join([f"  name '{c['name']}'" if c["name"] else "",
                             f"  T offset {c['temperature_offset']:+g} °C" if c["temperature_offset"] else "",
                             f"  RH offset {c['humidity_offset']:+g} %" if c["humidity_offset"] else ""])
            print(f"{MODELS.get(info['pid'], '?'):22s} key {key:14s} {info['path']}{extra}")
        if not found:
            print("no sensors found")
        print(f"settings: {st.path}")
        return 0
    if cmd == "set":
        vals = {k: v for k, v in (("name", a.name), ("temperature_offset", a.temperature_offset),
                                  ("humidity_offset", a.humidity_offset)) if v is not None}
        if not vals:
            ap.error("nothing to set: give --name, --temperature-offset or --humidity-offset")
        Settings().set(a.key, **vals)
        print(f"{a.key}: {vals}")
        return 0

    sensors, errors = open_all()
    for e in errors:
        print("error:", e, file=sys.stderr)
    if not sensors:
        print("no sensors could be opened", file=sys.stderr)
        return 1
    try:
        if cmd == "read":
            for s in sensors:
                try:
                    q = s.read()
                    vals = "  ".join(fmt(k, v) for k, v in q.items())
                    print(f"{s.label}: {vals}" + (f"   [{s.last_raw.hex(' ')}]" if a.raw else ""))
                except HidError as e:
                    print(f"{s.label}: error: {e}", file=sys.stderr)
            return 0
        out = open(a.output, "a", newline="") if a.output else sys.stdout
        w = csv.writer(out)
        if not a.output or out.tell() == 0:
            w.writerow(["time", "sensor", "temperature_C", "humidity_pct"])
        while True:
            t = time.time()
            for s in sensors:
                try:
                    q = s.read()
                    w.writerow([iso_time(t), s.label, q.get("temperature", ""), q.get("humidity", "")])
                except HidError as e:
                    print(f"{s.label}: error: {e}", file=sys.stderr)
            out.flush()
            time.sleep(max(0.0, a.interval - (time.time() - t)))
    except KeyboardInterrupt:
        return 0
    finally:
        for s in sensors:
            s.close()


if __name__ == "__main__":
    sys.exit(main())
