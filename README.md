# Home-Location-Endpoint

把一台 Linux 服务器部署为 Apple 网络定位修改端点。安装器提供三种模式：

1. **新手模式**：一键式全自动安装定位修改器，并接入代理服务。
2. **进阶模式（推荐）**：增加 Telegram bot 控制，并可选择 更多不同的代理协议。
3. **高手模式**：只安装定位修改器，用户自己搭建代理相关服务。

三种模式都会按服务器公网出口 IP 识别城市，并在该城市内抽取随机坐标。普通代理流量不经
定位修改器。

> 当前为早期版本。请先在非关键设备与非关键服务器上验证。不要依赖本项目处理紧急呼叫、
> Find My、防盗、合规或人身安全场景。

项目网站与图文教程：<https://applelocation.shutiao.us/>。静态网站源码位于 [`website/`](website/)。

## 特性

- 支持 Debian 12/13、Ubuntu 22.04/24.04；代理模式支持 `amd64`/`arm64`，仅定位模式
  不限制 CPU 架构。
- 交互脚本，可选择新手、进阶或高手模式。
- 新手模式会生成 定位修改器+VLESS+REALITY+Vision 节点；进阶模式还可选择
  `2022-blake3-aes-256-gcm` SS2022，并输出标准 `ss://` URL。
- VLESS + REALITY 使用安装器统一管理的 SNI/target，并在落地机现场校验证书、TLS 1.3 与 HTTP/2。
- 进阶模式安装 Telegram Bot：提供命令菜单，以不同颜色状态按钮切换、增加、
  删除地点，并可一键恢复真实定位；十个内置地点入口统一使用中文短名。
- 进阶模式安装完成后，Bot 主动交付凭据提示、纯节点链接、CA 安装说明和纯下载链接；
  节点 URL 与一次性 URL 均可直接长按复制或点击，不与说明文字混在同一条消息中。
- 每次进阶安装为洛杉矶、东京、香港、新加坡、吉隆坡、巴黎、法兰克福、雷克雅未克和南极
  昆仑站生成安装级随机坐标；不同服务器不会共享同一组精确坐标。
- Bot 使用独立低权限账号，不监听公网端口；它只能读取专门用于 Telegram 交付的节点 URL 副本
  和只含 CA 公钥的描述文件，不能读取 `install.env`、Xray 配置或叶证书私钥。安装器会校验
  Token/Chat ID，`hle verify` 还会检查 Bot API 心跳。
- 通过公网出口 IP 识别城市，再从该城市的 OpenStreetMap 行政边界内随机抽取 WGS84 坐标。
- 若无法取得城市边界，则在 IP 定位中心附近 3 km 内随机回退，并明确标记回退状态。
- 只路由 Apple 网络定位域名到本机拦截器；其他域名不会经过定位拦截器。
- 保留 Apple 返回批次内的相对几何关系，并在选定中心周围做平滑的 8 m 微漂移；南极昆仑站
  因高纬稀疏覆盖关闭运行时漂移，保持安装时生成的固定中心。
- 对正常 WLOC/WifiTile 几何做统一平移，并把公里级热点批次等比例压缩到目标周围最大 45 m；
  已小于 45 m 的相对结构不会被放大。对已确认的全 sentinel 无定位批次，生成目标点周围最大
  45 m 的稳定坐标簇；单条记录使用
  目标坐标，蜂窝记录使用至少 1000 m 精度，未知或畸形批次仍失败关闭。
- 对高纬无覆盖区常见的稀疏 sentinel 和精确 WifiTile 404，使用有 TTL、有限额的纯内存恢复链；
  只复用手机近期请求中真实出现的 Wi-Fi 身份，或平移并限幅完整 Apple 模板，不记录或持久化原始内容。
