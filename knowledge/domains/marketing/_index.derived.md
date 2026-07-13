# marketing 域 · 派生层附录

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

本域除基表外还有以下派生表。**同名近义表怎么选，看「选哪张」列**。

| 表名 | 层级 | 一句话 | 选哪张 |
|------|------|--------|--------|
| `tmp_campaign_roi_analysis`（废弃） | 历史遗留 | 某分析师做活动复盘时留下的一次性 ROI 中间表，未再维护 | ⛔ 别用，改用 `mart_channel_daily` |

详情读各表卡片：`tmp_campaign_roi_analysis.md`
