/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

// 全局回调：供 v-html 内联的 onclick/onkeyup 调用。
// 用 v-html 注入的 HTML 无法绑定 Vue 事件，只能挂到 window 上由内联属性调用。
declare global {
  interface Window {
    handleClarificationSelect?: (suggestion: string) => void
    handleClarificationSubmit?: () => void
    handleClarificationDismiss?: () => void
  }
}
