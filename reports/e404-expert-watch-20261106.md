# E404 专家持续观察

更新时间2026-11-06 06:08 UTC。状态：完整观察完成，仍未达到三秒或全窗无故障验收。

05:29:18.797928至05:59:21.393534 UTC共1802.6秒。14个当前应审自然版本均有同attempt模型及系统双回执：1 succeeded、13 unresolved，没有漏回执。六场新推荐真实接受六项，桌面1440及手机390真实点击各一次，卡片、当前回执、原文和期间方向一致；无JS异常或横向溢出。两次状态采样异常仍保留，不能写全窗Telegram健康。旧消息修订与自然新帖分开统计。

队列中位2.196秒、最长300.101秒；15次模型生成最短6.016/中位8.213/最长331.563秒；14版本端到端最短13.297/中位16.182/最长370.838秒，0项达到3秒。输入准备均不超过0.984秒，领取中位1.599秒。回顾图片仍触发长检索，模型自由说明不能代替真实绑定证据。

672次Telegram事件请求全200，但报价写入出现两次TimeoutError，分别05:33:36及05:52:16 UTC。E405同PID等待采样均记录持续DataFileRead、无阻塞PID。不能把事件请求200当整个采集链路无错误。

实际命令：bash .runtime/e209-live/e404-finalize-independent.sh 退出0；Docker独立compact终审退出0；真实点击脚本e404-six-signals-browser-02退出0。首浏览器脚本生成sed失败，空脚本0属于无效验证，未计入通过。证据.runtime/e209-live/e404-summary-final.json、e404-current-versions-final.json、e404-compact-summary-final.json、e404-api-phases-final.json及浏览器live-clicks.json。

下一步继续E409整窗，冻结验证E408两次远端读取消除，研究报价IO和已证实队名异写；未解决项仍保持未完成。
