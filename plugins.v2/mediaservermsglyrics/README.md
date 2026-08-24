# 媒体库服务器通知·实时歌词

基于 MoviePilot 官方“媒体库服务器通知”插件制作的独立 V2 插件。

## 1.1.6 更新

- 普通 LRC 只使用行级时间戳：当前行直接整行高亮，不再根据行时长模拟逐字扫光。
- 只有包含 `<mm:ss.xx>` 字词时间戳的增强 LRC 才启用精确逐字高亮，网页和 Apple TV 行为一致。
- 补充 GitHub 第三方插件市场结构、在线安装地址和开源来源说明。

## 1.1.5 更新

- 手机与网页端彻底移除封面 Liquid Glass 底座、悬浮动画和叠加高光，改成固定正方形 `<img>` 直接显示。
- Plex 封面在服务端统一方向、色彩模式并裁切为 `800×800` JPEG，规避 iOS 微信 WebView 只绘制半张图片。
- 每首歌返回独立 `cover_version`，封面接口改为禁止缓存；旧通知链接自动跟随下一首时会立即清除旧封面并加载新封面。
- 支持增强 LRC 的 `<mm:ss.xx>` 逐字时间戳，手机网页与电视画面都会按真实字词进度高亮。
- 1.1.5 曾为普通逐行 LRC 提供估算扫光；该行为已在 1.1.6 取消，当前版本只做整行高亮。

## 1.1.4 更新

- 电视投屏升级为 `3840×2160 / 30 FPS` H.264 输出；1080p 高精度场景由 FFmpeg 放大到 4K，避免 Python 搬运原生 4K RGB 帧造成卡顿。
- 封面模糊、动态色场、玻璃托盘与阴影改为后台预合成缓存，播放期间每帧只更新歌词和进度。
- 自动探测 Intel Quick Sync；MoviePilot 容器已映射 `/dev/dri` 时使用核显缩放和编码，否则自动回退 libx264。
- 电视歌词字号和字重重新调整，活动歌词增加克制的描边，改善 4K 电视上的锯齿和过细观感。
- 手机封面不再使用 `<img>` 百分比布局，改为固定正方形背景层，规避 iOS 微信 WebView 只渲染半张图片。
- 已打开的手机歌词链接会跟随同一 Plex 播放设备自动切换到下一首，包括新歌词、封面、音质和进度。

## 1.1.3 更新

- 修复 iOS 微信 WebView 中左上角专辑封面只显示半张的问题：使用独立组件类名与明确的正方形尺寸，不再依赖嵌套百分比高度。
- 修复手机端设备行点按无反应：使用指针事件委托、移动端触摸优化，并立即显示“连接中”反馈。
- 正式电视流由 MJPEG 改为低延迟 H.264/MPEG-TS，解决 UnPlay 在 Apple TV 上持续转圈的问题。
- 每次投屏生成唯一取流标记，规避重复任务出现 `file exists`；无效或过期标记会明确返回 404。
- 不再把 UnPlay HTTP 200 当成成功：会等待电视实际取流并检查 `/PlaybackEvent` 进入 `PLAYING`，失败时区分“没有访问流”和“已取流但未播放”。

## 1.1.2 更新

- 网页和 Apple TV 画面都会从当前专辑封面提取三个主色，生成低速、低振幅的高斯模糊动态色场；下一首歌曲会自动换色。
- 封面改为轻微悬浮在窄边 Liquid Glass 托盘上，玻璃只负责承托封面，不包裹歌词内容。
- 投屏按钮改为通过 SSDP 自动发现同一局域网的 UnPlay，并显示 Apple TV 设备选择器；IP 地址降级为 Docker 组播不可达时的备用配置。
- 网页背景运动使用 GPU 变换，并支持系统“减少动态效果”；电视端以低分辨率柔光层合成，控制渲染开销。
- 保留 1.1.1 的 Apple Music 双栏布局、同步歌词、防跳动时钟和音质信息。

## 1.1.1 更新

- 依据 Apple 官方 tvOS 26 Apple Music 歌词页重新设计电视画面：沉浸式封面取色背景、左侧专辑信息、右侧同步大字歌词。
- 移除 1.1.0 中所有装饰弧线、歌词玻璃卡片、状态圆点和重复音质标签。
- 遵循 Apple HIG 的内容层级：Liquid Glass 只适合控制与导航，不再把歌词等内容放进玻璃容器。
- 音质信息保留为专辑信息下方的低调纯文本，进度条改为底部细线。

