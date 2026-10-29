"""Sunspot Node proof of concept: an energy-aware AI runtime on a laptop battery.

The laptop battery stands in for the node's LiFePO4 battery, and the charger
stands in for solar surplus. The script shows three behaviours of the runtime:

  1. Battery ladder  - the model in use steps down as charge falls.
  2. Surplus window  - a batch job runs only while plugged in and nearly full,
                       and pauses as soon as the charger is pulled.
  3. Safe stops      - each result is written atomically, a restart skips
                       finished work, and at the reserve level the runtime
                       unloads its model and exits.

It does not show solar, forecasting, the full node's hardware, the pool, or
setup by non-experts.

Usage
  python sunspot_poc.py run --input data/book.txt
  python sunspot_poc.py plot

--input is any long plain-text file. It is split into chunks and the batch job
summarises each one. Everything goes in ./sunspot-run, or the folder given with
--workdir: out/ holds one file per finished chunk, log.csv one row per event,
and `plot` prints a summary and writes ladder.png.

README.md has the setup steps and the demonstration that produced results/test-a.
"""

import argparse
import csv
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

OLLAMA = "http://localhost:11434"
WORKDIR = "sunspot-run"
LOG_FIELDS = ["time", "charge_pct", "plugged", "watts", "model", "event", "chunks_done", "tok_per_s"]
PROBE_PROMPT = ("In two sentences, explain why a battery-powered computer "
                "should save its work before the battery runs out.")
BATCH_PROMPT = "Summarise this passage in three sentences:\n\n"


# Ollama ---------------------------------------------------------------------

def ollama(path, payload=None):
    """GET (no payload) or POST JSON to the local Ollama server."""
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(OLLAMA + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)


def generate(model, prompt, max_tokens):
    """Run one prompt to completion; return (text, generation tokens per second)."""
    r = ollama("/api/generate", {"model": model, "prompt": prompt, "stream": False,
                                 "options": {"num_predict": max_tokens}})
    secs = r.get("eval_duration", 0) / 1e9
    return r["response"], (r.get("eval_count", 0) / secs if secs else 0.0)


def unload(model):
    ollama("/api/generate", {"model": model, "keep_alive": 0, "stream": False})


def check_models(models):
    try:
        have = {m["name"] for m in ollama("/api/tags")["models"]}
    except urllib.error.URLError:
        sys.exit("Ollama is not running. Open the Ollama app or run `ollama serve`, then try again.")
    have |= {name.removesuffix(":latest") for name in have}
    missing = [m for m in models if m not in have]
    if missing:
        sys.exit("Pull these models first:\n" + "\n".join(f"  ollama pull {m}" for m in missing))


# Power ----------------------------------------------------------------------

def battery():
    """Return (charge %, plugged in)."""
    import psutil  # imported here so `plot` runs without it
    b = psutil.sensors_battery()
    if b is None:
        sys.exit("No battery found. Run this on a laptop.")
    return float(b.percent), bool(b.power_plugged)


def watts():
    """Battery discharge rate in watts as the laptop reports it, or None if it doesn't.

    The firmware smooths this over several seconds, so treat it as an average.
    It reads 0 while plugged in, because the battery isn't discharging.
    """
    if platform.system() == "Linux":
        for bat in ("BAT0", "BAT1"):
            try:
                with open(f"/sys/class/power_supply/{bat}/power_now") as f:
                    return int(f.read()) / 1e6  # microwatts
            except (OSError, ValueError):
                pass
    elif platform.system() == "Windows":
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
               "(Get-CimInstance -Namespace root/wmi -ClassName BatteryStatus"
               " | Select-Object -First 1).DischargeRate"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
            return int(out) / 1000 if out else None  # milliwatts
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return None


# Runtime --------------------------------------------------------------------

def pick_model(charge, floors, models):
    """The largest model whose floor the charge is at or above; None is the reserve band."""
    for floor, model in zip(floors, models):
        if charge >= floor:
            return model
    return None


def split_chunks(path, size=3000):
    """Split a text file into chunks of about `size` characters at paragraph breaks."""
    with open(path, encoding="utf-8", errors="replace") as f:
        paras = [p.strip() for p in f.read().split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) > size:
            chunks.append(cur)
            cur = ""
        cur += p + "\n\n"
    if cur:
        chunks.append(cur)
    return chunks


