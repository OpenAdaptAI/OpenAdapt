#!/usr/bin/env bash
set -euo pipefail

FFMPEG_VERSION="${FFMPEG_VERSION:-8.1.2}"
RUNTIME_REVISION="${RUNTIME_REVISION:-r1}"
SOURCE_SHA256="${SOURCE_SHA256:-464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c}"
TARGET_TRIPLE="${TARGET_TRIPLE:?TARGET_TRIPLE is required}"
SOURCE_ARCHIVE="${SOURCE_ARCHIVE:?SOURCE_ARCHIVE is required}"
OUTPUT_DIR="${OUTPUT_DIR:?OUTPUT_DIR is required}"
if command -v python3 >/dev/null 2>&1; then
  python_cmd="python3"
else
  python_cmd="python"
fi
temp_root="${RUNNER_TEMP:-/tmp}"
if [[ "${TARGET_TRIPLE}" == "x86_64-pc-windows-msvc" ]]; then
  SOURCE_ARCHIVE="$(cygpath -u "${SOURCE_ARCHIVE}")"
  OUTPUT_DIR="$(cygpath -u "${OUTPUT_DIR}")"
  temp_root="$(cygpath -u "${temp_root}")"
fi

workspace="$(pwd)"
work_root="${temp_root}/openadapt-ffmpeg-${TARGET_TRIPLE}"
source_root="${work_root}/source"
build_root="${work_root}/build"
bundle_dir="${OUTPUT_DIR}/bundle"
build_id="ffmpeg-${FFMPEG_VERSION}-${RUNTIME_REVISION}-${TARGET_TRIPLE}"
archive="${OUTPUT_DIR}/openadapt-${build_id}.zip"
manifest_entry="${OUTPUT_DIR}/${build_id}.manifest-entry.json"

"${python_cmd}" - "${SOURCE_ARCHIVE}" "${SOURCE_SHA256}" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
actual = hashlib.sha256(path.read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit(f"source SHA-256 mismatch: expected {expected}, got {actual}")
PY

mkdir -p "${source_root}" "${build_root}" "${bundle_dir}/bin"
tar -xf "${SOURCE_ARCHIVE}" -C "${source_root}"
source_dir="${source_root}/ffmpeg-${FFMPEG_VERSION}"
test -x "${source_dir}/configure"

common_args=(
  "--prefix=${build_root}/stage"
  "--disable-gpl"
  "--disable-nonfree"
  "--disable-version3"
  "--disable-doc"
  "--disable-debug"
  "--disable-network"
  "--disable-autodetect"
  "--disable-everything"
  "--disable-ffplay"
  "--enable-ffmpeg"
  "--enable-ffprobe"
  "--enable-small"
  "--enable-static"
  "--disable-shared"
  "--enable-zlib"
  "--enable-swscale"
  "--enable-protocol=file,pipe"
  "--enable-demuxer=concat,image2,mov,rawvideo"
  "--enable-muxer=mp4,null,image2,image2pipe"
  "--enable-decoder=png,mpeg4,h264,rawvideo"
  "--enable-parser=h264,mpeg4video"
  "--enable-filter=scale,format,setpts,select"
)

platform_args=()
hardware_encoder=""
case "${TARGET_TRIPLE}" in
  aarch64-apple-darwin)
    platform_args=(
      "--arch=arm64"
      "--cc=clang"
      "--enable-videotoolbox"
      "--enable-encoder=png,mpeg4,h264_videotoolbox"
    )
    hardware_encoder="h264_videotoolbox"
    ;;
  x86_64-apple-darwin)
    platform_args=(
      "--arch=x86_64"
      "--cc=clang"
      "--enable-videotoolbox"
      "--enable-encoder=png,mpeg4,h264_videotoolbox"
    )
    hardware_encoder="h264_videotoolbox"
    ;;
  x86_64-unknown-linux-gnu)
    platform_args=(
      "--arch=x86_64"
      "--cc=gcc"
      "--pkg-config-flags=--static"
      "--enable-encoder=png,mpeg4"
    )
    ;;
  x86_64-pc-windows-msvc)
    # The produced process is a native x86_64 PE executable. The Tauri target
    # name stays MSVC because that is the installer target; FFmpeg itself uses
    # the supported MinGW/UCRT toolchain and links zlib statically.
    platform_args=(
      "--target-os=mingw32"
      "--arch=x86_64"
      "--cc=gcc"
      "--pkg-config-flags=--static"
      "--extra-ldflags=-static"
      "--enable-encoder=png,mpeg4"
    )
    ;;
  *)
    echo "unsupported FFmpeg runtime target: ${TARGET_TRIPLE}" >&2
    exit 2
    ;;
