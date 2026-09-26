# Quote/0 公交屏运维手册

这份手册记录这个项目从零到上线的全部步骤，以及背后的原因。目标是：以后你自己（或任何人）
能独立完成调试、部署、改配置、排错和下线，而不只是照抄命令。

---

## 1. 整体架构

```
EventBridge Scheduler ──每 2 分钟触发──▶ Lambda (quote0-busboard)
  (悉尼时间 10:00–18:58)                   │
                                           ├─ 1. 不在 10:00–17:00 → 直接返回 skipped
                                           ├─ 2. 查 Quote/0 状态接口：设备下次几点醒？
                                           │     不在接下来 2 分钟内 → 返回 skipped
                                           ├─ 3. 调 TfNSW departure_mon，拿每个站台的实时班次
                                           ├─ 4. 用 Pillow 画 296×152 的 1-bit PNG
                                           └─ 5. POST 到 Quote/0 Image API
                                                        │
                                          1–2 分钟后设备醒来，取走这张新图显示
```

- **没有服务器、数据库、API Gateway、S3。** 只有一个 Lambda 和一个定时器，成本基本为 0。
- **显示延迟是核心问题：** Quote/0 平时休眠，只在自己的唤醒间隔到了才联网，从 Dot 云端
  取最新的图。设备不会来找我们，我们也叫不醒它。如果按固定时间推送（比如整点、半点），
  而设备在 :27、:57 醒来，屏幕上的数据就旧了将近 30 分钟。
- **解决办法：跟随设备唤醒（`refresh_mode = "follow_device"`，默认）。** Quote/0 的状态接口
  `GET /device/:id/status` 会给出 `renderInfo.next`，也就是预测的下次唤醒时间（分别给出电池
  和插电两种情况，取其中最早的未来时间就是真正的下次唤醒）。Lambda 每 2 分钟看一眼，
  只在“设备 2 分钟内就会醒”的那一次才取数据、推送。于是设备每次醒来拿到的都是 1–2 分钟前的数据，
  推送次数又和设备唤醒次数一样少。
- **兜底：** 状态接口失败、或者没有未来的唤醒时间（设备离线），自动退回固定节奏
  （`normal_refresh_minutes`，默认每 30 分钟，整点和半点）。`refresh_mode = "fixed"` 可以
  完全关闭跟随逻辑。
- **为什么定时器是每 2 分钟：** 这是跟随精度。每个“唤醒前 2 分钟”窗口里恰好有一次触发。
  不需要推送的触发几毫秒就返回 `{"status": "skipped", "reason": ...}`。
- **调用量（10:00–17:00）：** 状态接口约 210 次/天；推送次数 = 设备唤醒次数
  （电池 30 分钟间隔约 14 次/天，插电 5 分钟间隔约 84 次/天），每次推送调 TfNSW 2 次。
- 标题栏的 `Updated HH:MM` 是数据生成时刻；到站**钟点**无论多晚显示都是准的。

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
| `versions.tf` | Terraform 和 AWS provider 的版本要求、S3 state backend 声明 |
| `backend.hcl.example` | S3 state 配置模板；复制成 `backend.hcl`（git 忽略）后使用 |
| `variables.tf` | 所有可配置项及默认值、校验规则 |
| `main.tf` | 资源定义：Lambda、IAM 角色、日志组、定时器 |
| `outputs.tf` | apply 后打印的信息（函数名、账户 ID 等） |
| `terraform.tfvars.example` | 变量文件模板 |
| `.terraform.lock.hcl` | 锁定 provider 的确切版本，要提交到 git |

核心命令：

| 命令 | 做什么 | 会不会改 AWS |
|---|---|---|
| `terraform init -backend-config=backend.hcl` | 连接 S3 state、下载 AWS provider 到 `.terraform/` | 不会 |
| `terraform fmt` | 格式化 `.tf` 文件 | 不会 |
| `terraform validate` | 检查语法和变量校验规则 | 不会 |
| `terraform plan -out=tfplan` | 对比代码和现实，列出要增/改/删什么，并存成计划文件 | 不会 |
| `terraform apply tfplan` | 严格执行刚才那份计划 | **会** |
| `terraform output` | 打印输出值 | 不会 |
| `terraform destroy` | 删除这个项目创建的所有资源 | **会** |

