<script setup>
import { ref } from 'vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiChip from '@/components/ui/UiChip.vue'
import UiSeg from '@/components/ui/UiSeg.vue'
import UiInput from '@/components/ui/UiInput.vue'
import UiPanel from '@/components/ui/UiPanel.vue'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import NoteCard from '@/components/notes/NoteCard.vue'

const sampleNote = {
  id: 1,
  author: 'agent-alpha',
  kind: 'note',
  body: '用第 4 章做了内容整理，结论写在正文第 4.5 节旁边。',
  createdAt: new Date(Date.now() - 8 * 60 * 1000).toISOString(),
  docId: 'd1',
}
const samplePitfall = {
  id: 2,
  author: 'claude-code',
  kind: 'pitfall',
  body: '附录页码缺失：TOC 解析时页码未知，引用时跳过这几节。',
  createdAt: new Date(Date.now() - 3 * 3600 * 1000).toISOString(),
  docId: 'd1',
}

const seg = ref('all')
const segItems = [
  { value: 'all', label: '全部', count: 11 },
  { value: 'note', label: '笔记', count: 5 },
  { value: 'pitfall', label: '踩坑', count: 4 },
  { value: 'handoff', label: '交接', count: 2 },
]
const text = ref('')
const choice = ref('')
const area = ref('')
const iconOnly = ref('')
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div>
        <h1>UI 组件画廊</h1>
        <p>薄组件层的全部形态。现有页面尚未改动，先确认视觉再逐步替换。</p>
      </div>
    </div>

    <UiPanel title="按钮 · 层级" sub="quiet 是默认，primary 每屏最多一个">
      <div class="row">
        <UiButton variant="primary">发布</UiButton>
        <UiButton>重新索引</UiButton>
        <UiButton variant="ghost">取消</UiButton>
        <UiButton variant="danger">删除</UiButton>
        <UiButton disabled>禁用</UiButton>
      </div>
      <div class="row">
        <UiButton size="sm">小号</UiButton>
        <UiButton size="md">中号</UiButton>
        <UiButton size="lg">大号</UiButton>
        <UiButton size="sm" variant="ghost">小号幽灵</UiButton>
      </div>
    </UiPanel>

    <UiPanel title="胶囊" sub="状态 / 类型 / 可点筛选">
      <div class="row">
        <UiChip>默认</UiChip>
        <UiChip tone="accent">交接</UiChip>
        <UiChip tone="ok">通过</UiChip>
        <UiChip tone="warn">需调整</UiChip>
        <UiChip tone="danger">失败</UiChip>
        <UiChip size="sm">小号</UiChip>
      </div>
      <div class="row">
        <UiChip as="button">可点筛选</UiChip>
        <UiChip as="button" active>已选中</UiChip>
      </div>
    </UiPanel>

    <UiPanel title="分段开关" sub="替换现在的 .seg；没有灰色轨道，只有当前项浮起">
      <div class="row">
        <UiSeg v-model="seg" :items="segItems" />
        <UiSeg v-model="seg" :items="segItems" size="sm" />
      </div>
      <div class="hint">当前值：{{ seg }}</div>
    </UiPanel>

    <UiPanel title="输入 / 下拉 / 多行" sub="focus 是柔光环，不是硬框">
      <div class="grid">
        <UiInput v-model="text" placeholder="普通输入" />
        <UiInput v-model="iconOnly" type="search" icon placeholder="带放大镜的搜索" />
        <UiInput v-model="choice" type="select">
          <option value="">请选择文档</option>
          <option value="a">ACME 100.56-2023</option>
          <option value="b">RPT 7.5-2014</option>
        </UiInput>
        <UiInput v-model="text" size="sm" placeholder="小号输入" />
      </div>
      <UiInput v-model="area" type="textarea" :rows="3" placeholder="多行文本，例如手记正文…" class="stack" />
    </UiPanel>

    <UiPanel title="面板" sub="头 + 体，间距收进组件">
      <template #actions>
        <UiButton size="sm" variant="ghost">全部 →</UiButton>
      </template>
      <div class="hint">面板内容区。列表类内容可用 flush 贴边。</div>
    </UiPanel>

    <UiPanel title="空状态" sub="一句结论 + 一句引导">
      <UiEmpty title="这份文档还没有手记" hint="在右侧写下第一条：做了什么、踩了什么坑，下一位接手前会看到。" />
    </UiPanel>

    <UiPanel title="手记条目" sub="作者徽标 / 类型 / 时间 / 正文的唯一定义处">
      <NoteCard :note="sampleNote" show-doc @open-doc="() => {}" @reply="() => {}">
        <template #actions>
          <button class="kit-link">→ 切片</button>
        </template>
      </NoteCard>
      <div class="sep-line"></div>
      <NoteCard :note="samplePitfall" compact :replyable="false" />
    </UiPanel>

    <UiPanel title="对照：现有写法" sub="当前 main.css 里的 .text-btn / .review-btn / .seg">
      <div class="row">
        <button class="text-btn">回复</button>
        <button class="text-btn">→ 切片</button>
        <button class="review-btn pass">通过</button>
        <button class="review-btn issue">需调整</button>
        <div class="seg">
          <button class="active">全部</button><button>笔记</button><button>踩坑</button>
        </div>
      </div>
    </UiPanel>
  </section>
</template>

<style scoped>
.ui-panel + .ui-panel { margin-top: 14px; }
.stack { margin-top: 8px; }
.row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.row + .row { margin-top: 10px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 8px; }
.hint { font-size: 11.5px; color: var(--faint); margin-top: 8px; }
.sep-line { height: 1px; background: var(--border); margin: 12px 0; }
.kit-link {
  border: 0; background: none; padding: 0; cursor: pointer;
  font-size: 11px; color: var(--muted);
}
.kit-link:hover { color: var(--accent); }
</style>
