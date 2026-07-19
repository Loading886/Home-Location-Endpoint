# 进阶模式与 Telegram 定位控制

`advanced` 是本项目推荐的安装模式，介于新手完整模式和高手仅定位模式之间：项目仍负责
Xray、证书、定位路由、TCP 基线与运维命令，同时增加一个只授权单个 Chat ID 的 Telegram 菜单。

## 安装

交互安装选择“进阶模式”，再选择：

- `VLESS + REALITY + Vision`：推荐公网接入，SNI/target 由安装器统一管理并现场校验；
- `SS2022`：使用 `2022-blake3-aes-256-gcm`，同时监听同端口 TCP/UDP。

先通过 `@BotFather` 创建一个**专用于本节点**的 Bot，向它发送 `/start`，准备 Bot Token 和数字
Chat ID。安装器会隐藏 Token 输入、校验 Bot 身份与 Chat 可访问性；校验失败不会写入受管配置。

无人值守示例：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/v0.3.0/install.sh \
  | sudo env \
      HLE_VERSION=v0.3.0 \
      HLE_TELEGRAM_BOT_TOKEN='BOT_TOKEN' \
      HLE_TELEGRAM_CHAT_ID='NUMERIC_CHAT_ID' \
      bash -s -- --mode advanced --protocol vless-reality --port 443
```

不要与其他程序共用同一 Bot Token。Telegram 的 `getUpdates` 是单消费者模型；另一套轮询程序会
造成更新争用。安装器发现 Bot 已配置 webhook 时会拒绝接管，不会删除现有 webhook；本项目自身
不配置 webhook，也不开放 Bot 入站端口。

## 菜单

安装完成后，Telegram 输入框旁会出现 `Menu` 按钮；点击后可直接选择“打开定位菜单”或
“查看当前定位”。向 Bot 发送 `/menu`、`/start`、`/location` 或 `/status` 也可打开同一菜单：

交互式安装完成时，Bot 会主动发送四条互相独立的消息：代理凭据提示、纯节点 URI、CA 描述文件
安装说明，以及纯下载 URL。下载链接默认有效 100 分钟，并在首次成功下载后立即失效；Telegram
网页预览被禁用，避免预览请求提前消耗一次性链接。

- 选择地点：服务端立即将该地点设为活动位置并恢复定位改写，但 iPhone 可能继续使用定位缓存。
  请先等待几分钟；仍未变化时，到“设置 → 隐私与安全性 → 定位服务”关闭后重新打开，必要时重启手机。
- `🌍 真实定位`：暂停响应改写，但代理和 Apple 定位请求转发继续运行。
- `➕ 增加地点`：依次输入短名称、识别地址和 `纬度, 经度` 格式的 WGS84 坐标，确认后保存。
- `➖ 删除地点`：二次确认后删除非活动地点；当前地点和最后一个地点不可删除。
- `📱 获取描述文件`：直接发送可在 iPhone 安装的 CA `.mobileconfig` 附件。
- `🔗 获取节点链接`：按当前安装协议返回一行 `vless://` 或 `ss://` 节点 URI。

输入框旁的命令菜单也提供 `/profile` 和 `/node`。为了让 iPhone 能保存并安装附件、让代理客户端
能复制节点 URI，这两类消息不会启用 Telegram `protect_content`。CA 描述文件只含公钥证书；
节点 URI 含代理连接凭据，并会保留在 Telegram 私聊历史中，应使用专用私有 Bot 并妥善保管账号。

菜单沿用 Apple Relay 控制器的状态配色：蓝色表示普通可选操作，绿色表示当前地点或正向操作，
红色表示删除、取消等破坏性操作。切换地点或恢复真实定位后，绿色会随当前状态移动。

地点总数限制为 50 个（含预置与出口城市），避免生成超过 Telegram 实用范围的键盘；达到上限后
先删除不用的地点再增加。

首次安装会生成出口 IP 城市随机点，并为以下地点各生成一个安装级随机点：

`洛杉矶`、`东京`、`香港`、`新加坡`、`吉隆坡`、`巴黎`、`法兰克福`、`雷克雅未克`、
`南极昆仑站`。

随机点使用密码学随机源，在各城市中心的保守半径内按面积均匀抽取。每台服务器独立生成，
重复安装保留已有地点库；不会每次打开菜单就跳到另一个坐标。

南极昆仑站仍会在安装时于站点附近生成独立随机中心，但不会启用每 120 秒的平滑微漂移。高纬
地区的 Apple 数据可能只有少量 no-fix sentinel，或对精确 WifiTile 返回 404；拦截器会用近期
真实出现过的 Wi-Fi 身份和纯内存模板生成稳定的 45 m 微型簇。身份窗口为 30 分钟，缓存不会写盘，
切换地点时保持温热并按新目标重新变换，服务重启后才清空。iPhone 设备端缓存仍可能推迟可见变化。

## 权限与文件

Bot 运行在独立的 `home-location-bot` 系统账号：

```text
/etc/home-location-endpoint/telegram/token       root:home-location-bot 0640
/etc/home-location-endpoint/telegram/chat_id     root:home-location-bot 0640
/etc/home-location-endpoint/telegram/node-uri.txt root:home-location-bot 0640
/etc/home-location-endpoint/telegram/Home-Location-Endpoint-CA.mobileconfig
/var/lib/home-location-endpoint/control/         home-location-bot:home-location 0750
/var/backups/home-location-endpoint/              home-location-bot:home-location-bot 0700
/run/home-location-endpoint-bot/health            home-location-bot private runtime file
```

Bot 只能读取 Telegram 目录中的交付副本，不能读取 root-only 的原始 `node-uri.txt`、
`install.env`、Xray 配置或 `leaf.key`。定位拦截器只有地点文件所在组的只读权限；所有修改均采用
临时文件、`fsync` 和原子替换，并在改动前保留最近 30 份本地备份。

只有配置的 Chat ID 会被处理。其他 Chat 的消息被静默忽略。Bot 主动轮询 Telegram 官方 HTTPS
API，不监听公网端口；systemd 还限制可写目录、地址族、能力、命名空间、任务数和内存。

## 检查与故障

```bash
sudo hle status
sudo hle verify
sudo systemctl status home-location-telegram-bot --no-pager
sudo journalctl -u home-location-telegram-bot --since '30 minutes ago' --no-pager
```

`hle verify` 除了服务状态，还检查地点库、文件权限和最近 180 秒内的 Bot API 心跳。只有一次
`getUpdates` 长轮询成功后才会写入心跳；进程存活但 Token 已撤销、网络无法到达 Telegram、
同一 Token 被另一控制器占用或 API 长期报错时，心跳都会失败。

重新运行相同模式的安装器会复用地点库、Bot 凭据、代理凭据与有效 CA，并重新验证 Bot/Chat。
安装器不支持原地切换安装模式或代理协议；需要变更时先安全卸载，再重新安装并在手机更新节点。

## SS2022 注意事项

SS2022 使用 Xray 原生 Shadowsocks 入站和标准 SIP002 `ss://` URI。同一端口需要同时放行 TCP
与 UDP；Realm 只能转发 TCP，因此在 Realm 前置链路中 SS2022 的原生 UDP 不会通过。当前 Xray
会对 Shadowsocks 打印弃用警告，未来版本可能移除该实现；公网新部署优先选择 VLESS + REALITY，
SS2022 作为兼容选项使用，并在升级 Xray 前执行端到端测试。
