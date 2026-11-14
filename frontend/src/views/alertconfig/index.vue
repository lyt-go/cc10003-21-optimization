<template>
  <section class="page" data-module="alertconfig">
    <header class="page-head">
      <div>
        <h2>指标预警管理</h2>
        <p class="page-desc">规则绑定指标口径版本，按级别、阈值边界、持续周期与生效时间裁决冲突，同一指标同一周期只形成一条预警。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记预警规则</button>
        <button class="btn" type="button" @click="exportRows">导出指标预警清单</button>
      </div>
    </header>

    <div class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </div>

    <!-- 规则维护 -->
    <template v-if="activeTab === 'rules'">
      <div class="stat-row">
        <article v-for="item in stats" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>

      <form class="filter-bar" @submit.prevent="reload">
        <label v-for="field in filterFields" :key="field" class="filter-item">
          <span>{{ field }}</span>
          <input v-model="filters[field]" :placeholder="`按${field}检索`" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in columns" :key="column">{{ column }}</th>
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)">
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>{{ row.status ?? '—' }}</td>
            <td class="row-actions">
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 2" class="empty-state">暂无指标预警数据，可先登记预警规则</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ total }} 条指标预警记录</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <!-- 冲突裁决 -->
    <template v-else>
      <div class="toolbar">
        <button class="btn primary" type="button" :disabled="running" @click="runArbitration(false)">
          {{ running ? '裁决中…' : '执行裁决' }}
        </button>
        <button class="btn" type="button" :disabled="running" @click="runArbitration(true)">强制重裁</button>
        <button class="btn ghost" type="button" @click="loadAdjudication">刷新</button>
        <span v-if="consistency" class="badge" :class="consistency['一致'] ? 'badge-ok' : 'badge-warn'">
          一致性自检：{{ consistency['一致'] ? '通过' : '存在不一致' }}
        </span>
        <span class="mono">状态 {{ stateRows.length }} · 通知 {{ notificationRows.length }} · 冲突 {{ conflictRows.length }}</span>
      </div>

      <div class="panel">
        <h3 class="panel-title">预警状态（每指标每周期唯一，携带裁决版本与被吸收规则身份）</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>指标</th><th>周期</th><th>口径版本</th><th>结论</th><th>胜出规则</th>
              <th>级别</th><th>连续周期</th><th>结论分布</th><th>吸收与冲突</th><th>裁决版本</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="state in stateRows" :key="`${state['指标编号']}-${state['周期']}`">
              <td>{{ state['指标编号'] }}</td>
              <td>{{ state['周期'] }}</td>
              <td>{{ state['口径版本'] }}</td>
              <td>
                <span class="badge" :class="state['是否预警'] ? 'badge-trigger' : 'badge-normal'">
                  {{ state['是否预警'] ? '预警' : '正常' }}
                </span>
              </td>
              <td>{{ state['胜出预警编号'] ?? '—' }}</td>
              <td>{{ state['预警级别'] ?? '—' }}</td>
              <td>{{ state['连续周期数'] }}</td>
              <td>
                <span
                  v-for="(count, label) in state['结论汇总']"
                  :key="String(label)"
                  class="badge"
                  :class="badgeClass(String(label))"
                  style="margin-right: 4px"
                >{{ label }} {{ count }}</span>
              </td>
              <td>
                <ul v-if="state['被吸收规则']?.length || state['冲突记录']?.length" class="sub-list">
                  <li v-for="item in state['被吸收规则']" :key="`a-${item['规则id']}`">
                    吸收 {{ item['预警编号'] }}：{{ item['吸收原因'] }}
                  </li>
                  <li v-for="item in state['冲突记录']" :key="`c-${item.id}`">
                    冲突 {{ item['落选预警编号'] }}（{{ item['裁决依据'] }}）
                  </li>
                </ul>
                <span v-else class="mono">无</span>
              </td>
              <td class="mono">{{ state['裁决版本'] }}</td>
            </tr>
            <tr v-if="!stateRows.length">
              <td colspan="10" class="empty-state">尚未执行裁决，点击「执行裁决」生成预警状态</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3 class="panel-title">通知清单（与预警状态读取同一裁决版本）</h3>
        <table class="data-table">
          <thead>
            <tr><th>指标</th><th>周期</th><th>口径版本</th><th>预警规则</th><th>接收人</th><th>级别</th><th>吸收说明</th><th>裁决版本</th></tr>
          </thead>
          <tbody>
            <tr v-for="notice in notificationRows" :key="`${notice['指标编号']}-${notice['周期']}`">
              <td>{{ notice['指标编号'] }}</td>
              <td>{{ notice['周期'] }}</td>
              <td>{{ notice['口径版本'] }}</td>
              <td>{{ notice['预警编号'] }}</td>
              <td>{{ notice['通知接收人'] }}</td>
              <td>{{ notice['预警级别'] }}</td>
              <td>
                <ul v-if="notice['吸收说明']?.length" class="sub-list">
                  <li v-for="line in notice['吸收说明']" :key="line">{{ line }}</li>
                </ul>
                <span v-else class="mono">无</span>
              </td>
              <td class="mono">{{ notice['裁决版本'] }}</td>
            </tr>
            <tr v-if="!notificationRows.length">
              <td colspan="8" class="empty-state">暂无通知</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3 class="panel-title">同级规则冲突留痕</h3>
        <table class="data-table">
          <thead>
            <tr><th>指标</th><th>周期</th><th>口径版本</th><th>胜出规则</th><th>落选规则</th><th>共同级别</th><th>裁决依据</th><th>时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="conflict in conflictRows" :key="conflict.id">
              <td>{{ conflict['指标编号'] }}</td>
              <td>{{ conflict['周期'] }}</td>
              <td>{{ conflict['口径版本'] }}</td>
              <td><span class="badge badge-conflict">{{ conflict['胜出预警编号'] }}</span></td>
              <td>{{ conflict['落选预警编号'] }}</td>
              <td>{{ conflict['共同级别'] }}</td>
              <td>{{ conflict['裁决依据'] }}</td>
              <td class="mono">{{ conflict['记录时间'] }}</td>
            </tr>
            <tr v-if="!conflictRows.length">
              <td colspan="8" class="empty-state">暂无同级冲突</td>
            </tr>
          </tbody>
        </table>
      </div>

      <footer class="page-foot">
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type DictRow = Record<string, any>

