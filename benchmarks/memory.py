"""Offline native pipeline benchmark; inputs are never modified.

Run with the project's Python, in a fresh process for each measurement:
    .venv/bin/python benchmarks/memory.py mux VIDEO AUDIO --temp-root /disk/tmp
    .venv/bin/python benchmarks/memory.py tag MP4 --temp-root /disk/tmp
    .venv/bin/python benchmarks/memory.py hls --concurrency 8 --repeats 3

HLS uses a separate localhost server process and discards received bytes.
Mux/tag use private temporary outputs, cleaned up even after failure.
Linux /proc supplies sampled RSS/anonymous/file PSS; ru_maxrss catches short peaks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import resource
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory


def memory():
    result = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in {"Rss", "Anonymous", "Pss_File"}:
            result[key] = int(value.split()[0])
    return result


def high_water():
    # Unlike ru_maxrss, VmHWM does not include an ancestor's pre-exec peak
    # retained by some process launchers.
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    return None


def serve(connection):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            number = int(self.path.strip("/"))
            remaining = (1, 3, 7, 11)[number % 4] * 1024 * 1024 + number
            self.send_response(200)
            self.send_header("Content-Length", str(remaining))
            self.end_headers()
            block = b"x" * 65536
            try:
                while remaining:
                    size = min(remaining, len(block))
                    self.wfile.write(block[:size])
                    remaining -= size
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        connection.send(server.server_port)
        connection.close()
        server.serve_forever()


async def transfer(port, args):
    from gamdl import _native

    stages = []
    for _ in range(args.repeats):
        stream = _native.NativeStream(
            [(f"http://127.0.0.1:{port}/{i}", None, None) for i in range(24)],
            args.concurrency,
        )
        received = 0
        try:
            while (chunk := await stream.next_chunk()) is not None:
                received += len(chunk)
                if args.consumer_delay:
                    await asyncio.sleep(args.consumer_delay)
        finally:
            stream.close()
        del stream, chunk
        await asyncio.sleep(0.05)
        stages.append({"bytes": received, "after_KiB": memory()})
    return stages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("mux", "tag", "hls"))
    parser.add_argument("inputs", nargs="*")
    parser.add_argument("--temp-root")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--consumer-delay", type=float, default=0)
    args = parser.parse_args()
    expected = {"mux": 2, "tag": 1, "hls": 0}[args.mode]
    if len(args.inputs) != expected:
        parser.error(f"{args.mode} expects {expected} input paths")

    from gamdl.download import Tags
    from gamdl.muxer import Muxer

    server = None
    with TemporaryDirectory(prefix="gamdl-benchmark-", dir=args.temp_root) as directory:
        output = Path(directory) / "output.m4v"
        if args.mode == "tag":
            shutil.copyfile(args.inputs[0], output)
        elif args.mode == "hls":
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            server = context.Process(target=serve, args=(child,), daemon=True)
            server.start()
            child.close()
            port = parent.recv()
            parent.close()

        baseline = memory()
        samples = [baseline]
        stop = threading.Event()

        def sample():
            while not stop.wait(0.001):
                samples.append(memory())

        monitor = threading.Thread(target=sample)
        monitor.start()
        started = time.perf_counter()
        cpu_started = time.process_time()
        try:
            if args.mode == "mux":
                Muxer().mux_file(
                    video=args.inputs[0], audio=args.inputs[1], destination=output
                )
                details = {"output_bytes": output.stat().st_size}
            elif args.mode == "tag":
                Muxer().tag(
                    output, tags=Tags(title="Memory benchmark: changed metadata")
                )
                details = {"output_bytes": output.stat().st_size}
            else:
                details = {"transfers": asyncio.run(transfer(port, args))}
        finally:
            elapsed = time.perf_counter() - started
            cpu = time.process_time() - cpu_started
            stop.set()
            monitor.join()
            if server is not None:
                server.terminate()
                server.join()
        print(
            json.dumps(
                {
                    "mode": args.mode,
                    "seconds": elapsed,
                    "cpu_seconds": cpu,
                    "maxrss_KiB": high_water(),
                    "rusage_maxrss_KiB": resource.getrusage(
                        resource.RUSAGE_SELF
                    ).ru_maxrss,
                    "baseline_KiB": baseline,
                    "peak_sample_KiB": max(samples, key=lambda s: s["Rss"]),
                    "after_KiB": memory(),
                    **details,
                }
            )
        )


if __name__ == "__main__":
    main()
