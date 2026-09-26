#!/usr/bin/env bash
set -euo pipefail

# Build Linux/x86_64 dependencies inside AWS's Python 3.11 image so Pillow is
# compatible with Lambda even when packaging from macOS or Windows.
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="$project_dir/build"
image="public.ecr.aws/sam/build-python3.11:latest"

command -v docker >/dev/null || {
  echo "Docker is required to package Linux Lambda dependencies." >&2
  exit 1
}
command -v zip >/dev/null || {
  echo "zip is required to create the Lambda archive." >&2
  exit 1
}

rm -rf "$build_dir"
mkdir -p "$build_dir/python"

docker run --rm --platform linux/amd64 \
  -v "$project_dir:/var/task" \
  -w /var/task \
  "$image" \
  /bin/sh -c 'pip install --no-cache-dir -r requirements.txt -t build/python && cp src/app.py build/python/app.py && cp -r src/fonts build/python/fonts && cd build/python && zip -qr ../lambda.zip .'

echo "Created $build_dir/lambda.zip"
