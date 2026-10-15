// SPDX-License-Identifier: MIT
use workpulse_reader::Sample;
fn example() -> Sample {
    let mut histogram = vec![0; 24];
    histogram[0] = 2;
    histogram[3] = 8;
    Sample {
        schema: 1,
        period_ms: 100,
        effective_period_ns: 100_000_000,
        samples: 10,
        sum_late_ns: 100_000,
        max_late_ns: 15_000,
        last_late_ns: 8_000,
        started_ns: 49_000_000_000,
        uptime_ns: 2_000_000_000,
        last_sample_ns: 50_000_000_000,
        histogram_us_log2: histogram,
    }
}
#[test]
fn round_trip_and_quantile_bounds() {
    let s = example();
    let parsed = Sample::parse(&serde_json::to_vec(&s).unwrap()).unwrap();
    assert_eq!(parsed.quantile_upper_us(10), Some(2));
    assert_eq!(parsed.quantile_upper_us(50), Some(16));
    assert_eq!(parsed.quantile_upper_us(99), Some(16));
    assert_eq!(parsed.quantile_upper_us(0), None);
}
#[test]
fn rejects_corrupt_counts_unknown_fields_and_oversize() {
    let mut s = example();
    s.samples = 9;
    assert!(Sample::parse(&serde_json::to_vec(&s).unwrap()).is_err());
    assert!(Sample::parse(br#"{"surprise":true}"#).is_err());
    assert!(Sample::parse(&vec![b' '; 16385]).is_err());
    s = example();
    s.last_late_ns = 16_000;
    assert!(Sample::parse(&serde_json::to_vec(&s).unwrap()).is_err());
}
#[test]
fn handles_zero_and_open_ended_histograms() {
    let mut s = example();
    s.samples = 0;
    s.sum_late_ns = 0;
    s.max_late_ns = 0;
    s.last_late_ns = 0;
    s.last_sample_ns = 0;
    s.histogram_us_log2.fill(0);
    assert_eq!(s.quantile_upper_us(99), None);
    assert!(Sample::parse(&serde_json::to_vec(&s).unwrap()).is_ok());
    s.histogram_us_log2[23] = 1;
    s.samples = 1;
    s.max_late_ns = u64::MAX;
    s.sum_late_ns = u64::MAX;
    assert_eq!(s.quantile_upper_us(99), None);
}
#[test]
fn detects_counter_regression() {
    let s = example();
    let mut newer = s.clone();
    newer.uptime_ns += 1;
    assert!(newer.follows(&s));
    newer.histogram_us_log2[0] -= 1;
    newer.histogram_us_log2[3] += 1;
    assert!(!newer.follows(&s));
    newer = s.clone();
    newer.started_ns += 1;
    assert!(!newer.follows(&s));
}
