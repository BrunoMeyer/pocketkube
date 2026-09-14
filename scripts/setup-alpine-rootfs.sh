#!/data/data/com.termux/files/usr/bin/sh
set -eu

ALPINE_VERSION="${ALPINE_VERSION:-3.24.0}"
ROOTFS="${POCKETKUBE_PROOT_ROOTFS:-$HOME/rootfs/alpine}"
BASE_DIR="$(dirname "$ROOTFS")"

case "$(uname -m)" in
  armv7l|armv8l|arm) ALPINE_ARCH=armv7 ;;
  aarch64|arm64) ALPINE_ARCH=aarch64 ;;
  *)
    echo "Unsupported architecture: $(uname -m)" >&2
    echo "Set up an Alpine rootfs manually and pass --rootfs PATH." >&2
    exit 1
    ;;
esac

ARCHIVE="alpine-minirootfs-${ALPINE_VERSION}-${ALPINE_ARCH}.tar.gz"
URL="https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/${ALPINE_ARCH}/${ARCHIVE}"

mkdir -p "$BASE_DIR"
cd "$BASE_DIR"

if [ ! -f "$ARCHIVE" ]; then
  echo "Downloading $URL"
  wget "$URL"
fi

rm -rf "$ROOTFS"
mkdir -p "$ROOTFS"

# GNU tar from the legacy Android 5 Termux repository has a symlink chmod bug.
# BusyBox tar works around it and is safe to use on newer Termux as well.
if command -v busybox >/dev/null 2>&1; then
  busybox tar -xzf "$ARCHIVE" -C "$ROOTFS"
else
  echo "busybox is required for reliable extraction on legacy Android Termux." >&2
  echo "Install it with: pkg install busybox" >&2
  exit 1
fi

# Alpine /bin/sh is normally an absolute symlink to /bin/busybox.
# From the Android host, `test -e` follows that absolute symlink against the
# host root and can therefore report a false negative. Accept either a real
# file or a symlink, and validate BusyBox separately.
if [ ! -e "$ROOTFS/bin/sh" ] && [ ! -L "$ROOTFS/bin/sh" ]; then
  echo "Extraction failed: $ROOTFS/bin/sh is missing" >&2
  exit 1
fi

if [ ! -e "$ROOTFS/bin/busybox" ]; then
  echo "Extraction failed: $ROOTFS/bin/busybox is missing" >&2
  exit 1
fi

echo "Alpine rootfs ready at: $ROOTFS"
echo "Start PocketKube with: pocketkube serve --runtime proot --rootfs '$ROOTFS'"
