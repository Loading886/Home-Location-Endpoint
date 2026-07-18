# 仅定位修改器模式

`modifier-only` 面向已经能独立维护代理核心与路由规则的用户。它只安装：

- 回环定位拦截器 `127.0.0.1:10451`；
- 城市识别、随机选点与平滑微漂移；
- 私有 CA、叶证书和 iOS CA 描述文件；
- `hle` 运维命令、systemd 服务与日志轮转；
- `/etc/home-location-endpoint/xray-location-routing.example.json` 接线示例。

它不会安装 Xray、创建代理入站、开放端口、修改 UFW 或写入 TCP 调优参数。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/main/install.sh \
  | sudo bash -s -- --mode modifier-only
```

同一台机器后续只能用相同模式重跑安装器。安装器不会在 `full` 与 `modifier-only` 之间自动迁移，
避免覆盖用户自管的代理配置。

## 接入 Xray

承载手机流量的入站必须启用嗅探，并只把嗅探结果用于路由：

```json
"sniffing": {
  "enabled": true,
  "destOverride": ["http", "tls", "quic"],
  "routeOnly": true
}
```

再把示例文件中的两部分合并进现有配置：

1. 将 `outbounds[0]` 加入现有 `outbounds`；`tag` 必须保持为 `location-interceptor`。
2. 将 `routing.rules[0]` 放在可能提前匹配 Apple 域名的宽泛规则之前。

示例规则只匹配 Apple 网络定位域名的 TCP/443，并把已解密后的内层目标连接重定向到回环
拦截器。不要把 VLESS/REALITY 的外层加密连接直接 DNAT 到 `10451`；定位修改器无法解析代理
外层协议。

如果使用其他代理核心，必须具备等价能力：在代理认证与解密后取得 TLS SNI，仅将文档列出的
Apple 定位域名送到 `127.0.0.1:10451`，其他目标保持原目的地址。

## 验证

合并配置后，先使用代理核心自己的配置检查命令，再重启该核心。随后执行：

```bash
sudo hle verify
sudo hle status
sudo journalctl -u home-location-endpoint --since '10 minutes ago'
```

`hle verify` 只能验证定位修改器及接线示例本身，无法证明用户已把片段正确合并到第三方配置。
应从客户端做一次定位请求，并确认普通网站没有进入定位拦截器日志。

## CA 与坐标

CA 描述文件位于：

```text
/etc/home-location-endpoint/Home-Location-Endpoint-CA.mobileconfig
```

把它安全传到 iPhone，核对安装时输出的 SHA-256 指纹，安装后在证书信任设置中开启完全信任。
重新随机选择同一出口城市内的坐标：

```bash
sudo hle relocate
```

不用时应从手机删除 CA 描述文件，并从代理配置中移除定位路由和回环出站。
