# 乐鱼正式赛果协议：已确认路由与缺少的响应证据

更新时间：2026-10-27 17:18 UTC。本轮只读研究当前项目内既有授权协议、v1 参考与已保存的 Android 静态证据，并通过运行中采集器的受保护配置读取现有业务会话。没有登录、下注、续期、切换代理、修改业务配置、写入正式赛果或访问项目外目录。现有采集工作器和乐鱼客户端没有修改。

## 已核实的协议位置

既有 REA 方法清单的证据 ID 为 ev_e2b3204de04d17ad2e3cf504e304a9c5ad169368432af1ac241c0aec5fe93413，来源是已核验发行签名的 Android 2.0.2 修复包。方法清单本身只证明方法存在；以下路由来自项目内既有 DEX 注解，调用参数来自本次只读 DEX 指令与寄存器复核，不能把静态观察写成成功业务响应。

| 能力 | 精确方法与路由 | 已确认边界 |
| --- | --- | --- |
| 普通体育赛果 | getMatchResultPB，POST /yewu11/v1/m/matcheResultPB | matche 是原始拼写，不改为 match；返回 OBBaseEntity<String> |
| 比赛详情 | getMatchDetail，GET /yewu11/v1/m/matchDetail/getMatchDetailPB | 静态路由，未在本轮调用；不能直接当正式结果确认 |
| 冠军赛果 | getChampionMatchResultPB，POST /yewu11/api/result/championMatchResultPB | 与普通足球结果分开，未调用 |
| 虚拟体育赛果 | getVRMatchResultPB，POST /yewu11/api/result/getVRResultListPB | 与真实赛事分开，未调用 |
| 虚拟比分 | getMatchSorce，GET /yewu11/v1/w/virtual/getMatchSorce | 这是 virtual 路由，不能用于真实足球 |

调用链是 SportResultOBViewModel 的 getSportResults 协程，经 OBRepository.getMatchResultPB、OBRepositoryImpl、OBRemoteDataSource.u0 和 OBApi.getMatchResultPB。结果 mapper 对压缩字符串解码后使用 ArrayList<OBEvent> 的 Gson TypeToken。不是根据方法名随意构造 JSON 对象。

本次复核材料的 SHA-256：

| 项目内材料 | SHA-256 |
| --- | --- |
| Android classes7 DEX | 51f8eed84db0ebe71429bcd0e95ea95a79a0d0df73b67492f337b2a5cdfe784f |
| Android classes8 DEX | 1d4d6c187526798481915e2e31130522b18328693843203f432e13f0c7d7c3fd |
| 已有 API 注解清单 | 7677166231dad59b2f628c069b37ed3713a58910bfda4a2a1ab436fcd8843dc9 |
| 当前项目内 v1 实时客户端参考 | 00327c4d1f125d3ecb1153807ebbc9402fe4e692bf29f8513cd79decc960a3a3 |

原始材料与受控脚本保存在忽略的项目内分析缓存，权限为私有，不复制会话和签名值到文档或源码。

## 请求字段与一次已修正的分析错误

OBMatchResultListRequest 的构造器是 String、int、int，分别进入 euid、sort、type；device 默认 v2_h5_st，category 默认 1，可由调用者覆盖。OBRemoteDataSource.u0 的参数依次是 sportId、sort、md、category，构造 type 使用常量 28。

| 请求字段 | 静态确认的本次足球查询值 | 依据 |
| --- | --- | --- |
| euid | 字符串 1 | 赛果页当前体育项目默认 1，参数名 sportId |
| sort | 数字 1 | 页调用时传入固定排序值 1 |
| type | 数字 28 | u0 构造结果请求时设置常量 28 |
| category | 数字 1 | COMPLETED 视图传入 1，另一视图传入 0 |
| device | v2_h5_st | 请求对象构造默认值 |
| md | 可为空；日期辅助方法生成 yyyy-MM-dd'T'HH:mm:ss | 已观察日期对象加 43,200,000 毫秒后格式化；本轮日期探针使用对应日 12:00 |

cps、cuid、tid 是可空字段；没有证据要求本次凭空填写。构造器、u0、界面调用对应的 DEX 偏移分别为 2902848、3181452、5038032；日期格式辅助方法偏移为 7084220。

首次把构造调用寄存器实参顺序误读为 sort=28/type=1，前两次只读探针因此请求形状不正确。逐指令核对 invoke-direct 的寄存器顺序后已经纠正。两次失败保留在下表，不能用它们证明正确接口不可用；也不因后续成功空响应删去这项分析错误。

## 真实只读测量

所有时间为 UTC。每个探针至多一次数据请求，复用配置版本 7 的原有业务会话、采集头与当前 HTTPS 中转，不使用登录代理、不跟随重定向、不自动重试，不修改数据库。响应读取有上限；只输出类别、字段名和计数。退出码 0 只证明诊断脚本正常输出，业务状态必须读本表的响应列。

