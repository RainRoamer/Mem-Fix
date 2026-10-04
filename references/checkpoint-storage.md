# 检查点保存与恢复

有 Python 3.9+ 和文件执行能力时，使用 [checkpoint.py](../scripts/checkpoint.py)。脚本仅依赖 Python 标准库；跨模型规则本身不要求安装 Python。脚本保护交接文档，不备份或恢复项目代码。

## 保存

把下列命令中的脚本、项目与候选文件路径换成实际路径。命令示例适用于常见终端；Python 可执行命令由环境决定。

```text
python "<skill>/scripts/checkpoint.py" inspect "<project>/docs/HANDOFF.md"
```

读取正式交接文件，保留这次 `inspect` 返回的 `sha256`；文件不存在时值为 `missing`。把合并后的完整内容写到单独的候选文件，例如 `HANDOFF.task-a.draft.md`。不要先直接编辑正式文件再调用脚本，否则已失去原快照。

人工核对候选内容中的任务状态、要求和实际验证结果后保存：

```text
python "<skill>/scripts/checkpoint.py" save "<project>/docs/HANDOFF.md" --source "<project>/docs/HANDOFF.task-a.draft.md" --expect "<inspect 返回的 sha256 或 missing>"
```

每次保存的行为：

1. 检查候选文件是非空、可解码的 UTF-8 Markdown，并含标题；不验证内容真实性。
2. 获取操作系统文件锁，核对正式文件是否仍与之前读取的哈希相同。
3. 在目标同目录写入临时文件，刷新到磁盘并逐字节校验。
4. 将原正式文件另存并替换到 `HANDOFF.md.prev`，然后用 `os.replace` 替换正式文件。
5. 重新读取正式文件，确认与候选内容一致，再报告成功。

首次保存没有上一份备份；内容相同时不旋转备份。`.prev` 保留最近一次被替换的可读取快照，脚本无法证明它的语义正确。正式文件已经损坏时，普通保存拒绝旋转备份，以保护仍可用的 `.prev`。

哈希不符时重新读取并合并，不仅仅刷新 `--expect` 后强行覆盖。锁被占用时稍后重试或使用独立任务文件，不删除锁文件来绕过冲突。

## 恢复

正式文件损坏、缺失或需要回到上一份交接时，先查看 `.prev`，确认它属于要恢复的任务。恢复会替换正式交接文档，可能丢弃较新的记录，应先保留仍需参考的内容。

```text
python "<skill>/scripts/checkpoint.py" inspect "<project>/docs/HANDOFF.md.prev"
python "<skill>/scripts/checkpoint.py" inspect "<project>/docs/HANDOFF.md"
python "<skill>/scripts/checkpoint.py" restore "<project>/docs/HANDOFF.md" --expect "<正式文件的 sha256 或 missing>"
```

恢复使用相同的锁、临时写入和原文件哈希检查；保持 `.prev` 不变，避免用损坏的正式文件覆盖有效备份。没有可读取的备份时停止恢复，只根据已知代码和事实重建记录，明确存在信息缺口。

## 保护边界与副产物

- 同目录替换通常能让本地文件系统上的读者看到完整旧文件或完整新文件。网络盘、同步盘、磁盘故障与突然断电的行为取决于环境；文件刷新不构成断电零丢失保证。
- 文件锁只约束使用这个脚本的写入者。编辑器或其他不遵守锁的程序仍可能竞争；脚本会检查可观察到的变更，但不能消除最后检查与替换之间的全部外部竞争。
- 操作系统会在进程结束后释放锁。`HANDOFF.md.lock` 文件有意保留，不代表仍被占用；不把它当成交接内容，不靠删除它解锁。
- 硬中断可能留下 `.HANDOFF.md.mem-fix-*.tmp` 和候选文件。它们不是正式检查点；仅在确认无写入者工作且正式记录已核对后清理属于自己的残留。
- 仅整理文档不需要自动修改 `.gitignore`；需要提交时，显式选择正式交接文件和必要证据，不把锁、临时文件或候选文件混入。
- Python 不可用时，可使用所在工具明确支持的同目录替换和备份能力；否则保留上一份记录、写入候选、读取校验后再替换，并注明不具备脚本的锁和冲突保护。不能安全替换时交付候选路径或完整文本，不声称正式文件已保存。

## 多任务同时进行

已知多个任务并行时，各自使用 `docs/handoffs/<task-id>.md` 和独立候选文件、检查点编号；相应 `.prev` 与 `.lock` 自动独立。`docs/HANDOFF.md` 可由一个协调者维护任务路径索引。其他任务直接读取各自文件，不同时改索引。

独立交接文件减少记录覆盖，不能解决产品代码的并发修改；仍需明确文件归属，必要时使用项目已有的分支或独立工作区方案。不要仅为了启用记忆而自动创建多模型任务。

## 维护脚本时的验证

[行为测试](../scripts/tests/test_checkpoint.py) 在临时目录运行，覆盖保存与备份、冲突拒绝、损坏恢复、并发锁、替换前后进程中断，以及同一 HEAD 下的内容变化识别。修改脚本后执行：

```text
python -B -m unittest discover -s "<skill>/scripts/tests" -v
```

2026-10-05 已在 Windows、Python 3.14.7 上通过 14 项测试；这验证脚本的本地行为，不代表已经验证所有模型的遵守情况、其他操作系统或真实项目的完整接手流程。
