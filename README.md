# WorkPulse

**A Linux workqueue latency sampler, a Rust reader, and an isolated VM test bench.**

WorkPulse measures how late its own delayed-work callback starts relative to a monotonic scheduled deadline. The kernel module exposes bounded counters and a logarithmic histogram through a read-only `/proc/workpulse` file. Rust validates each snapshot and reports approximate percentile bucket bounds.

It has no task hooks, credential access, network channel or writable runtime controls. The code is small enough to inspect and the lifecycle is tested in a networkless QEMU VM.

```text
scheduled deadline → dedicated workqueue → monotonic lateness sample
                                            │
                                  spinlock-protected counters
                                            │
                                read-only /proc/workpulse
                                            │
                                   validated Rust reader
```

## What is actually measured?

For each callback, lateness is `max(0, callback_start - expected_deadline)`. The next deadline is relative to the current callback, using the effective jiffy-rounded period. The module measures its own delayed-work scheduling behavior, not every process's scheduler latency, interrupt latency, application response time or CPU utilisation.

The default period is 100 ms. `period_ms` is accepted only at module load, between 10 and 10,000 ms, and is read-only afterward. The telemetry includes both the requested period and effective jiffy-rounded period.

The histogram has 24 buckets of integer microseconds. Bucket 0 contains integer values 0 and 1; bucket `n >= 1` contains `2^n .. 2^(n+1)-1`. The final bucket is open-ended. Reported percentile bounds are **exclusive upper bounds**, not exact percentile measurements. A null bound means an empty sample or the open-ended bucket.

## Build the reader

```bash
cargo build --release --locked --manifest-path reader/Cargo.toml
cargo test --locked --manifest-path reader/Cargo.toml
```

Read a saved telemetry JSON file:

```bash
./reader/target/release/workpulse-reader examples/sample.json
```

Inside a Linux test VM with the module loaded:

```bash
./reader/target/release/workpulse-reader /proc/workpulse 10
```

The optional count is 1..10,000; successive reads are one second apart. `-` reads one JSON snapshot from stdin. Regressing counters or a changed module start identifier are rejected.

## Build and run the module in a disposable VM

Use a matching kernel image and headers for the VM. **The harness never runs host `insmod` or `rmmod`.** It boots an initramfs with BusyBox, the module, no network interface, no shared host directory and no disk image.

On Ubuntu 24.04:

```bash
sudo apt update
sudo apt install build-essential linux-image-generic linux-headers-generic busybox-static qemu-system-x86 dwarves python3
```

On Debian 13, install `linux-image-amd64` and `linux-headers-amd64` instead of the two `-generic` packages. Select one installed kernel version for both the headers and image:

```bash
KERNEL_VERSION=$(find /boot -maxdepth 1 -name 'vmlinuz-*' -printf '%f\n' | sed 's/vmlinuz-//' | sort -V | tail -1)
test -d "/usr/src/linux-headers-$KERNEL_VERSION"
make -C module KDIR="/usr/src/linux-headers-$KERNEL_VERSION"
python3 vm/run_vm.py \
  --kernel "/boot/vmlinuz-$KERNEL_VERSION" \
  --busybox /bin/busybox \
  --output .vm/check
```

The `KDIR` must match the VM image, not necessarily the running host kernel. The checked local experiment uses Debian **6.12.111+deb13-amd64** and QEMU TCG with two virtual CPUs and 256 MiB RAM. A compiled `.ko` is kernel-version-specific; it is not a universal installer.

See [the VM guide](docs/VM_GUIDE.md) for toolchain paths, logs, lifecycle checks and bounded soak runs. See [measured results](docs/VALIDATION.md) for completed checks and the current endurance-test status.

## Design details

- A dedicated unbound, freezable workqueue with one active callback.
- Constant-size counters and 24 histogram buckets; no per-event kernel allocation.
- A short spinlock critical section; `seq_file` formatting happens on a copied snapshot outside the lock.
- Read-only proc data and module parameter.
- Open readers pin the module. Unload synchronously cancels work before releasing resources.
- The Rust parser checks schema, counter sums, histogram consistency, monotonicity and bounded input.

The related [Systems Foundry repository](https://github.com/Ev3ilMor3ty/systems-foundry) contains the Java/C++/Rust/HTML operations demo. WorkPulse is separate because kernel builds and module lifecycle need their own environment and test boundary.

## License

`module/`: **GPL-2.0-only**. `reader/` and `vm/`: **MIT**. Complete license texts are under `LICENSES/`; upstream kernel and BusyBox licenses apply to those external tools. The repository contains their test instructions and hashes, not a repackaged Linux distribution.