- 自动生成 iOS CA 描述文件；CA 私钥签发完成后立即删除。
- 安装与升级使用互斥锁、预检查和文件级事务；服务或完整性检查失败会恢复先前受管状态。
- 对鉴权失败的 REALITY fallback 使用每次安装随机化的双向限速参数，降低被扫描后滥用的风险。
- 内核可用时使用显式 IPv4/IPv6 双栈监听；只支持 IPv4 的主机自动保持 IPv4 监听。
- 对定位域名阻断 QUIC/UDP 443，促使其回退到能够被限定拦截器处理的 TCP；普通域名不受影响。
- 安装器不修改 SSH 端口、SSH 密钥、密码，也不会主动启用原本关闭的 UFW。
- `sudo hle pause` 可暂停坐标改写并返回 Apple 原始定位响应，`sudo hle resume` 即时恢复；
  代理节点和普通流量不中断，状态跨重启保留。
- 重复安装或执行 `sudo hle relocate` 会重新随机选点；进阶模式由 Telegram 管理多地点，
  重复安装保留其地点库；VLESS 模式始终保持固定 SNI。

## 工作方式

新手/进阶代理模式：

```text
iPhone full-tunnel client
        |
        | VLESS + REALITY + Vision (TCP), or SS2022 (TCP/UDP)
        v
Home-Location-Endpoint landing server
        |-- ordinary traffic ----------------------> Internet
        `-- scoped Apple location TLS -------------> loopback interceptor
                                                      | rewrite response
                                                      `-------------> Apple origin
```

高手模式保留右侧的定位修改器，并提供 Xray 接线片段；代理入站、认证、出站与防火墙全部由
用户自行管理。详见[仅定位修改器](docs/MODIFIER-ONLY.md)。进阶模式详见
[Telegram 定位控制](docs/ADVANCED.md)。

如果前面还有中转，只允许使用 Realm 做纯 TCP 转发。Realm 不解密、不改写，也不运行第二层
代理服务；最终的代理入站和定位拦截器必须在落地机上。SS2022 原生 UDP 不经过纯 TCP Realm。
详见
[Realm 中转说明](docs/REALM.md)。

## 一键安装

交互安装会询问选择新手、进阶或高手模式；项目推荐选择进阶模式（输入 `2`）：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/v0.2.5/install.sh \
  | sudo env HLE_VERSION=v0.2.5 bash
```

代理模式要求服务器上没有不受本项目管理的 Xray 配置；高手模式可以与用户现有代理核心共存。
建议至少保留 384 MiB RAM。安装器在代理模式少于 200 MiB、高手模式少于 50 MiB 根分区
可用空间时会拒绝继续。

无人值守安装完整模式：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/v0.2.5/install.sh \
  | sudo env HLE_VERSION=v0.2.5 bash -s -- --mode full --port 443
```

无人值守安装进阶模式（建议为此节点创建专用 Bot，并先向 Bot 发送 `/start`）：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/v0.2.5/install.sh \
  | sudo env \
      HLE_VERSION=v0.2.5 \
      HLE_TELEGRAM_BOT_TOKEN='BOT_TOKEN' \
      HLE_TELEGRAM_CHAT_ID='NUMERIC_CHAT_ID' \
      bash -s -- --mode advanced --protocol ss2022 --port 443
```

`--protocol` 可取 `vless-reality` 或 `ss2022`。Token 只写入 root 管理、Bot 组只读的凭据文件，
不会进入 `install.env`、节点 URI 或普通日志。只有 Telegram 长轮询成功后服务才会报告健康；
地点总数上限为 50 个，避免长期操作生成过大的菜单。

无人值守安装仅定位模式：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/v0.2.5/install.sh \
  | sudo env HLE_VERSION=v0.2.5 bash -s -- --mode modifier-only
```

VLESS 模式使用安装器内置且统一管理的 REALITY SNI/target。安装时会从落地服务器现场检查证书
链、主机名、TLS 1.3 和 HTTP/2；检查失败会停止安装，不会静默回退到其他 SNI。
安装器不再接受 `--reality-sni` 或 `--reality-target` 覆盖，避免不同节点产生参数漂移。

