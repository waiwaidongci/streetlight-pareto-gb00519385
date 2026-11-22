# 001 初始表结构

应用首次启动以 SQLAlchemy 模型创建表与唯一约束。现有数据表只补建缺失表，升级到修改过字段的版本需要显式迁移。

- `users` 账号角色和加盐密码摘要。
- `datasets` 不可变记录批次，以名称和 SHA-256 去重；`source` 区分 GIS 灯组名册（roster）与市政热线批次（hotline），`batch_date` 标记来源批次归属日。
- `runs` 记录参数、数据批次、运行租约、分析结果、导出文件及复核决定。定时日报另存 `report_day` 与锁定的 `roster_dataset_id`、`hotline_dataset_id`；来源未齐时为 `waiting`。
- `schedules` 保存定时计划、下一到期时间、首份报告日锚点 `anchor_report_day` 与偏移 `report_offset`，支撑漏发后的逐日补跑。
- `deliveries` 保存站内分发回执，每次运行最多一个回执；`state` 含 delivered、blocked、pending、retrying，`attempts` 记录发布尝试次数。
- `audit_events` 记录登录、数据入库、运行、等待/补挂、复核和报表分发动作。

## 002 定时日报补跑字段

在已存在的 PostgreSQL 库上升级时执行（新库由 `create_all` 直接建好）：

```sql
ALTER TABLE datasets ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'roster';
ALTER TABLE datasets ADD COLUMN batch_date VARCHAR(10);
ALTER TABLE runs ADD COLUMN roster_dataset_id INTEGER REFERENCES datasets(id);
ALTER TABLE runs ADD COLUMN hotline_dataset_id INTEGER REFERENCES datasets(id);
ALTER TABLE runs ADD COLUMN report_day VARCHAR(10);
ALTER TABLE schedules ADD COLUMN anchor_report_day VARCHAR(10);
ALTER TABLE schedules ADD COLUMN report_offset INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deliveries ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deliveries ADD COLUMN updated_at TIMESTAMP;
CREATE INDEX ix_datasets_source ON datasets(source);
CREATE INDEX ix_datasets_batch_date ON datasets(batch_date);
CREATE INDEX ix_runs_report_day ON runs(report_day);
CREATE INDEX ix_deliveries_state ON deliveries(state);
```
