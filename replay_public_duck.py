"""Reproduce HANDOFF_v3.md section 3: exact same-budget replay of StallPool vs per-game cap on the
public Duck-harness traces of Qwen3.8-Flash-Next (adsdemaybe/arc-agi-3, runs v1 and v2).

usage: python3 replay_public_duck.py            (downloads the two benchmark.json files, ~3 s CPU)
"""
import json
import urllib.request

from stallpool import records_from_duck, replay_report, traces_from_records

URL = "https://raw.githubusercontent.com/adsdemaybe/arc-agi-3/main/runs/kaggle_submission-{}/benchmark.json"

for run in ("v1", "v2"):
    with urllib.request.urlopen(URL.format(run)) as f:
        obj = json.load(f)
    traces = traces_from_records(records_from_duck(obj))
    full = sum(t.ps[-1][2] for t in traces) / len(traces)
    print(f"== Duck {run}: logged full-window RHAE {full:.2f}, levels {sum(t.ps[-1][0] for t in traces)}")
    for slots in (6, 8):
        print(replay_report(traces, slots=slots))
