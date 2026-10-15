# VM guide

## Boundary

The Python harness launches QEMU as an ordinary user. It supplies a kernel image and a generated initramfs containing a static BusyBox executable, `workpulse.ko` and a small init script. Module load/unload commands execute only inside that VM. Networking is disabled with `-nic none`; no host directory, block device or filesystem is shared.

TCG is used explicitly so the test does not require KVM access. VM latency observations reflect virtualization and host scheduling; they are not a native-system performance benchmark.

## Prerequisites

- A Linux x86-64 kernel image with module loading, procfs, devtmpfs, serial console and basic initramfs support.
- Exactly matching kernel headers and module-build tools.
- G++, Make and `pahole`/dwarves as required by the chosen headers.
- A static BusyBox executable with the applets used in `vm/run_vm.py`.
- QEMU system x86, Python 3.11+, and standard QEMU firmware/data files.

Build with:

```bash
make -C module KDIR=/path/to/matching/linux-headers
```

The build treats warnings as errors. Do not load a module built for an unrelated kernel. The harness catches a failed VM load and preserves the serial log.

## Short lifecycle test

```bash
python3 vm/run_vm.py \
  --kernel /path/to/vmlinuz \
  --busybox /path/to/static/busybox \
  --output .vm/lifecycle
```

Optional paths:

- `--module FILE`: module to place in the initramfs.
- `--qemu FILE`: QEMU executable.
- `--bios-dir DIRECTORY`: QEMU firmware/data search path.
- `--bios FILE`: explicit PC BIOS image for an extracted local toolchain.
- `--timeout SECONDS`: bounded host timeout, default 180 seconds.

The harness checks invalid periods, read-only data/parameter interfaces, unload refusal while a reader pins the module, telemetry consistency, monotonic counters, activity under competing VM work, eight reload cycles and absence of selected kernel fault diagnostics. It verifies all expected readings and a successful poweroff.

`serial.log`, `result.json` and the generated initramfs are preserved in the output directory. The result includes kernel/module hashes, VM resources and every captured sample. A passing test is finite evidence for that exact module and kernel, not a guarantee for other kernels or workloads.

## Longer soak

```bash
python3 vm/soak_vm.py \
  --kernel /path/to/vmlinuz \
  --busybox /path/to/static/busybox \
  --seconds 14400 \
  --output .vm/four-hour
```

Two VM reader loops repeatedly open/read/close the proc file. The test samples counters, applies short competing-work pulses and exercises removal with active readers. A safe removal or `EBUSY` during that race is valid; eventual unload after readers stop is required. The module is reloaded each cycle and its new start identity keeps histories separate.

The output directory is protected by an exclusive lock. Atomic `checkpoint.json` records phase, coverage, identities and progress. `samples.jsonl` and `serial.log` preserve evidence. The host runner bounds total duration, detects a stalled VM, and terminates only its own QEMU process on failure or interruption.

A finished result requires the VM completion marker and successful QEMU exit. Interrupted runs remain incomplete and are never resumed or counted as passed. Choose a fresh output directory for another run. Copy/freeze the module and harness before a long run if development will continue elsewhere.

## Guest-only manual inspection

For a disposable Linux guest you control, the relevant operations are:

```bash
insmod workpulse.ko period_ms=100
cat /proc/workpulse
rmmod workpulse
```

The automatic harness already performs these inside its VM. No host module load is necessary for the documented tests.