def write_atomic(path, text):
    """Write via a temp file and rename, so a power cut leaves the old file or the new one, never half of one."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def run(args):
    models = args.models.split(",")
    floors = [float(x) for x in args.floors.split(",")]
    if len(models) != len(floors) or floors != sorted(floors, reverse=True):
        sys.exit("--models and --floors need the same number of items, largest model and highest floor first.")
    check_models(models)

    out_dir = os.path.join(args.workdir, "out")
    os.makedirs(out_dir, exist_ok=True)
    chunks = split_chunks(args.input)
    out_path = [os.path.join(out_dir, f"chunk_{i:05d}.txt") for i in range(len(chunks))]
    todo = [i for i in range(len(chunks)) if not os.path.exists(out_path[i])]
    current, batch_on, last_probe = None, False, 0.0

    def record(event, tok_s=""):
        charge, plugged = battery()
        w = watts()
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "charge_pct": charge, "plugged": int(plugged),
               "watts": "" if w is None else round(w, 2), "model": current or "", "event": event,
               "chunks_done": len(chunks) - len(todo), "tok_per_s": tok_s}
        log_path = os.path.join(args.workdir, "log.csv")
        is_new = not os.path.exists(log_path)
        with open(log_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)
        print(f"{row['time']}  {charge:5.1f}%  {'plugged' if plugged else 'battery'}  "
              f"{current or '-':<12} {event}  [{row['chunks_done']}/{len(chunks)} chunks]")

    record(f"start: {len(todo)} of {len(chunks)} chunks left")
    try:
        while True:
            try:
                charge, plugged = battery()
                model = pick_model(charge, floors, models)

                if model != current:
                    if current:
                        unload(current)
                    current = model
                    record("switch" if model else "reserve: model unloaded")

                if model is None:
                    if not plugged:
                        record("shutdown: finished work is on disk")
                        return
                    time.sleep(args.tick)  # charging back out of the reserve band
                    continue

                window = plugged and charge >= args.surplus
                if todo and window != batch_on:
                    batch_on = window
                    record("batch running" if window else "batch paused")

                if time.time() - last_probe >= args.probe_every:
                    _, tok_s = generate(current, PROBE_PROMPT, 128)
                    last_probe = time.time()
                    record("probe", round(tok_s, 1))

                if batch_on and todo:
                    i = todo[0]
                    text, tok_s = generate(current, BATCH_PROMPT + chunks[i], 200)
                    write_atomic(out_path[i], text)
                    todo.pop(0)
                    record("chunk done", round(tok_s, 1))
                    if not todo:
                        record("batch finished")
                    continue  # re-check power before the next chunk

                record("tick")
                time.sleep(args.tick)
            except OSError as e:  # includes Ollama connection errors and timeouts
                record(f"error: {e}")
                time.sleep(args.tick)
    except KeyboardInterrupt:
        record("stopped by user")


# Plot -----------------------------------------------------------------------

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]  # fixed order, by first appearance
INK, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7", "#fcfcfb"


def plot(args):
    log_path = os.path.join(args.workdir, "log.csv")
    if not os.path.exists(log_path):
        sys.exit(f"No {log_path} yet. Run `python sunspot_poc.py run --input <file>` first.")
    with open(log_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"{log_path} is empty.")
    for r in rows:
        r["t"] = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S")
    models = list(dict.fromkeys(r["model"] for r in rows if r["model"]))

    # Per-model figures come from the last stretch on battery, after the charger was last pulled
    last_plugged = max((i for i, r in enumerate(rows) if r["plugged"] == "1"), default=-1)
    drain = rows[last_plugged + 1:]
    if drain:
        mins = (drain[-1]["t"] - drain[0]["t"]).total_seconds() / 60
        print(f"On battery from {drain[0]['time']} to {drain[-1]['time'][11:]}: "
              f"{float(drain[0]['charge_pct']):.0f}% to {float(drain[-1]['charge_pct']):.0f}% in {mins:.1f} min\n")
        print(f"{'Model':<14}{'Probes':>7}{'Tok/s':>8}{'Watts*':>8}{'Minutes':>9}")
        for m in models:
            seg = [r for r in drain if r["model"] == m]
            probes = [float(r["tok_per_s"]) for r in seg if r["event"] == "probe" and r["tok_per_s"]]
            draw = [float(r["watts"]) for r in seg if r["watts"]]
            tok = f"{sum(probes) / len(probes):.1f}" if probes else "-"
            wat = f"{sum(draw) / len(draw):.1f}" if draw else "-"
            span = f"{(seg[-1]['t'] - seg[0]['t']).total_seconds() / 60:.1f}" if seg else "-"
            print(f"{m:<14}{len(probes):>7}{tok:>8}{wat:>8}{span:>9}")
        print("* average laptop draw while this model was in use, as the firmware reports it\n")
    else:
        print("No time on battery after the charger was last connected, so no per-model figures.\n")

    out_dir = os.path.join(args.workdir, "out")
    files = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
    done = [f for f in files if f.endswith(".txt")]
    in_order = done == [f"chunk_{i:05d}.txt" for i in range(len(done))]
    empty = sum(os.path.getsize(os.path.join(out_dir, f)) == 0 for f in done)
    tmp = sum(f.endswith(".tmp") for f in files)
    errors = sum(r["event"].startswith("error") for r in rows)
    print(f"Chunks on disk: {len(done)} (log says {rows[-1]['chunks_done']}), numbered in order: "
          f"{'yes' if in_order else 'no'}, empty: {empty}, temporary files: {tmp}, errors logged: {errors}\n")

    try:
        import matplotlib
    except ImportError:
        sys.exit("For the chart, install matplotlib: pip install matplotlib")
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    from matplotlib.ticker import MaxNLocator

    t = [r["t"] for r in rows]
    charge = [float(r["charge_pct"]) for r in rows]
    color = dict(zip(models, SERIES))

    fig, (top, bottom) = plt.subplots(2, 1, sharex=True, figsize=(9, 5.4), facecolor=SURFACE,
                                      gridspec_kw={"height_ratios": [2.2, 1]})
    top.fill_between(t, 0, 1, where=[r["plugged"] == "1" for r in rows], step="post",
                     color=AXIS, alpha=0.35, linewidth=0, transform=top.get_xaxis_transform())
    start = 0  # draw the charge line in runs, coloured by the model in use
    for i in range(1, len(rows) + 1):
        if i == len(rows) or rows[i]["model"] != rows[start]["model"]:
            end = min(i + 1, len(rows))  # include the next point so runs meet
            top.plot(t[start:end], charge[start:end], color=color.get(rows[start]["model"], MUTED), lw=2,
                     drawstyle="steps-post")
            start = i
    for r in rows[1:]:
        if r["event"].startswith("start:"):  # the process was stopped and started again
            for ax in (top, bottom):
                ax.axvline(r["t"], color=MUTED, lw=0.8, ls=":")
            top.text(r["t"], 0.03, " restarted", transform=top.get_xaxis_transform(), color=MUTED, fontsize=9)
    bottom.step(t, [int(r["chunks_done"]) for r in rows], where="post", color=MUTED, lw=2)

    for ax in (top, bottom):
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, lw=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(AXIS)
        ax.tick_params(colors=MUTED)
    top.set_ylim(max(0, min(charge) - 2), 101)
    top.set_ylabel("Battery charge (%)", color=MUTED)
    handles = [Line2D([], [], color=color[m], lw=2, label=m) for m in models]
    handles.append(Patch(facecolor=AXIS, alpha=0.35, label="Charger connected"))
    top.legend(handles=handles, frameon=False, labelcolor=INK, loc="lower left", bbox_to_anchor=(0, 1),
               ncol=len(handles), fontsize=9, borderaxespad=0.2, handlelength=1.6, columnspacing=1.4)
    bottom.set_ylabel("Chunks done", color=MUTED)
    bottom.yaxis.set_major_locator(MaxNLocator(nbins=4, integer=True))
    bottom.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    out = os.path.join(args.workdir, "ladder.png")
    fig.tight_layout()
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    print("Wrote", out)


def main():
    p = argparse.ArgumentParser(description="Sunspot Node proof of concept: an energy-aware runtime on a laptop battery.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the energy-aware runtime")
    r.add_argument("--input", required=True, help="long plain-text file for the batch job")
    r.add_argument("--models", default="gemma3:4b,gemma3:1b,gemma3:270m",
                   help="ladder models, largest first (default: %(default)s)")
    r.add_argument("--floors", default="65,35,15",
                   help="lowest charge %% for each model, matching --models; below the last is reserve (default: %(default)s)")
    r.add_argument("--surplus", type=float, default=90,
                   help="batch runs only when plugged in and at or above this charge %%; "
                        "set it lower if your laptop stops charging at 80%% (default: %(default)s)")
    r.add_argument("--tick", type=float, default=10, help="seconds between power checks (default: %(default)s)")
    r.add_argument("--probe-every", type=float, default=300,
                   help="seconds between test prompts that measure speed (default: %(default)s)")
    pl = sub.add_parser("plot", help="print a per-model summary and chart log.csv to ladder.png")
    for s in (r, pl):
        s.add_argument("--workdir", default=WORKDIR,
                       help="folder for the log, finished chunks and chart (default: %(default)s)")
    args = p.parse_args()
    if args.cmd == "run":
        run(args)
    else:
        plot(args)


if __name__ == "__main__":
    main()
