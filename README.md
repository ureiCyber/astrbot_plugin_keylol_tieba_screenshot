# 其乐 / 百度贴吧主楼截图

一个为 [AstrBot](https://docs.astrbot.app/) 开发的帖子截图插件。自动识别群消息中的其乐（Keylol）和百度贴吧链接，将主楼内容生成适合手机阅读的图片，也支持通过指令手动截图。浏览器截取和拼接使用无损 PNG，发送前统一编码为 JPEG。

## ✨ 功能

- 识别其乐、贴吧帖子链接，支持 OneBot / NapCat 上报的贴吧 QQ 分享卡片。
- 截取帖子标题、主楼正文和图片，并附上作者、发布时间及来源链接。
- 其乐默认优先保留网页移动端布局，失败时自动尝试 HTML；贴吧默认通过客户端接口读取主楼并生成 HTML 截图。
- 其乐目录帖可按章节生成多张图片，方便分段阅读（仅网页截图模式）。
- 发送截图时引用触发截图的原消息，自动识别与手动指令均支持；目录多图合并在同一条回复中。
- 支持配置登录 Cookie，读取当前账号有权查看的帖子内容及附件。
- 支持群消息去重、单条消息处理数量和截图并发限制。

只截取主楼，不包含后续回复。视频、音频会显示为封面或查看原帖的提示，不能在截图中播放。

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

其乐网页截图和贴吧显式 `playwright` 实验模式需要 AstrBot 所在环境能够使用 Chrome、Edge 或 Playwright Chromium。贴吧默认 `html` 模式通过接口和 AstrBot HTML renderer 工作，不需要插件启动浏览器访问贴吧网页。插件依赖包含 Playwright Python 组件，但不会自动下载浏览器内核。

如果没有可用浏览器，请在 **运行 AstrBot 的同一个 Python 环境** 中执行：

```bash
python -m playwright install chromium
```

使用 `uv` 安装 AstrBot 时，也要使用对应工具环境中的 Python；Docker 部署则需要在容器内准备浏览器。Linux 缺少系统依赖时可参考 [Playwright 浏览器安装说明](https://playwright.dev/python/docs/browsers)。其乐的 `auto` 模式在浏览器不可用时会尝试 HTML；贴吧的 `auto` 是旧配置兼容值，直接使用 API + HTML，不访问贴吧网页。

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
- `tieba_cookie`：贴吧登录 Cookie，使用贴吧功能时必填。必须包含 `BDUSS`；主楼接口将 BDUSS 作为表单参数提交。HTML/API 模式下，可选的 `STOKEN` 仅用于贴吧正文图片请求，并且只会随请求发往 `tieba.baidu.com` 或 `www.tieba.baidu.com`。实验性浏览器模式也只向这两个域名注入 BDUSS 和可选 STOKEN；图片 CDN 和其他第三方主机不会收到登录 Cookie。

### 消息处理

- `auto_detect_enabled`：自动识别群消息中的链接和分享卡片，默认开启。
- `max_links_per_message`：每条消息最多处理的链接数，默认 `1`，范围 `1–3`。
- `dedupe_seconds`：同一群内相同链接的去重时间，默认 `60` 秒，设为 `0` 可关闭。
- `default_url`：`/keylol` 未提供链接时使用的其乐帖子地址。
- `tieba_default_url`：`/tieba` 未提供链接时使用的贴吧帖子地址，默认为空。

### 截图设置

- `keylol_render_engine`：其乐截图引擎，默认 `auto`。`auto` 优先网页截图，失败时尝试 HTML；`playwright` 只使用网页截图，失败时返回错误；`html` 只使用清洗后的 HTML。
- `tieba_render_engine`：贴吧截图引擎，默认 `html`。
  - `html`：推荐。通过贴吧客户端接口读取主楼，并生成安全 HTML 截图；需要含 BDUSS 的 `tieba_cookie`。
  - `playwright`：实验性真实网页截图，可能触发百度安全验证；失败时直接报错，不切换到其他模式。
  - `auto`：旧配置兼容值，等同 `html`，直接走接口和 HTML，不访问贴吧网页。
- `content_width`：帖子布局宽度，默认 `390` CSS 像素，范围 `320–440`。
- `split_toc_sections`：其乐目录帖按章节分图，默认开启，仅对其乐网页截图生效。
- `max_toc_sections`：目录分图上限，默认 `12` 张，范围 `1–20`；超出时只处理前面的章节。

`content_width` 控制帖子布局的 CSS 宽度，范围 `320–440`。Playwright 网页截图使用 Chromium 原生 DPR 2：390 CSS px 对应 780 物理像素，320 / 440 CSS px 分别对应 640 / 880 物理像素。贴吧默认 HTML/API 路径使用 AstrBot renderer 的 `device` 缩放和 `ultra` 档位（官方 DPR 1.8）：390 CSS px 对应 702 物理像素，440 CSS px 对应 792 物理像素。两种路径都由各自的渲染器直接输出目标物理像素，不通过截图后插值放大；其乐 HTML 路径继续输出 CSS 像素。

未触发安全限制时保留 renderer 输出的物理尺寸。网页长图先计算安全画布，再从无损 PNG 分段按统一坐标拼接；只有达到发送安全限制才等比缩小。每张目录图、贴吧图及 HTML 图发送前均检查：最长边不超过 **16384 px**、总像素不超过 **20,000,000**、JPEG 不超过 **10 MiB**。这些是内部硬限制，不提供配置项。独立的 **100000 CSS px** 网页高度上限用于阻止失控页面。

JPEG 优先使用 quality 100；体积超限时寻找 50–100 内可用的最高整数质量，仍无法满足才搜索更小的尺寸。所有编码尝试都来自无损像素；最终校验失败则不发送该截图。图片组件使用内存中的 JPEG 字节，浏览器产生的临时 PNG 会在组装消息后清理。

贴吧 API + HTML 是正式默认路径：贴吧客户端接口读取主楼后，由 AstrBot 的 `html_render` 生成图片。贴吧 HTML 截图使用 `scale=device` 和 `device_scale_factor_level=ultra` 请求原生 DPR 1.8，再按内容裁掉底部多余空白；裁剪只改高度，不改物理宽度。它仍执行上述全部发送安全检查，未触发限制时不会缩回 CSS 宽度。其乐的 HTML 路径保持原有 CSS 像素输出。接口依据：[AstrBot HTML 渲染代理](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/utils/t2i/network_strategy.py)、[官方渲染服务的 DPR 参数](https://github.com/AstrBotDevs/astrbot-t2i-service/blob/main/src/render.py)。

### 高级设置

- `proxy_url`：两站网页截图、其乐内容请求及 Steam Widget 商品卡使用的 HTTP / HTTPS 代理，默认为空；不作用于贴吧 API 请求或其他外部图片。Steam Widget 代理仅访问同一 AppID 的商店 Widget 与允许的官方封面路径，每次重定向仍检查地址并固定已验证的公共 IP，环境代理始终关闭。贴吧默认 HTML/API 模式不会访问真实贴吧网页。请填写 AstrBot 所在环境可访问的地址。
- `max_concurrency`：同时处理截图的数量，默认 `2`，范围 `1–4`。
- `request_timeout_seconds`：内容解析与图片内嵌请求的超时，默认 `25` 秒；网页截图使用下方独立的超时设置。
- `browser_capture_timeout_ms`：网页截图基础超时，默认 `120000` 毫秒；其乐多章节截图会额外预留处理时间。
- `render_timeout_ms`：AstrBot HTML renderer 渲染超时，默认 `30000` 毫秒。
- `adaptive_height`：裁掉 HTML 截图底部多余空白，默认开启；贴吧 HTML 截图按 renderer DPR 换算裁剪留白，不改变宽度。
- `inline_keylol_images` / `inline_tieba_images`：在 HTML 路径中下载并内嵌对应站点的图片，默认开启；不影响网页截图模式。贴吧 API 图片按 `origin_src`、`big_cdn_src`、`cdn_src`、`src` 的顺序选取资源；下载只访问 HTTPS 图片白名单，逐跳校验重定向，限制单图和图片总量。

### 如何获取 Cookie？

1. 在 Chrome 或 Edge 中登录 [其乐](https://keylol.com/) 或 [百度贴吧](https://tieba.baidu.com/)，打开一篇自己能够正常查看的帖子。
2. 按 `F12` 打开开发者工具，切换到 **Network（网络）**，刷新页面。
3. 选择当前帖子页面的请求（类型通常为 `document`），在 **Request Headers（请求标头）** 中找到 `Cookie`。
4. 复制完整的 Cookie 值，粘贴到 AstrBot 插件配置中对应的 `keylol_cookie` 或 `tieba_cookie`，保存并重载插件。
5. 使用 AstrBot 管理员账号私聊发送对应的验证指令，确认后再测试截图。

若标头中没有直接显示 Cookie，可右键帖子请求，选择 **Copy → Copy as cURL (bash)**，仅粘贴到本地记事本查看，再复制其中 `-b '...'` 或 `-H 'cookie: ...'` 的 Cookie 部分，不要粘贴整条 cURL 命令。贴吧 Cookie 至少应包含 `BDUSS`；若请求没有携带登录 Cookie，请重新登录后再试。

> Cookie 相当于账号登录凭证，会以明文保存在 AstrBot 配置目录中。请使用专用小号，只在可信的 WebUI 中填写，不要发送到群聊、Issue、日志或代码仓库。

## 📋 更新日志

### v0.5.7

- 修复其乐帖子内 Steam Widget 商品卡解析，兼容中英文商品标题和新版封面地址，展示简介、原价、折扣与现价。
- 商品卡采用适合移动端阅读的静态布局；封面加载失败时仍保留商品文字，Steam 错误页显示对应提示，视频保持静态封面或原帖提示。
- Steam Widget 和对应封面支持已配置的 `proxy_url`；代理请求逐跳校验同一 AppID、允许的域名与资源路径，并固定已验证的公共 IP。
- 代理认证兼容项目支持的 aiohttp 版本；补充商品卡、代理连接、重定向限制与诊断脱敏的回归测试。

### v0.5.6

- 贴吧默认改为客户端 API + HTML renderer；历史配置 `auto` 直接使用该路径，只有显式 `playwright` 才访问真实贴吧网页，且失败直接报错。
- 贴吧 HTML 截图使用官方 renderer 的 ultra DPR 1.8，并保留 BDUSS 表单提交、原图优先和图片来源及大小限制；接口请求禁用重定向并清理远端错误文本，避免认证值进入异常信息。
- 390 / 440 CSS px 分别输出 702 / 792 物理像素；裁底不改变宽度，继续执行全部发送安全限制。其乐截图行为保持不变。

### v0.5.5

- 贴吧首帖定位支持多种楼层标记，并等待异步正文准备完成。
- 单独识别登录、百度安全验证、风控及页面结构异常，避免统一误报 `missing_first_post`。
- 原生路径失败时保存脱敏诊断信息与本地页面快照，保留 HTML 回退和全部图片安全限制。
- 补充原生 DPR2 截图到发送边界的浏览器集成验证。

### v0.5.4

- 自动识别链接、`/keylol` 与 `/tieba` 发送截图时，引用触发截图的原消息。
- 多链接和目录分图共用一次引用；平台未提供消息 ID 时仍正常发送截图，引用显示效果取决于平台支持。
- 改进贴吧 Cookie 格式兼容及截图失败诊断，并记录发送前后的图片尺寸，方便排查清晰度问题。

### v0.5.3

- 其乐和贴吧网页截图统一使用原生 DPR2，无损分段拼接；安全范围内保留完整物理分辨率。
- 长图在分配画布前自适应计算安全尺寸，修复分段缩放的细节接缝问题。
- 所有发送路径统一执行最长边、总像素和 10 MiB 文件体积安全锁，并自适应选择 JPEG 质量。
- 使用图片字节发送并清理浏览器临时文件，补充截图、编码、发送与取消清理测试。
- HTML 兼容模式保留真实 CSS 像素输出，不进行后期放大。

### v0.5.2

- 改进其乐与贴吧网页截图中的外部图片、嵌入内容和折叠内容处理。
- 修复贴吧表情在 API 与兼容模式中的显示，并加强媒体下载、重定向和资源边界校验。
- 补充对应的安全回归测试与测试夹具。

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
- Cookie 仅用于访问对应站点，不会发送给第三方图床。贴吧 API 使用 BDUSS 表单参数读取主楼；HTML/API 模式下，可选 STOKEN 仅随图片请求发送到两个精确贴吧页面 host，离开这些 host 的重定向会移除凭据。账号能查看的受限内容可能被截图转发到群里，请控制插件的使用范围。
- 插件不执行发帖、回复、点赞或签到等操作；验证码、访问限制、内容删除及网站改版仍可能导致截图失败。
- 不要分享含 Cookie 的配置或日志；怀疑凭据泄露时，请及时撤销账号会话或修改密码。

遇到问题可提交 [Issue](https://github.com/ureiCyber/astrbot_plugin_keylol_tieba_screenshot/issues)，说明插件版本、截图模式和错误信息，并先移除个人信息及登录凭据。

## 开发验证

仅显式选择贴吧 `playwright` 实验模式时，失败日志的 `page_diagnostics` 才会记录最终 URL（查询值已遮盖）、标题、加载状态、HTML/正文长度、页面特征、各候选 selector 命中数、HTTP 状态及被拦截的资源数量。失败时会在插件目录 `logs/debug/tieba/` 保存带时间戳的 HTML 和视口截图；HTML 会遮盖已知登录凭据，文件不会加入发送链。诊断保存失败不会改变原始错误；Playwright 失败会直接报错，不会回退。`html` 和历史 `auto` 只调用贴吧 API + HTML renderer，不访问真实贴吧网页，也不会触发这组网页诊断。目录已加入 Git 忽略列表，截图与 HTML 可能含帖子内容及账号显示信息。

Playwright 实验模式按以下顺序定位首帖，等待与 transform 共用同一套规则：

1. `[data-field] / [data-field-json]` 的 `content.post_no / floor` 等楼层元数据（桌面与数据驱动布局）。
2. `[data-floor] / [data-post-no]` 的明确楼层属性（移动或语义布局）。
3. `.l_post / .j_l_post` 内全部楼层尾注（缺少 JSON 的旧桌面布局）。

每种结构都必须确认 1 楼，再从稳定的 `post_content_` ID、桌面正文 class 或移动正文标记提取非空正文，不把首个回复或整个页面当首帖。导航后最多再等待 10 秒（不超过当前浏览器超时配置），直到首帖正文出现；不会把 `domcontentloaded` 当成正文已就绪，也不以固定 sleep 或 `networkidle` 作为首帖等待条件。允许白名单内的第一方 HTTPS `.js` 与同帖 GET 读取，仍阻止其他 API、非 GET、非目标文档和 iframe 导航。

异常原因包括 `tieba_login_required`、`tieba_verify_required`、`tieba_risk_control`、`tieba_app_redirect`、`tieba_post_not_found`、`tieba_permission_denied`、`tieba_blank_page`、`tieba_page_not_loaded`、`tieba_first_post_timeout`（有加载中或未完成的主楼证据）、`tieba_dom_changed`（未识别结构）和 `tieba_transform_failed`（脚本处理异常）。普通导航栏的登录或下载提示不应单独判定为异常。验证码页面只做识别和诊断，实验模式直接返回错误；贴吧默认 API + HTML 路径不会访问该网页。

安装 `requirements.txt` 中的依赖后，运行全部测试：

```bash
python -m unittest discover -s tests -v
```

其中贴吧 HTML 集成测试使用本机 Edge / Chrome 离线渲染实际正文模板，验证 DPR 1.8 的输出宽度、自适应裁底和 JPEG 安全编码；不请求线上 T2I 服务或真实贴吧网页。

本地 Chromium 截图夹具会阻止所有网络请求，验证两站共享的分段逻辑、三种 CSS 宽度、短图和超限长图，并在 `.test-tmp/capture-segmented-fixture` 输出 JPEG 与 JSON 报告：

```bash
python tests/capture_segmented_fixture.py --chrome "浏览器可执行文件路径"
```

## 📄 许可证

本项目采用 [MIT License](LICENSE)。

© 2026 キツネの嫁入り。
