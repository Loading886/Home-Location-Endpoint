# Home-Location-Endpoint

把一台 Linux 落地机部署为 Apple 网络定位修改端点。安装器提供两种模式：

1. **完整代理端点（推荐）**：安装 VLESS + REALITY + Vision，并接入定位修改器。
2. **仅定位修改器（高级）**：不安装代理核心，由用户把自己的代理入站接到定位修改器。

两种模式都会按落地机公网出口 IP 识别城市，并在该城市内抽取随机坐标。普通代理流量不经
定位修改器。

> 当前为早期版本。请先在非关键设备与非关键服务器上验证。不要依赖本项目处理紧急呼叫、
> Find My、防盗、合规或人身安全场景。

## 特性

- 支持 Debian 12/13、Ubuntu 22.04/24.04；完整模式支持 `amd64`/`arm64`，仅定位模式
  不限制 CPU 架构。
- 可交互选择完整代理端点或仅定位修改器。
- 完整模式自动安装并校验固定版本的 Xray-core，生成 VLESS + REALITY + Vision 节点。
- 完整模式每次运行安装器都会随机打乱内置 SNI 池，并选用首个通过现场证书、TLS 1.3 与
  HTTP/2 校验的站点。
- 通过公网出口 IP 识别城市，再从该城市的 OpenStreetMap 行政边界内随机抽取 WGS84 坐标。
- 若无法取得城市边界，则在 IP 定位中心附近 3 km 内随机回退，并明确标记回退状态。
- 只路由 Apple 网络定位域名到本机拦截器；其他域名不会经过定位拦截器。
- 保留 Apple 返回批次内的相对几何关系，并在选定中心周围做平滑的 8 m 微漂移。
- 自动生成 iOS CA 描述文件；CA 私钥签发完成后立即删除。
- 安装与升级使用互斥锁、预检查和文件级事务；服务或完整性检查失败会恢复先前受管状态。
- 对鉴权失败的 REALITY fallback 使用每次安装随机化的双向限速参数，降低被扫描后滥用的风险。
- 内核可用时使用显式 IPv4/IPv6 双栈监听；只支持 IPv4 的主机自动保持 IPv4 监听。
- 对定位域名阻断 QUIC/UDP 443，促使其回退到能够被限定拦截器处理的 TCP；普通域名不受影响。
- 安装器不修改 SSH 端口、SSH 密钥、密码，也不会主动启用原本关闭的 UFW。
- 重复安装或执行 `sudo hle relocate` 会重新随机选点；完整模式重跑安装器还会更换 SNI。

## 工作方式

完整模式：

```text
iPhone full-tunnel client
        |
        | VLESS + REALITY + Vision (TCP)
        v
Home-Location-Endpoint landing server
        |-- ordinary traffic ----------------------> Internet
        `-- scoped Apple location TLS -------------> loopback interceptor
                                                      | rewrite response
                                                      `-------------> Apple origin
```

仅定位模式保留右侧的定位修改器，并提供 Xray 接线片段；代理入站、认证、出站与防火墙全部由
高级用户自行管理。详见[仅定位修改器](docs/MODIFIER-ONLY.md)。

如果前面还有中转，只允许使用 Realm 做纯 TCP 转发。Realm 不解密、不改写，也不运行第二层
VLESS/REALITY；最终的 REALITY 服务和定位拦截器必须在落地机上。详见
[Realm 中转说明](docs/REALM.md)。

## 一键安装

交互安装会询问选择完整模式或仅定位模式：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/main/install.sh \
  | sudo bash
```

完整模式要求服务器上没有不受本项目管理的 Xray 配置；仅定位模式可以与用户现有代理核心共存。
建议至少保留 384 MiB RAM。安装器在完整模式少于 200 MiB、仅定位模式少于 50 MiB 根分区
可用空间时会拒绝继续。

无人值守安装完整模式：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/main/install.sh \
  | sudo bash -s -- --mode full --port 443
```

无人值守安装仅定位模式：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/main/install.sh \
  | sudo bash -s -- --mode modifier-only
```

完整模式默认从仓库内的去重候选池随机打乱，逐个检查 TLS 1.3、HTTP/2、证书链和主机名，
使用首个校验成功的 SNI。也可以显式覆盖：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/main/install.sh \
  | sudo bash -s -- \
      --mode full \
      --port 443 \
      --reality-sni www.aws.com \
      --reality-target www.aws.com:443
```

