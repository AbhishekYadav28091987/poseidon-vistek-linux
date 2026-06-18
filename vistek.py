#!/usr/bin/env python3
"""
vistek.py — Linux driver/daemon for the COUGAR Poseidon Vistek ARGB
1.9" LCD AIO cooler display (USB HID 2c65:1000, "HWCX USB Display").

Reverse-engineered from the Windows "RM-Hardware" software (TempComm.dll +
XKWLib.dll). The screen is a value-driven display: the host pushes a 64-byte
HID report (command 0x02) carrying CPU temp/load/clock, fan RPMs, time, etc.,
and the cooler's firmware renders it. This tool reads the CPU temperature from
the Linux `k10temp` sensor and pushes it to the screen.

Report 0x02 layout (the 65-byte buffer written to hidraw = [report-id 0x00] + 64):
  idx  field
   0   0x00  HID report id (stripped by kernel for unnumbered reports)
   1   0x02  command: dynamic monitoring data
   2   CPU temperature (deg C)
   3   (unused here)
   4   hour     5 minute   6 second   7 millisecond/10
   8   year/100 (century, =20)   9 year%100   10 month   11 day   12 weekday
  13   CPU load %
  14   CPU clock hi   15 CPU clock lo   (MHz, big-endian)
  ...  GPU / MEM / disk / fans / voltages / watts (left 0)
  49   display mode byte
  rest 0

Usage:
  sudo ./vistek.py test [TEMP]   # send a fixed temp (default 77) once, to verify
  sudo ./vistek.py once          # send the real CPU temp once
  sudo ./vistek.py daemon        # loop forever, updating every second
  sudo ./vistek.py raw HEX...    # send arbitrary 64 payload bytes (debug)
"""
import os, sys, glob, time, struct, datetime, json

VID, PID = 0x2C65, 0x1000
REPORT_LEN = 64          # payload size (without the leading report-id byte)
CMD_DYNAMIC = 0x02


# ---------------------------------------------------------------- device I/O
def find_hidraw():
    """Return /dev/hidrawN for the 2c65:1000 display, or None."""
    want = f"{VID:08X}:{PID:08X}".upper()  # HID_ID looks like 0003:00002C65:00001000
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(path, "device/uevent")) as f:
                txt = f.read().upper()
        except OSError:
            continue
        if f"{VID:08X}:{PID:08X}" in txt.replace("0X", ""):
            return "/dev/" + os.path.basename(path)
        # robust match on the HID_ID line
        for line in txt.splitlines():
            if line.startswith("HID_ID=") and want in line:
                return "/dev/" + os.path.basename(path)
    return None


def open_device(wait=False):
    """Open the display hidraw node. If wait=True, block until it appears
    (used by the systemd service so it survives USB/boot ordering)."""
    dev = find_hidraw()
    while wait and not dev:
        time.sleep(3)
        dev = find_hidraw()
    if not dev:
        sys.exit("error: Poseidon Vistek display (2c65:1000) not found in /sys/class/hidraw")
    try:
        fd = os.open(dev, os.O_RDWR)
    except PermissionError:
        sys.exit(f"error: no permission for {dev} (run with sudo, or install the udev rule)")
    return fd, dev


def send_report(fd, payload):
    """payload = 64 bytes; prepend report-id 0x00 and write 65 bytes."""
    assert len(payload) == REPORT_LEN, len(payload)
    buf = bytes([0x00]) + bytes(payload)
    n = os.write(fd, buf)
    if n != len(buf):
        raise IOError(f"short write {n}/{len(buf)}")


# ------------------------------------------------------------ packet builder
def _put16(p, hi_idx, value):
    """Store a 16-bit big-endian value at payload[hi_idx], payload[hi_idx+1]."""
    v = max(0, min(0xFFFF, int(round(value))))
    p[hi_idx] = (v >> 8) & 0xFF
    p[hi_idx + 1] = v & 0xFF


