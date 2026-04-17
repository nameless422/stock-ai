# 后端结构说明

如果你是第一次维护这个后端，先按这个顺序看：

1. `app.py`
2. `routers/`
3. `services/`
4. `repositories/`
5. `core/`

各目录职责：

- `routers/`
  只做接口收参、返回值、状态码处理
- `services/`
  放业务逻辑，负责把多个模块串起来
- `repositories/`
  只负责数据库读写
- `core/`
  放独立于 Web 层的核心模块，比如策略引擎、选股执行、任务系统

维护约定：

- 不要在 `routers/` 里直接写 SQL
- 不要在 `repositories/` 里塞业务判断
- 能复用的纯逻辑优先放到 `core/`
- 一个新功能优先按 `router -> service -> repository/core` 这条链路落地
