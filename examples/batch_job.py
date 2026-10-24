"""A fake long-running batch job: progress bar, checkpoints and the odd warning.

    taskmon run -n batch -- python examples/batch_job.py
"""
import random
import sys
import time

random.seed(4)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 240
print(f"[{time.strftime('%H:%M:%S')}] loading {N} orders", flush=True)
for i in range(1, N + 1):
    time.sleep(0.2)
    pct = i * 100 // N
    sys.stdout.write(f"\rprocessing [{'#' * (pct // 5):<20}] {pct}% ({i}/{N})")
    sys.stdout.flush()
    if i % 40 == 0:
        print(f"\n[{time.strftime('%H:%M:%S')}] checkpoint: {i} orders written to report.csv", flush=True)
    if random.random() < 0.03:
        print(f"\nwarning: order {1000 + i} has no shipping address, skipped", flush=True)
print(f"\n[{time.strftime('%H:%M:%S')}] done: report.csv ({N} rows)")
