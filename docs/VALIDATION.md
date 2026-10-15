# Validation

Completed local checks:

- Clean, warning-as-error module build against Debian `6.12.111+deb13-amd64` headers.
- Isolated QEMU TCG lifecycle test with two vCPUs and 256 MiB RAM: 32 captured telemetry readings and eight reload cycles.
- Invalid parameters rejected; proc data and the parameter remained read-only; a held-open reader prevented unload; eventual unload removed the proc entry.
- Every captured reading passed independent host checks and the Rust reader.
- Rust reader tests cover counts, bucket bounds, empty/open-ended histograms, malformed input, regressions and module start identity.
- A detached 65-second soak smoke completed, including live readers, one reload cycle and final cleanup.

See [the lifecycle result](evidence/vm-lifecycle.json) and [serial log](evidence/vm-lifecycle-serial.log) for the exact tested module hash and observations.

The separately frozen four-hour lifecycle soak completed successfully: **2,864 validated telemetry readings and 238 reload cycles**, with live reader races, competing-work pulses and final cleanup. See [the completed summary](evidence/four-hour-soak.json) and [all captured samples](evidence/four-hour-samples.jsonl). Results apply to the recorded kernel/module identities; a changed module does not inherit this result.

Development caught and corrected a telemetry format-argument mismatch. Warnings are now build errors, and host validation checks the relationship between module start time, sample time, uptime and populated histogram buckets. Earlier prototype outputs are not presented as validation of the corrected module.

These observations concern this module's callback scheduling inside a VM. They do not establish native kernel latency, real-time guarantees, general driver safety or support for every Linux kernel.