def build_dynamic(cpu_temp, cpu_load=0, cpu_clock_mhz=0, cpu_watt=0,
                  cpu_fan_rpm=0, pump_rpm=0, cpu_voltage=0.0,
                  display_mode=None, now=None):
    """Build the 64-byte command-0x02 payload (offsets verified vs TempComm.dll)."""
    p = bytearray(REPORT_LEN)
    p[0] = CMD_DYNAMIC
    p[1] = clamp_u8(cpu_temp)                 # [2]  CPU temperature

    now = now or datetime.datetime.now()
    p[3] = now.hour                           # [4]
    p[4] = now.minute                         # [5]
    p[5] = now.second                         # [6]
    p[6] = now.microsecond // 10000           # [7]  ms/10
    p[7] = now.year // 100                    # [8]  century (20)
    p[8] = now.year % 100                     # [9]
    p[9] = now.month                          # [10]
    p[10] = now.day                           # [11]
    p[11] = (now.weekday() + 1) % 7           # [12] .NET DayOfWeek: Sun=0

    p[12] = clamp_u8(cpu_load)                # [13] CPU load %
    _put16(p, 0x0d, cpu_clock_mhz)            # [14/15] CPU clock MHz

    _put16(p, 0x18, cpu_fan_rpm)              # [25/26] CPU/radiator fan RPM
    # CPU voltage: whole volts + hundredths, packed as 16-bit (whole<<8 | frac)
    volt = max(0.0, float(cpu_voltage))
    p[0x1a] = clamp_u8(int(volt))             # [27] volts (integer part)
    p[0x1b] = clamp_u8(round((volt - int(volt)) * 100))  # [28] hundredths
    _put16(p, 0x1c, cpu_watt * 10)            # [29/30] CPU power (W x10)

    _put16(p, 0x31, pump_rpm)                 # [50/51] water-pump RPM

    if display_mode is not None:
        p[48] = display_mode & 0xFF           # [49]

    return p


def clamp_u8(v):
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        v = 0
    return max(0, min(255, v))


# ----------------------------------------------------------- sensor readers
def read_cpu_temp():
    """Read CPU temperature (deg C) from the k10temp hwmon (AMD). Tctl preferred."""
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            name = open(os.path.join(hw, "name")).read().strip()
        except OSError:
            continue
        if name not in ("k10temp", "zenpower", "coretemp"):
            continue
        # prefer a label like Tctl/Tdie/Package, else first temp input
        best = None
        for inp in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            label_file = inp.replace("_input", "_label")
            label = ""
            if os.path.exists(label_file):
                label = open(label_file).read().strip()
            milli = int(open(inp).read().strip())
            c = milli / 1000.0
            if label in ("Tctl", "Tdie", "Package id 0"):
                return c
            if best is None:
                best = c
        if best is not None:
            return best
    raise RuntimeError("no CPU temp sensor (k10temp) found")


_rapl_prev = None  # (energy_uj, monotonic_time)

def _rapl_path():
    for d in sorted(glob.glob("/sys/class/powercap/intel-rapl:*")):
        try:
            if open(os.path.join(d, "name")).read().strip() == "package-0":
                return d
        except OSError:
            pass
    # fallback: first package
    cands = sorted(glob.glob("/sys/class/powercap/intel-rapl:[0-9]"))
    return cands[0] if cands else None

def read_cpu_watt():
    """CPU package power (W) via RAPL energy delta. Needs root. 0 if unavailable."""
    global _rapl_prev
    d = _rapl_path()
    if not d:
        return 0
    try:
        e = int(open(os.path.join(d, "energy_uj")).read())
        mx = int(open(os.path.join(d, "max_energy_range_uj")).read())
    except (OSError, ValueError):
        return 0
    t = time.monotonic()
    if _rapl_prev is None:
        _rapl_prev = (e, t)
        return 0
    de = (e - _rapl_prev[0]) % mx
    dt = t - _rapl_prev[1]
    _rapl_prev = (e, t)
    return 0 if dt <= 0 else de / dt / 1e6


