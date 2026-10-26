#!/usr/bin/env python3
"""Install a pinned prebuilt Blaze/mbx toolchain without compiling either tool."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile


BLAZE_PROGRAMS = (
    "blaze", "cargo-blaze", "cargo-ra-jit", "ra-jit", "ra-jit-rustc",
    "ra-jit-rustc-first",
)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def host():
    system, arch = platform.system(), platform.machine()
    if system == "Darwin" and arch == "arm64":
        return "aarch64-apple-darwin"
    if system == "Linux" and arch in ("x86_64", "aarch64"):
        libc, version = platform.libc_ver()
        if libc != "glibc" or tuple(map(int, version.split(".")[:2])) < (2, 34):
            raise RuntimeError("Blaze requires Linux glibc 2.34 or newer")
        return arch + "-unknown-linux-gnu"
    raise RuntimeError("This prebuilt toolchain supports Linux and Apple Silicon macOS")


def unpack(asset, destination, required):
    """Verify the complete archive before copying executable regular files."""
    with tempfile.TemporaryDirectory(prefix="blaze-download-") as temporary:
        archive = Path(temporary) / "archive.tar.gz"
        run("curl", "--proto", "=https", "--tlsv1.2", "-fsSL", "--retry", "3",
            asset["url"], "-o", str(archive))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != asset["sha256"]:
            raise RuntimeError("Downloaded archive does not match the pinned SHA-256")
        allowed = set(required) | {"blaze-proc-macro-srv"}
        found = set()
        with tarfile.open(archive, "r:gz") as source:
            for member in source:
                name = Path(member.name).name
                if name not in allowed or not member.isfile():
                    continue
                if name in found:
                    raise RuntimeError("Duplicate executable in archive: " + name)
                found.add(name)
                with source.extractfile(member) as incoming:
                    with (destination / name).open("wb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                (destination / name).chmod(0o755)
        missing = set(required) - found
        if missing:
            raise RuntimeError("Archive lacks required programs: " + ", ".join(sorted(missing)))


def compiler(lock):
    toolchain = lock["toolchain"]
    probe = subprocess.run(
        ["rustup", "which", "--toolchain", toolchain, "rustc"],
        capture_output=True, text=True,
    )
    rustc = Path(probe.stdout.strip()) if probe.returncode == 0 else None
    source = rustc.parent.parent / "lib/rustlib/src/rust/library" if rustc else None
    components = subprocess.run(
        ["rustup", "component", "list", "--toolchain", toolchain, "--installed"],
        capture_output=True, text=True,
    )
    has_lints = all(name in components.stdout for name in ("clippy-", "rustfmt-"))
    if rustc is None or not source.is_dir() or not has_lints:
        run("rustup", "toolchain", "install", toolchain, "--profile", "minimal",
            "--component", "rust-src", "--component", "clippy", "--component", "rustfmt")
        rustc = Path(run("rustup", "which", "--toolchain", toolchain, "rustc",
                         capture_output=True).stdout.strip())
    version = run(str(rustc), "--version", capture_output=True).stdout.strip()
    if version != lock["rustc_version"]:
        raise RuntimeError("Compiler identity differs from the prebuild: " + version)
    return rustc


def activate(root, lock, rustc, github):
    # Give mbx its own shim directory so excluding it from PATH retains Blaze.
    shim = root / "shim"
    shim.mkdir(exist_ok=True)
    launcher = shim / "cargo"
    launcher.write_text(
        '#!/bin/sh\n'
        'root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
        '# Keep mbx outside Blaze so dependency compilations remain cacheable.\n'
        'if [ "${MBX_BLAZE_ROUTED:-}" = 1 ]; then\n'
        '  case "${RUSTC_WORKSPACE_WRAPPER:-}" in\n'
        '    "$root/bin/ra-jit-rustc"|"$root/bin/ra-jit-rustc-first")\n'
        '      case "${RUSTC_WRAPPER:-}" in\n'
        '        "$root/bin/ra-jit-rustc"|"$root/bin/ra-jit-rustc-first")\n'
        '          unset RUSTC_WRAPPER RA_JIT_EVERY_CRATE ;;\n'
        '      esac ;;\n'
        '  esac\n'
        'fi\n'
        'export MBX_CARGO_SHIM_MODE=1\n'
        'export MBX_CARGO_SHIM_PATH="$root/shim/cargo"\n'
        'exec "$root/bin/mbx" "$@"\n'
    )
    launcher.chmod(0o755)
    values = {
        "RUSTUP_TOOLCHAIN": lock["toolchain"],
        # A matching explicit rustc keeps Blaze from replacing mbx's CARGO shim.
        "RUSTC": str(rustc),
        "MBX_BLAZE": "1",
        "MBX_DISABLE": "0",
        "MBX_BLAZE_ROOT": str(root),
    }
    if lock.get("std_from"):
        source = lock["std_from"]
        values["RA_JIT_STD_FROM"] = source.get(host(), "off") if isinstance(source, dict) else source
    paths = [str(shim), str(root / "bin")]
    lines = ['export PATH=' + shlex.quote(":".join(paths)) + ':"$PATH"']
    lines += ["export " + key + "=" + shlex.quote(value) for key, value in values.items()]
    (root / "env.sh").write_text("\n".join(lines) + "\n")
    if github:
        with open(os.environ["GITHUB_PATH"], "a") as output:
            output.write("\n".join(paths) + "\n")
        with open(os.environ["GITHUB_ENV"], "a") as output:
            output.write("\n".join(key + "=" + value for key, value in values.items()) + "\n")


def install(lock_path, root, github=False):
    import fcntl

    raw = lock_path.read_bytes()
    lock = json.loads(raw)
    target = host()
    if target not in lock["blaze"] or target not in lock["mbx"]:
        raise RuntimeError("A qualified prebuilt is not published for " + target)
    identity = hashlib.sha256(raw + target.encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "setup.lock").open("w") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        rustc = compiler(lock)
        receipt = root / "receipt"
        programs = (*BLAZE_PROGRAMS, "mbx")
        ready = (receipt.is_file() and receipt.read_text().strip() == identity
                 and all(os.access(root / "bin" / name, os.X_OK) for name in programs))
        if not ready:
            with tempfile.TemporaryDirectory(prefix="stage-", dir=root) as temporary:
                stage = Path(temporary)
                unpack(lock["blaze"][target], stage, BLAZE_PROGRAMS)
                unpack(lock["mbx"][target], stage, ("mbx",))
                version = run(str(stage / "mbx"), "--version", capture_output=True).stdout.strip()
                if version != "mbx " + lock["mbx_version"]:
                    raise RuntimeError("Unexpected mbx version: " + version)
                env = {**os.environ, "RUSTUP_TOOLCHAIN": lock["toolchain"], "RUSTC": str(rustc)}
                chosen = run(str(stage / "blaze"), "toolchain", env=env,
                             capture_output=True).stdout.strip()
                if "Rust " + lock["toolchain"] not in chosen:
                    raise RuntimeError("Unexpected Blaze compiler: " + chosen)
                (root / "bin").mkdir(exist_ok=True)
                for binary in stage.iterdir():
                    os.replace(binary, root / "bin" / binary.name)
                receipt.write_text(identity + "\n")
        activate(root, lock, rustc, github)
    print("Ready: mbx " + lock["mbx_version"] + ", Blaze " + lock["source_revision"][:12]
          + ", " + lock["toolchain"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--github-env", action="store_true")
    args = parser.parse_args()
    try:
        install(args.lock, args.root.resolve(), args.github_env)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
