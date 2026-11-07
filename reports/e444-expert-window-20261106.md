# E444 专家真实三十分钟观察

更新时间：2026-11-06 10:54 UTC。完整时间2026-11-06T10:21:15.808139Z至10:51:18.224562Z，1802.4秒。原监控与独立终审actual0，当前版本摘要actual0；不等于整体功能通过。

## 结果与证据

- 截止10个当前应审版本：1 succeeded、9 unresolved，当前attempt模型/系统双回执齐，无缺任务/缺回执。9个可测完整结论13.753–200.592秒、中位39.456秒，0/9≤3秒；第十项计时缺失单列，不能补零。Hermes HTTP涵盖整个框架和工具，4.686–67.352秒、中位7.167秒。
- 新增推荐0b074a94正文“大1.5/2”，506×40原图阿卡青年对克拉科夫青年、74分钟1:0；系统唯一绑定阿卡U19对克拉科夫维斯瓦U19、全场大1.75，系统价1.87。真实1440/390两场景原图/同attempt双回执/推荐价actual0，人工核验原图球队与比分。其余为“哈哈”、表情包、旧串关“得吃”、收米赛果及对话，无新增推荐不能强行生成。该样本未明示期间，记录工具接受事实，不把原图比分条说成明示全场。
- 本窗跨E435远端collector和E443 API/worker/web发布：5个Telegram坏样本，另43/44两次source unavailable，live计数重置；六群连续健康判定false，不能声称整窗无中断。E435旧租约409及恢复时间已在进度记录。无deferred事件，不把恢复后的健康倒填早期。
- 发布前API/worker旧日志与发布后日志按实际UTC边界拼接，626事件请求全200（225 live、363 reconciliation、20 delete、18未分类）；未见数据库错误。live HTTP p50 1120.1/p95 1607.0ms，reconciliation p50 2425.1/p95 4433.2ms。没有事件HTTP错误不证明任意未来消息不会遗漏。
- E443本窗另纠正之前88326半场歧义误录，保留原文、报价、旧trace/账本并追加审计；当前回执0、撤销与统计排除真实双端可见，不能将其当本窗新推荐。

## 可复核命令与后续

bash .runtime/e209-live/e444-finalize-independent.sh，e444-stitch-window-logs.py，event-phase-audit.py及e445-compact-summary.py e444均在限资源Docker运行，退出0。私有证据e444-{summary,current-versions,review-latency}-final.json、e444-compact-summary.json、e444-api-phases-complete.json、e444-natural-browser-output/live-clicks.json；未公开原群号、凭据或会话。

E445于10:46:17左右启动下一完整1800秒观察，尚未截止。E446配置与图片准备并发代码正隔离回归；首129项155.473秒虽actual0但15模型治理用例因环境缺失skip，不能称通过，补齐后重跑原组。总体三秒、无中断与单队结算规则证据仍未全部完成。
