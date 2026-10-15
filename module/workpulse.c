// SPDX-License-Identifier: GPL-2.0-only
/* WorkPulse: bounded telemetry for a dedicated delayed-work callback.
 * No task interception, tracing hooks, network access or writable proc knobs.
 */
#include <linux/module.h>
#include <linux/workqueue.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/ktime.h>
#include <linux/spinlock.h>
#include <linux/log2.h>
#include <linux/jiffies.h>

#define WP_BUCKETS 24
#define WP_VERSION 1
static unsigned int period_ms = 100;
module_param(period_ms, uint, 0444);
MODULE_PARM_DESC(period_ms, "Sampling period in milliseconds (10..10000), read-only after load");

struct wp_stats {
    u64 samples;
    u64 sum_late_ns;
    u64 max_late_ns;
    u64 last_late_ns;
    u64 started_ns;
    u64 last_ns;
    u64 histogram[WP_BUCKETS];
};
static DEFINE_SPINLOCK(stats_lock);
static struct wp_stats stats;
static struct workqueue_struct *wp_queue;
static struct delayed_work wp_work;
static struct proc_dir_entry *wp_proc;
static u64 expected_ns;
static unsigned long delay_jiffies;
static bool stopping;

/* Bucket 0 covers [0,1] us; bucket n>=1 covers [2^n,2^(n+1)-1]
 * integer microseconds. The final bucket also includes all larger values.
 */
static unsigned int bucket_for(u64 ns)
{
    u64 us = div_u64(ns, 1000);
    return min_t(unsigned int, us ? ilog2(us) : 0, WP_BUCKETS - 1);
}

static void wp_sample(struct work_struct *unused)
{
    unsigned long flags;
    u64 now = ktime_get_ns();
    u64 late = now > expected_ns ? now - expected_ns : 0;
    bool again;
    (void)unused;
    spin_lock_irqsave(&stats_lock, flags);
    stats.samples++;
    /* At the minimum period, these u64 counters still take thousands of
     * years to overflow for ordinary delays. Saturate sum defensively. */
    if (U64_MAX - stats.sum_late_ns < late)
        stats.sum_late_ns = U64_MAX;
    else
        stats.sum_late_ns += late;
    stats.max_late_ns = max(stats.max_late_ns, late);
    stats.last_late_ns = late;
    stats.last_ns = now;
    stats.histogram[bucket_for(late)]++;
    expected_ns = now + jiffies_to_nsecs(delay_jiffies);
    again = !stopping;
    spin_unlock_irqrestore(&stats_lock, flags);
    if (again)
        queue_delayed_work(wp_queue, &wp_work, delay_jiffies);
}

static int wp_show(struct seq_file *m, void *unused)
{
    struct wp_stats copy;
    unsigned long flags;
    int i;
    (void)unused;
    spin_lock_irqsave(&stats_lock, flags);
    copy = stats;
    spin_unlock_irqrestore(&stats_lock, flags);
    seq_printf(m, "{\"schema\":%d,\"period_ms\":%u,\"effective_period_ns\":%llu,"
               "\"samples\":%llu,\"sum_late_ns\":%llu,\"max_late_ns\":%llu,"
               "\"last_late_ns\":%llu,\"started_ns\":%llu,\"uptime_ns\":%llu,\"last_sample_ns\":%llu,"
               "\"histogram_us_log2\":[",
               WP_VERSION, period_ms, jiffies_to_nsecs(delay_jiffies),
               copy.samples, copy.sum_late_ns, copy.max_late_ns,
               copy.last_late_ns, copy.started_ns, ktime_get_ns() - copy.started_ns, copy.last_ns);
    for (i = 0; i < WP_BUCKETS; i++)
        seq_printf(m, "%s%llu", i ? "," : "", copy.histogram[i]);
    seq_puts(m, "]}\n");
    return 0;
}

static int wp_open(struct inode *inode, struct file *file)
{
    int ret;
    if (!try_module_get(THIS_MODULE))
        return -ENODEV;
    ret = single_open(file, wp_show, NULL);
    if (ret)
        module_put(THIS_MODULE);
    return ret;
}
static int wp_release(struct inode *inode, struct file *file)
{
    int ret = single_release(inode, file);
    module_put(THIS_MODULE);
    return ret;
}
static const struct proc_ops wp_ops = {
    .proc_open = wp_open,
    .proc_read = seq_read,
    .proc_lseek = seq_lseek,
    .proc_release = wp_release,
};

static int __init wp_init(void)
{
    if (period_ms < 10 || period_ms > 10000)
        return -EINVAL;
    delay_jiffies = max_t(unsigned long, 1, msecs_to_jiffies(period_ms));
    stats.started_ns = ktime_get_ns();
    expected_ns = stats.started_ns + jiffies_to_nsecs(delay_jiffies);
    wp_queue = alloc_workqueue("workpulse", WQ_UNBOUND | WQ_FREEZABLE, 1);
    if (!wp_queue)
        return -ENOMEM;
    INIT_DELAYED_WORK(&wp_work, wp_sample);
    wp_proc = proc_create("workpulse", 0444, NULL, &wp_ops);
    if (!wp_proc) {
        destroy_workqueue(wp_queue);
        return -ENOMEM;
    }
    queue_delayed_work(wp_queue, &wp_work, delay_jiffies);
    pr_info("workpulse: ready, period=%ums, read /proc/workpulse\n", period_ms);
    return 0;
}
static void __exit wp_exit(void)
{
    unsigned long flags;
    spin_lock_irqsave(&stats_lock, flags);
    stopping = true;
    spin_unlock_irqrestore(&stats_lock, flags);
    cancel_delayed_work_sync(&wp_work);
    destroy_workqueue(wp_queue);
    proc_remove(wp_proc);
    pr_info("workpulse: stopped\n");
}
module_init(wp_init);
module_exit(wp_exit);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("Ev3ilMor3ty");
MODULE_DESCRIPTION("Read-only delayed-work lateness histogram for isolated Linux experiments");
MODULE_VERSION("0.1.0");
