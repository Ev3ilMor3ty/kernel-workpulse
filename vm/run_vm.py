#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build a disposable initramfs and run WorkPulse checks inside QEMU only.

No host insmod/rmmod, disk image, network interface, shared directory or root
privilege is used. Supply a matching kernel image and module built from headers.
"""
import argparse, gzip, hashlib, json, os, pathlib, shutil, subprocess, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parents[1]
INIT = r"""#!/bin/sh
export PATH=/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
fail() { echo "WP_FAIL:$*"; poweroff -f; }
echo "WP_KERNEL:$(uname -r)"
if insmod /workpulse.ko period_ms=0; then fail "accepted period zero"; fi
if insmod /workpulse.ko period_ms=10001; then fail "accepted oversized period"; fi
insmod /workpulse.ko period_ms=10 || fail "initial load"
[ -r /proc/workpulse ] || fail "missing proc file"
if echo forbidden > /proc/workpulse; then fail "proc file accepted a write"; fi
if echo 22 > /sys/module/workpulse/parameters/period_ms; then fail "parameter changed after load"; fi
exec 3< /proc/workpulse
if rmmod workpulse; then fail "unloaded while reader held reference"; fi
exec 3<&-
i=0
while [ "$i" -lt 12 ]; do
  sleep 0.1
  echo "WP_SAMPLE:$(cat /proc/workpulse)"
  i=$((i+1))
done
# Add competing work in the VM, never in the host.
yes > /dev/null & load1=$!
yes > /dev/null & load2=$!
i=0
while [ "$i" -lt 12 ]; do
  sleep 0.1
  echo "WP_LOAD_SAMPLE:$(cat /proc/workpulse)"
  i=$((i+1))
done
kill "$load1" "$load2"
wait "$load1" "$load2" 2>/dev/null
rmmod workpulse || fail "first unload"
[ ! -e /proc/workpulse ] || fail "proc file leaked after unload"
for period in 10 17 100 1000 10 17 100 1000; do
  insmod /workpulse.ko period_ms="$period" || fail "reload $period"
  sleep 1.1
  echo "WP_CYCLE_SAMPLE:$(cat /proc/workpulse)"
  rmmod workpulse || fail "cycle unload"
  [ ! -e /proc/workpulse ] || fail "cycle proc leak"
