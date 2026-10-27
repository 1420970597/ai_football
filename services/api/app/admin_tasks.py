"""Actionable diagnostics derived only from bounded, observed service facts."""


def diagnostic_tasks(data: dict) -> list[dict]:
    tasks = []

    def add(key, label, state, target, detail, count=None):
        task = {'key': key, 'label': label, 'state': state, 'target': target, 'detail': detail}
        if count is not None:
            task['count'] = count
        tasks.append(task)

    collector = data.get('collector') or {}
    sources = collector.get('sources') or {}
    leyu = sources.get('leyu') or {}
    rejected = leyu.get('failure_group') in {'authorization', 'session'} or leyu.get('last_error') in {
        'auth_expired', 'auth_rejected', 'renewal_api_unavailable', 'upstream_auth_expired',
        'session_expired', 'authorized_session_missing'}
    leyu_state = 'action_required' if rejected else 'clear' if leyu.get('last_success_at') and not leyu.get('last_error') else 'unverified'
    add('leyu_session', '乐鱼采集会话', leyu_state, 'leyu',
        '会话或续期异常，请核对采集配置与续期状态' if rejected else
        '最近采集成功；此状态不代表所有盘口新鲜' if leyu_state == 'clear' else '尚无当前会话可用证据')
    auth = collector.get('auth_state')
    tg_state = ('clear' if collector.get('state') == 'reachable' and collector.get('authorized') and auth == 'authorized'
                else 'action_required' if collector.get('state') == 'reachable' and auth in {'unauthorized', 'expired', 'revoked', 'unconfigured', 'failed', 'session_invalid'}
                else 'unverified')
    add('telegram_session', 'Telegram 授权', tg_state, 'telegram',
        '已授权；群组权限和媒体状态另行核验' if tg_state == 'clear' else
        '请检查授权状态并按页面提示恢复' if tg_state == 'action_required' else '采集服务或授权状态尚未核验')
    for key, label, target, advice in (
        ('pending_media', '媒体待上传', 'telegram', '检查对象存储和原消息，保留原文并等待重试'),
        ('unresolved_reviews', '未解决推荐', 'telegram', '按作者查看原文、模型分析与工具拒绝原因'),
        ('stale_quotes', '过期报价', 'leyu', '检查源时间和采集状态，禁止扩大时限冒充新鲜'),
        ('unknown_orders', '未知订单', 'orders', '核对官方订单回执；结果未知时禁止重复提交'),
    ):
        metric = data.get(key)
        count = metric.get('count') if isinstance(metric, dict) else None
        valid = type(count) is int and 0 <= count <= 5000
        state = 'action_required' if valid and count else 'clear' if valid else 'unverified'
        add(key, label, state, target, advice if state == 'action_required' else
            '本次有界检查未发现待办' if state == 'clear' else '尚未取得有效计数',
            {'count': count, 'capped': bool(metric.get('capped'))} if valid else None)
    schema = data.get('schema')
    add('schema_migration', '数据库结构', 'clear' if schema == 'verified' else
        'action_required' if schema == 'migration_required' else 'unverified', 'system',
        '当前版本与迁移摘要一致' if schema == 'verified' else '先备份并核对受控迁移，禁止由业务进程自动改表')
    add('migration_failures', '失败迁移记录', 'unverified', 'system',
        '失败事务不会留下成功登记；需核对部署迁移日志，结构已核验不代表没有历史失败')
    return tasks