**永远先 plan、看清楚、再 apply。** 重点看最后一行 `Plan: X to add, Y to change, Z to destroy`，
以及有没有意外的 `destroy` 或 `-/+`（先删后建，Lambda 会短暂不可用）。

### 4.1 State（状态文件）——最容易踩坑的地方

Terraform 用 state 记录“我创建过哪些资源、它们的 ID”。

- **它包含 Lambda 的环境变量，也就是你的 API key 明文。** 不能提交、不能分享。
- **丢了 state，Terraform 就不认识已创建的资源。** 再 apply 会尝试重建，因为同名资源已存在而报错。

所以本项目把 state 放在 **S3**（远端 state），而不是某台电脑的本地文件：

| 设置 | 值 | 为什么 |
|---|---|---|
| 桶名 | `quote0-busboard-tfstate-<账户ID>` | S3 桶名全球唯一，带上账户 ID 避免冲突 |
| key | `quote0-busboard/terraform.tfstate` | 桶里的对象路径 |
| 公开访问 | 全部禁止（Block Public Access 四项全开） | state 里有密钥 |
| 版本控制 | 开启 | state 写坏了可以回滚到上一版 |
| 加密 | SSE-S3（AES256） | 静态加密 |
| 锁 | `use_lockfile = true` | 两个人同时 apply 时互斥（Terraform ≥ 1.10，S3 原生锁，不需要 DynamoDB） |

**任何一台电脑**只要有 AWS 凭据和 `backend.hcl`，`terraform init -backend-config=backend.hcl`
之后就能接着管理这套资源。

`backend.hcl` 为什么不直接写进 `versions.tf`？因为 backend 块不能用变量，而桶名里含账户 ID，
属于环境相关配置，所以用“partial configuration”：代码里只写 `backend "s3" {}`，具体值由
`-backend-config` 文件提供。

state 出问题时：

- 看历史版本：`aws s3api list-object-versions --bucket <桶> --prefix quote0-busboard/`
- 回滚：下载某个旧版本覆盖回去（先确认它和 AWS 实际资源一致）。
- 锁没释放（apply 中途被杀）：`terraform force-unlock <LOCK_ID>`，LOCK_ID 在报错信息里。
- state 彻底丢失：用 `terraform import` 把现有资源重新登记，例如
  `terraform import aws_lambda_function.board quote0-busboard`，逐个资源导入。

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

### 5.3 创建 state 桶（每个 AWS 账户只做一次）

state 桶要在 Terraform 之前存在（鸡生蛋问题），所以用 AWS CLI 手动建：

```sh
set -a; . ./.env; set +a
export AWS_DEFAULT_REGION=$AWS_REGION
ACCT=$(aws sts get-caller-identity --query Account --output text)
B=quote0-busboard-tfstate-$ACCT

aws s3api create-bucket --bucket $B --region $AWS_REGION \
  --create-bucket-configuration LocationConstraint=$AWS_REGION
aws s3api put-public-access-block --bucket $B --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket $B --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket $B --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

sed "s/<aws-account-id>/$ACCT/" terraform/backend.hcl.example | grep -v '^#' > terraform/backend.hcl
```

`create-bucket` 在 `us-east-1` 以外的区域必须带 `LocationConstraint`，否则报错。

### 5.4 生成 Terraform 变量文件

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

注意：`secrets.auto.tfvars` 只在本地，不在 S3。换电脑部署时要按这一步重新生成。

### 5.5 打包

```sh
./scripts/package_lambda.sh      # 或 PYTHON=.venv/bin/python ./scripts/package_lambda.sh
```

脚本用 `pip --platform manylinux2014_x86_64 --python-version 3.11 --only-binary=:all:`
下载 **Linux 版** Pillow，所以在 macOS / Windows 上打出来的包也能在 Lambda 上运行，
不需要 Docker。产物是 `build/lambda.zip`（约 9 MB），包含 `app.py`、`fonts/`、`PIL/`。

### 5.6 Plan 和 Apply

