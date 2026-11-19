# 迷你 Coding Agent（Python / LLM 工具调用）

一个命令行编程助手：**模型决定调用哪个工具 → 程序执行 → 结果回填 → 循环直到任务完成**。
只用 Python 标准库，无三方依赖。

## 快速开始

```bash
# 离线演示（不需要 API key，用脚本化的假模型跑完整循环）
python agent.py --demo

# 跑自检（10 个单元测试，不联网）
python test_agent.py

# 真实调用（OpenAI 兼容接口，DeepSeek 默认）
set LLM_API_KEY=sk-xxxx          # Windows
export LLM_API_KEY=sk-xxxx       # Linux/macOS
python agent.py --workspace ./your_repo --task "找出所有没关闭的文件句柄"
```

可选环境变量：`LLM_BASE_URL`（默认 `https://api.deepseek.com/v1`）、`LLM_MODEL`（默认 `deepseek-chat`）。

## 结构

| 文件 | 职责 |
|---|---|
| `agent.py` | 主循环、上下文压缩、trace 记录、CLI |
| `tools.py` | 5 个工具的 Schema 与实现 + 安全边界 |
| `llm.py` | OpenAI 兼容客户端（urllib，无依赖）+ 离线 Mock |
| `test_agent.py` | 10 个单元测试：沙箱、注入、唯一替换、循环、轮数上限、上下文压缩 |

## 核心机制

### 1. Agent 循环

```
最多 12 轮:
    发消息（system + 任务 + 历史）给模型，带上工具定义
    如果模型返回 tool_calls:
        逐个执行 → 结果作为 role=tool 的消息追加
    否则:
        输出最终答案，结束
```

**和普通补全的区别**：补全是"给上下文，产一段文本"；这里是"给目标，模型自己决定下一步动作"，
文本输出换成了结构化的工具调用，执行结果再回到模型手里。

### 2. 上下文压缩（`build_context`）

保留 `system` + 首条任务 + **最近若干轮**，超出预算（24000 字符）时丢弃中间并插入一条占位说明。
工具输出另有 3000 字符截断。

> 取舍：工业实现会用摘要压缩（把中间轮次总结成一段），我这里用"保留头尾"的朴素策略——
> 便宜、可预测，代价是可能丢掉早期的关键上下文。

### 3. 安全边界（`tools.py`）

| 风险 | 措施 |
|---|---|
| 路径逃逸 | 所有路径 `resolve()` 后必须在工作目录内，`../` 直接拒绝 |
| 命令注入 | 白名单（ls/cat/grep/wc/git/python…）+ **禁止 `;` `|` `&&` `>` `` ` `` `$(`** + `shell=False` |
| 误改代码 | `replace_in_file` 要求 `old` 在文件中**唯一出现**，否则报错让它自己细化 |
| 死循环/超支 | 最大轮数、单次命令 20s 超时、工具输出截断 |
| 工具异常打死 Agent | 所有工具异常都转成结构化错误回喂模型 |

### 4. 可观测性

每一轮打印：思考内容、调用的工具与参数、执行耗时与结果首行。
完整 trace 落盘为 `trace_*.json`（含每轮的 ok / 耗时 / 结果片段）。

## 演示输出（离线 mock）

```
── 轮次 1 ──  调用: list_files({"dir": "."})        → ok  demo_app.py  83B
── 轮次 2 ──  调用: read_file({"path": "demo_app.py"}) → ok  (共 6 行，显示 1-6)
── 轮次 3 ──  调用: search({"pattern": "def \w+"})    → ok  demo_app.py:1: def main():
── 轮次 4 ──  调用: run_shell({"cmd": "rm -rf demo_app.py"})
                  → ERR 命令包含禁止的内容 'rm '，已拒绝执行
── 轮次 5 ──  [完成] demo_app.py 一共 6 行，包含 main 入口；危险命令已被白名单拒绝
```
