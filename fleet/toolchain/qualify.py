#!/usr/bin/env python3
"""Prove a prebuilt client reaches mbx and JIT-compiles a nightly Rust edit."""

import argparse
import json
import re
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
    parser.add_argument("--sync-unwind", action="store_true",
                        help="Probe Perry release flags, currently refused by Blaze")
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
    dependency = workspace.with_name(workspace.name + "-dependency")
    (dependency / "src").mkdir(parents=True)
    (dependency / "Cargo.toml").write_text(
        '[package]\nname="qualification-dependency"\nversion="0.1.0"\nedition="2024"\n'
    )
    (dependency / "src/lib.rs").write_text('pub fn base() -> f64 { 1.0 }\n')
    (workspace / "Cargo.toml").write_text(
        '[package]\nname="blaze-qualification"\nversion="0.1.0"\nedition="2024"\n'
        '[dependencies]\nqualification-dependency={path=' + json.dumps(str(dependency)) + '}\n'
    )
    flags = '["-C", "force-unwind-tables=yes"]'
    if args.sync_unwind:
        flags = '["-C", "force-unwind-tables=yes", "-Z", "use-sync-unwind=yes"]'
    (workspace / ".cargo/config.toml").write_text(
        "[build]\nrustflags=" + flags + "\n"
    )
    source = 'fn value() -> f64 { qualification_dependency::base().algebraic_add(%s) }\n'
    source += 'fn main() { println!("{}", value()); }\n'
    main_file = workspace / "src/main.rs"
    main_file.write_text(source % "1.0")
    env.update(MBX_LOG="debug", MBX_CACHE_DIR=str(workspace / "mbx-cache"))
    cargo = str(root / "shim/cargo")
    cold = command([cargo, "build"], workspace, env, workspace / "cold.log")
    if "sending `cargo build` through" not in cold or "mbx[cache]" not in cold:
        raise RuntimeError("The cold build did not demonstrate both Blaze routing and mbx")
    if "deferring to it, so this build is not cached" in cold:
        raise RuntimeError("Blaze replaced mbx's dependency compiler wrapper")
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
            plain = {**env, "MBX_BLAZE": "0"}
            command([cargo, "clean", "-p", "qualification-dependency"], workspace, plain,
                    workspace / "clean-dependency.log")
            restored = command([cargo, "build"], workspace, plain, workspace / "cache-restore.log")
            if not re.search(r"mbx\[cache\]: (?:object cache: )?[1-9][0-9]* hits", restored):
                raise RuntimeError("Dependency objects were not restored from mbx\n" + restored)
            print("PASS: Blaze JIT edit, changed output, rustc check, mbx dependency cache hit")
            return
        time.sleep(2)
    raise RuntimeError("No edit was compiled by Blaze; Cargo success alone does not qualify\n" + output)


if __name__ == "__main__":
    main()
