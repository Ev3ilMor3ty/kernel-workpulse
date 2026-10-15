#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Timed, checkpointed kernel lifecycle soak in a networkless QEMU VM.

Never loads a module on the host. A stopped/failed run is preserved as incomplete;
starting again requires a different output directory, not resuming a VM clock.
"""
import argparse, fcntl, gzip, hashlib, json, os, pathlib, selectors, shutil, signal, subprocess, tempfile, time
from run_vm import newc, validate_sample, ROOT

SCRIPT = r"""#!/bin/sh
export PATH=/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
fail() { echo "WP_SOAK_FAIL:$*"; poweroff -f; }
read up rest < /proc/uptime
start=${up%%.*}
i=0
cycles=0
reader1=0
reader2=0
load=0
start_readers() {
  (while true; do cat /proc/workpulse > /dev/null 2>&1; sleep 0.1; done) & reader1=$!
  (while true; do cat /proc/workpulse > /dev/null 2>&1; sleep 0.1; done) & reader2=$!
}
stop_readers() {
  kill "$reader1" "$reader2" 2>/dev/null
  wait "$reader1" "$reader2" 2>/dev/null
}
insmod /workpulse.ko period_ms=10 || fail "initial load"
start_readers
while true; do
  read up rest < /proc/uptime
  elapsed=$((${up%%.*}-start))
  [ "$elapsed" -ge __SECONDS__ ] && break
  sleep 5
  echo "WP_SOAK_SAMPLE:$(cat /proc/workpulse)"
  i=$((i+1))
  if [ $((i % 12)) -eq 1 ]; then
    yes > /dev/null & load=$!
  fi
  if [ $((i % 12)) -eq 2 ]; then
    kill "$load" 2>/dev/null
    wait "$load" 2>/dev/null
    load=0
  fi
  if [ $((i % 12)) -eq 0 ]; then
    # Race removal with short-lived readers. Either a safe success or EBUSY
    # is valid. Stop readers before requiring an eventual clean unload.
    if rmmod workpulse 2>/dev/null; then
      [ ! -e /proc/workpulse ] || fail "proc leak during reader race"
      insmod /workpulse.ko period_ms=10 || fail "race reload"
    fi
    stop_readers
    tries=0
    while ! rmmod workpulse 2>/dev/null; do
      tries=$((tries+1))
      [ "$tries" -ge 20 ] && fail "unload remained busy after readers stopped"
      sleep 0.1
    done
    [ ! -e /proc/workpulse ] || fail "proc leak"
    cycles=$((cycles+1))
    period=10
    [ $((cycles % 2)) -eq 1 ] && period=17
    insmod /workpulse.ko period_ms="$period" || fail "reload"
    start_readers
    echo "WP_SOAK_CYCLE:$cycles"
    echo "WP_MEMORY:$(/bin/busybox awk '/MemAvailable:/{print $2}' /proc/meminfo)"
  fi
done
stop_readers
[ "$load" -ne 0 ] && kill "$load" 2>/dev/null
tries=0
while ! rmmod workpulse 2>/dev/null; do
  tries=$((tries+1))
  [ "$tries" -ge 20 ] && fail "final unload"
  sleep 0.1
