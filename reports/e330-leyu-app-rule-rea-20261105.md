# E330 乐鱼App官方计分口径定向分析

更新时间2026-11-05 13:58 UTC。状态：研究中。用户要求使用REA分析乐鱼App；目标找出滚球大小球、半场大小球是否包含推荐前进球的官方依据。责任reports及必要的collector协议映射/API结算；前置已核验APK、九DEX来源、E247/E272规则入口负面证据、E319录制流量范围。现有缺口为非零基线推荐的score_basis仍unknown，不能据此改收益。

设计：只在Docker使用固定现有REA 6.7.0及现有JADX 0.7.1/JDK，外部工具目录只读挂载，不修改全局配置或安装依赖。先按已核验DEX分片有界分析精确规则界面、调用及返回内容，避免完整合并包OOM；仅有名称/URL不算官方计分证据。对实际源码/规则文本保留摘要和出处、区分静态候选与实际返回；不执行投注，不猜私有端点，不改变授权。MCP binary_session确认工具可见，但不能证明宿主进程Docker隔离，因此执行Docker内CLI，不将普通DEX解析冒称REA。

验收场景：定位实际规则入口、调用链、请求契约、明确期间与比分口径；有规则证据后才新增有来源的映射及正常/半赢半输/不明基线/修订幂等测试，冻结回归后受控部署。失败处理：保留错误及覆盖范围、换有证据的定向入口；结算维持pending，不编造官方规则。E328交接监控与本研究分开记录。

14:12 UTC：Docker现有REA6.7.0可用，JADX0.7.1/494MiB/单worker，未安装依赖。standings.requestData原名不在去编译方法列表，首轮invalid_request1保留；用返回的精确s方法重试0，源码明确launch("getRuleInfo", OBStandingsRuleViewModel$requestData$1)。下一层invokeSuspend有元数据但JADX无法建立唯一反汇编，execution_failure1，不重复相同请求。帮助中心HelpCenterUtilsKt类清单0、b方法反编译0、HelpCenterItemType清单0：确认存在SPORT_RULES菜单和主题配置动态跳转，需要继续核对switch映射。普通DEX索引只是定位候选：h5RuleDesc实际属于邀请奖金实体，已排除，不能误称体育规则。全部分析network none，未改结算或访问投注。

14:15 UTC：HelpCenterUtilsKt.a精确内部类（首次误用$原名invalid_request1保留）REA方法<clinit>返回完整smali、exit0，明确SPORT_RULES映射case4；结合b方法case4→AppTheme.a.g().ru()，确认体育规则实际从主题配置ru()读取。AppTheme定义在classes5，classes3仅引用，初次选classes3 invalid_request1保留；classes5 g反编译0返回ThemeKt.e0((String)g.getValue())，继续追踪工厂及配置。离线898录制请求规则链接字段扫描0，仅第三方监控/域名分发规则，未取得体育规则链接；结果e330-recorded-rule-links.json，不能据此声称App不存在规则。

14:28 UTC：中文默认Z→lazy h→t5.i.invoke→ThemeKt.h→g0的Java/smali均真实REA退出0，g0构造ua.b$c并设置语言ZhCN。ua.b$c的定义经普通DEX class_def定位在classes6；前面按引用所在分片选classes5/8导致invalid_request1，已保留，转精确定义分片继续ru方法。这里引用/定义区别只是静态定位证据，未把失败称作不存在。E329近24小时待处理22项全部原文编辑/删除失效，eligible0；补扫86次/2091条、deferred0，不构成新发言模型验收。

14:33 UTC：ua.b$c本身仅构造方法，ru为继承方法；普通class_def核验其父类ua.e。错误选择ua.b父外部类得到method_not_found1保留，现转精确父类方法。不能将子类不声明误判为配置不存在，仍无官方计分口径正文。

14:40 UTC重要更正：ua.e.ru→rL(2788)→ua.a.a[2788]的完整REA源码已取得，实际常量为“了解规则，秒解各类赛事”，是帮助菜单描述而非URL。前述“规则地址配置调用链”推断不成立，保留原记录并在此纠正；SPORT_RULES菜单存在已证实，但地址尚未定位。数组c方法覆盖2000–2999，静态decompile0、不执行App，不含官方计分规则。继续从帮助中心事件/导航处理而非沿描述文本找URL，结算保持未知不改收益。

14:54 UTC：实际点击处理HelpCenterViewModel.k反编译完整0，case4调用WebViewJumpHelper.m，传入AppTheme.g().ou()与i5.a.G()。REA分别证实ou→rL(2785)即标题“体育规则”，G返回c("",c,"/app/tutorialNew/sportRules")，首次取得实际体育规则H5路径，继续核验c的域名/配置组合。证据ev_9068be33c306ed2e2809105ba8911a36acb5794b0506468cefe73fa767475262、ev_4a0dd04fc8f00fd9c2095729b60da622e1f1ec420395e60a166a304778eac8d6、ev_bba0c9b7deae2271ab0728ea6dd59dde23705219000d56fb1d9182c80e09931a；均为静态证据，尚未读取规则正文。一次误用CLI命名参数返回Unknown flag1，按既有help改为位置参数成功；普通索引首次容器非root读0600失败，未放宽权限，改user0只读核验真实类定义。E331约半程无新帖、六群/租约正常；无样本不称审核通过。
