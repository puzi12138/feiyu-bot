(() => {
/**
 * 文件职责：配置渲染核心组件，基于 Schema 动态生成表单并处理保存/会话差异配置逻辑。
 */
 
 const { Box, TextField, Switch, Typography, Button, Accordion, AccordionSummary, AccordionDetails, Paper, Chip, Slider, MenuItem, ToggleButton, ToggleButtonGroup } = MaterialUI;
 const { useState, useEffect, useRef, useLayoutEffect } = React;

// 递归扫描 Schema 中所有 object 节点，生成“全部展开 / 收起”功能需要的路径列表。
const getAllExpandablePaths = (schema, prefix = '') => {
    let paths = [];
    Object.entries(schema).forEach(([key, value]) => {
        const currentPath = prefix ? `${prefix}.${key}` : key;
        if (value.type === 'object' && value.items) {
            paths.push(currentPath);
            paths = paths.concat(getAllExpandablePaths(value.items, currentPath));
        }
    });
    return paths;
};

// 为常见配置组与字段定义 emoji 图标，提升配置页的可扫读性。
const CONFIG_ICONS = {
    friend_settings: '👤',
    group_settings: '👥',
    web_admin: '🌐',
    auto_trigger_settings: '🤖',
    schedule_settings: '🕒',
    tts_settings: '🔊',
    segmented_reply_settings: '🔪',
    enable: '✅',
    session_list: '📋',
    proactive_prompt: '🧠',
    group_idle_trigger_minutes: '⏳',
    enable_auto_trigger: '✅',
    auto_trigger_after_minutes: '⏱️',
    min_interval_minutes: '⏱️',
    max_interval_minutes: '⏱️',
    quiet_hours: '🌙',
    max_unanswered_times: '🛑',
    enable_tts: '💬',
    always_send_text: '🔤',
    words_count_threshold: '📏',
    split_mode: 'Ⓜ️',
    regex: '🧩',
    split_words: '📝',
    interval_method: '⏱️',
    interval: '🎲',
    log_base: '📈',
    enabled: '✅',
    host: '📡',
    port: '🔌',
    password: '🔑',
    session_name: '🏷️'
};

// 某些 Schema 描述文本自身已带 emoji；这里剥离前缀，避免界面上图标重复出现。
const LEADING_EMOJI_REGEX = /^\s*(?:\p{Extended_Pictographic}(?:\uFE0F|\u200D\p{Extended_Pictographic})*)\s*/u;

const stripLeadingEmoji = (text) => {
    if (typeof text !== 'string') return text;
    return text.replace(LEADING_EMOJI_REGEX, '').trimStart();
};

/**
 * 配置字段渲染组件
 * 根据后端返回的 Schema 动态渲染不同类型的输入控件
 */
function ConfigField({ fieldKey, schema, value, onChange, depth = 0, path = '', expandedKeys = [], onToggleExpand = () => {} }) {
    // hidden 字段完全不渲染，通常用于后端保留字段或不希望前端直接编辑的项。
    if (schema.hidden) return null;

    // 对输入控件使用一层本地态，便于处理 Slider / 文本输入中的过渡值。
    const [localValue, setLocalValue] = useState(value);

    useEffect(() => {
        // 当外部配置值变化（如切换会话 / 回滚 / 重新加载）时，同步重置局部编辑态。
        setLocalValue(value);
    }, [value]);

    const handleChange = (newValue) => {
        // 所有字段都通过统一出口把最新值同时写入本地态与父级配置对象。
        setLocalValue(newValue);
        onChange(newValue);
    };

    const rawTitle = schema.description || fieldKey;
    const titleText = stripLeadingEmoji(rawTitle) || rawTitle;
    const icon = CONFIG_ICONS[fieldKey] || '⚙️';
    const currentPath = path ? `${path}.${fieldKey}` : fieldKey;

    // object + items 表示一个可折叠的配置分组，内部再递归渲染子字段。
    if (schema.type === 'object' && schema.items) {
        const isExpanded = expandedKeys.includes(currentPath);

        return (
            <Paper
                elevation={depth === 0 ? 2 : 0}
                sx={{
                    my: depth === 0 ? 1 : 0.75,
                    overflow: 'hidden',
                    border: depth === 0 ? 1.5 : 1,
                    borderColor: depth === 0 ? 'primary.main' : 'primary.light',
                    borderRadius: 2,
                    background: depth === 0
                        ? 'linear-gradient(135deg, rgba(0, 90, 193, 0.04) 0%, rgba(0, 90, 193, 0.01) 100%)'
                        : 'transparent'
                }}
            >
                <Accordion
                    expanded={isExpanded}
                    onChange={() => onToggleExpand(currentPath)}
                    elevation={0}
                    sx={{
                        '&:before': { display: 'none' },
                        bgcolor: 'transparent'
                    }}
                >
                    <AccordionSummary
                        expandIcon={
                            <Box sx={{ fontSize: '14px', lineHeight: 1 }}>
                                {isExpanded ? '▲' : '▼'}
                            </Box>
                        }
                        sx={{
                            px: 2,
                            py: 1,
                            minHeight: '48px !important',
                            bgcolor: depth === 0 ? 'rgba(0, 90, 193, 0.06)' : depth === 1 ? 'rgba(0, 90, 193, 0.02)' : 'transparent',
                            '&:hover': {
                                bgcolor: depth === 0 ? 'rgba(0, 90, 193, 0.1)' : 'rgba(0, 90, 193, 0.05)'
                            },
                            transition: 'all 0.2s ease',
                            '& .MuiAccordionSummary-expandIconWrapper': {
                                transform: 'none !important'
                            },
                            '& .MuiAccordionSummary-expandIconWrapper.Mui-expanded': {
                                transform: 'none !important'
                            }
                        }}
                    >
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flex: 1 }}>
                            <Box sx={{ fontSize: depth === 0 ? '20px' : '16px' }}>{icon}</Box>
                            <Box sx={{ flex: 1 }}>
                                <Typography
                                    variant="subtitle2"
                                    sx={{
                                        fontWeight: 700,
                                        color: depth === 0 ? 'primary.main' : depth > 0 ? 'primary.dark' : 'text.primary',
                                        fontSize: depth === 0 ? '0.95rem' : '0.875rem'
                                    }}
                                >
                                    {titleText}
                                </Typography>
                                {schema.hint && depth === 0 && (
                                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.25 }}>
                                        {schema.hint}
                                    </Typography>
                                )}
                            </Box>
                            {depth === 0 && (
                                <Chip
                                    label={`${Object.keys(schema.items).length}项`}
                                    size="small"
                                    color="primary"
                                    variant="outlined"
                                    sx={{ height: 22, fontSize: '0.7rem' }}
                                />
                            )}
                            {depth > 0 && (
                                <Typography variant="caption" sx={{ color: 'primary.main', fontWeight: 600, mr: 0.5 }}>
                                    {isExpanded ? '收起' : '展开'}
                                </Typography>
                            )}
                        </Box>
                    </AccordionSummary>
                    <AccordionDetails sx={{ px: 3, py: 0, bgcolor: 'background.default' }}>
                        <Box sx={{ display: 'flex', flexDirection: 'column' }}>
                            {Object.entries(schema.items).map(([key, subSchema]) => (
                                <ConfigField
                                    key={key}
                                    fieldKey={key}
                                    schema={subSchema}
                                    value={localValue?.[key]}
                                    onChange={(newValue) => handleChange({ ...localValue, [key]: newValue })}
                                    depth={depth + 1}
                                    path={currentPath}
                                    expandedKeys={expandedKeys}
                                    onToggleExpand={onToggleExpand}
                                />
                            ))}
                        </Box>
                    </AccordionDetails>
                </Accordion>
            </Paper>
        );
    }

    // 通用左侧描述区，统一标题与 hint 的排版逻辑，减少各字段类型重复 JSX。
    const DescriptionSection = ({ flex = 8 }) => (
        <Box sx={{ flex: flex, pr: 2, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700, color: 'text.primary', fontSize: '0.9rem', lineHeight: 1.3 }}>
                {schema.description || fieldKey}
            </Typography>
            {schema.hint && (
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5, lineHeight: 1.3 }}>
                    {schema.hint}
                </Typography>
            )}
        </Box>
    );

    // 布尔配置映射为开关组件，适合表示 enable / always_send_text 这类 on-off 项。
    if (schema.type === 'bool' || schema.type === 'boolean') {
        return (
            <Box sx={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                py: 1.5,
                borderBottom: depth === 0 ? 'none' : '1px solid',
                borderColor: 'divider',
                '&:last-child': { borderBottom: 'none' }
            }}>
                <DescriptionSection flex={8} />
                <Box sx={{ flex: 2, display: 'flex', justifyContent: 'flex-end', alignItems: 'center' }}>
                    <Switch
                        checked={localValue !== undefined ? localValue : (schema.default || false)}
                        onChange={(e) => handleChange(e.target.checked)}
                        sx={{
                            width: 52,
                            height: 32,
                            padding: 0,
                            '& .MuiSwitch-switchBase': {
                                padding: 0,
                                margin: '4px',
                                transitionDuration: '300ms',
                                '&.Mui-checked': {
                                    transform: 'translateX(20px)',
                                    color: '#fff',
                                    '& + .MuiSwitch-track': {
                                        backgroundColor: 'primary.main',
                                        opacity: 1,
                                        border: 0,
                                    },
                                },
                            },
                            '& .MuiSwitch-thumb': {
                                boxSizing: 'border-box',
                                width: 24,
                                height: 24,
                                boxShadow: '0 2px 4px rgba(0, 0, 0, 0.2)',
                            },
                            '& .MuiSwitch-track': {
                                borderRadius: 16,
                                backgroundColor: 'rgba(0, 0, 0, 0.12)',
                                opacity: 1,
                                transition: 'background-color 300ms',
                            },
                        }}
                    />
                </Box>
            </Box>
        );
    }

    // 数值字段支持“滑杆 + 数字输入框”的双通道编辑，兼顾直观性与精确输入。
    if (['integer', 'int', 'number', 'float', 'double'].includes(schema.type)) {
        const sliderConfig = schema.slider;
        const hasRange = sliderConfig !== undefined;
        // 如果有范围，展示 Slider，布局 5:3:2；否则回退到 8:2
        const descFlex = hasRange ? 5 : 8;
        
        const min = sliderConfig?.min ?? schema.minimum;
        const max = sliderConfig?.max ?? schema.maximum;
        const step = sliderConfig?.step ?? (schema.type === 'integer' || schema.type === 'int' ? 1 : 0.1);
        const fallbackNumber = schema.default ?? min ?? 0;
        const parsedLocalNumber = Number(localValue);
        const sliderValueRaw = Number.isFinite(parsedLocalNumber) ? parsedLocalNumber : Number(fallbackNumber);
        const sliderValue = Math.min(
            max ?? sliderValueRaw,
            Math.max(min ?? sliderValueRaw, sliderValueRaw)
        );

        return (
            <Box sx={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                py: 1.5,
                borderBottom: depth === 0 ? 'none' : '1px solid',
                borderColor: 'divider',
                '&:last-child': { borderBottom: 'none' }
            }}>
                <DescriptionSection flex={descFlex} />
                
                {hasRange && (
                    <Box sx={{ flex: 3, px: 2, display: 'flex', alignItems: 'center' }}>
                        <Slider
                            value={sliderValue}
                            onChange={(e, newValue) => setLocalValue(newValue)}
                            onChangeCommitted={(e, newValue) => handleChange(newValue)}
                            min={min}
                            max={max}
                            step={step}
                            size="small"
                            valueLabelDisplay="auto"
                            sx={{ color: 'primary.main' }}
                        />
                    </Box>
                )}

                <Box sx={{ flex: 2, display: 'flex', justifyContent: 'flex-end' }}>
                    <TextField
                        fullWidth
                        size="small"
                        type="number"
                        value={localValue !== undefined ? localValue : (schema.default || 0)}
                        onChange={(e) => {
                            const v = e.target.value;
                            // 允许输入空值或负号
                            if (v === '' || v === '-') {
                                setLocalValue(v);
                                return;
                            }
                            const num = schema.type === 'integer' || schema.type === 'int' ? parseInt(v) : parseFloat(v);
                            handleChange(isNaN(num) ? 0 : num);
                        }}
                        inputProps={{
                            min: min,
                            max: max,
                            step: step
                        }}
                        variant="outlined"
                        sx={{
                            '& .MuiOutlinedInput-root': {
                                borderRadius: 1.5,
                                bgcolor: 'background.paper',
                                '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
                            }
                        }}
                    />
                </Box>
            </Box>
        );
    }

    // 列表字段使用“每行一项”的多行文本输入，适合编辑 session_list / split_words 等数组数据。
    if (schema.type === 'list' || schema.type === 'array') {
        return (
            <Box sx={{
                py: 1.5,
                borderBottom: depth === 0 ? 'none' : '1px solid',
                borderColor: 'divider',
                '&:last-child': { borderBottom: 'none' }
            }}>
                <Box sx={{ mb: 1 }}>
                    <Typography variant="subtitle2" sx={{ fontWeight: 700, color: 'text.primary', fontSize: '0.9rem' }}>
                        {schema.description || fieldKey}
                    </Typography>
                    {schema.hint && (
                        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                            {schema.hint}
                        </Typography>
                    )}
                </Box>
                <TextField
                    fullWidth
                    multiline
                    rows={3}
                    size="small"
                    value={Array.isArray(localValue) ? localValue.join('\n') : ''}
                    onChange={(e) => handleChange(e.target.value.split('\n'))}
                    placeholder="每行一项"
                    variant="outlined"
                    sx={{
                        '& .MuiOutlinedInput-root': {
                            borderRadius: 1.5,
                            bgcolor: 'background.paper',
                            '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
                        }
                    }}
                />
            </Box>
        );
    }

    // 若 Schema 给出了 options，则优先渲染为下拉选择框，而不是自由文本输入。
    if (schema.options && Array.isArray(schema.options)) {
        const optionLabels = Array.isArray(schema.labels) ? schema.labels : [];
        const optionLabelMap = new Map(
            schema.options.map((option, index) => [option, optionLabels[index] || option])
        );
        const currentValue = localValue !== undefined ? localValue : (schema.default || '');

        return (
            <Box sx={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                py: 1.5,
                borderBottom: depth === 0 ? 'none' : '1px solid',
                borderColor: 'divider',
                '&:last-child': { borderBottom: 'none' }
            }}>
                <DescriptionSection flex={8} />
                <Box sx={{ flex: 2, display: 'flex', justifyContent: 'flex-end' }}>
                    <TextField
                        select
                        fullWidth
                        size="small"
                        value={currentValue}
                        onChange={(e) => handleChange(e.target.value)}
                        variant="outlined"
                        SelectProps={{
                            renderValue: (selected) => optionLabelMap.get(selected) || selected,
                            MenuProps: {
                                PaperProps: {
                                    sx: {
                                        borderRadius: 1.5,
                                        mt: 0.5,
                                        boxShadow: '0 4px 16px rgba(0,0,0,0.1)'
                                    }
                                }
                            }
                        }}
                        sx={{
                            '& .MuiOutlinedInput-root': {
                                borderRadius: 1.5,
                                bgcolor: 'background.paper',
                                '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
                            },
                            '& .MuiSelect-select': {
                                textAlign: 'center',
                                pr: '32px !important' // 给箭头图标留出空间，确保视觉居中
                            }
                        }}
                    >
                        {schema.options.map((option) => (
                            <MenuItem key={option} value={option} sx={{ justifyContent: 'center' }}>
                                {optionLabelMap.get(option) || option}
                            </MenuItem>
                        ))}
                    </TextField>
                </Box>
            </Box>
        );
    }

    // 其余情况按字符串处理，并依据字段语义决定是否切换为多行文本框。
    const shouldBeMultiline =
        schema.type === 'text' ||
        (schema.hint && schema.hint.length > 100) ||
        fieldKey.includes('prompt') ||
        fieldKey.includes('format') ||
        fieldKey.includes('template') ||
        fieldKey.includes('pattern') ||
        fieldKey.includes('message') ||
        fieldKey.includes('body') ||
        fieldKey.includes('content');
    const multilineRows =
        fieldKey.includes('prompt') || schema.type === 'text'
            ? 8
            : 4;
    
    // 如果是长文本，保持上下布局；否则使用 8:2 布局
    if (shouldBeMultiline) {
        return (
            <Box sx={{
                py: 1.75,
                borderBottom: depth === 0 ? 'none' : '1px solid',
                borderColor: 'divider',
                '&:last-child': { borderBottom: 'none' }
            }}>
                <Box sx={{ mb: 1.25 }}>
                    <Typography variant="subtitle2" sx={{ fontWeight: 700, color: 'text.primary', fontSize: '0.95rem', lineHeight: 1.35 }}>
                        {schema.description || fieldKey}
                    </Typography>
                    {schema.hint && (
                        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5, lineHeight: 1.5 }}>
                            {schema.hint}
                        </Typography>
                    )}
                </Box>
                <TextField
                    fullWidth
                    multiline
                    minRows={multilineRows}
                    maxRows={fieldKey.includes('prompt') || schema.type === 'text' ? 18 : 10}
                    value={localValue !== undefined ? localValue : (schema.default || '')}
                    onChange={(e) => handleChange(e.target.value)}
                    placeholder={fieldKey.includes('prompt') ? '请输入该会话/全局场景下的 Prompt 内容…' : ''}
                    variant="outlined"
                    sx={{
                        '& .MuiOutlinedInput-root': {
                            alignItems: 'flex-start',
                            borderRadius: 2,
                            bgcolor: 'background.paper',
                            fontFamily: 'inherit',
                            lineHeight: 1.65,
                            '& textarea': {
                                lineHeight: 1.7,
                                fontSize: '0.95rem',
                                resize: 'vertical'
                            },
                            '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
                        }
                    }}
                />
            </Box>
        );
    }

    return (
        <Box sx={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            py: 1.5,
            borderBottom: depth === 0 ? 'none' : '1px solid',
            borderColor: 'divider',
            '&:last-child': { borderBottom: 'none' }
        }}>
            <DescriptionSection flex={8} />
            <Box sx={{ flex: 2, display: 'flex', justifyContent: 'flex-end' }}>
                <TextField
                    fullWidth
                    size="small"
                    value={localValue !== undefined ? localValue : (schema.default || '')}
                    onChange={(e) => handleChange(e.target.value)}
                    variant="outlined"
                    sx={{
                        '& .MuiOutlinedInput-root': {
                            borderRadius: 1.5,
                            bgcolor: 'background.paper',
                            '&.Mui-focused > fieldset': { borderColor: 'primary.main' }
                        }
                    }}
                />
            </Box>
        </Box>
    );
}

