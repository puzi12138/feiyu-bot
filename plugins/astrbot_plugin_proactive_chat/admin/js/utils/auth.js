/**
 * 文件职责：认证工具模块，负责 token 的读写清理与鉴权请求头拼装。
 */

(function () {
    // 单独定义 token key，避免在多个位置硬编码同一个 localStorage 键名。
    const TOKEN_KEY = 'proactive_admin_token';

    window.AuthUtil = {
        getToken: function () {
            try {
                // 统一从 localStorage 读取访问令牌；读取失败时返回 null 让上层自行兜底。
                return localStorage.getItem(TOKEN_KEY);
            } catch (e) {
                // 某些受限环境下 localStorage 可能不可用，这里静默降级。
                return null;
            }
        },
        setToken: function (token) {
            try {
                // 登录成功后将 token 持久化，供后续页面刷新和 WebSocket 连接复用。
                localStorage.setItem(TOKEN_KEY, token);
            } catch (e) {
                // 存储失败不会阻止当前会话继续使用，只是刷新后需要重新登录。
            }
        },
        clearToken: function () {
            try {
                // token 失效或用户主动退出时清空本地凭据。
                localStorage.removeItem(TOKEN_KEY);
            } catch (e) {
                // 删除失败时无需中断业务流程。
            }
        },
        withAuthHeaders: function (headers) {
            const token = window.AuthUtil.getToken();
            // 始终先复制一份 headers，避免调用方对象被原地修改。
            const base = Object.assign({}, headers || {});
            if (token && token !== 'no-auth') {
                // 仅在真实鉴权场景下注入 Authorization；no-auth 是前后端协商用哨兵值。
                base.Authorization = 'Bearer ' + token;
            }
            return base;
        }
    };
})();

