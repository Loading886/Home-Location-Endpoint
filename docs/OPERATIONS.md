# 运维与恢复

## 安装前检查

- 系统必须是 systemd 作为 PID 1 的 Debian 12/13 或 Ubuntu 22.04/24.04。
- 完整模式只支持 `amd64`、`arm64`，并要求不存在其他 Xray 安装或同端口监听。
- 完整模式至少准备 200 MiB 根分区空间；仅定位模式至少 50 MiB。低于 384 MiB RAM 会警告，
  不建议在同时承载大量代理连接时忽略。
- 确认落地机能访问 GitHub、IPWHOIS、Nominatim、候选 REALITY target 和 Apple 源站。
- 云安全组、NAT 和上游防火墙由操作者负责。安装器只会在 UFW 已经处于 active 时加入 TCP 端口。
- 安装器将 `needrestart` 设为仅列出待重启服务，不会在远程安装途中自动重启 networkd、sshd
  或其他无关系统服务。系统更新留下的待重启项应在维护窗口人工处理或通过重启主机完成。

## NAT、双 IP 与 Realm

安装器自动探测的是出口 IP，不一定是客户端能够连接的入口 IP。以下场景必须显式传入：

```bash
sudo bash install.sh --mode full --port 443 --server ENTRY_IP_OR_HOST
```

- VPS 的入口和出口 IP 不同；
- 服务器在 NAT 后；
- 客户端实际连接 Realm 前置机。

Realm 只做纯 TCP 转发，建议前后端保持同一端口。UUID、flow、SNI、REALITY public key 和 short
ID 必须原样到达最终落地机。安装器检测到可用双栈 socket 时监听 IPv4+IPv6；这不会替你开放
IPv6 云防火墙。

## 重复安装与升级

安装器和 `hle relocate` 共用 `/run/home-location-endpoint.lock`，并发操作会立即拒绝。重复安装：

- 保留现有 UUID、X25519 key、short ID 和有效 CA；
- 按项目约定重新选择 REALITY SNI，并生成新的随机 fallback 限速；
- 重新选择城市内坐标；若外部定位服务暂时失败，则验证并保留旧坐标；
- 叶证书或 CA 距到期不足 30 天时拒绝静默继续，要求显式 `--rotate-ca`。

`--rotate-ca` 会让旧手机描述文件失效，必须重新分发、核对指纹并开启完全信任。

正式环境建议使用 Git tag 对应的版本，而不是长期追踪可变的 `main`。不要把完整模式安装到还
承载其他 Xray 配置的主机。

## 卸载

```bash
sudo hle uninstall          # 交互确认
sudo hle uninstall --yes    # 脚本化，跳过确认
```

`hle uninstall` 停止并禁用本项目的服务，按安装时的受管清单删除 systemd unit、配置/程序/状态/
日志目录、`/usr/local/sbin/hle`、logrotate 与受限 CA。它用 `install_mode` 判定模式：只有当本机
确实由本项目安装了受管 Xray（完整模式）时，才会删除该 Xray 二进制、其配置和 TCP sysctl 文件；
仅定位模式绝不触碰你自己的代理核心。只有安装清单明确记录为本项目新建的低权限账户/组才会
被删除；从旧版升级而缺少证据时会安全保留。UFW 规则不会自动删除，请按端口和注释人工核对。
sysctl 调优在下次重启前仍然生效。卸载不会删除已装到 iPhone 上的 CA 描述文件，请手动移除。

不要用未经审查的 `rm -rf` 清理混合环境；如果安装被强制中断留下半套状态，先按下文
“事务与失败边界”排查后再决定用 `hle uninstall` 或手动恢复备份。

例如固定安装 `v0.1.3`：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/v0.1.3/install.sh \
  | sudo env HLE_VERSION=v0.1.3 bash
```

## 事务与失败边界

受管配置、程序、状态缓存、systemd unit，以及完整模式的 Xray 和 sysctl 文件会在修改前备份。
服务启动或 `hle verify` 失败时自动恢复原文件和原服务状态。UFW 放行及 sysctl 运行时加载只在
事务成功后执行。

不会回滚的项目：APT 已安装的软件包、已创建的低权限系统账户、故障日志、已加载但未使用的
内核模块。`SIGKILL`、宿主机崩溃或断电无法运行进程内回滚。

若强制中断后重跑出现 `partial or unmanaged ... files already exist`，不要直接 `rm -rf`。先保存：

```bash
sudo systemctl status home-location-endpoint xray --no-pager
sudo journalctl -u home-location-endpoint -u xray -n 200 --no-pager
sudo find /etc/home-location-endpoint /opt/home-location-endpoint \
  -maxdepth 2 -printf '%M %u:%g %p -> %l\n'
```

确认文件确实属于本项目后再恢复备份或清理。不要在公开 Issue 上传节点 URI、私钥或原始日志。

## 日常检查

```bash
sudo hle verify
sudo hle status
sudo systemctl status home-location-endpoint xray --no-pager
sudo journalctl -u home-location-endpoint -u xray --since '30 minutes ago' --no-pager
sudo ss -lntup
```

仅定位模式没有 `xray.service`。`hle verify` 检查 Xray/示例 JSON、证书链与有效期、叶证书 key、
描述文件内 CA、坐标、受管文件权限、回环监听和服务状态。

## 常见故障

- **URI 无法连接**：核对 `--server` 是否为入口而非出口、端口映射、云安全组和 Realm 端口。
- **普通代理可用但定位不变**：客户端必须使用全局 TUN/VPN，并捕获 UDP、IPv6 和系统服务流量；
  查看日志是否出现 `TRANSLATE` 或 `WIFITILE_TRANSLATE`。
- **定位不可用**：查看 `TRANSLATE_FAIL_CLOSED`、`WIFITILE_FAIL_CLOSED`、证书信任和 Apple 是否
  改变协议。默认 fail-closed，解析不确定时不会悄悄返回真实坐标。
- **安装找不到可用 SNI**：候选 target 必须从该机通过有效证书、TLS 1.3 和 HTTP/2 现场校验，
  实际对端 IP还必须是公网地址。
- **日志停止增长**：活动文件达到 16 MiB 后会暂停文件写入，等待每日 logrotate；同时检查
  systemd journal。

## 客户端边界

VLESS URI 只描述节点协议，不会自动配置 iOS 的全局路由、远程 DNS、按 App 排除项或防止 App
绕过 VPN。服务器只能阻断已经到达 Xray 且能识别出定位域名的 QUIC；未进入隧道的 UDP、IPv6、
硬编码 IP、GPS、蜂窝基带、蓝牙和证书固定不在服务端控制范围内。
