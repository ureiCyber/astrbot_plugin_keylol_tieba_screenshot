# 其乐 / 百度贴吧主楼截图

一个为 [AstrBot](https://docs.astrbot.app/) 开发的帖子截图插件。自动识别群消息中的其乐（Keylol）和百度贴吧链接，将主楼内容生成适合手机阅读的 PNG 图片，也支持通过指令手动截图。

## ✨ 功能

- 识别其乐、贴吧帖子链接，支持 OneBot / NapCat 上报的贴吧 QQ 分享卡片。
- 截取帖子标题、主楼正文和图片，并附上作者、发布时间及来源链接。
- 优先保留网页的移动端布局，网页截图失败时自动尝试 HTML 兼容模式。
- 其乐目录帖可按章节生成多张图片，方便分段阅读（仅网页截图模式）。
- 支持配置登录 Cookie，读取当前账号有权查看的帖子内容及附件。
- 支持群消息去重、单条消息处理数量和截图并发限制。

只截取主楼，不包含后续回复。视频、音频会显示为封面或查看原帖的提示，不能在 PNG 中播放。

## 🚀 安装与更新

### 安装

1. 打开 AstrBot WebUI，进入 **插件管理**。
2. 点击 **＋ / 安装插件**，选择通过链接安装，粘贴下面的仓库地址。
3. 安装完成后启用插件，在插件配置中填写需要的 Cookie。

```text
https://github.com/ureiCyber/astrbot_plugin_keylol_tieba_screenshot
```

使用其乐公开帖子截图时可以不填 Cookie；使用贴吧功能前需要填写 `tieba_cookie`。安装入口可参考 [AstrBot WebUI 文档](https://docs.astrbot.app/use/webui.html#插件)。

### 浏览器准备

网页截图需要 AstrBot 所在环境能够使用 Chrome、Edge 或 Playwright Chromium。插件依赖包含 Playwright Python 组件，但不会自动下载浏览器内核。

如果没有可用浏览器，请在 **运行 AstrBot 的同一个 Python 环境** 中执行：

```bash
python -m playwright install chromium
```

使用 `uv` 安装 AstrBot 时，也要使用对应工具环境中的 Python；Docker 部署则需要在容器内准备浏览器。Linux 缺少系统依赖时可参考 [Playwright 浏览器安装说明](https://playwright.dev/python/docs/browsers)。默认 `auto` 模式在浏览器不可用时会尝试 HTML 兼容模式。

### 更新

在 AstrBot 插件管理中找到本插件，使用更新入口即可。插件通过公开仓库下载，不需要在部署机器上登录 GitHub；用 `uv` 升级 AstrBot 本体不会自动更新插件。

<details>
<summary>从 0.5.0 或旧 ZIP 安装的版本接入更新</summary>

如果旧版没有配置更新来源，请先备份 AstrBot 配置，再打开已安装插件目录中的 `metadata.yaml`，补上：

```yaml
repo: https://github.com/ureiCyber/astrbot_plugin_keylol_tieba_screenshot
```

保存并重载插件，然后再使用更新入口。重载本身不会下载新版本，也不要把上传同名 ZIP 当作覆盖更新或直接卸载旧插件。

</details>

## 📝 使用

### 自动识别

启用插件后，在群聊中发送帖子链接即可，无需指令：

```text
https://keylol.com/t1046223-1-1
https://tieba.baidu.com/p/10937213244
```

也可以分享贴吧 QQ 卡片，由插件读取其中的帖子链接。默认每条消息最多处理 1 个帖子链接，同一群内的相同链接在 60 秒内不会重复处理。

### 手动截图

```text
/keylol <其乐帖子链接>
/tieba <贴吧帖子链接>
```

例如：

```text
/keylol https://keylol.com/t1046223-1-1
```

不带链接时，`/keylol` 使用配置中的 `default_url`，`/tieba` 使用 `tieba_default_url`。后者默认为空，需要先设置或在指令后提供链接。

### 验证 Cookie

使用 **AstrBot 管理员账号私聊** 机器人：

```text
/keylol_check
/tieba_check
```

普通用户或群聊中的验证请求不会执行。群管理员不等于 AstrBot 管理员。

## ⚙️ 配置项

以下配置均可在 AstrBot WebUI 的本插件设置中修改。首次使用通常只需配置 Cookie，其余选项可保留默认值。

### 身份验证

- `keylol_cookie`：其乐登录 Cookie，选填。公开帖子不需要，登录后可见的附件需要有效 Cookie。
- `tieba_cookie`：贴吧登录 Cookie，使用贴吧功能时必填。必须包含 `BDUSS`，建议同时保留 `STOKEN`。

### 消息处理

- `auto_detect_enabled`：自动识别群消息中的链接和分享卡片，默认开启。
- `max_links_per_message`：每条消息最多处理的链接数，默认 `1`，范围 `1–3`。
- `dedupe_seconds`：同一群内相同链接的去重时间，默认 `60` 秒，设为 `0` 可关闭。
- `default_url`：`/keylol` 未提供链接时使用的其乐帖子地址。
- `tieba_default_url`：`/tieba` 未提供链接时使用的贴吧帖子地址，默认为空。

### 截图设置

- `keylol_render_engine` / `tieba_render_engine`：分别设置两站的截图引擎，默认均为 `auto`。
  - `auto`：优先网页截图，失败时尝试 HTML 兼容模式。
  - `playwright`：只使用网页截图，失败时返回错误，适合排查浏览器问题。
  - `html`：只使用清洗后的 HTML 生成截图。
- `content_width`：网页截图宽度，默认 `390` CSS 像素，范围 `320–440`。
- `split_toc_sections`：其乐目录帖按章节分图，默认开启，仅对其乐网页截图生效。
- `max_toc_sections`：目录分图上限，默认 `12` 张，范围 `1–20`；超出时只处理前面的章节。

### 高级设置

- `proxy_url`：两站网页截图及其乐内容请求使用的 HTTP / HTTPS 代理，默认为空；不作用于贴吧 API 兼容流程。请填写 AstrBot 所在环境可访问的地址。
- `max_concurrency`：同时处理截图的数量，默认 `2`，范围 `1–4`。
- `request_timeout_seconds`：内容解析与图片内嵌请求的超时，默认 `25` 秒；网页截图使用下方独立的超时设置。
- `browser_capture_timeout_ms`：网页截图基础超时，默认 `120000` 毫秒；其乐多章节截图会额外预留处理时间。
- `render_timeout_ms`：HTML 兼容模式渲染超时，默认 `30000` 毫秒。
- `adaptive_height`：裁掉 HTML 兼容模式截图底部的多余空白，默认开启。
- `inline_keylol_images` / `inline_tieba_images`：在 HTML 兼容模式中下载并内嵌对应站点的图片，默认开启，不影响网页截图模式。

### 如何获取 Cookie？

1. 在 Chrome 或 Edge 中登录 [其乐](https://keylol.com/) 或 [百度贴吧](https://tieba.baidu.com/)，打开一篇自己能够正常查看的帖子。
2. 按 `F12` 打开开发者工具，切换到 **Network（网络）**，刷新页面。
3. 选择当前帖子页面的请求（类型通常为 `document`），在 **Request Headers（请求标头）** 中找到 `Cookie`。
4. 复制完整的 Cookie 值，粘贴到 AstrBot 插件配置中对应的 `keylol_cookie` 或 `tieba_cookie`，保存并重载插件。
5. 使用 AstrBot 管理员账号私聊发送对应的验证指令，确认后再测试截图。

若标头中没有直接显示 Cookie，可右键帖子请求，选择 **Copy → Copy as cURL (bash)**，仅粘贴到本地记事本查看，再复制其中 `-b '...'` 或 `-H 'cookie: ...'` 的 Cookie 部分，不要粘贴整条 cURL 命令。贴吧 Cookie 至少应包含 `BDUSS`；若请求没有携带登录 Cookie，请重新登录后再试。

> Cookie 相当于账号登录凭证，会以明文保存在 AstrBot 配置目录中。请使用专用小号，只在可信的 WebUI 中填写，不要发送到群聊、Issue、日志或代码仓库。

## 📋 更新日志

### v0.5.1

- 接入 GitHub 仓库链接安装和插件更新。
- 将 Cookie 验证限制为 AstrBot 管理员私聊。
- 加强贴吧兼容模式的图片来源和重定向检查。
- 补充安装、配置和隐私说明。

### v0.5.0

- 新增贴吧真实移动网页截图。
- 支持在网页截图失败时自动尝试 HTML 兼容模式。

### v0.4.9

- 修复其乐目录多图只发送第一张的问题。
- 改进未配置 Cookie 时的公开页面处理。

### v0.4.8

- 新增其乐移动网页截图和目录分图。
- 改进长帖与懒加载图片的截图效果。

### v0.4.6

- 改进其乐登录后可见的附件图片处理。
- 为无法播放的音视频内容提供静态提示。

版本下载见 [Releases](https://github.com/ureiCyber/astrbot_plugin_keylol_tieba_screenshot/releases)。

## ⚠️ 使用须知

- 帖子内容来自其乐和百度贴吧，请尊重内容作者的权益及对应网站的使用规则。
- Cookie 仅用于访问对应站点，不会发送给第三方图床；账号能查看的受限内容可能被截图转发到群里，请控制插件的使用范围。
- 插件不执行发帖、回复、点赞或签到等操作；验证码、访问限制、内容删除及网站改版仍可能导致截图失败。
- 不要分享含 Cookie 的配置或日志；怀疑凭据泄露时，请及时撤销账号会话或修改密码。

遇到问题可提交 [Issue](https://github.com/ureiCyber/astrbot_plugin_keylol_tieba_screenshot/issues)，说明插件版本、截图模式和错误信息，并先移除个人信息及登录凭据。

## 📄 许可证

本项目采用 [MIT License](LICENSE)。

© 2026 キツネの嫁入り。
