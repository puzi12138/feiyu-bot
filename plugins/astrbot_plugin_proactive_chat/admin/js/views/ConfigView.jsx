(() => {
/**
 * 文件职责：配置页视图容器，负责承载配置页头部结构并挂载 ConfigRenderer。
 */
 
 const { Box, Typography } = MaterialUI;

function ConfigView() {
    return (
        // 该视图本身只负责提供页面容器与标题，真正的配置编辑逻辑全部下沉到 ConfigRenderer。
        <Box sx={{ height: '100%' }}>
            <div className="card config-card">
                <div className="config-header">
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                        {/* 左侧彩色竖条用于强化“当前处于配置管理页”的视觉识别。 */}
                        <div style={{ width: '4px', height: '24px', background: 'var(--md-sys-color-primary)', borderRadius: '2px' }}></div>
                        <Typography variant="h6" sx={{ fontWeight: 800, letterSpacing: '-0.5px' }}>配置管理</Typography>
                    </div>
                </div>
                {/* 动态 Schema 解析、草稿缓存、保存与会话差异逻辑都由 ConfigRenderer 负责。 */}
                <ConfigRenderer />
            </div>
        </Box>
    );
}

// 暴露为全局视图组件，供应用入口按 currentView 渲染。
window.ConfigView = ConfigView;
})();

