# Stock AI

一个面向 A 股的 FastAPI 后端项目，当前包含 3 个主要页面：

- `/`：单只股票分析
- `/screener`：策略选股
- `/strategies`：策略管理

## 快速开始

先准备 MySQL，并设置环境变量：

```bash
export STOCK_AI_DB_URL='mysql://用户名:密码@127.0.0.1:3306/stock_ai?charset=utf8mb4'
```

安装依赖并启动：

```bash
pip install -r requirements.txt
./run.sh
```

更多使用方式见 [USAGE.md](/Users/zhongyi.zhang/project/python/stock-ai/USAGE.md)。

## 新人先看哪里

如果你是第一次接手这个项目，建议按这个顺序看：

1. [main.py](/Users/zhongyi.zhang/project/python/stock-ai/main.py)
2. [app/app.py](/Users/zhongyi.zhang/project/python/stock-ai/app/app.py)
3. [app/routers](/Users/zhongyi.zhang/project/python/stock-ai/app/routers)
4. [app/services](/Users/zhongyi.zhang/project/python/stock-ai/app/services)
5. [app/repositories](/Users/zhongyi.zhang/project/python/stock-ai/app/repositories)
6. [app/core](/Users/zhongyi.zhang/project/python/stock-ai/app/core)
7. [db/schema.py](/Users/zhongyi.zhang/project/python/stock-ai/db/schema.py)

## 后端结构

当前后端分层是这样的：

- `main.py`
  只负责启动应用
- `app/app.py`
  负责创建 FastAPI 应用、挂载路由、初始化数据库和任务系统
- `app/routers/`
  只处理 HTTP 入参、响应和错误码
- `app/services/`
  放业务逻辑，负责把多个能力串起来
- `app/repositories/`
  只做数据库读写
- `app/core/`
  放可复用的核心模块，比如策略引擎、选股核心、任务系统
- `db/`
  放数据库连接兼容层和 schema 初始化

这套结构的目标是：

- 路由不写 SQL
- 服务层不直接拼页面响应
- 仓储层不夹带业务判断
- 核心引擎与 Web 层解耦

## 当前保留的关键文件

- [run.sh](/Users/zhongyi.zhang/project/python/stock-ai/run.sh)：本地启动入口
- [deploy/deploy_remote.sh](/Users/zhongyi.zhang/project/python/stock-ai/deploy/deploy_remote.sh)：本地一键部署到远程服务器
- [deploy/setup_server.sh](/Users/zhongyi.zhang/project/python/stock-ai/deploy/setup_server.sh)：服务器部署入口
- [deploy/stock-ai.env.example](/Users/zhongyi.zhang/project/python/stock-ai/deploy/stock-ai.env.example)：部署环境变量示例
- [deploy/remote.env.example](/Users/zhongyi.zhang/project/python/stock-ai/deploy/remote.env.example)：远程部署脚本配置示例
- [USAGE.md](/Users/zhongyi.zhang/project/python/stock-ai/USAGE.md)：使用说明
- [DEPLOY_TENCENT_CLOUD.md](/Users/zhongyi.zhang/project/python/stock-ai/DEPLOY_TENCENT_CLOUD.md)：腾讯云部署说明
- [CLAUDE.md](/Users/zhongyi.zhang/project/python/stock-ai/CLAUDE.md)：给代码助手/维护者的说明
