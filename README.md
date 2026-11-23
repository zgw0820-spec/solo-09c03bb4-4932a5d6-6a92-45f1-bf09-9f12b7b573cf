# Shared Helm Values Cleanup & Rollout Rehearsal API

两个能力，同一个 FastAPI 服务，**全程不连集群**：

1. **共享 values 清理**（`POST /api/v1/solve`）：生产仍在用旧版 Helm Chart（v1），
   预发已切到新版（v2），两边共用同一份 values 覆盖文件。本服务找出覆盖文件里那些
   只是重复 Chart 默认值、删掉后**每个版本**渲染结果都保持不变的映射键，返回清理后的
   YAML 和删键路径；其余覆盖原样保留。
2. **换版预演**（`POST /api/v1/rehearse-rollout`）：解决滚动更新时旧 Pod 提前读到
   新版配置的问题。提交现行 Deployment 和同命名空间的 ConfigMap 清单，以及挂载目录里的
   新版 Chart + values，服务用 `helm template` 渲染并产出一份“先建不可变 ConfigMap
   副本、再换 Deployment、可回退”的发布计划，旧配置一个字节都不动。

## 目录结构

```
mounted-charts/                 # 模拟“挂载目录”
├── orders-api/{v1,v2}/         # 清理 API 用的两个版本
└── payment-web/
    ├── v1/ v2/                 # 清理 API 用的两个版本
    └── v3/                     # 换版预演用的新版（含 envFrom / ConfigMap 卷 / 外部引用）
overrides/                      # 两版共用的 values 覆盖（清理对象）
rehearsal-values/               # v3 换版预演用的 values
live-manifests/payment-web/     # “现行”清单：deployment.yaml + configmaps*.yaml
app/                            # FastAPI 服务
├── main.py                     # 路由、请求模型、Swagger 样例
├── rehearsal.py                # 换版预演核心：渲染、引用分析、不可变副本规划
├── solver.py                   # 清理：基线渲染 → 单键筛选 → 组合穷举 → 复核
├── helm.py                     # helm template 封装（固定 release/namespace）
├── normalize.py                # 清单归一化与语义比对
└── values_tools.py             # 叶键枚举、路径表示、删键
tests/                          # pytest 自测（51 个用例）
Dockerfile
docker-compose.yml
requirements.txt
examples/                       # Swagger 样例对应的原始请求体（可直接 curl）
```

## 清理规则与算法

1. **基线渲染**：用固定发布名 `platform-rel` 和命名空间 `platform` 对每个版本执行
   `helm template -f <覆盖文件>`。任一版本基线渲染失败 → 直接报错（HTTP 422）。
2. **叶键枚举**：只枚举映射键的叶路径（标量/列表终止），天然互不包含；
   形如 `image.pullPolicy`，带点的键用引号段表示（如
   `podAnnotations."checksum/legacy"`）。
3. **单键筛选**：逐个删键试渲染，只有单独删除后所有版本仍渲染成功且清单与基线
   语义一致的键才进入候选——避免把 8 个名额浪费在不可能删除的键上。
4. **候选上限 8 个**：可删键超过 8 个时按覆盖文件中的顺序取前 8（响应中
   `candidate_cap_applied=true`）。
5. **组合穷举**：从最大子集开始尝试，找到能同时删除的最多候选键即返回。
   某组合渲染失败不会中断流程，自动跳过并尝试其他组合（`render_failures` 计数）。
6. **最终复核**：用清理后的 YAML 对每个版本重新渲染并与基线比对，全部一致才置
   `verified=true`。

### 比对语义

- 忽略 YAML 注释（解析为对象后注释不存在）
- 忽略文档先后（按 `kind/namespace/name` 排序后比较）
- 忽略映射键顺序（字典按内容比较）
- **列表顺序必须相同**（列表逐元素有序比较）

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# Swagger UI（可直接试两个样例）： http://localhost:8000/docs
# OpenAPI JSON：                    http://localhost:8000/openapi.json
# 健康检查：                        http://localhost:8000/health
```

镜像内安装了 Helm 3，并把 `mounted-charts/`、`overrides/` 以只读卷挂到
`/workspace/...`，与示例请求里的绝对路径一致。

### 本地直跑（无 Docker 时）

```bash
# 依赖 Helm 3 在 PATH 上，Python 依赖见 requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

### `POST /api/v1/solve`

请求体：

| 字段 | 说明 |
| --- | --- |
| `values_override_path` | 共用覆盖 YAML 的绝对路径 |
| `versions[]` | 每个版本的 `label`（唯一）与 `chart_dir`（绝对路径） |
| `release_name` | 固定发布名，默认 `platform-rel` |
| `namespace` | 固定命名空间，默认 `platform` |

响应关键字段：`cleaned_yaml`（删键后的覆盖）、`removed_paths`（可删路径）、
`candidate_paths`（最多 8 个候选）、`kept_paths`（保留的其余叶键）、
`versions[].manifests_match_baseline`、`verified`。

两个可在 Swagger 直接试用的样例（请求体同时保存在 `examples/`）：

- `orders-api`：21 个叶键，**8 个可全部删除**；跨版默认值翻转的
  `podSecurityContext.*` 被正确保留。
