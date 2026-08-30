# Issue tracker：本地 Markdown

这个仓库的 issue 和 spec 作为 markdown 文件存在 `.scratch/` 下。

## 本仓库特有约定

**`.scratch/` 已加入 `.gitignore`**——这是对公开 fork 的有意偏离（默认设计是随 git 协作提交）。原因：地图和票会引用内部网关地址、API key 位置、计费分析等不适合进公开仓库的内容。因此本仓库的 `.scratch/` 是**机器本地数据**，不随 git 同步、不提交、不外传；跨机器不共享。

## 约定

- 一个 feature 一个目录，即 `.scratch/<feature-slug>/`
- spec 是 `.scratch/<feature-slug>/spec.md`
- 实现 issue 一个 ticket 一个文件，放在 `.scratch/<feature-slug>/issues/<NN>-<slug>.md`，从 `01` 编号，绝不要写成单个合并的 tickets 文件
- Triage 状态记录为每个 issue 文件顶部附近的 `Status:` 行（角色字符串见 `triage-labels.md`）。改状态用 `set_ticket_status` 工具，不要手写 `Status:` 行——工具保证格式对齐 indexer 解析、校验合法枚举、触发 frontier/看板刷新。`claimed`/`resolved` 有专用工具 `ticket.claim`/`ticket.resolve` 优先用专用工具。这个工具不常驻在工具列表里（触发频率低于 wayfinder/domain，改用 `mcp__ext__invoke` 调用），字段照抄：

  ```jsonc
  {
    "name": "matt-flow",
    "method": "ticket.setStatus",
    "args": {
      "workspace": "<项目根绝对路径>",
      "feature": "<所属分组>",
      "number": "<ticket 编号，如 01>",
      "status": "needs-triage" // 合法值：needs-triage / needs-info / ready-for-agent / ready-for-human / claimed / resolved / wontfix，非法值会抛错
    }
  }
  ```

- 评论和对话历史追加到文件底部 `## Comments` 标题下——triage 场景用 `append_triage_comment` 工具（自动前置 AI 免责声明），其他场景直接 Write/Edit 追加。同样不常驻，走 `mcp__ext__invoke`：

  ```jsonc
  {
    "name": "matt-flow",
    "method": "triage.appendComment",
    "args": {
      "workspace": "<项目根绝对路径>",
      "feature": "<所属分组>",
      "number": "<ticket 编号，如 01>",
      "body": "<评论正文，不含免责声明，工具自动前置>"
    }
  }
  ```

  两个方法缺字段都会抛"缺必填字段 X"，照 reason 补齐重试即可，不会静默把 `undefined` 写进文件。

## 当技能说「发布到 issue tracker」时

在 `.scratch/<feature-slug>/` 下创建一个新文件（需要的话连目录一起建）。

## 当技能说「取相关 ticket」时

读取被引用路径的文件。用户通常会直接传路径或 issue 编号。

## Wayfinding 操作

`matt-flow:wayfinder` 用。**map** 是一个文件，每个 ticket 一个**子**文件。

- **Map**，`.scratch/<effort>/map.md`，正文七个区：Destination（走到尽头是什么样）、Notes（领域/技能/常驻偏好）、Lighthouses（外部参考）、Shoals（自己撞过、会被重复撞到的坑）、Decisions so far（已关闭 ticket 的决策索引）、Not yet specified（战争迷雾，能感知但还无法 ticket）、Out of scope（范围外，永不毕业）。用 `create_wayfinder_map` 工具创建骨架（保证七区标题完整，indexer 按这些 `##` 标题解析），不要手写整个 map.md。map.md 已存在但缺几个分区（早期版本、或不是 create_wayfinder_map 建的旧图）时，用 `backfill_map_sections` 工具补齐——已有分区和内容一字不动，只补缺的空区；不要手写 map.md 去补分区标题，那样容易把内容写进错误的区。
- **子 ticket**，`.scratch/<effort>/issues/NN-<slug>.md`，从 `01` 编号，问题写在正文里。`Type:` 行记录 ticket 类型（`research`/`prototype`/`grilling`/`task`），`Status:` 行记录 `claimed`/`resolved`。
- **Blocking**，顶部附近的 `Blocked by: NN, NN` 行。一个 ticket 在它列出的每个文件都是 `resolved` 时才算解除阻塞。
- **Frontier**，扫 `.scratch/<effort>/issues/` 找 open、unblocked 且 unclaimed 的文件，编号小的优先。
- **Claim**，把 `Status:` 设成 `claimed` 并保存，再开始任何工作。
- **Resolve**，调 `resolve_wayfinder_ticket` 工具，一次调用做完三步：在 `## Answer` 标题下写答案、把 `Status:` 设成 `resolved`、往 `map.md` 的 Decisions-so-far 追加一个上下文指针（用 ticket 标题指代，不用裸编号）。不要自己用 Write/Edit 分三次手写——容易漏一步，或者答案分区和决策指针的格式跟 indexer 解析的不一致。

  参数：`workspace`/`feature`/`number`/`answer`/`gist`，外加必填的 `elevation`——**决策提升阀门**，必须显式声明这张票的决策是否已提升到 ADR，不声明会被工具硬拦。ADR 三条件（见 `domain-modeling` SKILL）：难逆转 / 没上下文会显得意外 / 真实权衡有替代方案。
  - 三条都满足的不可逆决策：先用 `commit_adr` 落盘成 ADR 拿到编号，再填 `elevation: { elevated: true, adrRefs: ["0003"] }`（编号不存在会被拦）。
  - 不满足三条件的普通决策：填 `elevation: { elevated: false, rationale: "为什么不需要 ADR，对照三条件说缺哪条" }`（rationale 为空会被拦）。
