# Terraform 使用说明

这份文档专讲本项目怎么用 Terraform：它是什么、项目里怎么组织、state 为什么放在 S3、
Terraform 每次从哪里拿到 AWS 权限、第一次和换电脑时怎么初始化、平时怎么做增量更新。

日常操作步骤（改站点、看日志、排错等）见 [`HANDBOOK.md`](HANDBOOK.md)；这里讲原理和“为什么”。

---

## 目录

1. [全景图：Terraform 相关的东西都在哪](#1-全景图terraform-相关的东西都在哪)
2. [基本概念](#2-基本概念)
3. [本项目的 Terraform 设计](#3-本项目的-terraform-设计)
4. [疑惑一：怎样一开始就把 state 放在 S3，并且以后一直用它](#4-疑惑一怎样一开始就把-state-放在-s3并且以后一直用它)
5. [疑惑二：Terraform 怎样一直拿到 S3 和 AWS 的访问权限](#5-疑惑二terraform-怎样一直拿到-s3-和-aws-的访问权限)
6. [疑惑三：初始化到底做了什么，换电脑怎么重新初始化](#6-疑惑三初始化到底做了什么换电脑怎么重新初始化)
7. [增量更新：改了东西之后发生什么](#7-增量更新改了东西之后发生什么)
8. [命令速查](#8-命令速查)
9. [常见问题](#9-常见问题)

---

## 1. 全景图：Terraform 相关的东西都在哪

```
你的电脑（或任何运行 terraform 的地方）                          AWS 账户（ap-southeast-2）
┌──────────────────────────────────────────────┐              ┌──────────────────────────────────┐
│ git 仓库里（提交）                             │              │ S3 桶 quote0-busboard-tfstate-<ID>  │
│   terraform/versions.tf   backend "s3" {}     │              │   quote0-busboard/terraform.tfstate │◀─┐
│   terraform/main.tf       8 个资源的定义       │              │   quote0-busboard/…tfstate.tflock   │  │ state
│   terraform/variables.tf  变量和默认值         │              └──────────────────────────────────┘  │ 读写
│   terraform/outputs.tf                        │                                                    │
│   terraform/.terraform.lock.hcl provider 版本  │   terraform plan / apply                           │
│                                              │ ───────────────────────────────────────────────────┤
│ 只在本机（git 忽略）                            │   用 AWS 凭据签名的 HTTPS 请求                      │
│   terraform/backend.hcl        state 在哪      │                                                    │
│   terraform/secrets.auto.tfvars 三个密钥变量   │              ┌──────────────────────────────────┐  │ 资源
│   terraform/.terraform/        init 生成的缓存 │              │ Lambda、定时器、IAM 角色、日志组 … │◀─┘ 增删改查
│   build/lambda.zip             打包产物        │              └──────────────────────────────────┘
│   AWS 凭据（环境变量 / ~/.aws / SSO）           │
└──────────────────────────────────────────────┘
```

记住三句话：

- **代码（`.tf`）描述“应该是什么样”**，提交在 git 里，所有人共享。
- **state 记录“上次部署成了什么样”**，放在 S3，所有人共享，只有一份。
- **凭据和密钥只在运行 terraform 的那台机器上**，Terraform 自己从不保存它们。

---

## 2. 基本概念

| 概念 | 一句话解释 | 本项目里的例子 |
|---|---|---|
| **声明式（IaC）** | 写“最终应该有什么”，而不是“先做什么后做什么” | `main.tf` 只写了“有一个 256 MB 的 Lambda”，没写怎么创建 |
| **provider** | 插件，把资源声明翻译成某个平台的 API 调用 | `hashicorp/aws` 5.100.0，`init` 时下载的 675 MB 程序 |
| **resource** | 一个由 Terraform 创建和管理的东西 | `aws_lambda_function.board`、`aws_scheduler_schedule.refresh` |
| **data source** | 只读查询，不创建任何东西 | `data "aws_caller_identity" "current"`：查当前账户 ID |
| **variable** | 输入参数，可以有默认值和校验规则 | `locations`、`active_start`、`tfnsw_api_key` |
| **local** | 代码内部的中间值 | `local.lambda_environment`：把变量拼成 Lambda 环境变量 |
| **output** | apply 后打印出来的值 | `lambda_function_name`、`aws_account_id` |
| **state** | Terraform 的“账本”：资源名 ↔ AWS 真实 ID，以及上次的属性 | S3 上的 `terraform.tfstate`（JSON） |
| **backend** | state 存在哪、怎么加锁 | `s3`，锁用 S3 原生锁文件 |
| **plan** | 比较“代码 / state / 真实 AWS”，列出要做的改动，**不改任何东西** | `Plan: 0 to add, 1 to change, 0 to destroy` |
| **apply** | 执行 plan 里的改动，并把新 state 写回 backend | 调用 `UpdateFunctionCode` 等 API |
| **依赖图** | 资源之间的引用决定创建顺序 | Lambda 引用了 IAM 角色 → 先建角色 |

两个名字很像、但完全不同的“锁”：

| | `.terraform.lock.hcl` | `terraform.tfstate.tflock` |
|---|---|---|
| 锁的是什么 | **provider 版本**（保证大家用同一个 5.100.0） | **state**（同一时间只能一个人 plan/apply） |
| 在哪 | git 仓库里，提交 | S3 里，运行期间临时存在，结束即删 |
| 谁生成 | `terraform init` | `terraform plan/apply` 开始时 |

---

## 3. 本项目的 Terraform 设计

### 3.1 文件分工

| 文件 | 提交 | 内容 |
|---|---|---|
| `versions.tf` | ✅ | Terraform ≥ 1.10、AWS provider `~> 5.0`、`backend "s3" {}` |
| `main.tf` | ✅ | provider 配置（区域）、8 个资源、`locals` |
| `variables.tf` | ✅ | 所有变量：类型、默认值、校验（如“每个地点 1–2 个站台”） |
| `outputs.tf` | ✅ | 函数名、ARN、定时器名、账户 ID |
| `.terraform.lock.hcl` | ✅ | provider 的确切版本和校验和 |
| `backend.hcl.example` / `terraform.tfvars.example` | ✅ | 两个本地文件的模板 |
| `backend.hcl` | ❌ | state 桶名（含账户 ID）、key、区域、加密、锁 |
| `secrets.auto.tfvars` | ❌ | `tfnsw_api_key`、`quote0_api_key`、`quote0_device_id` 以及想覆盖的变量 |
| `.terraform/` | ❌ | `init` 生成：backend“便条” + provider 插件 |
| `tfplan` | ❌ | `plan -out` 生成的计划文件（含密钥） |

### 3.2 资源和依赖关系

```
aws_cloudwatch_log_group.lambda ─┐
aws_iam_role.lambda ─────────────┼─▶ aws_iam_role_policy.lambda_logs ─┐
                                 │                                    ├─▶ aws_lambda_function.board ─┬─▶ aws_iam_role_policy.scheduler_invoke
                                 └────────────────────────────────────┘                             ├─▶ aws_scheduler_schedule.refresh ─▶ aws_lambda_permission.scheduler
aws_iam_role.scheduler ──────────────────────────────────────────────────────────────────────────────┘
```

箭头来自代码里的引用（例如 `role = aws_iam_role.lambda.arn`）和 `depends_on`。Terraform 按这张图
并行创建没有依赖关系的资源，删除时则反过来。

### 3.3 Terraform 管什么、不管什么

| Terraform 管 | Terraform **不**管（在别处处理） |
|---|---|
| Lambda 的代码包上传、运行时、内存、超时、环境变量 | 打包代码：`scripts/package_lambda.sh` 生成 `build/lambda.zip` |
| 定时器的频率、时区、目标 | 存 state 的 S3 桶本身（先有桶才能有 state，见 4.2） |
| IAM 角色和权限、日志组和保留期 | Quote/0 设备的唤醒间隔和轮播内容（在 Dot App 里设置） |
| 所有业务配置（通过 Lambda 环境变量） | AWS 凭据本身 |

### 3.4 变量的值从哪里来

同一个变量可能在多个地方被赋值，**后面的覆盖前面的**：

1. `variables.tf` 里的 `default`
2. 环境变量 `TF_VAR_<变量名>`，例如 `TF_VAR_active_end=18:00`
3. `terraform.tfvars` / `terraform.tfvars.json`（本项目没用）
4. `*.auto.tfvars`，按文件名字母顺序（本项目的 `secrets.auto.tfvars`，**自动加载**，不用在命令里写）
5. 命令行 `-var-file=...` 和 `-var name=value`

`tfnsw_api_key`、`quote0_api_key`、`quote0_device_id` 没有默认值，所以 **plan 和 apply 都需要**
`secrets.auto.tfvars`（或对应的 `TF_VAR_*`），否则 Terraform 会报缺少变量。

### 3.5 密钥的流向

```
secrets.auto.tfvars ──▶ var.tfnsw_api_key ──▶ local.lambda_environment ──▶ Lambda 环境变量
                                                                     └──▶ S3 上的 state（明文！）
```

`variables.tf` 里标了 `sensitive = true` 的变量，只是在终端输出里显示成 `(sensitive value)`，
**state 文件里仍然是明文**。所以 state 桶必须私有、加密，而且不要把 state 下载后随意保存。

---

## 4. 疑惑一：怎样一开始就把 state 放在 S3，并且以后一直用它

### 4.1 原理：backend 写在代码里

Terraform 默认把 state 存成当前目录下的 `terraform.tfstate` 文件（local backend）。
只要代码里声明了别的 backend，它就会改用那个：

```hcl
# terraform/versions.tf
terraform {
  backend "s3" {}
}
```

因为这行在 git 仓库里，**任何人在任何电脑上 `init` 这份代码，都只能把 state 接到 S3**，
不会不小心又用回本地文件。这就是“以后一直用它”的保证。

`{}` 里是空的，具体的桶和路径从 `backend.hcl` 传进来（partial configuration）：

```hcl
# terraform/backend.hcl（本地文件）
bucket       = "quote0-busboard-tfstate-<账户ID>"   # 哪个桶
key          = "quote0-busboard/terraform.tfstate"  # 桶里的哪个对象，自己起名
region       = "ap-southeast-2"                     # 桶所在区域
encrypt      = true                                 # 要求服务端加密
use_lockfile = true                                 # 用 S3 锁文件做并发保护（Terraform ≥ 1.10）
```

为什么不直接写进 `versions.tf`？`backend` 块里**不能用变量**，而桶名带账户 ID、属于环境相关的配置，
拆出来可以让同一份代码部署到不同账户。如果只有一个账户、仓库也是私有的，直接写死在 `versions.tf`
里也完全可以，那样 `init` 时就不用带 `-backend-config`。

### 4.2 第一次：先有桶，再有 state（鸡生蛋）

state 要存进桶里，但桶不能由“把 state 存在这个桶里”的 Terraform 来创建。所以第一次要先单独建桶：

```sh
ACCT=$(aws sts get-caller-identity --query Account --output text)
B=quote0-busboard-tfstate-$ACCT
aws s3api create-bucket --bucket $B --region ap-southeast-2 \
  --create-bucket-configuration LocationConstraint=ap-southeast-2
aws s3api put-public-access-block --bucket $B --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning  --bucket $B --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption  --bucket $B --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
```

（另一种常见做法：单独写一个很小的 `bootstrap/` Terraform 项目，用本地 state 只管这个桶。
本项目只有一个桶，用 CLI 更简单。）

然后：

```sh
sed "s/<aws-account-id>/$ACCT/" terraform/backend.hcl.example | grep -v '^#' > terraform/backend.hcl
cd terraform
terraform init -backend-config=backend.hcl
```

此时 S3 里还没有 state 对象；第一次 `apply` 成功后，Terraform 才会在 `key` 指定的位置写入它。

### 4.3 init 之后，Terraform 怎么“记住”用 S3

`init` 把 `backend "s3" {}` 和 `backend.hcl` 合并后，写进 `terraform/.terraform/terraform.tfstate`。
这个文件名容易误导，但里面**只有 backend 配置，没有任何资源**：

```json
{ "backend": { "type": "s3",
               "config": { "bucket": "quote0-busboard-tfstate-…",
                           "key": "quote0-busboard/terraform.tfstate",
                           "region": "ap-southeast-2", "encrypt": true, "use_lockfile": true } } }
```

之后每次 `plan` / `apply` 都先读这张“便条”，再去 S3 读真正的 state。所以 `init` 只需要做一次；
便条丢了（比如删了 `.terraform/`、换了电脑）重新 `init` 就好。

### 4.4 其他情况

| 情况 | 命令 |
|---|---|
| 已经用本地 state 部署过，想搬到 S3 | 加上 `backend "s3" {}` 后 `terraform init -backend-config=backend.hcl -migrate-state`，Terraform 会把本地 state 上传 |
| 修改了 backend 配置，但**不想**搬 state（比如只是改了写法） | `terraform init -backend-config=backend.hcl -reconfigure` |
| 想把 state 换到另一个桶/key | 改 `backend.hcl` 后 `init -migrate-state` |
| 同一个桶放多个项目 | 每个项目用不同的 `key`，例如 `projectA/terraform.tfstate` |

---

## 5. 疑惑二：Terraform 怎样一直拿到 S3 和 AWS 的访问权限

### 5.1 核心事实：Terraform 不保存凭据

`.terraform/`、state、`backend.hcl`、`.tf` 文件里**都没有 AWS 凭据**。每次运行 `terraform`，
它都在当时的环境里重新找凭据。“一直能访问”的前提是：**你每次运行时，环境里都有有效凭据**。

### 5.2 两个客户端，同一套找凭据的规则

一次 `plan` 里其实有两个独立的 AWS 客户端：

| 客户端 | 属于 | 访问什么 | 在哪配置 |
|---|---|---|---|
| S3 backend | Terraform 本体 | state 桶 | `backend.hcl` |
| AWS provider | `hashicorp/aws` 插件 | Lambda、IAM、Scheduler、Logs… | `main.tf` 的 `provider "aws"` 块 |

两者都用 AWS 官方 SDK 的**默认凭据链**，按顺序找，找到第一个就用：

1. 环境变量 `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`（+ 临时凭据的 `AWS_SESSION_TOKEN`）
2. `AWS_PROFILE` 指定的 profile（`~/.aws/credentials`、`~/.aws/config`），没指定就用 `default`
3. SSO 登录缓存（`aws sso login` 之后）
4. Web Identity / OIDC（GitHub Actions 等 CI 用）
5. 运行在 AWS 上时的机器角色（EC2 / ECS / CodeBuild）

本项目目前用的是第 1 种：`set -a; . ./.env; set +a` 把 `.env` 里的密钥导进环境变量。

两个客户端默认拿到同一套凭据。需要时也可以分开，例如 backend 用一个专门读写 state 的角色：

```hcl
# backend.hcl
assume_role = { role_arn = "arn:aws:iam::<ID>:role/terraform-state" }
# 或 profile = "state-admin"
```

### 5.3 推荐的凭据方式

| 场景 | 做法 |
|---|---|
| 自己电脑，长期用 | `aws configure --profile quote0`（填 IAM 用户的密钥），然后每次 `export AWS_PROFILE=quote0` |
| 公司/组织账户 | `aws configure sso` 一次，之后 `aws sso login --profile quote0`，凭据几小时后自动过期 |
| CI（GitHub Actions） | OIDC 让 GitHub 临时扮演一个 IAM 角色，仓库里不存任何长期密钥 |
| 本项目当前 | `.env` 里的 root 密钥 → **应尽快换成 IAM 用户或 SSO**（见 HANDBOOK 5.1） |

运行前先确认身份：

```sh
aws sts get-caller-identity     # 看 Account 和 Arn 是不是你以为的那个
```

### 5.4 最少需要哪些权限

只读写 state（S3 backend）：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::quote0-busboard-tfstate-<ID>" },
    { "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::quote0-busboard-tfstate-<ID>/quote0-busboard/terraform.tfstate" },
    { "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::quote0-busboard-tfstate-<ID>/quote0-busboard/terraform.tfstate.tflock" }
  ]
}
```

管理资源（AWS provider）还需要：Lambda、IAM（含 `iam:PassRole`）、EventBridge Scheduler、
CloudWatch Logs、`sts:GetCallerIdentity`。只做 `plan` 的话，这些服务的只读权限就够。

### 5.5 权限相关报错

| 报错 | 原因 |
|---|---|
| `No valid credential sources found` | 环境里没有凭据：没导入 `.env`、没设 `AWS_PROFILE`、SSO 过期 |
| `InvalidClientTokenId` / `SignatureDoesNotMatch` | 密钥错了，或者环境里残留了别的 `AWS_*` 变量覆盖了（`env \| grep AWS_` 查） |
| `ExpiredToken` | 临时凭据 / SSO 过期，重新登录 |
| `AccessDenied` 读 state | 凭据属于别的账户，或缺上面 5.4 的 S3 权限 |
| `Error acquiring the state lock` | 有人（或上次中断的你）正持有锁，见 9 节 |

---

## 6. 疑惑三：初始化到底做了什么，换电脑怎么重新初始化

### 6.1 `terraform init` 做的三件事

```
terraform init -backend-config=backend.hcl
  │
  ├─ 1. 初始化 backend
  │     合并 backend "s3" {} + backend.hcl
  │     用当前凭据连一下 S3，确认桶可访问
  │     写入 .terraform/terraform.tfstate（“便条”）
  │
  ├─ 2. 安装 provider
  │     读 .terraform.lock.hcl，下载完全相同版本的 hashicorp/aws（5.100.0）
  │     校验文件哈希，放进 .terraform/providers/
  │
  └─ 3. 安装 module（本项目没有）
```

`init` **不会**创建或修改任何 AWS 资源，也不会改动 state（`-migrate-state` 除外），随时可以放心重跑。

### 6.2 什么时候需要（重新）init

| 情况 | 命令 |
|---|---|
| 第一次 / 换电脑 / 删了 `.terraform/` | `terraform init -backend-config=backend.hcl` |
| 改了 backend 配置 | 加 `-reconfigure`（不搬 state）或 `-migrate-state`（搬 state） |
| 想升级 provider 版本 | `terraform init -upgrade`，然后提交更新后的 `.terraform.lock.hcl` |
| 在 macOS 上 init 后，CI 在 Linux 上报 lock 文件缺平台哈希 | `terraform providers lock -platform=linux_amd64 -platform=darwin_arm64` |

忘了 init 时，`plan` 会直接报 `Backend initialization required, please run "terraform init"`。

### 6.3 换电脑：完整清单

新电脑上什么都没有也没关系，state 在 S3 上。需要准备的只有：

| 需要 | 从哪来 |
|---|---|
| 代码 | `git clone` |
| Terraform ≥ 1.10、AWS CLI、Python 3 | 安装（macOS：`brew install terraform awscli`） |
| AWS 凭据 | 见 5.3 |
| `terraform/backend.hcl` | 按 4.2 从模板生成（只是桶名，不是秘密） |
| `terraform/secrets.auto.tfvars` | 三个密钥变量，从你的密码管理器里取（见 HANDBOOK 5.4） |
| `build/lambda.zip` | `./scripts/package_lambda.sh`（`plan` 要计算它的哈希，没有会报错） |

步骤：

```sh
git clone https://github.com/LYJSPEEDX/quote0-bus-dashboard.git && cd quote0-bus-dashboard
export AWS_PROFILE=quote0                    # 或其他方式准备好凭据
aws sts get-caller-identity                  # 确认账户

cp terraform/backend.hcl.example terraform/backend.hcl        # 填入账户 ID
# 创建 terraform/secrets.auto.tfvars，chmod 600

./scripts/package_lambda.sh
cd terraform
terraform init -backend-config=backend.hcl
terraform plan
```

**验收标准：`plan` 显示 `No changes.`** 说明新电脑的代码、密钥变量、打包结果都和线上完全一致，
并且接上了同一份 state。

如果 `plan` 显示有改动，逐项看是哪里不一致：

- `source_code_hash` 变了 → 本机代码/依赖和线上不同（打包是可复现的，同样的代码一定得到同样的哈希）
- `environment` 变了 → `secrets.auto.tfvars` 里的值或覆盖的变量和线上不同
- 其他属性变了 → 有人在控制台手动改过（drift），或者代码没 `git pull` 到最新

---

## 7. 增量更新：改了东西之后发生什么

### 7.1 plan 的三方比对

```
      代码（期望）              state（上次记录）              AWS（实际）
   .tf + 变量 + zip 哈希   ◀──比对──▶  S3 上的 JSON  ◀──刷新──▶  provider 调 Get* API 查询
                    └───────────────── 差异 = plan ─────────────────┘
```

1. **刷新**：provider 用只读 API（`GetFunction`、`GetRole`、`GetSchedule`…）查每个资源的真实状态。
2. **比对**：代码里算出来的期望值 vs 真实值。
3. **输出计划**：只列出有差异的资源和属性。

### 7.2 读懂 plan 的符号

| 符号 | 含义 | 本项目中的例子 |
|---|---|---|
| `+ create` | 新建 | 第一次部署的 8 个资源 |
| `~ update in-place` | 原地修改，资源不中断 | 改代码、改环境变量、改内存 |
| `-/+ replace` | 先删后建（或反过来），资源会被重建 | 改 `function_name`（标注 `forces replacement`） |
| `- destroy` | 删除 | 从代码里删掉一个资源 |
| `<= read` | 读取 data source | `aws_caller_identity` |

**每次 apply 前都看最后一行 `Plan: X to add, Y to change, Z to destroy`**，出现意料之外的
`destroy` 或 `replace` 就停下来检查。

### 7.3 各种改动对应什么

| 你改了什么 | 需要打包？ | plan 里看到 | provider 调用 |
|---|---|---|---|
| `src/app.py` | ✅ | `aws_lambda_function.board` 的 `source_code_hash` | `UpdateFunctionCode` |
| `requirements.txt` | ✅ | 同上 | `UpdateFunctionCode` |
| `locations`、时段、`refresh_mode` 等变量 | ❌ | `environment.variables` | `UpdateFunctionConfiguration` |
| 密钥轮换 | ❌ | `environment`（显示为 sensitive） | `UpdateFunctionConfiguration` |
| `memory_size`、`timeout` | ❌ | 对应属性 | `UpdateFunctionConfiguration` |
| 定时器表达式 | ❌ | `aws_scheduler_schedule.refresh` | `UpdateSchedule` |
| 日志保留天数 | ❌ | `aws_cloudwatch_log_group.lambda` | `PutRetentionPolicy` |

代码变化能被发现，是因为 `main.tf` 里 `source_code_hash = filebase64sha256("build/lambda.zip")`：
打包脚本是可复现的（固定文件顺序、时间戳、权限，不生成 `.pyc`），所以**只有代码或依赖真的变了，
哈希才会变**。只改了 `app.py` 但没重新打包，Terraform 是看不到的。

### 7.4 标准流程

```sh
# 0. 准备：凭据、最新代码
set -a; . ./.env; set +a          # 或 export AWS_PROFILE=...
git pull

# 1. 改代码 → 测试 → 打包（只改变量可以跳过这一步）
.venv/bin/python -m unittest discover -s tests
./scripts/package_lambda.sh

# 2. 生成计划并保存
cd terraform
terraform plan -out=tfplan

# 3. 看清楚后，严格执行这份计划
terraform apply tfplan
```

用 `-out=tfplan` + `apply tfplan` 的好处：apply 执行的**一定**是你看过的那份计划；
如果中间 state 被别人改过，apply 会拒绝执行，而不是悄悄做别的事。

### 7.5 控制台里手动改过（drift）

有人在 AWS 控制台改了 Lambda 的环境变量：

- `terraform plan` 会显示把它**改回**代码里的值。
- 只想看看有哪些 drift、不改资源：`terraform plan -refresh-only`；确认要接受控制台的改动、只更新 state：`terraform apply -refresh-only`。
- 正确做法是：以代码为准。想要控制台里的那个值，就把它写进 `.tf` 或 `tfvars`。

---

## 8. 命令速查

| 命令 | 作用 | 改 AWS？ | 改 state？ |
|---|---|---|---|
| `terraform init -backend-config=backend.hcl` | 连接 S3 state、下载 provider | ❌ | ❌ |
| `terraform fmt` / `validate` | 格式化 / 语法和变量校验 | ❌ | ❌ |
| `terraform plan -out=tfplan` | 生成并保存计划 | ❌ | ❌ |
| `terraform apply tfplan` | 执行计划 | ✅ | ✅ |
| `terraform output` | 查看输出值 | ❌ | ❌ |
| `terraform state list` | 列出 state 里的资源 | ❌ | ❌ |
| `terraform state show aws_lambda_function.board` | 看某个资源在 state 里的记录（会显示密钥，注意终端） | ❌ | ❌ |
| `terraform plan -refresh-only` | 只看 drift | ❌ | ❌ |
| `terraform import <地址> <ID>` | 把已存在的 AWS 资源登记进 state | ❌ | ✅ |
| `terraform force-unlock <LOCK_ID>` | 强制释放残留的锁 | ❌ | ❌ |
| `terraform destroy` | 删除所有受管资源 | ✅ | ✅ |

---

## 9. 常见问题

**Q：state 在 S3，本机要不要备份什么？**
不用。本机的 `.terraform/` 随时能用 `init` 重建。S3 开了版本控制，历史 state 都在；真正要自己备份的
只有密钥（`.env` / `secrets.auto.tfvars` 的内容）。

**Q：两个人同时 apply 会怎样？**
后来的那个会在“上锁”一步失败：`Error acquiring the state lock`，并显示锁的持有者和开始时间。
等对方结束即可。

**Q：apply 中途断网/被杀，锁一直没释放？**
确认没有别人在跑之后：`terraform force-unlock <报错里的 LOCK_ID>`。

**Q：state 被写坏了？**
在 S3 控制台打开这个对象的 “Versions”，下载上一个正常版本覆盖回去；或
`aws s3api list-object-versions --bucket <桶> --prefix quote0-busboard/` 查版本后用 `get-object --version-id` 取回。

**Q：state 彻底丢了？**
AWS 上的资源不受影响，照常运行。用 `terraform import` 把 8 个资源逐个登记回来，例如：
```sh
terraform import aws_lambda_function.board quote0-busboard
terraform import aws_iam_role.lambda quote0-busboard-lambda-role
terraform import aws_scheduler_schedule.refresh default/quote0-busboard-refresh
```
import 完 `plan` 应该是 `No changes`（或只有很小的差异）。

**Q：为什么 plan 需要 `build/lambda.zip` 和密钥变量，我只是想看看？**
`plan` 要算出“期望状态”，期望状态里包括代码包的哈希和环境变量的值，所以两者缺一不可。

**Q：`terraform destroy` 会删 state 桶吗？**
不会。桶不归这份 Terraform 管，需要时手动删（见 HANDBOOK 第 10 节）。

**Q：我能不能跳过打包，只改变量就部署？**
可以，但 `build/lambda.zip` 必须存在，而且要和线上是同一份代码，否则 plan 会顺带把代码也更新了。
打包是可复现的，从同一个 git 提交打出来的包哈希一致，所以“先打包再 plan”永远是安全的。