REALITY 的 SNI/target 必须能从该服务器正常建立 TLS，且证书应匹配 SNI。生产使用时优先选择
与服务器网络位置合适、长期稳定、不是通用 CDN 开放转发目标的站点。候选池来自操作者提供的
名单，进入池中不代表项目对站点可用性、安全性或长期稳定性作保证。

> 完整模式每次重跑安装器都会重新选择 SNI，即使 UUID、X25519 密钥和 short ID 被复用，
> VLESS URI 也会变化。重跑后必须把新 URI 更新到客户端及所有使用该参数的配置中。

默认 URI 中的服务器地址来自落地机检测到的公网**出口** IP。若落地机位于 NAT 后、入口与出口
地址不同，或客户端先连接 Realm 前置机，必须用 `--server <客户端实际连接的入口地址>`；前后端
端口应保持相同。安装器无法从落地机可靠推断云安全组、NAT 映射或前置机地址。

完整模式安装结束会输出：

- 一行 VLESS URI；
- `/etc/home-location-endpoint/Home-Location-Endpoint-CA.mobileconfig`；
- CA 的 SHA-256 指纹；
- IP 识别城市与抽样方式。

仅定位模式输出 CA 描述文件、回环监听地址和 Xray 接线片段，不生成 VLESS URI，也不安装 Xray、
TCP 调优或防火墙规则。

## iPhone 设置

1. 安全地把 `.mobileconfig` 复制到 iPhone，核对安装器输出的 CA SHA-256 指纹。
2. 安装描述文件。
3. 在“设置 → 通用 → 关于本机 → 证书信任设置”中为该 CA 开启完全信任。
4. 完整模式把 VLESS URI 导入支持 REALITY + Vision 的客户端，并使用全局 TUN/VPN 模式连接；
   仅定位模式按[接线文档](docs/MODIFIER-ONLY.md)接入自己的代理。
5. 不再使用时，删除描述文件并关闭/删除该代理节点。

只安装 CA、只配置系统 DNS、或只让浏览器走代理都不足以保证 Apple 定位请求经过落地机。
节点 URI 也不会替客户端配置远程 DNS、VPN 排除项或防止 App 绕过 VPN；这些属于客户端能力。

## 运维命令

```bash
sudo hle verify
sudo hle status
sudo hle show-link
sudo hle profile
sudo hle relocate
sudo hle uninstall
```

`hle show-link` 只适用于完整模式。`hle` 命令是 `/usr/local/sbin/hle`，需要 root 运行；
普通用户的 PATH 可能不含 `/usr/local/sbin`，因此上面统一用 `sudo`。

`sudo hle uninstall` 停止并删除本项目安装的服务、受管文件、脚手证书 CA 与低权限账户，并在
确认后执行（脚本化可加 `--yes`）。完整模式还会删除受管的 Xray、其配置、TCP sysctl 文件，并
尝试移除本项目添加的 UFW 放行；仅定位模式只删除自身文件，不触碰你自己的代理核心。它不会
删除已装到 iPhone 上的 CA 描述文件——请手动从手机移除。

`hle relocate` 会在服务器当前公网出口 IP 所在城市重新随机取点。拦截器按文件变更自动加载，
无需重启。安装器再次运行时同样会重新抽点，并默认复用既有 CA。完整模式会复用 UUID、
X25519 密钥与 short ID，但会重新选择 SNI；`--rotate-ca` 才会轮换 CA。

重复安装时，如果 IP 定位服务临时不可用，安装器会验证并保留已有坐标，而不是中断一台原本
正常的节点；首次安装没有可用旧坐标时仍会安全失败。软件包安装、系统账户创建和失败日志不在
文件事务的回滚范围内，详情见[运维与恢复](docs/OPERATIONS.md)。

## “每次随机”的定义

本项目在每次安装或每次 `hle relocate` 时生成一个新的城市内随机中心。连接运行期间不会为每个
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

Home-Location-Endpoint installs either a complete VLESS + REALITY + Vision landing endpoint or an
advanced location-modifier-only integration. It rewrites only scoped Apple network-location
responses to a random WGS84 point inside the city detected from the server's public egress IP.
Read the security and privacy limitations before trusting the generated private CA on an iPhone.

## License

[MIT](LICENSE)
