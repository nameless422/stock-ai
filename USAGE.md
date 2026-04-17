# 项目使用说明

## 1. 项目简介

这是一个 A 股分析服务，包含 3 个主要页面：

- `/`：单只股票分析
- `/screener`：策略选股
- `/strategies`：策略管理

后端基于 FastAPI，当前结构已经按分层整理：

- `main.py`：启动入口
- `app/routers/`：HTTP 路由
- `app/services/`：业务逻辑
- `app/repositories/`：数据库访问
- `app/core/`：策略引擎、选股核心、任务系统
- `db/`：数据库兼容层与 schema
- `templates/`：页面模板
- `static/`：静态资源
- `deploy/setup_server.sh`：服务器部署入口
- `deploy/stock-ai.env.example`：部署环境变量示例

## 2. 本地启动

安装依赖：

```bash
pip install -r requirements.txt
```

启动方式任选其一：

```bash
python main.py
```

```bash
./run.sh
```

默认监听：

```text
http://127.0.0.1:8000
```

## 3. 数据库说明

项目现在只支持 MySQL。

必须设置环境变量：

- `STOCK_AI_DB_URL`

MySQL 连接示例：

```bash
export STOCK_AI_DB_URL='mysql://user:password@127.0.0.1:3306/stock_ai?charset=utf8mb4'
```

数据库 schema 在：

- `db/schema.py`

数据库兼容封装在：

- `db/compat.py`

## 4. 常用环境变量

- `STOCK_AI_DB_URL`：MySQL 连接串
- `SCREENING_MAX_WORKERS`：选股并发数
- `SCREENING_SUBMIT_BATCH`：选股批处理大小
- `SCREENING_SAVE_INTERVAL`：选股结果落库间隔
- `STOCK_INFO_TTL`：股票信息缓存秒数
- `KLINE_TTL`：K 线缓存秒数
- `SEARCH_TTL`：搜索缓存秒数
- `LLM_API_KEY` / `OPENAI_API_KEY` / `MINIMAX_API_KEY`：策略生成/AI 总结使用

## 5. 功能说明

### 股票分析

- 获取股票基础信息
- 获取日 / 周 / 月 K 线
- 计算 MA、MACD、KDJ、RSI、BOLL 等指标
- 输出简单 AI 分析建议

### 策略管理

- 新建策略
- 编辑策略
- 删除策略
- 管理策略组
- 生成策略代码提示上下文

### 选股任务

- 手动触发选股
- 异步任务执行
- 查询任务状态
- 查看历史结果
- 每日定时选股、清理旧数据、同步行情缓存

## 6. 常用接口

- `GET /api/stock/{stock_code}`
- `GET /api/kline/{stock_code}`
- `GET /api/indicators/{stock_code}`
- `GET /api/analyze/{stock_code}`
- `GET /api/search?q=关键词`
- `GET /api/quote/{stock_code}`
- `GET /api/strategies`
- `POST /api/strategies`
- `PUT /api/strategies/{strategy_id}`
- `DELETE /api/strategies/{strategy_id}`
- `GET /api/screener/run`
- `GET /api/screener/status`
- `GET /api/screener/results`
- `GET /api/screener/history`

## 7. 腾讯云部署

部署入口脚本：

```bash
sudo ./deploy/setup_server.sh
```

脚本会完成：

- 安装 Python 运行环境
- 创建 `.venv`
- 安装依赖
- 安装并初始化 MySQL
- 写入 `/etc/stock-ai/stock-ai.env`
- 写入 `systemd` 服务
- 启动 `stock-ai`

更详细的腾讯云部署步骤见：

- `DEPLOY_TENCENT_CLOUD.md`

## 8. 运维命令

查看服务状态：

```bash
systemctl status stock-ai --no-pager
```

查看日志：

```bash
journalctl -u stock-ai -f
```

重启服务：

```bash
systemctl restart stock-ai
```

## 9. 当前已删除的旧功能

以下内容已经移除，不再使用：

- 虚拟炒股
- 用户管理
- 旧迁移脚本
- 旧单文件选股脚本
- 旧临时测试脚本
