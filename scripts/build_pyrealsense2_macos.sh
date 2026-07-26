#!/usr/bin/env bash
# Build the pyrealsense2 python bindings for macOS and install them into .venv.
#
# Why this script exists: upstream publishes NO macOS wheel for pyrealsense2 on PyPI
# (manylinux + win_amd64 only), so `pip install pyrealsense2` can never work here. The
# module has to be compiled from librealsense source against the exact interpreter that
# will import it. The resulting wheel is therefore pinned to one python minor version and
# one architecture — upgrade python and you must re-run this.
#
# The build links against the Homebrew librealsense dylib rather than bundling its own, so
# the two MUST stay on the same version. Both are pinned to $LRS_VERSION below; bump them
# together.
#
# Usage:
#   scripts/build_pyrealsense2_macos.sh                # build + install into .venv
#   LRS_VERSION=2.58.3 scripts/build_pyrealsense2_macos.sh
#
# Runtime note: on macOS 12+ the SDK needs root to claim the camera's UVC interface, so
# anything that actually opens a device must run under sudo. See scripts/validate_camera.py.
set -euo pipefail

LRS_VERSION="${LRS_VERSION:-2.58.3}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="${VENV_PY:-$REPO_ROOT/.venv/bin/python}"
WHEEL_DIR="$REPO_ROOT/third_party/wheels"

[ -x "$VENV_PY" ] || { echo "no interpreter at $VENV_PY (run 'uv sync' first)" >&2; exit 1; }

# The bindings load the brew dylib at runtime via rpath, so the C++ SDK must be present
# and at the same version we compile against.
command -v brew >/dev/null || { echo "Homebrew required" >&2; exit 1; }
brew list --versions librealsense | grep -q "$LRS_VERSION" || {
  echo "librealsense $LRS_VERSION not installed via brew (found: $(brew list --versions librealsense || echo none))" >&2
  echo "run: brew install librealsense" >&2
  exit 1
}
BREW_LIB="$(brew --prefix librealsense)/lib"

# Architecture must match the interpreter's: an x86_64 python cannot load an arm64 module.
# On an Apple-silicon Mac with a Rosetta Homebrew/python this is x86_64, not arm64.
ARCH="$($VENV_PY -c 'import platform; print(platform.machine())')"
PY_TAG="$($VENV_PY -c 'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")')"
EXT="$($VENV_PY -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
echo "==> building pyrealsense2 $LRS_VERSION for $PY_TAG/$ARCH against $BREW_LIB"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

curl -fsSL -o lrs.tar.gz \
  "https://github.com/realsenseai/librealsense/archive/refs/tags/v${LRS_VERSION}.tar.gz"
tar xzf lrs.tar.gz
cd "librealsense-${LRS_VERSION}"

# Only the python target is needed; examples/tools/tests roughly triple the build time.
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES="$ARCH" \
  -DBUILD_PYTHON_BINDINGS=ON \
  -DPYTHON_EXECUTABLE="$VENV_PY" \
  -DBUILD_EXAMPLES=OFF -DBUILD_GRAPHICAL_EXAMPLES=OFF -DBUILD_TOOLS=OFF \
  -DBUILD_UNIT_TESTS=OFF -DBUILD_WITH_OPENMP=OFF -DENABLE_CCACHE=OFF \
  -DCHECK_FOR_UPDATES=OFF -DCMAKE_CXX_STANDARD=17
cmake --build build --target pyrealsense2 --parallel "$(sysctl -n hw.ncpu)"

SO="build/Release/pyrealsense2.${LRS_VERSION}${EXT}"
[ -f "$SO" ] || { echo "expected module not produced at $SO" >&2; exit 1; }

# Repoint the rpath: cmake bakes in the (temporary) build dir, which disappears with $WORK.
STAGE="$WORK/stage"
mkdir -p "$STAGE"
cp "$SO" "$STAGE/pyrealsense2${EXT}"
chmod u+w "$STAGE/pyrealsense2${EXT}"
# Delete whatever rpaths cmake baked in rather than a guessed literal: cmake may record a
# symlink-resolved form of $WORK (/private/var/... vs /var/...), so a hardcoded -delete_rpath
# can silently miss and leave the module pointing at a directory that no longer exists.
while read -r stale; do
  [ -n "$stale" ] && install_name_tool -delete_rpath "$stale" "$STAGE/pyrealsense2${EXT}"
done < <(otool -l "$STAGE/pyrealsense2${EXT}" | awk '/LC_RPATH/{f=1} f&&/ path /{print $2; f=0}')
install_name_tool -add_rpath "$BREW_LIB" "$STAGE/pyrealsense2${EXT}"

# Verify no dangling rpath survived and the dylib genuinely resolves.
otool -l "$STAGE/pyrealsense2${EXT}" | awk '/LC_RPATH/{f=1} f&&/ path /{print "    rpath: "$2; f=0}'

# Package as a real wheel so uv/pip track it as an installed distribution instead of it
# sitting in site-packages as an untracked stray file.
DIST="$STAGE/pyrealsense2-${LRS_VERSION}.dist-info"
mkdir -p "$DIST"
cat > "$DIST/METADATA" <<EOF
Metadata-Version: 2.1
Name: pyrealsense2
Version: ${LRS_VERSION}
Summary: Python bindings for Intel RealSense SDK (librealsense), built locally for macOS
Home-page: https://github.com/realsenseai/librealsense
License: Apache-2.0

Built by scripts/build_pyrealsense2_macos.sh — upstream ships no macOS wheel.
Requires the librealsense ${LRS_VERSION} dylib from Homebrew at runtime (resolved by rpath).
EOF
cat > "$DIST/WHEEL" <<EOF
Wheel-Version: 1.0
Generator: scripts/build_pyrealsense2_macos.sh
Root-Is-Purelib: false
Tag: ${PY_TAG}-${PY_TAG}-macosx_11_0_${ARCH}
EOF

mkdir -p "$WHEEL_DIR"
WHEEL="$WHEEL_DIR/pyrealsense2-${LRS_VERSION}-${PY_TAG}-${PY_TAG}-macosx_11_0_${ARCH}.whl"
( cd "$STAGE" && "$VENV_PY" - "$WHEEL" <<'PY'
import base64, csv, hashlib, os, sys, zipfile
wheel = sys.argv[1]
names = sorted(n for n in os.listdir(".") if n.endswith(".so")) + \
        [f"{d}/{f}" for d in os.listdir(".") if d.endswith(".dist-info")
         for f in ("METADATA", "WHEEL")]
rows = []
for n in names:
    data = open(n, "rb").read()
    h = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    rows.append((n, f"sha256={h}", len(data)))
record = next(n for n in names if n.endswith("WHEEL")).replace("WHEEL", "RECORD")
rows.append((record, "", ""))
with open(record, "w", newline="") as f:
    csv.writer(f).writerows(rows)
with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as z:
    for n in names + [record]:
        z.write(n)
PY
)
echo "==> wrote $WHEEL"

# --python is required: a uv-created venv has no pip of its own, and uv otherwise infers the
# target env from the cwd — which is $WORK here, not the project. --reinstall forces the new
# build in even though the version string is unchanged.
( cd "$REPO_ROOT" && uv pip install --python "$VENV_PY" --reinstall "$WHEEL" )

# Import proves the rpath resolves; enumeration is NOT checked here because it needs root.
"$VENV_PY" -c "import pyrealsense2 as rs; print('==> installed pyrealsense2', rs.__version__)"
echo "==> next: sudo $VENV_PY scripts/validate_camera.py     # device access needs root"
