# PostgreSQL 报告存储

后端配置 `DATABASE_URL` 后会在启动时自动创建报告表：

- `trip_reports`：保存每次 `POST /api/trip/plan` 的原始请求、完整规划结果、当前选中方案、城市、天数、预算和生成模式。
- `trip_report_revisions`：当 `POST /api/trip/recalculate` 传入 `report_id` 时，保存重算、补景点或重排后的修订记录。

也可以不用 `DATABASE_URL`，改用拆分配置：

```env
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=travel
POSTGRES_USER=travel
POSTGRES_PASSWORD=your-password
```

新增查询接口：

- `GET /api/reports`：获取最近报告列表。
- `GET /api/reports/{report_id}`：获取单个报告详情和修订历史。

生成行程接口返回的 `data.report_id` 是后续重算保存修订的关联 ID。前端或调用方在调用 `POST /api/trip/recalculate` 时把该值作为 `report_id` 传回即可。
