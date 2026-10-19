"""Validate built artifacts in fresh environments outside the source checkout.

Package installation may access PyPI. All analysis/build fixtures are synthetic;
the installed runtime commands need no network, credentials or environment files.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import venv
import zipfile


def run(command: list[str], cwd: Path, env: dict) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    args.work = args.work.resolve()
    args.work.mkdir(parents=True, exist_ok=False)
    wheels, sources = sorted(args.dist.glob("*.whl")), sorted(args.dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Select a directory containing exactly one wheel and one sdist")
    with zipfile.ZipFile(wheels[0]) as archive:
        if not any(name.endswith("/licenses/LICENSE") for name in archive.namelist()):
            raise ValueError("Wheel is missing LICENSE")
        if any(
            name.endswith((".parquet", ".dbn", ".zst", ".arrow")) for name in archive.namelist()
        ):
            raise ValueError("Wheel contains forbidden data artifacts")
    with tarfile.open(sources[0]) as archive:
        names = archive.getnames()
        if any(name.endswith((".parquet", ".dbn", ".zst", ".arrow")) for name in names):
            raise ValueError("Source distribution contains forbidden data artifacts")
        if any(member.issym() or member.islnk() for member in archive.getmembers()):
            raise ValueError("Source distribution contains symlinks")
        standalone = next(name for name in names if name.endswith("/contract_coverage.py"))
        source = archive.extractfile(standalone)
        if source is None:
            raise ValueError("Source distribution is missing the standalone selector")
        with source:
            (args.work / "contract_coverage.py").write_bytes(source.read())
    env = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    env["POLARS_MAX_THREADS"] = "2"
    constraints = root / "requirements/validation.txt"
    for label, artifact in (("wheel", wheels[0]), ("sdist", sources[0])):
        work = args.work / label
        work.mkdir()
        environment = work / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        binary = environment / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        product = binary / ("product-coverage.exe" if os.name == "nt" else "product-coverage")
        futures = binary / ("futures-ts.exe" if os.name == "nt" else "futures-ts")
        pip = [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-c",
            str(constraints),
        ]
        run([*pip, "pip==26.2.1", "setuptools==84.0.0"], work, env)
        run([*pip, "--no-build-isolation", str(artifact.resolve())], work, env)
        assertion = (
            "import importlib.util,product_coverage,sys; "
            "assert importlib.util.find_spec('databento') is None; "
            "assert importlib.util.find_spec('databento_dbn') is None; "
            "assert product_coverage.__file__.startswith(sys.prefix); "
            "from importlib.metadata import metadata; "
            "assert metadata('futures-ts')['License-Expression']=='0BSD'"
        )
        run([str(python), "-I", "-c", assertion], work, env)
        for name in ("activity_export.csv", "activity_mapping.json"):
            shutil.copyfile(root / "examples" / name, work / name)
        common = ["--input", "activity_export.csv", "--mapping", "activity_mapping.json"]
        run([str(product), "normalize", *common, "--out", "normalized.parquet"], work, env)
        run(
            [
                str(product),
                "analyze",
                "--input",
                "normalized.parquet",
                "--start",
                "2024-01-01",
                "--end",
                "2024-12-31",
                "--out",
                "training",
            ],
            work,
            env,
        )
        run(
            [
                str(product),
                "evaluate",
                *common,
                "--policy",
                "training",
                "--start",
                "2025-01-01",
                "--out",
                "holdout",
            ],
            work,
            env,
        )
        report = json.loads((work / "training/report.json").read_text())
        assert sum(row["minimum_capacity"] for row in report["groups"]) == 4
        run([*pip, "--no-build-isolation", str(artifact.resolve()) + "[databento]"], work, env)
        source_path = work / "dbn-source"
        # Fixture generation uses the installed SDK; only synthetic helper code is copied.
        (work / "fixtures").mkdir()
        shutil.copyfile(root / "tests/generate_fixtures.py", work / "fixtures/generate.py")
        run([str(python), "fixtures/generate.py", "--out", str(source_path)], work, env)
        for command in (
            ["build", "--src", str(source_path), "--out", "series", "--roots", "CL"],
            ["coverage", "--out", "series"],
            ["check", "--out", "series"],
            ["analyze", "--out", "series", "--target", "0.99"],
        ):
            run([str(futures), *command], work, env)
        installed = subprocess.check_output(
            [
                str(python),
                "-I",
                "-c",
                "from importlib.metadata import distributions; "
                "print('\\n'.join(sorted(d.metadata['Name']+'=='+d.version "
                "for d in distributions() "
                "if d.metadata['Name'].lower()!='futures-ts')))",
            ],
            cwd=work,
            env=env,
            text=True,
        )
        (work / "installed-requirements.txt").write_text(installed)
    run(
        [sys.executable, "-I", str(args.work / "contract_coverage.py"), "--self-test"],
        args.work,
        env,
    )
    (args.work / "passed.json").write_text(
        json.dumps(
            {
                "artifacts": [p.name for p in [*wheels, *sources]],
                "core_sdk_absent": True,
                "isolated_wheel_and_sdist": True,
                "synthetic_pipeline": True,
                "license": "0BSD",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
