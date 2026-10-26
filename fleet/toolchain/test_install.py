import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("installer", Path(__file__).with_name("install.py"))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_checksum_mismatch_never_extracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            archive.write_bytes(b"wrong archive")
            destination = root / "bin"
            destination.mkdir()
            with patch.object(installer, "run", side_effect=lambda *args: shutil.copy(archive, args[-1])):
                with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                    installer.unpack({"url": "https://example.invalid/archive", "sha256": "0" * 64},
                                     destination, ("mbx",))
            self.assertEqual(list(destination.iterdir()), [])

    def test_archive_requires_complete_companions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                member = tarfile.TarInfo("release/ra-jit")
                member.size = 4
                output.addfile(member, io.BytesIO(b"tool"))
            asset = {"url": "https://example.invalid/archive",
                     "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
            destination = root / "bin"
            destination.mkdir()
            with patch.object(installer, "run", side_effect=lambda *args: shutil.copy(archive, args[-1])):
                with self.assertRaisesRegex(RuntimeError, "blaze"):
                    installer.unpack(asset, destination, installer.BLAZE_PROGRAMS)

    def test_activation_routes_cargo_to_its_pinned_mbx(self):
        with tempfile.TemporaryDirectory(prefix="blaze setup ") as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            mbx = root / "bin/mbx"
            mbx.write_text('#!/bin/sh\nprintf "%s\\n" "$MBX_CARGO_SHIM_MODE" '
                           '"$MBX_CARGO_SHIM_PATH" "$RUSTC" "$MBX_BLAZE" "$@"\n')
            mbx.chmod(0o755)
            installer.activate(root, {"toolchain": "nightly-probe"}, root / "rustc", False)
            result = subprocess.run(
                ['sh', '-c', '. "$1/env.sh"; cargo build --package "name with spaces"',
                 'sh', str(root)], capture_output=True, text=True, check=True,
                env={**os.environ, "MBX_DISABLE": "1"},
            )
            self.assertEqual(result.stdout.splitlines(), [
                "1", str(root / "shim/cargo"), str(root / "rustc"), "1",
                "build", "--package", "name with spaces",
            ])


if __name__ == "__main__":
    unittest.main()
