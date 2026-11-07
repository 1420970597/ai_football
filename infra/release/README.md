# 只读正式发布计划

`stable_plan` 是可执行的本地计划和复核工具，不构建、不连接 GitHub、不打标签、不上传镜像或 APK、不创建正式 Release。没有签名身份、实际设备和真实业务证据时明确拒绝。

在受限 Docker 容器、项目工作目录下执行 `python -m infra.release.stable_plan plan`，必须给出 `--manifest`（签名清单）、`--apk`、`--package-output`、`--worktree-status`、`--image-inspect`、`--evidence`、`--notes`。输出 JSON 可重定向到项目内私有目录；随后同样参数运行 `validate --plan 项目内计划路径`，重新验证每项当前输入并逐字段比对，不能仅信任先前的通过标记。所有文件必须是项目内非符号链接。

镜像输入以六个组件名为键，每项包含 `reference`（带摘要的 ghcr 引用）和 `inspection`（拉取后 `docker image inspect` 的单个原始对象）。工具要求精确组件仓库、完整 RepoDigests、内容 ID，以及与 APK 一致的 revision/version 标签，不把随意填写的可变 tag 当镜像证据。输入不需要也不应包含 registry 凭据。

计划绑定同一正式版本、源码提交、干净工作树、签名 APK 精确摘要、六镜像摘要、固定 Hermes 镜像、Hermes 项目上下文/技能/运行器/Compose 摘要、当前全部迁移摘要和发布说明摘要。八类实际证据继续使用现有门禁，两个 Android 域必须对应当前精确签名 APK。任何缺项、失败、过期、候选版本、脏树、改动后的产物或配置都会拒绝。

计划始终为 `publication_state=validated_dry_run`、`published=false`、`production_ready=false`。本工具验证已提供的本地产物和原始记录，不独立证明 registry 当前可拉取、证据本身为真实环境、标签不存在或最终安装成功。后续正式受保护编排仍需执行清洁部署、只读业务验收、远端标签检查、不可变发布与资产读回，不得把 dry-run JSON 当实际 Release。
