# 京能七站总功率预测服务

接收七站真实功率、温度和湿度，返回未来24小时的 **96点总功率预测**，间隔15分钟，单位MW。当前模型为 `trend_detail_7station_2025_v1`，包含NHITS、PatchTST和StationAttentionHF的TrendDetail融合；无需重新训练。

原预测平台JSON接口：`POST /api/v1/fluxcast/compute`，输入 `point_table + frames`，输出 `result_point`。`varname` 为 `totalPowerForecast`，`event_key` 沿用 `JNH.Fluxcast.Compute`。历史/天气未就绪返回HTTP 200和空结果，附原因。

新增独立实测接口 `POST /api/v1/fluxcast/observations`：接收七站19个功率测点的单帧UTC数据，返回实测总功率及可匹配的预测偏差。与预测服务在同一容器内分进程运行，默认端口分别为8000、8002，缓存分开。原预测代码与权重不变。

**时间规则：历史训练数据已确认为北京时间（`Asia/Shanghai`）。接口输入、输出均为UTC；预测模型内部转换为北京时间，保持与训练特征一致。输出保留本次请求的时间格式，预测请求的96帧须使用一致格式。实测接口按UTC同刻匹配，不重复换算。模型权重不变；目标服务器仍需联调验收，使用与当前源码提交对应的镜像。**

## 交接文档

| 内容 | 文件 |
|---|---|
| 预测请求、响应、缺测、原因码与完整JSON样例 | [接口交接说明](docs/接口交接说明.md) |
| 预测所需七站35个原始测点编码及单位 | [测点需求清单](docs/测点需求清单.md) |
| 构建、启动、缓存、镜像交付与验收 | [Docker部署运行说明](docs/Docker部署运行说明.md) |
| 本地整改状态、真实数据复测和待确认项 | [平台适配验证记录](docs/平台适配验证记录.md) |
| 单帧实测接收、缺测状态、UTC匹配及输入输出样例 | [实测接口说明](docs/实测接口时间与预测匹配说明.md) |

源码交付至公司指定的功率预测GitLab分支，包含运行代码、权重、文档、样例、测试和部署配置。大型镜像及SHA256校验文件交付到公司制品库或指定目录，不提交普通源码历史。本工程未配置公司GitLab Runner流水线。

## 文件用途

| 路径 | 用途 |
|---|---|
| `app/api.py`、`app/platform_adapter.py` | HTTP路由、平台输入输出适配、空结果和日志 |
| `app/time_policy.py` | UTC与模型北京时间转换、缓存时间规则版本 |
| `app/input_adapter.py`、`app/history_cache.py` | 测点、特征、缺失处理及历史缓存 |
| `app/predict.py`、`app/models/` | 预测计算代码 |
| `observations_service/` | 新实测服务、独立归档、同容器启动器及该模块文档和测试 |
| `models/active_model.json`、`models/versions/` | 当前模型配置与训练权重，必须保留 |
| `tests/` | 接口、缓存、打包与真实数据滚动测试 |
| `docs/`、`examples/` | 交接文档和JSON样例 |
| `Dockerfile`、`requirements.txt`、`compose.yaml`、`.env.example` | 镜像构建和部署配置 |
| `.github/workflows/build-image.yml` | 平台接口、重建恢复、73天评分及镜像导入测试 |

`runtime/` 是预测运行缓存，`runtime_observations/` 是实测与预测曲线归档，均不交付、不预装。`tests/`、`docs/`、`examples/` 及实测目录中的文档、测试、样例不进入运行镜像。

## 数据接入

### 预测接口（8000）

1. 每次提交七站同一时间范围的完整快照，共96帧，每15分钟一帧；预测调用频率沿用原流程，可每天生成一次未来24小时曲线。
2. `point_table` 列全19个功率、8个温度、8个湿度编码，`frames` 内直接使用测点编码作为key。缺测推荐写 `null`，也兼容省略该帧测点。
3. 预测服务先从早到晚补历史，或持续累计。672个连续且天气可构造的点（7天）满足后才预测；不足返回200和空结果，不清空缓存。
4. 从 `result_point[].timestamp` 和 `value` 读取未来七站总功率。`reason` 和 `message` 解释空结果。HTTP 200不等于已有预测。

