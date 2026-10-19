"""Release-only Gitleaks scan of reachable history and the nonignored source tree.

Downloads a pinned official binary only when --binary is absent. Runtime CLIs
and pytest never call this release tool. Scanner reports redact all findings.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import urllib.request

VERSION = "8.30.1"
ARCHIVES = {
    ("Darwin", "arm64"): (
        "darwin_arm64",
        "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    ),
    ("Linux", "x86_64"): (
        "linux_x64",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    ),
}


def install(directory: Path) -> Path:
    try:
        suffix, expected = ARCHIVES[(platform.system(), platform.machine())]
    except KeyError as exc:
        raise ValueError("Run on Linux x64/macOS arm64 or provide a trusted --binary") from exc
    name = f"gitleaks_{VERSION}_{suffix}.tar.gz"
    archive = directory / name
    with urllib.request.urlopen(
        f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/{name}", timeout=60
    ) as response:
        archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError("Gitleaks release checksum mismatch")
    with tarfile.open(archive) as handle:
        member = handle.getmember("gitleaks")
        if not member.isfile() or member.size > 100_000_000:
            raise ValueError("Invalid scanner archive")
        source = handle.extractfile(member)
        if source is None:
            raise ValueError("Missing scanner binary")
        binary = directory / "gitleaks"
        with source, binary.open("wb") as target:
            shutil.copyfileobj(source, target)
    binary.chmod(0o700)
    return binary


def scan(binary: Path, root: Path, out: Path) -> None:
    version = subprocess.check_output([str(binary), "version"], text=True).strip()
    if version != VERSION:
        raise ValueError(f"Expected Gitleaks {VERSION}, received {version}")
    common = ["--redact=100", "--no-banner", "--report-format=json"]
    subprocess.run(
        [
            str(binary),
            "git",
            str(root),
            "--log-opts=--all",
            *common,
            f"--report-path={out / 'history.json'}",
        ],
        check=True,
    )
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
        )
        .decode()
        .split("\0")
    )
    with TemporaryDirectory(prefix="source-", dir=out) as temporary:
        source_root = Path(temporary)
        for name in filter(None, names):
            path = root / name
            if not path.exists():
                continue
            if (
                path.is_symlink()
                or path.stat().st_size > 2_000_000
                or Path(name).is_absolute()
                or ".." in Path(name).parts
            ):
                raise ValueError(f"Source scan refuses symlink, large file or unsafe path: {name}")
            target = source_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        subprocess.run(
            [
                str(binary),
                "dir",
                str(source_root),
                *common,
                f"--report-path={out / 'working-tree.json'}",
            ],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--out", type=Path, default=Path("out/security"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    with TemporaryDirectory(prefix="scanner-") as temporary:
        binary = args.binary.resolve() if args.binary else install(Path(temporary))
        scan(binary, root, args.out.resolve())


if __name__ == "__main__":
    main()
