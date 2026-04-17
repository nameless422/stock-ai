# 腾讯云 Linux 部署

通用使用方式见 `USAGE.md`，这里仅保留腾讯云部署步骤。

## 1. 登录服务器并初始化

你的项目已经在服务器目录：

```text
/root/.openclaw/workspace/stock-ai
```

直接登录后执行：

```bash
ssh ubuntu@<你的服务器公网IP>
cd /home/ubuntu/stock-ai
chmod +x deploy/setup_server.sh run.sh
sudo ./deploy/setup_server.sh
```

脚本会自动：

- 安装 `python3`、`venv`、`pip`
- 安装并初始化本机 `MySQL`
- 创建虚拟环境 `/home/ubuntu/stock-ai/.venv`
- 安装依赖
- 写入部署环境文件 `/etc/stock-ai/stock-ai.env`
- 注册并启动 `systemd` 服务 `stock-ai`

默认会创建：

- 数据库：`stock_ai`
- 用户：`stock_ai`
- 密码：`StockAI_123456`
- 服务：`stock-ai`

脚本默认不会改 MySQL `root` 密码；只有你显式设置了 `MYSQL_ROOT_PASSWORD` 才会执行这一步。
应用服务会从 `/etc/stock-ai/stock-ai.env` 读取 `STOCK_AI_DB_URL`。

如果你要改默认值，可以先导出环境变量再执行：

```bash
export MYSQL_DATABASE=stock_ai
export MYSQL_APP_USER=stock_ai
export MYSQL_APP_PASSWORD='你的强密码'
export MYSQL_HOST=127.0.0.1
sudo ./deploy/setup_server.sh
```

如果你确实希望脚本顺手设置 MySQL `root` 密码，再额外加：

```bash
export MYSQL_ROOT_PASSWORD='你的 root 密码'
sudo ./deploy/setup_server.sh
```

## 2. 放行端口

在腾讯云安全组里放行入站端口：

- `22`：SSH 登录
- `8000`：应用访问

如果你要从外部直接连 MySQL，再额外放行：

- `3306`：MySQL

更推荐只开放 `22` 和 `8000`。

然后浏览器访问：

```text
http://<你的服务器公网IP>:8000
```

## 3. 常用运维命令

查看部署环境变量：

```bash
sudo cat /etc/stock-ai/stock-ai.env
```

修改数据库连接后重启服务：

```bash
sudo vi /etc/stock-ai/stock-ai.env
sudo systemctl restart stock-ai
```

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

## 4. 更新代码

如果你重新上传了代码，服务器执行：

```bash
cd /home/ubuntu/stock-ai
./.venv/bin/pip install -r requirements.txt
systemctl restart stock-ai
```

## 5. 可选：域名反向代理

如果你后面想绑定域名，建议再加 `Nginx`，对外走 `80/443`，内部转发到 `127.0.0.1:8000`。