esac

configure_args=("${common_args[@]}" "${platform_args[@]}")
printf '%s\n' "${configure_args[@]}" >"${build_root}/configure-args.txt"

cd "${source_dir}"
./configure "${configure_args[@]}" | tee "${build_root}/configure-output.txt"
if ! grep -q '^License: LGPL version 2.1 or later' "${build_root}/configure-output.txt"; then
  echo "FFmpeg did not configure as LGPL-2.1-or-later" >&2
  grep '^License:' "${build_root}/configure-output.txt" >&2 || true
  exit 1
fi
if grep -Eq -- '--enable-(gpl|nonfree|version3)' ffbuild/config.mak; then
  echo "forbidden FFmpeg license flag reached the build" >&2
  exit 1
fi

exe_suffix=""
if [[ "${TARGET_TRIPLE}" == "x86_64-pc-windows-msvc" ]]; then
  exe_suffix=".exe"
fi
jobs="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
make -j"${jobs}" "ffmpeg${exe_suffix}" "ffprobe${exe_suffix}"

cp "ffmpeg${exe_suffix}" "${bundle_dir}/bin/ffmpeg${exe_suffix}"
cp "ffprobe${exe_suffix}" "${bundle_dir}/bin/ffprobe${exe_suffix}"
chmod +x "${bundle_dir}/bin/ffmpeg${exe_suffix}" "${bundle_dir}/bin/ffprobe${exe_suffix}"

ffmpeg_bin="${bundle_dir}/bin/ffmpeg${exe_suffix}"
ffprobe_bin="${bundle_dir}/bin/ffprobe${exe_suffix}"
mkdir -p "${bundle_dir}/LICENSES" "${bundle_dir}/PROVENANCE"
cp COPYING.LGPLv2.1 "${bundle_dir}/LICENSES/FFmpeg-LGPL-2.1-or-later.txt"
cp LICENSE.md "${bundle_dir}/LICENSES/FFmpeg-LICENSE.md"
cp "${build_root}/configure-args.txt" "${bundle_dir}/PROVENANCE/configure-args.txt"
zlib_package="operating-system library"
if [[ "${TARGET_TRIPLE}" == "x86_64-pc-windows-msvc" ]]; then
  zlib_license="/ucrt64/share/licenses/zlib/LICENSE"
  test -f "${zlib_license}"
  cp "${zlib_license}" "${bundle_dir}/LICENSES/zlib.txt"
  zlib_package="$(pacman -Q mingw-w64-ucrt-x86_64-zlib)"
fi

"${ffmpeg_bin}" -version >"${bundle_dir}/PROVENANCE/ffmpeg-version.txt" 2>&1
"${ffmpeg_bin}" -buildconf >"${bundle_dir}/PROVENANCE/ffmpeg-buildconf.txt" 2>&1
"${ffmpeg_bin}" -hide_banner -encoders >"${bundle_dir}/PROVENANCE/ffmpeg-encoders.txt" 2>&1
"${ffmpeg_bin}" -hide_banner -muxers >"${bundle_dir}/PROVENANCE/ffmpeg-muxers.txt" 2>&1
"${ffprobe_bin}" -version >"${bundle_dir}/PROVENANCE/ffprobe-version.txt" 2>&1
"${ffprobe_bin}" -buildconf >"${bundle_dir}/PROVENANCE/ffprobe-buildconf.txt" 2>&1

