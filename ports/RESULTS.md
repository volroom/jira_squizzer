# Port comparison

These were produced by `python3 ports/bench.py --ts5 <path to a TypeScript 5 tsc>`, and the raw numbers are in `results.json`.
Each figure is the median of 5 runs, except cold builds, which are the median of 2. The machine has 8 cores and runs Linux 7.2.

Versions: Go 1.25.3, Rust 1.94.1, Python 3.13.5 (uv 0.12.15), Node 24.14.1, with TypeScript 7.0.2 (the native compiler) and 5.9.3 (the classic JavaScript one).

## Code

| | Go | Rust | Python | TypeScript |
|---|---|---|---|---|
| App code lines (no blanks or comments) | 778 | 1,038 | 711 | 666 |
| Test code lines | 124 | 198 | 82 | 82 |
| App code characters | 19.7k | 27.4k | 21.5k | 22.1k |
| Direct / total dependencies | 2 / 16 | 4 / 189 | 2 / 14 | 8 / 65 (42 at runtime) |

## Build

Each cell shows wall time / CPU time / peak memory.

| | Wall time | CPU time | Peak memory |
|---|---|---|---|
| Go, cold (empty build cache, including the standard library) | 11.7 s | 61.0 s | 228 MB |
| Go, after a one-line edit | 0.57 s | 1.15 s | 170 MB |
| Rust debug, cold | 19.1 s | 117 s | 482 MB |
| Rust debug, after a one-line edit | 0.52 s | 0.86 s | 253 MB |
| Rust release, cold | 38.4 s | 241 s | 508 MB |
| Rust release, after a one-line edit | 4.05 s | 13.6 s | 364 MB |
| TypeScript, `tsc` 7 (native), full build | 0.16 s | 0.66 s | 108 MB |
| TypeScript, `tsc` 5 (classic), full build | 1.39 s | 2.94 s | 352 MB |
| Python, no compile step (`compileall` takes 0.05 s) | n/a | n/a | n/a |

Installing dependencies from a warm package cache: `uv sync` into a fresh venv takes 0.04 s, and `npm ci` takes 1.65 s (2.5 s of CPU, 247 MB).

## Tests (5 ported test cases each)

| | Run only | Edit, rebuild and run |
|---|---|---|
| Go (`go test -count=1`, which relinks every time) | 0.39 s | 0.51 s |
| Rust (`cargo test`) | 0.13 s | 0.46 s |
| Python (`pytest`) | 0.47 s | n/a (same as run) |
| TypeScript (`node --test`), built with `tsc` 7 | 0.17 s | 0.45 s |
| TypeScript (`node --test`), built with `tsc` 5 | 0.17 s | 1.55 s |

## Running the app

The app ran in a 140×40 pseudo-terminal against a fake Jira holding 40 tickets.

| | Go | Rust (release) | Python | TypeScript |
|---|---|---|---|---|
| Startup until the board is drawn | 48 ms | 11 ms | 287 ms | 467 ms |
| Memory while idle on the board | 17.2 MB | 4.7 MB | 41.5 MB | 145 MB |
| Peak memory | 40.5 MB | 4.7 MB | 42.6 MB | 206 MB |
| CPU while idle | 1.7 % | 0 % | 0 % | 0 % |
| CPU for 160 navigation key presses | 0.65 s | 0.37 s | 1.22 s | 1.30 s |
| Time to quit | 35 ms | 2 ms | 218 ms | 95 ms |
| What you ship | 10.3 MB binary | 4.2 MB binary | 10.1 MB venv + Python | 10.1 MB node_modules + 30 KB dist + Node |

## Caveats

- **Cold builds:** these start with the dependencies already downloaded, using a temporary `GOCACHE` or `CARGO_TARGET_DIR`. The downloading itself isn't timed.
- **Python and npm installs:** both use a warm package cache.
- **CPU and peak memory for builds and tests:** these come from `os.wait4`. CPU time covers the whole process tree. Peak memory is the largest single process in that tree.
- **Fake terminal:** the pseudo-terminal answers terminal queries the way a real terminal would. Without that, termenv in the Go version waits 5 s for a reply.
- **Line counts:** Python docstrings are counted as code.
- **Rust total dependencies:** the 189 counts every crate in the resolved graph for Linux, including build-time-only crates.
