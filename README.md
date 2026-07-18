# Home-Location-Endpoint

把一台全新的 Linux 落地机部署为 **VLESS + REALITY + Vision** 代理出口，并只对 Apple
网络定位相关请求返回该落地机 IP 所在城市内的随机坐标。普通代理流量仍由落地机直接出网。

> 当前为预发布版本。请先在非关键设备与非关键服务器上验证。不要依赖本项目处理紧急呼叫、
> Find My、防盗、合规或人身安全场景。

## 特性

- 支持 Debian 12/13、Ubuntu 22.04/24.04，`amd64` 与 `arm64`。
- 自动安装并校验固定版本的 Xray-core。
- 自动生成 VLESS + REALITY + Vision 节点、UUID、X25519 密钥和 short ID。
- 通过公网出口 IP 识别城市，再从该城市的 OpenStreetMap 行政边界内随机抽取 WGS84 坐标。
- 若无法取得城市边界，则在 IP 定位中心附近 3 km 内随机回退，并明确标记回退状态。
- 只路由 Apple 网络定位域名到本机拦截器；其他域名不会经过定位拦截器。
- 保留 Apple 返回批次内的相对几何关系，并在选定中心周围做平滑的 8 m 微漂移。
- 自动生成 iOS CA 描述文件；CA 私钥签发完成后立即删除。
- 安装器不修改 SSH 端口、SSH 密钥、密码，也不会主动启用原本关闭的 UFW。
- 重复安装或执行 `sudo hle relocate` 会重新随机选点，节点凭据默认保持不变。

## 工作方式

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

如果前面还有中转，只允许使用 Realm 做纯 TCP 转发。Realm 不解密、不改写，也不运行第二层
VLESS/REALITY；最终的 REALITY 服务和定位拦截器必须在落地机上。详见
[Realm 中转说明](docs/REALM.md)。

## 一键安装

准备一台没有现存 Xray 配置的干净服务器，并在服务商防火墙中开放 TCP 443：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/main/install.sh \
  | sudo bash
```

自定义端口或 REALITY 伪装站点：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/main/install.sh \
  | sudo bash -s -- \
      --port 443 \
      --reality-sni www.microsoft.com \
      --reality-target www.microsoft.com:443
```

REALITY 的 SNI/target 必须能从该服务器正常建立 TLS，且证书应匹配 SNI。生产使用时优先选择
与服务器网络位置合适、长期稳定、不是通用 CDN 开放转发目标的站点。

安装结束会输出：

- 一行 VLESS URI；
- `/etc/home-location-endpoint/Home-Location-Endpoint-CA.mobileconfig`；
- CA 的 SHA-256 指纹；
- IP 识别城市与抽样方式。

## iPhone 设置

1. 安全地把 `.mobileconfig` 复制到 iPhone，核对安装器输出的 CA SHA-256 指纹。
2. 安装描述文件。
3. 在“设置 → 通用 → 关于本机 → 证书信任设置”中为该 CA 开启完全信任。
4. 把 VLESS URI 导入支持 REALITY + Vision 的客户端，并使用全局 TUN/VPN 模式连接。
5. 不再使用时，删除描述文件并关闭/删除该代理节点。

只安装 CA、只配置系统 DNS、或只让浏览器走代理都不足以保证 Apple 定位请求经过落地机。

## 运维命令

```bash
sudo hle verify
sudo hle status
sudo hle show-link
sudo hle profile
sudo hle relocate
```

`hle relocate` 会在服务器当前公网出口 IP 所在城市重新随机取点。拦截器按文件变更自动加载，
无需重启。安装器再次运行时同样会重新抽点，但会复用既有节点凭据与 CA，除非显式使用
`--rotate-ca`。

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

Home-Location-Endpoint turns a clean Debian/Ubuntu landing server into a VLESS + REALITY + Vision
endpoint and rewrites only scoped Apple network-location responses to a random WGS84 point inside
the city detected from the server's public egress IP. Read the security and privacy limitations
before installing the generated private CA on an iPhone.

## License

[MIT](LICENSE)