| UTC 起始时间 | 请求差异 | 响应或错误 | 数据请求耗时 | 脚本退出码 |
| --- | --- | --- | ---: | ---: |
| 17:11:26.439 | 错误实参 sort28/type1；昨日 12:00 | 读取超时；未得到 HTTP 响应状态 | 12,050.4 毫秒 | 0 |
| 17:12:56.750 | 错误实参 sort28/type1；空 md | 读取超时；未得到 HTTP 响应状态 | 20,051.4 毫秒 | 0 |
| 17:14:46.769 | 对照：既有 getOriginalDataPB 赛程 GET | 成功，字典载荷，2,512 场 | 2,948.5 毫秒 | 0 |
| 17:15:45.372 | 已纠正 sort1/type28/category1；空 md | HTTP200、code0000000、解码为空数组，103 响应字节 | 995.6 毫秒 | 0 |
| 17:16:00.567 | 正确实参；2026-10-26T12:00:00 | 读取超时；未得到 HTTP 响应状态 | 20,070.3 毫秒 | 0 |
| 17:17:13.756 | 正确实参；2026-10-27T12:00:00 | 读取超时；未得到 HTTP 响应状态 | 35,059.9 毫秒 | 0 |

成功空响应的 envelope ts 为 1793121347524；这是响应封装时间，不会冒充单场结果修订时间。只有一条成功空数组无法核验任何具体足球终场、半场、加时、取消或更正字段。对照赛程成功也不能推导结果路径的区域、日期、请求体、上游查询耗时或中转行为完全一致；超时本身不是权限拒绝、令牌失效、限流或代理质量的证据。

命令使用已运行采集容器，不运行宿主机应用代码：

    docker exec -i ai-football-v2-collector-1 python - < .runtime/leyu-results-20261027/probe_results.py
    docker exec -i ai-football-v2-collector-1 python - < .runtime/leyu-results-20261027/probe_results_default.py
    docker exec -i ai-football-v2-collector-1 python - < .runtime/leyu-results-20261027/probe_control_snapshot.py
    docker exec -i ai-football-v2-collector-1 python - < .runtime/leyu-results-20261027/probe_results_corrected.py
    docker exec -i ai-football-v2-collector-1 python - < .runtime/leyu-results-20261027/probe_results_corrected_date.py
    docker exec -i ai-football-v2-collector-1 python - < .runtime/leyu-results-20261027/probe_results_corrected_today.py

静态字段核对使用仅本项目只读挂载、network=none、0.25 CPU、256MiB 的既有验证镜像。首次默认非 root 镜像读取私有材料退出 1；改为 root 的只读分析容器后退出 0，没有放宽文件权限。原始文本工具的两个脚本语法错误未执行，修正后 DEX 脚本正常完成。未安装或执行目标 APK。

## 返回字段的已知与未知

OBEvent 已观察到 mid 对应 matchId、csid 对应 sportId、mgt 对应 startTime；msc 为字符串数组，ms 为可空整数，mst 为字符串，med 与 serverTime 为可空长整数。finishedScore、homeScore、awayScore 也是可空字符串。以上类型和 SerializedName 映射可由静态材料复核，不能据字段英语名直接决定结算含义。

当前缺少的最小协议证据是：

1. 普通足球结果接口的至少一条非空成功请求/响应，保存精确日期、请求内容类型、业务响应码、压缩前后字节摘要及该场 mid；秘密单独私有保存。需要核对 md 实际取值和服务端时区，不继续猜测不同字段组合。
2. 同一场真实足球的常规全场与半场结果，明确 msc 各 S 编码，以及常规时间与加时/点球如何分开；字段存在不能证明 S1 永远是 90 分钟结果。
3. 官方取消、延期、腰斩与作废的非空状态样本及结算适用规则；普通赛果页的 COMPLETED/另一分类不等于直接拥有 void 结论。
4. 单场结果修订的上游时间或稳定版本字段及前后两版样本；envelope ts、接收时钟、开赛时间、比赛结束时间均不能替代修订时间。
5. 当前会话下的成功非空结果确认后，才设计低并发、按推荐关联场次/日期的预算查询与短期更正跟踪，并连接已有 /internal/collector/results。不能全站逐场串行拉取，也不能用无来源 now 造单调版本。

## v1 结束事件为什么不能补成正式确认

项目内 v1 实时客户端已在 C109 分支注明其含义为移除，可能取消或关闭；只有显式 ms3/mmp999 才标结束。v1 的 ScoreStore 以本地保存 C103 比分与结束状态弥补历史接口不返比分，该机制只能证明曾观察到的实时事实。它不能证明常规时间结算规则、正式取消、补赛或赛果修订。因此不把旧缓存重新命名为官方结果确认，不接入收益台账作为 confirmed。

当前 API 已有带来源引用、源时间、证据与版本的赛果导入和重建能力，也会拒绝同时间冲突及陈旧赛果。本轮没有绕开这些门禁，不新增永远返回空值的伪生产采集器，不用合成样本声称上游确认通过。

## 任务证据映射

P29-01 已从泛称缺接口推进到可复核的正式普通赛果路由、字段类型和一次成功空响应；非空终场/半场、取消/加时及订单结算样本仍不足，整个节点不能勾完成。P29-04 的 API 来源、版本及冲突逻辑已有隔离证据，上游修订映射仍未完成。P18-07、P29-03/05/06、R05-01 继续待正式协议和非空响应证据，不能用比分时钟、列表消失或旧本地缓存替代。

本轮没有修改生产采集代码，因此不新增以自己构造的赛果字段为真值的测试。上一存储包的 45 项冻结恢复/配置测试结果独立保留，不能拿来证明赛果协议。本轮后续应以本报告列出的最小成功样本继续，而不是无限提高请求次数、变更身份或伪造最终确认。
