#!/bin/bash
# Build two copies of Yamaha's reface CS/DX updater app:
#   updater/stock/reface updater.app    - payload rebuilt by refacefw.py (byte-identical to Yamaha's)
#   updater/noclock/reface updater.app  - payload with the no-clock patch
# Both are ad-hoc re-signed and de-quarantined so macOS will launch them.
set -euo pipefail
cd "$(dirname "$0")/.."

ZIP=updater/reface_updater_CSDX_v130-3_mac.zip
EXPECT_ZIP=291e5836af25d39e536ec2fd3d8cf82905edfce6d0d86692023656d9697889c2
EXPECT_DAT=645309bef54e9444ebdeeacaa42f727b0fd0bdcfc06992ca1adde30a16112abb
EXPECT_NOCLOCK=af1d24539c2f064c192a0d2bc86c06bbfefdcabc1486b5bcefe9379817ce4799
APP="reface updater.app"
DAT="$APP/Contents/Resources/reface updater.dat"

sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
[ "$(sha "$ZIP")" = "$EXPECT_ZIP" ] || { echo "zip hash mismatch"; exit 1; }
# the updater is x86_64-only; on Apple Silicon it needs Rosetta
if [ "$(uname -m)" = "arm64" ] && ! arch -x86_64 /usr/bin/true 2>/dev/null; then
  echo "Rosetta is not installed; the updater app cannot run. Install with: softwareupdate --install-rosetta"; exit 1
fi

rm -rf updater/orig && mkdir -p updater/orig
unzip -q "$ZIP" -d updater/orig
ORIG_APP="updater/orig/reface_updater_CSDX_v130-3_mac/$APP"
[ "$(sha "$ORIG_APP/Contents/Resources/reface updater.dat")" = "$EXPECT_DAT" ] || { echo "payload hash mismatch"; exit 1; }

# 1. rebuild stock payload and patched payload from the original
python3 -I tools/refacefw.py roundtrip "$ORIG_APP/Contents/Resources/reface updater.dat"
python3 -I tools/refacefw.py extract  "$ORIG_APP/Contents/Resources/reface updater.dat" out/reface_csdx_main_v130.bin
python3 -I tools/refacefw.py repack   out/reface_csdx_main_v130.bin out/reface_updater_CSDX_v130_stock_rebuilt.mid
cmp out/reface_updater_CSDX_v130_stock_rebuilt.mid "$ORIG_APP/Contents/Resources/reface updater.dat"
python3 -I tools/patch_clock.py out/reface_csdx_main_v130.bin out/reface_csdx_main_v130_noclock.bin out/reface_updater_CSDX_v130_noclock.mid
[ "$(sha out/reface_updater_CSDX_v130_noclock.mid)" = "$EXPECT_NOCLOCK" ] || { echo "patched payload hash mismatch: patch_clock.py output changed"; exit 1; }

# 2. make the two app copies
for variant in stock noclock; do
  rm -rf "updater/$variant" && mkdir -p "updater/$variant"
  cp -R "$ORIG_APP" "updater/$variant/"
  case $variant in
    stock)   cp out/reface_updater_CSDX_v130_stock_rebuilt.mid "updater/$variant/$DAT" ;;
    noclock) cp out/reface_updater_CSDX_v130_noclock.mid       "updater/$variant/$DAT" ;;
  esac
  xattr -cr "updater/$variant/$APP"
  codesign --force --deep --sign - "updater/$variant/$APP"
  codesign --verify --deep --strict "updater/$variant/$APP"
  echo "$variant: $(sha "updater/$variant/$DAT")  $(codesign -dv "updater/$variant/$APP" 2>&1 | grep -E '^Signature' || true)"
done
echo
echo "stock   payload must equal $EXPECT_DAT"
echo "noclock payload differs from stock in 6 bytes:"
cmp -l "updater/stock/$DAT" "updater/noclock/$DAT" || true
