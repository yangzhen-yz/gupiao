<template>
  <div class="pagination">
    <button
      class="page-btn"
      :disabled="modelValue <= 1"
      @click="go(modelValue - 1)"
    >‹ 上一页</button>
    <span class="page-info">第 {{ modelValue }} / {{ totalPages }} 页</span>
    <button
      class="page-btn"
      :disabled="modelValue >= totalPages"
      @click="go(modelValue + 1)"
    >下一页 ›</button>
    <span class="page-total">共 {{ total }} 条</span>
  </div>
</template>

<script setup>
const props = defineProps({
  modelValue: { type: Number, required: true },
  total: { type: Number, default: 0 },
  totalPages: { type: Number, default: 1 },
})
const emit = defineEmits(['update:modelValue'])

function go(p) {
  if (p < 1 || p > props.totalPages) return
  emit('update:modelValue', p)
}
</script>

<style scoped>
.pagination {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 0.25rem 0.25rem;
  font-size: 0.8125rem;
  color: var(--text-secondary, #52525b);
  flex-wrap: wrap;
}
.page-btn {
  padding: 0.25rem 0.6rem;
  border: 1px solid var(--border-color, #e4e4e7);
  background: var(--bg-card, #fff);
  color: var(--text-secondary, #52525b);
  border-radius: var(--radius-sm, 6px);
  cursor: pointer;
  font-size: 0.8125rem;
  transition: all 0.15s;
}
.page-btn:hover:not(:disabled) {
  border-color: var(--accent-primary, #2563eb);
  color: var(--accent-primary, #2563eb);
}
.page-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.page-info {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  min-width: 6.5rem;
  text-align: center;
}
.page-total {
  margin-left: auto;
  color: var(--text-tertiary, #a1a1aa);
  font-size: 0.75rem;
}
</style>
