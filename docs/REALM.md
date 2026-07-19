# Realm 前置中转

本页适用于 `full` 或 `advanced` 代理模式。前置机只做 TCP 字节流转发。它不安装 Xray、不终止 REALITY、
不持有 UUID/私钥，也不运行定位拦截器。

假设：

- Realm 前置入口：`RELAY_IP:443`
- 落地机 Home-Location-Endpoint：`LANDING_IP:443`

Realm TOML：

```toml
[log]
level = "warn"
output = "stdout"

[network]
no_tcp = false
use_udp = false
tcp_timeout = 10
tcp_keepalive = 15
tcp_keepalive_probe = 3

[[endpoints]]
listen = "0.0.0.0:443"
remote = "LANDING_IP:443"
```

客户端节点中只把服务器地址与端口替换成 `RELAY_IP:443`；UUID、flow、SNI、REALITY public key、
short ID 等全部保持落地机生成的值。

## 必须满足

- 转发协议是 TCP；不要给这条 REALITY 链增加 PROXY protocol。
- 前置机到落地机的安全组/防火墙应尽量只允许前置机 IP 访问落地端口。
- 若客户端还需要 UDP 应用，VLESS 自身可在 TCP/REALITY 内承载 XUDP；Realm 这一跳仍只转发
  外层 TCP，不要把同端口配置成公开 UDP full-cone relay。
- 进阶模式的 SS2022 可通过 Realm 使用 TCP，但原生 UDP 不会被纯 TCP Realm 转发；需要 UDP
  时应让客户端直达落地机同端口 UDP，或改用 VLESS/XUDP。
- 多层中转时每层都按同样方式纯 TCP 转发，最终只能有一个 Home-Location-Endpoint 落地。
- REALITY 伪装站点由落地机访问，与 Realm 前置机无关。

Realm 的安装、升级、systemd 与防火墙不属于本仓库一键安装器的管理范围。
