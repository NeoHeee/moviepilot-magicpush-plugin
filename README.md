# MoviePilot V2 MagicPush 消息通知插件

本插件监听 MoviePilot V2 的 `NoticeMessage` 通知事件，并通过 MagicPush 的 Token 推送接口发送消息。

## 功能

- 支持 MagicPush 自建地址和接口 Token
- 支持纯文本、Markdown、HTML
- 支持按 MoviePilot 通知类型筛选
- 支持在正文中附加海报
- 支持传递 MoviePilot 消息跳转链接
- 支持一键发送测试通知
- 自动跳过指定原生渠道的交互回复，减少重复消息

## MagicPush 端准备

1. 登录 MagicPush。
2. 新建一个“接口”。
3. 将需要使用的通知渠道绑定到该接口。
4. 复制该接口的 Token。
5. 先用以下命令测试 MagicPush 本身：

```bash
curl -X POST "http://你的MagicPush地址:端口/api/push/你的Token" \
  -H "Content-Type: application/json" \
  -d '{"title":"MagicPush测试","content":"接口工作正常","type":"markdown"}'
```

## 推荐安装方式：自建第三方插件仓库

1. 在 GitHub 新建一个仓库。
2. 将本压缩包解压后的所有内容上传到仓库根目录，目录结构不要改变。
3. 在 MoviePilot V2 的插件市场设置中添加该 GitHub 仓库地址。
4. 刷新插件市场，搜索“MagicPush消息通知”并安装。
5. 安装后进入插件配置页面填写参数。

仓库根目录应当是：

```text
package.json
package.v2.json
icons/
  magicpush.png
plugins.v2/
  magicpushmsg/
    __init__.py
```

## 插件配置

- **MagicPush地址**：例如 `http://192.168.1.10:3000`
- **接口Token**：MagicPush 接口页面生成的 Token
- **消息格式**：建议选择 Markdown
- **接收的通知类型**：留空代表全部接收
- **正文附加海报**：开启后将 MoviePilot 海报地址附加到正文
- **标题前缀**：例如 `[MoviePilot]`

填写完成后：

1. 开启“启用插件”。
2. 开启“发送测试通知”。
3. 保存配置。
4. MagicPush 收到测试消息后，再关闭“发送测试通知”并保存。

## 注意事项

- MoviePilot 原生通知渠道和本插件同时启用时，同一事件可能收到两次。
- 海报是否能显示取决于 MagicPush 下游通知渠道是否支持 Markdown 或 HTML 图片。
- MagicPush 和 MoviePilot 位于不同 Docker 网络时，请使用双方均可访问的 LAN 地址，不要填写 `localhost` 或 `127.0.0.1`。
- 如果填写的是完整地址 `/api/push/Token`，插件也能识别；通常只需填写 MagicPush 根地址和 Token。
