#!/usr/bin/env bash
set -euo pipefail

# Download Linux/x86_64 CPython 3.11 wheels regardless of the host OS so Pillow
# is compatible with the Lambda runtime even when packaging from macOS or
# Windows. No Docker needed: only prebuilt manylinux wheels are accepted.
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="$project_dir/build"
python_bin="${PYTHON:-python3}"

command -v zip >/dev/null || {
  echo "zip is required to create the Lambda archive." >&2
  exit 1
}

rm -rf "$build_dir"
mkdir -p "$build_dir/python"

"$python_bin" -m pip install --quiet --no-cache-dir \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  --target "$build_dir/python" \
  -r "$project_dir/requirements.txt"

cp "$project_dir/src/app.py" "$build_dir/python/app.py"
cp -r "$project_dir/src/fonts" "$build_dir/python/fonts"
(cd "$build_dir/python" && zip -qr ../lambda.zip .)

echo "Created $build_dir/lambda.zip"
