# 清洁模式 · Clean Mode

锁住屏幕和键盘，安心擦一擦。

一个 macOS 小工具：点开始后，屏幕进入全屏倒计时，期间除 Esc 外所有按键（包括 ⌘Space、⌘Tab、⌘Q、⌘⇧3 等系统快捷键）都会被系统级拦截，你可以放心擦屏幕、擦键盘，时间到了自动恢复。

## 功能

- 全屏计时环，3 秒倒计时后开始，可选 30 秒 / 1 分钟 / 1 分半 / 3 分钟
- 清洁期间锁定键盘：基于 `CGEventTap`，⌘Space / ⌘Tab / ⌘Q / ⌘⇧3 等组合键也会被吞掉
- Esc 放行：连按两下 Esc 可提前结束
- 纯黑 / 纯白背景：黑底看灰尘，白底看指纹
- 清洁期间不会息屏、不会休眠（`caffeinate`）
- 安全兜底：界面若卡住超过 2.5 秒，按键自动放行，避免键盘被锁死
- 结束页显示用时，按任意键或点击关闭

## 运行

```bash
python3 clean_mode.py            # 菜单里选时长
python3 clean_mode.py 90         # 直接开始 90 秒
python3 clean_mode.py --white    # 白色背景
```

打包成 macOS 应用：

```bash
./build_app.sh                   # 生成 清洁模式.app
```

把生成的 app 拖进「应用程序」即可双击运行。

## 组合键拦截的授权

系统级拦截需要「辅助功能」权限：

1. 打开 app，菜单底部会显示「○ 组合键拦截未开启」；
2. 点这行字（或点「开始清洁」），前往 **系统设置 → 隐私与安全性 → 辅助功能**；
3. 勾选运行本程序的 **Python**（打包成 app 后，列表里通常显示为 `python3.x`）；
4. 回到 app，菜单底部变为「● 组合键拦截已开启」。

没有授权也能正常使用，只是 ⌘Space 这类系统快捷键拦不住，清洁界面底部会提示。

> 授权后如果仍提示「需要重启一次程序才生效」，把 app 关掉重开一次即可。

## 环境要求

- macOS（在 macOS 26 + Python 3.15 + Tk 9.0 上测试）
- Python 3.9+，且带 tkinter

## 说明

- 拦截只针对键盘，鼠标 / 触控板不受影响
- Esc 永远放行，避免把自己锁死
- 若进程异常退出，键盘会随之中断拦截、自动恢复

## English

A tiny macOS utility that freezes input while you wipe your screen and keyboard.
Full-screen countdown, system-level key blocking (requires Accessibility permission),
double-Esc to exit, dark/light backgrounds, prevents display sleep.

## License

MIT
