#!/usr/bin/env python3
import os
import re
import subprocess
import sys
import tempfile
import time


OUTPUT = "/var/lib/node_exporter/textfile_collector/vpp_ids_slab.prom"
COMMAND = ["/data/vpp/vppids/vppctl.sh", "show", "ids", "slab", "stats"]
NUMA_RE = re.compile(r"^numa id:\s*(\d+)")
PAGE_RE = re.compile(r"slab total page\s+(\d+),\s+free page\s+(\d+),")
SIZE_RE = re.compile(r"slab total size:\s+(\d+)\(MB\),\s+used\s+(\d+)\(MB\)")


def parse(output):
    samples = []
    current = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        match = NUMA_RE.match(line)
        if match:
            current = {"numa": match.group(1)}
            samples.append(current)
            continue
        if current is None:
            continue
        match = PAGE_RE.search(line)
        if match:
            current["total_pages"] = int(match.group(1))
            current["free_pages"] = int(match.group(2))
            continue
        match = SIZE_RE.search(line)
        if match:
            current["total_bytes"] = int(match.group(1)) * 1024 * 1024
            current["used_bytes"] = int(match.group(2)) * 1024 * 1024
    if not samples or any(len(item) != 5 for item in samples):
        raise ValueError("unable to parse VPP slab NUMA statistics")
    return samples


def metric(name, help_text, values):
    return "\n".join([f"# HELP {name} {help_text}", f"# TYPE {name} gauge", *values])


def main():
    directory = os.path.dirname(OUTPUT)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    timestamp = int(time.time())
    try:
        samples = None
        for attempt in range(3):
            result = subprocess.run(
                COMMAND,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            try:
                samples = parse(result.stdout)
                break
            except ValueError:
                if attempt == 2:
                    raise
                time.sleep(0.2)
        values = {name: [] for name in (
            "total_pages", "free_pages", "page_size_bytes", "total_bytes",
            "used_bytes", "free_page_bytes", "usage_ratio"
        )}
        for item in samples:
            label = f'{{numa="{item["numa"]}"}}'
            page_size = 128 * 1024
            values["total_pages"].append(f'vpp_ids_slab_total_pages{label} {item["total_pages"]}')
            values["free_pages"].append(f'vpp_ids_slab_free_pages{label} {item["free_pages"]}')
            values["page_size_bytes"].append(f'vpp_ids_slab_page_size_bytes{label} {page_size}')
            values["total_bytes"].append(f'vpp_ids_slab_total_bytes{label} {item["total_bytes"]}')
            values["used_bytes"].append(f'vpp_ids_slab_used_bytes{label} {item["used_bytes"]}')
            values["free_page_bytes"].append(f'vpp_ids_slab_free_page_bytes{label} {item["free_pages"] * page_size}')
            values["usage_ratio"].append(f'vpp_ids_slab_usage_ratio{label} {item["used_bytes"] / item["total_bytes"]:.8f}')
        names = {
            "total_pages": ("vpp_ids_slab_total_pages", "VPP IDS slab pages in the NUMA pool."),
            "free_pages": ("vpp_ids_slab_free_pages", "Free VPP IDS slab pages in the NUMA pool."),
            "page_size_bytes": ("vpp_ids_slab_page_size_bytes", "VPP IDS slab allocator page size in bytes."),
            "total_bytes": ("vpp_ids_slab_total_bytes", "Total VPP IDS slab size in bytes."),
            "used_bytes": ("vpp_ids_slab_used_bytes", "Used VPP IDS slab size in bytes."),
            "free_page_bytes": ("vpp_ids_slab_free_page_bytes", "Free VPP IDS slab page capacity in bytes."),
            "usage_ratio": ("vpp_ids_slab_usage_ratio", "Used VPP IDS slab ratio."),
        }
        blocks = [metric(names[key][0], names[key][1], values[key]) for key in names]
        blocks += [
            metric("vpp_ids_slab_scrape_success", "Whether the VPP IDS slab scrape succeeded.", ["vpp_ids_slab_scrape_success 1"]),
            metric("vpp_ids_slab_scrape_timestamp_seconds", "Unix timestamp of the VPP IDS slab scrape.", [f"vpp_ids_slab_scrape_timestamp_seconds {timestamp}"]),
        ]
        text = "\n".join(blocks) + "\n"
    except Exception as exc:
        debug_output = repr(result.stdout[:120]) if "result" in locals() else "<no result>"
        print("VPP IDS slab scrape failed: {} stdout={}".format(type(exc).__name__, debug_output), file=sys.stderr)
        text = "\n".join([
            metric("vpp_ids_slab_scrape_success", "Whether the VPP IDS slab scrape succeeded.", ["vpp_ids_slab_scrape_success 0"]),
            metric("vpp_ids_slab_scrape_timestamp_seconds", "Unix timestamp of the VPP IDS slab scrape.", [f"vpp_ids_slab_scrape_timestamp_seconds {timestamp}"]),
        ]) + "\n"

    fd, temporary = tempfile.mkstemp(prefix="vpp_ids_slab.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, OUTPUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    main()
