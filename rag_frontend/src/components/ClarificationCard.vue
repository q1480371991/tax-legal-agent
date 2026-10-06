<script setup lang="ts">
/**
 * 澄清卡片 —— 追问交互组件。
 *
 * 为什么是一个组件而不是一段 HTML 字符串：
 *   后端原先直接拼 `clarification-container` 的 HTML 塞进 final_answer，前端用
 *   v-html 注入。v-html 的内容不经过 Vue 编译，按钮没法用 @click，只能挂
 *   onclick + window 全局函数 —— 绕开了组件的生命周期管理，实测踩过两个 bug：
 *     · 提交后误删全局回调 → 后续卡片全部点不动
 *     · 提交后漏重置 isLoading → 界面永远停在"处理中"
 *   改成组件后，事件绑定与状态都归 Vue 管，这类问题从结构上不会再有。
 *
 * 后端只负责产出结构化数据（见 SSE 的 clarification 事件 / 非流式响应的
 * metadata.clarification_request），渲染完全在前端 —— 前后端分离。
 */
import { ref, nextTick, onMounted } from 'vue'
import { Lightbulb } from 'lucide-vue-next'

export interface ClarificationData {
  type?: string
  question: string
  suggestions?: string[]
  reason?: string
  required?: boolean
  placeholder?: string
}

const props = defineProps<{
  data: ClarificationData
  /** 已作答 / 已失效时禁用交互，避免重复提交 */
  disabled?: boolean
}>()

const emit = defineEmits<{
  /** 用户点选某个建议，或输入了自定义问题 */
  (e: 'select', text: string): void
  /** 用户点了"稍后再说"（仅非必答时出现） */
  (e: 'dismiss'): void
}>()

const customInput = ref('')
const inputRef = ref<HTMLInputElement | null>(null)

function choose(text: string) {
  const value = (text || '').trim()
  if (!value || props.disabled) return
  emit('select', value)
}

function submitCustom() {
  choose(customInput.value)
}

onMounted(() => {
  // 自动聚焦，用户可以键盘直接打字作答
  nextTick(() => inputRef.value?.focus())
})
</script>

<template>
  <div class="clarification-container" :class="{ 'is-disabled': disabled }">
    <div v-if="data.reason" class="reason-badge">
      <Lightbulb :size="14" />
      <span>{{ data.reason }}</span>
    </div>

    <div class="question-text">{{ data.question }}</div>

    <div v-if="data.suggestions?.length" class="suggestions">
      <button
        v-for="(s, i) in data.suggestions"
        :key="i"
        type="button"
        class="suggestion-btn"
        :disabled="disabled"
        @click="choose(s)"
      >{{ s }}</button>
    </div>

    <div class="custom-input-section">
      <input
        ref="inputRef"
        v-model="customInput"
        type="text"
        class="clarification-input"
        :disabled="disabled"
        :placeholder="data.placeholder || '或者直接输入您的具体问题...'"
        @keyup.enter="submitCustom"
      />
      <button type="button" class="submit-btn" :disabled="disabled" @click="submitCustom">发送</button>
    </div>

    <button
      v-if="data.required === false"
      type="button"
      class="dismiss-btn"
      :disabled="disabled"
      @click="emit('dismiss')"
    >稍后再说</button>
  </div>
</template>

<style scoped>
@keyframes clarificationPulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.7; }
}

.clarification-container {
  background: linear-gradient(135deg, #2563eb 0%, #0891b2 100%);
  border-radius: 16px;
  padding: 24px;
  color: white;
  margin: 16px 0;
  box-shadow: 0 10px 40px rgba(6, 145, 178, 0.3);
  animation: clarificationPulse 2s ease-in-out infinite;
}

/* 已作答：停止呼吸动画，视觉上"冷却"下来，避免诱导用户重复点击 */
.clarification-container.is-disabled {
  animation: none;
  opacity: 0.75;
}

.reason-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(255, 255, 255, 0.15);
  padding: 8px 16px;
  border-radius: 20px;
  font-size: 14px;
  margin-bottom: 16px;
  width: fit-content;
}

.question-text {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 16px;
  line-height: 1.5;
}

.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 20px;
}

.suggestion-btn {
  background: rgba(255, 255, 255, 0.2);
  border: 1px solid rgba(255, 255, 255, 0.4);
  color: white;
  padding: 12px 20px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s ease;
  font-size: 14px;
  font-weight: 500;
}

.suggestion-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.3);
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
}

.custom-input-section {
  display: flex;
  gap: 10px;
  margin-top: 16px;
}

.clarification-input {
  flex: 1;
  padding: 12px 16px;
  border: 1px solid rgba(255, 255, 255, 0.3);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.1);
  color: white;
  font-size: 14px;
  outline: none;
  transition: all 0.2s ease;
}

.clarification-input::placeholder {
  color: rgba(255, 255, 255, 0.6);
}

.clarification-input:focus {
  border-color: rgba(255, 255, 255, 0.6);
  background: rgba(255, 255, 255, 0.15);
}

.submit-btn {
  padding: 12px 24px;
  background: rgba(255, 255, 255, 0.9);
  color: #0891b2;
  border: none;
  border-radius: 8px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s ease;
}

.submit-btn:hover:not(:disabled) {
  background: white;
  transform: scale(1.02);
}

.dismiss-btn {
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.3);
  color: rgba(255, 255, 255, 0.8);
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s ease;
  margin-top: 12px;
}

.dismiss-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.1);
  color: white;
}

button:disabled,
input:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
</style>