const ENDPOINT = '/api/alertconfig'
const columns = ['预警编号', '关联指标', '口径版本', '比较运算符', '阈值数值', '持续周期数', '预警级别', '通知接收人', '生效时间', '修订版本']
const actions = ['提交规则评审', '生效规则', '关闭规则']
const stats = ref([
  { label: '预警规则总数', value: 0 },
  { label: '已生效规则', value: 0 },
  { label: '规则评审中', value: 0 },
])

const tabs = [
  { key: 'rules', label: '预警规则' },
  { key: 'arbitration', label: '冲突裁决' },
] as const
type TabKey = (typeof tabs)[number]['key']
const activeTab = ref<TabKey>('rules')

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const stateRows = ref<DictRow[]>([])
const notificationRows = ref<DictRow[]>([])
const conflictRows = ref<DictRow[]>([])
const consistency = ref<DictRow | null>(null)
const running = ref(false)

const BADGE_MAP: Record<string, string> = {
  触发: 'badge-trigger',
  吸收: 'badge-absorbed',
  复用: 'badge-reused',
  冲突: 'badge-conflict',
  未命中: 'badge-normal',
}

function badgeClass(label: string): string {
  return BADGE_MAP[label] ?? 'badge-normal'
}

function switchTab(key: TabKey) {
  activeTab.value = key
  if (key === 'arbitration') {
    void loadAdjudication()
  } else {
    void reload()
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '预警规则登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('指标预警动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标预警操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('预警规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((item) => item.status === '已生效').length
    stats.value[2].value = rows.value.filter((item) => item.status === '规则评审中').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标预警列表读取失败'
  }
}

async function loadAdjudication() {
  errorMessage.value = ''
  try {
    const [stateRes, noticeRes, conflictRes, consistencyRes] = await Promise.all([
      request(`${ENDPOINT}/arbitration/states`),
      request(`${ENDPOINT}/arbitration/notifications`),
      request(`${ENDPOINT}/arbitration/conflicts`),
      request(`${ENDPOINT}/arbitration/consistency`),
    ])
    if (!stateRes.ok || !noticeRes.ok || !conflictRes.ok || !consistencyRes.ok) {
      throw new Error('裁决数据读取失败')
    }
    stateRows.value = (await stateRes.json()).items ?? []
    notificationRows.value = (await noticeRes.json()).items ?? []
    conflictRows.value = (await conflictRes.json()).items ?? []
    consistency.value = await consistencyRes.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '裁决数据读取失败'
  }
}

async function runArbitration(force: boolean) {
  running.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/arbitration/run`, {
      method: 'POST',
      body: JSON.stringify({ force }),
    })
    if (!response.ok) {
      throw new Error('裁决执行失败')
    }
    await loadAdjudication()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '裁决执行失败'
  } finally {
    running.value = false
  }
}

onMounted(reload)
</script>