// 根据会话 ID 的消息类型片段判断是私聊还是群聊，用于筛选对应的配置 Schema。
function detectSessionType(sessionId) {
    const raw = String(sessionId || '');
    if (raw.includes(':GroupMessage:') || raw.includes(':GuildMessage:')) return 'group';
    if (raw.includes(':FriendMessage:') || raw.includes(':PrivateMessage:')) return 'friend';
    return 'friend';
}

// 会话差异配置页并不暴露完整全局 Schema，而是按会话类型抽取可编辑字段子集。
// 因此像 web_admin、notification_settings 这类纯全局配置块不会出现在会话差异编辑视图中。
function getSessionSchemaEntries(schema, sessionType) {
    const rootKey = sessionType === 'group' ? 'group_settings' : 'friend_settings';
    const rootItems = schema?.[rootKey]?.items || {};
    const orderedKeys = [
        'auto_trigger_settings',
        'group_idle_trigger_minutes',
        'proactive_prompt',
        'context_settings',
        'schedule_settings',
        'tts_settings',
        'segmented_reply_settings',
    ];
    const hiddenKeys = new Set(['enable', 'session_list']);

    const entries = [];
    const sessionNameSchema = rootItems.session_name || {
        type: 'string',
        default: '',
        description: '会话备注名',
        hint: '用于日志和管理端展示。为空时将回退显示 UMO。',
    };
    entries.push(['session_name', sessionNameSchema]);

    orderedKeys
        .filter((key) => !hiddenKeys.has(key) && rootItems[key])
        .forEach((key) => {
            entries.push([key, rootItems[key]]);
        });

    return entries;
}

