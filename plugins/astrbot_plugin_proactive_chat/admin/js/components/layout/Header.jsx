(() => {
/**
 * 文件职责：顶部栏组件，负责标题展示、时钟显示、连接状态指示与主题切换入口。
 */
 
 const { Box, Typography, IconButton } = MaterialUI;
 const { useState, useEffect } = React;

function RealTimeClock({ timeZone }) {
    const [timeStr, setTimeStr] = useState('');

    useEffect(() => {
        const updateTime = () => {
            // 头部时钟始终使用统一的格式化工具，确保与状态页、任务页时间显示风格一致。
            setTimeStr(formatDateTime(new Date(), timeZone || 'Asia/Shanghai', {
                includeYear: true,
                includeSeconds: true,
            }));
        };

        updateTime();
        // 每秒刷新一次时钟文本；组件卸载时清理定时器，避免后台泄漏。
        const timer = setInterval(updateTime, 1000);
        return () => clearInterval(timer);
    }, [timeZone]);

    // 初始尚未生成时间字符串时先不渲染，避免短暂出现占位空壳。
    if (!timeStr) return null;

    return (
        <div className="header-clock-chip">
            <span className="header-clock-label">当前时间 🕒</span>
            <span className="header-clock-value">{timeStr}</span>
        </div>
    );
}

function Header({ currentView }) {
    const { state, dispatch } = useAppContext();
    const { config, status } = state;
    // 若配置中未单独指定展示时区，则默认按插件主要使用场景的东八区展示。
    const displayTimezone = config?.displayTimezone || 'Asia/Shanghai';

    const toggleTheme = () => {
        dispatch({ type: 'TOGGLE_THEME' });
    };

    // 视图 key 到标题文案的映射集中维护，避免 JSX 中散落条件判断。
    const viewTitles = {
        status: '运行状态',
        tasks: '任务管理',
        notifications: '通知中心',
        docs: '文档浏览',
        config: '配置管理',
    };

    // Header 不直接感知底层 socket 实例，只消费后端状态中的连接计数结果。
    const wsCount = Number(status?.ws_connections ?? 0);
    const wsConnected = wsCount > 0;

    return (
        <>
            <div className="top-bar">
                <Typography variant="h5" sx={{
                    fontWeight: 800,
                    color: 'text.primary',
                    letterSpacing: '-0.5px'
                }}>
                    {viewTitles[currentView] || viewTitles.status}
                </Typography>

                <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                    <RealTimeClock timeZone={displayTimezone} />

                    {/* 连接状态胶囊用于快速反馈实时通道是否可用。 */}
                    <div className={`connection-chip ${wsConnected ? 'is-connected' : 'is-disconnected'}`}>
                        <div className="connection-chip-dot"></div>
                        <Typography variant="body2" className="connection-chip-text">
                            {wsConnected ? '已连接' : '未连接'}
                        </Typography>
                    </div>

                    <IconButton
                        onClick={toggleTheme}
                        sx={{
                            width: 44,
                            height: 44,
                            background: 'var(--md-sys-color-surface)',
                            border: '1px solid var(--glass-border)',
                            boxShadow: '0 2px 8px rgba(0,0,0,0.05)',
                            '&:hover': { background: 'var(--md-sys-color-surface-variant)' }
                        }}
                    >
                        <span style={{ fontSize: '18px' }}>
                            {/* 图标语义与即将切换到的模式对应，帮助用户快速理解当前状态。 */}
                            {state.theme === 'dark' ? '🌞' : '🌙'}
                        </span>
                    </IconButton>
                </Box>
            </div>
        </>
    );
}

// 暴露到全局，供入口应用直接渲染 Header。
window.Header = Header;
})();

