#!/usr/bin/env bash
# Isolated GitHub-hosted macOS test volume; never run against a user volume.
set -euxo pipefail
[[ "$(uname -s)" == Darwin ]] || { echo 'ERROR: this test requires Darwin.' >&2; exit 2; }
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
AREA="$(mktemp -d -t flywheel-noatime)"
AREA="$(cd "$AREA" && pwd -P)"
IMAGE="$AREA/fixture.dmg"
VOLUME="$AREA/volume"
MOUNTED=0
cleanup() {
  local result=$?
  trap - EXIT
  if [[ "$MOUNTED" == 1 ]]; then
    if ! hdiutil detach "$VOLUME" -quiet; then
      echo "ERROR: isolated noatime test volume did not detach: $VOLUME" >&2
      result=1
    fi
  fi
  if [[ "$MOUNTED" == 0 || ! -d "$VOLUME/tmp" ]]; then
    rm -rf "$AREA"
  fi
  exit "$result"
}
trap cleanup EXIT
mkdir "$VOLUME"
hdiutil create -size 512m -fs APFS -volname FlywheelNoAtime "$IMAGE"
hdiutil attach "$IMAGE" -nobrowse -mountpoint "$VOLUME" -mountOptions noatime
MOUNTED=1
mount | grep -F " on $VOLUME " | grep -E '(^|[, (])noatime([,) ]|$)'
mkdir "$VOLUME/tmp"
export TMPDIR="$VOLUME/tmp" TEMP="$VOLUME/tmp" TMP="$VOLUME/tmp"
export FLYWHEEL_EXPECT_ATIME_CHANGE=0 PYTHONDONTWRITEBYTECODE=1
python3 - <<'PY'
import json, os, pathlib, tempfile
with tempfile.TemporaryDirectory(prefix='verify-noatime-') as name:
    path = pathlib.Path(name).resolve() / 'probe'
    path.write_bytes(b'noatime fixture\n')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.utime(fd, ns=(1_000_000_000, 2_000_000_000))
        before = os.fstat(fd)
        assert os.read(fd, 4096) == b'noatime fixture\n'
        after = os.fstat(fd)
        assert before.st_atime_ns == after.st_atime_ns, 'mounted test volume is not behaving as noatime'
        assert before.st_ctime_ns == after.st_ctime_ns
        print(json.dumps({'fixture': str(path.parent), 'noatime_verified': True,
                          'atime_before': before.st_atime_ns, 'atime_after': after.st_atime_ns}), flush=True)
    finally:
        os.close(fd)
PY
status=0
for suite in test-installer-atime-contract.py test-installer-atime-interaction.py test-installer-xattrs.py test-installer-xattr-install.py test-installer-confirmation.py test-installer-confirmation-transaction.py test-macos-metadata-contract.py; do
  python3 "$ROOT/tests/$suite" -v || status=1
done
exit "$status"