/**
 * 主配置渲染器组件
 * 负责加载、显示和保存插件的完整配置
 * 包含了配置的获取、状态管理和保存逻辑
 */
function ConfigRenderer() {
    const { state } = useAppContext(); // 获取全局状态
    // schema: 后端返回的配置结构定义；config: 当前正在编辑的配置草稿。
    const [schema, setSchema] = useState(null);
    const [config, setConfig] = useState(null);
    const [expandedKeys, setExpandedKeys] = useState([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [saveFeedback, setSaveFeedback] = useState({ type: '', text: '' });

    // 多会话模式相关状态：global 表示编辑全局配置，session 表示编辑单会话差异配置。
    const [mode, setMode] = useState('global'); // global | session
    const [sessions, setSessions] = useState([]);
    const [selectedSession, setSelectedSession] = useState('');
    const [sessionLoading, setSessionLoading] = useState(false);
    const [sessionConfigState, setSessionConfigState] = useState({ baseAvailable: true, message: '' });

    const api = useApi();
    const scrollContainerRef = useRef(null);
    const loadConfigSeqRef = useRef(0);
    const isDirtyRef = useRef(false); // 标记用户是否有未保存的修改

    // 动态缓存 key：不同模式 / 不同会话使用不同 localStorage 键，避免草稿串线。
    const getDraftKey = (currentMode = mode, currentSession = selectedSession) => {
        if (currentMode === 'session' && currentSession) {
            return `astrbot_plugin_proactive_draft_config_session_${currentSession}`;
        }
        return 'astrbot_plugin_proactive_draft_config_global';
    };

    const getExpandedKey = (currentMode = mode, currentSession = selectedSession) => {
        if (currentMode === 'session' && currentSession) {
            return `astrbot_plugin_proactive_expanded_keys_session_${currentSession}`;
        }
        return 'astrbot_plugin_proactive_expanded_keys_global';
    };

    const getScrollKey = (currentMode = mode, currentSession = selectedSession) => {
        if (currentMode === 'session' && currentSession) {
            return `astrbot_scroll_config_list_session_${currentSession}`;
        }
        return 'astrbot_scroll_config_list_global';
    };

    useEffect(() => {
        // 首次进入页面时只初始化一次：加载 Schema，并准备会话列表。
        initializePage();
    }, []);

    useEffect(() => {
        if (!schema) return;
        // 切换模式或切换会话时，需要重新从服务端拉取当前上下文下的配置快照。
        setSaveFeedback({ type: '', text: '' });
        loadConfig(mode, selectedSession);
    }, [mode, selectedSession]);

    // 使用 useLayoutEffect 恢复滚动位置，尽量让用户在绘制完成前回到上次浏览位置。
    useLayoutEffect(() => {
        if (!loading && scrollContainerRef.current) {
            const savedPos = localStorage.getItem(getScrollKey());
            if (savedPos) {
                // 尝试恢复
                const pos = parseInt(savedPos, 10);
                if (pos > 0) {
                    scrollContainerRef.current.scrollTop = pos;
                    // 双重保障
                    requestAnimationFrame(() => {
                        if (scrollContainerRef.current) {
                            scrollContainerRef.current.scrollTop = pos;
                        }
                    });
                }
            }
        }
    }, [loading, mode, selectedSession]);

    // 监听滚动并节流写入 localStorage，便于大配置表单下次打开时恢复阅读位置。
    useEffect(() => {
        const el = scrollContainerRef.current;
        if (!el || loading) return;

        const handleScroll = () => {
            localStorage.setItem(getScrollKey(), el.scrollTop);
        };

        let timeout;
        const debouncedScroll = () => {
            clearTimeout(timeout);
            timeout = setTimeout(handleScroll, 100);
        };

        el.addEventListener('scroll', debouncedScroll);
        return () => {
            el.removeEventListener('scroll', debouncedScroll);
            clearTimeout(timeout);
        };
    }, [loading, mode, selectedSession]);

    // 自动保存草稿，但只有发生本地修改时才写入，避免把服务端新值反向覆盖成本地草稿。
    useEffect(() => {
        if (config && isDirtyRef.current) {
            localStorage.setItem(getDraftKey(), JSON.stringify(config));
        }
    }, [config, mode, selectedSession]);

    // 记忆折叠 / 展开状态，让用户在大型 Schema 中保持稳定的浏览结构。
    useEffect(() => {
        if (schema) { // 确保 schema 加载后再保存，避免初始化时的空状态覆盖
            localStorage.setItem(getExpandedKey(), JSON.stringify(expandedKeys));
        }
    }, [expandedKeys, schema, mode, selectedSession]);

    const initializePage = async () => {
        setLoading(true);
        try {
            // 初始化时优先拿 Schema；没有 Schema 就无法安全地动态构建表单。
            const schemaData = await api.getConfigSchema();
            setSchema(schemaData);

            await loadSessions();
        } catch (e) {
            console.error('初始化配置页失败', e);
            // showToast('初始化配置页失败,请检查控制台', 'error');
        } finally {
            setLoading(false);
        }
    };

    const loadSessions = async () => {
        try {
            // 会话列表用于会话差异配置模式下的下拉框、状态提示与覆写标记展示。
            const result = await api.listSessions();
            const sessionList = result?.sessions || [];
            
            // 确保 sessionList 中的每一项都是包含 session, has_override 的对象形式
            const normalizedSessions = sessionList.map(item =>
                typeof item === 'string' ? { session: item, has_override: false } : item
            );
            
            setSessions(normalizedSessions);

            // 默认选中第一个会话
            if (!selectedSession && normalizedSessions.length > 0) {
                setSelectedSession(normalizedSessions[0].session);
            }
        } catch (e) {
            console.error('加载会话列表失败', e);
            // showToast('加载会话列表失败,请检查控制台', 'error');
        }
    };

    const loadConfig = async (currentMode = mode, currentSession = selectedSession) => {
        // 所有配置加载都以当前 Schema 为前提，避免出现“无结构定义的盲编辑”。
        if (!schema) return;

        // 递增请求序号，防止快速切换会话时后返回的旧请求覆盖新状态。
        const requestSeq = ++loadConfigSeqRef.current;
        setLoading(true);
        try {
            let configData = null;

            if (currentMode === 'session') {
                if (!currentSession) {
                    if (requestSeq === loadConfigSeqRef.current) {
                        setConfig(null);
                        setSessionConfigState({ baseAvailable: false, message: '请先选择一个会话' });
                    }
                    return;
                }
                setSessionLoading(true);
                const sessionData = await api.getSessionConfig(currentSession);
                const hasBaseConfig = Boolean(sessionData?.base);
                setSessionConfigState(
                    hasBaseConfig
                        ? { baseAvailable: true, message: '' }
                        : { baseAvailable: false, message: '该会话尚未命中对应类型的全局 session_list，因此暂时无法保存会话差异配置。请先在对应的全局配置中加入该会话。' }
                );
                configData = sessionData?.effective || sessionData?.override || {};
            } else {
                setSessionConfigState({ baseAvailable: true, message: '' });
                configData = await api.getConfig();
            }

            // 如果不是最新请求，丢弃过期响应，避免会话串改
            if (requestSeq !== loadConfigSeqRef.current) {
                return;
            }

            // 1. 配置加载以服务端返回值为准
            // 旧版本这里会无条件恢复本地草稿，导致真实已保存配置被默认草稿覆盖
            // 因此改为始终优先使用服务端配置；草稿仅在当前编辑会话中用于暂存，不参与初始化覆盖
            const finalConfig = configData;
            isDirtyRef.current = false;

            // 2. 处理展开状态记忆
            const cachedExpandedStr = localStorage.getItem(getExpandedKey(currentMode, currentSession));
            let finalExpandedKeys = [];
            if (cachedExpandedStr) {
                try {
                    finalExpandedKeys = JSON.parse(cachedExpandedStr);
                } catch (e) {
                    console.error('解析展开状态失败', e);
                }
            } else {
                // 默认全展开
                finalExpandedKeys = getAllExpandablePaths(schema);
            }

            setConfig(finalConfig);
            setExpandedKeys(finalExpandedKeys);
        } catch (e) {
            if (requestSeq === loadConfigSeqRef.current) {
                console.error('加载配置失败', e);
                // showToast('加载配置失败,请检查控制台', 'error');
                setConfig(null);
            }
        } finally {
            if (requestSeq === loadConfigSeqRef.current) {
                setSessionLoading(false);
                setLoading(false);
            }
        }
    };

    const handleToggleExpand = (path) => {
        // 单个折叠面板的展开态切换逻辑集中处理，便于后续扩展批量控制。
        setExpandedKeys(prev => {
            if (prev.includes(path)) {
                return prev.filter(p => p !== path);
            } else {
                return [...prev, path];
            }
        });
    };

    const handleToggleAll = () => {
        // 会话模式只对会话可编辑字段做“全部展开”，不污染全局 Schema 的路径集合。
        const expandableSchema = mode === 'session'
            ? Object.fromEntries(sessionSchemaEntries)
            : schema;
        if (expandedKeys.length > 0) {
            setExpandedKeys([]); // 全部收起
        } else {
            setExpandedKeys(getAllExpandablePaths(expandableSchema)); // 全部展开
        }
    };

    const cleanConfig = (obj) => {
        // 保存前递归清理配置：数组中的字符串去首尾空白，并去掉空项，减小脏数据概率。
        if (Array.isArray(obj)) {
            return obj
                .map(item => typeof item === 'string' ? item.trim() : item)
                .filter(item => item !== '');
        }
        if (obj && typeof obj === 'object') {
            const newObj = {};
            for (const key in obj) {
                newObj[key] = cleanConfig(obj[key]);
            }
            return newObj;
        }
        return obj;
    };

    const handleSave = async () => {
        // 保存逻辑按 mode 分叉：全局配置直接写配置块，会话模式则保存差异配置。
        setSaving(true);
        try {
            const cleanedConfig = cleanConfig(config);
            const currentMode = mode;
            const currentSession = selectedSession;

            if (currentMode === 'session') {
                if (!currentSession) {
                    // showToast('请先选择会话', 'warning');
                    return;
                }
                const response = await api.updateSessionConfig(currentSession, {
                    mode: 'effective',
                    effective: cleanedConfig
                });
                isDirtyRef.current = false;
                localStorage.removeItem(getDraftKey(currentMode, currentSession));
                setConfig(response?.effective || cleanedConfig);
                setSaveFeedback({ type: 'success', text: '会话差异配置已保存' });
                await loadSessions();
                // 会话模式保存后强制回读服务端 effective，确保会话隔离与覆写状态显示正确
                await loadConfig(currentMode, currentSession);
            } else {
                const payload = {
                    friend_settings: cleanedConfig.friend_settings,
                    group_settings: cleanedConfig.group_settings,
                    web_admin: cleanedConfig.web_admin,
                };
                await api.updateConfig(payload);
                isDirtyRef.current = false;
                setConfig(cleanedConfig); // 全局模式可直接更新界面
                localStorage.removeItem(getDraftKey(currentMode, currentSession)); // 已保存，清除草稿
                setSaveFeedback({ type: 'success', text: '全局配置已保存' });
            }
        } catch (e) {
            console.error('保存配置失败', e);
            setSaveFeedback({ type: 'error', text: e?.message || '保存配置失败，请检查后端返回信息' });
            window.alert(e?.message || '保存配置失败，请检查后端返回信息');
        } finally {
            setSaving(false);
        }
    };

    const handleResetOverride = async () => {
        // 仅会话模式下可用：清空后该会话重新完全继承全局配置。
        if (!selectedSession) {
            // showToast('请先选择会话', 'warning');
            return;
        }

        const ok = confirm('确定要清空该会话的差异配置吗？\n\n清空后将完全继承全局默认配置。');
        if (!ok) return;

        setSaving(true);
        try {
            await api.resetSessionConfig(selectedSession);
            localStorage.removeItem(getDraftKey(mode, selectedSession));
            setSaveFeedback({ type: 'success', text: '会话差异配置已清空' });
            await loadSessions();
            await loadConfig(mode, selectedSession);
        } catch (e) {
            console.error('清空会话差异配置失败', e);
            setSaveFeedback({ type: 'error', text: e?.message || '清空会话差异配置失败' });
            window.alert(e?.message || '清空会话差异配置失败');
        } finally {
            setSaving(false);
        }
    };

    if (loading) {
        return (
            <Box sx={{ textAlign: 'center', py: 6 }}>
                <Box sx={{ fontSize: '36px', mb: 1.5 }}>⚙️</Box>
                <Typography variant="body1" color="text.secondary">加载配置中...</Typography>
            </Box>
        );
    }

    if (!schema || !config) {
        return (
            <Box sx={{ textAlign: 'center', py: 6 }}>
                <Box sx={{ fontSize: '36px', mb: 1.5 }}>❌</Box>
                <Typography variant="body1" color="error">无法加载配置</Typography>
            </Box>
        );
    }

    // 以下派生变量统一在渲染前计算，避免 JSX 中出现过多内联判断，降低维护复杂度。
    const selectedSessionMeta = sessions.find(s => (s.session || s) === selectedSession);
    const hasOverride = selectedSessionMeta?.has_override;
    const currentSessionType = detectSessionType(selectedSession);
    const sessionSchemaEntries = mode === 'session' ? getSessionSchemaEntries(schema, currentSessionType) : [];
    const schemaEntries = mode === 'session' ? sessionSchemaEntries : Object.entries(schema);
    const sessionEnabled = mode === 'session' ? Boolean(config?.enable) : false;
    const sessionTypeLabel = currentSessionType === 'group' ? '群聊' : '私聊';
    const canSaveSessionConfig = mode !== 'session' || sessionConfigState.baseAvailable;

    return (
        <Box sx={{ display: 'flex', flexDirection: 'column', flex: 1, overflow: 'hidden' }}>
            {/* 顶部控制区：负责在“全局配置”和“会话差异配置”两种编辑模式间切换。 */}
            <Box sx={{ px: 3, pt: 2, pb: 1, display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
                    <ToggleButtonGroup
                        exclusive
                        size="small"
                        value={mode}
                        onChange={(e, val) => {
                            if (!val) return;
                            setMode(val);
                        }}
                    >
                        <ToggleButton value="global">全局配置</ToggleButton>
                        <ToggleButton value="session">会话差异配置</ToggleButton>
                    </ToggleButtonGroup>

                    {mode === 'session' && (
                        <>
                            <TextField
                                select
                                size="small"
                                label="目标会话"
                                value={selectedSession}
                                onChange={(e) => setSelectedSession(e.target.value)}
                                sx={{ minWidth: 420, maxWidth: '100%' }}
                            >
                                {sessions.map((item) => {
                                    const displayName = item.session_display_name || item.session_name || item.session;
                                    const hasAlias = Boolean(item.session_name && item.session_name.trim());
                                    const optionText = hasAlias ? `${displayName}（${item.session}）` : displayName;
                                    return (
                                        <MenuItem key={item.session} value={item.session}>
                                            {optionText}
                                        </MenuItem>
                                    );
                                })}
                            </TextField>
                            {hasOverride && (
                                <Chip size="small" color="primary" label="已存在差异覆写" />
                            )}
                            {sessionLoading && (
                                <Typography variant="caption" color="text.secondary">会话配置加载中...</Typography>
                            )}
                        </>
                    )}
                </Box>

                {mode === 'session' && selectedSessionMeta && (
                    <Typography variant="caption" color="text.secondary">
                        当前会话：{selectedSessionMeta.session_display_name || selectedSessionMeta.session_name || selectedSessionMeta.session || selectedSessionMeta}
                        {selectedSessionMeta.session_name ? ` ｜ UMO：${selectedSessionMeta.session}` : ''}
                        {' ｜ '}未回复次数：{selectedSessionMeta.unanswered_count ?? 0}
                    </Typography>
                )}
                {mode === 'session' && !sessionConfigState.baseAvailable && sessionConfigState.message && (
                    <Box
                        sx={{
                            px: 1.5,
                            py: 1,
                            borderRadius: 2,
                            border: '1px solid',
                            borderColor: 'rgba(245, 124, 0, 0.24)',
                            background: 'rgba(245, 124, 0, 0.08)',
                        }}
                    >
                        <Typography
                            variant="caption"
                            sx={{
                                display: 'block',
                                fontWeight: 600,
                                color: '#ED6C02',
                                lineHeight: 1.6,
                            }}
                        >
                            {sessionConfigState.message}
                        </Typography>
                    </Box>
                )}
                {saveFeedback.text && (
                    <Box
                        sx={{
                            px: 1.5,
                            py: 1,
                            borderRadius: 2,
                            border: '1px solid',
                            borderColor: saveFeedback.type === 'success' ? 'rgba(46, 125, 50, 0.22)' : 'rgba(211, 47, 47, 0.22)',
                            background: saveFeedback.type === 'success' ? 'rgba(46, 125, 50, 0.08)' : 'rgba(211, 47, 47, 0.08)',
                        }}
                    >
                        <Typography
                            variant="caption"
                            sx={{
                                display: 'block',
                                fontWeight: 600,
                                color: saveFeedback.type === 'success' ? '#2E7D32' : '#D32F2F'
                            }}
                        >
                            {saveFeedback.text}
                        </Typography>
                    </Box>
                )}
            </Box>

            {/* 主编辑区：根据 schemaEntries 动态递归渲染所有当前可见配置项。 */}
            <Box
                ref={scrollContainerRef}
                sx={{
                    flex: 1,
                    overflowY: 'auto',
                    px: 3,
                    py: 2,
                '&::-webkit-scrollbar': {
                    width: '6px',
                },
                '&::-webkit-scrollbar-track': {
                    bgcolor: 'background.default',
                    borderRadius: 3,
                },
                '&::-webkit-scrollbar-thumb': {
                    bgcolor: 'primary.main',
                    borderRadius: 3,
                    '&:hover': {
                        bgcolor: 'primary.dark',
                    }
                }
            }}>
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                    {mode === 'session' && config && (
                        <Box sx={{ pb: 0.5 }}>
                            <Paper
                                elevation={0}
                                sx={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'space-between',
                                    gap: 2,
                                    px: 2.5,
                                    py: 1.75,
                                    borderRadius: 2.5,
                                    border: '1px solid',
                                    borderColor: sessionEnabled ? 'rgba(103, 80, 164, 0.22)' : 'rgba(244, 67, 54, 0.18)',
                                    background: sessionEnabled
                                        ? 'linear-gradient(135deg, rgba(103, 80, 164, 0.08) 0%, rgba(103, 80, 164, 0.03) 100%)'
                                        : 'linear-gradient(135deg, rgba(244, 67, 54, 0.08) 0%, rgba(244, 67, 54, 0.03) 100%)'
                                }}
                            >
                                <Box sx={{ minWidth: 0 }}>
                                    <Typography variant="subtitle1" sx={{ fontWeight: 800, color: 'text.primary', mb: 0.25 }}>
                                        {sessionTypeLabel}会话启用状态
                                    </Typography>
                                    <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.5 }}>
                                        这是当前会话的独立开关。关闭后，该会话会暂停主动消息，但不会影响同类型的全局配置与其他会话。
                                    </Typography>
                                </Box>
                                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, flexShrink: 0 }}>
                                    <Chip
                                        size="small"
                                        color={sessionEnabled ? 'success' : 'default'}
                                        label={sessionEnabled ? '已启用' : '已暂停'}
                                        variant={sessionEnabled ? 'filled' : 'outlined'}
                                    />
                                    <Switch
                                        checked={sessionEnabled}
                                        onChange={(e) => {
                                            isDirtyRef.current = true;
                                            setConfig({ ...config, enable: e.target.checked });
                                        }}
                                    />
                                </Box>
                            </Paper>
                        </Box>
                    )}
                    {schemaEntries.map(([key, subSchema]) => (
                        <Box key={key} sx={{ width: '100%' }}>
                            <ConfigField
                                fieldKey={key}
                                schema={subSchema}
                                value={config[key]}
                                onChange={(newValue) => { isDirtyRef.current = true; setConfig({ ...config, [key]: newValue }); }}
                                path=""
                                expandedKeys={expandedKeys}
                                onToggleExpand={handleToggleExpand}
                            />
                        </Box>
                    ))}
                </Box>
            </Box>

            {/* 底部操作栏：集中放置折叠控制、恢复默认、撤销、清空覆写与保存操作。 */}
            <Box sx={{
                flexShrink: 0,
                bgcolor: 'var(--md-sys-color-surface)', // 使用主题表面色
                backdropFilter: 'blur(8px)',
                borderTop: '1px solid',
                borderColor: 'divider',
                px: 3,
                py: 2,
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                boxShadow: '0 -4px 20px rgba(0, 0, 0, 0.05)',
                zIndex: 10
            }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 500 }}>
                        {schemaEntries.length} 个配置组
                    </Typography>
                    <Button
                        onClick={handleToggleAll}
                        size="small"
                        variant="text"
                        sx={{ minWidth: 'auto', px: 1 }}
                    >
                        {expandedKeys.length > 0 ? '全部收起' : '全部展开'}
                    </Button>
                </Box>
                <Box sx={{ display: 'flex', gap: 1.5 }}>
                    <Button
                        onClick={() => {
                            if (confirm('⚠️ 确定要恢复出厂设置吗？\n\n这将覆盖当前所有配置项为默认值（需要点击“保存配置”才能生效）。')) {
                                const generateDefaults = (s) => {
                                    const c = {};
                                    Object.entries(s).forEach(([k, v]) => {
                                        if (v.type === 'object' && v.items) {
                                            c[k] = generateDefaults(v.items);
                                        } else {
                                            c[k] = v.default !== undefined ? v.default : null;
                                        }
                                    });
                                    return c;
                                };

                                if (mode === 'session') {
                                    const sessionDefaults = { enable: true };
                                    schemaEntries.forEach(([k, v]) => {
                                        if (v.type === 'object' && v.items) {
                                            sessionDefaults[k] = generateDefaults(v.items);
                                        } else {
                                            sessionDefaults[k] = v.default !== undefined ? v.default : null;
                                        }
                                    });
                                    isDirtyRef.current = true;
                                    setConfig(sessionDefaults);
                                } else {
                                    const defaults = generateDefaults(schema);
                                    isDirtyRef.current = true;
                                    setConfig(defaults);
                                }
                                localStorage.removeItem(getDraftKey());
                            }
                        }}
                        disabled={saving}
                        variant="outlined"
                        color="error"
                        size="medium"
                        startIcon={<span>🗑️</span>}
                        sx={{
                            minWidth: 100,
                            borderRadius: 2,
                            borderWidth: 1.5,
                            fontSize: '0.875rem',
                            '&:hover': { borderWidth: 1.5 }
                        }}
                    >
                        恢复默认
                    </Button>
                    <Button
                        onClick={() => {
                            if (confirm('确定要撤销所有未保存的更改吗？\n\n这将重新加载服务器上已保存的配置。')) {
                                localStorage.removeItem(getDraftKey());
                                loadConfig();
                            }
                        }}
                        disabled={saving}
                        variant="outlined"
                        size="medium"
                        startIcon={<span>↩️</span>}
                        sx={{
                            minWidth: 100,
                            borderRadius: 2,
                            borderWidth: 1.5,
                            fontSize: '0.875rem',
                            '&:hover': { borderWidth: 1.5 }
                        }}
                    >
                        撤销更改
                    </Button>
                    {mode === 'session' && (
                        <Button
                            onClick={handleResetOverride}
                            disabled={saving || !selectedSession}
                            variant="outlined"
                            color="warning"
                            size="medium"
                            startIcon={<span>♻️</span>}
                            sx={{
                                minWidth: 130,
                                borderRadius: 2,
                                borderWidth: 1.5,
                                fontSize: '0.875rem',
                                '&:hover': { borderWidth: 1.5 }
                            }}
                        >
                            清空会话覆写
                        </Button>
                    )}
                    <Button
                        variant="contained"
                        onClick={handleSave}
                        disabled={saving || !canSaveSessionConfig}
                        size="medium"
                        startIcon={<span>💾</span>}
                        sx={{
                            minWidth: 120,
                            borderRadius: 2,
                            fontSize: '0.875rem',
                            boxShadow: '0 2px 8px rgba(0, 90, 193, 0.3)',
                            '&:hover': {
                                boxShadow: '0 4px 12px rgba(0, 90, 193, 0.4)',
                            }
                        }}
                    >
                        {saving ? '保存中...' : (!canSaveSessionConfig && mode === 'session' ? '当前会话不可保存' : (mode === 'session' ? '保存会话配置' : '保存配置'))}
                    </Button>
                </Box>
            </Box>
        </Box>
    );
}

window.ConfigRenderer = ConfigRenderer;
})();

