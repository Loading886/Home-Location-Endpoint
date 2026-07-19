# 运维与恢复

## 安装前检查

- 系统必须是 systemd 作为 PID 1 的 Debian 12/13 或 Ubuntu 22.04/24.04。
- 代理模式只支持 `amd64`、`arm64`，并要求不存在其他 Xray 安装或同端口监听。
- 代理模式至少准备 200 MiB 根分区空间；仅定位模式至少 50 MiB。低于 384 MiB RAM 会警告，
  不建议在同时承载大量代理连接时忽略。
- 确认落地机能访问 GitHub、IPWHOIS、Nominatim、安装器管理的 REALITY target 和 Apple 源站。
- 云安全组、NAT 和上游防火墙由操作者负责。安装器只会在 UFW 已经处于 active 时加入 TCP
  端口；SS2022 还会加入同端口 UDP。
- 进阶模式还需能访问 `api.telegram.org:443`，并准备专用 Bot Token 与数字 Chat ID。
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

Realm 只做纯 TCP 转发，建议前后端保持同一端口，所有客户端凭据必须原样到达最终落地机。
SS2022 的 TCP 可经 Realm 转发，但其原生 UDP 不会经 TCP Realm 链路传递。安装器检测到可用
双栈 socket 时监听 IPv4+IPv6；这不会替你开放 IPv6 云防火墙。

## 重复安装与升级

安装器和 `hle relocate` 共用 `/run/home-location-endpoint.lock`，并发操作会立即拒绝。重复安装：

- 保留现有 VLESS UUID/X25519 key/short ID 或 SS2022 密钥，以及有效 CA；
- 保持安装器管理的 REALITY SNI，并生成新的随机 fallback 限速；
- 重新选择城市内坐标；若外部定位服务暂时失败，则验证并保留旧坐标；
- 进阶模式保留 Telegram 地点库与 Bot 凭据，并在修改文件前重新验证 Bot/Chat；
- 叶证书或 CA 距到期不足 30 天时拒绝静默继续，要求显式 `--rotate-ca`。

`--rotate-ca` 会让旧手机描述文件失效，必须重新分发、核对指纹并开启完全信任。

正式环境建议使用 Git tag 对应的版本，而不是长期追踪可变的 `main`。不要把代理模式安装到还
承载其他 Xray 配置的主机。

## 卸载

```bash
sudo hle uninstall          # 交互确认
sudo hle uninstall --yes    # 脚本化，跳过确认
```

`hle uninstall` 停止并禁用本项目的服务，按安装时的受管清单删除 systemd unit、配置/程序/状态/
日志目录、`/usr/local/sbin/hle`、logrotate 与受限 CA。它用 `install_mode` 判定模式：只有当本机
确实由本项目安装了受管 Xray（新手/进阶模式）时，才会删除该 Xray 二进制、其配置和 TCP sysctl 文件；
仅定位模式绝不触碰你自己的代理核心。只有安装清单明确记录为本项目新建的低权限账户/组才会
被删除；从旧版升级而缺少证据时会安全保留。UFW 规则不会自动删除，请按端口和注释人工核对。
sysctl 调优在下次重启前仍然生效。卸载不会删除已装到 iPhone 上的 CA 描述文件，请手动移除。

不要用未经审查的 `rm -rf` 清理混合环境；如果安装被强制中断留下半套状态，先按下文
“事务与失败边界”排查后再决定用 `hle uninstall` 或手动恢复备份。

例如固定安装 `v0.2.3`：

```bash
curl -fsSL https://raw.githubusercontent.com/Loading886/Home-Location-Endpoint/v0.2.3/install.sh \
  | sudo env HLE_VERSION=v0.2.3 bash
```

## 事务与失败边界

受管配置、程序、状态缓存、systemd unit，以及代理模式的 Xray 和 sysctl 文件会在修改前备份。
服务启动或 `hle verify` 失败时自动恢复原文件和原服务状态。UFW 放行及 sysctl 运行时加载只在
事务成功后执行。

本次安装新建的低权限账号/组会在普通失败回滚中删除。不会回滚的项目包括 APT 已安装的软件包、
故障日志和已加载但未使用的内核模块。`SIGKILL`、宿主机崩溃或断电无法运行进程内回滚。

若强制中断后重跑出现 `partial or unmanaged ... files already exist`，不要直接 `rm -rf`。先保存：

```bash
sudo systemctl status home-location-endpoint xray home-location-telegram-bot --no-pager
sudo journalctl -u home-location-endpoint -u xray -u home-location-telegram-bot -n 200 --no-pager
sudo find /etc/home-location-endpoint /opt/home-location-endpoint \
  -maxdepth 2 -printf '%M %u:%g %p -> %l\n'
```

确认文件确实属于本项目后再恢复备份或清理。不要在公开 Issue 上传节点 URI、私钥或原始日志。

## 日常检查

```bash
sudo hle verify
sudo hle status
sudo hle pause
sudo hle resume
sudo hle profile serve
sudo systemctl status home-location-endpoint xray home-location-telegram-bot --no-pager
sudo journalctl -u home-location-endpoint -u xray -u home-location-telegram-bot --since '30 minutes ago' --no-pager
sudo ss -lntup
```

### 暂停与恢复定位修改

```bash
sudo hle pause
sudo hle status
sudo hle resume
```

