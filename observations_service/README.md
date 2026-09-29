# 实测功率与预测偏差接口

本模块与原96点预测服务放在同一个容器，分别运行、分别保存数据。不重新训练，不改预测特征、权重、UTC与北京时间转换或预测历史缓存。实测请求不会触发预测。

## 1. 如何调用

| 用途 | 请求 | 容器端口 |
|---|---|---|
| 原预测：过去7天预测未来1天 | `POST /api/v1/fluxcast/compute` | 8000 |
| 原预测：查询最近成功的96点曲线 | `GET /api/v1/fluxcast/compute/latest` | 8000 |
| 新实测：接收一个时刻的测点，返回总功率和可计算的偏差 | `POST /api/v1/fluxcast/observations` | 8002 |
| 新实测：服务健康状态 | `GET /health` | 8002 |

预测仍按原流程调用，可每天生成一次未来24小时曲线；本模块不会定时生成预测。公司每15分钟主动POST一次实测，不是算法主动向公司拉数据。不使用 `source_id`，每个请求包含七站同一时刻的数据。

在生产部署包根目录调用：

```bash
curl -X POST "http://127.0.0.1:8002/api/v1/fluxcast/observations" -H "Content-Type: application/json" --data-binary @observations_service/examples/input_complete.json
```

Windows使用 `curl.exe`。端口可以通过部署配置改为宿主机其他端口，网关也可以把两个路径映射到同一外部地址。服务无内置鉴权和HTTPS，必须由网关或内网访问控制保护。

## 2. 输入

完整可发送文件：[input_complete.json](examples/input_complete.json)。

- 顶层只有 `point_table` 和 `frames`；`frames` **恰好1帧**，不是预测接口的96帧。
- `point_table` 列全19个功率测点编码，不重复；frame内直接以测点编码为key，保留原编码中的点号和冒号。不传温湿度、不传预先求和的总值。
- 数值单位MW。JSON数值有效；真实0和有限负值保留。字符串、布尔值、null及其他不可用值视为该测点缺测，不把字符串自动当数值。
- 缺测推荐填 `null`，也兼容省略frame中的该字段，但 `point_table` 仍应列全。未知编码、缺少表内编码、帧数错误属于请求格式错误。
- `timestamp` 是该点对应的UTC时刻，不是到达服务器的时间。接受 `2026-11-25 05:00:00`、`2026-11-25T05:00:00Z`、UTC后缀 `+00:00`/`+0000` 等原预测支持的格式；无后缀也按UTC。小数秒可有1至9位，但必须全为0。
- 必须是00/15/30/45分、0秒，不做取整。拒绝非零时区偏移；输出原样保留本帧时间写法。新模块**不再加减8小时**，直接与原预测已返回的UTC目标时刻匹配。
- 请求头 `Content-Type: application/json`，使用UTF-8 JSON及Content-Length，不支持分块传输。最大256 KiB。NaN、Infinity不是合法JSON，请用null。

七站测点数为：高安屯3、京西5、京阳3、京桥3、京丰1、未来2、上庄2，共19点。编码与原预测输入中的功率部分一致，样例列出了全部字段。

## 3. 输出和缺测

`result_point` 只放浮点数；字符串放 `extra_info`，两者内部均为 `varname / timestamp / value`。同一列表内varname唯一；`event_key` 沿用 `JNH.Fluxcast.Compute`。

| 情况 | dataStatus | 返回数值 |
|---|---|---|
| 19点都有效，有同刻可用预测 | complete | totalPowerActual、totalPowerDeviation |
| 19点都有效，无同刻可用预测 | complete | 仅totalPowerActual；reason为no_matching_forecast |
| 只有1至18点有效 | incomplete | 空result_point；missingPoints列出不可用编码 |
| 19点全部不可用 | missing | 空result_point；missingPoints列出全部编码 |

**totalPowerActual = 19个功率测点求和；totalPowerDeviation = 同时刻预测值 − 实测总功率，单位均为MW，不是百分比。** 正偏差表示预测偏高。实测无7天预热要求。

部分缺测不补0、不沿用旧值，不把部分合计冒充完整实测。原预测模块的功率补0规则保持原样，两者用途不同。补传同一UTC时刻会更新实测与偏差，不新增重复记录；不会改写预测值。

缺测、尚无匹配预测均返回HTTP 200，由上述字段区分。缺失数值直接省略，不返回null、占位0或字符串。极端数值导致偏差溢出时保留实测，reason为 `deviation_out_of_range`。

