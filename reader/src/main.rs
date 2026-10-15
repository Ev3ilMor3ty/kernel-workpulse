// SPDX-License-Identifier: MIT
use std::{
    fs,
    io::{self, Read},
    thread,
    time::Duration,
};
use workpulse_reader::Sample;
fn run() -> Result<(), String> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let path = args.first().map_or("/proc/workpulse", String::as_str);
    let count: usize = args
        .get(1)
        .map_or(Ok(1), |s| s.parse())
        .map_err(|_| "count must be an integer")?;
    if count == 0 || count > 10000 {
        return Err("count must be in 1..10000".into());
    }
    if path == "-" && count != 1 {
        return Err("stdin mode supports one sample".into());
    }
    let mut previous: Option<Sample> = None;
    for i in 0..count {
        let mut bytes = Vec::new();
        if path == "-" {
            io::stdin()
                .take(16385)
                .read_to_end(&mut bytes)
                .map_err(|e| e.to_string())?;
        } else {
            fs::File::open(path)
                .map_err(|e| e.to_string())?
                .take(16385)
                .read_to_end(&mut bytes)
                .map_err(|e| e.to_string())?;
        }
        let sample = Sample::parse(&bytes)?;
        if previous.as_ref().is_some_and(|p| !sample.follows(p)) {
            return Err("counters regressed or module restarted".into());
        }
        println!(
            "{}",
            serde_json::json!({"sample":sample,"mean_late_us":if sample.samples==0 {0.0}else{sample.sum_late_ns as f64/sample.samples as f64/1000.0},"p50_bucket_upper_us":sample.quantile_upper_us(50),"p99_bucket_upper_us":sample.quantile_upper_us(99),"quantiles":"exclusive histogram bucket upper bounds; null means empty or open-ended"})
        );
        previous = Some(sample);
        if i + 1 < count {
            thread::sleep(Duration::from_secs(1));
        }
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("workpulse-reader: {e}");
        std::process::exit(1)
    }
}