def find_fan_hwmon():
    """Return the hwmon dir for the Super-IO chip (nct6687/nct6683/it87...)."""
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            name = open(os.path.join(hw, "name")).read().strip()
        except OSError:
            continue
        if name.startswith(("nct6", "it87", "nuvoton")):
            return hw
    return None

def list_fans():
    """Return {channel:int -> rpm:int} for all nonzero fan inputs on the SIO chip."""
    hw = find_fan_hwmon()
    out = {}
    if not hw:
        return out
    for f in glob.glob(os.path.join(hw, "fan*_input")):
        ch = int(os.path.basename(f)[3:].split("_")[0])
        try:
            out[ch] = int(open(f).read())
        except (OSError, ValueError):
            pass
    return out

def read_fan(channel):
    hw = find_fan_hwmon()
    if not hw:
        return 0
    try:
        return int(open(os.path.join(hw, f"fan{channel}_input")).read())
    except (OSError, ValueError):
        return 0


# ------------------------------------------------------- shared status file
STATUS_PATH = os.environ.get("VISTEK_STATUS", "/run/vistek/status.json")

def read_all():
    """Read every sensor once (stateful deltas advance once per call)."""
    return {
        "ts": time.time(),
        "cpu_temp": round(read_cpu_temp(), 1),
        "cpu_load": round(read_cpu_load(), 1),
        "cpu_clock_mhz": round(read_cpu_clock_mhz()),
        "cpu_watt": round(read_cpu_watt(), 1),
        "fans": {str(c): r for c, r in list_fans().items()},  # all channels
    }

def write_status(vals):
    """Atomically write the status JSON (world-readable) for the GUI widget."""
    try:
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(vals, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATUS_PATH)
    except OSError:
        pass

def packet_from(vals, fan_ch, pump_ch):
    return build_dynamic(
        cpu_temp=vals["cpu_temp"], cpu_load=vals["cpu_load"],
        cpu_clock_mhz=vals["cpu_clock_mhz"], cpu_watt=vals["cpu_watt"],
        cpu_fan_rpm=vals["fans"].get(str(fan_ch), 0),
        pump_rpm=vals["fans"].get(str(pump_ch), 0),
    )


def read_cpu_clock_mhz():
    """Average current CPU MHz from /proc/cpuinfo (best effort)."""
    try:
        mhz = [float(l.split(":")[1]) for l in open("/proc/cpuinfo")
               if l.lower().startswith("cpu mhz")]
        return sum(mhz) / len(mhz) if mhz else 0
    except Exception:
        return 0


_load_prev = None

def _cpu_snap():
    parts = open("/proc/stat").readline().split()[1:]
    vals = list(map(int, parts))
    idle = vals[3] + vals[4]
    return sum(vals), idle

def read_cpu_load():
    """Overall CPU load % since the previous call (non-blocking, stateful)."""
    global _load_prev
    total, idle = _cpu_snap()
    if _load_prev is None:
        _load_prev = (total, idle)
        return 0
    dt, di = total - _load_prev[0], idle - _load_prev[1]
    _load_prev = (total, idle)
    return 0 if dt <= 0 else max(0, min(100, 100 * (dt - di) / dt))