暂停只关闭坐标改写：代理服务、普通流量和定位请求本身仍保持可用，Apple 原始响应会直接返回。
恢复对后续请求立即生效，两种操作都不重启 Xray 或拦截器。状态跨重启和重复安装保留。进阶
模式的状态位于 `control/modifier.state`，其他模式位于顶层 `modifier.state`；如果 `hle verify`
报告 `modifier state: FAIL`，检查当前模式对应文件的所有权、权限和内容是否仅为 `active` 或
`paused`。

### 临时下载 CA 描述文件

交互式安装成功后，安装器会自动运行下列命令，无需用户再次输入：

```bash
sudo hle profile serve
```

该命令只提供带随机令牌的 `.mobileconfig`，默认监听 TCP `18080`，有效 100 分钟，并在首次成功
下载后关闭。终端支持时会显示二维码。以后需要重新下载时仍可手动运行该命令。它使用临时 HTTP，因此必须通过 SSH 终端显示的 SHA-256
核对 CA；不会自动修改 UFW、云安全组、NAT 或 Realm。入口地址无法自动判断时使用：

```bash
sudo hle profile serve --host PHONE_REACHABLE_IP_OR_HOST
```

可用 `--port 0` 选择随机空闲端口，或用 `--timeout-minutes 100` 调整有效期；随机端口仍需能够
从手机到达。下载过程中按 `Ctrl+C` 会立即关闭服务。

cloud-init、Ansible 等无交互终端安装会跳过自动启动，避免部署任务被最长 100 分钟的等待阻塞；
安装本身仍会正常完成，并在输出中给出稍后手动启动的命令。

仅定位模式没有 `xray.service`。`hle verify` 检查 Xray/示例 JSON、证书链与有效期、叶证书 key、
描述文件内 CA、坐标、受管文件权限、回环监听和服务状态。

进阶模式另行检查地点库、Bot 凭据权限、`home-location-telegram-bot.service` 与最近 180 秒内的
Bot API 心跳。心跳只在一次 `getUpdates` 长轮询成功后写入；服务 active 但心跳 FAIL 时，优先
检查 Token 是否被撤销、服务器到 Telegram 的 HTTPS 网络，以及是否有另一程序在使用同一 Token
轮询 `getUpdates`。

## 真机端到端验收

`hle verify` 验证服务端状态；代理模式还应至少运行一次真实客户端测试。直接在
已安装节点上验证回环和出口：

```bash
sudo ./tests/e2e_installed_endpoint.sh --expected-egress SERVER_EGRESS_IP --test-udp
```

从另一台 Linux 主机验证公网入口时，把 `hle show-link` 的输出临时保存为权限 `0600` 的文件，
再运行：

```bash
./tests/e2e_installed_endpoint.sh \
  --uri-file /tmp/hle-node-uri.txt \
  --expected-egress SERVER_EGRESS_IP
```

Windows PowerShell 可运行：

```powershell
$env:HLE_TEST_VLESS_URI = '<temporary VLESS URI>'
.\tests\e2e_remote_client.ps1 -ExpectedEgressIp 'SERVER_EGRESS_IP'
Remove-Item Env:HLE_TEST_VLESS_URI
```

Linux 脚本自动识别 VLESS/SS2022；`--test-udp` 还会做一次 SOCKS5 UDP DNS 往返。Windows 脚本
当前验证 VLESS。两套脚本都会启动临时 SOCKS 客户端、核对实际出口并请求 HTTP 204。URI 文件和环境变量属于
节点凭据，测试结束后立即清除，不要写入日志、Issue 或 Git。

开发者还可在专用测试机运行 `tests/e2e_ss2022_endpoint.sh` 验证临时 SS2022 服务端，或配合
回环测试 API 运行 `tests/e2e_advanced_bot.sh`，完整演练切换、恢复、新增、删除与权限边界；不要
把测试 API 配置用于生产。

## 常见故障

- **URI 无法连接**：核对 `--server` 是否为入口而非出口、端口映射、云安全组和 Realm 端口。
- **普通代理可用但定位不变**：客户端必须使用全局 TUN/VPN，并捕获 UDP、IPv6 和系统服务流量；
  查看日志是否出现 `TRANSLATE` 或 `WIFITILE_TRANSLATE`。
- **定位不可用**：查看 `TRANSLATE_FAIL_CLOSED`、`WIFITILE_FAIL_CLOSED`、证书信任和 Apple 是否
  改变协议。默认 fail-closed，解析不确定时不会悄悄返回真实坐标。
- **固定 SNI 校验失败**：安装器管理的 REALITY target 必须从该机通过有效证书、TLS 1.3 和 HTTP/2 现场
  校验，实际对端 IP 还必须是公网地址；安装器不会自动换用其他 SNI。
- **Telegram 菜单不响应**：确认专用 Bot 已收到 `/start`、Chat ID 正确、没有其他轮询消费者，
  并查看 `hle verify` 的 `Telegram Bot API heartbeat` 与 Bot journal。
- **SS2022 TCP 可用但 UDP 不通**：同时核对云防火墙/UFW 的 UDP 同端口；Realm 纯 TCP 链路不会
  承载 SS2022 原生 UDP。
- **日志停止增长**：活动文件达到 16 MiB 后会暂停文件写入，等待每日 logrotate；同时检查
  systemd journal。

## 客户端边界

节点 URI 只描述代理协议，不会自动配置 iOS 的全局路由、远程 DNS、按 App 排除项或防止 App
绕过 VPN。服务器只能阻断已经到达 Xray 且能识别出定位域名的 QUIC；未进入隧道的 UDP、IPv6、
硬编码 IP、GPS、蜂窝基带、蓝牙和证书固定不在服务端控制范围内。