预测输入中的功率缺失补0，真实0和有限负值保留；天气仅沿用该测点过去真实值。平台不传未来天气，不提供模型内部特征，也不需要把训练CSV上传生产服务器。

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/fluxcast/compute" -H "Content-Type: application/json" --data-binary @examples/platform_input_example.json
curl "http://127.0.0.1:8000/api/v1/fluxcast/compute/latest"
```

Windows使用 `curl.exe`；跨机器地址由部署方提供，Compose默认仅监听本机。预测样例由实际CSV的北京时间换算为UTC；空缓存只提交一天预测样例不会产生预测。平台已经发送UTC时，无需再手动加减8小时。

### 实测接口（8002）

平台每15分钟推送一次七站同一时刻的19个功率测点，使用 `point_table + frames`，每次恰好1帧；不传温湿度，无需7天历史预热。

19点完整才返回实测总功率，有符合条件的同刻UTC预测才返回“预测减实测”的偏差。缺测不补0、不返回部分合计，通过 `extra_info` 返回状态及缺测编码；实测请求不触发预测。

对接用JSON位于根目录 `examples/`：见 [实测输入](examples/observations_input_example.json)、[实测输出](examples/observations_output_example.json)。无匹配预测及缺测样例、字段和返回条件见 [实测接口说明](docs/实测接口时间与预测匹配说明.md)。

```bash
curl -X POST "http://127.0.0.1:8002/api/v1/fluxcast/observations" -H "Content-Type: application/json" --data-binary @examples/observations_input_example.json
```

## 检查与测试

在源码根目录、Python 3.11独立环境执行：

```bash
python -m pip install -r requirements.txt
python -m pip install matplotlib
python check_config.py
python -m unittest discover -s tests -p "test_*.py" -v
python -m unittest discover -s tests/migration_2025 -p "test_*.py" -v
python tests/run_platform_verification.py --output-dir tests/results/platform_adapter/check_01
```

最后一条会自行启动和关闭本机测试服务，使用独立缓存，验证平台接口、旧路由已移除、异常输入、重启恢复、旧时间规则结果隔离，以及北京时间2025-10-20至12-31共73天/7008点的滚动预测与MAPE。请求先换算为UTC，评分再与原始时间对齐。输出目录必须不存在，复测换新目录。结果见 `verification.json`，不会连接生产服务或修改模型。

已有独立测试服务时也可执行：

```bash
python tests/run_rolling_accuracy_test.py --base-url http://127.0.0.1:18000 --output-dir tests/results/platform_adapter/score_01
```

该测试服务必须从空缓存开始。脚本的 `--target-start`、测点样例生成器的 `--end-time` 均按原CSV北京时间填写，发送的JSON自动换算为UTC。评分CSV的 `ts` 保留北京时间，`timestamp_utc` 为接口返回时间；历史回放不替代实时平台验收。

## 构建与启动

在同架构构建机安装Docker Engine和Compose 2.20+，在源码根目录执行：

```bash
docker build -t jingneng-power-forecast:7station-platform-v1 .
```

首次复制 `.env.example` 为 `.env`，填写 `POWER_FORECAST_IMAGE=jingneng-power-forecast:7station-platform-v1`，已有配置不要覆盖。首次联调使用新的专用runtime目录，不复制旧接口缓存。

```bash
docker compose config --quiet
docker compose up -d --pull never --wait --wait-timeout 300
docker compose ps
docker compose logs --tail 100 forecast
```

构建打包现有权重，不重新训练。源码构建需要下载基础镜像和依赖；离线镜像导入、持久化、端口和运维方法见部署说明。服务没有内置鉴权和HTTPS，交由网关或受控内网管理。

## 版本与下载

原预测保留平台接口及六个配套JSON样例，新实测的样例单独位于 `observations_service/examples/`。旧接口和旧样例已移除。旧版可在Git历史中查阅，下载当前版本的源码ZIP不会包含历史目录或两套代码。模型权重仍为同一套七站模型，输出不含固定历史参考 `accuracy`。

从GitHub默认分支 `main` 下载或克隆当前源码；交付时记录提交号。只复制该提交内的受版本管理文件，不把本地 `runtime/`、`runtime_observations/`、`tests/results/`、`tests/fixtures/` 或开发环境一起上传。本地新增实测模块后须重新构建并验证镜像，历史Release附件不会自动更新。

[GitHub历史发布 v7station-2025-042dcd1](https://github.com/zhangqian-1/jingneng-power-forecast/releases/tag/v7station-2025-042dcd1) 包含旧接口源码及AMD64/ARM64镜像，当时测试MAPE约10.8778%。它们不包含本次平台适配，不能当作新版镜像使用。下载私有仓库附件需要有权限的账号。

新版构建通过后，镜像ZIP、ZIP校验文件及测试报告直接保存至 [源码仓库Releases](https://github.com/zhangqian-1/jingneng-power-forecast/releases)，标签为 `v7station-platform-提交号前12位`，不再依赖Actions产物存储。确认版本后，镜像可另行发布至 [公开下载仓库](https://github.com/zhangqian-1/jingneng-power-forecast-downloads/releases)，也可将完整ZIP及校验文件直接发给部署人员。公开下载仓库的自动生成 `Source code` 附件只有下载说明，不是算法源码或镜像。

交付时核对源码、镜像、`release.json` 和 `SHA256SUMS` 对应关系，按服务器架构选择一个镜像ZIP。正式上线前仍须完成目标服务器平台联调。