# ------------------------------------------------------------------- main
def main():
    args = sys.argv[1:]
    mode = args[0] if args else "daemon"

    if mode == "raw":
        fd, dev = open_device()
        hexstr = "".join(args[1:]).replace(",", " ")
        data = bytes.fromhex(hexstr.replace(" ", ""))
        payload = (data + bytes(REPORT_LEN))[:REPORT_LEN]
        send_report(fd, payload)
        print(f"sent {len(data)} bytes (padded to {REPORT_LEN}) to {dev}")
        return

    if mode == "test":
        temp = int(args[1]) if len(args) > 1 else 77
        fd, dev = open_device()
        p = build_dynamic(temp, cpu_load=50, cpu_clock_mhz=4200)
        send_report(fd, p)
        print(f"{dev}: sent TEST packet, CPU temp byte = {temp}.  Look at the LCD.")
        print("payload:", p.hex(" "))
        return

    if mode == "fans":
        hw = find_fan_hwmon()
        if not hw:
            print("No Super-IO fan chip loaded. Try: sudo modprobe nct6683 force=1")
            return
        print(f"fan chip: {open(os.path.join(hw,'name')).read().strip()} ({hw})")
        for ch, rpm in sorted(list_fans().items()):
            print(f"  fan{ch}: {rpm} RPM")
        print("\nSet VISTEK_FAN_CH (radiator fan) and VISTEK_PUMP_CH (pump) to the")
        print("channels above. Spin up the pump/fans to tell them apart by RPM.")
        return

    # channels for radiator fan + pump; auto = the two highest nonzero fans
    def pick_channels():
        # VISTEK_FAN_CH may be a single channel or a comma list; take the first here.
        fan_env = os.environ.get("VISTEK_FAN_CH", "").replace(" ", "").split(",")[0]
        pump_ch = os.environ.get("VISTEK_PUMP_CH")
        if fan_env and pump_ch:
            return int(fan_env), int(pump_ch)
        nz = sorted(((r, c) for c, r in list_fans().items() if r > 0), reverse=True)
        # heuristic: pump usually spins fastest -> highest RPM is pump
        pump = int(pump_ch) if pump_ch else (nz[0][1] if nz else 0)
        fan = int(fan_env) if fan_env else (nz[1][1] if len(nz) > 1 else 0)
        return fan, pump

    if mode == "once":
        fd, dev = open_device()
        fan_ch, pump_ch = pick_channels()
        read_cpu_load(); read_cpu_watt()      # prime stateful deltas
        time.sleep(0.3)
        vals = read_all()
        p = packet_from(vals, fan_ch, pump_ch)
        send_report(fd, p)
        vals["display_fan_ch"] = fan_ch
        write_status(vals)
        print(f"{dev}: sent CPU {p[1]}C load {p[12]}% watt {(p[0x1c]<<8|p[0x1d])/10}W "
              f"fan {p[0x18]<<8|p[0x19]} pump {p[0x31]<<8|p[0x32]} rpm")
        return

    if mode == "daemon":
        fd, dev = open_device(wait=("--wait" in args or os.environ.get("VISTEK_WAIT")))
        fan_ch, pump_ch = pick_channels()
        interval = float(os.environ.get("VISTEK_INTERVAL", "0.5"))  # 2 Hz like Windows
        # VISTEK_FAN_CH may be a comma list (e.g. "1,16"); the single RPM field on
        # the LCD then cycles through those channels every VISTEK_ALT_SECS seconds.
        raw = os.environ.get("VISTEK_FAN_CH", str(fan_ch))
        rpm_channels = [int(x) for x in raw.replace(" ", "").split(",") if x] or [fan_ch]
        alt_secs = float(os.environ.get("VISTEK_ALT_SECS", "5"))
        if len(rpm_channels) > 1:
            print(f"{dev}: streaming every {interval}s, RPM cycles {rpm_channels} "
                  f"every {alt_secs}s; Ctrl-C to stop")
        else:
            print(f"{dev}: streaming every {interval}s (fan=fan{rpm_channels[0]}); Ctrl-C to stop")
        read_cpu_load(); read_cpu_watt()      # prime deltas
        start = time.monotonic()
        while True:
            t0 = time.monotonic()
            cur = rpm_channels[int((t0 - start) // alt_secs) % len(rpm_channels)]
            try:
                vals = read_all()
                send_report(fd, packet_from(vals, cur, pump_ch))
                vals["display_fan_ch"] = cur
                write_status(vals)
            except Exception as e:
                sys.stderr.write(f"warn: {e}\n")
            time.sleep(max(0, interval - (time.monotonic() - t0)))
        return

    sys.exit(f"unknown mode: {mode!r}\n{__doc__}")


if __name__ == "__main__":
    main()
