/**
 * 多智能体「流程面板」的展示文案映射。
 *
 * 为什么要单独一层，而不是直接显示后端枚举值：
 *   后端返回的是内部标识（tax_calculation / multi_specialist_parallel），
 *   它是【契约】不是【文案】。直接显示给用户会有两个问题：
 *     1. 用户看不懂（tax_calculation 是什么？）
 *     2. 后端一改枚举名，UI 就跟着变，展示与契约被绑死
 *   所以这里做的是【面向用户的解释性映射】，不是简单翻译：
 *   每个意图额外给一句"这意味着什么"，让用户明白系统为什么这样判断。
 *   映射表命不中时回退到原始值，保证后端新增枚举不会显示成空白。
 */

/** 意图 → 中文标签 + 一句话解释 */
export const INTENT_DISPLAY: Record<string, { label: string; hint: string }> = {
  // ── 闲聊 / 元问题 ──
  greeting: { label: '问候', hint: '打招呼，无需检索知识库' },
  chit_chat: { label: '闲聊', hint: '非业务问题，直接回答' },
  // ── 知识检索 ──
  knowledge_query: { label: '知识问答', hint: '从企业知识库检索事实作答' },
  document_search: { label: '文档查找', hint: '定位相关文档或条款' },
  data_extraction: { label: '信息提取', hint: '从文档中抽取指定字段' },
  // ── 税务 ──
  tax_calculation: { label: '税额计算', hint: '计算应纳税额或适用税率' },
  tax_planning: { label: '税务筹划', hint: '给出税负优化方案' },
  tax_compliance: { label: '税务合规', hint: '检查申报与发票的合规性' },
  tax_declaration: { label: '纳税申报', hint: '申报流程与填报指导' },
  // ── 财务 ──
  financial_analysis: { label: '财务分析', hint: '分析报表与经营指标' },
  accounting_query: { label: '会计处理', hint: '会计分录与账务处理' },
  cost_control: { label: '成本管控', hint: '成本结构与优化建议' },
  investment_advisory: { label: '投资咨询', hint: '投资可行性与回报测算' },
  // ── 法务 ──
  contract_review: { label: '合同审查', hint: '审查条款与法律风险' },
  legal_consultation: { label: '法律咨询', hint: '解读法规、给出合规建议' },
  compliance_check: { label: '合规检查', hint: '核对是否满足监管要求' },
  ip_protection: { label: '知识产权', hint: '商标专利相关问题' },
  // ── 综合 ──
  risk_analysis: { label: '风险分析', hint: '识别与评估潜在风险' },
  report_generation: { label: '报告生成', hint: '产出结构化分析报告' },
  complex_task: { label: '复杂任务', hint: '需要多步骤拆解处理' },
  multi_specialist: { label: '跨领域问题', hint: '需要多位专家协同' },
  unknown: { label: '意图不明', hint: '信息不足，需要向你确认' },
}

/** 路由策略 → 中文标签 + 解释（用户最关心的是"你打算怎么处理"） */
export const ROUTING_DISPLAY: Record<string, { label: string; hint: string }> = {
  direct_answer: { label: '直接回答', hint: '不检索，直接作答' },
  rag_retrieval: { label: '知识库检索', hint: '检索企业文档后作答（不派专家）' },
  single_specialist: { label: '单专家', hint: '交由一位领域专家处理' },
  multi_specialist_parallel: { label: '多专家并行', hint: '多位专家同时分析后综合' },
  report_queue: { label: '报告队列', hint: '转入后台生成完整报告' },
  // 注：multi_specialist_sequential 已从后端移除（专家间无依赖，并行即可）
}

/** 复杂度 → 标签（后端 low/medium/high/very_high） */
export const COMPLEXITY_DISPLAY: Record<string, string> = {
  low: '简单',
  medium: '中等',
  high: '复杂',
  very_high: '高度复杂',
}

/** 专家标识 → 中文名（含带 _specialist 后缀的节点名，两种都要覆盖） */
export const SPECIALIST_DISPLAY: Record<string, string> = {
  finance: '财务专家',
  tax: '税务专家',
  legal: '法律专家',
  finance_specialist: '财务专家',
  tax_specialist: '税务专家',
  legal_specialist: '法律专家',
}

/** 反思审核结果（后端 reflection_result 的形状） */
export interface ReflectionInfo {
  /** 综合质量分 0~1 */
  score?: number
  /** 是否达到可接受阈值 */
  acceptable?: boolean
  /** 质量审查异常时的降级标记 */
  skipped?: boolean
  /** 已重做次数 */
  retry_count?: number
  /** 重做上限 */
  max_retries?: number
  /** 各维度问题（可能来自 LLM 的结构化 issues，也可能是字符串） */
  issues?: Array<{ dimension?: string; severity?: string; description?: string; suggestion?: string } | string>
}

/** 把任意形状的 reflection_result 规整成 ReflectionInfo，避免把对象显示成 [object Object] */
export function normalizeReflection(raw: unknown): ReflectionInfo | null {
  if (!raw) return null
  if (typeof raw === 'string') {
    // 兼容历史数据：早先把对象 String() 后存进了本地会话快照
    return raw ? { issues: [raw] } : null
  }
  if (typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const issues = Array.isArray(o.issues) ? (o.issues as ReflectionInfo['issues']) : undefined
  return {
    score: typeof o.score === 'number' ? o.score : undefined,
    acceptable: typeof o.acceptable === 'boolean' ? o.acceptable : undefined,
    skipped: typeof o.skipped === 'boolean' ? o.skipped : undefined,
    retry_count: typeof o.retry_count === 'number' ? o.retry_count : undefined,
    max_retries: typeof o.max_retries === 'number' ? o.max_retries : undefined,
    issues: issues?.length ? issues : undefined,
  }
}

/** 命不中就回退原值 —— 后端新增枚举时宁可显示英文，也不要显示空白 */
export function intentLabel(v?: string | null): string {
  if (!v) return ''
  return INTENT_DISPLAY[v]?.label || v
}

export function intentHint(v?: string | null): string {
  if (!v) return ''
  return INTENT_DISPLAY[v]?.hint || ''
}

export function routingLabel(v?: string | null): string {
  if (!v) return ''
  return ROUTING_DISPLAY[v]?.label || v
}

export function routingHint(v?: string | null): string {
  if (!v) return ''
  return ROUTING_DISPLAY[v]?.hint || ''
}

export function complexityLabel(v?: string | null): string {
  if (!v) return ''
  return COMPLEXITY_DISPLAY[v] || v
}

export function specialistLabel(v?: string | null): string {
  if (!v) return ''
  return SPECIALIST_DISPLAY[v] || v
}
