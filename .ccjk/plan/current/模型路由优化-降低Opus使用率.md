# 功能规划：模型路由优化 - 降低 Opus 使用率

## 概述

- **功能目标**：将 Opus 使用率从当前 40-60% 降低至 5-10%
- **预期价值**：提升 Sonnet 并发能力，降低成本 50-70%
- **影响范围**：`api_server.py` 中的 `MODEL_ROUTING_CONFIG` 配置

---

## 当前问题分析

| 配置项 | 当前值 | 问题 |
|--------|--------|------|
| `first_turn_opus_probability` | 90% | 几乎所有新对话都用 Opus |
| `main_agent_opus_probability` | 60% | 主对话流 60% 概率触发 |
| `base_opus_probability` | 30% | 即使简单任务也有 30% 概率 |
| `force_opus_keywords` | 30+ 个 | 覆盖大量日常操作 |

**累积效应**：实际 Opus 使用率约 40-60%

---

## 功能分解

- [ ] 阶段 1：配置分析与方案设计
- [ ] 阶段 2：保守优化（目标 20-30% Opus）
- [ ] 阶段 3：激进优化（目标 10-15% Opus）
- [ ] 阶段 4：精细调优（目标 5-10% Opus）
- [ ] 阶段 5：监控与回滚机制

---

## 技术方案

### 三阶段参数调整表

| 参数 | 当前值 | 阶段 2 | 阶段 3 | 阶段 4 |
|------|--------|--------|--------|--------|
| `first_turn_opus_probability` | 90% | 40% | 15% | 5% |
| `main_agent_opus_probability` | 60% | 30% | 10% | 5% |
| `base_opus_probability` | 30% | 15% | 5% | 2% |
| `force_opus_keywords` 数量 | 30+ | 15 | 8 | 5 |
| `execution_phase_sonnet_probability` | 80% | 80% | 85% | 90% |
| **预期 Opus 使用率** | 40-60% | 20-30% | 10-15% | 5-10% |

### 关键词精简策略

**最终保留的 Opus 关键词（5 个）**：
```python
"force_opus_keywords": [
    "创建项目", "create project",      # 完整项目创建
    "系统设计", "system design",        # 系统架构设计
    "架构设计",                         # 架构设计
],
```

**移除的关键词**：
- "分析"、"梳理"、"检查问题" → Sonnet 可胜任
- "设计"（单独）→ 太宽泛
- "规划"、"计划"（单独）→ 保留"整体规划"
- "重构"（单独）→ 普通重构不需要 Opus
- "UI-UX"、"设计稿" → UI 调整不需要 Opus

**新增 Sonnet 关键词**：
- "检查"、"查看"、"确认"、"验证"
- "调试"、"测试"、"debug"、"test"
- "优化"、"改进"、"调优"
- "配置"、"设置"、"部署"、"发布"
- "重构"、"设计"、"规划"、"分析"（普通级别）

---

## 验收标准

### 定量指标
1. **Opus 使用率**：降至 5-10%
2. **成本节约**：50-70%
3. **服务可用性**：保持 99.9%+
4. **错误率**：不超过基线 +5%

### 定性指标
1. Extended Thinking 100% 使用 Opus
2. 架构设计场景 100% 使用 Opus
3. 无明显服务质量投诉

---

## 实施计划

### 快速实施方案（推荐）

由于用户明确希望大幅降低 Opus 使用率，建议直接实施阶段 4 配置：

```python
MODEL_ROUTING_CONFIG = {
    "enabled": True,
    "opus_model": "claude-opus-4-5-20251101",
    "sonnet_model": "claude-sonnet-4-5-20250929",

    # 强制 Opus 场景
    "force_opus_on_thinking": True,           # Extended Thinking 保持 Opus
    "main_agent_opus_probability": 5,         # 60% → 5%

    # 首轮对话
    "first_turn_opus_probability": 5,         # 90% → 5%
    "first_turn_max_user_messages": 2,

    # 执行阶段
    "execution_phase_tool_calls": 5,
    "execution_phase_sonnet_probability": 90, # 80% → 90%

    # 保底概率
    "base_opus_probability": 2,               # 30% → 2%
}
```

### 部署步骤

```bash
# 1. 备份当前配置
cp api_server.py api_server.py.backup

# 2. 应用修改

# 3. 重启服务
bash start.sh

# 4. 验证
curl http://127.0.0.1:8100/
curl -X POST http://127.0.0.1:8100/admin/routing/reset
```

---

## 风险与应对

| 风险 | 应对方案 |
|------|---------|
| 服务质量下降 | 保留 Extended Thinking 强制 Opus；快速回滚机制 |
| 关键词匹配失效 | 监控路由日志，动态调整关键词 |
| 配置漂移 | 更新 CLAUDE.md 文档 |

### 回滚命令
```bash
cp api_server.py.backup api_server.py && bash start.sh
```

---

## 监控命令

```bash
# 查看路由统计
curl http://127.0.0.1:8100/admin/routing/stats

# 重置统计
curl -X POST http://127.0.0.1:8100/admin/routing/reset

# 查看路由决策日志
tail -f /var/log/ai-history-manager.log | grep "模型路由"
```