done
[ ! -e /proc/workpulse ] || fail "final proc leak"
echo "WP_SOAK_PASS:elapsed=$elapsed,reads=$i,cycles=$cycles"
poweroff -f
"""


def save(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", required=True, type=pathlib.Path)
    parser.add_argument(
        "--module", default=ROOT / "module/workpulse.ko", type=pathlib.Path
    )
    parser.add_argument("--busybox", required=True, type=pathlib.Path)
    parser.add_argument("--qemu", default="qemu-system-x86_64")
    parser.add_argument("--bios-dir", type=pathlib.Path)
    parser.add_argument("--bios", type=pathlib.Path)
    parser.add_argument("--seconds", type=int, default=14400)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    if not 60 <= args.seconds <= 86400:
        parser.error("duration must be 60..86400 seconds")
    args.output.mkdir(parents=True, exist_ok=True)
    lock = open(args.output / "run.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (args.output / "checkpoint.json").exists():
        raise SystemExit("Output already contains a run; choose a new directory")
    checkpoint = {
        "phase": "preparing",
        "requested_seconds": args.seconds,
        "started_epoch": time.time(),
        "readings": 0,
        "reload_cycles": 0,
        "complete": False,
        "kernel_sha256": hashlib.sha256(args.kernel.read_bytes()).hexdigest(),
        "module_sha256": hashlib.sha256(args.module.read_bytes()).hexdigest(),
        "module_source_sha256": hashlib.sha256(
            (ROOT / "module/workpulse.c").read_bytes()
        ).hexdigest(),
        "accelerator": "tcg",
        "vcpus": 2,
        "memory_mib": 256,
    }
    save(args.output / "checkpoint.json", checkpoint)
    process = None
    selector = selectors.DefaultSelector()
    previous = {}
    start = time.monotonic()
    last_output = start
    passed = False

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        with tempfile.TemporaryDirectory(prefix="workpulse-soak-") as temp:
            tree = pathlib.Path(temp) / "root"
            tree.mkdir()
            for directory in ["bin", "proc", "sys", "dev", "tmp"]:
                (tree / directory).mkdir()
            shutil.copy2(args.busybox, tree / "bin/busybox")
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
                "wait",
                "poweroff",
            ]:
                (tree / "bin" / tool).symlink_to("busybox")
            shutil.copy2(args.module, tree / "workpulse.ko")
            (tree / "init").write_text(SCRIPT.replace("__SECONDS__", str(args.seconds)))
            (tree / "init").chmod(0o755)
            initramfs = args.output / "initramfs.cpio.gz"
            newc(tree, initramfs)
            command = [
                args.qemu,
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
                str(args.kernel),
                "-initrd",
                str(initramfs),
                "-append",
                "console=ttyS0 rdinit=/init panic=-1 quiet",
            ]
            if args.bios_dir:
                command.extend(["-L", str(args.bios_dir)])
            if args.bios:
                command.extend(["-bios", str(args.bios)])
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0
            )
            selector.register(process.stdout, selectors.EVENT_READ)
            checkpoint.update(
                phase="running", qemu_pid=process.pid, runner_pid=os.getpid()
            )
            save(args.output / "checkpoint.json", checkpoint)
            print(
                json.dumps(
                    {
                        "phase": "running",
                        "qemu_pid": process.pid,
                        "output": str(args.output),
                    }
                ),
                flush=True,
            )
            buffer = b""
            with open(args.output / "serial.log", "wb", buffering=0) as serial, open(
                args.output / "samples.jsonl", "w", buffering=1
            ) as samples:
                while True:
                    if time.monotonic() - start > args.seconds + 150:
                        raise RuntimeError("VM exceeded bounded duration")
                    events = selector.select(1)
                    if not events:
                        if process.poll() is not None:
                            break
                        if time.monotonic() - last_output > 60:
                            raise RuntimeError(
                                "VM stopped producing progress for 60 seconds"
                            )
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    last_output = time.monotonic()
                    serial.write(chunk)
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.decode(errors="replace").strip()
                        if any(
                            token in line
                            for token in [
                                "WP_SOAK_FAIL:",
                                "BUG:",
                                "Oops:",
                                "KASAN:",
                                "WARNING: CPU:",
                            ]
                        ):
                            raise RuntimeError(
                                "VM reported a lifecycle or kernel failure: " + line
                            )
                        if line.startswith("WP_SOAK_SAMPLE:"):
                            sample = json.loads(line.split(":", 1)[1])
                            validate_sample(sample)
                            key = sample["started_ns"]
                            old = previous.get(key)
                            if old:
                                assert all(
                                    sample[k] >= old[k]
                                    for k in [
                                        "samples",
                                        "sum_late_ns",
                                        "max_late_ns",
                                        "uptime_ns",
                                        "last_sample_ns",
                                    ]
                                )
                                assert all(
                                    b >= a
                                    for a, b in zip(
                                        old["histogram_us_log2"],
                                        sample["histogram_us_log2"],
                                    )
                                )
                            previous[key] = sample
                            # Keep the in-memory history bounded across module reloads.
                            if len(previous) > 4:
                                previous.pop(next(iter(previous)))
                            samples.write(json.dumps(sample) + "\n")
                            checkpoint["readings"] += 1
                            checkpoint["last_sample"] = sample
                        elif line.startswith("WP_SOAK_CYCLE:"):
                            checkpoint["reload_cycles"] = int(line.split(":", 1)[1])
                        elif line.startswith("WP_MEMORY:"):
                            checkpoint["vm_mem_available_kib"] = int(
                                line.split(":", 1)[1]
                            )
                        elif line.startswith("WP_SOAK_PASS:"):
                            passed = True
                            checkpoint["vm_summary"] = line
                        checkpoint["elapsed_seconds"] = round(
                            time.monotonic() - start, 3
                        )
                        save(args.output / "checkpoint.json", checkpoint)
            process.wait(timeout=10)
            if not passed or process.returncode != 0:
                raise RuntimeError("VM ended without a successful soak completion")
            checkpoint.update(
                phase="completed",
                complete=True,
                elapsed_seconds=round(time.monotonic() - start, 3),
                finished_epoch=time.time(),
            )
    except BaseException as error:
        checkpoint.update(
            phase="incomplete",
            error=f"{type(error).__name__}: {error}",
            elapsed_seconds=round(time.monotonic() - start, 3),
        )
        raise
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        selector.close()
        save(args.output / "checkpoint.json", checkpoint)
        lock.close()
    print(
        json.dumps(
            {
                "phase": checkpoint["phase"],
                "readings": checkpoint["readings"],
                "cycles": checkpoint["reload_cycles"],
            }
        )
    )


if __name__ == "__main__":
    main()
