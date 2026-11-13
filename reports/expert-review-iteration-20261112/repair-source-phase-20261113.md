# 来源阶段消歧候选修复

更新时间：2026-11-13 01:28 UTC。仅修复 UTC [2026-11-12T23:30:00Z, 2026-11-13T00:00:00Z) 已确认缺陷 `5f4f4ad462cffaca92331e9f5c4baf550bc0379e1f534524a419b1cda09965f0`；本轮 analysis_status=not_run。

TLSv1.3、transaction_read_only=on 的既有业务连接重新核对目标有效版本和审核轨迹；授权原图哈希校验并目视确认来源阶段明确。旧源码执行 attempt=1/minimal_repro.py 退出1，断言 source_live_refinement_missing。不能由此证明历史盘口有效或存在已确认可成交推荐遗漏。

责任范围为 API 解析、有限 MCP 与审核技能。ResolveInput 和 MCP 增加可选 source_phase，仅在完整、任务所属候选快照内精确消歧，未知仍保持歧义；不按可售性选择候选，不默认期间。当前阶段变化时不新增绑定。回归覆盖分页未完、同阶段重复、球队不符、停止上报、阶段变化、原生传输与非法值零调用。

冻结验证命令：有资源限制 Docker、只读源码挂载，独立 af-phase-5f4f4ad-net / 临时 PostgreSQL，python -m unittest test_review_source_phase test_mcp_description_budget test_mcp_composed_query test_mcp_review_batch test_mcp_single_round test_reviews。首轮148项因PG未就绪发生4个连接错误，退出1；确认TCP就绪后原组重跑148项全部通过，39.034秒，退出0。git diff --check 退出0。旧线上路由与本次候选仅新增来源阶段字段不同。

原始候选快照隔离复验：无阶段声明保持 ambiguous_fixture；补充已独立核验的来源阶段后唯一解析滚球。私有证据仅在本轮原posted保留清单内，用后删除。合成库存下原图真实Hermes调用返回，12.713273秒、接受0、新字段未使用，严格验收未通过；探针进程退出0不等于业务验收成功。初次探针缺少隔离上下文及原posted外键冲突的失败均保留脱敏类别。会话清理helper未验证成功，随后删除整个本任务Hermes容器，不保留活动请求。未执行生产信号写入、历史重审、下单或部署。

当前状态：代码已写，148项隔离回归通过；原图端到端模型验收未通过，validation_passed=false、repair_status=pending。下一步先核查探针时间映射与模型实际工具轨迹，补齐原图正例及相同阶段双候选负例的严格验收，再按现有空闲门禁串行准备、验收并部署。不得复制旧通过证明或放松20秒、期间、价格门禁。生产仍为原镜像，本轮不声称准确率或多专家20秒达标。

脱敏详细验证文件位于本轮私有目录 validation/：frozen-tests.log、frozen-tests-rerun.log、baseline-repro.log、original-replay.json、original-model-result.json、original-model-diagnostic.json。原始内容不进入Git或此说明。已有未跟踪 .omc/ 保留。
