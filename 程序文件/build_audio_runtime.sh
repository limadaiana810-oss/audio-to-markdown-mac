#!/bin/bash
# Rebuild the bundled audio-only FFmpeg from the included unmodified sources.
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SOURCE_DIR")"
ARCHIVE="${1:-$PROJECT_DIR/分发资源/ffmpeg-9.0.2.tar.xz}"
DESTINATION="${2:-$PROJECT_DIR/.package-cache/audio}"
mkdir -p "$DESTINATION"
DESTINATION="$(cd "$DESTINATION" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/recording-ffmpeg.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
tar -xf "$ARCHIVE" -C "$WORK"
cd "$WORK/ffmpeg-9.0.2"
export MACOSX_DEPLOYMENT_TARGET=14.0
./configure --prefix=/opt/recording-transcriber --arch=arm64 --target-os=darwin --cc=/usr/bin/clang \
  --extra-cflags='-arch arm64 -mmacosx-version-min=14.0' \
  --extra-ldflags='-arch arm64 -mmacosx-version-min=14.0' \
  --disable-everything --disable-autodetect --disable-network \
  --disable-shared --enable-static --disable-gpl --disable-nonfree --disable-version3 \
  --disable-programs --enable-ffmpeg --disable-ffplay --disable-ffprobe \
  --disable-doc --disable-debug --disable-asm --enable-small \
  --disable-avdevice --disable-swscale --enable-swresample --enable-avfilter \
  --enable-protocol=file --enable-demuxer=wav,mp3,mov,aac,flac,ogg,asf,amr \
  --enable-muxer=wav,null \
  --enable-decoder=pcm_alaw,pcm_mulaw,pcm_s8,pcm_u8,pcm_s16be,pcm_s16le,pcm_u16be,pcm_u16le,pcm_s24be,pcm_s24le,pcm_u24be,pcm_u24le,pcm_s32be,pcm_s32le,pcm_u32be,pcm_u32le,pcm_s64be,pcm_s64le,pcm_f32be,pcm_f32le,pcm_f64be,pcm_f64le,pcm_s16le_planar,pcm_s24le_planar,pcm_s32le_planar,mp3,mp3float,aac,aac_fixed,alac,flac,opus,vorbis,wmav1,wmav2,wmapro,wmavoice,amrnb,amrwb \
  --enable-parser=aac,aac_latm,mpegaudio,flac,opus,vorbis \
  --enable-encoder=pcm_s16le --enable-filter=aresample,aformat,dynaudnorm,silencedetect,anull,abuffer,abuffersink \
  > "$DESTINATION/configure.log" 2>&1
make -j8 > "$DESTINATION/make.log" 2>&1
cp ffmpeg "$DESTINATION/ffmpeg"
cp COPYING.LGPLv2.1 LICENSE.md "$DESTINATION/"
"$DESTINATION/ffmpeg" -version > "$DESTINATION/version.txt" 2>&1
"$DESTINATION/ffmpeg" -hide_banner -filters > "$DESTINATION/filters.txt" 2>&1
echo "已构建音频运行组件：$DESTINATION/ffmpeg"