grep -q -- '--disable-gpl' "${bundle_dir}/PROVENANCE/ffmpeg-buildconf.txt"
grep -q -- '--disable-nonfree' "${bundle_dir}/PROVENANCE/ffmpeg-buildconf.txt"
grep -q -- '--disable-version3' "${bundle_dir}/PROVENANCE/ffmpeg-buildconf.txt"
if grep -Eq -- '--enable-(gpl|nonfree)' "${bundle_dir}/PROVENANCE/ffmpeg-buildconf.txt"; then
  echo "forbidden FFmpeg build flag in executable" >&2
  exit 1
fi
grep -Eq '^[[:space:]]*V[^[:space:]]*[[:space:]]+mpeg4[[:space:]]' \
  "${bundle_dir}/PROVENANCE/ffmpeg-encoders.txt"
grep -Eq '^[[:space:]]*V[^[:space:]]*[[:space:]]+png[[:space:]]' \
  "${bundle_dir}/PROVENANCE/ffmpeg-encoders.txt"
grep -Eq '^[[:space:]]*E[[:space:]]+mp4[[:space:]]' \
  "${bundle_dir}/PROVENANCE/ffmpeg-muxers.txt"
grep -Eq '^[[:space:]]*E[[:space:]]+image2pipe[[:space:]]' \
  "${bundle_dir}/PROVENANCE/ffmpeg-muxers.txt"

case "${TARGET_TRIPLE}" in
  *-apple-darwin)
    otool -L "${ffmpeg_bin}" >"${bundle_dir}/PROVENANCE/native-dependencies.txt"
    ;;
  x86_64-unknown-linux-gnu)
    ldd "${ffmpeg_bin}" >"${bundle_dir}/PROVENANCE/native-dependencies.txt"
    ;;
  x86_64-pc-windows-msvc)
    objdump -p "${ffmpeg_bin}" >"${bundle_dir}/PROVENANCE/native-dependencies.txt"
    if grep -qi 'DLL Name:.*zlib' "${bundle_dir}/PROVENANCE/native-dependencies.txt"; then
      echo "Windows runtime has an unprovisioned zlib DLL dependency" >&2
      exit 1
    fi
    ;;
esac

compiler="$("${CC:-cc}" --version 2>&1 | head -n 1 || true)"
export TARGET_TRIPLE FFMPEG_VERSION RUNTIME_REVISION SOURCE_SHA256 GITHUB_SHA GITHUB_RUN_ID
export GITHUB_SERVER_URL GITHUB_REPOSITORY GITHUB_WORKFLOW_REF compiler hardware_encoder
export zlib_package
"${python_cmd}" - "${bundle_dir}" <<'PY'
import json
import os
import pathlib
import sys

bundle = pathlib.Path(sys.argv[1])
source = {
    "source_url": f"https://ffmpeg.org/releases/ffmpeg-{os.environ['FFMPEG_VERSION']}.tar.xz",
    "source_sha256": os.environ["SOURCE_SHA256"],
    "signature_url": f"https://ffmpeg.org/releases/ffmpeg-{os.environ['FFMPEG_VERSION']}.tar.xz.asc",
    "signing_key_fingerprint": "FCF986EA15E6E293A5644F10B4322F04D67658D8",
}
build = {
    "target": os.environ["TARGET_TRIPLE"],
    "runtime_revision": os.environ["RUNTIME_REVISION"],
    "repository": os.environ.get("GITHUB_REPOSITORY", ""),
    "commit": os.environ.get("GITHUB_SHA", ""),
    "run_id": os.environ.get("GITHUB_RUN_ID", ""),
    "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF", ""),
    "compiler": os.environ.get("compiler", ""),
    "optional_hardware_encoder": os.environ.get("hardware_encoder", ""),
    "software_fallback_encoder": "mpeg4",
    "zlib_provenance": os.environ.get("zlib_package", ""),
    "license": "LGPL-2.1-or-later",
}
(bundle / "PROVENANCE" / "SOURCE.json").write_text(
    json.dumps(source, indent=2, sort_keys=True) + "\n"
)
(bundle / "PROVENANCE" / "BUILD.json").write_text(
    json.dumps(build, indent=2, sort_keys=True) + "\n"
)
PY

