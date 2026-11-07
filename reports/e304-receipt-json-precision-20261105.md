# E304 更正回执的数据库原值保留

更新时间：2026-11-05 09:26 UTC。状态：设计已记录。

目标：避免E301 Python JSON往返把原PG numeric浮点尾差规整；责任services/api、tests、infra/remote。依赖E301严格审计已发现7份新回执claim字段约1e-11ms尾差、59原始payload逐项SHA不变。该差异不改变页面或结算，不应夸大为业务耗时丢失。设计：正常拒绝纠正时在同事务/trace锁下用数据库原payload合并新的确定性汇总，原数值不经Python编码；既有v1纠正的无拒绝回执，仅原始指向属于同job/attempt且除工具拒绝/content/纠正元信息外业务证据严格相同，且确有numeric差异时，追加透明precision更正链（不改已有trace）。最新完成任务/当前消息、无活跃审核及原completion检查继续。重复执行no-op；其它未知算法/跨任务根源拒绝。原推荐、报价、结算、模型和任务状态不变。

验收：隔离PG真实长numeric（不得只用3位小数float）正常纠正逐字段JSONB精确一致；v1精度修复只追加一次、原两个trace保留、审计、并发、非法来源/当前任务检查、事务失败回滚。定向和完整冻结通过后，只读预览7明确目标，按证据执行；保护原始及v1两代trace、推荐/台账，双端浏览器逐份核对。失败保留原数据与退出码、不改断言。E303自然监控继续，维护不重启服务。

## 候选与定向通过（2026-11-05 09:30 UTC）

9733007冻结，32定向23.260秒actual exit0。新增真实PG长numeric正常纠正原值相等、v1精度修复保留前两代trace/追加一次/审计、并发一次、伪造来源及业务差异拒绝四场景；原8纠正场景继续通过。新trace在同事务trace序列锁内以原JSONB合并确定性字段，失败整体回滚。限定v1同job/attempt、除content/拒绝/纠正元信息与claim尾差外原值完全相同，尾差绝对值须小于0.000001ms，不接受任意耗时改动。完整冻结套件启动，期间不修改候选源码；未对生产执行本修复。

## 完整冻结与有界维护（2026-11-05 09:43 UTC）

97330077完整1387项675.235秒actual exit0、0失败错误跳过/源码true，报告SHA7eb240afe72b0536fa687a7a0ef5bc797067c06f6e711a47004106ed68df61c1（.runtime/e304-candidate/.runtime/test-evidence/2919580/backend-tests.json）。维护镜像sha256:9517bf2f37460fd6791f382e0e863d2e8318bb9a3f10cd73c0a401ea142f5eb1，完整源码标签及89文件精确核验exit0。首次构建标签为短号，第二次改完整号并重核验；无运行服务切换。既定7目标只读preview实际exit0，全部would_correct/receipt-jsonb-precision-v1、均0拒绝移除；显式apply正在逐事务执行。前两代所有system回执SHA和全推荐/台账快照已保存，不能在追加未完成时称最终验收通过。E305完整套件按E304 actual0后顺序启动，候选不修改。

## 实际维护及精确验收（2026-11-05 09:50 UTC）

7份apply实际exit0，重新apply全部not_needed实际exit0；同job/attempt原JSONB claim/event耗时及其它业务字段逐项严格相等，独立只读审计7份新精确回执/累计14维护审计actual exit0，证据e304-audit-read.json，不再使用E301诊断中的错误总标签。7条透明precision更正均指向v1及最原始trace；原15份system回执（包含其它历史attempt）SHA逐项不变，追加后22份。283原推荐报价/411旧ledger保持，期间自然新增1至284。14个1440/390真实浏览器场景actual0，原链关联、说明、推荐数和同attempt全部通过。保护证据e304-receipt-and-browser-protection.json、e304-ledger-protection.json及e304-correction-output。无服务重启、无模型重跑、无结算或投注权限改动。此工作包精确保留及历史页面验收通过；整体专家延迟和E303锁超时仍未通过，不混作全部交付。