## 1.1.0 更新

- 新增 Apple TV / UnPlay 非镜像投屏：歌词页可一键启动独立电视歌词画面。
- 使用 MoviePilot 内置 Pillow 实时生成 MJPEG 直播流，不要求容器额外安装 ffmpeg。
- 电视频道绑定 Plex 播放设备；同一设备开始下一首后沿用原直播流并自动换歌。
- 电视画面使用专辑封面取色、高斯模糊背景和 Apple Music 风格内容布局。
- 从 Plex Media/Part/AudioStream 元数据读取编码、采样率、位深和码率，例如 `FLAC · 44.1 kHz / 16-bit · 916 kbps`。
- 插件服务端直接调用 UnPlay HTTP API，避免手机浏览器的跨域和 HTTPS 混合内容限制。
- 内置 Noto Sans SC 字体，确保 MoviePilot Docker 环境正确显示中文歌词。

## 1.0.2 更新

- 根据实际录屏修复 Plex `viewOffset` 分段心跳导致的 `01:17 → 01:19 → 01:18` 循环回退。
- 远端进度不变时不再覆盖本地播放时钟，歌词和进度条持续单调前进。
- Plex 心跳刷新时只做有限的向前校正；检测到真正拖动进度时仍会立即同步。

## 1.0.1 更新

- 使用单一平滑播放时钟吸收 Plex 轮询抖动，避免歌词在相邻行之间反复回跳。
- 当前歌词不再改变布局字号，改用轻微缩放和颜色强调。
- 进度条改为逐帧 GPU 变换，播放、暂停和跳转时更及时。
- PC 浏览器改为左侧专辑信息、右侧歌词的响应式双栏布局。

## 功能

- 保留 Emby、Jellyfin、Plex 的播放、停止、入库等原通知能力。
- 正确显示 Plex 音乐的歌曲名、歌手和专辑。
- Plex 开始播放音乐时，企业微信通知链接跳转到实时歌词页。
- 歌词页每秒同步 Plex 当前会话的播放、暂停、停止、进度和跳转。
- 优先读取音频文件旁边的同名 `.lrc` 或 `.txt`。
- 本地歌词缺失时，可自动从 LRCLIB 查询同步歌词。
- 使用 Plex 专辑封面取色，生成高斯模糊的深色渐变背景。
- Plex Token 仅在服务端使用；浏览器通过插件的同源封面代理读取图片。
- 歌词链接使用随机临时会话 ID，默认六小时后失效。
- 可把歌词画面作为独立媒体流投到 Apple TV，不占用手机画面，也不是屏幕镜像。

## 在线安装（推荐）

本插件已发布在 PlaySong 的 MoviePilot 第三方插件市场：

```text
https://github.com/songdone/MoviePilot-Plugins
```

1. 打开 MoviePilot 的“设置 → 插件市场”。
2. 在“插件市场仓库”或环境变量 `PLUGIN_MARKET` 中加入上面的仓库地址。
3. 如果需要继续使用官方市场，请保留官方地址，并用英文逗号分隔多个仓库。
4. 刷新插件市场，搜索“媒体库服务器通知·实时歌词”并在线安装。
5. 安装完成后进入插件配置页，选择 Plex 媒体服务器并保存。

本项目目前是 MoviePilot V2 插件，市场索引位于 `package.v2.json`。使用 MoviePilot V3 时不会出现在 V3 专用市场中。

## 本地安装（备用）

1. 在 MoviePilot 的“本地插件安装”中选择完整 ZIP 文件。
2. 打开“媒体库服务器通知·实时歌词”。
3. 选择 Plex 媒体服务器。
4. 消息类型至少勾选“开始播放”；需要停止通知时再勾选“停止播放”。
5. 保持“Plex音乐通知打开实时歌词”和“缺少本地歌词时在线获取”开启。
6. 歌词页公网地址填写 `https://mp.playsong.cn`。
7. 保存配置后播放一首 Plex 音乐测试。

### 配置 UnPlay 投屏

1. 在 Apple TV 打开 UnPlay，并保持它处于可接收状态。
2. 从微信打开歌词页，点击“投到电视”；插件会按 UPnP/SSDP 规范自动搜索并显示同网段的 UnPlay 设备。
3. 选择 Apple TV 后，UnPlay 会使用 FFmpeg 播放器打开插件生成的 H.264/MPEG-TS 直播流；Plex 同一播放设备换到下一首时，电视歌词自动同步。
4. 正常情况下无需填写 IP。只有 MoviePilot 的 Docker 网络阻止 UDP 1900 组播、设备列表始终为空时，才在“UnPlay 备用 IP”填写 `192.168.1.111:9030`；也可以把容器改为 host/macvlan 等能访问局域网组播的网络模式。