- `payment-web`：18 个叶键，其中 10 个单独可删，受 8 键上限约束取前 8；
  v2 独有的 `nodeSelector.disktype`（v1 模板忽略它，看似可删）被正确保留。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/api/v1/solve \
  -H 'Content-Type: application/json' \
  --data @examples/orders-api.request.json | jq .
# 想要 YAML 响应：加 -H 'Accept: application/yaml'
```

错误约定：覆盖文件不存在/无法解析 → 400；基线 `helm template` 失败 → 422（附 helm stderr）。

### `POST /api/v1/rehearse-rollout`（换版预演）

**要解决的问题**：Kubernetes 的 ConfigMap 不做版本化，原地更新会让滚动更新期间还在运行的
旧 Pod 立刻读到新内容。预演的做法是**永不修改现行 ConfigMap**：内容有变化（或全新）的
配置另建一个不可变副本，新 Deployment 只引用副本；内容没变的沿用原名。

请求体：

| 字段 | 说明 |
| --- | --- |
| `current_deployment` | 现行 Deployment 清单：对象、单元素列表或 YAML 字符串 |
| `current_configmaps` | 同命名空间的现行 ConfigMap 清单：对象列表或多文档 YAML 字符串 |
| `chart_dir` | 挂载目录里**新版** Chart 的绝对路径 |
| `values_path` | 新版 values 绝对路径；为 `null` 时只渲染 Chart 默认值 |
| `release_name` / `namespace` | 固定发布名/命名空间，默认 `platform-rel` / `platform` |

处理规则：

1. `helm template` 渲染新版 Chart，**只处理同名、同命名空间且 `spec.selector` 不变**的
   Deployment；名字对不上、命名空间不符或选择器变化 → 422，不给发布清单。
2. 只处理**非可选**的两类引用（可选引用原样保留并在 `skipped_optional_references` 中列出）：
   - 容器（含 initContainers）的 `envFrom[].configMapRef`；`secretRef` 不在范围内；
   - Pod 卷 `volumes[].configMap`。
3. Chart 没有渲染的 ConfigMap（如平台共享配置）视为**外部引用**：原名保留，只校验它确实
   存在于提交的现行清单里。
4. 同名 ConfigMap 的 `data`/`binaryData` 内容与渲染结果一致 → 沿用原名，不建副本。
5. 内容变化或全新的配置 → 生成不可变副本，名字为
   `<原名>-<内容sha256前12位>`（RFC 1123，截断到 63 字符），**只由内容决定，重试不变**。
6. 已存在同名副本时，仅当它 `immutable: true` 且内容完全一致才复用；否则报**名称冲突**。
7. 校验失败一次报全部原因（HTTP 422，`detail` 为原因列表），响应里不含任何发布清单：
   新旧 Deployment 的非可选引用缺 ConfigMap、ConfigMap 卷 `items[].key` 缺键、副本名称冲突、
   选择器/命名空间/名字不符、helm 渲染失败。请求格式或路径问题 → 400。

响应关键字段：`configmaps_to_create`（要创建的不可变副本，含来源与内容哈希注解）、
`new_deployment`（引用已改写的新版 Deployment）、`rollback_deployment`（剥掉
resourceVersion/uid 等集群字段、仍指向旧配置名的回退 Deployment）、
`release_yaml`（按 **ConfigMap → 新版 Deployment → 回退 Deployment** 排序的多文档 YAML）、
`configmap_plans`、`reference_changes`、`skipped_optional_references`。

Swagger 内置两个可直接试跑的样例（请求体同时保存在 `examples/`）：

- `payment-web v2 -> v3 (happy path)`：变更的 Chart ConfigMap 建不可变副本，三处引用
  （initContainer envFrom、容器 envFrom、ConfigMap 卷）改写，外部引用保留，两个可选引用跳过。
- `payment-web v2 -> v3 (same-name conflict)`：副本名已被一个可变 ConfigMap 占用 → 422，
  返回冲突原因且没有发布清单。

curl 示例：

```bash
# 正常换版（200，release_yaml 即按序可 apply 的清单）
curl -s -X POST http://localhost:8000/api/v1/rehearse-rollout \
  -H 'Content-Type: application/json' \
  --data @examples/payment-web.rehearse.request.json | jq .
# 同名冲突（422，detail 是原因列表）
curl -s -X POST http://localhost:8000/api/v1/rehearse-rollout \
  -H 'Content-Type: application/json' \
  --data @examples/payment-web.rehearse.conflict.request.json | jq .
# 想要 YAML 响应：加 -H 'Accept: application/yaml'
```

安全边界：服务只读挂载目录与请求体内的清单，不调用任何 Kubernetes API；回退 Deployment
是现行清单的深拷贝（剥离运行时字段），输入对象本身不会被修改。

## 自测

```bash
PATH=/path/to/helm:$PATH PYTHONPATH=<deps> python3 -m pytest tests/ -q
# 当前实测：51 passed（清理 24 + 换版预演 27）
```

覆盖：归一化比对四条规则、路径工具、两个 Chart 的端到端清理、8 键上限、
跨版陷阱键保留、基线失败报错、删键组合渲染失败自动跳过、API 200/400/422 与
Swagger 样例；换版预演的不可变副本规划与重试稳定性、同名内容沿用、同名副本复用/冲突、
缺配置/缺卷键/选择器变化报错、外部引用保留、可选引用跳过、回退清单可 apply、
文档顺序（ConfigMap → 新版 → 回退）。