默认 URI 中的服务器地址来自落地机检测到的公网**出口** IP。若落地机位于 NAT 后、入口与出口
地址不同，或客户端先连接 Realm 前置机，必须用 `--server <客户端实际连接的入口地址>`；前后端
端口应保持相同。安装器无法从落地机可靠推断云安全组、NAT 映射或前置机地址。

代理模式安装结束会输出：

- 一行 VLESS 或 SS2022 URI；
- `/etc/home-location-endpoint/Home-Location-Endpoint-CA.mobileconfig`；
- CA 的 SHA-256 指纹；
- IP 识别城市与抽样方式。

高手模式输出 CA 描述文件、回环监听地址和 Xray 接线片段，不生成代理 URI，也不安装 Xray、
TCP 调优或防火墙规则。

## iPhone 设置

1. 交互式安装成功后，安装器会自动启动 `hle profile serve`。用 iPhone Safari 打开输出的随机
   下载地址或扫描终端二维码。链接默认有效 100 分钟，并在首次成功下载后立即关闭。以后需要
   重新下载时可手动运行 `sudo hle profile serve`；NAT/Realm 场景如需覆盖入口地址，使用
   `--host <手机可访问地址>`。
2. 核对终端输出的 CA SHA-256 指纹。该临时服务使用 HTTP，只应短时开放下载端口；如需更强的
   传输保护，请改用 SCP、SFTP 或自行配置的可信 HTTPS。
3. 安装描述文件。
4. 在“设置 → 通用 → 关于本机 → 证书信任设置”中为该 CA 开启完全信任。
5. 代理模式把 URI 导入支持对应协议的客户端，并使用全局 TUN/VPN 模式连接；高手模式按
   [接线文档](docs/MODIFIER-ONLY.md)接入自己的代理。
6. 进阶模式打开专用 Bot 并发送 `/menu`；菜单可切换预置/自定义地点、恢复真实定位，也可直接
   获取节点链接和 CA 描述文件。为允许复制链接和安装附件，消息不会禁止保存；应使用专用私有 Bot，
   并把 Telegram 私聊历史按敏感凭据保管。
   交互式安装完成时，Bot 还会主动发送四条独立消息：凭据提示、节点 URI、CA 安装说明，以及
   100 分钟内有效且首次成功下载后立即失效的一次性 URL。
7. 不再使用时，删除描述文件并关闭/删除该代理节点。

只安装 CA、只配置系统 DNS、或只让浏览器走代理都不足以保证 Apple 定位请求经过落地机。
节点 URI 也不会替客户端配置远程 DNS、VPN 排除项或防止 App 绕过 VPN；这些属于客户端能力。

## 运维命令

```bash
sudo hle verify
sudo hle status
sudo hle pause
sudo hle resume
sudo hle show-link
sudo hle profile
sudo hle profile serve
sudo hle relocate
sudo hle uninstall
```

`hle show-link` 只适用于代理模式。`hle` 命令是 `/usr/local/sbin/hle`，需要 root 运行；
普通用户的 PATH 可能不含 `/usr/local/sbin`，因此上面统一用 `sudo`。

`hle pause` 不会停止 Xray 或定位拦截器，也不会改变代理端口。它让已进入拦截器的 Apple 定位
请求继续访问原始 Apple host，并把响应不作坐标改写地返回；`hle resume` 恢复改写。服务端对
新请求无需重启服务或重新连接节点，但 iPhone 仍可能继续使用定位缓存；状态保存在
`/var/lib/home-location-endpoint/modifier.state`。
进阶模式使用 `/var/lib/home-location-endpoint/control/modifier.state`。

`hle profile serve` 默认在 TCP `18080` 启动带随机令牌的一次性 HTTP 下载，100 分钟后或首次
成功下载后自动退出，不提供目录浏览，也不会暴露 CA 私钥。UFW、云安全组、NAT 或 Realm 不会
被自动修改；需要临时让手机能够到达该端口。可用 `--port`、`--host`、`--bind`、
`--timeout-minutes` 和 `--no-qr` 调整行为。无交互终端的 cloud-init、Ansible 等自动化安装不会
启动这个最长阻塞 100 分钟的下载服务；安装器会输出命令，待需要时再运行。

