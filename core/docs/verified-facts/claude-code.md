# Claude Code CLI

confidence tag の凡例: [README](README.md)。

- permission パス: 先頭 **単一 `/` は project-root 相対**、**絶対 FS パスは `//`** が要る
  (例 `//workspace/...`)。session が `/workspace` で動くと単一 `/workspace` は
  `/workspace/workspace/...` を指してしまう。 `[docs][review]`
- `--dangerously-skip-permissions` (bypassPermissions) はツール確認を飛ばすが
  **folder-trust dialog は飛ばさない**。`--safe-mode` も trust dialog は飛ばさない
  (`--safe-mode` の役割は project の `.claude/` 設定 = hooks/plugins/CLAUDE.md/skills/commands の無効化)。
  `[docs][empirical]`
- folder trust の事前許可は `.claude.json` の `projects.<path>.hasTrustDialogAccepted=true`
  (+ `projectOnboardingSeenCount >= 1`)。**注意:** `CLAUDE_CONFIG_DIR` が `.claude.json` 自体を移すのか
  `.claude/` ディレクトリだけ移すのかは**公式に未文書**。取り違えると初回起動が trust dialog で停止する
  ので、`$CLAUDE_CONFIG_DIR/.claude.json` と node home の `~/.claude.json` の**両方**に種を書く
  (`entrypoint.sh` の setup_home)。 `[docs(一部)][empirical]`
- `disableBypassPermissionsMode: "disable"` (settings) は `--dangerously-skip-permissions` を
  **無効化**する (任意スコープから効く)。bypass mode に依存するなら設定しない。 `[docs][review]`
- `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` は Anthropic 認証情報 (`ANTHROPIC_API_KEY` /
  `CLAUDE_CODE_OAUTH_TOKEN` 等) を**ツール子プロセスの env からのみ**除去する。main claude プロセスの
  env や `/proc/<pid>/environ` は対象外 (→ SECURITY-MODEL.md の不変条件 2)。 `[docs]`
- `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` の scrub/隔離は **bubblewrap 必須**。bwrap が無い/使えない環境
  では claude が起動時に `error: bubblewrap is required for subprocess env scrubbing and isolation`
  で**落ちる**。redact サンドボックスは `cap_drop: ALL` で `CAP_SYS_ADMIN` を持たず、setuid でも
  unprivileged userns でも bwrap が user namespace を作れないため、**`=0`** で無効化する。scrub は
  不変条件 2 のとおり同一 uid 子には実効が無く、cred の実保護は egress 固定 + ephemeral home +
  短命 access token が担うので、これは防御の後退にならない。 `[empirical(起動エラー)]`
- claude の **Bash サンドボックス** (`sandbox.enabled`, settings) は scrub とは別機構で、Linux では
  同じく bubblewrap を使う。既定は `false`。**bwrap 欠如時は既定で warn して継続** (sandbox 無しで実行)
  し、managed settings の `sandbox.failIfUnavailable: true` の時だけ起動を中断する。非特権コンテナでは
  bwrap が新しい `/proc` を mount できず、`enableWeakerNestedSandbox: true` が要るが docs 自身が
  「considerably weakens security」と注意する。本サンドボックスは OS mount + firewall が封じ込めの実体
  なので claude の Bash sandbox は使わず、将来 default が変わっても bwrap 依存が復活しないよう settings
  で `sandbox.enabled: false` を明示 pin する。 `[docs: code.claude.com/docs/en/sandboxing]`
- `.devcontainer/core/claude/*.json` の設定キー検証 (schemastore `claude-code-settings.json`。root は
  **`additionalProperties: true`** なので未収載キーは弾かれず、`$schema` を付けても既知キーの enum 検証
  + 補完だけ効く): `tui` = `"fullscreen"|"default"`、`effortLevel` = `low|medium|high|xhigh|max`、
  `permissions.defaultMode` = `default|acceptEdits|plan|auto|dontAsk|bypassPermissions|delegate`。
  `includeCoAuthoredBy` は valid だが **deprecated** (後継 `attribution`)。`theme` /
  `remoteControlAtStartup` / `inputNeededNotifEnabled` / `agentPushNotifEnabled` は公式 docs にはあるが
  schema 未収載 (additionalProperties:true なので問題にならない)。 `[docs][schema]`
