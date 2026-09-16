# 11 - 交付：resume-checkpoint 全组审查+提交

**构建什么.** 全部实现票 resolved 后领这张：对整组 diff 跑 code-review（编码规范轴 + spec 轴，spec 为 .scratch/resume-checkpoint/spec.md），修完过修复回审一轮，提交到当前分支。这张票的存在让「整组审过了」成为可判的文件事实：open = 整组未审，resolved = 已收口。

**阻塞于.** 01, 02, 03, 04, 05, 06, 07, 08, 09, 10

**状态.** ready-for-agent

- [ ] 双轴（编码规范轴 + spec 轴）code-review 报告已出，覆盖 01–10 的全部 diff
- [ ] 审查发现的问题已修复，并完成一轮修复回审
- [ ] 整组 diff 已提交到当前分支，提交信息引用 ADR-0022 与本 feature
