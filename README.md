# 本地音乐播放器（Local Music Player）

一个轻量的本地音乐播放器桌面程序，专为「已经下载到电脑里的音乐文件夹」设计。
界面只保留「正在播放」控制台：歌名 / 歌手、进度条、上一首 / 播放暂停 / 下一首、
随机、循环、音量，以及一个可展开的全曲列表。无广告、不上传、不依赖任何在线服务。

![screenshot](docs/screenshot.png)

> 上图为程序运行界面：正在播放曲目「降温」，底部为「📁 更换文件夹」按钮。

## 功能特性

- **本地优先**：直接播放你电脑上的音乐文件夹，文件不外传。
- **记忆目录**：首次用「选择文件夹」选过目录后，下次打开自动加载，无需重复选择。
- **拖拽加载**：把文件夹拖进窗口即可临时播放（拖入方式出于浏览器安全限制不记忆路径）。
- **无边框悬浮窗**：标题栏任意空白处拖动移动；右上角 `—` 最小化 / `✕` 关闭。
- **可缩放窗口**：鼠标移到窗口边框或四角，出现对应缩放光标，拖拽即可改大小。
- **播放模式**：仅两种——「顺序播放」与「单曲循环」（单曲循环时图标内显示 `1`）。
- **随机播放**：独立开关，可随时切换。
- **全曲列表**：双击歌名或点右上角 ☰ 打开，支持搜索；打开时仅保留列表自带关闭按钮。

## 目录结构

```
player/
├── app.py              # 入口：pywebview 窗口 + 内嵌本地文件服务（支持 Range 拖动定位）+ 目录记忆
├── player.html         # 无边框 WebView2 界面（控制台 + 列表浮层 + 缩放手柄）
├── requirements.txt    # Python 依赖
├── 音乐播放器.spec      # PyInstaller 打包配置
└── dist/               # 打包后的 exe（已被 .gitignore 忽略，见下方自行打包）
```

## 使用方式

### 方式一：直接运行（推荐）

从 Releases 下载已打包好的 `音乐播放器.exe`，双击即可。
首次打开点「📁 更换文件夹」选择你的音乐目录，之后会自动记忆。

### 方式二：自行打包成 exe

```bash
pip install -r requirements.txt
pyinstaller --noconfirm --onefile --windowed \
  --name "音乐播放器" \
  --add-data "player.html;." \
  --collect-submodules webview \
  --collect-submodules mutagen \
  --hidden-import pythonnet \
  app.py
```

生成的单文件 exe 位于 `dist/音乐播放器.exe`。

### 方式三：直接当网页用（开发调试）

`player.html` 也可在浏览器中打开调试，但「选择文件夹」需通过系统文件对话框，
拖拽加载与部分原生能力以浏览器能力为准。

## 技术栈

- [pywebview](https://github.com/r0x0r/pywebview) 6.2.1 —— 用系统原生 WebView（Windows 上为 Edge WebView2）承载界面，无控制台窗口。
- [mutagen](https://github.com/quodlibet/mutagen) 1.48.1 —— 读取音频时长等元数据。
- [PyInstaller](https://github.com/pyinstaller/pyinstaller) 6.22.3 —— 打包为单文件 exe。
- 内嵌 `http.server` 提供本地音频流，支持 HTTP Range，可在进度条任意位置拖动定位。

## 支持的音乐格式

`.mp3 / .flac / .m4a / .wav / .ogg / .aac / .wma`

## 许可证

[MIT](LICENSE)
