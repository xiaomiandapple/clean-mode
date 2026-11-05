#!/bin/bash
# 把 clean_mode.py 打包成 macOS 的「清洁模式.app」
# 用法：./build_app.sh
set -euo pipefail

cd "$(dirname "$0")"

APP="清洁模式.app"
BUNDLE_ID="com.cleanmode.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>清洁模式</string>
    <key>CFBundleDisplayName</key>
    <string>清洁模式</string>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleExecutable</key>
    <string>clean_mode</string>
    <key>CFBundleIconFile</key>
    <string>clean_mode</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleVersion</key>
    <string>1.1</string>
    <key>CFBundleShortVersionString</key>
    <string>1.1</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.utilities</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/clean_mode" <<'SH'
#!/bin/bash
# 挑一个带 tkinter 的 python3 来运行。
HERE="$(cd "$(dirname "$0")/../Resources" && pwd)"

for py in \
  /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
  "$(command -v python3 2>/dev/null)" \
  /usr/bin/python3
do
  [ -n "$py" ] || continue
  if "$py" -c 'import tkinter' >/dev/null 2>&1; then
    exec "$py" "$HERE/clean_mode.py" "$@"
  fi
done

osascript -e 'display alert "找不到带 tkinter 的 python3" message "请先安装 python3 与 python-tk（例如 brew install python-tk）。"'
SH

chmod +x "$APP/Contents/MacOS/clean_mode"
cp clean_mode.py "$APP/Contents/Resources/"
if [ -f assets/icon.icns ]; then
    cp assets/icon.icns "$APP/Contents/Resources/clean_mode.icns"
fi

echo "已生成：$APP"
echo "把它拖进「应用程序」文件夹即可双击使用。"
