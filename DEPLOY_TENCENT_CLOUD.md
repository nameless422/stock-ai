# 腾讯云 Linux 部署

## 1. 登录服务器并初始化

你的项目已经在服务器目录：

```text
/root/.openclaw/workspace/stock-ai
```

直接登录后执行：

```bash
ssh root@<你的服务器公网IP>
cd /root/.openclaw/workspace/stock-ai
chmod +x deploy/setup_server.sh run.sh run_vt.sh start.sh
./deploy/setup_server.sh
```

脚本会自动：

- 安装 `python3`、`venv`、`pip`
- 创建虚拟环境 `/root/.openclaw/workspace/stock-ai/.venv`
- 安装依赖
- 注册并启动 `systemd` 服务 `stock-ai`

## 3. 放行端口

在腾讯云安全组里放行入站端口：

- `22`：SSH 登录
- `8000`：应用访问

然后浏览器访问：

```text
http://<你的服务器公网IP>:8000
```

## 4. 常用运维命令

查看服务状态：

```bash
systemctl status stock-ai --no-pager
```

查看实时日志：

```bash
journalctl -u stock-ai -f
```

重启服务：

```bash
systemctl restart stock-ai
```

停止服务：

```bash
systemctl stop stock-ai
```

## 5. 更新代码

如果你重新上传了代码，服务器执行：

```bash
cd /root/.openclaw/workspace/stock-ai
./.venv/bin/pip install -r requirements.txt
systemctl restart stock-ai
```

## 6. 可选：域名反向代理

如果你后面想绑定域名，建议再加 `Nginx`，对外走 `80/443`，内部转发到 `127.0.0.1:8000`。