插件会调用 MoviePilot 容器内的 FFmpeg 编码电视流；若定制镜像移除了 FFmpeg，网页会直接显示明确错误，不会再让电视一直转圈。

### 为 4K/30 启用 Intel Quick Sync

N305 的核显足以承担本插件的 4K 缩放与 H.264 编码。Docker 部署时请把核显设备映射给 MoviePilot 容器：

```yaml
devices:
  - /dev/dri:/dev/dri
```

保存编排并重建 MoviePilot 容器后，再发起投屏。网页成功提示中显示“Intel Quick Sync”即表示核显已启用；若显示“libx264 软件编码”，插件仍可播放，但应检查宿主机是否存在 `/dev/dri/renderD*` 以及容器设备映射。

电视流只承载歌词画面，不重新传送歌曲音频。音乐应继续由原 Plex/Plexamp 播放设备输出。如果音乐本来就在 Apple TV 的 Plex App 内播放，切换到 UnPlay 会被 tvOS 视为切换应用并可能停止原 App 的音频，这属于 tvOS 应用模型限制。

UnPlay 接收的是已经渲染好的 H.264 视频帧，不会执行 SwiftUI，因此此投屏模式不能调用 tvOS 26 原生 `glassEffect(_:in:)`。真正的系统 Liquid Glass 必须制作并安装独立 tvOS 原生 App；本插件遵循其信息层级和克制原则，但不会把模拟效果标注成苹果原生 Liquid Glass。

安装并启用本插件后，请停用官方“媒体库服务器通知”和之前的 Plus 版本，否则同一播放事件会产生重复通知。

## 本地歌词路径

插件通过 Plex 返回的音频文件路径查找同名歌词，例如：

```text
/music/刘若英/我等你/01 我爱洗澡.flac
/music/刘若英/我等你/01 我爱洗澡.lrc
```

MoviePilot 容器必须能读取该音乐目录。如果 Plex 和 MoviePilot 容器内的路径不同，在“音乐路径映射”中每行填写一条：

```text
/volume1/music => /media/music
```

路径映射只负责转换路径，不能代替 Docker/NAS 的目录挂载。如果 MoviePilot 完全没有挂载音乐目录，本地歌词无法读取，但在线回退仍可工作。

## 公网访问

现有反向代理需要把以下路径转发到 MoviePilot：

```text
/api/v1/plugin/MediaServerMsgLyrics/lyrics
/api/v1/plugin/MediaServerMsgLyrics/lyrics/state
/api/v1/plugin/MediaServerMsgLyrics/lyrics/cover
/api/v1/plugin/MediaServerMsgLyrics/lyrics/cast
/api/v1/plugin/MediaServerMsgLyrics/lyrics/tv.ts
/api/v1/plugin/MediaServerMsgLyrics/lyrics/tv.mjpg
```

如果 `https://mp.playsong.cn` 已经整体反向代理到 MoviePilot，通常不需要新增规则。

`tv.ts` 是正式投屏使用的长连接直播流，`tv.mjpg` 仅作为旧版调试兼容。若反向代理单独配置了超时或缓冲，请为 `tv.ts` 关闭响应缓冲并延长读取超时；Nginx 可使用 `proxy_buffering off; proxy_read_timeout 12h;`。插件响应同时带有 `X-Accel-Buffering: no`。

## 回滚

停用本插件，重新启用官方插件或旧 Plus 插件即可。插件不会修改 MoviePilot 主程序、Plex 数据库、Docker 配置或企业微信设置。

## 开源说明

本插件派生自 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins/tree/main/plugins.v2/mediaservermsg) 中的 `MediaServerMsg`，由 PlaySong 在独立类 `MediaServerMsgLyrics` 中继续开发，避免覆盖官方插件。项目与修改代码继续遵循 GPL-3.0；内置 Noto Sans SC 字体的许可证见 `FONT-LICENSE.txt`。在线歌词回退使用 LRCLIB 公共接口。

源码与版本发布页：

- 源码：<https://github.com/songdone/MoviePilot-Plugins/tree/main/plugins.v2/mediaservermsglyrics>
- Releases：<https://github.com/songdone/MoviePilot-Plugins/releases>
