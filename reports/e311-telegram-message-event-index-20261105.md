# E311 原文消息事件查找索引

更新时间：2026-11-05 10:36 UTC。状态：研究中，设计已记录，未改生产数据结构。

目标：修复真实浏览器原文详情503，降低按message_id读取最新原文事件及消息外键检查的扫描成本。责任services/api/migrations、tests；依赖受控远端DDL权限/迁移锁、当前26版本结构、新版本API与实际review-worker一致生效。当前证据：10:33:52.763768 request 12e9cd207bf0d7ada99a5e85ae92925f、backend3403453，telegram_message_detail首fetchrow驱动整体5秒耗尽（prepare124.587ms/execute4875.527ms）。只读目录检查事件表估计39607活行5783死行、无message_id索引；实际EXPLAIN为Limit→Sort→全表Seq Scan，估计cost5928.73。原图仍可授权读取并非媒体缺失。

设计：新增有序迁移027，B-tree(message_id,received_at DESC,id DESC)。查询保持原权限/原最新事件排序及完整content，不截断、不缓存旧正文、不提高5秒超时。索引同时覆盖每消息事件过滤和排序，保留历史多事件及NULL message_id。新库常规CREATE INDEX IF NOT EXISTS；生产先在受限maintenance_work_mem/无并行构建、迁移advisory锁下CREATE INDEX CONCURRENTLY，再核验精确定义/indisvalid/indisready，随后受控登记迁移并切换API+实际worker，防旧schema进程重启失败。失败保留无效索引诊断、不得误登记；不自动删业务行或撤销已登记迁移。迁移与消费者阶段顺序及回滚兼容必须实际验证后执行。

实现动作：冻结候选新增迁移、迁移清单契约、真实隔离PG多版本/同时间id排序/NULL无归属/大量无关事件计划验证；完整套件与API镜像逐文件核验；运行前只读身份/权限/定义核验，受控DDL与版本登记日志；自然空闲门发布API和实际worker并保存原信号报价及结算哈希。验收：实际全量原文不变、重复注册幂等、真实计划index无Sort/Seq Scan、连续桌面手机点击正确正文/图片/回执、越权仍拒绝；实时采集审核不丢，独立30分钟后才能判稳定。失败归档，改后重跑原失败场景。

并行未解决：桥接3次downstream_reply断开尚无请求来源证据，BTTS真实玩法契约缺口、旧两条滚球计分口径未知、3秒时延未达；此索引不宣称解决所有问题。
