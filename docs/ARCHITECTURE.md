# 架构说明

## 安装模式

- `full`：新手模式，安装并管理 Xray、VLESS + REALITY + Vision 入站、定位路由、TCP 基线和
  已有 UFW 中的入站放行规则。
- `advanced`：进阶模式，在完整代理端点上增加 Telegram 多地点控制，可在首次安装时选择
  VLESS + REALITY + Vision 或 SS2022；模式和协议都不支持原地转换。
- `modifier-only`：只安装定位拦截器、证书和接线示例，不接管用户已有代理核心、网络端口或
  系统调优。

三种模式共用相同的定位选择、证书、拦截器与运维 CLI。首次安装后模式被写入
`/etc/home-location-endpoint/mode`，安装器拒绝原地跨模式转换。

## 数据面

以下是代理模式的数据面；仅定位模式由用户自管的代理核心完成第 1、2 和第 6 步。

1. 客户端通过 VLESS + REALITY + Vision，或进阶模式的 SS2022，连接落地机。
2. Xray 对入站流量启用 `http/tls/quic` sniffing，但使用 `routeOnly`，普通目标保持原目的地址。
3. 仅以下域名进入定位策略：
   - `gs-loc.apple.com`
   - `gs-loc-cn.apple.com`
   - `gspe85(-数字)?(-cn)?-ssl.ls.apple.com`
4. 上述域名的 UDP/443 被丢弃以促使 QUIC 回退 TCP；TCP/443 被送入
   `location-interceptor`。普通域名的 QUIC 不受影响。
5. `location-interceptor` 只监听 `127.0.0.1:10451`，不对公网开放。
6. 拦截器终止该内层 TLS，以 HTTP/1.1 向真实 Apple 源站重新发起请求，随后平移 WLOC 或
   WifiTile 响应中的坐标。
7. 其他流量命中默认 `direct` freedom 出站，从落地机直接访问目标。

代理模式在可创建 IPv6 双栈 socket 时监听 `::` 并显式设置 `v6only=false`；否则监听
`0.0.0.0`。这只决定代理入站，不代表云安全组或客户端自动允许 IPv6。

## 为什么不是每个响应都换一个城市内坐标

Apple 的一批返回通常包含多个 Wi-Fi AP 或蜂窝塔。把每个点压到完全相同的坐标，或让连续响应
在城市两端跳动，会破坏物理几何关系并容易被系统判为不可信。本项目：

- 安装或 `hle relocate` 时重新抽取城市内中心；
- 同一批次保留所有 AP/蜂窝点之间的相对距离和方向；
- 运行时在中心 8 m 内按 120 秒周期平滑移动；南极昆仑站预设关闭这层漂移；
- 对明确的 `(-180,-180)` 无定位批次生成不超过 45 m 的稳定微型簇；未知、畸形或只有部分
  无效点的响应不会被误判为安全 no-fix。

## 南极与无覆盖区

Apple 在高纬稀疏覆盖区可能只返回 1-3 条 no-fix sentinel，或让精确
`/wifi_request_tile` 返回 404。仅依靠单点改写会让 iOS 在“有效定位”和“无定位”之间反复。
本项目按以下顺序恢复：

1. WLOC 只有在整个可识别批次均为 sentinel 且少于 32 条时，才从最近 10 分钟同一进程看到的
   真实 Wi-Fi 身份补足至最多 32 条，再生成以目标为中心、半径不超过 45 m 的稳定坐标簇。
2. 精确 WifiTile 404 先复用 24 小时内缓存的完整 Apple tile 并平移几何关系。
3. 没有缓存时请求一个已知有效的 Apple 公共 seed tile，并平移到目标；仍失败时，才用近期真实
   Wi-Fi 身份构造最小 tile。
4. 没有任何可信身份或模板时保留 Apple 原始 404；可选回退失败不会扩大为定位服务整体故障。

Wi-Fi 身份最多保留 256 个、仅在内存中存在，服务重启即清空；完整模板也只保留一份。运行日志
只记录分支、数量和字节数，不记录 BSSID、坐标、tile key、请求体或响应体。

## 城市抽样

1. 从 `ipwho.is` 获取公网出口 IP、城市、行政区、国家、中心点与时区。
2. 按城市/行政区/国家向 Nominatim 请求一次经过拓扑保持简化的 Polygon/MultiPolygon，并缓存。
3. 在边界包围盒内使用拒绝采样，确认点位于外环且不在洞内。
4. 如果边界服务失败或没有可用多边形，在 IP 提供方中心 3 km 半径内按面积均匀随机。
5. 配置记录 `city-boundary` 或 `ip-center-radius-fallback`，便于审计。

