# HEARTH · Edge Lab

面向 HEARTH / Thermodynamic Food Grid 的边缘软件研究原型。当前交付覆盖任务清单 T01–T05 的核心链路，并提前实现 T06 / T14 所需的本地持久化与可重试事件队列。原仓库只有 README，本次属于首批实现。

**仅用于研究演示或人工监督下的影子采集。所有参数均未经过真实食品批次标定。绿灯表示模型品质状态，`sale_allowed` 与 `safety_certified` 始终为 `false`。本项目不会批准销售、出具食品安全证书、发放碳信用或执行支付。**

## 本地运行

需要 Python 3.13+。macOS 示例：

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
hearth demo --scenario normal --hours 24 --output artifacts/normal.json
hearth serve
```

打开终端显示的本地页面。首次运行会在 `artifacts/credentials.json` 创建三个随机凭证，分别对应 `viewer`、`ingest`、`operator`。将本地文件中的 `operator` 值填入演示页面。凭证文件在 POSIX 系统上使用 `0600` 权限，并已被 `.gitignore` 排除。页面不把凭证写入 URL、浏览器持久存储或日志。

服务仅绑定 `127.0.0.1`，默认端口 8000。`hearth serve --port 8001` 可更换端口。禁止将开发服务未经加固暴露到公网。

```bash
# 五种合成场景：normal / cooled / heatwave / outage / sensor_fault
hearth demo --scenario outage --hours 72 --interval-minutes 1

# 导出与运行时代码同源的 JSON Schema 和 OpenAPI 3.1 JSON
hearth contracts

# 产品、阈值、单位、时间容差与容量限额配置
hearth --config src/hearth/defaults.json demo --scenario cooled
```

`defaults.json` 是示例配置，可复制后修改。数据库保存配置指纹，已有数据库遇到配置变化会拒绝启动，防止红灯历史被新阈值静默重解释。原型阶段请使用新数据库；后续需补充带版本的迁移流程。

## 现在能运行什么

传感器契约 → 校验与去重 → 等效温度暴露 → 三色状态及独立健康标记 → SQLite 状态/台账/待发送事件 → 本地演示页面。

模拟器生成 NH3、CO2、乙烯与温湿度读数，含随机噪声、漂移、缺测和故障场景。气体数据已进入契约与台账，**尚未用于真实品质推断**。当前推断使用温度历史；Gompertz 曲线只作为单独的合成对照，不能用于证明真实预测精度。

本地服务提供 `/v1/readings:batch`、批次注册与关闭、摊位状态、条件货架期、台账分页与校验、隔离演示等功能。完整契约见 `contracts/`。未实现的订单、定价、POS、ESG、返利等功能没有伪造成功的占位端点。

## 验证

```bash
python -m pytest -q
python -m coverage run -m pytest
python -m coverage report -m
python -m pip install -e '.[visual]'
python -m playwright install chromium
python scripts/visual_check.py
python scripts/static_review.py
```

`visual_check.py` 会启动真实本地服务并测试桌面、手机、320px 窄屏，生成截图与 JSON。浏览器策略禁止本地导航的受限环境可使用 `--bridge`：本地 HTML/CSS/JS 由 Playwright 渲染，API 请求经 Python 转发至同一真实本地服务。该模式不等同于浏览器原生网络测试。

架构及任务边界见 `docs/ARCHITECTURE.md`，原始材料的问题与修订见 `docs/REVIEW.md`，实际测试记录及未验证项见 `docs/VALIDATION.md`。
