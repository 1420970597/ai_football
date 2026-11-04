# E231 审核与采集事务等待调查

更新时间：2026-11-04 16:38 UTC。状态：研究中，未改业务代码。

目标：定位E229窗口中仍发生的跨接口Timeout和审核租约等待，降低事务占锁与远端往返；责任目录services/api、tests、reports、docs/platform-v2。依赖当前正式窗口不变、远端只读pg_stat_activity/pg_blocking_pids以及有限表名分类，不读取查询参数或凭据。

当前证据：37次/188.4秒只读采样95条活动，90次ClientRead、2次transactionid等待、2次DataFileRead、1次无等待；最大事务6.167秒/最大查询4.159秒。第12次某未分类语句等待另一ClientRead事务1.505秒；第13次review_lease_renewal等待前者0.783秒，尚不足以定位占锁业务。另一约140秒CPU差分API限流52期/3.747秒、隧道82期/5.960秒；仅累计差分，不能把它单独解释成请求5秒超时。完整E229窗口仍需终审。

缺口与设计：现有other_statement分类不够，不应猜测全部是网络或锁。追加37次/5秒间隔只读采样，将job更新/认领/过期、检查点更新与事件成员访问分类，并只提取有限v2_表名和锁语句标识，不输出完整SQL、参数或消息。保留阻塞链与事务时间后再选择单一可复现根因；不放宽5秒command或2秒lock预算、不增加资源、不重启依赖。

实现与验收待证据决定：仅在取得具体事务路径后记录设计、真实临时PG构造旧版失败/候选恢复并验证并发互斥、租约/版本/幂等与权限，再冻结完整测试。未取得复现时不盲改索引/锁/隔离级别。当前仅调查，不能称已修复。

16:41追加调查结束：docker exec -i ai-football-v2-api-1 python - < .runtime/e209-live/e231-waits.py，exit0；37次188.5秒、111条活动，其中110 ClientRead、1无等待、0阻塞，最大事务4.480秒；stderr空。该时段未重现前次阻塞，不改业务锁。保留原第12/13次等待作为未解释问题，下一步在正式窗口终审后关联接口和自然审核时间；不把未重现称为修复。原始受限证据.runtime/e209-live/e229-waits-mid.jsonl及e231-waits-01.jsonl。

17:06追加假设：E230中段239消息接口全200，但其他接口7Timeout，其中6包含asyncpg._get_timeout_impl。读取当前容器固定依赖源码，connection._do_execute在_get_statement完成后扣除整体命令剩余时间；_get_timeout_impl在剩余≤0时直接抛Timeout。因此这批错误至少有准备阶段已耗尽预算的路径，不能一律归因执行语句锁等待。拟独立只读连接做30轮冷/热同形参数查询，临时连接内统计prepare耗时/整体耗时，SQL只输出是否存在且UUID用随机不存在值，无写入/锁/用户资料输出；不修改生产池、预算或TLS。单探针只能检验路径，不足以归因全部线上失败。

17:12探针结束：30轮60次只读原参数查询/独立连接全部成功，冷整体P50/P95 436.659/522.225ms、prepare193.908/241.562ms；热整体241.595/384.215ms、prepare0.011/0.032ms。首个热标签尚未预热，保留其243.977ms准备耗时、不剔除来美化结果；其余同形命中。未重现5秒超时，不能据此只调缓存就宣称修复。docker exec -i运行exit0/stderr空，证据e231-prepare-probe-01.jsonl；该独立连接计时不代表生产池全部查询，后续继续关联真实超时。

17:39被动传输调查设计：正式E232窗口固定时，在当前API与storage-tunnel各采集61次/10秒间隔的/proc/net/snmp、netstat及本容器cpu.stat。只保存自带名称的TCP重传/超时/丢包恢复/CPU限流计数和UTC，不读取连接地址、SQL、消息或凭据，不发网络探针、不改连接/SSH/资源。对齐同时间日志错误以判断是否同窗发生网络恢复或CPU停顿；相关性不是自动因果，缺少证据不改路由/出口或超时。

17:42区分事件循环/DB等待：确认实际API uvicorn两worker、healthz为直接返回常量的async路由，不访问DB。追加600秒、每秒两条本容器localhost持久连接的healthz只读计时，2秒截止/总2req/s，不增加worker/资源、不打远端DB或重启，原无access log设置不改。连接不保证分别覆盖两个worker，不能外推所有进程；若本机无DB路径也慢，再与CPU/TCP和SQL错误对齐，未见证据不盲目线程化全部代码。

