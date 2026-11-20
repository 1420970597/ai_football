# E282：排除监听队列丢包假设

更新2026-11-05 04:39 UTC。状态：只读研究完成，不进入代码修改。目标定位E277单群ConnectTimeout是否由远端HTTP转发器监听队列过小造成；责任infra/remote、services/collector；前置桥仅16并发槽且ThreadingHTTPServer默认队列5，线上桥启动时间和源码不变。

只读读取本项目远端桥容器自己的网络命名空间/proc/net/netstat：ListenOverflows0、ListenDrops0、TCPReqQFullDoCookies0、TCPReqQFullDrop0，默认队列5。实际exit0，e282-bridge-listen-read-02.json。现有累计计数不支持发生监听队列溢出，不以假设扩大队列或并发，不声称根因已修。第一次脚本复用片段误留AUDIT_END引用KeyError actual1，删除无关日志分支后重跑原只读检查exit0，失败保留。

单群状态自动恢复、主TG连接与授权和租约持续有效；继续对照自然消息和后续超时，不增加盲重试或变更网络出口。队列默认值与并发槽数不同本身不构成生产故障证明。
