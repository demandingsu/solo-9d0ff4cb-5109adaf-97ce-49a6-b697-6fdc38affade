# 焚烧厂日进料补数服务

焚烧厂补发一段**最多 14 天**的日进料明细。同一天可能存进多个冷档（ZIP），
调取按**整包字节数**计费，因此补数计划要在"能集齐全部日值"的整包组合中
选出**总字节最少**的一组。

## 业务规则

- 冷档：ZIP 内有且仅有一份 CSV，表头固定 `date,revision,feed_kg`
  （日期 / 修订号 / 当天进料公斤数）。
- 登记：包大小、整包 SHA-256、逐日修订与行级校验全部入 SQLite。
- 补数只用**请求开始时已登记**且状态正常的包（按 id 快照，请求期间新登记的不参与）。
- **按日取最高修订**；某日没有任何记录（缺日）或最高修订对应多个值（异值）→ 422 报错。
- 以总字节最少为原则选整包组合（天数 ≤ 14，子集 DP 精确求解；
  平局取包数更少、id 更小的组合，结果确定）。
- 只从入选包取数，逐日注明来源包与修订号。
- 入选包**缺失或 SHA-256 校验失败**→ 剔除该包，按本次已确定的修订**重选**
  （不重定修订、不回落旧修订）。
- 剔除后无任何组合能全覆盖 → **409 整次退回**，不拿旧修订补齐。

## 启动（Docker Compose）

```bash
docker compose up --build
# 服务: http://localhost:8000  Swagger UI: http://localhost:8000/docs
```

数据（SQLite + 冷档文件）持久化在命名卷 `app_data`（容器内 `/data`）。

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --reload
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/packages` | 上传 ZIP 冷档并登记（multipart 字段名 `file`） |
| GET  | `/packages` | 列出已登记冷档（含 status: registered/missing/corrupt） |
| POST | `/backfill` | 补数计划，入参 `{"start_date","end_date"}`，跨度 ≤ 14 天 |
| GET  | `/health` | 健康检查 |

`/backfill` 响应：逐日 `date / revision / feed_kg / source_package_id / source_package_name`，
入选包清单、`total_bytes`（计费量）、`reselections`（本次被剔除重选的包及原因）。

错误：缺日或最高修订异值 → 422（含 `missing_days` / `conflicts` 明细）；
无法全覆盖 → 409（含 `uncoverable_days` 与被剔除包清单）；区间 > 14 天 → 422。

## 从 /docs 试跑

### 1. 造样例冷档

```bash
python3 scripts/make_samples.py samples
# week1_rev1.zip        11-01..11-07 rev1   ~1.7KB
# week2_rev1.zip        11-05..11-11 rev1   ~1.7KB（与 week1 交叠 3 天且同值）
# fix_1106_1107_rev2.zip 11-06/11-07 rev2    168B（更高修订）
# cheap_1101_1104_rev1.zip 11-01..11-04 rev1 180B（字节最少）
```

### 2. 交叠覆盖

1. 打开 http://localhost:8000/docs → `POST /packages` → Try it out，
   依次上传 4 个 ZIP（各返回包 id、字节数、SHA-256）。
2. `POST /backfill` → `{"start_date": "2026-11-01", "end_date": "2026-11-11"}`。
3. 期望：入选 `cheap + week2 + fix`（total_bytes = 2048），而不是更贵的 week1；
   11-06/11-07 取修订 2 且来源为 fix 包；逐日均有来源包与修订号。

### 3. 坏包重选

```bash
# 篡改已登记的 cheap 包（id=4），模拟冷档损坏
docker compose exec web sh -c 'echo tampered > /data/packages/pkg_000004.zip'
```

再次执行同一 `/backfill`：响应 `reselections` 记录
`cheap_1101_1104_rev1.zip / checksum_mismatch`，组合自动换成
`week1 + week2 + fix`（total_bytes = 3565），各日修订号不变；
`GET /packages` 可见该包状态已置为 `corrupt`。

把包文件删掉再试（`rm /data/packages/pkg_000004.zip`）则记 `missing` 重选。
若某日的最高修订只存在于坏包中（如只有 rev2 的包损坏），重选后无人能供应该日
→ 409 整次退回，**不会**回落到 rev1 的旧值。

## 测试

```bash
pip install pytest httpx
python3 -m pytest tests/ -q     # 14 个用例：交叠选包/坏包重选/409/422/登记校验
```

## 设计说明

- **快照语义**：`/backfill` 开始时取 `MAX(id)`，只用 `id ≤ 快照` 且
  `status='registered'` 的包；请求期间新登记的包不参与本次。
- **修订一经确定不再变更**：目标修订由登记目录（SQLite 行）确定；
  之后的剔除重选只换包、不换修订，缺了就是 409。
- **调取信任目录**：取数时只对入选包做"存在 + SHA-256"校验，不重新解析 CSV；
  逐日数值以登记目录为准（登记时已做严格校验）。
- **坏包状态落库**：missing/corrupt 写入 `packages.status`，后续请求直接跳过。