## Xray 回环安全例外

新版 Xray 对来自 VLESS 入站、指向私网/保留地址的 freedom 连接有默认阻止策略。定位出站必须
访问回环拦截器，因此配置只显式放行：

```json
{
  "action": "allow",
  "network": "tcp",
  "ip": ["127.0.0.1/32"],
  "port": "10451"
}
```

普通 `direct` 出站没有增加 allow-all，因此仍保留 Xray 对私网目标的默认保护。

## Telegram 控制面

进阶模式把运行时地点配置移到 Bot 可原子更新、拦截器只读的控制目录。Bot 通过 Telegram
`getUpdates` 长轮询接收单个 Chat ID 的命令，不增加公网监听。每次成功连接 Bot API 都更新
`/run/home-location-endpoint-bot/health`，安装器和 `hle verify` 用它区分“进程存活”与“控制面
真正在线”。

切换地点时只替换 JSON 和 `modifier.state`；拦截器会在后续请求读取新状态，不重启 Xray，也
不终止已有普通代理连接。Bot 账号只获得控制目录和备份目录写权限，以及 Telegram 目录中节点
URI 交付副本和公开 CA 描述文件的只读权限。root-only 原始节点文件、`install.env`、Xray 配置和
叶证书私钥继续通过 Unix 权限及 systemd `InaccessiblePaths` 隐藏。

## 文件布局

```text
/etc/home-location-endpoint/
  mode                        full, advanced, or modifier-only
  install.env                 root-only node credentials
  location.json               active random city point
  jitter.seed                 smooth-drift seed
  ca.crt / ca.der             public CA certificate
  leaf.crt / leaf.key         scoped server leaf
  node-uri.txt                root-only VLESS or SS2022 URI
  runtime.env                 advanced interceptor path override
  telegram/token              advanced, root:bot 0640
  telegram/chat_id            advanced, root:bot 0640
  telegram/node-uri.txt       advanced Bot handoff copy, root:bot 0640
  telegram/Home-Location-Endpoint-CA.mobileconfig
  Home-Location-Endpoint-CA.mobileconfig
  xray-location-routing.example.json
/opt/home-location-endpoint/  Python runtime
/usr/local/etc/xray/config.json
/var/lib/home-location-endpoint/city-boundary.json
/var/lib/home-location-endpoint/modifier.state    active or paused
/var/lib/home-location-endpoint/control/          advanced presets and state
/var/backups/home-location-endpoint/               advanced atomic-change backups
/run/home-location-endpoint-bot/health             advanced Bot API heartbeat
/var/log/home-location-endpoint/interceptor.log
```

`modifier.state` 是持久运行状态。`active` 时拦截器执行坐标改写；`paused` 时仍完成限定域名的 TLS
转发，但使用手机请求的原始 Apple host 并返回未改写响应。这个设计避免因停服务导致定位不可用，
也避免为了切换状态重载 Xray 配置。

`node-uri.txt` 与 Xray 配置只存在于代理模式。仅定位模式的 `install.env` 不含代理凭据。进阶模式
的 Bot Token 独立保存，不写入 `install.env`。Telegram 交付副本由安装器从本次验证通过的节点 URI
和 CA 描述文件生成，Bot 不解析 `install.env`，也不接触 REALITY 私钥或叶证书私钥。

CA 私钥不保留在磁盘。叶证书到期或显式 `--rotate-ca` 时，需要轮换 CA，并在手机重新安装和信任
新描述文件。

## 安装事务

安装器在写受管文件前保存当前配置、运行时程序、systemd unit、状态缓存和代理模式的 Xray/
sysctl 文件。最终服务启动与 `hle verify` 任一步失败时，会先停新服务，再还原文件及原服务的
启用/运行状态。UFW 与 sysctl 的运行时应用被安排在事务提交后，避免失败安装留下开放端口或
半套内核参数。

安装失败时，本次新建的低权限账号/组也会按依赖顺序删除；既有账号不会动。APT 软件包、故障
日志和已加载内核模块不会回滚；强制断电或 `SIGKILL` 也无法执行进程内回滚。此时安装器会拒绝
猜测不完整状态，需按[运维与恢复](OPERATIONS.md)检查。