| HTTP | 含义 |
|---|---|
| 400 | 编码表、帧数、时间、JSON或请求头不合法，message说明原因 |
| 404 | 路径不存在 |
| 408 / 413 | 读取请求超时 / 请求过大 |
| 500 | 内部存储或处理错误，应查看日志并重试 |
| 503 | 仅健康检查：存储异常或同步线程退出 |

全部样例位于 [examples](examples/)。这些是**接口说明用的人工数值，不是模型实测成绩**：19点合计1795.0，假设同刻预测1850.0，偏差55.0。在空库中发送完整输入时，应得到 [output_no_forecast.json](examples/output_no_forecast.json)，不能凭该样例得到55.0偏差。

## 4. 如何匹配已有预测

新进程启动后，每5秒只读访问容器内原预测的 `GET /api/v1/fluxcast/compute/latest`，检查96点曲线，归档到独立SQLite数据库。不调用预测POST，也不直接读写原预测缓存。预测服务暂不可用时，仍可返回实测；已有归档可以继续用于匹配。

采用**精确UTC目标时刻**匹配，不找最近时刻、不用请求到达时间、不跨时刻补值。首次实测请求选择“目标时刻前已归档、且覆盖该目标时刻”的最新曲线；选定后固定该批次，后续补传修正实测时仍用它。历史曲线保留，防止latest被新曲线覆盖后迟到实测无据可查。

原预测接口没有可验证的生成时间或批次ID。本模块用曲线内容SHA256标识批次、以实际首次收到的UTC时间为记录依据。**首次取得曲线已经晚于或等于目标时刻时，不补算该点偏差**，避免使用事后预测。相同内容重复读取不会修改首次取得时间。数据库记录输入、响应及选用批次，便于内部追溯。

请在预测生成前启动本模块，并在首个预测目标时刻前留出至少一个同步周期及网络处理余量；服务器时钟应同步。健康检查中的 `forecast_sync`、`last_sync_utc_epoch` 用于检查是否持续取得曲线。`no_forecast`/`unavailable` 不妨碍实测求和。轮询无法保证捕获5秒内连续覆盖的每个中间批次，也不能恢复服务停机期间从未读取的旧曲线；此时宁可缺少偏差，不伪造匹配。

## 5. 部署与测试

同容器配置见 [Docker部署运行说明](../docs/Docker部署运行说明.md)。原端口8000不变，新增8002；宿主机配置：

```dotenv
POWER_OBSERVATIONS_BIND=127.0.0.1
POWER_OBSERVATIONS_PORT=8002
POWER_OBSERVATIONS_RUNTIME_DIR=./runtime_observations
```

新目录挂载至 `/app/runtime_observations`，保存 `observations.sqlite3` 及SQLite辅助文件。**与原runtime分开，不能共用多个实例**。升级/重建保留两个目录；备份前停止服务，整体备份该目录。清空实测库会失去旧预测批次、实测记录和固定匹配关系。当前不自动清理归档，运维需监控磁盘并按业务补传期限另行制定保留策略。

一条Compose启动命令运行两个独立进程，其中一个异常退出时容器退出，由重启策略处理。健康检查同时检查原预测latest和新模块health；健康不代表数据完整或已经有可匹配预测。旧镜像不包含本模块，必须重新构建，不能仅替换Compose后直接使用旧镜像。

不使用Docker时可单独启动实测模块（原预测按原方式启动）：

```bash
python -m observations_service.api --host 127.0.0.1 --port 8002
python -m unittest discover -s observations_service/tests -p "test_*.py" -v
```

模块及测试只用Python标准库，不加载torch或模型。新代码、文档、样例、测试集中在本目录；运行镜像只复制本目录顶层Python文件，不装入样例、测试或已有数据库。历史预测功能仍使用原测试验证，新实测测试不替代模型精度评估。

本地验证结论见 [验证记录](验证记录.md)。从源码执行联合复测需安装原模型依赖，输出使用一个尚不存在的独立测试目录：

```bash
python -m observations_service.tests.run_local_verification --output-dir tests/results/observations_service/local_http_new
```

该复测启动并关闭独立端口的两个服务，不使用生产runtime；测试真实7天历史的96点预测、实测的缺测响应、另行标注的完整测点示例求和、只读预测及重启恢复。GitHub工作流已接入实测检查，但只有实际运行成功才算容器验证通过。
