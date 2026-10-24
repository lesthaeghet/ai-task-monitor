"""Replay a recorded JSON event stream slowly, as if an agent were producing it.
Lets you try the agent transcript view without running (or paying for) a real agent.

    taskmon run -n agent-demo -- python examples/replay.py examples/claude_stream.jsonl
"""
import sys
import time

delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.8
with open(sys.argv[1], encoding="utf-8") as f:
    for line in f:
        sys.stdout.write(line)
        sys.stdout.flush()
        time.sleep(delay)
