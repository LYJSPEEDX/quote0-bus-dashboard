#!/usr/bin/env bash
set -euo pipefail

# Download Linux/x86_64 CPython 3.11 wheels regardless of the host OS so Pillow
# is compatible with the Lambda runtime even when packaging from macOS or
# Windows. No Docker needed: only prebuilt manylinux wheels are accepted.
#
# The archive is reproducible: the same sources always give the same bytes, so
# Terraform's source_code_hash only changes when the code or dependencies do.
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="$project_dir/build"
python_bin="${PYTHON:-python3}"

rm -rf "$build_dir"
mkdir -p "$build_dir/python"

# --no-compile: .pyc files embed timestamps and would change the hash on
# every build. Lambda's filesystem is read-only, so they would not be written
# at runtime either; this only skips a small bytecode cache.
"$python_bin" -m pip install --quiet --no-cache-dir --no-compile \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  --target "$build_dir/python" \
  -r "$project_dir/requirements.txt"

cp "$project_dir/src/app.py" "$build_dir/python/app.py"
cp -r "$project_dir/src/fonts" "$build_dir/python/fonts"

# Zip with sorted entries, a fixed timestamp and fixed permissions, and without
# pip's install metadata that records the local install (RECORD/INSTALLER/etc.
# are harmless to drop at runtime but differ between machines).
"$python_bin" - "$build_dir/python" "$build_dir/lambda.zip" <<'PY'
import sys
import zipfile
from pathlib import Path

source, target = Path(sys.argv[1]), Path(sys.argv[2])
skip = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}
files = sorted(
    path for path in source.rglob("*")
    if path.is_file() and not (path.parent.name.endswith(".dist-info") and path.name in skip)
)
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in files:
        info = zipfile.ZipInfo(path.relative_to(source).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, path.read_bytes())
PY

echo "Created $build_dir/lambda.zip"