smoke_dir="${build_root}/smoke"
mkdir -p "${smoke_dir}"
"${python_cmd}" - "${smoke_dir}" <<'PY'
import binascii
import json
import pathlib
import struct
import sys
import zlib

root = pathlib.Path(sys.argv[1])
width = height = 16
rows = b"".join(
    b"\x00" + b"".join(bytes((x * 16, y * 16, 128)) for x in range(width))
    for y in range(height)
)
def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(
        ">I", binascii.crc32(kind + data) & 0xFFFFFFFF
    )
png = (
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(rows))
    + chunk(b"IEND", b"")
)
(root / "frame.png").write_bytes(png)
(
    root / "frames.ffconcat"
).write_text(
    "ffconcat version 1.0\n"
    "file frame.png\n"
    "duration 0.040000000\n"
    "file frame.png\n",
    encoding="utf-8",
)
frames = b"".join(
    b"".join(bytes((x * 16, y * 16, frame * 8)) for y in range(height) for x in range(width))
    for frame in range(25)
)
(root / "frames.rgb").write_bytes(frames)
PY

"${ffmpeg_bin}" -hide_banner -loglevel error -nostdin -y \
  -f concat -safe 1 -i "${smoke_dir}/frames.ffconcat" \
  -an -c:v mpeg4 -q:v 5 -pix_fmt yuv420p -fps_mode vfr -f mp4 \
  "${smoke_dir}/png-input.mp4"

"${ffmpeg_bin}" -hide_banner -loglevel error -nostdin \
  -f rawvideo -pixel_format rgb24 -video_size 16x16 -framerate 25 \
  -i "${smoke_dir}/frames.rgb" -an -c:v mpeg4 -q:v 5 -pix_fmt yuv420p \
  -y "${smoke_dir}/raw-input.mp4"

"${ffprobe_bin}" -v error -count_frames -show_streams -show_format \
  -of json "${smoke_dir}/raw-input.mp4" >"${smoke_dir}/probe.json"
"${ffmpeg_bin}" -hide_banner -loglevel error -nostdin \
  -i "${smoke_dir}/raw-input.mp4" -vf 'select=eq(n\,0)' -frames:v 1 \
  -f image2pipe -vcodec png -y "${smoke_dir}/decoded.png"

"${python_cmd}" - "${smoke_dir}" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
probe = json.loads((root / "probe.json").read_text())
video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
assert video["codec_name"] == "mpeg4"
assert (video["width"], video["height"]) == (16, 16)
assert int(video["nb_read_frames"]) == 25
assert (root / "decoded.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
PY

if [[ -n "${hardware_encoder}" ]]; then
  set +e
  "${ffmpeg_bin}" -hide_banner -loglevel error -nostdin \
    -f rawvideo -pixel_format rgb24 -video_size 16x16 -framerate 25 \
    -i "${smoke_dir}/frames.rgb" -an -c:v "${hardware_encoder}" \
    -y "${smoke_dir}/hardware.mp4" \
    >"${bundle_dir}/PROVENANCE/hardware-probe.txt" 2>&1
  hardware_status=$?
  set -e
  printf '\nexit_status=%s\n' "${hardware_status}" \
    >>"${bundle_dir}/PROVENANCE/hardware-probe.txt"
fi

cd "${workspace}"
"${python_cmd}" scripts/package_ffmpeg_runtime.py \
  --bundle-dir "${bundle_dir}" \
  --output "${archive}" \
  --target "${TARGET_TRIPLE}" \
  --build-id "${build_id}" \
  --manifest-entry "${manifest_entry}"
