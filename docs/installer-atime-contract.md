# 安装器读取 atime 契约与尚未通过的安全门

**这份后续修复仍是草稿，不是已证明安全的 macOS 修复。** PR #30 已由维护者合并到 `main`（`9318f16a11c581661913d17b7371a108dc0e5154`）；本次工作从该提交另开分支，不修改已合并的 PR。允许读取自然更新 atime 可以消除原来的读后恢复时间问题，但不能解决原子交换本身与严格 ctime 契约之间的冲突。

## 唯一获准的副作用

预检、比较、提案预览或读取现有 `AGENTS.md`、Codex/Claude settings 时，文件系统可能自然更新访问时间 **atime**。这也包括用户按 **N**、空输入、EOF、没有终端、提案不需要改动或安全检查最终拒绝更新的情况。noatime 挂载可能不更新它；安装器不要求 atime 一定发生变化。

安装器不恢复任何既有 inode 的 atime，不尝试设置 ctime，也不在读后采样一个新 ctime 来重新授权原件。生成新 staging inode 时，仅按原件设置 mtime；它使用自身当前 atime，而不重用原件 atime。这个规则不授权修改用户业务内容或任何其他元数据。

## 必须继续比较的版本

读取前后和确认前的版本校验只排除 atime，继续包含设备/inode、链接数、文件类型/模式、owner/group、size、内容 hash、mtime、**ctime**、全部可枚举 xattrs，以及 Darwin ACL 和 BSD flags；确认还绑定父目录身份。任何读取错误、枚举不完整、字段变化或 staging 校验不一致仍拒绝。

预检的两套 JSON 读取使用同一 helper，不再各自 `utime`。确认基线保存原 bytes 和完整版本；完整 diff 显示前后绑定原始副本与 proposed staging 的版本。CLI 应用还要复核该展示记录，拒绝显示后被改写、恢复内容但 ctime 已变化、替换 inode 或丢失记录的提案。应用原件和最终新 inode 的路径/版本检查在 staging 与 journal 写入后再次执行，而不是把准备 staging 的时间当作已验证的窗口。

## Darwin 元数据不是只有 xattrs

ACL 使用 [Apple 的原生 fd 接口和二进制格式](https://github.com/apple-oss-distributions/Libc/blob/main/include/sys/acl.h)读取、复制和重新验证，包含 ACL-wide flags、ACE flags、权限、GUID 及顺序。只有原生 `ENOENT` 被明确表示为无 ACL；其他错误不是空 ACL。传给无长度参数的 `acl_copy_int_native` 前，验证序列化 header、条目数和完整长度。若新 staging 继承了 ACL，而原件没有 ACL，当前实现不能证明安全移除时直接拒绝，不把空 ACL 当作 absence。

BSD flags 单独读取和验证。当前只允许安全 staging 的 `UF_NODUMP`、`UF_HIDDEN` 组合；immutable、append、nounlink、压缩及未知/system flags 不被悄悄清零，而是拒绝该更新。缺少 Darwin ACL/flags API 同样拒绝。

全部 xattrs 经 fd 原生后端完整读取，包括 binary、empty、quarantine 和 `com.apple.ResourceFork`。`com.apple.decmpfs` 仍拒绝，不能将旧压缩元数据套在新 payload 上。ACL、flags、xattrs 及资源 fork 的失败和并发变化不能用“内容相同”覆盖。这里列出的是具体已检查的元数据，不是对所有文件系统私有字段或未来 macOS 元数据的全覆盖证明。

## 尚未解决：交换后不能归因的 ctime

原协议已有一个专用于交换后的 ctime 例外：rename/exchange 自己会改变 ctime，因此交换后比较 active/displaced inode 时没有要求历史 ctime 相等。本次将其显式限定为 `allow_exchange_ctime=True`，默认身份比较仍严格；**显式命名这个旧例外，并不使它满足“只忽略 atime”的新要求。**

`tests/test-installer-ctime-gate.py` 给出确定性的独立进程反例：

1. 用户确认的原版本通过全部最后复核，staging 和 journal 均已完成。
2. 在进入真实 exchange 前，一个相同 UID、不合作的子进程改写同长度 bytes，随后恢复原 bytes 和 mtime。
3. 完整快照证明内容、inode、links、模式、owner、mtime、xattrs、ACL 和 flags 均与提案相同，唯一受保护的差异是 **ctime**。
4. 真实原子 exchange 仍执行，旧的交换后 ctime 例外会把该历史变化一起接受并报告 `APPLIED`。

这项测试要求拒绝，当前实现会失败；它是正常阻断 CI 的安全门，不标记为 expectedFailure，也不使用 continue-on-error。普通安装或 atime 用例通过不等于这个门通过。

再加一次 `fstat`、保留 fd、更随机的名称、0700 临时目录、协作锁或事后重复 hash，都不能使“最后版本检查 + 名称交换”成为针对不合作写者的原子版本 CAS。[macOS rename 手册](https://keith.github.io/xcode-man-pages/rename.2.html)没有提供预期 ctime/内容版本作为交换条件。也不能把交换后新 ctime 宣称为只有安装器造成。

## 恢复与下一步

普通身份记录新增持久 ctime、link count 和 Darwin 元数据，恢复检查继续严格。安装器自己 publish/link/rename 也可能改变 ctime；遇到历史 expected 不再匹配时保留 active、staging/recovery entry 和 journal，报告需要人工检查，而不是忽略 ctime 后删除或移动文件。确认交换产生的旧 inode 备份同样不自动删除或交换回去；已打开的写 fd 仍可能继续写它。

在严格 ctime/不合作同 UID 写者的契约下，当前草稿不得合并或称为修复完成。安全出口是证明一个有明确版本条件的原子发布原语，或者另行取得更窄的威胁模型/管理前提授权；不能把用户对 atime 的同意解释成对 ctime 例外、合作写者或元数据缺口的同意。

## 如何验证

- `bash tests/test-install.sh`：完整 Linux 安装/预检/提案/重试/恢复回归，包含新增 atime 与交互用例。
- `python3 tests/test-installer-ctime-gate.py -v`：严格安全门，Linux/macOS 独立运行；当前应暴露上述拒绝要求未满足，而不是被普通正向结果掩盖。
- 普通 macOS CI：yes/no/EOF/no-TTY、展示后版本变化、native xattrs API 缺失、ACL/flags/ResourceFork、独立同 UID 修改并恢复、重试与保守恢复。
- `bash tests/run-macos-noatime.sh`：只在测试 runner 上创建并挂载私有 APFS 镜像，检查挂载标志和真实 read 前后 atime 后，执行相同功能回归；无法创建/验证真实 noatime 卷就是失败，不以模拟代替。

所有实验只使用隔离 fixture 和托管 CI runner，不访问用户 Mac 或真实目标项目。真实运行链接和未解决的失败以本次新 PR 检查结果为准；Linux 上 Darwin 专用 skip 不算 macOS 通过。
