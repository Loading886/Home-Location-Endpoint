# 架构说明

## 数据面

1. 客户端通过 VLESS + REALITY + Vision 连接落地机。
2. Xray 对入站流量启用 `http/tls/quic` sniffing，但使用 `routeOnly`，普通目标保持原目的地址。
3. 仅以下 TCP/443 域名被路由到 `location-interceptor`：
   - `gs-loc.apple.com`
   - `gs-loc-cn.apple.com`
   - `gspe85(-数字)?(-cn)?-ssl.ls.apple.com`
4. `location-interceptor` 只监听 `127.0.0.1:10451`，不对公网开放。
5. 拦截器终止该内层 TLS，以 HTTP/1.1 向真实 Apple 源站重新发起请求，随后平移 WLOC 或
   WifiTile 响应中的坐标。
6. 其他流量命中默认 `direct` freedom 出站，从落地机直接访问目标。

## 为什么不是每个响应都换一个城市内坐标

Apple 的一批返回通常包含多个 Wi-Fi AP 或蜂窝塔。把每个点压到完全相同的坐标，或让连续响应
在城市两端跳动，会破坏物理几何关系并容易被系统判为不可信。本项目：

- 安装或 `hle relocate` 时重新抽取城市内中心；
- 同一批次保留所有 AP/蜂窝点之间的相对距离和方向；
- 运行时在中心 8 m 内按 120 秒周期平滑移动；
- 对明确的 `(-180,-180)` 无定位批次保持原样，不凭空制造定位。

## 城市抽样

1. 从 `ipwho.is` 获取公网出口 IP、城市、行政区、国家、中心点与时区。
2. 按城市/行政区/国家向 Nominatim 请求一次 Polygon/MultiPolygon，并缓存。
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

## 文件布局

```text
/etc/home-location-endpoint/
  install.env                 root-only node credentials
  location.json               active random city point
  jitter.seed                 smooth-drift seed
  ca.crt / ca.der             public CA certificate
  leaf.crt / leaf.key         scoped server leaf
  node-uri.txt                root-only VLESS URI
  Home-Location-Endpoint-CA.mobileconfig
/opt/home-location-endpoint/  Python runtime
/usr/local/etc/xray/config.json
/var/lib/home-location-endpoint/city-boundary.json
/var/log/home-location-endpoint/interceptor.log
```

CA 私钥不保留在磁盘。叶证书到期或显式 `--rotate-ca` 时，需要轮换 CA，并在手机重新安装和信任
新描述文件。
