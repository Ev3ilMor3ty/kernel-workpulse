// SPDX-License-Identifier: MIT
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Sample {
    pub schema: u32,
    pub period_ms: u32,
    pub effective_period_ns: u64,
    pub samples: u64,
    pub sum_late_ns: u64,
    pub max_late_ns: u64,
    pub last_late_ns: u64,
    pub started_ns: u64,
    pub uptime_ns: u64,
    pub last_sample_ns: u64,
    pub histogram_us_log2: Vec<u64>,
}
impl Sample {
    pub fn parse(bytes: &[u8]) -> Result<Self, String> {
        if bytes.len() > 16_384 {
            return Err("telemetry exceeds 16 KiB".into());
        }
        let s: Self = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
        if s.schema != 1
            || !(10..=10000).contains(&s.period_ms)
            || s.effective_period_ns == 0
            || s.histogram_us_log2.len() != 24
        {
            return Err("unsupported schema or invalid period/histogram shape".into());
        }
        let total = s
            .histogram_us_log2
            .iter()
            .try_fold(0u64, |a, b| a.checked_add(*b))
            .ok_or("histogram overflow")?;
        if total != s.samples
            || s.last_late_ns > s.max_late_ns
            || (s.samples == 0
                && (s.sum_late_ns != 0 || s.max_late_ns != 0 || s.last_sample_ns != 0))
        {
            return Err("inconsistent telemetry counters".into());
        }
        if s.samples > 0
            && (s.sum_late_ns < s.max_late_ns
                || s.last_sample_ns < s.started_ns
                || s.last_sample_ns
                    > s.started_ns
                        .checked_add(s.uptime_ns)
                        .ok_or("uptime overflow")?)
        {
            return Err("invalid lateness total or sample time".into());
        }
        if s.samples > 0 {
            let bucket = |ns: u64| {
                let us = ns / 1000;
                if us == 0 {
                    0
                } else {
                    (63 - us.leading_zeros() as usize).min(23)
                }
            };
            let highest = s.histogram_us_log2.iter().rposition(|c| *c > 0).unwrap();
            if bucket(s.max_late_ns) != highest || s.histogram_us_log2[bucket(s.last_late_ns)] == 0
            {
                return Err("lateness outside populated histogram buckets".into());
            }
        }
        Ok(s)
    }
    /// Exclusive upper bound of the bucket containing the quantile, in us.
    /// The last bucket is open-ended and returns None rather than a false bound.
    pub fn quantile_upper_us(&self, percent: u32) -> Option<u64> {
        if self.samples == 0 || percent == 0 || percent > 100 {
            return None;
        }
        let rank = ((self.samples as u128 * percent as u128) + 99) / 100;
        let mut cumulative = 0u128;
        for (i, count) in self.histogram_us_log2.iter().enumerate() {
            cumulative += *count as u128;
            if cumulative >= rank {
                return if i == 23 { None } else { Some(1u64 << (i + 1)) };
            }
        }
        None
    }
    pub fn follows(&self, previous: &Self) -> bool {
        self.started_ns == previous.started_ns
            && self.period_ms == previous.period_ms
            && self.effective_period_ns == previous.effective_period_ns
            && self.uptime_ns >= previous.uptime_ns
            && self.samples >= previous.samples
            && self.sum_late_ns >= previous.sum_late_ns
            && self.max_late_ns >= previous.max_late_ns
            && self.last_sample_ns >= previous.last_sample_ns
            && self
                .histogram_us_log2
                .iter()
                .zip(&previous.histogram_us_log2)
                .all(|(a, b)| a >= b)
    }
}