17:54被动与本机计时完成：API/隧道各61次17:39:55–17:49:56，两个Docker exec进程exit0/stderr空；API重传/网络超时增量均0，隧道重传9（fast8/probe1）、网络超时增量0。两容器CPU累计限流19.045/27.008秒，但5个真实DB Timeout对应10秒桶均无TCP重传，且17:42:27那一桶API/隧道都0限流；因此这几项不是这5个超时的充分解释。仅10秒桶及累计数，未排除更早请求阶段或远端主机问题，不称根因已找到。

本机无DB健康17:41:54.567855–17:51:54.469185共600样本1200请求全200，P50/P95 3.479/5.402ms、max90.115ms，exit0；两个持久连接未保证覆盖两个worker，仍不能排除其他worker停顿。只保存计时/状态，未读取秘密。汇总docker run --rm --network none --cpus0.1 --memory64m现有API镜像执行e231-probe-summary.py，exit0，证据e231-probe-summary-final.json；原错误时间/脱敏帧在e231-error-moments-mid.json。正式E232窗仍到17:55:06以后才终审。

17:59追加远端预算调查设计：本机TCP/健康尚未归因，使用现有已核验SSH配置、固定旧台账容器名ai-football-postgres，仅docker exec读取该数据库容器自身cgroup cpu.stat/cpu.max/memory.events和CPU/IO/memory pressure，61次/10秒。远端代码全部在该Docker容器中执行，不改预算/服务/目录、不读账号/环境/连接地址/SQL。SSH输出仅固定指标，10秒采样对齐后续错误；若SSH或指标缺失保留失败，不枚举其他容器或提权绕过。不把远端累计限流直接当因果，先确认是否存在及请求时间相关性。

18:15远端采样结束：18:00:27–18:10:28，61样本/SSH与远端探针exit0/stderr空，cpu.max max/CPU配额限流0、memory.events各项增量0。完整CPU/IO/内存pressure累计停顿1.404/18.894/2.553秒，IO avg10最高9.85%，但两个真实DB Timeout对应桶IO仅130/177毫秒，不能单凭累计IO直接解释5秒超时。容器内pressure不能代表全部远端主机或网络。Docker隔离汇总exit0，e231-remote-budget-summary.json。

后续设计：提高pg_stat_activity脱敏采样至600秒/每秒一次、单独只读连接、单查询5秒预算，既有65活动上限，增加UTC以精确对齐错误。relation_names使用明确POSIX空白类（仅诊断脚本，不改业务）；曾怀疑旧双反斜杠错误，复核它是Python正常转义，不能把此项说成旧诊断bug。原5秒采样可能遗漏2秒锁等待。保存固定operation、v2_表名、等待类型、时长、pid和阻塞链，不输出SQL/参数/账号/地址。每秒一次是有界临时诊断，不调整池/资源/查询截止，也不将新探针负载伪装自然流量。生产E234窗口与E235冻结测试继续。

18:23抓到真实失败对应服务端IO：自然结果浏览器登录18:20:23.639213返回503/exit1，API帧admin_login→_create_session:132→execute，SQL是现有INSERT v2_sessions。高频采样同一pid3391432在18:20:18.762729至22.794455连续5样本DataFileRead，query_seconds0.858→4.857、无blockers；随后该连接消失，时间吻合5秒截止。至少此次超时确实包含服务端磁盘读取长等待，不能再说全是客户端准备或本机事件循环，不能外推全部19个错误同因。附近其他连接同时DataFileRead/WALWrite，原始字段仅关系名/时间无参数。

下一步只读检查该PG容器自身memory.current/max/high/swap和宿主可见MemAvailable/Swap、数据卷剩余空间及当前数据库共享缓存/IO计时设置、v2_sessions实际表/索引大小。全部在Docker内、固定对象、有限一轮、不读账户/会话内容，不改缓存配置或重启共享数据库。先区分缓存缺页、磁盘满和宿主内存压力，再决定可在项目边界内验证的修复。此次浏览器失败不盖为通过，后续恢复必须原场景重跑。

18:25只读环境结果：两探针exit0/stderr空。PG容器memory.current约1.42GiB/max与high无限制/swap约7MiB；宿主MemAvailable3883972KiB、盘使用23%，排除该时刻明显内存不足/盘满。宿主/proc/pressure/io full avg10=67.71%/avg60=52.88%/avg300=61.27%，远高于前一探针容器自身IO压力；两者范围不同，不能混称。数据库shared_buffers128MiB、effective_cache_size4GiB（规划估计非实际缓存）、work_mem4MiB、同步提交on、IO计时off。v2_sessions表40960字节/索引57344字节；current_quotes表约610MiB/索引约616MiB。pg_stat_database读块/临时文件为累计，9.22TB历史临时字节与21死锁不能归因本窗口。共享PG还承载旧数据，不按本项目需求擅自重启/修改全局配置或清理数据；继续定位项目当前查询及IO来源，只读结果不能直接承诺解决宿主存储延迟。