```sh
cd terraform
terraform init -backend-config=backend.hcl
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

期望结果（`force_refresh`）：

```json
{"status": "updated", "departures": {"Olympic Park": {"Strathfield": 3, "Rhodes": 3}}, "forced": true}
```

不带 `force_refresh` 时，大多数调用会返回 `{"status": "skipped", "reason": "device wakes at 12:57"}`
之类，说明它查过设备、还没到唤醒前 2 分钟，这是正常的。

确认跟随逻辑在工作：看日志里 `Refresh due: device wakes at HH:MM` 的时间，
应该总是比 HH:MM 早 1–2 分钟；再对照设备状态接口里的 `renderInfo.last`。

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
refresh_mode           = "follow_device"  # 或 "fixed"
normal_refresh_minutes = 30        # fixed 模式或兜底时的节奏，必须是 2 的倍数
peak_windows           = ""        # 例如 "16:30-18:30"，空字符串表示不启用
peak_refresh_minutes   = 2
```

如果 `active_end` 要晚于 19:00，还要改 `main.tf` 里定时器的小时范围 `10-18`。

在 `follow_device` 模式下，**屏幕多久更新一次由设备自己的唤醒间隔决定**（Dot App 里设置，
或 `POST /device/:id/settings` 的 `interval.batteryMs / powerMs`），Lambda 会自动跟上，
不需要改 Terraform。

### 7.4 轮换 API key

改 `.env` → 重新生成 `secrets.auto.tfvars`（5.4 节）→ plan / apply。plan 里 Lambda 的
environment 会显示 `(sensitive value)` 变更，这是正常的。

---

## 8. 排错速查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| Lambda 返回 `skipped` | 不在刷新时段，或设备还没到唤醒前 2 分钟（看 `reason`） | 正常。要立即刷新用 `force_refresh` |
| 日志 `Device status unavailable; using fixed cadence` | Quote/0 状态接口失败 | 偶发可忽略；持续出现检查 `QUOTE0_API_KEY`、设备序列号 |
| 日志 `No upcoming device wake reported` | 设备离线（没电、断网），预测时间都已过去 | 给设备充电 / 联网；期间按固定节奏推送 |
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
- S3 state 桶：一个 20 KB 左右的文件加历史版本，可忽略。

正常情况下每月费用为 0 或几美分。

---

## 10. 下线

```sh
cd terraform
terraform destroy
```

会列出要删除的 8 个资源，确认后删除。然后在 Dot App 里把 Image API 内容移出轮播，
并删除不再使用的 API key / token / AWS Access Key。

state 桶不归 Terraform 管，确认不再需要后手动删除（开了版本控制，要先删掉所有版本）：

```sh
aws s3api delete-objects --bucket $B --delete "$(aws s3api list-object-versions --bucket $B \
  --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json)"
aws s3api delete-bucket --bucket $B
```

---

## 附录：当前部署记录

| 项目 | 值 |
|---|---|
| 首次部署 | 2026-09-26 |
| 区域 | `ap-southeast-2`（悉尼） |
| Lambda | `quote0-busboard`，Python 3.11，x86_64 |
| 定时器 | `quote0-busboard-refresh`，`cron(0/2 10-18 ? * * *)`，`Australia/Sydney` |
| 刷新策略 | `follow_device`：10:00–17:00 内，设备每次唤醒前 1–2 分钟推送；查不到唤醒时间时每 30 分钟 |
| 日志组 | `/aws/lambda/quote0-busboard`，保留 30 天 |
| State | `s3://quote0-busboard-tfstate-<账户ID>/quote0-busboard/terraform.tfstate` |
| 地点 | Olympic Park，526：`212726` → Strathfield，`212727` → Rhodes |
| 设备 | Quote/0，电池唤醒间隔 30 分钟，夜间休眠 22:00–08:00，轮播里只有 1 个 Image API 内容 |
| 部署验证 | `force_refresh` 返回 `updated`，两方向各 3 班；普通调用返回 `skipped` |
| 2026-09-26 更新 | 切换为 `follow_device`：只原地更新 Lambda（代码 + `REFRESH_MODE`） |
