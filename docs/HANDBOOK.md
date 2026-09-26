# Quote/0 公交屏运维手册

这份手册记录这个项目从零到上线的全部步骤，以及背后的原因。目标是：以后你自己（或任何人）
能独立完成调试、部署、改配置、排错和下线，而不只是照抄命令。

---

## 1. 整体架构

```
EventBridge Scheduler ──每 2 分钟触发──▶ Lambda (quote0-busboard)
  (悉尼时间 10:00–18:58)                   │
                                           ├─ 1. 判断现在是否到了刷新点（默认 10:00–17:00，每 30 分钟）
                                           ├─ 2. 调 TfNSW departure_mon，拿每个站台的实时班次
                                           ├─ 3. 用 Pillow 画 296×152 的 1-bit PNG
                                           └─ 4. POST 到 Quote/0 Image API
                                                        │
                                          Quote/0 设备下次唤醒时显示（设备自己的刷新间隔）
```

- **没有服务器、数据库、API Gateway、S3。** 只有一个 Lambda 和一个定时器，成本基本为 0。
- **为什么定时器每 2 分钟触发，而不是直接每 30 分钟？** 刷新节奏（包括可选的高峰加密
  `peak_windows`）由 Lambda 内的 `should_refresh()` 决定，改节奏只需要改环境变量、不用
  改定时器。未到刷新点的调用会立即返回 `{"status": "skipped"}`，不访问任何外部接口。
  每天约 270 次空调用，在 Lambda 免费额度内。
- **显示延迟：** Quote/0 是电池设备，平时休眠，只在自己的唤醒间隔到了才联网取图。
  所以屏幕上显示的是“推送那一刻”的数据，标题栏的 `Updated HH:MM` 就是那个时刻；
  右侧和大数字旁的到站**钟点**无论多晚显示都是准的。

### 代码结构

| 路径 | 作用 |
|---|---|
| `src/app.py` | 全部业务逻辑：配置解析、刷新判断、TfNSW 请求、渲染、推送、Lambda 入口 |
| `src/fonts/` | DejaVu Sans 字体。Lambda 没有系统字体，必须随包带上 |
| `tests/test_app.py` | 单元测试，不访问网络 |
| `scripts/local_refresh.py` | 本地跑一次完整刷新（可选推送），用 `.env` 里的凭据 |
| `scripts/package_lambda.sh` | 打 Lambda 部署包 `build/lambda.zip` |
| `terraform/` | 所有 AWS 资源的定义 |

---

## 2. 凭据清单