`sudo hle uninstall` 停止并删除本项目安装的服务、受管文件与受限 CA，并在确认后执行
（脚本化可加 `--yes`）。代理模式还会删除受管的 Xray、其配置和 TCP sysctl 文件；高手模式
只删除自身文件，不触碰你自己的代理核心。只有被安装清单明确记录为本项目新建的低权限账户/
组才会被删除。为避免误删用户原有规则，UFW 放行只提示人工检查。手机上的 CA 描述文件也需
手动移除。

`hle relocate` 会在服务器当前公网出口 IP 所在城市重新随机取点。拦截器按文件变更自动加载，
无需重启。安装器再次运行时同样会重新抽点，并默认复用既有 CA。VLESS 会复用 UUID、X25519
密钥、short ID 与固定 SNI；SS2022 会复用现有 32 字节密钥；`--rotate-ca` 才会轮换 CA。
进阶模式禁止 `hle relocate`，以免覆盖 Telegram 管理的多地点库。

重复安装时，如果 IP 定位服务临时不可用，安装器会验证并保留已有坐标，而不是中断一台原本
正常的节点；首次安装没有可用旧坐标时仍会安全失败。失败事务会删除本次新建的低权限账号；
APT 软件包、故障日志和已加载内核模块不回滚，详情见[运维与恢复](docs/OPERATIONS.md)。

## “每次随机”的定义

新手/高手模式在每次安装或每次 `hle relocate` 时生成新的城市内随机中心；进阶模式首次安装
生成整个随机地点库，后续相同模式重装会保留。连接运行期间不会为每个
请求重新选择相隔数公里的点，因为这种不可能的瞬移容易让 `locationd` 拒绝整批定位结果；运行时
只在中心附近做连续、确定性的微漂移。

## 隐私与局限

- 本项目会把手机原始的 Apple 网络定位请求转发给 Apple，再改写返回结果。因此 Apple 仍可能从
  请求中的 Wi-Fi BSSID、蜂窝信息、账户或其他遥测推断真实位置。
- 它不会保证覆盖 GPS/GNSS、蓝牙、蜂窝基带、紧急定位、证书固定、App 自有定位协议或服务端
  根据账号/IP 作出的定位。
- 一些 App 会综合多种信号；结果可能仍显示真实位置、无定位，或在真假位置之间切换。
- 如果客户端没有把 UDP、IPv6、硬编码 IP 或系统服务流量纳入全隧道，服务端无法补救该绕过。
- 安装私有 CA 会扩大设备信任面。项目将 CA 私钥删除，并把路由及叶证书限制在相关 Apple
  定位域名，但使用者仍应理解并接受这一风险。
- 随机坐标可能落在城市行政边界内的公园、水域、工业区或其他不适合模拟日常活动的位置。

完整模型见 [安全与隐私](docs/SECURITY-AND-PRIVACY.md)，实现结构见
[架构说明](docs/ARCHITECTURE.md)。

## 数据与上游

- IP 城市识别默认使用 [ipwho.is](https://ipwho.is/)；可在代码/命令参数中替换 HTTPS 提供方。
- 城市边界来自 [Nominatim](https://nominatim.openstreetmap.org/) 与
  [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)，查询结果会本地缓存。
- 代理核心使用 [XTLS/Xray-core](https://github.com/XTLS/Xray-core)。
- 中转示例使用 [zhboner/realm](https://github.com/zhboner/realm)。

本仓库不包含上述项目的源码。协议解析实现为本项目独立实现；详见 [NOTICE](NOTICE.md)。

## English

Home-Location-Endpoint offers beginner, advanced, and modifier-only installs. The recommended advanced mode adds a
single-operator Telegram location menu and supports either VLESS + REALITY + Vision or SS2022.
It rewrites only scoped Apple network-location responses to selected WGS84 points. Read the security
and privacy limitations before trusting the generated private CA on an iPhone.

## License

[MIT](LICENSE)
