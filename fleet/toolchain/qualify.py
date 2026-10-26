#!/usr/bin/env python3
"""Prove a prebuilt client reaches mbx and JIT-compiles a nightly Rust edit."""

import argparse
from pathlib import Path
import subprocess
import tempfile
import time


def command(argv, cwd, env, log):
    result = subprocess.run(argv, cwd=cwd, env=env, text=True,
                            capture_output=True, timeout=60)
    output = result.stdout + result.stderr
    log.write_text(output)
    if result.returncode:
        raise RuntimeError(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    inherited = subprocess.check_output(
        ["sh", "-c", '. "$1/env.sh"; env -0', "sh", str(root)],
    )
    env = dict(item.decode().split("=", 1) for item in inherited.split(b"\0") if item)
    workspace = Path(tempfile.mkdtemp(prefix="blaze-nightly-qualification-"))
    print("Qualification logs: " + str(workspace), flush=True)
    (workspace / "src").mkdir()
    (workspace / ".cargo").mkdir()
    (workspace / "Cargo.toml").write_text(
        '[package]\nname="blaze-qualification"\nversion="0.1.0"\nedition="2024"\n'
    )
    (workspace / ".cargo/config.toml").write_text(
        '[build]\nrustflags=["-C", "force-unwind-tables=yes", "-Z", "use-sync-unwind=yes"]\n'
    )
    source = '#![feature(float_algebraic)]\nfn value() -> f64 { 1.0f64.algebraic_add(%s) }\n'
    source += 'fn main() { println!("{}", value()); }\n'
    main_file = workspace / "src/main.rs"
    main_file.write_text(source % "1.0")
    env.update(MBX_LOG="debug", MBX_CACHE_DIR=str(workspace / "mbx-cache"))
    cargo = str(root / "shim/cargo")
    cold = command([cargo, "build"], workspace, env, workspace / "cold.log")
    if "sending `cargo build` through" not in cold or "mbx[cache]" not in cold:
        raise RuntimeError("The cold build did not demonstrate both Blaze routing and mbx")
    for attempt in range(1, 5):
        main_file.write_text(source % (str(attempt + 1) + ".0"))
        output = command([cargo, "build"], workspace, env, workspace / f"edit-{attempt}.log")
        actual = command([str(workspace / "target/debug/blaze-qualification")],
                         workspace, env, workspace / "program.log").strip()
        if actual != str(attempt + 2):
            raise RuntimeError("Edited program returned " + actual)
        if "[ra-jit]" in output:
            command([cargo, "check"], workspace, {**env, "MBX_BLAZE": "0"},
                    workspace / "rustc-check.log")
            print("PASS: mbx cold build, Blaze nightly JIT edit, changed output, rustc check")
            return
        time.sleep(2)
    raise RuntimeError("No edit was compiled by Blaze; Cargo success alone does not qualify")


if __name__ == "__main__":
    main()