| 凭据 | 从哪里拿 | 用在哪里 | 泄露后果 / 轮换方法 |
|---|---|---|---|
| TfNSW API token | [Open Data Hub](https://opendata.transport.nsw.gov.au/) → 个人菜单 → API Tokens | Lambda 环境变量 `TFNSW_API_KEY` | 只能读公开交通数据。在同一页面删除旧 token、新建一个 |
| Quote/0 API key | Dot App → More → API Keys → Create | Lambda 环境变量 `QUOTE0_API_KEY` | 可以控制你的设备显示内容。在 Dot App 删除后重建 |
| Quote/0 设备序列号 | Dot App 设备信息，或 `GET /api/authV2/open/devices` | `QUOTE0_DEVICE_ID` | 不是秘密，但别公开 |
| AWS Access Key | IAM → Users → 用户 → Security credentials | 只在**部署时**给 Terraform / AWS CLI 用，Lambda 运行不需要 | 能动你整个 AWS 账户，最敏感。部署后建议停用，下次部署再启用 |

**本地存放：**

- `.env`（项目根目录，`chmod 600`，git 忽略）：本地脚本用。格式见 `.env.example`。
- `terraform/secrets.auto.tfvars`（`chmod 600`，git 忽略）：Terraform 用。Terraform 会自动
  加载目录下所有 `*.auto.tfvars` 文件，所以不用在命令里指定。
- 仓库里**永远不要**出现真实密钥。提交前可以用 `git grep` 搜一下 key 的前几位确认。

**关于 AWS 密钥：** 不要用 root 账户的 Access Key（ARN 以 `:root` 结尾）。root 密钥无法被
权限策略限制，一旦泄露整个账户就失守。正确做法是建一个 IAM 用户（见第 5.1 节），用它的密钥，
root 账户只开 MFA 用来登录控制台。

---

## 3. 本地开发与调试

### 3.1 环境

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # 然后填入凭据
```

### 3.2 单元测试

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试不访问网络，所有外部请求都用假的 `opener` 替代。

### 3.3 逐段调通（推荐的排错顺序）

每一段独立验证，出了问题能立刻定位是哪一环：

1. **TfNSW 取数据 + 渲染**（不需要 Quote/0 凭据）：

   ```sh
   .venv/bin/python scripts/local_refresh.py
   ```

   打印每个站台的分钟数，并生成 `board-1.png`（多个地点就是 `board-2.png`…）。打开图片看效果。

2. **推送到 Quote/0：**

   ```sh
   .venv/bin/python scripts/local_refresh.py --push
   ```

3. **Lambda 线上调用：** 部署后见第 6 节。

### 3.4 直接调接口（排错时用）

先把 `.env` 加载进当前 shell：

```sh
set -a; . ./.env; set +a
```

TfNSW：按名字找站台编号（`stop_finder`）：

```sh
curl -sG https://api.transport.nsw.gov.au/v1/tp/stop_finder \
  -H "Authorization: apikey $TFNSW_API_KEY" \
  --data-urlencode outputFormat=rapidJSON --data-urlencode type_sf=any \
  --data-urlencode "name_sf=Australia Ave Sydney Olympic Park" \
  --data-urlencode coordOutputFormat=EPSG:4326 --data-urlencode TfNSWSF=true \
  | python3 -c "import json,sys; [print(l['id'], l['name']) for l in json.load(sys.stdin)['locations'] if l.get('type')=='stop']"
```

返回的 `G212726` 去掉 `G` 就是站台编号。**马路两侧是两个不同的编号**，分别对应两个方向。
找对面站台可以按坐标搜附近站点（`/v1/tp/coord`，`type_1=BUS_POINT`，`radius_1=300`）。

TfNSW：看某个站台的班次（`departure_mon`）：

```sh
curl -sG https://api.transport.nsw.gov.au/v1/tp/departure_mon \
  -H "Authorization: apikey $TFNSW_API_KEY" \
  --data-urlencode outputFormat=rapidJSON --data-urlencode mode=direct \
  --data-urlencode type_dm=stop --data-urlencode name_dm=212726 \
  --data-urlencode depArrMacro=dep --data-urlencode TfNSWDM=true \
  --data-urlencode coordOutputFormat=EPSG:4326 \
  | python3 -c "import json,sys; [print(e['transportation']['number'], e['transportation']['destination']['name'], e.get('departureTimeEstimated') or e['departureTimePlanned']) for e in json.load(sys.stdin).get('stopEvents',[])[:10]]"
```

如果 `stopEvents` 为空、`locations` 也为空，说明站台编号不存在（项目最初的 `212711` 就是这个问题）。

Quote/0：看设备状态、轮播内容和设置：

```sh
for p in status loop/list settings; do
  curl -s "https://dot.mindreset.tech/api/authV2/open/device/$QUOTE0_DEVICE_ID/$p" \
    -H "Authorization: Bearer $QUOTE0_API_KEY"; echo
done
```

- `status.current = Snapping`：设备在休眠省电，推送要等下次唤醒才显示。
- `loop/list` 里必须有 `IMAGE_API` 类型的内容，否则推送返回 404。
- `settings.interval.batteryMs / powerMs`：电池 / 插电时的唤醒间隔（毫秒）。

---

## 4. Terraform 基础（够用版）

Terraform 把“AWS 里应该有什么”写成代码（`terraform/*.tf`），然后负责把现实变成代码描述的样子。

| 文件 | 内容 |
|---|---|
| `versions.tf` | Terraform 和 AWS provider 的版本要求 |
| `variables.tf` | 所有可配置项及默认值、校验规则 |
| `main.tf` | 资源定义：Lambda、IAM 角色、日志组、定时器 |
| `outputs.tf` | apply 后打印的信息（函数名、账户 ID 等） |
| `terraform.tfvars.example` | 变量文件模板 |
| `.terraform.lock.hcl` | 锁定 provider 的确切版本，要提交到 git |

核心命令：

| 命令 | 做什么 | 会不会改 AWS |
|---|---|---|
| `terraform init` | 下载 AWS provider 到 `.terraform/` | 不会 |
| `terraform fmt` | 格式化 `.tf` 文件 | 不会 |
| `terraform validate` | 检查语法和变量校验规则 | 不会 |
| `terraform plan -out=tfplan` | 对比代码和现实，列出要增/改/删什么，并存成计划文件 | 不会 |
| `terraform apply tfplan` | 严格执行刚才那份计划 | **会** |
| `terraform output` | 打印输出值 | 不会 |
| `terraform destroy` | 删除这个项目创建的所有资源 | **会** |

**永远先 plan、看清楚、再 apply。** 重点看最后一行 `Plan: X to add, Y to change, Z to destroy`，
以及有没有意外的 `destroy` 或 `-/+`（先删后建，Lambda 会短暂不可用）。

### 4.1 State（状态文件）——最容易踩坑的地方

Terraform 用 `terraform/terraform.tfstate` 记录“我创建过哪些资源、它们的 ID”。

- **它包含 Lambda 的环境变量，也就是你的 API key 明文。** 不能提交、不能分享。
- **丢了 state，Terraform 就不认识已创建的资源。** 再 apply 会尝试重建，因为同名资源已存在而报错。
- 本项目默认使用本地 state。如果在临时环境（例如云端开发容器）里部署，**会话结束前必须
  把 state 保存到安全的地方**，或者改用远端 state（S3 backend）。

丢了 state 的补救：用 `terraform import` 把现有资源重新登记进 state，例如
`terraform import aws_lambda_function.board quote0-busboard`，逐个资源导入。
或者在控制台手动删除这 8 个资源后重新 apply。

---

## 5. 首次部署到 AWS

### 5.1 准备 AWS 账户凭据（推荐做法）

1. 用 root 登录控制台，开启 MFA。
2. IAM → Users → Create user，比如 `quote0-deployer`，不需要控制台访问。
3. 权限：最简单是附加 `AdministratorAccess`。想最小化的话，需要：
   Lambda（创建/更新函数、权限）、IAM（创建角色、内联策略、`iam:PassRole`）、
   EventBridge Scheduler、CloudWatch Logs、`sts:GetCallerIdentity`。
4. 该用户 → Security credentials → Create access key → 用途选 CLI。
5. 写入 `.env` 的 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`。

验证身份：

```sh
set -a; . ./.env; set +a
export AWS_DEFAULT_REGION=$AWS_REGION
aws sts get-caller-identity
```

`Arn` 应该是 `...:user/quote0-deployer`，不是 `...:root`。

### 5.2 安装工具

- Terraform ≥ 1.6：<https://developer.hashicorp.com/terraform/install>（macOS：`brew install terraform`）
- AWS CLI v2：<https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html>
- `zip`、Python 3 + pip

### 5.3 生成 Terraform 变量文件

```sh
set -a; . ./.env; set +a
umask 077
cat > terraform/secrets.auto.tfvars <<EOF
tfnsw_api_key    = "$TFNSW_API_KEY"
quote0_api_key   = "$QUOTE0_API_KEY"
quote0_device_id = "$QUOTE0_DEVICE_ID"
EOF
```

其他变量（地点、时段、刷新间隔）有默认值，要改就在这个文件里追加，写法见 `terraform.tfvars.example`。

### 5.4 打包

```sh
./scripts/package_lambda.sh      # 或 PYTHON=.venv/bin/python ./scripts/package_lambda.sh
```

脚本用 `pip --platform manylinux2014_x86_64 --python-version 3.11 --only-binary=:all:`
下载 **Linux 版** Pillow，所以在 macOS / Windows 上打出来的包也能在 Lambda 上运行，
不需要 Docker。产物是 `build/lambda.zip`（约 9 MB），包含 `app.py`、`fonts/`、`PIL/`。

### 5.5 Plan 和 Apply

```sh
cd terraform
terraform init
terraform plan -out=tfplan
# 仔细看输出，首次部署应该是：Plan: 8 to add, 0 to change, 0 to destroy.
terraform apply tfplan
```

首次部署会创建：

| 资源 | 名字 | 作用 |
|---|---|---|
| `aws_cloudwatch_log_group.lambda` | `/aws/lambda/quote0-busboard` | Lambda 日志，保留 30 天 |
| `aws_iam_role.lambda` | `quote0-busboard-lambda-role` | Lambda 运行身份 |
| `aws_iam_role_policy.lambda_logs` | `quote0-busboard-logs` | 只允许往上面那个日志组写日志 |
| `aws_lambda_function.board` | `quote0-busboard` | 代码本体，Python 3.11，256 MB，超时 30 秒 |
| `aws_iam_role.scheduler` | `quote0-busboard-scheduler-role` | 定时器的身份 |
| `aws_iam_role_policy.scheduler_invoke` | `quote0-busboard-invoke` | 只允许调用这一个 Lambda |
| `aws_scheduler_schedule.refresh` | `quote0-busboard-refresh` | `cron(0/2 10-18 ? * * *)`，悉尼时区，自动处理夏令时 |
| `aws_lambda_permission.scheduler` | — | Lambda 侧只接受这一个定时器的调用 |

权限按最小化设计：Lambda 除了写自己的日志，不能访问任何 AWS 资源；外部调用只有
TfNSW 和 Quote/0 两个 HTTPS 接口。

---

## 6. 部署后验证

手动触发一次（`force_refresh` 会跳过时间段判断，立即刷新）：

```sh
aws lambda invoke \
  --function-name quote0-busboard \
  --region ap-southeast-2 \
  --cli-binary-format raw-in-base64-out \
  --payload '{"force_refresh":true}' \
  /tmp/out.json && cat /tmp/out.json
```

期望结果：

```json
{"status": "updated", "departures": {"Olympic Park": {"Strathfield": 3, "Rhodes": 3}}, "forced": true}
```

看日志：

```sh
aws logs tail /aws/lambda/quote0-busboard --follow --region ap-southeast-2
```

看定时器：

```sh
aws scheduler get-schedule --name quote0-busboard-refresh --region ap-southeast-2
```

---

## 7. 日常变更

**原则：改 → 测 → 打包（只有改代码才需要）→ plan → 看清楚 → apply。**

### 7.1 改代码（比如样式）

```sh
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/local_refresh.py          # 看 board-1.png
./scripts/package_lambda.sh
cd terraform && terraform plan -out=tfplan && terraform apply tfplan
```

Terraform 通过 `source_code_hash`（zip 的哈希）发现代码变了，plan 里会显示
`aws_lambda_function.board` 的 `~ update in-place`。

### 7.2 改地点 / 站台

在 `terraform/secrets.auto.tfvars` 里追加：

```hcl
locations = [
  {
    name     = "Olympic Park"
    route    = "526"
    task_key = "olympic-park"
    stops = [
      { id = "212726", label = "Strathfield" },
      { id = "212727", label = "Rhodes" },
    ]
  },
]
```

- 每个地点 = 一条线路 + 1 到 2 个站台（一个方向一行）。
- 多个地点时，每个地点要在 Dot App 里各加一个 Image API 内容，并配上**不重复**的 `task_key`，
  设备轮播会依次显示。只有一个地点时 `task_key` 可以不填。
- 站台编号的找法见 3.4 节。
- 只改环境变量，不需要重新打包，直接 plan / apply。

### 7.3 改刷新时段

同样在 `secrets.auto.tfvars` 里改：

```hcl
active_start           = "10:00"
active_end             = "17:00"   # 不含
normal_refresh_minutes = 30        # 必须是 2 的倍数
peak_windows           = ""        # 例如 "16:30-18:30"，空字符串表示不启用
peak_refresh_minutes   = 2
```

如果 `active_end` 要晚于 19:00，还要改 `main.tf` 里定时器的小时范围 `10-18`。

**记得同步调整设备唤醒间隔**（Dot App 或 settings 接口），不然推送再频繁，设备也只按自己的
节奏显示。

### 7.4 轮换 API key

改 `.env` → 重新生成 `secrets.auto.tfvars`（5.3 节）→ plan / apply。plan 里 Lambda 的
environment 会显示 `(sensitive value)` 变更，这是正常的。

---

## 8. 排错速查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| Lambda 返回 `skipped` | 不在刷新时段或不在刷新点 | 正常。要立即刷新用 `force_refresh` |
| 日志 `TfNSW departure request failed: HTTP Error 401` | TfNSW token 错或失效 | 重新生成 token，更新变量后 apply |
| 某方向一直 `No buses` | 站台编号错，或线路号不经过该站 | 用 3.4 节的 `departure_mon` 命令核对 |
| `Quote/0 image request failed: HTTP Error 404 (no Image API task ...)` | 设备轮播里没有 Image API 内容，或 `task_key` 不匹配 | Dot App 添加 Image API 内容；用 `loop/list` 核对 key |
| 推送成功但屏幕没变 | 设备休眠中 | 等下次唤醒，或插电；查 `status.renderInfo.next` |
| 字体变丑、没有粗体 | 部署包里缺 `fonts/` | 用 `scripts/package_lambda.sh` 重新打包 |
| `aws` 报 `InvalidClientTokenId` | 密钥错，或当前 shell 里有别的 `AWS_*` 环境变量覆盖了 | `env \| grep AWS_` 检查；重新 `. ./.env` |
| `terraform apply` 报资源已存在 | 本地 state 丢失 | 见 4.1 节，`terraform import` 或手动清理 |
| `Error: Invalid value for variable` | 变量校验没通过（例如站台超过 2 个、多地点缺 `task_key`） | 按报错信息修改 tfvars |

---

## 9. 成本

- Lambda：每天约 270 次调用，大部分几毫秒就返回，远低于免费额度（每月 100 万次）。
- EventBridge Scheduler：每月免费 1400 万次调用。
- CloudWatch Logs：每天几 KB，保留 30 天。

正常情况下每月费用为 0 或几美分。

---

## 10. 下线

```sh
cd terraform
terraform destroy
```

会列出要删除的 8 个资源，确认后删除。然后在 Dot App 里把 Image API 内容移出轮播，
并删除不再使用的 API key / token / AWS Access Key。