done
echo "WP_PASS:lifecycle,invalid-parameters,read-only,held-open,telemetry,load,reload"
sync
poweroff -f
"""


def newc(tree, destination):
    # A tiny deterministic newc writer avoids requiring a host cpio install.
    out = bytearray()

    def add(name, mode, data, ino):
        name = name.encode() + b"\0"
        fields = [ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0]
        out.extend(b"070701" + b"".join(f"{v:08x}".encode() for v in fields))
        out.extend(name)
        out.extend(b"\0" * ((-len(out)) % 4))
        out.extend(data)
        out.extend(b"\0" * ((-len(out)) % 4))

    for ino, p in enumerate(sorted(tree.rglob("*")), 1):
        mode = p.lstat().st_mode
        data = (
            os.readlink(p).encode()
            if p.is_symlink()
            else p.read_bytes() if p.is_file() else b""
        )
        add(str(p.relative_to(tree)), mode, data, ino)
    add("TRAILER!!!", 0, b"", len(out))
    destination.write_bytes(gzip.compress(out, mtime=0))


def validate_sample(s):
    assert s["schema"] == 1 and 10 <= s["period_ms"] <= 10000
    assert (
        len(s["histogram_us_log2"]) == 24
        and sum(s["histogram_us_log2"]) == s["samples"]
    )
    assert (
        s["samples"] > 0 and s["last_late_ns"] <= s["max_late_ns"] <= s["sum_late_ns"]
    )
    assert s["effective_period_ns"] > 0 and s["uptime_ns"] > 0
    assert (
        0 < s["started_ns"] <= s["last_sample_ns"] <= s["started_ns"] + s["uptime_ns"]
    )
    bucket = lambda ns: min(23, max(0, (ns // 1000).bit_length() - 1))
    assert bucket(s["max_late_ns"]) == max(
        i for i, c in enumerate(s["histogram_us_log2"]) if c
    )
    assert s["histogram_us_log2"][bucket(s["last_late_ns"])] > 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kernel", required=True, type=pathlib.Path)
    p.add_argument("--busybox", required=True, type=pathlib.Path)
    p.add_argument("--module", type=pathlib.Path, default=ROOT / "module/workpulse.ko")
    p.add_argument("--qemu", default="qemu-system-x86_64")
    p.add_argument("--bios-dir", type=pathlib.Path)
    p.add_argument("--bios", type=pathlib.Path)
    p.add_argument("--output", type=pathlib.Path, default=ROOT / ".vm/latest")
    p.add_argument("--timeout", type=int, default=180)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="workpulse-vm-") as d:
        tree = pathlib.Path(d) / "root"
        tree.mkdir()
        for directory in ["bin", "proc", "sys", "dev", "tmp"]:
            (tree / directory).mkdir()
        shutil.copy2(a.busybox, tree / "bin/busybox")
        (tree / "bin/busybox").chmod(0o755)
        for tool in [
            "sh",
            "mount",
            "insmod",
            "rmmod",
            "cat",
            "sleep",
            "yes",
            "kill",
            "sync",
            "poweroff",
            "uname",
        ]:
            (tree / "bin" / tool).symlink_to("busybox")
        shutil.copy2(a.module, tree / "workpulse.ko")
        (tree / "init").write_text(INIT)
        (tree / "init").chmod(0o755)
        initramfs = a.output / "initramfs.cpio.gz"
        newc(tree, initramfs)
        command = [
            a.qemu,
            "-accel",
            "tcg",
            "-m",
            "256",
            "-smp",
            "2",
            "-nographic",
            "-vga",
            "none",
            "-no-reboot",
            "-nic",
            "none",
            "-kernel",
            str(a.kernel),
            "-initrd",
            str(initramfs),
            "-append",
            "console=ttyS0 rdinit=/init panic=-1 quiet",
        ]
        if a.bios_dir:
            command.extend(["-L", str(a.bios_dir)])
        if a.bios:
            command.extend(["-bios", str(a.bios)])
        started = time.time()
        try:
            r = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=a.timeout,
            )
        except subprocess.TimeoutExpired as e:
            (a.output / "serial.log").write_bytes(e.stdout or b"")
            raise SystemExit("VM timed out; serial log preserved")
        (a.output / "serial.log").write_bytes(r.stdout)
    text = r.stdout.decode(errors="replace").replace("\r", "")
    samples = []
    groups = {}
    for line in text.splitlines():
        if line.startswith(("WP_SAMPLE:", "WP_LOAD_SAMPLE:", "WP_CYCLE_SAMPLE:")):
            kind, payload = line.split(":", 1)
            s = json.loads(payload)
            validate_sample(s)
            samples.append({"phase": kind, "sample": s})
            groups.setdefault(kind, []).append(s)
    assert "WP_PASS:" in text and "WP_FAIL:" not in text, text[-5000:]
    assert r.returncode == 0 and len(samples) == 32, (r.returncode, len(samples))
    for kind in ["WP_SAMPLE", "WP_LOAD_SAMPLE"]:
        values = groups[kind]
        for older, newer in zip(values, values[1:]):
            for key in [
                "samples",
                "sum_late_ns",
                "max_late_ns",
                "uptime_ns",
                "last_sample_ns",
            ]:
                assert newer[key] >= older[key]
            assert all(
                b >= a
                for a, b in zip(older["histogram_us_log2"], newer["histogram_us_log2"])
            )
    assert not any(
        word in text for word in ["BUG:", "Oops:", "KASAN:", "WARNING: CPU:"]
    ), "kernel diagnostic in serial log"
    report = {
        "ok": True,
        "elapsed_seconds": round(time.time() - started, 3),
        "accelerator": "tcg",
        "vcpus": 2,
        "memory_mib": 256,
        "kernel_sha256": hashlib.sha256(a.kernel.read_bytes()).hexdigest(),
        "module_sha256": hashlib.sha256(a.module.read_bytes()).hexdigest(),
        "samples": samples,
        "checks": [
            "invalid parameters",
            "read-only proc",
            "read-only parameter",
            "held-open unload rejected",
            "monotonic counters",
            "histogram sums",
            "VM load",
            "eight reload cycles",
            "no detected kernel diagnostic",
        ],
    }
    (a.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "ok": True,
                "sample_count": len(samples),
                "elapsed_seconds": report["elapsed_seconds"],
                "output": str(a.output),
            }
        )
    )


if __name__ == "__main__":
    main()
