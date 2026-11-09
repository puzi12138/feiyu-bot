(() => {
/**
 * 文件职责：状态页视图，负责服务状态、计时器可视化与手动刷新交互展示。
 */
 
 const { Box, Typography, Chip } = MaterialUI;

function dedupeStatusTimerCards(cards) {
    const priorityMap = {
        // 同一会话若同时命中多类计时器，群沉默卡优先级更高，避免信息重复。
        group_silence: 3,
        auto_trigger: 2,
    };
    const merged = new Map();

    cards.forEach((card) => {
        const key = String(card.session_id || '');
        if (!key) return;

        const existing = merged.get(key);
        if (!existing) {
            merged.set(key, card);
            return;
        }

        const currentPriority = priorityMap[card.timer_kind] ?? 0;
        const existingPriority = priorityMap[existing.timer_kind] ?? 0;

        if (currentPriority > existingPriority) {
            merged.set(key, card);
            return;
        }

        if (currentPriority === existingPriority) {
            // 若类型优先级相同，则保留“更快触发”的那张卡，突出最紧迫状态。
            const currentRemaining = Number(card.remaining_seconds ?? Number.MAX_SAFE_INTEGER);
            const existingRemaining = Number(existing.remaining_seconds ?? Number.MAX_SAFE_INTEGER);
            if (currentRemaining < existingRemaining) {
                merged.set(key, card);
            }
        }
    });

    return Array.from(merged.values());
}

function StatusMetricRow({ label, value, emphasize = false, status = '' }) {
    return (
        <div className="status-metric-row">
            <Typography variant="body2" className="status-metric-label">
                {label}
            </Typography>
            <Typography
                variant="body2"
                className={`status-metric-value ${emphasize ? 'is-emphasize' : ''} ${status ? `is-${status}` : ''}`}
            >
                {value}
            </Typography>
        </div>
    );
}


function resolveStatusTimerCard(timer, nowMs, displayTimezone) {
    // 后端 target_time / started_at 以秒级时间戳返回，这里统一转为 Date 便于格式化和比较。
    const targetTime = parseDateish(timer.target_time ? Number(timer.target_time) * 1000 : null);
    const startedAt = parseDateish(timer.started_at ? Number(timer.started_at) * 1000 : null);
    const remainingSeconds = Number.isFinite(Number(timer.remaining_seconds))
        ? Math.max(0, Number(timer.remaining_seconds))
        : (targetTime ? Math.max(0, Math.ceil((targetTime.getTime() - nowMs) / 1000)) : 0);
    const windowSeconds = Math.max(0, Number(timer.window_seconds ?? 0));
    // 若后端未给出 progress_percent，则前端基于总窗口时长与剩余秒数推导一个近似值。
    const fallbackProgress = windowSeconds > 0
        ? Math.max(0, Math.min(100, Math.round(((windowSeconds - remainingSeconds) / windowSeconds) * 100)))
        : 0;
    const progressPercent = Math.max(0, Math.min(100, Math.round(Number(timer.progress_percent ?? fallbackProgress) || 0)));

    // 根据剩余时间给卡片打上状态标签，供颜色、文案和动画统一使用。
    let status = 'future';
    let statusLabel = '稳定运行';
    if (!targetTime) {
        status = 'unknown';
        statusLabel = '待确认';
    } else if (remainingSeconds <= 0) {
        status = 'expired';
        statusLabel = '待刷新';
    } else if (remainingSeconds <= 300) {
        status = 'urgent';
        statusLabel = '即将结束';
    } else if (remainingSeconds <= 1800) {
        status = 'soon';
        statusLabel = '正常计时';
    }

    const isGroupSession = timer.session_category === 'group';
    const isGroupSilence = timer.timer_kind === 'group_silence';
    const categoryLabel = isGroupSession ? '群会话' : '私聊会话';
    const sectionKey = isGroupSilence ? 'group_silence' : 'auto_trigger';
    const sectionTitle = isGroupSilence ? '群沉默倒计时' : '自动触发检测';
    const accentClass = isGroupSilence
        ? 'accent-group-silence'
        : (isGroupSession ? 'accent-auto-group' : 'accent-auto-friend');
    const kindBadgeLabel = isGroupSilence
        ? '沉默重置型'
        : (isGroupSession ? '群自动触发' : '私聊自动触发');
    const sourceModeLabel = resolveSourceModeLabel(timer.source_mode);
    const countdownText = targetTime
        ? (remainingSeconds > 0 ? `${formatDuration(remainingSeconds, { compact: true, maxUnits: 3 })} 后到期` : '等待下一轮刷新确认')
        : '暂无有效目标时间';
    const sessionIdText = String(timer.session_id || '');
    const sessionDisplayName = String(timer.session_display_name || timer.session_name || sessionIdText || '--');
    const hasAlias = Boolean(sessionDisplayName && sessionIdText && sessionDisplayName !== sessionIdText);
    const sessionSubText = hasAlias ? sessionIdText : '';

    return {
        ...timer,
        startedAt,
        targetTime,
        remainingSeconds,
        progressPercent,
        status,
        statusLabel,
        categoryLabel,
        // 这些派生文案与样式字段统一在这里计算，减少渲染层的模板噪声。
        sectionKey,
        sectionTitle,
        accentClass,
        kindBadgeLabel,
        sourceModeLabel,
        unansweredLabel: formatUnansweredLabel(timer.unanswered_count, timer.max_unanswered_times),
        countdownText,
        sessionDisplayName,
        sessionSubText,
        hasAlias,
        targetText: targetTime ? formatDateTime(targetTime, displayTimezone, { includeYear: true, includeSeconds: true }) : '--',
    };
}

function StatusTimerCard({ timer, displayTimezone, nowMs, resetHint }) {
    const meta = resolveStatusTimerCard(timer, nowMs, displayTimezone);
    // 未回复次数大于 0 时用 warning 色，帮助管理员快速定位“机器人已被晾着”的会话。
    const chipColor = Number(meta.unanswered_count ?? 0) > 0 ? 'warning' : 'default';

    return (
        <div className={`card status-timer-card ${meta.accentClass} ${meta.status === 'urgent' ? 'is-urgent' : ''} ${meta.status === 'expired' ? 'is-expired' : ''} ${resetHint ? 'is-resetting' : ''}`}>
            <div className="status-timer-card-top">
                <div className="status-timer-title-block">
                    <Typography variant="subtitle2" className="status-timer-kicker">
                        {meta.sectionTitle}
                    </Typography>
                    <Typography
                        variant="body1"
                        className={`status-timer-session ${meta.hasAlias ? 'is-primary' : 'mono'}`}
                    >
                        {meta.sessionDisplayName}
                    </Typography>
                    {meta.sessionSubText ? (
                        <Typography variant="caption" className="status-timer-session-sub mono">
                            {`UMO · ${meta.sessionSubText}`}
                        </Typography>
                    ) : null}
                </div>
                <div className="status-timer-chip-stack">
                    <div className={`status-timer-kind-badge ${meta.accentClass}`}>{meta.kindBadgeLabel}</div>
                    <div className="status-timer-kind-badge">{meta.sourceModeLabel}</div>
                    <Chip
                        label={meta.unansweredLabel}
                        size="small"
                        color={chipColor}
                        variant={Number(meta.unanswered_count ?? 0) > 0 ? 'filled' : 'outlined'}
                    />
                </div>
            </div>

            <div className="status-timer-meta-row">
                <div className={`task-status-pill is-${meta.status}`}>{meta.statusLabel}</div>
                <div className="status-timer-category-pill">{meta.categoryLabel}</div>
            </div>

            {resetHint ? (
                // 群聊出现新消息导致目标时间明显后移时，短暂显示“已重置”提示，帮助理解状态变化原因。
                <div className="status-timer-reset-hint">
                    <span className="status-timer-reset-dot"></span>
                    群聊刚刚有新消息，沉默计时器已重新开始计时
                </div>
            ) : null}

            <div className="status-timer-panel">
                <div className="status-timer-primary-row">
                    <div>
                        <Typography variant="caption" className="status-timer-label">
                            倒计时
                        </Typography>
                        <Typography variant="body2" className="status-timer-countdown">
                            {meta.countdownText}
                        </Typography>
                    </div>
                    <div className="status-timer-progress-value">{meta.progressPercent}%</div>
                </div>
                <div className="task-progress-track">
                    <div className={`task-progress-bar is-${meta.status}`} style={{ width: `${meta.progressPercent}%` }}></div>
                </div>
            </div>

            <div className="status-timer-info-grid">
                <div className="status-timer-info-item">
                    <Typography variant="caption" className="status-timer-info-label">
                        目标时间
                    </Typography>
                    <Typography variant="body2" className="status-timer-info-value">
                        {meta.targetText}
                    </Typography>
                </div>
                <div className="status-timer-info-item">
                    <Typography variant="caption" className="status-timer-info-label">
                        计时窗口
                    </Typography>
                    <Typography variant="body2" className="status-timer-info-value">
                        {meta.window_seconds ? formatDuration(meta.window_seconds, { compact: true, maxUnits: 2 }) : '--'}
                    </Typography>
                </div>
            </div>
        </div>
    );
}

function StatusView({ onRefresh }) {
    const { state } = useAppContext();
    const status = state.status || {};
    // nowMs 每秒更新一次，用于驱动倒计时与相对时间文本实时刷新。
    const [nowMs, setNowMs] = React.useState(Date.now());
    // resetHintMap 记录哪些群沉默卡片当前需要显示“刚刚被重置”的短暂提示。
    const [resetHintMap, setResetHintMap] = React.useState({});
    // 手动刷新按钮的视觉反馈状态机：idle -> loading -> success / error。
    const [manualRefreshState, setManualRefreshState] = React.useState({ phase: 'idle', finishedAt: 0 });
    const previousTimerSnapshotRef = React.useRef({});
    const resetHintTimersRef = React.useRef({});
    const displayTimezone = state.config?.displayTimezone || 'Asia/Shanghai';

    React.useEffect(() => {
        const timer = setInterval(() => {
            setNowMs(Date.now());
        }, 1000);
        return () => clearInterval(timer);
    }, []);

    React.useEffect(() => {
        return () => {
            // 组件卸载时清理所有“重置提示”的超时器，避免状态在卸载后回写。
            Object.values(resetHintTimersRef.current).forEach((timerId) => {
                clearTimeout(timerId);
            });
        };
    }, []);

    // 统一把后端状态字段规整为前端友好的基础数字类型。
    const uptimeSeconds = Number(status.uptime_seconds ?? 0);
    const jobsCount = Number(status.jobs_count ?? 0);
    const sessionsCount = Number(status.sessions_count ?? 0);
    const autoTriggerTimers = Number(status.auto_trigger_timers ?? 0);
    const groupTimers = Number(status.group_timers ?? 0);
    const schedulerRunning = Boolean(status.scheduler_running);
    const pluginRunning = Boolean(status.running);
    const autoTriggerCards = Array.isArray(status.auto_trigger_cards) ? status.auto_trigger_cards : [];
    const groupTimerCards = Array.isArray(status.group_timer_cards) ? status.group_timer_cards : [];
    // 合并并去重所有会话计时器，保证一个会话最多出现一张最关键卡片。
    const timerCards = dedupeStatusTimerCards([...groupTimerCards, ...autoTriggerCards]);
    const timerSections = [
        {
            key: 'group_silence',
            title: '群沉默计时器',
            description: '群内一旦有新消息就会重新计时，适合观察沉默窗口是否被活跃消息重置。',
            emptyText: '当前没有正在运行的群沉默计时器。',
            cards: timerCards.filter((timer) => timer.timer_kind === 'group_silence'),
        },
        {
            key: 'auto_trigger',
            title: '自动触发计时器',
            description: '用于观察会话的自动触发状况，并按群聊 / 私聊显示不同标签。通常情况下，插件运行一段时间后，这里将不会出现新的卡片。',
            emptyText: '当前没有正在运行的自动触发计时器。',
            cards: timerCards.filter((timer) => timer.timer_kind === 'auto_trigger'),
        },
    ];

    React.useEffect(() => {
        const nextSnapshot = {};
        const nextHints = {};
        const activeKeys = new Set();

        timerCards.forEach((timer) => {
            const key = `${timer.timer_kind}-${timer.session_id}`;
            const currentRemaining = Math.max(0, Number(timer.remaining_seconds ?? 0));
            const currentTarget = Number(timer.target_time ?? 0);
            const currentStatus = String(timer.status || '');
            const previous = previousTimerSnapshotRef.current[key] || null;

            activeKeys.add(key);
            nextSnapshot[key] = {
                remainingSeconds: currentRemaining,
                targetTime: currentTarget,
                status: currentStatus,
            };

            // 真正的“被重置”应表现为：同一张群沉默卡片的剩余时间突然回升，且当前仍处于有效运行态。
            const isGroupReset = timer.timer_kind === 'group_silence'
                && previous
                && currentStatus !== 'expired'
                && currentStatus !== 'unknown'
                && currentRemaining - Number(previous.remainingSeconds ?? 0) > 3;

            if (isGroupReset) {
                nextHints[key] = true;
                if (resetHintTimersRef.current[key]) {
                    clearTimeout(resetHintTimersRef.current[key]);
                }
                resetHintTimersRef.current[key] = setTimeout(() => {
                    setResetHintMap((current) => {
                        const updated = { ...current };
                        delete updated[key];
                        return updated;
                    });
                    delete resetHintTimersRef.current[key];
                }, 4200);
            }
        });

        Object.keys(resetHintTimersRef.current).forEach((key) => {
            if (!activeKeys.has(key)) {
                clearTimeout(resetHintTimersRef.current[key]);
                delete resetHintTimersRef.current[key];
            }
        });

        previousTimerSnapshotRef.current = nextSnapshot;
        setResetHintMap((current) => {
            const filtered = Object.fromEntries(
                Object.entries(current).filter(([key]) => activeKeys.has(key))
            );
            return Object.keys(nextHints).length > 0 ? { ...filtered, ...nextHints } : filtered;
        });
    }, [timerCards]);

    const handleManualRefresh = async () => {
        if (manualRefreshState.phase === 'loading') return;
        setManualRefreshState((current) => ({ ...current, phase: 'loading' }));
        try {
            // 手动刷新会回到入口层执行一次完整 loadAll，确保状态、会话、配置、任务全部同步。
            await onRefresh();
            setManualRefreshState({ phase: 'success', finishedAt: Date.now() });
        } catch (e) {
            setManualRefreshState({ phase: 'error', finishedAt: Date.now() });
        }
    };

    const refreshButtonLabel = manualRefreshState.phase === 'loading'
        ? '正在刷新控制台数据...'
        : manualRefreshState.phase === 'success'
            ? '刷新完成'
            : manualRefreshState.phase === 'error'
                ? '刷新失败，请重试'
                : '刷新控制台数据';

    const refreshNoteText = manualRefreshState.phase === 'loading'
        ? '正在重新拉取运行状态、会话列表、配置与任务列表。'
        : manualRefreshState.phase === 'success'
            ? `已完成手动刷新 · ${formatFriendlyTime(manualRefreshState.finishedAt, displayTimezone)}`
            : manualRefreshState.phase === 'error'
                ? '本次手动刷新失败，请检查服务连接或稍后重试。'
                : '手动刷新会重新拉取运行状态、会话列表、配置与任务列表。';

    return (
        <Box>
            <div className="dashboard-grid proactive-status-grid">
                <div className="span-4">
                    <div className="card status-panel-card status-panel-card-primary">
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 3 }}>
                            <div className="status-panel-icon status-panel-icon-blue">⚡</div>
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>服务状态</Typography>
                        </Box>

                        <div className="status-metric-list">
                            <StatusMetricRow
                                label="插件状态"
                                value={pluginRunning ? '运行中' : '已停止'}
                                emphasize
                                status={pluginRunning ? 'success' : 'error'}
                            />
                            <div className="status-divider"></div>
                            <StatusMetricRow
                                label="运行时长"
                                value={formatDuration(uptimeSeconds, { compact: true, maxUnits: 4 })}
                                emphasize
                            />
                            <StatusMetricRow label="已连接 WebSocket" value={`${Number(status.ws_connections ?? 0)} 个`} />
                        </div>
                    </div>
                </div>

                <div className="span-4">
                    <div className="card status-panel-card">
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 3 }}>
                            <div className="status-panel-icon status-panel-icon-purple">⏱️</div>
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>调度概览</Typography>
                        </Box>

                        <div className="status-metric-list">
                            <StatusMetricRow
                                label="调度器"
                                value={schedulerRunning ? '已启动' : '未启动'}
                                emphasize
                                status={schedulerRunning ? 'success' : 'error'}
                            />
                            <div className="status-divider"></div>
                            <StatusMetricRow label="当前任务总数" value={`${jobsCount} 个`} emphasize />
                            <StatusMetricRow label="会话数据量" value={`${sessionsCount} 个`} />
                            <StatusMetricRow label="自动触发计时器" value={`${autoTriggerTimers} 个`} />
                            <StatusMetricRow label="群沉默计时器" value={`${groupTimers} 个`} />
                        </div>
                    </div>
                </div>

                <div className="span-4">
                    <div className="card status-panel-card">
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 3 }}>
                            <div className="status-panel-icon status-panel-icon-pink">🚀</div>
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>快捷操作</Typography>
                        </Box>

                        <div className="status-actions-list">
                            <div className="status-action-tooltip-wrap">
                                <button
                                    className="btn btn-action"
                                    onClick={handleManualRefresh}
                                    disabled={manualRefreshState.phase === 'loading'}
                                    aria-describedby="status-refresh-tooltip"
                                    style={{
                                        opacity: manualRefreshState.phase === 'loading' ? 0.82 : 1,
                                        cursor: manualRefreshState.phase === 'loading' ? 'wait' : 'pointer',
                                    }}
                                >
                                    <span style={{ fontSize: '18px' }}>
                                        {manualRefreshState.phase === 'loading' ? '⏳' : manualRefreshState.phase === 'success' ? '✅' : manualRefreshState.phase === 'error' ? '⚠️' : '🔄'}
                                    </span>
                                    {refreshButtonLabel}
                                </button>
                                <div className="status-action-tooltip-bubble" role="note" id="status-refresh-tooltip">
                                    {refreshNoteText}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div className="span-12">
                    <div className="status-timers-section">
                        <Box className="status-timers-header-row">
                            <div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 0.5 }}>
                                    {`会话计时器可视化 (当前共 ${timerCards.length} 个计时器)`}
                                </Typography>
                                <Typography variant="body2" className="tasks-header-subtitle">
                                    实时展示自动触发检测与群沉默检测的倒计时、进度和会话状态。此处卡片的倒计时结束后会进入任务管理页面
                                </Typography>
                            </div>
                            <div className="status-timers-summary-pills" aria-hidden="true">
                                {timerSections.map((section) => (
                                    <div className="status-timers-summary-pill" key={`summary-${section.key}`}>
                                        <span className="status-timers-summary-pill-label">{section.title}</span>
                                        <span className="status-timers-summary-pill-count">{section.cards.length}</span>
                                    </div>
                                ))}
                            </div>
                        </Box>

                        {timerCards.length === 0 ? (
                            <div className="card status-timers-empty-card">
                                <div className="tasks-empty-icon">🫧</div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>暂无运行中的会话计时器</Typography>
                                <Typography variant="body1" color="text.secondary">
                                    当前没有正在计时的自动触发或群沉默会话。等插件进入下一轮会话调度后，这里会自动出现对应卡片。
                                </Typography>
                            </div>
                        ) : (
                            <div className="status-timer-sections">
                                {timerSections.map((section) => (
                                    <div className="status-timer-section-block" key={section.key}>
                                        <div className="status-timer-section-head">
                                            <div className="status-timer-section-head-main">
                                                <Typography variant="subtitle1" className="status-timer-section-title">
                                                    {section.title}
                                                </Typography>
                                                <Typography variant="body2" className="status-timer-section-desc">
                                                    {section.description}
                                                </Typography>
                                            </div>
                                            <div className="status-timer-section-count">{section.cards.length} 张</div>
                                        </div>

                                        <div className="status-timer-section-body">
                                            {section.cards.length === 0 ? (
                                                <div className="status-timer-section-empty">
                                                    {section.emptyText}
                                                </div>
                                            ) : (
                                                <div className="status-timers-grid">
                                                    {section.cards.map((timer) => (
                                                        <StatusTimerCard
                                                            key={`${timer.timer_kind}-${timer.session_id}`}
                                                            timer={timer}
                                                            displayTimezone={displayTimezone}
                                                            nowMs={nowMs}
                                                            resetHint={Boolean(resetHintMap[`${timer.timer_kind}-${timer.session_id}`])}
                                                        />
                                                    ))}
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </Box>
    );
}

// 暴露为全局视图组件，供应用入口按当前路由态切换展示。
window.StatusView = StatusView;
})();

