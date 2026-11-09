(() => {
/**
 * 文件职责：任务页视图，负责调度任务列表、倒计时进度与任务操作入口展示。
 */
 
 const { Box, Typography, Button, Chip } = MaterialUI;

function normalizeTimestampValue(value) {
    // 兼容后端可能返回“秒级”或“毫秒级”时间戳；统一规整为毫秒再交给 Date 处理。
    if (value === null || value === undefined || value === '') return null;
    if (typeof value === 'number') {
        return value < 1000000000000 ? value * 1000 : value;
    }
    return value;
}

function resolveTaskProgress(job, nowMs) {
    const nextRun = parseDateish(job.next_run_time);
    const nextTrigger = parseDateish(normalizeTimestampValue(job.next_trigger_time));
    const scheduledAtMs = normalizeTimestampValue(job.last_scheduled_at);
    const randomIntervalSeconds = Number(job.last_schedule_random_interval_seconds);
    const minIntervalSeconds = Number(job.last_schedule_min_interval_seconds);
    const maxIntervalSeconds = Number(job.last_schedule_max_interval_seconds);

    if (!nextRun) {
        // 若调度器未给出下一次执行时间，则返回一组可安全渲染的默认元信息。
        return {
            nextRun: null,
            nextTrigger,
            remainingSeconds: 0,
            remainingText: '--',
            countdownText: '暂无有效执行时间',
            friendlyText: '--',
            status: 'unknown',
            statusLabel: '待确认',
            progressPercent: 0,
        };
    }

    // remainingSeconds 驱动任务卡片的倒计时、状态颜色与进度条表现。
    const remainingSeconds = Math.max(0, Math.ceil((nextRun.getTime() - nowMs) / 1000));
    const friendlyText = formatFriendlyTime(nextRun, 'Asia/Shanghai');
    const remainingText = remainingSeconds > 0
        ? formatDuration(remainingSeconds, { compact: true, maxUnits: 3 })
        : '已到触发时间';

    let status = 'future';
    let statusLabel = '正常排队';

    if (remainingSeconds <= 0) {
        status = 'expired';
        statusLabel = '待刷新';
    } else if (remainingSeconds <= 300) {
        status = 'urgent';
        statusLabel = '即将触发';
    } else if (remainingSeconds <= 1800) {
        status = 'soon';
        statusLabel = '等待触发';
    }

    // 优先使用更明确的随机调度窗口；若缺失，再尝试用 nextTrigger / max / min 区间推导。
    const candidateWindowSeconds = [
        randomIntervalSeconds,
        nextTrigger ? Math.max(0, Math.round((nextTrigger.getTime() - nowMs) / 1000)) : 0,
        maxIntervalSeconds,
        minIntervalSeconds,
    ].filter((value) => Number.isFinite(value) && value > 0);
    const windowSeconds = candidateWindowSeconds.length > 0 ? candidateWindowSeconds[0] : 0;

    let progressPercent = remainingSeconds <= 0 ? 100 : 0;
    if (windowSeconds > 0) {
        let elapsedSeconds = 0;

        if (scheduledAtMs) {
            // 若保存了调度创建时刻，则以真实经历时长计算进度更准确。
            elapsedSeconds = Math.max(0, (nowMs - scheduledAtMs) / 1000);
        } else {
            // 否则退化为“总窗口 - 剩余时间”的近似值。
            elapsedSeconds = Math.max(0, windowSeconds - remainingSeconds);
        }

        progressPercent = Math.max(0, Math.min(100, Math.round((elapsedSeconds / windowSeconds) * 100)));
    }

    return {
        nextRun,
        nextTrigger,
        remainingSeconds,
        remainingText,
        countdownText: remainingSeconds > 0 ? `${remainingText} 后执行` : '等待下一轮刷新确认',
        friendlyText,
        status,
        statusLabel,
        progressPercent,
    };
}

function formatQuietHoursText(value) {
    const raw = String(value || '').trim();
    if (!raw) return '未配置';

    const matched = raw.match(/^(\d{1,2})\s*-\s*(\d{1,2})$/);
    if (!matched) return raw;

    const startHour = Number(matched[1]);
    const endHour = Number(matched[2]);
    if (!Number.isInteger(startHour) || !Number.isInteger(endHour)) return raw;
    if (startHour < 0 || startHour > 23 || endHour < 0 || endHour > 23) return raw;

    return `${String(startHour).padStart(2, '0')}:00 - ${String(endHour).padStart(2, '0')}:00`;
}

function formatScheduleIntervalText(minMinutes, maxMinutes) {
    const minValue = Number(minMinutes);
    const maxValue = Number(maxMinutes);
    const hasMin = Number.isFinite(minValue) && minValue > 0;
    const hasMax = Number.isFinite(maxValue) && maxValue > 0;

    if (!hasMin && !hasMax) return '未配置';
    if (hasMin && hasMax) {
        if (minValue > maxValue) {
            return `配置异常：${minValue} > ${maxValue} 分钟`;
        }
        return `${minValue} - ${maxValue} 分钟`;
    }
    if (hasMin) return `${minValue} 分钟`;
    return `${maxValue} 分钟`;
}


function TasksView({ onRefresh }) {
    const { state, dispatch } = useAppContext();
    const api = useApi();
    // 每秒刷新当前时间，驱动任务卡片上的倒计时与进度条更新。
    const [nowMs, setNowMs] = React.useState(Date.now());
    const [triggerFeedbackMap, setTriggerFeedbackMap] = React.useState({});
    const [rescheduleFeedbackMap, setRescheduleFeedbackMap] = React.useState({});
    const displayTimezone = state.config?.displayTimezone || 'Asia/Shanghai';

    React.useEffect(() => {
        const timer = setInterval(() => {
            setNowMs(Date.now());
        }, 1000);
        return () => clearInterval(timer);
    }, []);

    const triggerNow = async (session) => {
        setTriggerFeedbackMap((prev) => ({
            ...prev,
            [session]: { status: 'pending', text: '正在触发，等待 LLM 回复完成…' },
        }));
        try {
            // 手动触发后会重新走一次父级全量刷新，确保状态页与任务页同步更新。
            const result = await api.triggerJob(session);
            setTriggerFeedbackMap((prev) => ({
                ...prev,
                [session]: {
                    status: 'pending',
                    text: result?.message || '已开始立即触发，正在等待 LLM 回复完成…',
                },
            }));
            await onRefresh();
        } catch (e) {
            setTriggerFeedbackMap((prev) => ({
                ...prev,
                [session]: { status: 'error', text: e.message || '触发任务失败，请稍后重试' },
            }));
            dispatch({ type: 'SET_ERROR', payload: e.message || '触发任务失败' });
        }
    };

    const cancelJob = async (session) => {
        try {
            await api.cancelJob(session);
            await onRefresh();
        } catch (e) {
            dispatch({ type: 'SET_ERROR', payload: e.message || '取消任务失败' });
        }
    };

    const rescheduleJob = async (session) => {
        setRescheduleFeedbackMap((prev) => ({
            ...prev,
            [session]: { status: 'pending', text: '正在重新调度下一次主动消息时间…' },
        }));
        try {
            const result = await api.rescheduleJob(session);
            setRescheduleFeedbackMap((prev) => ({
                ...prev,
                [session]: { status: 'success', text: result?.message || '已重新调度下一次主动消息时间' },
            }));
            await onRefresh();
        } catch (e) {
            setRescheduleFeedbackMap((prev) => ({
                ...prev,
                [session]: { status: 'error', text: e.message || '重新调度失败，请稍后重试' },
            }));
            dispatch({ type: 'SET_ERROR', payload: e.message || '重新调度失败' });
        }
    };

    const jobs = state.jobs || [];

    React.useEffect(() => {
        setTriggerFeedbackMap((prev) => {
            let changed = false;
            const next = { ...prev };
            jobs.forEach((job) => {
                if (job.manual_trigger_in_progress) {
                    const current = next[job.id];
                    const expectedText = '正在触发，等待 LLM 回复完成…';
                    if (!current || current.status !== 'pending' || current.text !== expectedText) {
                        next[job.id] = { status: 'pending', text: expectedText };
                        changed = true;
                    }
                    return;
                }

                if (next[job.id]?.status === 'pending') {
                    next[job.id] = { status: 'success', text: '本次立即触发已完成，按钮已恢复可用' };
                    changed = true;
                }
            });
            return changed ? next : prev;
        });
    }, [jobs]);

    React.useEffect(() => {
        const successEntries = Object.entries(triggerFeedbackMap)
            .filter(([, value]) => value?.status === 'success');
        if (successEntries.length === 0) {
            return undefined;
        }

        const timers = successEntries.map(([sessionId]) => setTimeout(() => {
            setTriggerFeedbackMap((prev) => {
                const current = prev[sessionId];
                if (!current || current.status !== 'success') {
                    return prev;
                }

                const next = { ...prev };
                delete next[sessionId];
                return next;
            });
        }, 3000));

        return () => {
            timers.forEach((timer) => clearTimeout(timer));
        };
    }, [triggerFeedbackMap]);

    React.useEffect(() => {
        const successEntries = Object.entries(rescheduleFeedbackMap)
            .filter(([, value]) => value?.status === 'success');
        if (successEntries.length === 0) {
            return undefined;
        }

        const timers = successEntries.map(([sessionId]) => setTimeout(() => {
            setRescheduleFeedbackMap((prev) => {
                const current = prev[sessionId];
                if (!current || current.status !== 'success') {
                    return prev;
                }

                const next = { ...prev };
                delete next[sessionId];
                return next;
            });
        }, 3000));

        return () => {
            timers.forEach((timer) => clearTimeout(timer));
        };
    }, [rescheduleFeedbackMap]);

    return (
        <Box>
            <Box className="tasks-header-row">
                <div>
                    <Typography variant="h6" sx={{ fontWeight: 700, mb: 0.5 }}>
                        {`调度任务 (当前共 ${jobs.length} 个调度任务)`}
                    </Typography>
                </div>
                <Button variant="contained" onClick={onRefresh} startIcon={<span>🔄</span>} sx={{ borderRadius: 3, boxShadow: 'none', px: 2.25 }}>
                    刷新任务
                </Button>
            </Box>

            {jobs.length === 0 ? (
                <div className="card tasks-empty-card">
                    <div className="tasks-empty-icon">🪄</div>
                    <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>暂无调度任务</Typography>
                    <Typography variant="body1" color="text.secondary">
                        当前没有待执行的主动消息任务。当会话满足调度条件后，这里会自动展示任务卡片与倒计时信息。
                    </Typography>
                </div>
            ) : (
                <div className="tasks-grid-enhanced">
                    {jobs.map((job) => {
                        // 每张任务卡在渲染前先推导出倒计时、状态与进度等派生信息。
                        const task = resolveTaskProgress(job, nowMs);
                        const chipColor = job.unanswered_count > 0 ? 'warning' : 'default';
                        const sessionIdText = String(job.id || '');
                        const sessionDisplayName = String(job.session_display_name || job.session_name || sessionIdText || '--');
                        const hasAlias = Boolean(sessionDisplayName && sessionIdText && sessionDisplayName !== sessionIdText);
                        const sessionSubText = hasAlias ? sessionIdText : '';
                        const sourceModeLabel = resolveSourceModeLabel(job.source_mode);
                        const unansweredLabel = formatUnansweredLabel(job.unanswered_count, job.max_unanswered_times);

                        const isTriggerRunning = Boolean(job.manual_trigger_in_progress);
                        const triggerFeedback = triggerFeedbackMap[job.id];
                        const rescheduleFeedback = rescheduleFeedbackMap[job.id];
                        const isRescheduling = rescheduleFeedback?.status === 'pending';
                        const triggerButtonLabel = isTriggerRunning ? '触发中…' : '立即触发';
                        const triggerHelperText = isTriggerRunning
                            ? (triggerFeedback?.text || '正在触发，等待 LLM 回复完成…')
                            : triggerFeedback?.text;
                        const rescheduleHelperText = rescheduleFeedback?.text;
                        const scheduleIntervalText = formatScheduleIntervalText(
                            job.schedule_min_interval_minutes,
                            job.schedule_max_interval_minutes,
                        );
                        const quietHoursText = formatQuietHoursText(job.quiet_hours);

                        return (
                            <div className={`card task-card-enhanced ${task.status === 'urgent' ? 'is-urgent' : ''} ${task.status === 'expired' ? 'is-expired' : ''}`} key={job.id}>
                                <div className="task-card-top" style={{ overflow: 'visible' }}>
                                    <div className="task-card-title-block" style={{ overflow: 'visible' }}>
                                        <Typography variant="subtitle2" className="task-card-kicker">
                                            会话
                                        </Typography>
                                        <Typography
                                            variant="body1"
                                            className={`task-card-session ${hasAlias ? 'is-primary' : 'mono'}`}
                                        >
                                            {sessionDisplayName}
                                        </Typography>
                                    </div>
                                    <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 1, flexShrink: 0 }}>
                                        <Box
                                            sx={{
                                                display: 'inline-flex',
                                                alignItems: 'center',
                                                justifyContent: 'center',
                                                px: 1.25,
                                                py: 0.5,
                                                borderRadius: '999px',
                                                fontSize: 11,
                                                fontWeight: 800,
                                                whiteSpace: 'nowrap',
                                                border: '1px solid rgba(103, 80, 164, 0.18)',
                                                background: 'rgba(103, 80, 164, 0.08)',
                                                color: 'var(--md-sys-color-primary)',
                                                lineHeight: 1.2,
                                            }}
                                        >
                                            {sourceModeLabel}
                                        </Box>
                                        <Chip
                                            label={unansweredLabel}
                                            size="small"
                                            color={chipColor}
                                            variant={job.unanswered_count > 0 ? 'filled' : 'outlined'}
                                        />
                                    </Box>
                                </div>

                                {sessionSubText ? (
                                    <Typography
                                        variant="caption"
                                        className="task-card-session-sub mono"
                                        sx={{
                                            display: 'block',
                                            width: '100%',
                                            mt: -0.5,
                                            mb: 0.5,
                                            overflow: 'visible',
                                            whiteSpace: 'nowrap',
                                        }}
                                    >
                                        {`UMO · ${sessionSubText}`}
                                    </Typography>
                                ) : null}

                                <div className="task-next-run-panel">
                                    <div className="task-next-run-primary-row">
                                        <div>
                                            <Typography variant="caption" className="task-next-run-label">
                                                下一次执行时间
                                            </Typography>
                                            <Typography variant="body2" className="task-next-run-time">
                                                {task.nextRun ? formatDateTime(task.nextRun, displayTimezone, { includeYear: true, includeSeconds: true }) : '--'}
                                            </Typography>
                                        </div>
                                        <div className={`task-status-pill is-${task.status}`}>
                                            {task.statusLabel}
                                        </div>
                                    </div>
                                    <Typography variant="body2" className="task-countdown-text">
                                        {task.countdownText}
                                    </Typography>
                                    <div className="task-progress-track">
                                        <div
                                            className={`task-progress-bar is-${task.status}`}
                                            style={{ width: `${task.progressPercent}%` }}
                                        ></div>
                                    </div>
                                </div>

                                <Box
                                    sx={{
                                        mt: 1.5,
                                        display: 'grid',
                                        gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
                                        gap: 1.25,
                                    }}
                                >
                                    <Box
                                        sx={{
                                            px: 1.5,
                                            py: 1.25,
                                            borderRadius: 2.5,
                                            border: '1px solid rgba(103, 80, 164, 0.12)',
                                            background: 'rgba(103, 80, 164, 0.04)',
                                            minWidth: 0,
                                        }}
                                    >
                                        <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mb: 0.4 }}>
                                            调度间隔
                                        </Typography>
                                        <Typography variant="body2" sx={{ fontWeight: 600, color: 'text.primary' }}>
                                            {scheduleIntervalText}
                                        </Typography>
                                    </Box>
                                    <Box
                                        sx={{
                                            px: 1.5,
                                            py: 1.25,
                                            borderRadius: 2.5,
                                            border: '1px solid rgba(103, 80, 164, 0.12)',
                                            background: 'rgba(103, 80, 164, 0.04)',
                                            minWidth: 0,
                                        }}
                                    >
                                        <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mb: 0.4 }}>
                                            免打扰时段
                                        </Typography>
                                        <Typography variant="body2" sx={{ fontWeight: 600, color: 'text.primary' }}>
                                            {quietHoursText}
                                        </Typography>
                                    </Box>
                                </Box>
 
                                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, mt: 'auto', pt: 1.5 }}>
                                    {triggerHelperText ? (
                                        <Typography
                                            variant="caption"
                                            sx={{
                                                minHeight: 20,
                                                color: triggerFeedback?.status === 'error'
                                                    ? 'error.main'
                                                    : triggerFeedback?.status === 'success'
                                                        ? 'success.main'
                                                        : 'text.secondary',
                                            }}
                                        >
                                            {triggerHelperText}
                                        </Typography>
                                    ) : null}
                                    {rescheduleHelperText ? (
                                        <Typography
                                            variant="caption"
                                            sx={{
                                                minHeight: 20,
                                                color: rescheduleFeedback?.status === 'error'
                                                    ? 'error.main'
                                                    : rescheduleFeedback?.status === 'success'
                                                        ? 'success.main'
                                                        : 'text.secondary',
                                            }}
                                        >
                                            {rescheduleHelperText}
                                        </Typography>
                                    ) : null}
                                    <Box sx={{ display: 'flex', gap: 1 }}>
                                        <Button
                                            variant="outlined"
                                            size="small"
                                            fullWidth
                                            disabled={isTriggerRunning || isRescheduling}
                                            onClick={() => triggerNow(job.id)}
                                            sx={{ borderRadius: 2.5 }}
                                        >
                                            {triggerButtonLabel}
                                        </Button>
                                        <Button
                                            variant="outlined"
                                            size="small"
                                            fullWidth
                                            disabled={isTriggerRunning || isRescheduling}
                                            onClick={() => rescheduleJob(job.id)}
                                            sx={{ borderRadius: 2.5 }}
                                        >
                                            {isRescheduling ? '重新调度中…' : '重新调度'}
                                        </Button>
                                        <Button
                                            variant="outlined"
                                            color="error"
                                            size="small"
                                            fullWidth
                                            disabled={isRescheduling}
                                            onClick={() => cancelJob(job.id)}
                                            sx={{ borderRadius: 2.5 }}
                                        >
                                            取消任务
                                        </Button>
                                    </Box>
                                </Box>
                            </div>
                        );
                    })}
                </div>
            )}
        </Box>
    );
}

// 暴露为全局视图组件，供应用入口按 currentView 切换。
window.TasksView = TasksView;
})();

