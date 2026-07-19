# 进阶模式与 Telegram 定位控制

`advanced` 介于新手完整模式和高手仅定位模式之间：项目仍负责 Xray、证书、定位路由、TCP
基线与运维命令，同时增加一个只授权单个 Chat ID 的 Telegram 菜单。

## 安装

交互安装选择“进阶模式”，再选择：

- `VLESS + REALITY + Vision`：推荐公网接入，固定 SNI/target 为 `www.usc.edu:443`；
- `SS2022`：使用 `2022-blake3-aes-256-gcm`，同时监听同端口 TCP/UDP。

先通过 `@BotFather` 创建一个**专用于本节点**的 Bot，向它发送 `/start`，准备 Bot Token 和数字
Chat ID。安装器会隐藏 Token 输入、校验 Bot 身份与 Chat 可访问性；校验失败不会写入受管配置。

无人值守示例：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/main/install.sh \
  | sudo env \
      HLE_TELEGRAM_BOT_TOKEN='BOT_TOKEN' \
      HLE_TELEGRAM_CHAT_ID='NUMERIC_CHAT_ID' \
      bash -s -- --mode advanced --protocol vless-reality --port 443
```

不要与其他程序共用同一 Bot Token。Telegram 的 `getUpdates` 是单消费者模型；另一套轮询程序会
造成更新争用。安装器发现 Bot 已配置 webhook 时会拒绝接管，不会删除现有 webhook；本项目自身
不配置 webhook，也不开放 Bot 入站端口。

## 菜单

向 Bot 发送 `/menu`、`/start`、`/location` 或 `/status` 可打开同一定位菜单：

- 选择地点：立即将该地点设为活动位置，并恢复定位改写；无需重启服务或代理连接。
- `🌍 真实定位`：暂停响应改写，但代理和 Apple 定位请求转发继续运行。
- `➕ 增加地点`：依次输入短名称、识别地址和 `纬度, 经度` 格式的 WGS84 坐标，确认后保存。
- `➖ 删除地点`：二次确认后删除非活动地点；当前地点和最后一个地点不可删除。

地点总数限制为 50 个（含预置与出口城市），避免生成超过 Telegram 实用范围的键盘；达到上限后
先删除不用的地点再增加。

首次安装会生成出口 IP 城市随机点，并为以下地点各生成一个安装级随机点：

`洛杉矶`、`东京`、`香港`、`新加坡`、`吉隆坡`、`巴黎`、`法兰克福`、`Reykjavík`、
`南极昆仑站`。

随机点使用密码学随机源，在各城市中心的保守半径内按面积均匀抽取。每台服务器独立生成，
重复安装保留已有地点库；不会每次打开菜单就跳到另一个坐标。

南极昆仑站仍会在安装时于站点附近生成独立随机中心，但不会启用每 120 秒的平滑微漂移。高纬
地区的 Apple 数据可能只有少量 no-fix sentinel，或对精确 WifiTile 返回 404；拦截器会用近期
真实出现过的 Wi-Fi 身份和纯内存模板生成稳定的 45 m 微型簇。缓存不会写盘，服务重启后清空。

## 权限与文件

Bot 运行在独立的 `home-location-bot` 系统账号：

```text
/etc/home-location-endpoint/telegram/token       root:home-location-bot 0640
/etc/home-location-endpoint/telegram/chat_id     root:home-location-bot 0640
/var/lib/home-location-endpoint/control/         home-location-bot:home-location 0750
/var/backups/home-location-endpoint/              home-location-bot:home-location-bot 0700
/run/home-location-endpoint-bot/health            home-location-bot private runtime file
```

Bot 不能读取 `install.env`、`node-uri.txt` 或 `leaf.key`。定位拦截器只有地点文件所在组的只读权限；
所有修改均采用临时文件、`fsync` 和原子替换，并在改动前保留最近 30 份本地备份。

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
