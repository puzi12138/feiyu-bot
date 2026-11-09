/**
 * 文件职责：API 访问 Hook，封装后端接口调用并提供稳定引用的方法集合。
 */

function useApi() {
    // 将每个接口都包装成稳定引用的回调，避免依赖它们的 effect / memo 无谓重跑。
    const getStatus = React.useCallback(() => window.HttpUtil.get('/api/status'), []);
    const getConfig = React.useCallback(() => window.HttpUtil.get('/api/config'), []);
    const getConfigSchema = React.useCallback(() => window.HttpUtil.get('/api/config-schema'), []);
    const updateConfig = React.useCallback(
        // 全局配置保存接口，传入完整 payload 由后端进行白名单字段更新。
        (payload) => window.HttpUtil.post('/api/config', payload),
        []
    );

    const listSessions = React.useCallback(
        // 拉取所有已知会话及其覆写状态，用于会话差异配置页与主应用首载。
        () => window.HttpUtil.get('/api/session-config/sessions'),
        []
    );
    const getSessionConfig = React.useCallback(
        // 会话 ID 可能包含特殊字符，因此路径参数必须先进行 encodeURIComponent。
        (session) => window.HttpUtil.get(`/api/session-config/${encodeURIComponent(session)}`),
        []
    );
    const updateSessionConfig = React.useCallback(
        // 同一个接口同时支持 override 与 effective 两种保存模式，由 payload.mode 区分。
        (session, payload) =>
            window.HttpUtil.post(`/api/session-config/${encodeURIComponent(session)}`, payload),
        []
    );
    const resetSessionConfig = React.useCallback(
        // 删除会话覆写后，该会话会重新完全继承全局配置。
        (session) => window.HttpUtil.del(`/api/session-config/${encodeURIComponent(session)}`),
        []
    );

    const listJobs = React.useCallback(() => window.HttpUtil.get('/api/jobs'), []);
    const listMarkdownFiles = React.useCallback(
        () => window.HttpUtil.get('/api/markdown-files'),
        []
    );
    const getMarkdownFile = React.useCallback(
        (path) => window.HttpUtil.get(`/api/markdown-files/${encodeURIComponent(path)}`),
        []
    );
    const getNotifications = React.useCallback(
        () => window.HttpUtil.get('/api/notifications'),
        []
    );
    const readNotification = React.useCallback(
        (id) => window.HttpUtil.post('/api/notifications/read', { id }),
        []
    );
    const readAllNotifications = React.useCallback(
        () => window.HttpUtil.post('/api/notifications/read-all', {}),
        []
    );
    const refreshNotifications = React.useCallback(
        () => window.HttpUtil.post('/api/notifications/refresh', {}),
        []
    );
    const triggerJob = React.useCallback(
        // “立即触发”接口本质上是一个无参 POST，因此 body 传空对象保持请求格式统一。
        (session) => window.HttpUtil.post(`/api/jobs/${encodeURIComponent(session)}/trigger`, {}),
        []
    );
    const cancelJob = React.useCallback(
        (session) => window.HttpUtil.del(`/api/jobs/${encodeURIComponent(session)}`),
        []
    );
    const rescheduleJob = React.useCallback(
        (session) => window.HttpUtil.post(`/api/jobs/${encodeURIComponent(session)}/reschedule`, {}),
        []
    );

    return React.useMemo(
        () => ({
            // 通过 useMemo 返回统一 API 对象，确保消费方在依赖比较时具备稳定引用。
            getStatus,
            getConfig,
            getConfigSchema,
            updateConfig,
            listSessions,
            getSessionConfig,
            updateSessionConfig,
            resetSessionConfig,
            listJobs,
            listMarkdownFiles,
            getMarkdownFile,
            getNotifications,
            readNotification,
            readAllNotifications,
            refreshNotifications,
            triggerJob,
            cancelJob,
            rescheduleJob,
        }),
        [
            getStatus,
            getConfig,
            getConfigSchema,
            updateConfig,
            listSessions,
            getSessionConfig,
            updateSessionConfig,
            resetSessionConfig,
            listJobs,
            listMarkdownFiles,
            getMarkdownFile,
            getNotifications,
            readNotification,
            readAllNotifications,
            refreshNotifications,
            triggerJob,
            cancelJob,
            rescheduleJob,
        ]
    );
}

// 暴露到全局，供未经过 ESModule 打包的其余 JSX 文件直接调用。
window.useApi = useApi;

